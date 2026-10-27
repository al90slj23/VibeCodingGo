import AppKit
import Foundation

struct CueEffectTag { let id: String; let name: String }
enum CueEffectPath { case perimeter, topEdge, corners }
enum CueEffectBrush { case ribbon, line, beam, dashes, points, squares, wave, ripple }
enum CueEffectMotionKind { case still, breathe, orbit, pulse }

enum CueEffectPresence: String, CaseIterable {
    case gentle, clear, strong
    var name: String { self == .gentle ? "柔和" : self == .clear ? "清晰" : "醒目" }
    var coreScale: Double { self == .gentle ? 0.72 : self == .clear ? 1 : 1.45 }
    var peakGain: Double { self == .gentle ? 0.78 : self == .clear ? 1.35 : 1.70 }
    var motionScale: Double { self == .gentle ? 2.3 : self == .clear ? 3.5 : 4.8 }
    func haloSpread(for width: CGFloat) -> CGFloat {
        let scale = self == .gentle ? 1.7 : self == .clear ? 2.7 : 3.8
        return min(28, max(self == .gentle ? 4.5 : self == .clear ? 8 : 11, width * scale))
    }
}

/// An optical light field, shaded along the local screen normal independently
/// of its path, moving head/tail and lifetime envelope.
struct CueEffectMaterial {
    var body: Double = 0.86
    var occlusion: Double = 0.20
    var specular: Double = 0.72
    var bloom: Double = 0.42
    var reflection: Double = 0.16
    var roughness: Double = 28
    var whiteness: Double = 0.12
    var dispersion: Double = 0
    var bodyWidth: Double = 1.25
    var edgeSoftness: Double = 0.78
    var doubleRefraction: Double = 0
    var reflectionOffset: Double = 1.7
}

/// Geometry, paint, movement and lifetime are independent recipe components.
struct CueEffectLayer {
    let brush: CueEffectBrush
    var width: Double = 0.10
    var minimumWidth: Double = 0.65
    var opacity: Double = 0.8
    var offset: Double = 0
    var count: Int = 1
    var length: Double = 0.14
    var phase: Double = 0
    var colorOffset: Double = 0
    var glow: Double = 0
    var frequency: Double = 6
    var amplitude: Double = 0
    var movement: Double = 1
}
struct CueEffectMotion {
    let kind: CueEffectMotionKind
    var period: Double = 12
    func sample(at time: TimeInterval, speed: Double, direction: String, reduceMotion: Bool) -> (phase: Double, strength: Double, sign: Double) {
        if reduceMotion { return (0.18, 1, 1) }
        let progress = max(0, time) * speed / max(0.2, period)
        let phase = direction == "counterclockwise" ? -progress : direction == "pingpong" ? 0.5 - 0.5 * cos(progress * .pi * 2) : progress
        let sign = direction == "counterclockwise" ? -1.0 : direction == "pingpong" && sin(progress * .pi * 2) < 0 ? -1.0 : 1.0
        switch kind {
        case .still: return (0, 1, 1)
        case .breathe: return (0, 0.82 + 0.18 * sin(progress * .pi * 2), 1)
        case .orbit: return (phase, 1, sign)
        case .pulse: return (phase, 0.58 + 0.42 * pow(max(0, sin(progress * .pi * 2)), 2), sign)
        }
    }
}
struct CueEffectEnvelope {
    var fadeIn: TimeInterval = 0.4
    var fadeOut: TimeInterval = 0.6
    func opacity(at time: TimeInterval, duration: TimeInterval?, reduceMotion: Bool) -> Double {
        if reduceMotion { return 1 }
        func smooth(_ value: Double) -> Double { let t = min(1, max(0, value)); return t * t * (3 - 2 * t) }
        let entering = fadeIn > 0 ? smooth(time / fadeIn) : 1
        guard let duration else { return entering }
        let leaving = fadeOut > 0 ? smooth((duration - time) / fadeOut) : (time < duration ? 1 : 0)
        return min(entering, leaving)
    }
}
struct CueEffectRecipe {
    var path: CueEffectPath = .perimeter
    let layers: [CueEffectLayer]
    var motion: CueEffectMotion = .init(kind: .still)
    var envelope = CueEffectEnvelope()
    var material: CueEffectMaterial?
}
struct CueEffectPreset {
    let id: String
    let name: String
    let family: String
    let variant: Int
    let defaultPalette: [String]
    let tagIDs: [String]
    let recipe: CueEffectRecipe
}

enum CueEffectCatalog {
    static let defaultID = "luminous-flow"
    static let tags: [CueEffectTag] = [
        .init(id: "recommended", name: "推荐"),
        .init(id: "lighting", name: "光影"), .init(id: "minimal", name: "极简"),
        .init(id: "flow", name: "流动"), .init(id: "corners", name: "角落"),
        .init(id: "stationary", name: "静态"), .init(id: "classic", name: "经典"),
        .init(id: "marquee", name: "跑马灯")
    ]
    static let recommendedPresets: [CueEffectPreset] = [
        .init(id: "luminous-flow", name: "柔光流体", family: "lighting", variant: 0,
              defaultPalette: ["#64B6D7", "#A29BD7"], tagIDs: ["recommended", "lighting", "marquee", "flow"],
              recipe: .init(layers: [.init(brush: .ribbon, width: 1, minimumWidth: 2.2, opacity: 0.88)],
                            motion: .init(kind: .orbit, period: 10), material: .init())),
        .init(id: "pearl-flow", name: "珍珠流光", family: "lighting", variant: 1,
              defaultPalette: ["#B7CDD7", "#C7BED4"], tagIDs: ["recommended", "lighting", "marquee", "flow"],
              recipe: .init(layers: [.init(brush: .ribbon, width: 1, minimumWidth: 2.2, opacity: 0.86)],
                            motion: .init(kind: .orbit, period: 14), material: .init(body: 0.64, occlusion: 0.12, specular: 0.82, bloom: 0.22, reflection: 0.46, roughness: 42, whiteness: 0.30, dispersion: 0.035, bodyWidth: 0.96, edgeSoftness: 0.58, reflectionOffset: 0.92))),
        .init(id: "prism-flow", name: "棱镜流光", family: "lighting", variant: 2,
              defaultPalette: ["#6ABED2", "#8B96D1", "#C8A7C9"], tagIDs: ["recommended", "lighting", "marquee", "flow"],
              recipe: .init(layers: [.init(brush: .ribbon, width: 1, minimumWidth: 2.2, opacity: 0.90)],
                            motion: .init(kind: .orbit, period: 12), material: .init(body: 0.82, occlusion: 0.14, specular: 0.68, bloom: 0.29, reflection: 0.17, roughness: 22, whiteness: 0.08, dispersion: 0.42, bodyWidth: 0.78, edgeSoftness: 0.52, doubleRefraction: 0.90, reflectionOffset: 1.4)))
    ]
    static let minimalPresets: [CueEffectPreset] = {
        let palette = ["#A5B8CB", "#D4DEE7"]
        func preset(_ id: String, _ name: String, _ tags: [String], _ recipe: CueEffectRecipe) -> CueEffectPreset {
            .init(id: id, name: name, family: "quiet", variant: 0, defaultPalette: palette, tagIDs: ["minimal"] + tags, recipe: recipe)
        }
        return [
            preset("quiet-edge", "轻柔边光", ["stationary"], .init(layers: [
                .init(brush: .line, width: 0.42, opacity: 0.035),
                .init(brush: .line, width: 0.055, minimumWidth: 1.2, opacity: 0.80, glow: 0.35)])),
            preset("quiet-beam", "单束流光", ["flow"], .init(layers: [
                .init(brush: .line, width: 0.035, opacity: 0.08),
                .init(brush: .beam, width: 0.10, minimumWidth: 1.25, opacity: 0.85, length: 0.13, glow: 0.4)], motion: .init(kind: .orbit, period: 16))),
            preset("quiet-corners", "角落提示", ["corners", "stationary"], .init(path: .corners, layers: [
                .init(brush: .line, width: 0.08, minimumWidth: 1.2, opacity: 0.82, glow: 0.28)])),
            preset("quiet-top", "顶部微光", ["stationary"], .init(path: .topEdge, layers: [
                .init(brush: .line, width: 0.50, opacity: 0.025),
                .init(brush: .line, width: 0.07, minimumWidth: 1.2, opacity: 0.80)])),
            preset("quiet-outline", "静息细线", ["stationary"], .init(layers: [
                .init(brush: .line, width: 0.035, minimumWidth: 1.2, opacity: 0.82, offset: 0.10)])),
            preset("quiet-breath", "缓缓呼吸", ["flow"], .init(layers: [
                .init(brush: .line, width: 0.25, opacity: 0.055, glow: 0.2),
                .init(brush: .line, width: 0.075, minimumWidth: 1.2, opacity: 0.80)], motion: .init(kind: .breathe, period: 8)))
        ]
    }()
    // The old IDs are preserved; their paint now uses the same components.
    static let legacyPresets: [CueEffectPreset] = {
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
                CueEffectPreset(id: family + "-" + id, name: names[index], family: family, variant: index, defaultPalette: palette,
                                tagIDs: ["classic", family == "corner" ? "corners" : "flow"] + (["marquee", "dash"].contains(family) ? ["marquee"] : []),
                                recipe: legacyRecipe(family: family, variant: index))
            }
        }
    }()
    static let presets = recommendedPresets + minimalPresets + legacyPresets

    private static func legacyRecipe(family: String, variant: Int) -> CueEffectRecipe {
        let v = Double(variant), base = CueEffectLayer(brush: .line, width: 0.04, opacity: 0.10)
        let motion = CueEffectMotion(kind: .orbit, period: 7.5 + v * 0.9)
        // Data composition only: the renderer never switches on IDs or families.
        switch family {
        case "glow":
            return .init(layers: [
                .init(brush: .line, width: 0.75 + v * 0.12, opacity: 0.08, offset: v * 0.04),
                .init(brush: .line, width: 0.08 + v * 0.035, opacity: 0.72, offset: v * 0.18, colorOffset: v * 0.12, glow: 0.5)
            ], motion: .init(kind: variant == 1 || variant == 3 ? .orbit : .breathe, period: 4.5 + v))
        case "marquee":
            if variant == 2 {
                return .init(layers: [base,
                    .init(brush: .beam, width: 0.29, opacity: 0.86, length: 0.16, glow: 0.22),
                    .init(brush: .beam, width: 0.20, opacity: 0.70, offset: 0.42,
                          length: 0.12, phase: 0.5, colorOffset: 0.5, glow: 0.15, movement: -1)], motion: motion)
            }
            let brushes: [CueEffectBrush] = [.beam, .dashes, .beam, .points, .squares]
            return .init(layers: [base, .init(brush: brushes[variant], width: 0.24 + v * 0.025, opacity: 0.86,
                count: variant == 2 ? 2 : variant < 3 ? 1 : 36 + variant * 4,
                length: 0.12 + v * 0.018, phase: v * 0.05, glow: 0.22)], motion: motion)
        case "comet":
            return .init(layers: [base,
                .init(brush: .beam, width: 0.22 + v * 0.035, opacity: 0.9, count: variant == 1 ? 2 : variant == 2 ? 3 : 1,
                      length: 0.19 + v * 0.035, phase: v * 0.06, glow: 0.35),
                .init(brush: variant == 3 ? .points : .line, width: 0.035 + v * 0.012, opacity: variant == 3 ? 0.4 : 0.08,
                      offset: 0.5, count: 15 + variant * 3)], motion: motion)
        case "dash":
            return .init(layers: [
                .init(brush: variant == 2 ? .squares : .dashes, width: 0.14 + v * 0.035, opacity: 0.83,
                      count: 24 + variant * 7, length: 0.014 + v * 0.002, glow: 0.2),
                .init(brush: .line, width: 0.035, opacity: 0.12 + v * 0.025, offset: v * 0.2)], motion: motion)
        case "scanner":
            return .init(layers: [base,
                .init(brush: .beam, width: 0.16 + v * 0.10, opacity: 0.82, count: variant == 2 ? 2 : 1,
                      length: 0.13 + v * 0.045, phase: v * 0.13, glow: 0.15),
                .init(brush: .points, width: 0.13, opacity: 0.65, count: 1 + variant, phase: 0.10)],
                motion: .init(kind: .orbit, period: 5.5 + v * 0.7))
        case "corner":
            return .init(path: .corners, layers: [
                .init(brush: .line, width: 0.10 + v * 0.065, opacity: 0.85, glow: 0.3),
                .init(brush: .line, width: 0.045 + v * 0.01, opacity: 0.38, offset: 0.35 + v * 0.15, colorOffset: 0.4)],
                motion: .init(kind: .breathe, period: 3.5 + v * 0.8))
        case "wave":
            return .init(layers: [
                .init(brush: variant == 4 ? .ripple : .wave, width: 0.09 + v * 0.016, opacity: 0.78,
                      count: variant == 4 ? 4 : 1, colorOffset: v * 0.1, frequency: 4 + v * 1.8, amplitude: 0.15 + v * 0.08),
                .init(brush: .wave, width: 0.05 + v * 0.01, opacity: 0.28, offset: 0.45, phase: 0.5,
                      colorOffset: 0.5, frequency: 4.5 + v, amplitude: 0.25)], motion: motion)
        case "particle":
            return .init(layers: [base,
                .init(brush: variant == 4 ? .squares : .points, width: 0.08 + v * 0.025, opacity: 0.85,
                      offset: 0.10 + v * 0.12, count: 21 + variant * 9, phase: v * 0.08, glow: 0.3)], motion: motion)
        case "spectrum":
            return .init(layers: [
                .init(brush: .line, width: 0.30 + v * 0.15, opacity: 0.23, colorOffset: v * 0.1, glow: 0.25),
                .init(brush: variant == 2 ? .dashes : .wave, width: 0.10 + v * 0.02, opacity: 0.84,
                      offset: v * 0.1, count: 48, length: 0.011, phase: 0.3, colorOffset: 0.25,
                      frequency: 3 + v, amplitude: 0.08 + v * 0.07)], motion: motion)
        default:
            return .init(layers: [
                .init(brush: variant == 1 ? .beam : variant == 2 ? .dashes : .ripple, width: 0.08 + v * 0.025,
                      opacity: 0.82, count: variant == 2 ? 24 : 3 + variant, length: 0.20 + v * 0.04,
                      phase: v * 0.08, colorOffset: 0.2, glow: 0.15),
                .init(brush: .line, width: 0.04, opacity: 0.10, offset: v * 0.12)], motion: .init(kind: .pulse, period: 4 + v))
        }
    }
    static func preset(id: String) -> CueEffectPreset { presets.first { $0.id == id } ?? recommendedPresets[0] }
    static func previewImage(for preset: CueEffectPreset, size: NSSize = NSSize(width: 180, height: 96), time: TimeInterval = 1.35) -> NSImage {
        let style = CueVisualStyle(fields: ["effect": preset.id, "palette": preset.defaultPalette.joined(separator: ","), "width": "12", "inset": "7"], fallback: .systemMint)
        let image = NSImage(size: size)
        image.addRepresentation(CueEdgeEffectView.bitmap(style: style, size: size, time: time, background: true))
        return image
    }
    static func renderContactSheet(to url: URL) throws {
        let columns = 5, rows = Int(ceil(Double(presets.count) / 5)), cell = NSSize(width: 330, height: 188)
        let size = NSSize(width: cell.width * CGFloat(columns), height: cell.height * CGFloat(rows))
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width), pixelsHigh: Int(size.height), bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: Int(size.width) * 4, bitsPerPixel: 32)!
        NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)!
        NSColor(srgbRed: 0.045, green: 0.055, blue: 0.07, alpha: 1).setFill(); NSRect(origin: .zero, size: size).fill()
        for (index, preset) in presets.enumerated() {
            let x = CGFloat(index % columns) * cell.width, y = size.height - CGFloat(index / columns + 1) * cell.height
            previewImage(for: preset, size: NSSize(width: cell.width - 24, height: cell.height - 48)).draw(in: NSRect(x: x + 12, y: y + 38, width: cell.width - 24, height: cell.height - 48))
            (String(format: "%02d  ", index + 1) + preset.name).draw(in: NSRect(x: x + 16, y: y + 10, width: cell.width - 32, height: 22), withAttributes: [.font: NSFont.systemFont(ofSize: 12, weight: .medium), .foregroundColor: NSColor.white.withAlphaComponent(0.88)])
        }
        NSGraphicsContext.restoreGraphicsState(); try rep.representation(using: .png, properties: [:])!.write(to: url, options: .atomic)
    }
    static func validationReport() -> [String: Bool] {
        let fallback = NSColor(srgbRed: 0.2, green: 0.8, blue: 0.6, alpha: 1)
        let bounded = CueVisualStyle(fields: ["speed": "nan", "width": "999", "opacity": "-4", "inset": "-1", "trail": "nan", "palette": "garbage,#123456,#ABCDEF", "direction": "invalid"], fallback: fallback)
        func hashes(_ items: [CueEffectPreset]) -> Set<UInt64> {
            Set(items.map { preset in
                let style = CueVisualStyle(fields: ["effect": preset.id, "palette": "#47EDB9,#70A7FF,#FA74C8", "width": "10", "inset": "5"], fallback: fallback)
                let rep = CueEdgeEffectView.bitmap(style: style, size: NSSize(width: 180, height: 106), time: 1.41, background: false)
                guard let bytes = rep.bitmapData else { return 0 }
                return (0..<(rep.bytesPerRow * rep.pixelsHigh)).reduce(UInt64(1469598103934665603)) { ($0 ^ UInt64(bytes[$1])) &* 1099511628211 }
            })
        }
        let envelope = CueEffectEnvelope(), steady = CueEffectMotion(kind: .orbit).sample(at: 8, speed: 2, direction: "clockwise", reduceMotion: true)
        return [
            "legacy_50_ids_preserved": legacyPresets.count == 50 && Set(legacyPresets.map(\.id)).count == 50 && legacyPresets.allSatisfy { preset(id: $0.id).id == $0.id },
            "three_lighting_recommendations": recommendedPresets.count == 3 && recommendedPresets.allSatisfy { $0.tagIDs.contains("lighting") && $0.recipe.material != nil } && legacyPresets.allSatisfy { !$0.tagIDs.contains("recommended") },
            "six_minimal_ids_preserved": minimalPresets.count == 6 && minimalPresets.allSatisfy { $0.tagIDs.contains("minimal") },
            "stable_unique_effect_ids": Set(presets.map(\.id)).count == presets.count,
            "default_is_recommended": recommendedPresets.contains { $0.id == defaultID } && CueVisualStyle(fields: [:], fallback: fallback).effectID == defaultID,
            "recipes_use_shared_components": presets.allSatisfy { !$0.recipe.layers.isEmpty && $0.recipe.layers.count <= 4 && $0.recipe.layers.allSatisfy { $0.width > 0 && $0.minimumWidth > 0 && (0...1).contains($0.opacity) && $0.count > 0 } },
            "recommended_cores_have_visible_minimum": recommendedPresets.allSatisfy { preset in
                preset.recipe.layers.contains { $0.minimumWidth >= 2.2 && $0.opacity >= 0.80 }
            },
            "effect_tags_valid": presets.allSatisfy { !$0.tagIDs.isEmpty && Set($0.tagIDs).isSubset(of: Set(tags.map(\.id))) },
            "bounded_finite_controls": bounded.speed.isFinite && bounded.width == 80 && bounded.opacity == 0.1 && bounded.inset == 0 && bounded.trail.isFinite,
            "palette_validation": bounded.paletteHex == ["#123456", "#ABCDEF"], "direction_validation": bounded.direction == "clockwise",
            "recommended_renders_distinct": hashes(recommendedPresets).count == 3, "legacy_renders_distinct": hashes(legacyPresets).count == 50,
            "presence_defaults_clear_and_roundtrips": bounded.presence == .clear && CueVisualStyle.normalizedFields(["presence": "strong"], fallback: fallback)["presence"] == "strong",
            "envelope_separate_from_movement": envelope.opacity(at: 0, duration: 8, reduceMotion: false) == 0 && envelope.opacity(at: 1, duration: 8, reduceMotion: false) == 1 && envelope.opacity(at: 8, duration: 8, reduceMotion: false) == 0,
            "reduce_motion_static_visible": steady.phase == 0.18 && steady.strength == 1 && envelope.opacity(at: 0, duration: 8, reduceMotion: true) == 1
        ]
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
    let presence: CueEffectPresence

    init(fields: [String: String], fallback: NSColor) {
        effectID = CueEffectCatalog.preset(id: fields["effect"] ?? CueEffectCatalog.defaultID).id
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
        presence = CueEffectPresence(rawValue: fields["presence"] ?? "") ?? .clear
    }

    static func number(_ text: String?, fallback: Double, low: Double, high: Double) -> Double {
        guard let text, let value = Double(text), value.isFinite else { return fallback }
        return min(high, max(low, value))
    }

    static func normalizedFields(_ fields: [String: String], fallback: NSColor) -> [String: String] {
        let style = CueVisualStyle(fields: fields, fallback: fallback)
        var result = fields
        result["effect"] = fields["effect"] == "random" ? "random" : style.effectID
        result["palette"] = style.paletteHex.joined(separator: ",")
        result["direction"] = style.direction; result["speed"] = String(style.speed)
        result["width"] = String(Double(style.width)); result["opacity"] = String(style.opacity)
        result["inset"] = String(Double(style.inset)); result["trail"] = String(style.trail)
        result["card_style"] = style.cardID
        result["presence"] = style.presence.rawValue
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

/// Every preset uses this renderer. No ID, family or variant drawing branches.
private struct CueEffectRenderer {
    let style: CueVisualStyle
    let recipe: CueEffectRecipe
    let time: TimeInterval
    let reduceMotion: Bool
    let duration: TimeInterval?
    let capture: Bool

    private struct RGB {
        var r: Double; var g: Double; var b: Double
        func mixed(with other: RGB, amount: Double) -> RGB {
            let t = min(1, max(0, amount))
            return .init(r: r + (other.r - r) * t, g: g + (other.g - g) * t, b: b + (other.b - b) * t)
        }
        func scaled(_ value: Double) -> RGB { .init(r: r * value, g: g * value, b: b * value) }
    }
    private struct LightSample {
        var r = 0.0; var g = 0.0; var b = 0.0; var a = 0.0
        mutating func over(_ color: RGB, alpha: Double) {
            let opacity = min(0.985, max(0, alpha)), remain = 1 - opacity
            r = color.r * opacity + r * remain; g = color.g * opacity + g * remain
            b = color.b * opacity + b * remain; a = opacity + a * remain
        }
        var components: [CGFloat] {
            guard a > 0.00001 else { return [0, 0, 0, 0] }
            return [CGFloat(min(1, max(0, r / a))), CGFloat(min(1, max(0, g / a))),
                    CGFloat(min(1, max(0, b / a))), CGFloat(a)]
        }
    }
    private struct RibbonNode { let t: Double; let point: NSPoint; let normal: NSPoint }

    func draw(in bounds: NSRect) {
        guard bounds.width > 8, bounds.height > 8 else { return }
        let width = min(style.width * style.presence.coreScale, min(bounds.width, bounds.height) / 5)
        let spill = style.presence.haloSpread(for: width)
        let perimeter = CuePerimeter(bounds: bounds, inset: style.inset + width * 0.6 + min(8, spill * 0.35))
        let motion = recipe.motion.sample(at: time, speed: style.speed, direction: style.direction, reduceMotion: reduceMotion)
        let envelope = capture ? 1 : recipe.envelope.opacity(at: time, duration: duration, reduceMotion: reduceMotion)
        guard envelope > 0 else { return }
        let alpha = envelope * motion.strength
        let mainLayers = recipe.layers.filter { $0.opacity >= 0.25 }
        let referenceWidth = max(0.001, mainLayers.map(\.width).max() ?? 1)
        let referenceOpacity = max(0.001, mainLayers.map(\.opacity).max() ?? 1)
        let geometryUnit = width * style.presence.motionScale
        for layer in recipe.layers {
            let phase = motion.phase * layer.movement + layer.phase
            let ratio = layer.opacity < 0.25 ? min(4, layer.width / referenceWidth) : layer.width / referenceWidth
            let lineWidth = max(CGFloat(layer.minimumWidth) * style.presence.coreScale, width * CGFloat(ratio))
            let offset = geometryUnit * CGFloat(layer.offset)
            let layerAlpha = alpha * min(1, layer.opacity / referenceOpacity) * style.presence.peakGain
            switch layer.brush {
            case .ribbon:
                drawRibbon(perimeter, width: lineWidth, phase: phase, sign: motion.sign * layer.movement,
                           alpha: layerAlpha * style.opacity, material: recipe.material ?? CueEffectMaterial())
            case .line, .wave:
                for (start, length) in ranges(perimeter, width: width) {
                    drawStroke(perimeter, start: start, length: length, width: lineWidth, alpha: layerAlpha, offset: offset,
                               colorOffset: phase + layer.colorOffset, glow: layer.glow, wave: layer.brush == .wave ? geometryUnit * CGFloat(layer.amplitude) : 0,
                               frequency: layer.frequency, phase: phase)
                }
            case .beam:
                let tail = min(0.65, max(0.025, layer.length * style.trail / 0.18))
                for index in 0..<layer.count {
                    let head = phase + Double(index) / Double(layer.count)
                    for step in 0..<20 {
                        let progress = Double(step) / 20
                        let start = head - motion.sign * layer.movement * tail * (1 - progress)
                        drawStroke(perimeter, start: start, length: tail / 20 + 0.0005, width: lineWidth * CGFloat(0.25 + 0.75 * progress),
                                   alpha: layerAlpha * pow(progress, 1.65), offset: offset, colorOffset: layer.colorOffset + progress * 0.2, glow: layer.glow)
                    }
                }
            case .dashes:
                let count = layer.count == 1 ? 36 : layer.count
                for index in 0..<count {
                    drawStroke(perimeter, start: Double(index) / Double(count) + phase, length: min(layer.length, 0.65 / Double(count)),
                               width: lineWidth, alpha: layerAlpha, offset: offset, colorOffset: Double(index) / Double(count) + layer.colorOffset, glow: layer.glow)
                }
            case .points, .squares:
                for index in 0..<layer.count {
                    let position = Double(index) / Double(layer.count) + phase
                    let point = locate(perimeter, at: position, offset: offset)
                    let radius = max(0.8, lineWidth * 0.58)
                    let rect = NSRect(x: point.x - radius, y: point.y - radius, width: radius * 2, height: radius * 2)
                    let tint = color(at: position + layer.colorOffset, alpha: layerAlpha)
                    for scale in [1.0, 0.65, 0.32] {
                        let expansion = spill * scale
                        color(at: position + layer.colorOffset, alpha: layerAlpha * 0.035).setFill()
                        NSBezierPath(ovalIn: rect.insetBy(dx: -expansion, dy: -expansion)).fill()
                    }
                    NSColor.black.withAlphaComponent(CGFloat(min(0.28, layerAlpha * style.opacity * 0.22))).setFill()
                    NSBezierPath(ovalIn: rect.insetBy(dx: -0.7, dy: -0.7)).fill()
                    tint.setFill(); if layer.brush == .squares { rect.fill() } else { NSBezierPath(ovalIn: rect).fill() }
                }
            case .ripple:
                for ring in 0..<layer.count {
                    let value = phase + Double(ring) / Double(layer.count)
                    let progress = value - floor(value)
                    drawStroke(perimeter, start: 0, length: 1, width: lineWidth, alpha: layerAlpha * (1 - progress),
                               offset: offset + geometryUnit * CGFloat(progress * 1.1), colorOffset: Double(ring) / Double(layer.count) + layer.colorOffset, glow: layer.glow)
                }
            }
        }
    }

    /// A continuous shaded strip, not a chain of round strokes. Adjacent cells
    /// share vertices and interpolate their end colors; no overlapping LED caps.
    /// Volume density tapers with the moving tail; specular light and a small
    /// shadow stay near its head. Reflection and bloom are separate channels.
    private func drawRibbon(_ perimeter: CuePerimeter, width: CGFloat, phase: Double, sign: Double,
                            alpha: Double, material: CueEffectMaterial) {
        guard let context = NSGraphicsContext.current?.cgContext, alpha > 0 else { return }
        let half = min(max(1.15, width * 0.70), perimeter.radius * 0.68)
        let extent = max(half * 2.7, style.presence.haloSpread(for: width))
        let steps = max(72, min(160, Int(perimeter.length / 64)))
        var fractions = (0...steps).map { Double($0) / Double(steps) }
        // Resolve the bright head locally instead of increasing every straight
        // edge's tessellation. Long tails still interpolate continuously.
        for shift in [-0.028, -0.018, -0.012, -0.006, 0, 0.006, 0.012, 0.018, 0.028] {
            let position = phase + shift
            fractions.append(position - floor(position))
        }
        let arcStarts = [perimeter.horizontal, perimeter.horizontal + perimeter.arc + perimeter.vertical,
                         perimeter.horizontal * 2 + perimeter.arc * 2 + perimeter.vertical,
                         perimeter.horizontal * 2 + perimeter.arc * 3 + perimeter.vertical * 2]
        for start in arcStarts {
            for step in 0...8 { fractions.append(Double((start + perimeter.arc * CGFloat(step) / 8) / perimeter.length)) }
        }
        fractions.sort()
        fractions = fractions.reduce(into: [Double]()) { result, value in
            if result.last.map({ abs($0 - value) > 1e-8 }) ?? true { result.append(value) }
        }
        let epsilon = 0.8 / Double(perimeter.length)
        let nodes = fractions.map { fraction -> RibbonNode in
            let before = perimeter.point(fraction - epsilon), after = perimeter.point(fraction + epsilon)
            let dx = after.x - before.x, dy = after.y - before.y, length = max(0.001, hypot(after.x - before.x, after.y - before.y))
            return .init(t: fraction, point: perimeter.point(fraction), normal: NSPoint(x: dy / length, y: -dx / length))
        }
        var offsets = [-1.0, -0.85, -0.65, -0.50, -0.40, -0.31, 0.31, 0.40, 0.50, 0.65, 0.85, 1].map { CGFloat($0) * extent }
        let radialSteps = max(20, Int(half * 2.5 / 0.30))
        for step in 0...radialSteps { offsets.append(half * (-1.25 + CGFloat(step) * 2.5 / CGFloat(radialSteps))) }
        offsets.append(-half); offsets.append(half)
        offsets.sort()
        let palette: [RGB] = style.palette.map {
            let color = $0.usingColorSpace(.sRGB) ?? .white
            return .init(r: Double(color.redComponent), g: Double(color.greenComponent), b: Double(color.blueComponent))
        }
        let white = RGB(r: 0.98, g: 0.99, b: 1), shadow = RGB(r: 0.012, g: 0.018, b: 0.028)
        let trail = min(0.82, max(0.18, style.trail * 2.4))
        func tint(_ position: Double) -> RGB {
            guard palette.count > 1 else { return palette[0] }
            let value = (position - floor(position)) * Double(palette.count)
            let lower = Int(value) % palette.count
            return palette[lower].mixed(with: palette[(lower + 1) % palette.count], amount: value - floor(value))
        }
        func shade(_ node: RibbonNode, offset: CGFloat) -> LightSample {
            let behind = (phase - node.t) * sign - floor((phase - node.t) * sign)
            let tail = behind < trail ? pow(max(0, 1 - behind / trail), 1.55) : 0
            let headDistance = min(behind, 1 - behind)
            let head = exp(-pow(headDistance / 0.012, 2))
            // The leading Gaussian joins both sides of the modular head. A
            // raw trailing-only ramp jumps at 0/1; using it for cross-section
            // width would cut a bright rectangular end into the moving body.
            let volume = tail + (1 - tail) * head
            let energy = min(1.28, 0.004 + volume * 0.92 + head * 0.34)
            let specularGate = min(1.15, head * 0.95 + pow(volume, 10) * 0.35)
            let taper = 0.12 + 0.88 * pow(volume, 0.40)
            let u = Double(offset), h = max(0.42, Double(half) * material.bodyWidth * taper), normalized = u / h
            let colorPosition = node.t - phase * 0.32 + material.dispersion * normalized * 0.16
            let base = tint(colorPosition).mixed(with: white, amount: material.whiteness)
            var sample = LightSample()
            let localSpill = max(1.4, Double(extent) * 0.43 * (0.24 + 0.76 * pow(volume, 0.30)))
            let spill = exp(-pow(u / localSpill, 2) * 0.5)
            sample.over(base, alpha: alpha * energy * material.bloom * spill * 0.46)
            let reflected = exp(-pow((u - h * material.reflectionOffset) / max(0.52, h * 0.30), 2) * 0.5)
            sample.over(tint(colorPosition + 0.08).mixed(with: white, amount: material.whiteness * 0.5),
                        alpha: alpha * energy * material.reflection * reflected * 0.44)
            let occlusion = exp(-pow((u - h * 1.02) / max(0.40, h * 0.18), 2) * 0.5)
            sample.over(shadow, alpha: alpha * specularGate * material.occlusion * occlusion * 0.28)
            // A density field with a soft boundary, not a hard cylindrical shell.
            let sigma = max(0.35, h * material.edgeSoftness)
            let density = exp(-pow(u / sigma, 2) * 0.5)
            let split = max(exp(-pow((u - h * 0.62) / max(0.28, sigma * 0.43), 2) * 0.5),
                            exp(-pow((u + h * 0.62) / max(0.28, sigma * 0.43), 2) * 0.5))
            let volumeDensity = density * (1 - material.doubleRefraction) + split * material.doubleRefraction
            let slope = tanh(normalized * 0.82), z = sqrt(max(0, 1 - slope * slope))
            let light = max(0.18, z * 0.82 + slope * (Double(node.normal.x) * -0.36 + Double(node.normal.y) * 0.44))
            let bodyColor = base.scaled(0.62 + 0.38 * light).mixed(with: white, amount: 0.025 * z)
            sample.over(bodyColor, alpha: alpha * energy * material.body * volumeDensity)
            let facing = Double(node.normal.x) * -0.6 + Double(node.normal.y) * 0.8
            let ridge = -h * (0.27 + 0.18 * facing)
            let specular = exp(-pow((u - ridge) / max(0.25, h * 0.15), 2) * 0.5)
            let secondRidge = exp(-pow((u + ridge) / max(0.22, h * 0.12), 2) * 0.5) * material.doubleRefraction * 0.48
            let gloss = pow(max(0, z * 0.94 + slope * facing * 0.18), material.roughness / 12)
            sample.over(white.mixed(with: base, amount: 0.18), alpha: alpha * specularGate * material.specular * (specular + secondRidge) * gloss)
            return sample
        }
        let space = CGColorSpaceCreateDeviceRGB()
        context.saveGState()
        context.setBlendMode(.normal)
        context.beginTransparencyLayer(auxiliaryInfo: nil)
        // Coverage contributions add inside an isolated surface. This avoids
        // doubled opacity at shared AA edges, then composites the whole surface
        // normally over the desktop. Radial body samples are <=0.3 points apart.
        context.setBlendMode(.plusLighter)
        context.setShouldAntialias(true)
        func point(_ node: RibbonNode, _ offset: CGFloat) -> NSPoint {
            let bounded = min(offset, perimeter.radius - 0.75)
            return NSPoint(x: node.point.x + node.normal.x * bounded, y: node.point.y + node.normal.y * bounded)
        }
        for band in 0..<(offsets.count - 1) {
            let midpoint = (offsets[band] + offsets[band + 1]) / 2
            var previous = shade(nodes[0], offset: midpoint)
            for index in 0..<(nodes.count - 1) {
                let a = nodes[index], b = nodes[index + 1], next = shade(nodes[index + 1], offset: midpoint)
                defer { previous = next }
                guard max(previous.a, next.a) > 0.001 else { continue }
                let components = previous.components + next.components
                guard let gradient = CGGradient(colorSpace: space, colorComponents: components, locations: [0, 1], count: 2) else { continue }
                let quad = CGMutablePath()
                quad.move(to: point(a, offsets[band])); quad.addLine(to: point(b, offsets[band]))
                quad.addLine(to: point(b, offsets[band + 1])); quad.addLine(to: point(a, offsets[band + 1])); quad.closeSubpath()
                context.saveGState(); context.addPath(quad); context.clip()
                context.drawLinearGradient(gradient, start: point(a, midpoint), end: point(b, midpoint), options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])
                context.restoreGState()
            }
        }
        context.endTransparencyLayer()
        context.restoreGState()
    }
    private func ranges(_ perimeter: CuePerimeter, width: CGFloat) -> [(Double, Double)] {
        if recipe.path != .corners { return [(0, 1)] }
        let span = min(0.055, max(0.008, Double((18 + width) / perimeter.length)))
        let centers = [perimeter.horizontal + perimeter.arc / 2, perimeter.horizontal + perimeter.vertical + perimeter.arc * 1.5,
                       perimeter.horizontal * 2 + perimeter.vertical + perimeter.arc * 2.5, perimeter.horizontal * 2 + perimeter.vertical * 2 + perimeter.arc * 3.5]
        return centers.map { (Double($0 / perimeter.length) - span, span * 2) }
    }
    private func locate(_ perimeter: CuePerimeter, at fraction: Double, offset: CGFloat) -> NSPoint {
        if recipe.path == .topEdge {
            let t = fraction - floor(fraction)
            return NSPoint(x: perimeter.rect.minX + perimeter.radius + CGFloat(t) * perimeter.horizontal, y: perimeter.rect.maxY - offset)
        }
        return perimeter.point(fraction, offset: offset)
    }
    private func color(at position: Double, alpha: Double) -> NSColor {
        let colors = style.palette, value = (position - floor(position)) * Double(style.palette.count)
        let lower = Int(value) % colors.count, upper = (lower + 1) % colors.count
        let tint = colors.count == 1 ? colors[0] : (colors[lower].blended(withFraction: CGFloat(value - floor(value)), of: colors[upper]) ?? colors[lower])
        return tint.withAlphaComponent(CGFloat(min(1, max(0, alpha * style.opacity))))
    }
    private func drawStroke(_ perimeter: CuePerimeter, start: Double, length: Double, width: CGFloat, alpha: Double, offset: CGFloat,
                            colorOffset: Double, glow: Double, wave: CGFloat = 0, frequency: Double = 0, phase: Double = 0) {
        guard length > 0, alpha > 0 else { return }
        let segments = style.palette.count > 1 ? max(1, min(40, Int(length * 48))) : 1
        for segment in 0..<segments {
            let begin = start + length * Double(segment) / Double(segments), span = length / Double(segments), path = NSBezierPath()
            let physicalLength = recipe.path == .topEdge ? perimeter.horizontal : perimeter.length
            let steps = max(2, min(320, Int(Double(physicalLength) * span / 7)))
            for step in 0...steps {
                let fraction = begin + span * Double(step) / Double(steps)
                let shift = offset + wave * CGFloat(sin((fraction * frequency + phase) * .pi * 2))
                let position = recipe.path == .topEdge && fraction == 1 ? 1 - 1e-9 : fraction
                let point = locate(perimeter, at: position, offset: shift)
                if step == 0 { path.move(to: point) } else { path.line(to: point) }
            }
            path.lineCapStyle = segments > 1 ? .butt : .round
            path.lineJoinStyle = .round
            let position = begin + span / 2 + colorOffset
            let spread = style.presence.haloSpread(for: min(width, style.width * 1.5))
            for (radius, strength) in [(1.0, 0.035), (0.65, 0.055), (0.32, 0.075)] {
                path.lineWidth = width + spread * CGFloat(radius * 2)
                color(at: position, alpha: alpha * strength * (0.65 + glow)).setStroke(); path.stroke()
            }
            path.lineWidth = width + 1.4
            NSColor.black.withAlphaComponent(CGFloat(min(0.28, alpha * style.opacity * 0.20))).setStroke(); path.stroke()
            path.lineWidth = width; color(at: position, alpha: alpha).setStroke(); path.stroke()
        }
    }
}

final class CueEdgeEffectView: NSView {
    var style: CueVisualStyle { didSet { needsDisplay = true } }
    var elapsed: TimeInterval = 0
    var displayDuration: TimeInterval?
    private(set) var lastRenderDuration: TimeInterval = 0
    private(set) var renderedFrameCount = 0
    override var isOpaque: Bool { false }
    init(frame: NSRect, style: CueVisualStyle) {
        self.style = style; super.init(frame: frame); wantsLayer = true; layer?.backgroundColor = NSColor.clear.cgColor
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    func advance(to time: TimeInterval) {
        let next = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion ? 0 : max(0, time)
        guard abs(next - elapsed) >= 1.0 / 32.0 else { return }
        elapsed = next; needsDisplay = true
    }
    override func draw(_ dirtyRect: NSRect) {
        let started = ProcessInfo.processInfo.systemUptime
        defer {
            lastRenderDuration = ProcessInfo.processInfo.systemUptime - started
            renderedFrameCount += 1
        }
        NSColor.clear.setFill(); dirtyRect.fill(using: .copy); NSGraphicsContext.current?.imageInterpolation = .high
        CueEffectRenderer(style: style, recipe: CueEffectCatalog.preset(id: style.effectID).recipe, time: elapsed,
                          reduceMotion: NSWorkspace.shared.accessibilityDisplayShouldReduceMotion, duration: displayDuration, capture: false).draw(in: bounds)
    }
    static func bitmap(style: CueVisualStyle, size: NSSize, time: TimeInterval, background: Bool) -> NSBitmapImageRep {
        let width = max(16, Int(size.width)), height = max(16, Int(size.height))
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: width * 4, bitsPerPixel: 32)!
        NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)!
        NSColor.clear.setFill(); NSRect(x: 0, y: 0, width: width, height: height).fill(using: .copy)
        if background {
            NSColor(srgbRed: 0.07, green: 0.085, blue: 0.105, alpha: 1).setFill()
            NSBezierPath(roundedRect: NSRect(x: 0, y: 0, width: width, height: height), xRadius: 8, yRadius: 8).fill()
        }
        CueEffectRenderer(style: style, recipe: CueEffectCatalog.preset(id: style.effectID).recipe, time: time, reduceMotion: false, duration: nil, capture: true)
            .draw(in: NSRect(x: 0, y: 0, width: width, height: height))
        NSGraphicsContext.restoreGraphicsState(); return rep
    }
}
