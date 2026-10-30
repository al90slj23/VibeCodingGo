import AppKit
import Foundation
import Metal

extension CueEffectGPU {
    /// Explicit offline verification only. Production alert paths never wait
    /// synchronously for compilation, the GPU, texture readback or image hashes.
    static func renderingValidationReport() -> [String:Any] {
        let semaphore=DispatchSemaphore(value:0)
        var ready: Context?
        let start=ProcessInfo.processInfo.systemUptime
        Self.shared.prepare { context in ready=context; semaphore.signal() }
        let deadline=Date().addingTimeInterval(30)
        while semaphore.wait(timeout:.now()) != .success {
            if Date()>=deadline { return ["checks":["effect_metal_pipeline_compiles":false],"error":"pipeline preparation timed out"] }
            RunLoop.current.run(until:Date().addingTimeInterval(0.01))
        }
        guard let context=ready else { return ["checks":["effect_metal_pipeline_compiles":false],"error":Self.shared.failureDescription ?? "Metal is unavailable"] }
        let compileMS=(ProcessInfo.processInfo.systemUptime-start)*1000
        let size=NSSize(width:640,height:360)
        func render(_ style:CueVisualStyle, origin:NSPoint = .zero, viewport:NSSize = NSSize(width:640,height:360), time:Double = 1.35) -> [UInt8]? {
            let descriptor=MTLTextureDescriptor.texture2DDescriptor(pixelFormat:.bgra8Unorm,width:Int(viewport.width),height:Int(viewport.height),mipmapped:false)
            descriptor.storageMode = .shared; descriptor.usage = [.renderTarget,.shaderRead]
            guard let texture=context.device.makeTexture(descriptor:descriptor) else { return nil }
            let values=Self.uniforms(style:style,canvas:size,origin:origin,viewport:viewport,time:time,duration:nil,reduceMotion:false)
            guard let command=Self.encode(values,texture:texture,context:context) else { return nil }
            command.commit(); command.waitUntilCompleted(); guard command.status == .completed else { return nil }
            var bytes=Array(repeating:UInt8(0),count:texture.width*texture.height*4)
            bytes.withUnsafeMutableBytes { pointer in
                if let base=pointer.baseAddress { texture.getBytes(base,bytesPerRow:texture.width*4,from:MTLRegionMake2D(0,0,texture.width,texture.height),mipmapLevel:0) }
            }
            return bytes
        }
        var allRendered=true, premultiplied=true, visible=0
        var hashes:[String:UInt64]=[:]
        for preset in CueEffectCatalog.presets {
            autoreleasepool {
                let style=CueVisualStyle(fields:["effect":preset.id,"width":"8","inset":"3","palette":"#47EDB9,#70A7FF,#FA74C8"],fallback:.systemMint)
                guard let bytes=render(style) else { allRendered=false; return }
                var hasLight=false, hash=UInt64(1469598103934665603)
                for index in stride(from:0,to:bytes.count,by:4) {
                    let alpha=Int(bytes[index+3]); if alpha>0 { hasLight=true }
                    if Int(bytes[index])>alpha+1 || Int(bytes[index+1])>alpha+1 || Int(bytes[index+2])>alpha+1 { premultiplied=false }
                    for offset in 0..<4 { hash=(hash ^ UInt64(bytes[index+offset])) &* 1099511628211 }
                }
                if hasLight { visible += 1 }; hashes[preset.id]=hash
            }
        }
        let style=CueVisualStyle(fields:["effect":"prism-flow","width":"8","inset":"3","palette":"#FFC44D"],fallback:.systemMint)
        let full=render(style), second=render(style,time:2.35)
        var strips=Array(repeating:UInt8(0),count:640*360*4), stitched=true
        for frame in CueEdgeEffectView.edgeViewports(size:size,style:style) {
            guard let bytes=render(style,origin:frame.origin,viewport:frame.size) else { stitched=false; continue }
            let w=Int(frame.width), h=Int(frame.height), x=Int(frame.minX), y=360-Int(frame.maxY)
            for row in 0..<h {
                let source=row*w*4, destination=((y+row)*640+x)*4
                strips.replaceSubrange(destination..<(destination+w*4),with:bytes[source..<(source+w*4)])
            }
        }
        var differences=0, largestDifference=0
        if let full { for index in full.indices { let d=abs(Int(full[index])-Int(strips[index]));if d>2 { differences += 1 };largestDifference=max(largestDifference,d) } } else { stitched=false }
        let center=full?[(180*640+320)*4+3] ?? 255
        var checks=Self.invariantReport()
        checks["effect_metal_pipeline_compiles"]=true
        checks["effect_all_catalog_recipes_render_on_real_gpu"]=allRendered && visible == CueEffectCatalog.presets.count
        checks["effect_gpu_pixels_use_premultiplied_transparent_alpha"]=premultiplied && center == 0
        checks["effect_legacy_50_gpu_outputs_remain_distinct"]=Set(CueEffectCatalog.legacyPresets.compactMap { hashes[$0.id] }).count == 50
        checks["effect_moving_light_field_changes_without_cpu_redraw"]=full != nil && second != nil && full != second
        checks["effect_strip_coordinates_match_full_canvas_without_seams"]=stitched && differences < 640*360*4/1000
        return ["checks":checks,"pipeline_ms":compileMS,"rendered_presets":visible,"seam_different_components":differences,"seam_max_component_difference":largestDifference,"validation_pixels_per_frame":640*360]
    }
}

extension CueEffectGPU {
    private static let exportGate=CueEffectFrameGate()
    private static let exportQueue=DispatchQueue(label:"local.vibecue.effect-export",qos:.utility,autoreleaseFrequency:.workItem)

    /// Explicit CLI/debug capture only. Produces one aspect-preserving PNG with
    /// fewer than eight million pixels. Main never waits for GPU or readback.
    /// Completion is delivered on main; concurrent captures fail without queueing.
    static func exportPNG(style:CueVisualStyle, canvasSize:NSSize, time:TimeInterval, duration:TimeInterval?, reduceMotion:Bool, to url:URL, scale requestedScale:CGFloat = 1, completion:@escaping (Bool)->Void) {
        func finish(_ ok:Bool) { exportGate.end(); DispatchQueue.main.async { completion(ok) } }
        guard exportGate.begin() else { DispatchQueue.main.async { completion(false) }; return }
        let w=canvasSize.width,h=canvasSize.height,area=w*h
        guard w.isFinite,h.isFinite,area.isFinite,w>0,h>0,w<=32768,h<=32768,time.isFinite,requestedScale.isFinite,requestedScale>0 else { finish(false);return }
        let scale=min(requestedScale,min(2,sqrt(7_999_999/area)))
        let pixelWidth=max(1,Int(floor(w*scale))),pixelHeight=max(1,Int(floor(h*scale)))
        guard pixelWidth*pixelHeight<8_000_000 else { finish(false);return }
        Self.shared.prepare { context in
            guard let context else { finish(false);return }
            Self.exportQueue.async {
                let descriptor=MTLTextureDescriptor.texture2DDescriptor(pixelFormat:.bgra8Unorm,width:pixelWidth,height:pixelHeight,mipmapped:false)
                descriptor.storageMode = .shared;descriptor.usage = [.renderTarget,.shaderRead]
                guard let texture=context.device.makeTexture(descriptor:descriptor) else { finish(false);return }
                let values=Self.uniforms(style:style,canvas:canvasSize,origin:.zero,viewport:canvasSize,time:time,duration:duration,reduceMotion:reduceMotion)
                guard let command=Self.encode(values,texture:texture,context:context) else { finish(false);return }
                command.addCompletedHandler { command in
                    guard command.status == .completed else { finish(false);return }
                    Self.exportQueue.async {
                        guard let rep=NSBitmapImageRep(bitmapDataPlanes:nil,pixelsWide:pixelWidth,pixelsHigh:pixelHeight,bitsPerSample:8,samplesPerPixel:4,hasAlpha:true,isPlanar:false,colorSpaceName:.deviceRGB,bytesPerRow:pixelWidth*4,bitsPerPixel:32),let data=rep.bitmapData else { finish(false);return }
                        texture.getBytes(data,bytesPerRow:rep.bytesPerRow,from:MTLRegionMake2D(0,0,pixelWidth,pixelHeight),mipmapLevel:0)
                        for index in stride(from:0,to:rep.bytesPerRow*pixelHeight,by:4) {
                            let blue=data[index];data[index]=data[index+2];data[index+2]=blue
                        }
                        guard let png=rep.representation(using:.png,properties:[:]) else { finish(false);return }
                        do { try png.write(to:url,options:.atomic);finish(true) } catch { finish(false) }
                    }
                }
                command.commit()
            }
        }
    }
}
