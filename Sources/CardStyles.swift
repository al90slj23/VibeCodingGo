import AppKit
import Foundation

struct CueCardPreset {
    let id: String
    let name: String
    let size: NSSize
}

enum CueCardCatalog {
    static let presets: [CueCardPreset] = [
        .init(id: "glass-dark", name: "深色玻璃", size: NSSize(width: 560, height: 164)),
        .init(id: "frosted-glass", name: "磨砂悬浮", size: NSSize(width: 560, height: 174)),
        .init(id: "paper-light", name: "浅色纸笺", size: NSSize(width: 560, height: 164)),
        .init(id: "compact", name: "紧凑通知", size: NSSize(width: 460, height: 118)),
        .init(id: "pill", name: "灵动胶囊", size: NSSize(width: 560, height: 96)),
        .init(id: "minimal", name: "极简留白", size: NSSize(width: 500, height: 138)),
        .init(id: "terminal", name: "终端控制台", size: NSSize(width: 600, height: 178)),
        .init(id: "neon", name: "霓虹面板", size: NSSize(width: 560, height: 176)),
        .init(id: "outline", name: "线框视窗", size: NSSize(width: 540, height: 154)),
        .init(id: "ticket", name: "任务票据", size: NSSize(width: 540, height: 174)),
        .init(id: "bubble", name: "对话气泡", size: NSSize(width: 520, height: 178)),
        .init(id: "stacked", name: "层叠卡片", size: NSSize(width: 560, height: 182)),
        .init(id: "sidebar-accent", name: "侧栏信号", size: NSSize(width: 570, height: 176)),
        .init(id: "split-panel", name: "分栏任务台", size: NSSize(width: 620, height: 188)),
        .init(id: "badge-top", name: "徽章悬停", size: NSSize(width: 480, height: 220)),
        .init(id: "centered", name: "居中聚焦", size: NSSize(width: 520, height: 212)),
        .init(id: "ribbon", name: "飘带标题", size: NSSize(width: 560, height: 188)),
        .init(id: "toast-wide", name: "横幅快讯", size: NSSize(width: 680, height: 148)),
        .init(id: "micro", name: "迷你闪讯", size: NSSize(width: 450, height: 102)),
        .init(id: "timeline", name: "时间轴节点", size: NSSize(width: 560, height: 178)),
        .init(id: "chat-left", name: "左侧对话", size: NSSize(width: 540, height: 180)),
        .init(id: "bookmark", name: "书签页角", size: NSSize(width: 530, height: 188)),
        .init(id: "dashboard", name: "仪表卡片", size: NSSize(width: 620, height: 194)),
        .init(id: "code-editor", name: "代码编辑器", size: NSSize(width: 610, height: 200)),
        .init(id: "blueprint", name: "蓝图标注", size: NSSize(width: 570, height: 180)),
        .init(id: "pixel", name: "像素街机", size: NSSize(width: 520, height: 168)),
        .init(id: "cyber-grid", name: "赛博网格", size: NSSize(width: 580, height: 180)),
        .init(id: "hologram", name: "全息折面", size: NSSize(width: 560, height: 180)),
        .init(id: "aurora", name: "极光幕布", size: NSSize(width: 580, height: 180)),
        .init(id: "gradient-ribbon", name: "渐变腰封", size: NSSize(width: 570, height: 188)),
        .init(id: "sunrise", name: "晨光居中", size: NSSize(width: 520, height: 214)),
        .init(id: "mono-black", name: "纯黑留白", size: NSSize(width: 520, height: 156)),
        .init(id: "mono-white", name: "纯白字卡", size: NSSize(width: 520, height: 156)),
        .init(id: "warm-paper", name: "暖色信笺", size: NSSize(width: 550, height: 174)),
        .init(id: "notebook", name: "横线笔记", size: NSSize(width: 540, height: 178)),
        .init(id: "sticky", name: "便签折角", size: NSSize(width: 480, height: 178)),
        .init(id: "newspaper", name: "报纸简讯", size: NSSize(width: 580, height: 196)),
        .init(id: "postage", name: "邮票边框", size: NSSize(width: 550, height: 180)),
        .init(id: "receipt", name: "热敏小票", size: NSSize(width: 490, height: 194)),
        .init(id: "polaroid", name: "拍立得页眉", size: NSSize(width: 530, height: 204)),
        .init(id: "spotlight", name: "聚光舞台", size: NSSize(width: 540, height: 214)),
        .init(id: "orbit", name: "轨道信标", size: NSSize(width: 580, height: 180)),
        .init(id: "metro", name: "都市色块", size: NSSize(width: 560, height: 176)),
        .init(id: "floating-strip", name: "悬浮细条", size: NSSize(width: 620, height: 120)),
        .init(id: "corner-tab", name: "页角索引", size: NSSize(width: 550, height: 186)),
        .init(id: "progress", name: "进度光轨", size: NSSize(width: 570, height: 182)),
        .init(id: "wave", name: "声波浮层", size: NSSize(width: 580, height: 180)),
        .init(id: "dotted", name: "点阵札记", size: NSSize(width: 550, height: 174)),
        .init(id: "contour", name: "等高线条", size: NSSize(width: 580, height: 180)),
        .init(id: "frosted-light", name: "浅色磨砂", size: NSSize(width: 560, height: 180))
    ]
    static func preset(id: String) -> CueCardPreset { presets.first { $0.id == id } ?? presets[0] }
    static let positions: [(String, String)] = [("top-center", "顶部居中"), ("top-right", "右上角"), ("top-left", "左上角"), ("bottom-center", "底部居中"), ("bottom-right", "右下角"), ("bottom-left", "左下角"), ("center", "屏幕中央"), ("mouse", "鼠标附近")]

    static func previewImage(for preset: CueCardPreset, size: NSSize, fields: [String: String] = [:], color: NSColor = .systemTeal) -> NSImage {
        let event = CueEvent(kind: "input", title: "模型接入 · 等待你的选择", message: "请选择下一步：继续验证，或检查当前配置。")
        var selected = fields; selected["card_style"] = preset.id
        let palette = fields.isEmpty ? [NSColor.systemTeal, .systemBlue, .systemPurple] : CueVisualStyle(fields: fields, fallback: color).palette
        let card = CueStyledBannerView(event: event, target: nil, color: color, palette: palette, style: CueCardStyle(fields: selected))
        let image = NSImage(size: size)
        image.lockFocus()
        NSColor.clear.setFill(); NSRect(origin: .zero, size: size).fill(using: .copy)
        if let rep = card.bitmapImageRepForCachingDisplay(in: card.bounds) {
            card.cacheDisplay(in: card.bounds, to: rep)
            let source = NSImage(size: card.bounds.size); source.addRepresentation(rep)
            let scale = min((size.width - 14) / card.bounds.width, (size.height - 12) / card.bounds.height)
            let scaled = NSSize(width: card.bounds.width * scale, height: card.bounds.height * scale)
            source.draw(in: NSRect(x: (size.width - scaled.width) / 2, y: (size.height - scaled.height) / 2, width: scaled.width, height: scaled.height))
        }
        image.unlockFocus()
        return image
    }

    static func renderContactSheet(to url: URL) throws {
        let cell = NSSize(width: 640, height: 282), columns = 5
        let rows = Int(ceil(Double(presets.count) / Double(columns)))
        let sheet = NSImage(size: NSSize(width: cell.width * CGFloat(columns), height: cell.height * CGFloat(rows)))
        sheet.lockFocus()
        NSColor(srgbRed: 0.08, green: 0.09, blue: 0.11, alpha: 1).setFill(); NSRect(origin: .zero, size: sheet.size).fill()
        for (index, preset) in presets.enumerated() {
            let origin = NSPoint(x: CGFloat(index % columns) * cell.width, y: CGFloat(rows - 1 - index / columns) * cell.height)
            let image = previewImage(for: preset, size: NSSize(width: 620, height: 240))
            image.draw(in: NSRect(x: origin.x + 10, y: origin.y + 2, width: 620, height: 240))
            let label = String(format: "%02d  %@", index + 1, preset.name)
            label.draw(at: NSPoint(x: origin.x + 24, y: origin.y + 250), withAttributes: [.font: NSFont.systemFont(ofSize: 16, weight: .semibold), .foregroundColor: NSColor.white])
        }
        sheet.unlockFocus()
        if let tiff = sheet.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff), let data = rep.representation(using: .png, properties: [:]) { try data.write(to: url) }
    }

    static func validationReport() -> [String: Bool] {
        let event = CueEvent(kind: "input", title: "固定测试对话", message: "同一组内容用于验证实际渲染与布局。")
        var hashes = Set<UInt64>(), noOverlap = true
        var cardHitRouting = true, buttonHitRouting = true, futureControlRouting = true, previewDoesNotNavigate = true
        for preset in presets {
            let image = previewImage(for: preset, size: NSSize(width: 320, height: 150))
            if let data = image.tiffRepresentation {
                let hash = data.reduce(UInt64(1469598103934665603)) { ($0 ^ UInt64($1)) &* 1099511628211 }
                hashes.insert(hash)
            }
            for scale in ["0.8", "1", "1.25"] {
                let view = CueStyledBannerView(event: event, target: nil, color: .systemTeal, palette: [.systemTeal, .systemBlue], style: CueCardStyle(fields: ["card_style": preset.id, "text_scale": scale, "card_width": "380"]))
                let labels: [NSView] = [view.statusLabel, view.conversationLabel, view.messageLabel, view.openButton, view.closeButton].filter { !$0.isHidden }
                for (index, a) in labels.enumerated() {
                    if !view.bounds.contains(a.frame) { noOverlap = false }
                    for b in labels.dropFirst(index + 1) where a.frame.intersects(b.frame) { noOverlap = false }
                }
                func hit(_ item: NSView) -> NSView? { view.hitTest(NSPoint(x: item.frame.midX, y: item.frame.midY)) }
                for item in [view.statusLabel, view.conversationLabel, view.messageLabel, view.icon] where !item.isHidden {
                    if hit(item) !== view { cardHitRouting = false }
                }
                if view.hitTest(NSPoint(x: 5, y: view.bounds.midY)) !== view { cardHitRouting = false }
                for button in [view.openButton, view.closeButton] where !button.isHidden {
                    if hit(button) !== button { buttonHitRouting = false }
                }
                if view.accessibilityPerformPress() { previewDoesNotNavigate = false }
                let futureButton = NSButton(title: "独立操作", target: nil, action: nil)
                futureButton.frame = NSRect(x: 20, y: 45, width: 100, height: 24); view.addSubview(futureButton)
                if hit(futureButton) !== futureButton { futureControlRouting = false }
                futureButton.removeFromSuperview()
                let editableField = NSTextField(frame: NSRect(x: 20, y: 45, width: 100, height: 24))
                editableField.isEditable = true; view.addSubview(editableField)
                if hit(editableField) !== editableField { futureControlRouting = false }
            }
        }
        return ["exactly_50_card_styles": presets.count == 50, "fifty_unique_card_ids": Set(presets.map(\.id)).count == 50,
                "fifty_distinct_card_renders": hashes.count == 50, "all_card_text_and_controls_fit_without_overlap": noOverlap,
                "all_cards_background_text_and_icon_hit_card": cardHitRouting,
                "all_card_buttons_keep_native_hit_routes": buttonHitRouting,
                "future_interactive_controls_keep_hit_routes": futureControlRouting,
                "preview_cards_do_not_navigate": previewDoesNotNavigate,
                "legacy_card_ids_preserved": ["glass-dark", "frosted-glass", "paper-light", "compact", "pill", "minimal", "terminal", "neon", "outline", "ticket", "bubble", "stacked"].allSatisfy { preset(id: $0).id == $0 }]
    }

}

struct CueCardStyle {
    let id: String
    let position: String
    let opacity: Double
    let radius: CGFloat?
    let textScale: CGFloat
    let width: CGFloat?
    let showMessage: Bool
    let showOpenButton: Bool

    init(fields: [String: String]) {
        id = CueCardCatalog.preset(id: fields["card_style"] ?? "glass-dark").id
        position = CueCardCatalog.positions.contains { $0.0 == fields["card_position"] } ? fields["card_position"]! : "top-center"
        func number(_ key: String, _ fallback: Double, _ lower: Double, _ upper: Double) -> Double {
            guard let value = Double(fields[key] ?? ""), value.isFinite else { return fallback }
            return min(upper, max(lower, value))
        }
        opacity = number("card_opacity", 0.97, 0.35, 1)
        radius = fields["card_radius"] == nil ? nil : CGFloat(number("card_radius", 20, 0, 48))
        textScale = CGFloat(number("text_scale", 1, 0.8, 1.25))
        width = fields["card_width"] == nil ? nil : CGFloat(number("card_width", 560, 380, 800))
        showMessage = fields["card_message"] != "false"
        showOpenButton = fields["card_open"] != "false"
    }
    var size: NSSize {
        let base = CueCardCatalog.preset(id: id).size
        return NSSize(width: width ?? base.width, height: base.height + max(0, textScale - 1) * 90)
    }
    func frame(in visible: NSRect, mouse: NSPoint) -> NSRect {
        let size = NSSize(width: min(self.size.width, visible.width - 32), height: self.size.height)
        var x = visible.midX - size.width / 2
        var y = visible.maxY - size.height - 28
        if position.hasSuffix("right") { x = visible.maxX - size.width - 24 }
        if position.hasSuffix("left") { x = visible.minX + 24 }
        if position.hasPrefix("bottom") { y = visible.minY + 24 }
        if position == "center" { y = visible.midY - size.height / 2 }
        if position == "mouse" { x = mouse.x + 18; y = mouse.y - size.height - 18 }
        x = min(visible.maxX - size.width - 12, max(visible.minX + 12, x))
        y = min(visible.maxY - size.height - 12, max(visible.minY + 12, y))
        return NSRect(origin: NSPoint(x: x, y: y), size: size)
    }
}

private enum CueCardLayout { case leading, plain, terminal, compact, pill, centered, sidebar, split, header, micro, rail, editor }

final class CueStyledBannerView: NSView {
    let event: CueEvent
    let accent: NSColor
    let palette: [NSColor]
    let style: CueCardStyle
    let statusLabel = NSTextField(labelWithString: "")
    let conversationLabel = NSTextField(labelWithString: "")
    let messageLabel = NSTextField(wrappingLabelWithString: "")
    let icon = NSImageView()
    let openButton = NSButton(title: "打开对话 ↗", target: nil, action: nil)
    let closeButton = NSButton(title: "×", target: nil, action: nil)
    private weak var controller: CueController?
    private var pressedForConversation = false

    init(event: CueEvent, target: AnyObject?, color: NSColor, palette: [NSColor], style: CueCardStyle, availableWidth: CGFloat? = nil) {
        self.event = event; accent = color; self.palette = palette.isEmpty ? [color] : palette; self.style = style
        controller = target as? CueController
        var size = style.size
        if let availableWidth { size.width = min(size.width, availableWidth) }
        super.init(frame: NSRect(origin: .zero, size: size))
        wantsLayer = true; layer?.opacity = Float(style.opacity)
        if ["neon", "frosted-glass", "hologram", "aurora", "spotlight", "frosted-light"].contains(style.id) {
            layer?.shadowColor = color.cgColor; layer?.shadowOpacity = style.id == "neon" ? 0.5 : 0.18
            layer?.shadowRadius = style.id == "neon" ? 12 : 8; layer?.shadowOffset = .zero
        }
        build(target: target)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override var isOpaque: Bool { false }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    private var canOpenConversation: Bool {
        controller != nil && event.provider == "codex" && !event.isPreview && !event.threadID.isEmpty &&
        event.threadID.unicodeScalars.allSatisfy { CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_")).contains($0) }
    }

    // Route passive content to the card, while preserving present and future
    // native controls (including selectable text and custom gestures).
    override func hitTest(_ point: NSPoint) -> NSView? {
        guard let hit = super.hitTest(point) else { return nil }
        var candidate: NSView? = hit
        while let view = candidate, view !== self {
            if !view.gestureRecognizers.isEmpty { return hit }
            if let text = view as? NSTextField {
                if text.isEditable || text.isSelectable || text.action != nil { return hit }
            } else if let image = view as? NSImageView {
                if image.isEditable || image.action != nil { return hit }
            } else if view is NSControl || view is NSTextView || view is NSScrollView { return hit }
            candidate = view.superview
        }
        return self
    }

    override func mouseDown(with event: NSEvent) { pressedForConversation = canOpenConversation }
    override func mouseUp(with event: NSEvent) {
        defer { pressedForConversation = false }
        let location = convert(event.locationInWindow, from: nil)
        let hitPoint = superview.map { convert(location, to: $0) } ?? location
        guard pressedForConversation, bounds.contains(location), hitTest(hitPoint) === self else { return }
        openBoundConversation(nil)
    }
    @objc private func openBoundConversation(_ sender: Any?) {
        guard canOpenConversation, let controller else { return }
        controller.open(event: event)
        if controller.current?.id == event.id { controller.dismissCurrent() }
    }
    override func accessibilityPerformPress() -> Bool {
        guard canOpenConversation else { return false }
        openBoundConversation(nil); return true
    }
    override func resetCursorRects() {
        super.resetCursorRects()
        guard canOpenConversation else { return }
        addCursorRect(bounds, cursor: .pointingHand)
        for child in subviews where child is NSControl && child !== icon && child !== statusLabel && child !== conversationLabel && child !== messageLabel {
            addCursorRect(child.frame, cursor: child === openButton ? .pointingHand : .arrow)
        }
    }

    private var layout: CueCardLayout {
        switch style.id {
        case "compact", "floating-strip": return .compact
        case "pill": return .pill
        case "micro": return .micro
        case "minimal", "blueprint", "pixel", "mono-black", "mono-white", "notebook", "sticky", "receipt", "dotted": return .plain
        case "terminal": return .terminal
        case "code-editor": return .editor
        case "badge-top", "centered", "sunrise", "spotlight": return .centered
        case "sidebar-accent", "metro": return .sidebar
        case "split-panel", "dashboard": return .split
        case "ribbon", "bookmark", "gradient-ribbon", "newspaper", "polaroid", "corner-tab": return .header
        case "timeline": return .rail
        default: return .leading
        }
    }
    private var light: Bool { ["paper-light", "mono-white", "warm-paper", "notebook", "sticky", "newspaper", "postage", "receipt", "polaroid", "sunrise", "frosted-light"].contains(style.id) }
    private var inkAccent: NSColor { light ? accent.blended(withFraction: 0.42, of: .black) ?? accent : accent }
    private var cornerRadius: CGFloat {
        if let radius = style.radius { return radius }
        switch style.id {
        case "pill": return bounds.height / 2
        case "terminal", "code-editor", "blueprint", "newspaper", "receipt": return 6
        case "pixel", "metro": return 0
        case "bubble", "chat-left", "frosted-light": return 28
        case "micro", "floating-strip": return 12
        default: return 20
        }
    }
    private var baseColor: NSColor {
        switch style.id {
        case "mono-black": return NSColor(white: 0.015, alpha: 1)
        case "mono-white": return .white
        case "warm-paper", "notebook", "newspaper", "postage", "receipt", "polaroid": return NSColor(srgbRed: 0.98, green: 0.96, blue: 0.90, alpha: 1)
        case "sticky": return NSColor(srgbRed: 1, green: 0.91, blue: 0.48, alpha: 1)
        case "sunrise": return NSColor(srgbRed: 1, green: 0.96, blue: 0.92, alpha: 1)
        case "frosted-light": return NSColor(srgbRed: 0.90, green: 0.98, blue: 0.98, alpha: 1)
        case "paper-light": return NSColor(srgbRed: 0.97, green: 0.98, blue: 0.99, alpha: 1)
        case "blueprint": return NSColor(srgbRed: 0.035, green: 0.13, blue: 0.24, alpha: 1)
        case "pixel": return NSColor(srgbRed: 0.09, green: 0.06, blue: 0.16, alpha: 1)
        case "cyber-grid", "metro": return NSColor(srgbRed: 0.025, green: 0.065, blue: 0.11, alpha: 1)
        case "hologram": return NSColor(srgbRed: 0.09, green: 0.07, blue: 0.15, alpha: 1)
        case "aurora": return NSColor(srgbRed: 0.025, green: 0.11, blue: 0.10, alpha: 1)
        default: return NSColor(srgbRed: 0.055, green: 0.07, blue: 0.09, alpha: 1)
        }
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.clear.setFill(); bounds.fill(using: .copy)
        let inset: CGFloat = style.id == "stacked" ? 9 : ["postage", "pixel"].contains(style.id) ? 7 : 2
        var rect = bounds.insetBy(dx: inset, dy: inset)
        if ["bubble", "chat-left"].contains(style.id) { rect = rect.insetBy(dx: 0, dy: 6) }
        if style.id == "stacked" {
            for offset: CGFloat in [7, 4] { fillRound(rect.offsetBy(dx: offset, dy: -offset), color: accent.withAlphaComponent(offset == 7 ? 0.16 : 0.3)) }
        }
        let shape = NSBezierPath(roundedRect: rect, xRadius: cornerRadius, yRadius: cornerRadius)
        baseColor.setFill(); shape.fill()
        NSGraphicsContext.saveGraphicsState(); shape.addClip()
        drawDecoration(in: rect)
        NSGraphicsContext.restoreGraphicsState()
        let borderAlpha: CGFloat = ["mono-black", "mono-white", "minimal", "sticky"].contains(style.id) ? 0.13 : 0.5
        inkAccent.withAlphaComponent(borderAlpha).setStroke(); shape.lineWidth = ["outline", "neon", "blueprint"].contains(style.id) ? 1.8 : 0.8; shape.stroke()
        drawOutsideDecoration(in: rect)
    }

    private func fillRound(_ rect: NSRect, color: NSColor, radius: CGFloat? = nil) {
        color.setFill(); NSBezierPath(roundedRect: rect, xRadius: radius ?? cornerRadius, yRadius: radius ?? cornerRadius).fill()
    }
    private func line(_ from: NSPoint, _ to: NSPoint, color: NSColor, width: CGFloat = 1, dash: [CGFloat] = []) {
        let p = NSBezierPath(); p.move(to: from); p.line(to: to); p.lineWidth = width
        if !dash.isEmpty { p.setLineDash(dash, count: dash.count, phase: 0) }
        color.setStroke(); p.stroke()
    }
    private func circle(_ rect: NSRect, color: NSColor, stroke: Bool = false, width: CGFloat = 1) {
        let p = NSBezierPath(ovalIn: rect); p.lineWidth = width
        if stroke { color.setStroke(); p.stroke() } else { color.setFill(); p.fill() }
    }
    private func polygon(_ points: [NSPoint], color: NSColor) {
        guard let first = points.first else { return }
        let path = NSBezierPath(); path.move(to: first); points.dropFirst().forEach { path.line(to: $0) }; path.close(); color.setFill(); path.fill()
    }
    private func gradient(_ rect: NSRect, colors: [NSColor], angle: CGFloat = -25) { NSGradient(colors: colors)?.draw(in: rect, angle: angle) }
    private func decorativeText(_ text: String, at point: NSPoint, color: NSColor, size: CGFloat = 9, mono: Bool = false) {
        text.draw(at: point, withAttributes: [.font: mono ? NSFont.monospacedSystemFont(ofSize: size, weight: .medium) : NSFont.systemFont(ofSize: size, weight: .semibold), .foregroundColor: color])
    }
    private func drawDecoration(in r: NSRect) {
        let w = bounds.width, h = bounds.height, a = inkAccent
        let soft = a.withAlphaComponent(light ? 0.11 : 0.14), faint = a.withAlphaComponent(0.07)
        switch style.id {
        case "glass-dark":
            line(NSPoint(x: 24, y: h - 3), NSPoint(x: w - 24, y: h - 3), color: .white.withAlphaComponent(0.1))
        case "frosted-glass", "neon", "bubble":
            gradient(r, colors: [palette[0].withAlphaComponent(style.id == "neon" ? 0.24 : 0.14), palette.last!.withAlphaComponent(0.04)])
            if style.id == "neon" { fillRound(NSRect(x: 14, y: 14, width: 3, height: h - 28), color: accent.withAlphaComponent(0.8), radius: 1.5) }
        case "paper-light":
            fillRound(NSRect(x: 20, y: h - 66, width: 37, height: 37), color: soft, radius: 11)
        case "compact":
            fillRound(NSRect(x: 13, y: h - 54, width: 37, height: 37), color: soft, radius: 11)
        case "pill":
            circle(NSRect(x: 13, y: h / 2 - 24, width: 48, height: 48), color: soft)
        case "minimal":
            a.setFill(); NSRect(x: 24, y: h - 9, width: 42, height: 2).fill()
        case "terminal", "code-editor":
            NSColor.white.withAlphaComponent(0.04).setFill(); NSRect(x: 0, y: h - 33, width: w, height: 33).fill()
            line(NSPoint(x: 0, y: h - 33), NSPoint(x: w, y: h - 33), color: .white.withAlphaComponent(0.09))
            for (i, c) in [NSColor.systemRed, .systemYellow, .systemGreen].enumerated() { circle(NSRect(x: 17 + CGFloat(i) * 13, y: h - 21, width: 6, height: 6), color: c.withAlphaComponent(0.85)) }
            if style.id == "code-editor" {
                decorativeText("vibecue.task", at: NSPoint(x: 77, y: h - 23), color: .white.withAlphaComponent(0.5), mono: true)
                NSColor.white.withAlphaComponent(0.025).setFill(); NSRect(x: 0, y: 0, width: 35, height: h - 34).fill()
                for (i, y) in [h - 61, h - 95, h - 128].enumerated() { decorativeText(String(i + 1), at: NSPoint(x: 14, y: y), color: .white.withAlphaComponent(0.28), mono: true) }
            }
        case "outline":
            line(NSPoint(x: 21, y: 43), NSPoint(x: w - 21, y: 43), color: faint)
        case "ticket":
            line(NSPoint(x: 60, y: 16), NSPoint(x: 60, y: h - 16), color: a.withAlphaComponent(0.4), dash: [3, 4])
        case "stacked":
            fillRound(NSRect(x: 20, y: h - 64, width: 36, height: 36), color: soft, radius: 11)
        case "sidebar-accent":
            gradient(NSRect(x: 0, y: 0, width: 78, height: h), colors: [accent.withAlphaComponent(0.35), palette.last!.withAlphaComponent(0.08)], angle: 90)
            line(NSPoint(x: 78, y: 0), NSPoint(x: 78, y: h), color: accent.withAlphaComponent(0.3))
        case "split-panel":
            fillRound(NSRect(x: w - 110, y: 14, width: 96, height: h - 28), color: soft, radius: 14)
            for n in 0..<4 { fillRound(NSRect(x: w - 91 + CGFloat(n) * 17, y: 30, width: 8, height: CGFloat(14 + n * 7)), color: palette[n % palette.count].withAlphaComponent(0.65), radius: 3) }
        case "badge-top":
            circle(NSRect(x: w / 2 - 28, y: h - 62, width: 56, height: 56), color: soft)
            circle(NSRect(x: w / 2 - 34, y: h - 68, width: 68, height: 68), color: faint, stroke: true)
        case "centered":
            line(NSPoint(x: w / 2 - 45, y: h - 13), NSPoint(x: w / 2 + 45, y: h - 13), color: a.withAlphaComponent(0.35), width: 2)
        case "ribbon":
            polygon([NSPoint(x: 0, y: h - 34), NSPoint(x: w - 48, y: h - 34), NSPoint(x: w - 65, y: h - 17), NSPoint(x: w - 48, y: h), NSPoint(x: 0, y: h)], color: soft)
            decorativeText("VIBECUE  /  任务提醒", at: NSPoint(x: 23, y: h - 24), color: a, size: 9.5)
        case "toast-wide":
            accent.setFill(); NSRect(x: 0, y: 0, width: 6, height: h).fill()
            gradient(NSRect(x: w - 150, y: 0, width: 150, height: h), colors: [accent.withAlphaComponent(0.005), accent.withAlphaComponent(0.1)], angle: 0)
        case "micro":
            fillRound(NSRect(x: 14, y: h / 2 - 18, width: 36, height: 36), color: soft, radius: 10)
        case "timeline":
            line(NSPoint(x: 39, y: 13), NSPoint(x: 39, y: h - 13), color: a.withAlphaComponent(0.25), width: 2)
            circle(NSRect(x: 33, y: 19, width: 12, height: 12), color: baseColor)
            circle(NSRect(x: 36, y: 22, width: 6, height: 6), color: a.withAlphaComponent(0.5))
            circle(NSRect(x: 21, y: h - 66, width: 36, height: 36), color: baseColor)
            circle(NSRect(x: 21, y: h - 66, width: 36, height: 36), color: a.withAlphaComponent(0.4), stroke: true)
        case "chat-left":
            fillRound(NSRect(x: 17, y: h - 66, width: 39, height: 39), color: soft, radius: 13)
            if !messageLabel.isHidden { fillRound(messageLabel.frame.insetBy(dx: -7, dy: -5), color: .white.withAlphaComponent(0.035), radius: 10) }
        case "bookmark":
            polygon([NSPoint(x: 24, y: h), NSPoint(x: 52, y: h), NSPoint(x: 52, y: h - 38), NSPoint(x: 38, y: h - 29), NSPoint(x: 24, y: h - 38)], color: accent.withAlphaComponent(0.8))
            decorativeText("待办 / ATTENTION", at: NSPoint(x: 66, y: h - 24), color: a.withAlphaComponent(0.65))
        case "dashboard":
            fillRound(NSRect(x: w - 107, y: 16, width: 91, height: h - 32), color: .white.withAlphaComponent(0.04), radius: 12)
            circle(NSRect(x: w - 92, y: h / 2 - 26, width: 60, height: 60), color: a.withAlphaComponent(0.16), stroke: true, width: 7)
            let arc = NSBezierPath(); arc.appendArc(withCenter: NSPoint(x: w - 62, y: h / 2 + 4), radius: 30, startAngle: 30, endAngle: 280, clockwise: false); arc.lineWidth = 7; accent.setStroke(); arc.stroke()
            decorativeText("READY", at: NSPoint(x: w - 81, y: 30), color: a.withAlphaComponent(0.7), mono: true)
        case "blueprint":
            for x in stride(from: CGFloat(0), to: w, by: 20) { line(NSPoint(x: x, y: 0), NSPoint(x: x, y: h), color: .white.withAlphaComponent(0.025)) }
            for y in stride(from: CGFloat(0), to: h, by: 20) { line(NSPoint(x: 0, y: y), NSPoint(x: w, y: y), color: .white.withAlphaComponent(0.025)) }
            for x: CGFloat in [10, w - 10] { line(NSPoint(x: x - 4, y: 10), NSPoint(x: x + 4, y: 10), color: a.withAlphaComponent(0.6)); line(NSPoint(x: x, y: 6), NSPoint(x: x, y: 14), color: a.withAlphaComponent(0.6)) }
        case "pixel":
            for x in stride(from: CGFloat(10), to: w - 10, by: 12) { a.withAlphaComponent(0.23).setFill(); NSRect(x: x, y: 8, width: 6, height: 3).fill(); NSRect(x: x, y: h - 11, width: 6, height: 3).fill() }
            decorativeText("+", at: NSPoint(x: w - 29, y: 9), color: a, size: 13, mono: true)
        case "cyber-grid":
            for n in 0..<10 { line(NSPoint(x: w - 150 + CGFloat(n) * 24, y: 0), NSPoint(x: w - 210 + CGFloat(n) * 24, y: h), color: accent.withAlphaComponent(0.06)) }
            for y in stride(from: CGFloat(14), to: h, by: 22) { line(NSPoint(x: w - 130, y: y), NSPoint(x: w, y: y), color: accent.withAlphaComponent(0.06)) }
            line(NSPoint(x: 15, y: 12), NSPoint(x: 62, y: 12), color: accent, width: 2)
        case "hologram":
            polygon([NSPoint(x: w * 0.6, y: 0), NSPoint(x: w, y: h), NSPoint(x: w, y: 0)], color: palette.last!.withAlphaComponent(0.14))
            polygon([NSPoint(x: 0, y: h), NSPoint(x: w * 0.7, y: h), NSPoint(x: w * 0.3, y: 0)], color: palette[0].withAlphaComponent(0.07))
            for y in stride(from: CGFloat(3), to: h, by: 4) { line(NSPoint(x: 0, y: y), NSPoint(x: w, y: y), color: .white.withAlphaComponent(0.018)) }
        case "aurora":
            for n in 0..<4 {
                let p = NSBezierPath(); p.move(to: NSPoint(x: -30, y: h * 0.2 + CGFloat(n) * 22)); p.curve(to: NSPoint(x: w + 30, y: h * 0.8 + CGFloat(n) * 10), controlPoint1: NSPoint(x: w * 0.3, y: h * 1.3), controlPoint2: NSPoint(x: w * 0.7, y: -h * 0.4)); p.lineWidth = 18
                palette[n % palette.count].withAlphaComponent(0.055).setStroke(); p.stroke()
            }
        case "gradient-ribbon":
            gradient(NSRect(x: 0, y: h - 35, width: w, height: 35), colors: palette.map { $0.withAlphaComponent(0.4) }, angle: 0)
            decorativeText("AI CODING COMPANION", at: NSPoint(x: 23, y: h - 24), color: .white.withAlphaComponent(0.85), size: 9)
        case "sunrise":
            gradient(r, colors: [NSColor.systemOrange.withAlphaComponent(0.13), NSColor.systemPink.withAlphaComponent(0.03)], angle: 90)
            circle(NSRect(x: w / 2 - 80, y: h - 122, width: 160, height: 160), color: .systemOrange.withAlphaComponent(0.055))
        case "mono-black":
            line(NSPoint(x: 24, y: 44), NSPoint(x: w - 24, y: 44), color: .white.withAlphaComponent(0.15))
        case "mono-white":
            NSColor.black.withAlphaComponent(0.75).setFill(); NSRect(x: 24, y: h - 8, width: w - 48, height: 2).fill()
        case "warm-paper":
            fillRound(NSRect(x: 16, y: h - 66, width: 40, height: 40), color: .systemOrange.withAlphaComponent(0.09), radius: 20)
            line(NSPoint(x: 73, y: 43), NSPoint(x: w - 20, y: 43), color: .systemBrown.withAlphaComponent(0.16))
        case "notebook":
            for y in stride(from: CGFloat(14), to: h - 9, by: 22) { line(NSPoint(x: 0, y: y), NSPoint(x: w, y: y), color: .systemBlue.withAlphaComponent(0.09)) }
            line(NSPoint(x: 15, y: 0), NSPoint(x: 15, y: h), color: .systemRed.withAlphaComponent(0.25))
        case "sticky":
            NSColor.black.withAlphaComponent(0.035).setFill(); NSRect(x: 0, y: h - 24, width: w, height: 24).fill()
            polygon([NSPoint(x: w - 28, y: 0), NSPoint(x: w - 28, y: 28), NSPoint(x: w, y: 28)], color: .systemOrange.withAlphaComponent(0.2))
        case "newspaper":
            decorativeText("VIBE DAILY", at: NSPoint(x: 24, y: h - 28), color: .black.withAlphaComponent(0.8), size: 15, mono: true)
            line(NSPoint(x: 23, y: h - 34), NSPoint(x: w - 23, y: h - 34), color: .black.withAlphaComponent(0.65), width: 1.4)
            line(NSPoint(x: 23, y: h - 38), NSPoint(x: w - 23, y: h - 38), color: .black.withAlphaComponent(0.3))
        case "postage":
            fillRound(NSRect(x: 18, y: h - 67, width: 41, height: 41), color: soft, radius: 3)
            for n in 0..<4 { circle(NSRect(x: w - 87 + CGFloat(n) * 4, y: h - 70, width: 54, height: 54), color: a.withAlphaComponent(0.025), stroke: true) }
        case "receipt":
            line(NSPoint(x: 23, y: 45), NSPoint(x: w - 23, y: 45), color: .black.withAlphaComponent(0.2), dash: [3, 3])
            for n in 0..<27 { NSColor.black.withAlphaComponent(0.14).setFill(); NSRect(x: w - 113 + CGFloat(n) * 3, y: 18, width: n % 3 == 0 ? 2 : 1, height: 15).fill() }
        case "polaroid":
            gradient(NSRect(x: 11, y: h - 38, width: w - 22, height: 27), colors: palette.map { $0.withAlphaComponent(0.4) }, angle: 0)
            decorativeText("捕捉这一刻的进度", at: NSPoint(x: 22, y: h - 31), color: .black.withAlphaComponent(0.6), size: 9)
        case "spotlight":
            for n in (0..<7).reversed() { circle(NSRect(x: w / 2 - CGFloat(20 + n * 13), y: h - CGFloat(59 + n * 13), width: CGFloat(40 + n * 26), height: CGFloat(40 + n * 26)), color: accent.withAlphaComponent(0.012)) }
        case "orbit":
            for n in 0..<3 { let oval = NSBezierPath(ovalIn: NSRect(x: -20 - CGFloat(n * 11), y: h - 88 - CGFloat(n * 10), width: 94 + CGFloat(n * 24), height: 74 + CGFloat(n * 17))); a.withAlphaComponent(0.11).setStroke(); oval.stroke() }
            circle(NSRect(x: 61, y: h - 39, width: 4, height: 4), color: accent.withAlphaComponent(0.8))
        case "metro":
            accent.withAlphaComponent(0.65).setFill(); NSRect(x: 0, y: 0, width: 78, height: h).fill()
            palette.last!.withAlphaComponent(0.3).setFill(); NSRect(x: 0, y: 0, width: 78, height: 18).fill()
        case "floating-strip":
            gradient(NSRect(x: 0, y: 0, width: w, height: 4), colors: palette, angle: 0)
            fillRound(NSRect(x: 13, y: h - 55, width: 38, height: 38), color: soft, radius: 19)
        case "corner-tab":
            fillRound(NSRect(x: 18, y: h - 33, width: 117, height: 23), color: accent.withAlphaComponent(0.2), radius: 6)
            decorativeText("当前对话 / CUE", at: NSPoint(x: 27, y: h - 25), color: a, size: 9)
            polygon([NSPoint(x: w - 38, y: h), NSPoint(x: w, y: h - 38), NSPoint(x: w, y: h)], color: soft)
        case "progress":
            fillRound(NSRect(x: 22, y: 7, width: w - 44, height: 4), color: faint, radius: 2)
            gradient(NSRect(x: 22, y: 7, width: (w - 44) * 0.7, height: 4), colors: palette, angle: 0)
        case "wave":
            for n in 0..<36 { let height = CGFloat(3 + Int(abs(sin(Double(n) * 0.7)) * 13)); fillRound(NSRect(x: w - 151 + CGFloat(n) * 4, y: 12, width: 2, height: height), color: palette[n % palette.count].withAlphaComponent(0.22), radius: 1) }
        case "dotted":
            for x in stride(from: CGFloat(14), to: w, by: 16) { for y in stride(from: CGFloat(14), to: h, by: 16) { circle(NSRect(x: x, y: y, width: 1.6, height: 1.6), color: a.withAlphaComponent(0.10)) } }
        case "contour":
            for n in 0..<8 { let oval = NSBezierPath(ovalIn: NSRect(x: w - 146 - CGFloat(n * 10), y: -52 - CGFloat(n * 7), width: 180 + CGFloat(n * 21), height: 156 + CGFloat(n * 19))); a.withAlphaComponent(0.07).setStroke(); oval.stroke() }
        case "frosted-light":
            gradient(r, colors: [palette[0].withAlphaComponent(0.12), palette.last!.withAlphaComponent(0.07)], angle: 12)
            fillRound(NSRect(x: 13, y: 13, width: w - 26, height: h - 26), color: .white.withAlphaComponent(0.22), radius: 20)
        default: break
        }
    }

    private func drawOutsideDecoration(in r: NSRect) {
        if style.id == "ticket" {
            for x in [r.minX - 6, r.maxX - 6] {
                NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current?.compositingOperation = .copy
                circle(NSRect(x: x, y: r.midY - 6, width: 12, height: 12), color: .clear); NSGraphicsContext.restoreGraphicsState()
            }
        }
        if ["bubble", "chat-left"].contains(style.id) {
            polygon([NSPoint(x: 40, y: 14), NSPoint(x: style.id == "chat-left" ? 17 : 29, y: 2), NSPoint(x: 59, y: 14)], color: baseColor)
        }
        if style.id == "postage" {
            for x in stride(from: CGFloat(14), to: bounds.width - 10, by: 15) {
                for y in [CGFloat(3), bounds.height - 6] { circle(NSRect(x: x, y: y, width: 4, height: 4), color: inkAccent.withAlphaComponent(0.4)) }
            }
            for y in stride(from: CGFloat(17), to: bounds.height - 12, by: 15) {
                for x in [CGFloat(3), bounds.width - 6] { circle(NSRect(x: x, y: y, width: 4, height: 4), color: inkAccent.withAlphaComponent(0.4)) }
            }
        }
        if style.id == "receipt" {
            for x in stride(from: CGFloat(7), to: bounds.width - 7, by: 10) {
                polygon([NSPoint(x: x, y: 2), NSPoint(x: x + 5, y: 7), NSPoint(x: x + 10, y: 2)], color: NSColor(srgbRed: 0.08, green: 0.09, blue: 0.11, alpha: 1))
            }
        }
        if style.id == "pixel" {
            for (x,y) in [(CGFloat(2),CGFloat(2)),(bounds.width - 10,CGFloat(2)),(CGFloat(2),bounds.height - 10),(bounds.width - 10,bounds.height - 10)] { accent.withAlphaComponent(0.7).setFill(); NSRect(x: x, y: y, width: 8, height: 8).fill() }
        }
    }

    private func build(target: AnyObject?) {
        let w = bounds.width, h = bounds.height, scale = style.textScale
        // AppKit wrapping labels are selectable by default. These three fields
        // are passive card content; future selectable/editor fields retain
        // their own hit routes through hitTest above.
        for field in [statusLabel, conversationLabel, messageLabel] { field.isEditable = false; field.isSelectable = false }
        let compact = layout == .compact, pill = layout == .pill, micro = layout == .micro, centered = layout == .centered
        let terminal = layout == .terminal || layout == .editor
        let small = compact || pill || micro
        var x: CGFloat = 72
        if layout == .plain || layout == .terminal || layout == .header || centered || layout == .split { x = 24 }
        if layout == .editor { x = 46 }
        if layout == .sidebar { x = 96 }
        if layout == .rail { x = 76 }
        if compact || micro { x = 61 }
        if style.id == "ticket" { x = 80 }
        let right: CGFloat = layout == .split ? 125 : pill || micro ? 109 : 42
        let titleHeight: CGFloat = (small ? 24 : 27) * scale
        let statusHeight: CGFloat = (small ? 15 : 18) * scale
        var top: CGFloat = terminal || layout == .header ? 43 : centered ? 64 : 13
        if style.id == "minimal" || style.id == "mono-white" { top = 16 }
        let statusY = h - top - statusHeight
        let titleY = statusY - (small ? 4 : 7) - titleHeight
        let titleWidth = max(70, w - x - right)
        let mono = terminal || ["blueprint", "pixel", "receipt"].contains(style.id)
        let textColor: NSColor = light ? NSColor(srgbRed: 0.1, green: 0.13, blue: 0.17, alpha: 1) : .white
        let subdued = light ? NSColor.black.withAlphaComponent(0.61) : NSColor.white.withAlphaComponent(0.66)
        icon.image = NSImage(systemSymbolName: event.symbol, accessibilityDescription: event.label)
        icon.contentTintColor = style.id == "metro" ? baseColor : inkAccent
        let iconSize: CGFloat = small ? 26 : 32
        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: iconSize - 3, weight: .medium)
        var iconFrame = NSRect(x: small ? 19 : 24, y: h - (small ? 51 : 61), width: iconSize, height: iconSize)
        if pill || micro { iconFrame.origin.y = h / 2 - iconSize / 2 }
        if layout == .sidebar { iconFrame = NSRect(x: 24, y: h / 2 - 16, width: 32, height: 32) }
        if layout == .split { iconFrame = NSRect(x: w - 78, y: h / 2 - 8, width: 32, height: 32) }
        if centered { iconFrame = NSRect(x: w / 2 - 15, y: h - 48, width: 30, height: 30) }
        icon.frame = iconFrame; icon.isHidden = layout == .plain || terminal || layout == .header
        addSubview(icon)
        statusLabel.stringValue = terminal ? "> \(event.providerLabel.lowercased())  /  \(event.label)" : "\(event.providerLabel.uppercased())  ·  \(event.label)"
        statusLabel.font = mono ? .monospacedSystemFont(ofSize: (small ? 9.5 : 11) * scale, weight: .medium) : .systemFont(ofSize: (small ? 10 : 11.5) * scale, weight: .semibold)
        statusLabel.textColor = inkAccent; statusLabel.lineBreakMode = .byTruncatingTail
        statusLabel.frame = NSRect(x: x, y: statusY, width: max(70, w - x - (layout == .split ? 125 : 42)), height: statusHeight)
        statusLabel.alignment = centered ? .center : .left; addSubview(statusLabel)
        conversationLabel.stringValue = event.title
        conversationLabel.font = mono ? .monospacedSystemFont(ofSize: (small ? 15 : 17) * scale, weight: .semibold) : .systemFont(ofSize: (small ? 16 : 19) * scale, weight: .semibold)
        conversationLabel.textColor = textColor; conversationLabel.lineBreakMode = .byTruncatingTail; conversationLabel.toolTip = event.title
        conversationLabel.frame = NSRect(x: x, y: titleY, width: titleWidth, height: titleHeight)
        conversationLabel.alignment = centered ? .center : .left; addSubview(conversationLabel)
        let openWidth: CGFloat = small ? 78 : 112
        let openHeight: CGFloat = small ? 22 : 25
        var openX = centered ? (w - openWidth) / 2 : x - 1
        if pill || micro || compact { openX = w - openWidth - 22 }
        let openY: CGFloat = pill || micro ? 20 : compact ? 8 : 13
        openButton.title = small ? "打开 ↗" : "打开对话 ↗"
        openButton.bezelStyle = .inline; openButton.contentTintColor = inkAccent
        openButton.appearance = NSAppearance(named: light ? .aqua : .darkAqua)
        openButton.font = .systemFont(ofSize: 11 * scale, weight: .semibold)
        openButton.target = self; openButton.action = #selector(openBoundConversation(_:))
        openButton.frame = NSRect(x: openX, y: openY, width: openWidth, height: openHeight)
        openButton.isEnabled = target == nil || canOpenConversation
        openButton.isHidden = !style.showOpenButton; addSubview(openButton)
        let bodyTop = titleY - (compact ? 4 : 8)
        let bodyBottom = (style.showOpenButton ? openY + openHeight : 15) + 8
        let bodyHeight = min((compact ? 16 : 32) * scale, max(0, bodyTop - bodyBottom))
        messageLabel.stringValue = event.message.isEmpty ? "回到这个对话查看最新进度。" : event.message
        messageLabel.font = mono ? .monospacedSystemFont(ofSize: 11.5 * scale, weight: .regular) : .systemFont(ofSize: (compact ? 11 : 12) * scale)
        messageLabel.textColor = subdued; messageLabel.maximumNumberOfLines = compact ? 1 : 2; messageLabel.lineBreakMode = .byTruncatingTail
        messageLabel.frame = NSRect(x: x, y: bodyTop - bodyHeight, width: max(70, w - x - (layout == .split ? 125 : 32)), height: bodyHeight)
        messageLabel.alignment = centered ? .center : .left
        messageLabel.isHidden = pill || micro || !style.showMessage || bodyHeight < 12
        addSubview(messageLabel)
        closeButton.bezelStyle = .inline; closeButton.contentTintColor = subdued; closeButton.font = .systemFont(ofSize: 22, weight: .light)
        closeButton.appearance = NSAppearance(named: light ? .aqua : .darkAqua)
        closeButton.target = target; closeButton.action = #selector(CueController.dismissCurrent)
        closeButton.frame = NSRect(x: w - 35, y: h - 33, width: 25, height: 25)
        closeButton.setAccessibilityLabel("关闭提醒"); addSubview(closeButton)
        setAccessibilityElement(true); setAccessibilityLabel("\(event.providerLabel) 提醒：\(event.label)，\(event.title)")
        setAccessibilityRole(canOpenConversation ? .button : .group)
        if canOpenConversation { setAccessibilityHelp("点击提示卡的背景、标题、正文或图标可打开此对话；关闭按钮只关闭提醒。") }
    }
}
