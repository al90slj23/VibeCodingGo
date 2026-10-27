import AppKit
import Foundation

struct CueEffectPreset {
    let id: String
    let name: String
    let family: String
    let variant: Int
    let defaultPalette: [String]
}

enum CueEffectCatalog {
    static let presets: [CueEffectPreset] = {
        let groups: [(String, [String], [String], [String])] = [
            ("glow", ["breathe", "neon", "glass", "double", "heartbeat"], ["柔光呼吸", "霓虹描边", "琉璃光带", "双层光环", "心跳边框"], ["#3BE8AB", "#55C8FF", "#9978FF"]),
            ("marquee", ["chase", "rainbow", "meeting", "dots", "train"], ["单环追光", "彩虹跑马灯", "双向交汇", "点阵绕屏", "像素列车"], ["#FF678C", "#FFC857", "#4DE3B7", "#50C8FF", "#AB7AFF"]),
            ("comet", ["long", "twins", "triple", "spark", "aurora"], ["流星长尾", "双彗星", "三星接力", "火花尾迹", "极光彗尾"], ["#65E2FF", "#8296FF", "#DD8CFF"]),
            ("dash", ["neon", "rhythm", "diamonds", "chains", "circuit"], ["霓虹虚线", "节拍断续", "钻石光链", "轨道双链", "电路短节"], ["#47E8B7", "#E8FF8C", "#58CCFF"]),
            ("scanner", ["band", "radar", "diagonal", "inspection", "probe"], ["光带扫描", "雷达扫边", "对角探照", "四边巡检", "跳跃探针"], ["#7AF8D0", "#C5FF91", "#F7FAFF"]),
            ("corner", ["breathe", "relay", "brackets", "arcs", "spark"], ["四角呼吸", "环角接力", "脉冲括号", "弧光四角", "角点火花"], ["#FFB54D", "#FF677D", "#D591FF"]),
            ("wave", ["sine", "twins", "tide", "piano", "ripple"], ["波纹流动", "双生波线", "潮汐宽带", "琴键边沿", "涟漪向内"], ["#51CFFF", "#5A8CFF", "#AC92FF"]),
            ("particle", ["firefly", "stars", "rain", "dust", "shards"], ["萤火边缘", "星点跃迁", "光子雨", "星尘尾流", "折射碎片"], ["#E8FF8A", "#7AF4C7", "#86CFFF"]),
            ("spectrum", ["flow", "aurora", "grid", "ribbons", "pulse"], ["七彩流光", "极光幕帘", "全息栅格", "彩带交织", "色谱脉冲"], ["#FF5D80", "#FFBD50", "#C7F15C", "#39DDBD", "#50A5FF", "#A77BFF"]),
            ("pulse", ["inward", "energy", "segments", "echo", "fragments"], ["向内扩散", "单圈能量", "分段涌动", "光环叠影", "呼吸碎环"], ["#CB8DFF", "#6FBCFF", "#61EDCE"])
        ]
        return groups.flatMap { family, ids, names, palette in
            ids.enumerated().map { index, id in
                CueEffectPreset(id: family + "-" + id, name: names[index], family: family, variant: index, defaultPalette: palette)
            }
        }
    }()

    static func preset(id: String) -> CueEffectPreset {
        presets.first(where: { $0.id == id }) ?? presets[0]
    }

    static func previewImage(for preset: CueEffectPreset, size: NSSize = NSSize(width: 180, height: 96), time: TimeInterval = 1.35) -> NSImage {
        let style = CueVisualStyle(fields: ["effect": preset.id, "palette": preset.defaultPalette.joined(separator: ","), "width": "12", "inset": "7"], fallback: .systemMint)
        let rep = CueEdgeEffectView.bitmap(style: style, size: size, time: time, background: true)
        let image = NSImage(size: size)
        image.addRepresentation(rep)
        return image
    }

    static func renderContactSheet(to url: URL) throws {
        let cell = NSSize(width: 330, height: 188)
        let size = NSSize(width: cell.width * 5, height: cell.height * 10)
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width), pixelsHigh: Int(size.height), bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: Int(size.width) * 4, bitsPerPixel: 32)!
        let context = NSGraphicsContext(bitmapImageRep: rep)!
        NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current = context
        NSColor(srgbRed: 0.045, green: 0.055, blue: 0.07, alpha: 1).setFill()
        NSRect(origin: .zero, size: size).fill()
        for (index, preset) in presets.enumerated() {
            let x = CGFloat(index % 5) * cell.width
            let y = size.height - CGFloat(index / 5 + 1) * cell.height
            let frame = NSRect(x: x + 12, y: y + 38, width: cell.width - 24, height: cell.height - 48)
            previewImage(for: preset, size: frame.size).draw(in: frame)
            let attributes: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 12, weight: .medium), .foregroundColor: NSColor.white.withAlphaComponent(0.88)]
            (String(format: "%02d  ", index + 1) + preset.name).draw(in: NSRect(x: x + 16, y: y + 10, width: cell.width - 32, height: 22), withAttributes: attributes)
        }
        NSGraphicsContext.restoreGraphicsState()
        try rep.representation(using: .png, properties: [:])!.write(to: url, options: .atomic)
    }

    static func validationReport() -> [String: Bool] {
        let fallback = NSColor(srgbRed: 0.2, green: 0.8, blue: 0.6, alpha: 1)
        let bounded = CueVisualStyle(fields: ["speed": "nan", "width": "999", "opacity": "-4", "inset": "-1", "trail": "nan", "palette": "garbage,#123456,#ABCDEF", "direction": "invalid"], fallback: fallback)
        var hashes = Set<UInt64>()
        for preset in presets {
            let style = CueVisualStyle(fields: ["effect": preset.id, "palette": "#47EDB9,#70A7FF,#FA74C8", "width": "10", "inset": "5"], fallback: fallback)
            let rep = CueEdgeEffectView.bitmap(style: style, size: NSSize(width: 180, height: 106), time: 1.41, background: false)
            if let bytes = rep.bitmapData {
                var hash: UInt64 = 1469598103934665603
                for offset in 0..<(rep.bytesPerRow * rep.pixelsHigh) { hash = (hash ^ UInt64(bytes[offset])) &* 1099511628211 }
                hashes.insert(hash)
            }
        }
        return ["exactly_50_presets": presets.count == 50,
                "stable_unique_ids": Set(presets.map(\.id)).count == 50,
                "all_presets_have_multicolor_palettes": presets.allSatisfy { $0.defaultPalette.count >= 2 && $0.defaultPalette.allSatisfy { NSColor.cueColor(hex: $0) != nil } },
                "bounded_finite_controls": bounded.speed.isFinite && bounded.width == 80 && bounded.opacity == 0.1 && bounded.inset == 0 && bounded.trail.isFinite,
                "palette_validation": bounded.paletteHex == ["#123456", "#ABCDEF"],
                "direction_validation": bounded.direction == "clockwise",
                "fifty_distinct_visual_renders": hashes.count == 50]
    }
}

struct CueVisualStyle {
    let effectID: String
    let palette: [NSColor]
    let paletteHex: [String]
    let direction: String
    let speed: Double
    let width: CGFloat
    let opacity: Double
    let inset: CGFloat
    let trail: Double
    let cardID: String

    init(fields: [String: String], fallback: NSColor) {
        effectID = CueEffectCatalog.preset(id: fields["effect"] ?? "glow-breathe").id
        let colors = (fields["palette"] ?? "").split(separator: ",").compactMap { NSColor.cueColor(hex: $0.trimmingCharacters(in: .whitespacesAndNewlines)) }.prefix(8)
        palette = colors.isEmpty ? [NSColor.cueColor(hex: fields["color"] ?? "") ?? fallback] : Array(colors)
        paletteHex = palette.map(\.cueHex)
        direction = ["clockwise", "counterclockwise", "pingpong"].contains(fields["direction"] ?? "") ? fields["direction"]! : "clockwise"
        speed = Self.number(fields["speed"], fallback: 1, low: 0.15, high: 4)
        width = CGFloat(Self.number(fields["width"], fallback: 28, low: 2, high: 80))
        opacity = Self.number(fields["opacity"], fallback: 0.85, low: 0.1, high: 1)
        inset = CGFloat(Self.number(fields["inset"], fallback: 10, low: 0, high: 80))
        trail = Self.number(fields["trail"], fallback: 0.18, low: 0.03, high: 0.65)
        cardID = fields["card_style"] ?? "glass-dark"
    }

    static func number(_ text: String?, fallback: Double, low: Double, high: Double) -> Double {
        guard let text, let value = Double(text), value.isFinite else { return fallback }
        return min(high, max(low, value))
    }

    static func normalizedFields(_ fields: [String: String], fallback: NSColor) -> [String: String] {
        let style = CueVisualStyle(fields: fields, fallback: fallback)
        var result = fields
        result["effect"] = style.effectID; result["palette"] = style.paletteHex.joined(separator: ",")
        result["direction"] = style.direction; result["speed"] = String(style.speed)
        result["width"] = String(Double(style.width)); result["opacity"] = String(style.opacity)
        result["inset"] = String(Double(style.inset)); result["trail"] = String(style.trail)
        result["card_style"] = style.cardID
        return result
    }
}

// A clockwise, distance-parameterized rounded rectangle. A phase moves at a
// constant physical speed on straight edges and around the corners.
private struct CuePerimeter {
    let rect: NSRect
    let radius: CGFloat
    let horizontal: CGFloat
    let vertical: CGFloat
    let arc: CGFloat
    let length: CGFloat

    init(bounds: NSRect, inset: CGFloat) {
        let safeInset = min(max(0, inset), max(0, min(bounds.width, bounds.height) / 2 - 3))
        let rect = bounds.insetBy(dx: safeInset, dy: safeInset)
        self.init(rect: rect, radius: min(22, min(rect.width, rect.height) / 5))
    }

    private init(rect: NSRect, radius: CGFloat) {
        self.rect = rect
        self.radius = max(1, min(radius, min(rect.width, rect.height) / 3))
        horizontal = max(0, rect.width - self.radius * 2)
        vertical = max(0, rect.height - self.radius * 2)
        arc = .pi * self.radius / 2
        length = max(1, horizontal * 2 + vertical * 2 + arc * 4)
    }

    func point(_ fraction: Double, offset: CGFloat = 0) -> NSPoint {
        if abs(offset) > 0.001 {
            // Rebuild inset curves instead of offsetting their normals past
            // the corner radius (which would fold wide ripples into loops).
            let bounded = min(offset, max(0, min(rect.width, rect.height) / 2 - 3))
            return CuePerimeter(rect: rect.insetBy(dx: bounded, dy: bounded), radius: max(1, radius - bounded)).point(fraction)
        }
        let t = fraction - floor(fraction)
        var distance = CGFloat(t) * length
        let point: NSPoint
        if distance <= horizontal { point = NSPoint(x: rect.minX + radius + distance, y: rect.maxY) }
        else {
            distance -= horizontal
            if distance <= arc { let a = .pi / 2 - distance / radius; point = NSPoint(x: rect.maxX - radius + cos(a) * radius, y: rect.maxY - radius + sin(a) * radius) }
            else {
                distance -= arc
                if distance <= vertical { point = NSPoint(x: rect.maxX, y: rect.maxY - radius - distance) }
                else {
                    distance -= vertical
                    if distance <= arc { let a = -distance / radius; point = NSPoint(x: rect.maxX - radius + cos(a) * radius, y: rect.minY + radius + sin(a) * radius) }
                    else {
                        distance -= arc
                        if distance <= horizontal { point = NSPoint(x: rect.maxX - radius - distance, y: rect.minY) }
                        else {
                            distance -= horizontal
                            if distance <= arc { let a = -.pi / 2 - distance / radius; point = NSPoint(x: rect.minX + radius + cos(a) * radius, y: rect.minY + radius + sin(a) * radius) }
                            else {
                                distance -= arc
                                if distance <= vertical { point = NSPoint(x: rect.minX, y: rect.minY + radius + distance) }
                                else { distance -= vertical; let a = .pi - distance / radius; point = NSPoint(x: rect.minX + radius + cos(a) * radius, y: rect.maxY - radius + sin(a) * radius) }
                            }
                        }
                    }
                }
            }
        }
        return point
    }
}

final class CueEdgeEffectView: NSView {
    var style: CueVisualStyle { didSet { needsDisplay = true } }
    var elapsed: TimeInterval = 0
    override var isOpaque: Bool { false }
    private var forcedMotionForCapture = false

    init(frame: NSRect, style: CueVisualStyle) {
        self.style = style
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func advance(to time: TimeInterval) {
        let next = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion ? 0 : time
        guard abs(next - elapsed) >= 1.0 / 32.0 else { return }
        elapsed = next; needsDisplay = true
    }

    private var time: Double {
        !forcedMotionForCapture && NSWorkspace.shared.accessibilityDisplayShouldReduceMotion ? 0 : elapsed
    }
    private var phase: Double {
        let value = time * style.speed * 0.13
        if style.direction == "counterclockwise" { return -value }
        if style.direction == "pingpong" { return 0.5 - 0.5 * cos(value * .pi * 2) }
        return value
    }
    private var motionSign: Double {
        if style.direction == "counterclockwise" { return -1 }
        if style.direction == "pingpong" { return sin(time * style.speed * 0.13 * .pi * 2) >= 0 ? 1 : -1 }
        return 1
    }

    private func color(_ position: Double, alpha: Double = 1) -> NSColor {
        let colors = style.palette
        guard colors.count > 1 else { return colors[0].withAlphaComponent(CGFloat(alpha * style.opacity)) }
        let value = (position - floor(position)) * Double(colors.count)
        let lower = Int(value) % colors.count, upper = (lower + 1) % colors.count
        return (colors[lower].blended(withFraction: CGFloat(value - floor(value)), of: colors[upper]) ?? colors[lower]).withAlphaComponent(CGFloat(alpha * style.opacity))
    }

    private func stroke(_ perimeter: CuePerimeter, start: Double, length: Double, width: CGFloat, tint: NSColor, offset: CGFloat = 0, wave: CGFloat = 0, frequency: Double = 0, phase: Double = 0) {
        guard length > 0, width > 0 else { return }
        let path = NSBezierPath()
        let steps = max(2, min(256, Int(Double(perimeter.length) * length / 9)))
        for step in 0...steps {
            let t = start + length * Double(step) / Double(steps)
            let shift = offset + wave * CGFloat(sin((t * frequency + phase) * .pi * 2))
            let point = perimeter.point(t, offset: shift)
            if step == 0 { path.move(to: point) } else { path.line(to: point) }
        }
        path.lineWidth = width; path.lineCapStyle = .round; path.lineJoinStyle = .round
        tint.setStroke(); path.stroke()
    }

    private func gradient(_ p: CuePerimeter, width: CGFloat, alpha: Double, offset: CGFloat = 0, colorOffset: Double = 0, wave: CGFloat = 0, frequency: Double = 0) {
        let segments = 128
        for index in 0..<segments {
            let t = Double(index) / Double(segments)
            stroke(p, start: t, length: 1 / Double(segments) + 0.001, width: width, tint: color(t + colorOffset, alpha: alpha), offset: offset, wave: wave, frequency: frequency, phase: phase)
        }
    }

    private func dot(_ p: CuePerimeter, at t: Double, radius: CGFloat, tint: NSColor, offset: CGFloat = 0, square: Bool = false) {
        let point = p.point(t, offset: offset)
        let frame = NSRect(x: point.x - radius, y: point.y - radius, width: radius * 2, height: radius * 2)
        tint.setFill()
        if square { frame.fill() } else { NSBezierPath(ovalIn: frame).fill() }
    }

    private func comet(_ p: CuePerimeter, head: Double, tail: Double, width: CGFloat, colorOffset: Double, sparks: Bool = false, offset: CGFloat = 0, reverse: Bool = false) {
        let sign = motionSign * (reverse ? -1 : 1)
        let segments = 40
        for segment in 0..<segments {
            let progress = Double(segment) / Double(segments)
            let start = head - sign * tail * (1 - progress)
            let a = pow(progress, 1.8)
            stroke(p, start: start, length: tail / Double(segments) + 0.001, width: width * CGFloat(0.2 + progress * 0.8), tint: color(colorOffset + progress * 0.3, alpha: a), offset: offset)
            if sparks && segment % 4 == 0 {
                dot(p, at: start, radius: max(1, width * 0.13), tint: color(colorOffset + progress, alpha: a * 0.75), offset: offset + CGFloat(sin(Double(segment) * 3.9 + time * 2)) * width)
            }
        }
        dot(p, at: head, radius: max(1.5, width * 0.4), tint: color(colorOffset, alpha: 1), offset: offset)
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.clear.setFill(); dirtyRect.fill(using: .copy)
        guard bounds.width > 8, bounds.height > 8 else { return }
        NSGraphicsContext.current?.imageInterpolation = .high
        let preset = CueEffectCatalog.preset(id: style.effectID)
        let width = min(style.width, min(bounds.width, bounds.height) / 5)
        let p = CuePerimeter(bounds: bounds, inset: style.inset + width * 0.6)
        let v = preset.variant
        let breathe = 0.62 + 0.28 * sin(time * style.speed * 1.65)
        switch preset.family {
        case "glow":
            switch v {
            case 0:
                gradient(p, width: width * 2.1, alpha: 0.10 * breathe, colorOffset: phase)
                gradient(p, width: width, alpha: 0.28 * breathe, colorOffset: phase)
                gradient(p, width: max(1.5, width * 0.12), alpha: 0.95 * breathe, colorOffset: phase)
            case 1:
                gradient(p, width: width * 0.7, alpha: 0.13, colorOffset: -phase)
                gradient(p, width: 2.2, alpha: 1, colorOffset: -phase)
                gradient(p, width: 1, alpha: 0.7, offset: width * 0.42, colorOffset: -phase + 0.25)
            case 2:
                for band in 0..<6 { gradient(p, width: max(1, width / 7), alpha: 0.1 + Double(band) * 0.065, offset: CGFloat(band - 2) * width / 7, colorOffset: phase + Double(band) * 0.09) }
            case 3:
                gradient(p, width: 2.5, alpha: 0.95, colorOffset: phase)
                gradient(p, width: width * 0.35, alpha: 0.12, offset: width * 0.8, colorOffset: -phase)
                gradient(p, width: 1.5, alpha: 0.8, offset: width * 0.8, colorOffset: -phase)
            default:
                let beat = 0.45 + 0.38 * pow(max(0, sin(time * style.speed * 2)), 4) + 0.12 * pow(max(0, sin(time * style.speed * 4 + 0.7)), 8)
                gradient(p, width: width * CGFloat(0.65 + beat), alpha: beat * 0.22)
                gradient(p, width: 2, alpha: beat, colorOffset: phase)
            }
        case "marquee":
            gradient(p, width: 1, alpha: 0.12)
            switch v {
            case 0: comet(p, head: phase, tail: style.trail * 0.65, width: width * 0.65, colorOffset: phase)
            case 1:
                for index in 0..<36 { let t = Double(index) / 36 + phase; stroke(p, start: t, length: 0.013, width: width * 0.52, tint: color(Double(index) / 36 + phase, alpha: 0.9)) }
            case 2:
                comet(p, head: phase, tail: style.trail * 0.55, width: width * 0.48, colorOffset: 0)
                comet(p, head: 0.5 - phase, tail: style.trail * 0.4, width: width * 0.24, colorOffset: 0.5, offset: width * 0.45, reverse: true)
            case 3:
                for index in 0..<64 { let t = Double(index) / 64 + phase; dot(p, at: t, radius: width * 0.17, tint: color(Double(index) / 64, alpha: 0.4 + 0.6 * abs(sin(Double(index) * 0.2 + time)))) }
            default:
                for index in 0..<13 { let t = phase - Double(index) * 0.012; dot(p, at: t, radius: width * CGFloat(0.3 - Double(index) * 0.012), tint: color(Double(index) / 13, alpha: 1 - Double(index) * 0.06), square: true) }
            }
        case "comet":
            gradient(p, width: 1, alpha: 0.10)
            let count = v == 1 ? 2 : v == 2 ? 3 : 1
            for index in 0..<count {
                let head = phase + Double(index) / Double(count)
                comet(p, head: head, tail: style.trail * (v == 0 ? 1.4 : v == 3 ? 0.9 : 1), width: width * (v == 3 ? 0.35 : 0.6), colorOffset: Double(index) / Double(count), sparks: v == 3)
                if v == 4 { comet(p, head: head - 0.03, tail: style.trail * 1.6, width: width * 0.25, colorOffset: 0.5, offset: width * CGFloat(0.6 + 0.15 * sin(time))) }
            }
        case "dash":
            switch v {
            case 0:
                for index in 0..<42 { let t = Double(index) / 42 + phase; stroke(p, start: t, length: 0.012, width: width * 0.28, tint: color(t - phase, alpha: 0.85)) }
            case 1:
                for index in 0..<15 { let t = Double(index) / 15 + phase; for tick in 0..<3 { stroke(p, start: t + Double(tick) * 0.011, length: tick == 2 ? 0.019 : 0.004, width: width * 0.45, tint: color(Double(index) / 15 + Double(tick) * 0.1)) } }
            case 2:
                for index in 0..<32 { let t = Double(index) / 32 + phase; let center = p.point(t); let r = width * 0.28; let path = NSBezierPath(); path.move(to: NSPoint(x: center.x, y: center.y + r)); path.line(to: NSPoint(x: center.x + r, y: center.y)); path.line(to: NSPoint(x: center.x, y: center.y - r)); path.line(to: NSPoint(x: center.x - r, y: center.y)); path.close(); color(t).setFill(); path.fill() }
            case 3:
                for index in 0..<32 { for track in 0..<2 { let t = Double(index) / 32 + (track == 0 ? phase : -phase); stroke(p, start: t, length: 0.015, width: max(1, width * 0.14), tint: color(Double(index) / 32 + Double(track) * 0.5), offset: CGFloat(track) * width * 0.65) } }
            default:
                for index in 0..<25 { let t = Double(index) / 25 + phase; stroke(p, start: t, length: 0.026, width: 1.5, tint: color(t)); dot(p, at: t, radius: width * 0.17, tint: color(t + 0.3), square: true); dot(p, at: t + 0.026, radius: width * 0.09, tint: color(t), offset: width * 0.4) }
            }
        case "scanner":
            gradient(p, width: 1, alpha: 0.13)
            switch v {
            case 0:
                for layer in 0..<5 { stroke(p, start: phase, length: style.trail * 1.6, width: width * CGFloat(1 - Double(layer) * 0.16), tint: color(Double(layer) * 0.1 + phase, alpha: 0.12 + Double(layer) * 0.11)) }
            case 1:
                comet(p, head: phase, tail: 0.36, width: width * 0.18, colorOffset: phase)
                stroke(p, start: phase, length: 0.008, width: width * 1.4, tint: color(0.3, alpha: 0.85))
            case 2:
                for position in [phase, phase + 0.5] { stroke(p, start: position, length: 0.14, width: width * 0.6, tint: color(position, alpha: 0.65)); dot(p, at: position + 0.07, radius: width * 0.22, tint: color(position + 0.3)) }
            case 3:
                let segment = floor((phase - floor(phase)) * 4) / 4
                stroke(p, start: segment + 0.008, length: 0.234, width: width * 0.25, tint: color(segment, alpha: 0.5 + 0.4 * breathe))
                dot(p, at: phase, radius: width * 0.4, tint: color(phase))
            default:
                for index in 0..<7 { let t = phase * 1.7 + Double(index) / 7; stroke(p, start: t, length: 0.002, width: width * CGFloat(0.4 + Double(index % 3) * 0.35), tint: color(Double(index) / 7, alpha: 0.75)); dot(p, at: t + 0.006, radius: 1.5, tint: color(t)) }
            }
        case "corner":
            // Fractional corner positions follow the actual rounded rectangle,
            // so wide and ultra-wide displays retain equal visual corner spans.
            let positions: [Double] = [Double(p.horizontal / p.length), Double((p.horizontal + p.vertical + p.arc) / p.length), Double((p.horizontal * 2 + p.vertical + p.arc * 2) / p.length), Double((p.horizontal * 2 + p.vertical * 2 + p.arc * 3) / p.length)]
            for (index, t) in positions.enumerated() {
                let span = min(0.13, max(0.025, Double(width * 5 / p.length)))
                switch v {
                case 0: stroke(p, start: t - span, length: span * 2 + Double(p.arc / p.length), width: width * 0.28, tint: color(Double(index) / 4, alpha: breathe))
                case 1:
                    let active = Int(floor((phase - floor(phase)) * 4)) == index
                    stroke(p, start: t - span, length: span * 2, width: active ? width * 0.6 : 1.5, tint: color(Double(index) / 4, alpha: active ? 1 : 0.18))
                case 2:
                    stroke(p, start: t - span * breathe, length: span * breathe * 2, width: 2, tint: color(Double(index) / 4))
                    stroke(p, start: t - span * 0.65, length: span * 1.3, width: 1, tint: color(Double(index) / 4 + 0.2, alpha: 0.4), offset: width * 0.65)
                case 3:
                    for ring in 0..<3 { stroke(p, start: t - span * 0.55, length: span * 1.1 + Double(p.arc / p.length), width: max(1, width * 0.12), tint: color(Double(index + ring) / 6, alpha: 0.85 - Double(ring) * 0.23), offset: CGFloat(ring) * width * 0.45) }
                default:
                    for particle in 0..<9 { let tt = t + Double(particle - 4) * span / 7; let shift = CGFloat(sin(time * 1.8 + Double(particle) * 2.7)) * width; dot(p, at: tt, radius: width * CGFloat(0.08 + 0.14 * abs(sin(time + Double(particle)))), tint: color(Double(index) / 4 + Double(particle) * 0.1, alpha: 0.8), offset: shift) }
                }
            }
        case "wave":
            switch v {
            case 0: gradient(p, width: max(2, width * 0.16), alpha: 0.9, colorOffset: phase, wave: width * 0.4, frequency: 9)
            case 1:
                gradient(p, width: max(1, width * 0.13), alpha: 0.9, colorOffset: phase, wave: width * 0.5, frequency: 8)
                gradient(p, width: max(1, width * 0.13), alpha: 0.75, offset: width * 0.6, colorOffset: 0.5 - phase, wave: -width * 0.5, frequency: 8)
            case 2:
                for band in 0..<4 { gradient(p, width: width * 0.2, alpha: 0.16 + Double(band) * 0.1, offset: CGFloat(band) * width * 0.22, colorOffset: Double(band) * 0.1 - phase, wave: width * 0.2, frequency: 4 + Double(band)) }
            case 3:
                for index in 0..<90 { let t = Double(index) / 90; let height = width * CGFloat(0.35 + 0.65 * abs(sin(t * .pi * 14 + time * style.speed))); stroke(p, start: t + phase * 0.15, length: 0.003, width: height, tint: color(t + phase, alpha: 0.8)) }
            default:
                for ring in 0..<4 { let progress = (time * style.speed * 0.3 + Double(ring) / 4).truncatingRemainder(dividingBy: 1); gradient(p, width: max(1, width * 0.12), alpha: (1 - progress) * 0.75, offset: CGFloat(progress) * width * 2.8, colorOffset: Double(ring) / 4) }
            }
        case "particle":
            gradient(p, width: 1, alpha: 0.07)
            let count = v == 1 ? 23 : v == 4 ? 35 : 60
            for index in 0..<count {
                let seed = Double(index) * 2.399963
                let t = Double(index) / Double(count) + phase * (v == 2 ? 0.25 : 1)
                let pulse = 0.35 + 0.65 * abs(sin(seed + time * style.speed * (v == 1 ? 0.8 : 1.4)))
                let offset = width * CGFloat(sin(seed + time * 0.8)) * (v == 0 ? 0.5 : 0.9)
                let radius = max(1, width * CGFloat(v == 1 ? 0.22 : 0.07 + pulse * 0.08))
                switch v {
                case 2:
                    let start = p.point(t, offset: offset)
                    let end = p.point(t + 0.001, offset: offset + width * CGFloat(pulse))
                    let line = NSBezierPath(); line.move(to: start); line.line(to: end); line.lineWidth = 1.3; color(t, alpha: pulse).setStroke(); line.stroke()
                case 3:
                    let behind = t - phase
                    let fraction = behind - floor(behind)
                    let visible = max(0.08, 1 - fraction / max(0.15, style.trail * 2))
                    dot(p, at: t, radius: radius * CGFloat(visible), tint: color(t + seed, alpha: visible * pulse), offset: offset)
                case 4:
                    let center = p.point(t, offset: offset)
                    let path = NSBezierPath(); path.move(to: NSPoint(x: center.x, y: center.y + radius * 2)); path.line(to: NSPoint(x: center.x + radius, y: center.y - radius)); path.line(to: NSPoint(x: center.x - radius, y: center.y - radius)); path.close(); color(t + seed, alpha: pulse).setFill(); path.fill()
                default:
                    dot(p, at: t, radius: radius * 2.2, tint: color(t + seed, alpha: pulse * 0.13), offset: offset)
                    dot(p, at: t, radius: radius, tint: color(t + seed, alpha: pulse), offset: offset)
                    if v == 1 { stroke(p, start: t - 0.004, length: 0.008, width: 1, tint: color(t, alpha: pulse)) }
                }
            }
        case "spectrum":
            switch v {
            case 0:
                gradient(p, width: width * 0.7, alpha: 0.32, colorOffset: phase)
                gradient(p, width: width * 0.2, alpha: 1, colorOffset: phase)
            case 1:
                for band in 0..<7 { let shift = CGFloat(band) * width * 0.19; gradient(p, width: width * 0.28, alpha: 0.06 + 0.08 * Double(7 - band), offset: shift, colorOffset: phase + Double(band) * 0.07, wave: width * 0.2, frequency: 3) }
            case 2:
                for track in 0..<3 { gradient(p, width: max(1, width * 0.08), alpha: 0.9 - Double(track) * 0.2, offset: CGFloat(track) * width * 0.6, colorOffset: -phase + Double(track) * 0.2) }
                for index in 0..<60 { stroke(p, start: Double(index) / 60, length: 0.001, width: width * 1.6, tint: color(Double(index) / 60 + phase, alpha: 0.32)) }
            case 3:
                gradient(p, width: width * 0.22, alpha: 0.95, colorOffset: phase, wave: width * 0.5, frequency: 6)
                gradient(p, width: width * 0.22, alpha: 0.7, colorOffset: phase + 0.5, wave: -width * 0.5, frequency: 6)
            default:
                gradient(p, width: width * CGFloat(0.45 + breathe), alpha: 0.18 + breathe * 0.3, colorOffset: phase)
                for index in 0..<18 { let t = Double(index) / 18; stroke(p, start: t, length: 0.026, width: 2.5, tint: color(t + phase, alpha: breathe)) }
            }
        default:
            switch v {
            case 0:
                for ring in 0..<4 { let progress = (time * style.speed * 0.35 + Double(ring) / 4).truncatingRemainder(dividingBy: 1); gradient(p, width: max(1, width * 0.12), alpha: (1 - progress) * 0.9, offset: CGFloat(progress) * width * 3.5, colorOffset: Double(ring) / 4 + 0.1) }
            case 1:
                let progress = phase - floor(phase)
                gradient(p, width: 1, alpha: 0.13)
                for step in 0..<80 { let t = progress * Double(step) / 80; stroke(p, start: t, length: progress / 80 + 0.001, width: width * 0.22, tint: color(t, alpha: 0.85)) }
                dot(p, at: progress, radius: width * 0.3, tint: color(progress))
            case 2:
                for index in 0..<24 { let t = Double(index) / 24; let pulse = 0.25 + 0.75 * pow(abs(sin(Double(index) * 0.3 - time * style.speed)), 2); stroke(p, start: t, length: 0.027, width: width * CGFloat(0.15 + pulse * 0.3), tint: color(t + phase, alpha: pulse)) }
            case 3:
                for ring in 0..<5 { let strength = 0.15 + 0.5 * abs(sin(time * style.speed - Double(ring) * 0.55)); gradient(p, width: 1.5, alpha: strength, offset: CGFloat(ring) * width * 0.55, colorOffset: Double(ring) / 5 - phase) }
            default:
                for index in 0..<12 { let t = Double(index) / 12 + phase * 0.3; stroke(p, start: t, length: 0.042, width: width * CGFloat(0.16 + breathe * 0.25), tint: color(Double(index) / 12, alpha: 0.8), offset: width * CGFloat(sin(time + Double(index))) * 0.5) }
            }
        }
    }

    static func bitmap(style: CueVisualStyle, size: NSSize, time: TimeInterval, background: Bool) -> NSBitmapImageRep {
        let width = max(16, Int(size.width)), height = max(16, Int(size.height))
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: width * 4, bitsPerPixel: 32)!
        let context = NSGraphicsContext(bitmapImageRep: rep)!
        NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current = context
        NSColor.clear.setFill(); NSRect(x: 0, y: 0, width: width, height: height).fill(using: .copy)
        if background {
            NSColor(srgbRed: 0.07, green: 0.085, blue: 0.105, alpha: 1).setFill()
            NSBezierPath(roundedRect: NSRect(x: 0, y: 0, width: width, height: height), xRadius: 8, yRadius: 8).fill()
        }
        let view = CueEdgeEffectView(frame: NSRect(x: 0, y: 0, width: width, height: height), style: style)
        view.elapsed = time; view.forcedMotionForCapture = true
        // Draw onto a transparent temporary plane, preserving the preview's
        // neutral backdrop when composited by NSImage below.
        if background {
            let transparent = bitmap(style: style, size: size, time: time, background: false)
            let image = NSImage(size: size); image.addRepresentation(transparent)
            image.draw(in: NSRect(origin: .zero, size: size))
        } else { view.draw(view.bounds) }
        NSGraphicsContext.restoreGraphicsState()
        return rep
    }
}
