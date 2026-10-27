#!/usr/bin/env python3
"""Offline material/motion evidence, without launching VibeCue or touching settings.

Requires only Python's standard library, Swift and the macOS frameworks used by
VibeCue. Sources are snapshotted into a temporary directory. The real runtime
model definitions are compiled, but no controller or NSApplication is started.
The generated frames are 1 pixel per logical point; they are not desktop screen
captures, compositing/color-management certification, or a judgement of taste.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import subprocess
import tempfile
import textwrap


SWIFT_HARNESS = r'''
import AppKit
import Foundation

struct AuditFrame {
    let id: String, name: String, direction: String, variant: String
    let time: Double, rep: NSBitmapImageRep
    let metadata: [String: Any]
}

@main struct LightingAudit {
    static let ids = ["luminous-flow", "pearl-flow", "prism-flow"]
    static func bitmap(_ width: Int, _ height: Int) -> NSBitmapImageRep {
        NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height,
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: width * 4, bitsPerPixel: 32)!
    }
    static func paint(_ rep: NSBitmapImageRep, _ block: () -> Void) {
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)!
        NSColor.clear.setFill(); NSRect(x: 0, y: 0, width: rep.pixelsWide, height: rep.pixelsHigh).fill(using: .copy)
        block(); NSGraphicsContext.restoreGraphicsState()
    }
    static func nsimage(_ rep: NSBitmapImageRep) -> NSImage {
        let image = NSImage(size: NSSize(width: rep.pixelsWide, height: rep.pixelsHigh))
        image.addRepresentation(rep); return image
    }
    static func write(_ rep: NSBitmapImageRep, _ url: URL) throws {
        guard let data = rep.representation(using: .png, properties: [:]) else { throw CocoaError(.fileWriteUnknown) }
        try data.write(to: url)
    }
    static func topRowFirst() -> Bool {
        let test = bitmap(16, 16)
        paint(test) { NSColor.red.setFill(); NSRect(x: 0, y: 12, width: 4, height: 4).fill() }
        return test.bitmapData![0] > 128
    }
    static func analyze(_ rep: NSBitmapImageRep, style: CueVisualStyle, topFirst: Bool) -> [String: Any] {
        let width = rep.pixelsWide, height = rep.pixelsHigh, p = rep.bitmapData!, rowBytes = rep.bytesPerRow
        let alphaFirst = rep.bitmapFormat.contains(.alphaFirst)
        let unpremultiplied = rep.bitmapFormat.contains(.alphaNonpremultiplied)
        func rgba(_ x: Int, _ y: Int) -> [Double] {
            let row = topFirst ? y : height - 1 - y
            let i = row * rowBytes + x * 4
            let a = Double(p[i + (alphaFirst ? 0 : 3)]) / 255
            var color = (0..<3).map { Double(p[i + (alphaFirst ? 1 : 0) + $0]) / 255 }
            if !unpremultiplied, a > 0 { color = color.map { min(1, $0 / a) } }
            return color + [a]
        }
        func luminance(_ v: [Double]) -> Double { (v[0] * 0.2126 + v[1] * 0.7152 + v[2] * 0.0722) * v[3] }
        let band = min(min(width, height) / 4, Int(ceil(Double(style.inset) + Double(style.width) * 3 + 64)))
        var maxInteriorAlpha = 0, nonzero = 0, left = width, right = -1, top = height, bottom = -1
        for y in 0..<height {
            let row = topFirst ? y : height - 1 - y
            for x in 0..<width {
                let a = Int(p[row * rowBytes + x * 4 + (alphaFirst ? 0 : 3)])
                if a > 1 { nonzero += 1; left = min(left,x); right = max(right,x); top = min(top,y); bottom = max(bottom,y) }
                if x >= band, x < width - band, y >= band, y < height - band { maxInteriorAlpha = max(maxInteriorAlpha,a) }
            }
        }
        // Independent screen-edge coordinates: top left→right, right top→bottom,
        // bottom right→left, left bottom→top. The renderer's private perimeter
        // geometry is deliberately not copied into this measurement.
        let bins = 512, perimeter = 2 * (width + height), depth = min(128, band)
        var energy = [Double](repeating: 0, count: bins)
        for index in 0..<bins {
            let distance = min(perimeter-1, index * perimeter / bins)
            var total = 0.0
            for n in 0..<depth {
                let point: (Int,Int)
                if distance < width { point = (distance,n) }
                else if distance < width + height { point = (width-1-n,distance-width) }
                else if distance < width*2 + height { point = (width-1-(distance-width-height),height-1-n) }
                else { point = (n,height-1-(distance-width*2-height)) }
                total += luminance(rgba(point.0,point.1))
            }
            energy[index] = total
        }
        // Select a visible straight section in the top edge, then sample normal
        // to that edge. Corner pixels and full-screen brightness are excluded.
        var profileX = width/2, best = -1.0
        for x in stride(from: min(256,width/4), to: max(min(256,width/4)+1,width-min(256,width/4)), by: 4) {
            let total = (0..<depth).reduce(0.0) { $0 + luminance(rgba(x,$1)) }
            if total > best { profileX = x; best = total }
        }
        var profile: [[Double]] = []
        for y in 0..<depth {
            var v = [Double](repeating:0,count:4)
            for x in max(0,profileX-2)...min(width-1,profileX+2) {
                let sample = rgba(x,y)
                for c in 0..<4 { v[c] += sample[c] / 5 }
            }
            let light = v[0]*0.2126 + v[1]*0.7152 + v[2]*0.0722
            profile.append([Double(y)] + v + [light,light*v[3],light*v[3] + 0.94*(1-v[3])])
        }
        var cornerEnergy = 0.0
        let cornerSize = min(256,min(width,height))
        for y in stride(from:0,to:cornerSize,by:3) {
            for x in stride(from:width-cornerSize,to:width,by:3) { cornerEnergy += luminance(rgba(x,y)) }
        }
        return ["pixels":[width,height], "allowed_edge_band_px":band,
                "max_interior_alpha_8bit":maxInteriorAlpha,
                "nonzero_alpha_fraction":Double(nonzero)/Double(width*height),
                "alpha_bounds_top_left":[left,top,right,bottom],
                "perimeter_energy_clockwise":energy,
                "profile_x":profileX,"normal_profile":profile,"top_right_corner_energy":cornerEnergy,
                "bitmap_alpha_nonpremultiplied":unpremultiplied]
    }
    static func composite(_ rep: NSBitmapImageRep, background: NSColor, crop: NSRect? = nil) -> NSBitmapImageRep {
        let source = crop ?? NSRect(x:0,y:0,width:rep.pixelsWide,height:rep.pixelsHigh)
        let out = bitmap(Int(source.width),Int(source.height))
        paint(out) {
            background.setFill(); NSRect(x:0,y:0,width:source.width,height:source.height).fill()
            nsimage(rep).draw(in:NSRect(origin:.zero,size:source.size),from:source,operation:.sourceOver,fraction:1)
        }
        return out
    }
    static func findFrame(_ frames: [AuditFrame], id: String, time: Double) -> AuditFrame {
        for frame in frames {
            if frame.id != id { continue }
            if frame.variant != "default-8" { continue }
            if frame.direction != "clockwise" { continue }
            if frame.time == time { return frame }
        }
        fatalError("Requested frame was not rendered")
    }
    static func main() throws {
        let configURL = URL(fileURLWithPath:CommandLine.arguments[1])
        let config = try JSONSerialization.jsonObject(with:Data(contentsOf:configURL)) as! [String:Any]
        let output = URL(fileURLWithPath:config["output"] as! String,isDirectory:true)
        let width = config["width"] as! Int, height = config["height"] as! Int
        let phases = config["phases"] as! [Double]
        let globalOpacity = 0.85
        let topFirst = topRowFirst()
        guard Set(ids).isSubset(of:Set(CueEffectCatalog.presets.map(\.id))) else {
            fputs("New material presets are not yet present; no fallback presets were rendered.\n",stderr); exit(2)
        }
        var checks: [String:Bool] = [:], frames: [AuditFrame] = []
        let groups = ["glow-breathe,neon,glass,double,heartbeat", "marquee-chase,rainbow,meeting,dots,train", "comet-long,twins,triple,spark,aurora", "dash-neon,rhythm,diamonds,chains,circuit", "scanner-band,radar,diagonal,inspection,probe", "corner-breathe,relay,brackets,arcs,spark", "wave-sine,twins,tide,piano,ripple", "particle-firefly,stars,rain,dust,shards", "spectrum-flow,aurora,grid,ribbons,pulse", "pulse-inward,energy,segments,echo,fragments"]
        let oldIDs = groups.flatMap { group -> [String] in let split=group.split(separator:"-",maxSplits:1); return split[1].split(separator:",").map { String(split[0])+"-"+$0 } }
        checks["all_50_legacy_ids_retained"] = oldIDs.count == 50 && oldIDs.allSatisfy { id in CueEffectCatalog.presets.contains { $0.id == id } }
        checks["catalog_ids_unique"] = Set(CueEffectCatalog.presets.map(\.id)).count == CueEffectCatalog.presets.count
        let fields = ["effect":"prism-flow","palette":"#69B6D2,#A8BED1","color":"#69B6D2","direction":"counterclockwise","speed":"1.7","width":"8","opacity":"0.85","inset":"3","trail":"0.14","presence":"strong","sound":"silent"]
        let loaded = CueSettings(json:["design_generation":3,"event_styles":["input":fields]])
        let restored = CueSettings(json:try JSONSerialization.jsonObject(with:JSONSerialization.data(withJSONObject:loaded.json)) as! [String:Any])
        let roundtrip = restored.eventStyles["input"] ?? [:]
        checks["application_settings_preserve_presence"] = roundtrip["presence"] == "strong"
        checks["application_settings_preserve_material_palette_direction"] = ["effect","palette","direction"].allSatisfy { roundtrip[$0] == fields[$0] }
        checks["application_settings_preserve_numeric_controls"] = ["speed","width","opacity","inset","trail"].allSatisfy { key in
            guard let original=Double(fields[key]!),let value=Double(roundtrip[key] ?? "") else {return false}; return abs(value-original)<0.00001
        }
        let randomFields = fields.merging(["effect":"random","effect_random_tags":"recommended,marquee"]) { _,new in new }
        let randomRestored = CueSettings(json:CueSettings(json:["design_generation":3,"event_styles":["input":randomFields]]).json)
        checks["application_settings_keep_random_and_scope"] = randomRestored.eventStyles["input"]?["effect"] == "random" && randomRestored.eventStyles["input"]?["effect_random_tags"] == "recommended,marquee"
        let resolved = CueResolvedSelection(fields:randomFields) { $0.last ?? "" }
        checks["random_resolves_to_real_scoped_id"] = CueEffectCatalog.presets.contains { $0.id == resolved.fields["effect"] && !$0.tagIDs.filter { ["recommended","marquee"].contains($0) }.isEmpty }
        checks["random_resolution_preserves_material_parameters"] = ["palette","presence","width","opacity","direction","speed","inset","trail"].allSatisfy { resolved.fields[$0] == randomFields[$0] }
        let started = Date()
        for id in ids {
            for variant in ["default-8","compat-3"] {
                let directions = variant == "default-8" ? ["clockwise","counterclockwise"] : ["clockwise"]
                for direction in directions {
                    let times = variant == "compat-3" ? [phases[0]] : direction == "clockwise" ? phases : Array(phases.prefix(2))
                    for time in times {
                        var raw = CueSettings.defaultStyles["input"] ?? [:]
                        raw.merge(["effect":id,"palette":"#69B6D2","direction":direction,"width":variant == "default-8" ? "8":"3","opacity":variant == "default-8" ? "0.85":"0.55","presence":"clear"]) { _,new in new }
                        let style = CueVisualStyle(fields:raw,fallback:.systemTeal)
                        let engine = CueEdgeEffectView.bitmap(style:style,size:NSSize(width:width,height:height),time:time,background:false)
                        let rep = bitmap(width,height)
                        paint(rep) { nsimage(engine).draw(in:NSRect(x:0,y:0,width:width,height:height),from:.zero,operation:.sourceOver,fraction:globalOpacity) }
                        let basename = "\(id)-\(variant)-\(direction)-t\(String(format:"%04.1f",time))"
                        try write(rep,output.appendingPathComponent(basename+".png"))
                        raw["global_opacity"] = String(globalOpacity)
                        var metadata = analyze(rep,style:style,topFirst:topFirst)
                        metadata["file"] = basename+".png"; metadata["id"]=id;metadata["variant"]=variant;metadata["direction"]=direction;metadata["time"]=time;metadata["fields"]=raw
                        frames.append(.init(id:id,name:CueEffectCatalog.preset(id:id).name,direction:direction,variant:variant,time:time,rep:rep,metadata:metadata))
                    }
                }
            }
        }
        var cropSources: [String:[String:String]] = [:]
        let backgrounds: [(String,NSColor)] = [("dark",NSColor(srgbRed:0.065,green:0.075,blue:0.09,alpha:1)),("light",NSColor(srgbRed:0.94,green:0.945,blue:0.95,alpha:1))]
        for (backgroundID,background) in backgrounds {
            let cellWidth=640,cellHeight=390
            let sheet=bitmap(cellWidth*phases.count,cellHeight*ids.count)
            paint(sheet) {
                background.setFill(); NSRect(x:0,y:0,width:sheet.pixelsWide,height:sheet.pixelsHigh).fill()
                for (row,id) in ids.enumerated() {
                    for (column,time) in phases.enumerated() {
                        let frame: AuditFrame = findFrame(frames,id:id,time:time)
                        let origin=NSPoint(x:CGFloat(column*cellWidth),y:CGFloat((ids.count-1-row)*cellHeight))
                        nsimage(frame.rep).draw(in:NSRect(x:origin.x,y:origin.y,width:CGFloat(cellWidth),height:360))
                        let color=backgroundID == "dark" ? NSColor.white:NSColor.black
                        "\(frame.name) · \(time)s · 8px / .85 × .85".draw(at:NSPoint(x:origin.x+12,y:origin.y+365),withAttributes:[.font:NSFont.systemFont(ofSize:14),.foregroundColor:color])
                    }
                }
            }
            try write(sheet,output.appendingPathComponent("contact-\(backgroundID).png"))
            for id in ids {
                let candidates=frames.filter {$0.id == id && $0.variant == "default-8" && $0.direction == "clockwise"}
                let straight=candidates[0]
                let corner=candidates.max { ($0.metadata["top_right_corner_energy"] as! Double) < ($1.metadata["top_right_corner_energy"] as! Double) }!
                cropSources[id] = ["straight":straight.metadata["file"] as! String,"corner":corner.metadata["file"] as! String]
                try write(composite(straight.rep,background:background,crop:NSRect(x:32,y:height-160,width:960,height:160)),output.appendingPathComponent("\(id)-straight-\(backgroundID)-100pct.png"))
                try write(composite(corner.rep,background:background,crop:NSRect(x:width-256,y:height-256,width:256,height:256)),output.appendingPathComponent("\(id)-corner-\(backgroundID)-100pct.png"))
                try write(composite(straight.rep,background:background),output.appendingPathComponent("\(id)-full-\(backgroundID).png"))
            }
        }
        let report: [String:Any] = ["checks":checks,"frames":frames.map(\.metadata),"catalog":CueEffectCatalog.presets.map { ["id":$0.id,"name":$0.name,"tags":$0.tagIDs] },"settings_roundtrip":roundtrip,"render_elapsed_seconds":Date().timeIntervalSince(started),"top_row_first":topFirst,"actual_screen_capture":false,"global_opacity":globalOpacity,"requested_size":[width,height],"crop_source_frames":cropSources]
        try JSONSerialization.data(withJSONObject:report,options:[.prettyPrinted,.sortedKeys]).write(to:output.appendingPathComponent("native-report.json"))
    }
}
'''


def correlation_shift(before: list[float], after: list[float], valid: list[bool]) -> tuple[int, float]:
    """Weighted Pearson correlation over non-overlapping straight-edge strips.

    Near a rectangular screen corner, an edge-normal strip also traverses the
    perpendicular edge. Excluding that footprint prevents a stationary corner
    from outweighing the moving head. This does not test corner continuity.
    """
    count = len(before)
    candidates = []
    for shift in range(-count//2, count//2):
        pairs = [(before[i], after[(i+shift) % count]) for i in range(count)
                 if valid[i] and valid[(i+shift) % count]]
        if len(pairs) < count // 2:
            continue
        mean_a = sum(x for x, _ in pairs) / len(pairs)
        mean_b = sum(y for _, y in pairs) / len(pairs)
        norm = math.sqrt(sum((x-mean_a)**2 for x, _ in pairs) * sum((y-mean_b)**2 for _, y in pairs))
        score = sum((x-mean_a)*(y-mean_b) for x,y in pairs) / norm if norm > 1e-12 else 0
        candidates.append((score, shift))
    score, shift = max(candidates, default=(0.0, 0))
    return shift, score


def profile_svg(rows: list[list[float]], title: str) -> str:
    width, height = 960, 300
    colors = [(4, '#8896a3', 'alpha'), (5, '#3f92c5', 'unpremultiplied luma'),
              (6, '#a061b9', 'luma over black'), (7, '#d28b3c', 'luma over light')]
    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<rect width="100%" height="100%" fill="#fafafa"/>',
             f'<text x="20" y="24" font-family="sans-serif" font-size="16">{title}</text>']
    for index, (column,color,label) in enumerate(colors):
        points = ' '.join(f'{45 + row[0]/max(1,len(rows)-1)*880:.2f},{255-row[column]*195:.2f}' for row in rows)
        lines += [f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.5"/>',
                  f'<text x="{30+index*225}" y="290" font-family="sans-serif" font-size="12" fill="{color}">{label}</text>']
    lines += ['<path d="M45,60 V255 H925" stroke="#bbb" fill="none"/>',
              '<text x="45" y="275" font-family="sans-serif" font-size="11">0: outer screen edge; distance inward in logical pixels →</text>', '</svg>']
    return '\n'.join(lines)


def analyze(output: Path, native: dict) -> dict:
    checks = dict(native['checks'])
    frames = native['frames']
    width,height = native['requested_size']
    perimeter = 2*(width+height)
    corners = [0,width,width+height,2*width+height,perimeter]
    valid = [min(abs(i*perimeter/512-corner) for corner in corners)>128 for i in range(512)]
    movement = []
    profiles = []
    for effect in ['luminous-flow','pearl-flow','prism-flow']:
        for direction in ['clockwise','counterclockwise']:
            selected = sorted((f for f in frames if f['id']==effect and f['variant']=='default-8' and f['direction']==direction), key=lambda f:f['time'])
            first, second = selected[:2]
            shift, correlation = correlation_shift(first['perimeter_energy_clockwise'],second['perimeter_energy_clockwise'],valid)
            sign_ok = shift > 1 if direction == 'clockwise' else shift < -1
            checks[f'{effect}_{direction}_motion_displaces_head'] = sign_ok and correlation > .35
            movement.append({'id':effect,'direction':direction,'times':[first['time'],second['time']], 'shift_bins_clockwise':shift, 'correlation':correlation,'bin_count':512,'corner_footprint_excluded_px':128})
        first = next(f for f in frames if f['id']==effect and f['variant']=='default-8' and f['direction']=='clockwise')
        profile = first['normal_profile']
        visible = [row[5] for row in profile if row[4] > .08]
        contrast = max(visible)-min(visible) if len(visible)>1 else 0
        checks[f'{effect}_normal_color_profile_is_not_flat'] = len(visible)>=3 and contrast > .025
        profiles.append({'id':effect,'file':first['file'],'profile_x':first['profile_x'], 'active_sample_count':len(visible),'unpremultiplied_luma_range':contrast})
        with (output/f'{effect}-normal-profile.csv').open('w') as stream:
            writer=csv.writer(stream);writer.writerow(['distance_from_top_px','red','green','blue','alpha','unpremultiplied_luma','luma_over_black','luma_over_light']);writer.writerows(profile)
        (output/f'{effect}-normal-profile.svg').write_text(profile_svg(profile,f'{effect}: straight-edge normal profile at x={first["profile_x"]}'),encoding='utf8')
    checks['all_exported_frames_have_exact_requested_size'] = all(f['pixels']==native['requested_size'] for f in frames)
    checks['interior_transparency_stays_within_8bit_rounding'] = all(f['max_interior_alpha_8bit']<=1 for f in frames)
    checks['edge_alpha_coverage_is_bounded'] = all(0 < f['nonzero_alpha_fraction'] < .30 for f in frames)
    checks['compatibility_width_3_frames_are_nonempty'] = sum(1 for f in frames if f['variant']=='compat-3' and f['nonzero_alpha_fraction']>0)==3
    # These are diagnostics, not a rule saying that a higher value is better.
    report={'checks':checks,'all_passed':all(checks.values()),'motion':movement,'normal_profiles':profiles,
            'maximum_interior_alpha_8bit':max(f['max_interior_alpha_8bit'] for f in frames),
            'render_elapsed_seconds':native['render_elapsed_seconds'],'frame_count':len(frames),
            'evidence_limits':['Offline NSBitmapImageRep renders, not captures of the desktop compositor.',
              'Default comparison uses identical single-color palette, width 8, style opacity .85 and window alpha .85; compatibility frames use width 3 and style opacity .55.',
              'Capture renders bypass the alert lifetime fade: the 12s frame is a material phase sample, not evidence that a 6s live preview persists.',
              'Motion correlation excludes 128px corner footprints where orthogonal sample strips overlap. Corner continuity is a separate visual check.',
              'Motion displacement, non-flat normal color profiles and transparent interior do not establish comfort, elegance, convincing 3D, or lack of corner seams.',
              'Inspect 100% crops and full-resolution frames on both backgrounds; shrinking contact sheets hides narrow highlights and seams.',
              'No application controller, Codex connection, live window, installation, notification or audio playback is started.']}
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    parts=['<!doctype html><meta charset="utf-8"><title>VibeCue lighting evidence</title>',
           '<style>body{font:16px system-ui;margin:32px;background:#f3f4f6;color:#17202b}img{max-width:100%}figure{margin:24px 0}code{font-size:13px}a{color:#1465a1}details{margin:20px 0}</style>',
           '<h1>VibeCue 光照材质：离屏验收帧</h1>',
           '<p>2560×1440 逻辑像素，同色、同宽、同强度。像素指标仅证明几何和动画行为；舒适度、材质可信度及转角连续性由 100% 图像独立复查。</p>',
           '<p><a href="report.json">指标与限制</a> · <a href="native-report.json">原始采样 / 实际配置持久化结果</a></p>']
    for background in ['dark','light']:
        parts.append(f'<figure><a href="contact-{background}.png"><img src="contact-{background}.png"></a><figcaption>{background} · 点击原图</figcaption></figure>')
    for effect in ['luminous-flow','pearl-flow','prism-flow']:
        parts.append(f'<h2>{effect}</h2><img src="{effect}-normal-profile.svg">')
        for background in ['dark','light']:
            parts.append(f'<p><a href="{effect}-full-{background}.png">{background} 2560×1440 全图</a></p>')
            for crop in ['straight','corner']:
                source=native['crop_source_frames'][effect][crop]
                parts.append(f'<figure><img src="{effect}-{crop}-{background}-100pct.png"><figcaption>{crop} · 100% · {background} · {source}</figcaption></figure>')
    parts.append('<details><summary>全部透明帧 / 相位 / 兼容宽度</summary><ul>')
    for frame in frames:
        parts.append(f'<li><a href="{frame["file"]}">{frame["file"]}</a></li>')
    parts.append('</ul></details>')
    (output/'index.html').write_text('\n'.join(parts),encoding='utf8')
    return report


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--width',type=int,default=2560)
    parser.add_argument('--height',type=int,default=1440)
    parser.add_argument('--phases',default='1,4,8,12')
    args=parser.parse_args()
    if args.width<1024 or args.height<512:
        parser.error('Use at least 1024×512 so the 100% crops remain valid.')
    phases=[float(v) for v in args.phases.split(',')]
    if len(phases)<2 or any(v<0 or not math.isfinite(v) for v in phases):
        parser.error('Supply at least two non-negative finite phases.')
    root=Path(__file__).resolve().parent.parent
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    sources={path.name:path.read_text(encoding='utf8') for path in sorted((root/'Sources').glob('*.swift'))}
    if not all(id in sources['Effects.swift'] for id in ['luminous-flow','pearl-flow','prism-flow']):
        parser.error('The new material presets have not been written yet; no fallback images will be exported.')
    # Snapshot definitions only. The application startup and all CLI self-test
    # branches are excluded, so compiling actual models has no live side effects.
    prefix=sources['main.swift'].split('\nif CommandLine.arguments.contains',1)[0]
    if len(prefix)==len(sources['main.swift']):
        parser.error('Cannot locate application type/startup boundary safely.')
    hashes={name:hashlib.sha256(text.encode()).hexdigest() for name,text in sources.items()}
    (output/'source-snapshot.json').write_text(json.dumps(hashes,indent=2)+'\n')
    with tempfile.TemporaryDirectory(prefix='vibecue-lighting-') as temporary:
        temp=Path(temporary)
        (temp/'RuntimeDefinitions.swift').write_text(prefix)
        for name,text in sources.items():
            if name!='main.swift': (temp/name).write_text(text)
        (temp/'LightingAudit.swift').write_text(SWIFT_HARNESS)
        config={'output':str(output),'width':args.width,'height':args.height,'phases':phases}
        (temp/'config.json').write_text(json.dumps(config))
        binary=temp/'lighting-audit'
        subprocess.run(['/usr/bin/swiftc','-O','-module-cache-path',str(temp/'module-cache'),'-framework','AppKit','-framework','Foundation','-framework','AVFoundation',*map(str,sorted(temp.glob('*.swift'))),'-o',str(binary)],check=True)
        subprocess.run([str(binary),str(temp/'config.json')],check=True)
    native=json.loads((output/'native-report.json').read_text())
    report=analyze(output,native)
    print(json.dumps({'output':str(output),'checks':report['checks'],'all_passed':report['all_passed'],'frame_count':report['frame_count'],'render_elapsed_seconds':report['render_elapsed_seconds']},ensure_ascii=False,indent=2))
    return 0 if report['all_passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
