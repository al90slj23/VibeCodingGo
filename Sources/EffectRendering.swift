import AppKit
import Foundation
import Metal
import QuartzCore

/// One process-wide pipeline. Compilation, drawable waits and encoding never
/// run on the main thread; a view has at most one submitted frame.
final class CueEffectGPU {
    struct Context { let device: MTLDevice; let queue: MTLCommandQueue; let pipeline: MTLRenderPipelineState }
    static let shared = CueEffectGPU()
    private let preparation = DispatchQueue(label: "local.vibecue.effect-pipeline", qos: .utility, autoreleaseFrequency: .workItem)
    private var context: Context?
    private(set) var failureDescription: String?
    private var prepared = false
    private init() {}
    func prepare(_ completion: @escaping (Context?) -> Void) {
        preparation.async {
            if self.prepared { completion(self.context); return }
            self.prepared = true
            if let device = MTLCreateSystemDefaultDevice(), let queue = device.makeCommandQueue() {
                do {
                    let library = try device.makeLibrary(source: Self.shader, options: nil)
                    let descriptor = MTLRenderPipelineDescriptor()
                    descriptor.vertexFunction = library.makeFunction(name: "cue_vertex")
                    descriptor.fragmentFunction = library.makeFunction(name: "cue_fragment")
                    descriptor.colorAttachments[0].pixelFormat = .bgra8Unorm
                    self.context = Context(device: device, queue: queue, pipeline: try device.makeRenderPipelineState(descriptor: descriptor))
                } catch { self.context = nil; self.failureDescription=String(describing:error) }
            }
            completion(self.context)
        }
    }
    static func uniforms(style: CueVisualStyle, canvas: NSSize, origin: NSPoint, viewport: NSSize, time: TimeInterval, duration: TimeInterval?, reduceMotion: Bool) -> [Float] {
        let recipe = CueEffectCatalog.preset(id: style.effectID).recipe
        let motion = recipe.motion.sample(at: time, speed: style.speed, direction: style.direction, reduceMotion: reduceMotion)
        var values = Array(repeating: Float(0), count: 144)
        func set(_ index: Int, _ value: Double) { values[index] = Float(value) }
        set(0,canvas.width); set(1,canvas.height); set(2,origin.x); set(3,origin.y)
        set(4,viewport.width); set(5,viewport.height)
        set(9,style.width); set(10,style.inset); set(11,style.opacity); set(12,style.trail)
        set(13,style.presence.coreScale); set(14,style.presence.peakGain); set(15,style.presence.motionScale)
        set(16,style.presence == .gentle ? 0 : style.presence == .clear ? 1 : 2)
        set(17,motion.phase); set(18,motion.strength * recipe.envelope.opacity(at: time,duration: duration,reduceMotion: reduceMotion)); set(19,motion.sign)
        set(20,recipe.path == .perimeter ? 0 : recipe.path == .topEdge ? 1 : 2)
        set(21,Double(min(4,recipe.layers.count))); set(22,Double(style.palette.count))
        for (index,color) in style.palette.enumerated() {
            let rgb = color.usingColorSpace(.sRGB) ?? .white
            set(24+index*4,rgb.redComponent); set(25+index*4,rgb.greenComponent); set(26+index*4,rgb.blueComponent)
        }
        let m = recipe.material ?? CueEffectMaterial()
        let material = [m.body,m.occlusion,m.specular,m.bloom,m.reflection,m.roughness,m.whiteness,m.dispersion,m.bodyWidth,m.edgeSoftness,m.doubleRefraction,m.reflectionOffset]
        for (index,value) in material.enumerated() { set(56+index,value) }
        let main = recipe.layers.filter { $0.opacity >= 0.25 }
        set(68,max(0.001,main.map(\.width).max() ?? 1)); set(69,max(0.001,main.map(\.opacity).max() ?? 1))
        for (index,layer) in recipe.layers.prefix(4).enumerated() {
            let brush: Double
            switch layer.brush { case .ribbon: brush=0; case .line: brush=1; case .beam: brush=2; case .dashes: brush=3; case .points: brush=4; case .squares: brush=5; case .wave: brush=6; case .ripple: brush=7 }
            let components = [brush,layer.width,layer.minimumWidth,layer.opacity,layer.offset,Double(layer.count),layer.length,layer.phase,layer.colorOffset,layer.glow,layer.frequency,layer.amplitude,layer.movement]
            for (offset,value) in components.enumerated() { set(72+index*16+offset,value) }
        }
        return values
    }
    struct UniformTemplate {
        let base:[Float]
        let motion:CueEffectMotion
        let envelope:CueEffectEnvelope
        let speed:Double
        let direction:String
        func frame(origin:NSPoint,viewport:NSSize,time:TimeInterval,duration:TimeInterval?,reduceMotion:Bool) -> [Float] {
            var values=base
            values[2]=Float(origin.x);values[3]=Float(origin.y)
            values[4]=Float(viewport.width);values[5]=Float(viewport.height)
            let sample=motion.sample(at:time,speed:speed,direction:direction,reduceMotion:reduceMotion)
            values[17]=Float(sample.phase)
            values[18]=Float(sample.strength * envelope.opacity(at:time,duration:duration,reduceMotion:reduceMotion))
            values[19]=Float(sample.sign)
            return values
        }
    }
    static func uniformTemplate(style:CueVisualStyle,canvas:NSSize) -> UniformTemplate {
        let recipe=CueEffectCatalog.preset(id:style.effectID).recipe
        let base=uniforms(style:style,canvas:canvas,origin:.zero,viewport:canvas,time:0,duration:nil,reduceMotion:true)
        return UniformTemplate(base:base,motion:recipe.motion,envelope:recipe.envelope,speed:style.speed,direction:style.direction)
    }
    static func encode(_ values: [Float], texture: MTLTexture, context: Context) -> MTLCommandBuffer? {
        guard let command = context.queue.makeCommandBuffer() else { return nil }
        let descriptor = MTLRenderPassDescriptor()
        descriptor.colorAttachments[0].texture = texture
        descriptor.colorAttachments[0].loadAction = .clear; descriptor.colorAttachments[0].storeAction = .store
        descriptor.colorAttachments[0].clearColor = MTLClearColorMake(0,0,0,0)
        guard let encoder = command.makeRenderCommandEncoder(descriptor: descriptor) else { return nil }
        encoder.setRenderPipelineState(context.pipeline)
        values.withUnsafeBytes { bytes in
            if let base = bytes.baseAddress { encoder.setFragmentBytes(base,length: bytes.count,index:0) }
        }
        encoder.drawPrimitives(type: .triangle,vertexStart:0,vertexCount:3)
        encoder.endEncoding(); return command
    }
    static let shader = """
#include <metal_stdlib>
using namespace metal;
struct Vertex { float4 position [[position]]; float2 uv; };
vertex Vertex cue_vertex(uint id [[vertex_id]]) {
    float2 positions[3] = {float2(-1,-1),float2(3,-1),float2(-1,3)};
    Vertex v; v.position=float4(positions[id],0,1); v.uv=(positions[id]+1)*0.5; return v;
}
float wrap(float x) { return x-floor(x); }
float signedWrap(float x) { return wrap(x+0.5)-0.5; }
float haloSpread(float width, float presence) {
    return min(28.0,max(presence<0.5?4.5:presence<1.5?8.0:11.0,width*(presence<0.5?1.7:presence<1.5?2.7:3.8)));
}
float3 tint(float position, constant float *u) {
    int count=int(u[22]); float value=wrap(position)*count; int low=int(value)%count;
    float3 a=float3(u[24+low*4],u[25+low*4],u[26+low*4]); int next=(low+1)%count;
    float3 b=float3(u[24+next*4],u[25+next*4],u[26+next*4]); return mix(a,b,value-floor(value));
}
void over(thread float4 &sample, float3 color, float alpha) {
    alpha=clamp(alpha,0.0,0.985); sample=float4(color*alpha+sample.rgb*(1-alpha),alpha+sample.a*(1-alpha));
}
float lineMask(float d,float width,float aa) { return 1-smoothstep(max(0.0,width*0.5-aa),width*0.5+aa,d); }
void paintLine(thread float4 &sample,float distance,float width,float alpha,float colorPosition,float glow,float aa,constant float *u) {
    float spread=haloSpread(min(width,u[9]*1.5),u[16]); float d=abs(distance);
    if(d>width*0.5+spread+aa || alpha<=0.00001) return;
    float3 c=tint(colorPosition,u);
    for(int j=0;j<3;j++) { float radius=j==0?1.0:j==1?0.65:0.32; float strength=j==0?0.035:j==1?0.055:0.075;
        over(sample,c,alpha*u[11]*strength*(0.65+glow)*lineMask(d,width+spread*radius*2,aa)); }
    over(sample,float3(0),min(0.28,alpha*u[11]*0.20)*lineMask(d,width+1.4,aa));
    over(sample,c,alpha*u[11]*lineMask(d,width,aa));
}
fragment float4 cue_fragment(Vertex in [[stage_in]],constant float *u [[buffer(0)]]) {
    if(u[18]<=0.00001) return float4(0);
    float2 p=in.uv*float2(u[4],u[5])+float2(u[2],u[3]);
    float width=min(u[9]*u[13],min(u[0],u[1])/5); float spread=haloSpread(width,u[16]);
    float inset=min(u[10]+width*0.6+min(8.0,spread*0.35),max(0.0,min(u[0],u[1])*0.5-3));
    float2 center=float2(u[0],u[1])*0.5, halfSize=center-inset;
    float radius=max(1.0,min(22.0,min(halfSize.x,halfSize.y)*0.4));
    float horizontal=max(0.0,halfSize.x*2-radius*2), vertical=max(0.0,halfSize.y*2-radius*2), arc=M_PI_F*radius*0.5;
    float perimeter=max(1.0,2*horizontal+2*vertical+4*arc);
    float2 local=p-center, side=select(float2(-1),float2(1),local>=0), q=abs(local)-(halfSize-radius);
    float2 out=max(q,float2(0)); float distance=length(out)+min(max(q.x,q.y),0.0)-radius;
    float2 outward;
    if(q.x>0 && q.y>0) outward=normalize(out)*side;
    else outward=q.x>q.y?float2(side.x,0):float2(0,side.y);
    float2 nearest=p-outward*distance;
    float t=0;
    if(q.x>0 && q.y>0) {
        float angle=atan2(abs(nearest.y-center.y)-(halfSize.y-radius),abs(nearest.x-center.x)-(halfSize.x-radius));
        if(side.x>0 && side.y>0) t=horizontal+radius*(M_PI_F*0.5-angle);
        else if(side.x>0) t=horizontal+arc+vertical+radius*angle;
        else if(side.y<0) t=horizontal*2+arc*2+vertical+radius*(M_PI_F*0.5-angle);
        else t=horizontal*2+arc*3+vertical*2+radius*angle;
    } else if(outward.y>0) t=nearest.x-inset-radius;
    else if(outward.x>0) t=horizontal+arc+(u[1]-inset-radius-nearest.y);
    else if(outward.y<0) t=horizontal+arc*2+vertical+(u[0]-inset-radius-nearest.x);
    else t=horizontal*2+arc*3+vertical+(nearest.y-inset-radius);
    t=wrap(t/perimeter); float radial=-distance; float2 normal=-outward;
    float aa=max(0.35,length(float2(dfdx(radial),dfdy(radial)))*0.65);
    if(u[20]>0.5 && u[20]<1.5) {
        radial=u[1]-inset-p.y; t=(p.x-inset-radius)/max(1.0,horizontal);
        if(t<0 || t>1) return float4(0); normal=float2(0,-1); perimeter=max(1.0,horizontal);
    }
    if(u[20]>1.5) {
        float centers[4]={horizontal+arc*0.5,horizontal+vertical+arc*1.5,horizontal*2+vertical+arc*2.5,horizontal*2+vertical*2+arc*3.5};
        float d=1.0; for(int j=0;j<4;j++) d=min(d,abs(signedWrap(t-centers[j]/perimeter)));
        float span=min(0.055,max(0.008,(18+width)/perimeter)); if(d>span) return float4(0);
    }
    float4 sample=float4(0); float geometry=width*u[15]; float alpha=u[18];
    for(int layer=0;layer<int(u[21]);layer++) {
        int base=72+layer*16; int brush=int(u[base]); float ratio=u[base+3]<0.25?min(4.0,u[base+1]/u[68]):u[base+1]/u[68];
        float lw=max(u[base+2]*u[13],width*ratio), offset=geometry*u[base+4]; int count=int(u[base+5]);
        float phase=u[17]*u[base+12]+u[base+7], la=alpha*min(1.0,u[base+3]/u[69])*u[14], colorOffset=u[base+8];
        if(brush==0) {
            float h0=min(max(1.15,lw*0.70),radius*0.68), extent=max(h0*2.7,haloSpread(lw,u[16]));
            if(abs(radial)>extent+1) continue;
            float behind=wrap((phase-t)*u[19]*u[base+12]); float trail=min(0.82,max(0.18,u[12]*2.4));
            float tail=behind<trail?pow(max(0.0,1-behind/trail),1.55):0;
            float head=exp(-pow(min(behind,1-behind)/0.012,2.0)); float volume=tail+(1-tail)*head;
            float energy=min(1.28,0.004+volume*0.92+head*0.34), specularGate=min(1.15,head*0.95+pow(volume,10.0)*0.35);
            float h=max(0.42,h0*u[64]*(0.12+0.88*pow(volume,0.40))), n=radial/h;
            float colorPosition=t-phase*0.32+u[63]*n*0.16; float3 white=float3(0.98,0.99,1);
            float3 c=mix(tint(colorPosition,u),white,u[62]); float a=la*u[11];
            float localSpill=max(1.4,extent*0.43*(0.24+0.76*pow(volume,0.30)));
            over(sample,c,a*energy*u[59]*exp(-pow(radial/localSpill,2.0)*0.5)*0.46);
            over(sample,mix(tint(colorPosition+0.08,u),white,u[62]*0.5),a*energy*u[60]*exp(-pow((radial-h*u[67])/max(0.52,h*0.30),2.0)*0.5)*0.44);
            over(sample,float3(0.012,0.018,0.028),a*specularGate*u[57]*exp(-pow((radial-h*1.02)/max(0.40,h*0.18),2.0)*0.5)*0.28);
            float sigma=max(0.35,h*u[65]); float density=exp(-pow(radial/sigma,2.0)*0.5);
            float split=max(exp(-pow((radial-h*0.62)/max(0.28,sigma*0.43),2.0)*0.5),exp(-pow((radial+h*0.62)/max(0.28,sigma*0.43),2.0)*0.5));
            float volumeDensity=mix(density,split,u[66]), slope=tanh(n*0.82), z=sqrt(max(0.0,1-slope*slope));
            float light=max(0.18,z*0.82+slope*(normal.x*-0.36+normal.y*0.44));
            over(sample,mix(c*(0.62+0.38*light),white,0.025*z),a*energy*u[56]*volumeDensity);
            float facing=normal.x*-0.6+normal.y*0.8, ridge=-h*(0.27+0.18*facing);
            float specular=exp(-pow((radial-ridge)/max(0.25,h*0.15),2.0)*0.5);
            float second=exp(-pow((radial+ridge)/max(0.22,h*0.12),2.0)*0.5)*u[66]*0.48;
            float gloss=pow(max(0.0,z*0.94+slope*facing*0.18),u[61]/12);
            over(sample,mix(white,c,0.18),a*specularGate*u[58]*(specular+second)*gloss);
        } else if(brush==1 || brush==6) {
            float wave=brush==6?geometry*u[base+11]*sin((t*u[base+10]+phase)*M_PI_F*2):0;
            paintLine(sample,radial-offset-wave,lw,la,t+phase+colorOffset,u[base+9],aa,u);
        } else if(brush==2) {
            float tail=min(0.65,max(0.025,u[base+6]*u[12]/0.18));
            for(int j=0;j<count;j++) { float behind=wrap((phase+float(j)/count-t)*u[19]*u[base+12]);
                if(behind<tail) { float progress=1-behind/tail; paintLine(sample,radial-offset,lw*(0.25+0.75*progress),la*pow(progress,1.65),colorOffset+progress*0.2,u[base+9],aa,u); } }
        } else if(brush==3) {
            int n=count==1?36:count; float cell=wrap((t-phase)*n), length=min(u[base+6],0.65/n)*n;
            if(cell<=length) paintLine(sample,radial-offset,lw,la,floor((t-phase)*n)/n+colorOffset,u[base+9],aa,u);
        } else if(brush==4 || brush==5) {
            float position=round((t-phase)*count)/count+phase, along=signedWrap(t-position)*perimeter;
            float r=max(0.8,lw*0.58), d=brush==5?max(abs(along),abs(radial-offset)):length(float2(along,radial-offset));
            if(d>r+spread+aa) continue;
            float3 c=tint(position+colorOffset,u);
            for(int j=0;j<3;j++) { float scale=j==0?1.0:j==1?0.65:0.32; over(sample,c,la*u[11]*0.035*(1-smoothstep(r+spread*scale-aa,r+spread*scale+aa,d))); }
            over(sample,float3(0),min(0.28,la*u[11]*0.22)*(1-smoothstep(r+0.7-aa,r+0.7+aa,d)));
            over(sample,c,la*u[11]*(1-smoothstep(r-aa,r+aa,d)));
        } else {
            for(int j=0;j<count;j++) { float progress=wrap(phase+float(j)/count);
                paintLine(sample,radial-offset-geometry*progress*1.1,lw,la*(1-progress),float(j)/count+colorOffset,u[base+9],aa,u); }
        }
    }
    return sample;
}
"""
}

/// A cancelled surface cannot enqueue another frame; a busy surface drops
/// requests rather than accumulating animation work behind a slow GPU.
final class CueEffectFrameGate {
    private let lock=NSLock()
    private var busy=false, cancelled=false, suspended=false
    func begin() -> Bool { lock.lock(); defer { lock.unlock() }; guard !busy && !cancelled && !suspended else { return false }; busy=true; return true }
    func end() { lock.lock(); busy=false; lock.unlock() }
    func cancel() { lock.lock(); cancelled=true; lock.unlock() }
    func suspend(_ value:Bool) { lock.lock();suspended=value;lock.unlock() }
    var canPresent:Bool { lock.lock();defer { lock.unlock() };return !cancelled && !suspended }
    var isCancelled: Bool { lock.lock(); defer { lock.unlock() }; return cancelled }
}

/// Each cropped window owns one small drawable pool. Requests coalesce; no
/// timer, queued frame backlog or main-thread nextDrawable wait is introduced.
final class CueEffectGPUFrameDriver {
    private let queue = DispatchQueue(label: "local.vibecue.effect-frame", qos: .utility, autoreleaseFrequency: .workItem)
    private let gate=CueEffectFrameGate()
    private let metalLayer: CAMetalLayer
    private let context: CueEffectGPU.Context
    init(layer: CAMetalLayer, context: CueEffectGPU.Context) { metalLayer=layer; self.context=context }
    func cancel() { gate.cancel() }
    func suspend(_ value:Bool) { gate.suspend(value) }
    @discardableResult func submit(_ values: [Float], completion: @escaping (TimeInterval?) -> Void) -> Bool {
        guard gate.begin() else { return false }
        queue.async { [weak self] in
            guard let self else { return }
            guard self.gate.canPresent, let drawable=self.metalLayer.nextDrawable(), self.gate.canPresent, let command=CueEffectGPU.encode(values,texture:drawable.texture,context:self.context) else {
                self.finish();DispatchQueue.main.async { completion(nil) };return
            }
            command.addCompletedHandler { [weak self] command in
                guard let self else { return };let visible=self.gate.canPresent;self.gate.end()
                let duration=visible && command.status == .completed ? max(0,command.gpuEndTime-command.gpuStartTime) : nil
                DispatchQueue.main.async { completion(duration) }
            }
            command.present(drawable); command.commit()
        }
        return true
    }
    private func finish() { gate.end() }
}


extension CueEffectGPU {
    /// Pure geometry/parameter/admission checks: no device, compilation, window,
    /// process, timer or display request is created by the normal build gate.
    static func invariantReport() -> [String:Bool] {
        let canvas=NSSize(width:2560,height:1440)
        let fallback=NSColor.systemMint
        let styles=[
            CueVisualStyle(fields:["effect":"pulse-fragments","presence":"strong","width":"8","inset":"3"],fallback:fallback),
            CueVisualStyle(fields:["effect":"prism-flow","width":"8","inset":"3"],fallback:fallback),
            CueVisualStyle(fields:["effect":"pearl-flow","width":"8","inset":"3"],fallback:fallback)]
        let rect=NSRect(origin:.zero,size:canvas)
        let viewports=styles.map { CueEdgeEffectView.edgeViewports(size:canvas,style:$0) }
        let gate=CueEffectFrameGate()
        let first=gate.begin(), drop = !gate.begin(); gate.end(); let resumed=gate.begin(); gate.cancel(); gate.end()
        let cancelled = !gate.begin()
        let visibilityGate=CueEffectFrameGate();visibilityGate.suspend(true)
        let hidden = !visibilityGate.begin() && !visibilityGate.canPresent
        visibilityGate.suspend(false);let visible=visibilityGate.begin()
        visibilityGate.suspend(true);let pausedInFlight = !visibilityGate.canPresent
        visibilityGate.end();visibilityGate.suspend(false);let restored=visibilityGate.begin()
        visibilityGate.cancel();visibilityGate.end();visibilityGate.suspend(false)
        let cancelledAfterRestore = !visibilityGate.begin()
        let allStyles=CueEffectCatalog.presets.map { CueVisualStyle(fields:["effect":$0.id,"palette":"#123456,#ABCDEF,#FFAACC,#44FF88,#FF0000,#00FF00,#0000FF,#FFFFFF"],fallback:fallback) }
        let uniforms=allStyles.map { Self.uniforms(style:$0,canvas:canvas,origin:.zero,viewport:canvas,time:1.35,duration:8,reduceMotion:false) }
        return [
            "effect_surfaces_are_nonoverlapping_edge_strips": viewports.allSatisfy { frames in
                frames.count == 4 && frames.allSatisfy { rect.contains($0) } && frames.enumerated().allSatisfy { index,a in frames.dropFirst(index+1).allSatisfy { a.intersection($0).isEmpty } }
            },
            "effect_default_surfaces_leave_video_and_desktop_center_uncovered": viewports.allSatisfy { frames in !frames.contains { $0.contains(NSPoint(x:1280,y:720)) } && frames.reduce(0) { $0+$1.width*$1.height } < canvas.width*canvas.height*0.3 },
            "effect_all_presets_have_finite_bounded_shader_uniforms": uniforms.allSatisfy { $0.count == 144 && $0.allSatisfy(\.isFinite) && (1...4).contains(Int($0[21])) && Int($0[22]) == 8 },
            "effect_reduce_motion_removes_time_from_shader_parameters": allStyles.allSatisfy { Self.uniforms(style:$0,canvas:canvas,origin:.zero,viewport:canvas,time:0,duration:8,reduceMotion:true) == Self.uniforms(style:$0,canvas:canvas,origin:.zero,viewport:canvas,time:100,duration:8,reduceMotion:true) },
            "effect_frame_admission_has_no_backlog_and_stays_cancelled": first && drop && resumed && cancelled,
            "effect_hidden_surfaces_suspend_and_restore_without_reopening_cancelled_work": hidden && visible && pausedInFlight && restored && cancelledAfterRestore,
            "effect_prepared_uniforms_match_original_parameters_for_every_preset": allStyles.allSatisfy { style in
                let template=Self.uniformTemplate(style:style,canvas:canvas)
                return [0.0,0.2,1.35,7.8,8.0].allSatisfy { time in
                    [false,true].allSatisfy { reduced in
                        template.frame(origin:NSPoint(x:17,y:23),viewport:NSSize(width:212,height:97),time:time,duration:8,reduceMotion:reduced) == Self.uniforms(style:style,canvas:canvas,origin:NSPoint(x:17,y:23),viewport:NSSize(width:212,height:97),time:time,duration:8,reduceMotion:reduced)
                    }
                }
            },
            "effect_static_steady_frames_are_identical_while_lifetime_fades_remain_distinct": {
                let style=CueVisualStyle(fields:["effect":"quiet-edge"],fallback:fallback)
                let template=Self.uniformTemplate(style:style,canvas:canvas)
                func frame(_ time:Double) -> [Float] { template.frame(origin:.zero,viewport:canvas,time:time,duration:8,reduceMotion:false) }
                return frame(1) == frame(5) && frame(0.2) != frame(1) && frame(7.8) != frame(1)
            }(),
            "effect_extreme_controls_keep_surface_coverage_bounded": CueEffectPresence.allCases.allSatisfy { presence in
                let style=CueVisualStyle(fields:["effect":"pulse-fragments","presence":presence.rawValue,"width":"80","inset":"80"],fallback:fallback)
                let frames=CueEdgeEffectView.edgeViewports(size:canvas,style:style)
                return frames.allSatisfy { rect.contains($0) } && frames.reduce(0) { $0+$1.width*$1.height } <= canvas.width*canvas.height
            }
        ]
    }
}
