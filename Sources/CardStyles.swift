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
        .init(id: "stacked", name: "层叠卡片", size: NSSize(width: 560, height: 182))
    ]
    static func preset(id: String) -> CueCardPreset { presets.first { $0.id == id } ?? presets[0] }
    static let positions: [(String, String)] = [("top-center", "顶部居中"), ("top-right", "右上角"), ("top-left", "左上角"), ("bottom-center", "底部居中"), ("bottom-right", "右下角"), ("bottom-left", "左下角"), ("center", "屏幕中央"), ("mouse", "鼠标附近")]

    static func renderContactSheet(to url: URL) throws {
        let cell = NSSize(width: 640, height: 250)
        let view = NSView(frame: NSRect(x: 0, y: 0, width: cell.width * 3, height: cell.height * 4))
        view.wantsLayer = true; view.layer?.backgroundColor = NSColor(srgbRed: 0.08, green: 0.09, blue: 0.11, alpha: 1).cgColor
        for (index, preset) in presets.enumerated() {
            let event = CueEvent(kind: "input", title: "模型接入 · 等待你的选择", message: "请选择下一步：继续验证，或检查当前配置。")
            let card = CueStyledBannerView(event: event, target: nil, color: .systemTeal, palette: [.systemTeal, .systemBlue, .systemPurple], style: CueCardStyle(fields: ["card_style": preset.id]))
            let x = CGFloat(index % 3) * cell.width + (cell.width - card.frame.width) / 2
            let y = CGFloat(3 - index / 3) * cell.height + 34
            card.setFrameOrigin(NSPoint(x: x, y: y)); view.addSubview(card)
            let label = NSTextField(labelWithString: String(format: "%02d  %@", index + 1, preset.name))
            label.textColor = .white; label.font = .systemFont(ofSize: 15, weight: .semibold)
            label.frame = NSRect(x: CGFloat(index % 3) * cell.width + 28, y: y + 196, width: 560, height: 22); view.addSubview(label)
        }
        guard let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
        view.cacheDisplay(in: view.bounds, to: bitmap)
        if let data = bitmap.representation(using: .png, properties: [:]) { try data.write(to: url) }
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

    init(event: CueEvent, target: AnyObject?, color: NSColor, palette: [NSColor], style: CueCardStyle, availableWidth: CGFloat? = nil) {
        self.event = event; accent = color; self.palette = palette.isEmpty ? [color] : palette; self.style = style
        var size = style.size
        if let availableWidth { size.width = min(size.width, availableWidth) }
        super.init(frame: NSRect(origin: .zero, size: size))
        wantsLayer = true
        layer?.opacity = Float(style.opacity)
        if style.id == "neon" || style.id == "frosted-glass" {
            layer?.shadowColor = color.cgColor; layer?.shadowOpacity = style.id == "neon" ? 0.5 : 0.15
            layer?.shadowRadius = style.id == "neon" ? 12 : 8; layer?.shadowOffset = .zero
        }
        build(target: target)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override var isOpaque: Bool { false }
    private var light: Bool { style.id == "paper-light" }
    private var cornerRadius: CGFloat { style.radius ?? (style.id == "pill" ? bounds.height / 2 : style.id == "terminal" ? 8 : style.id == "bubble" ? 28 : 20) }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.clear.setFill(); bounds.fill(using: .copy)
        let rect = bounds.insetBy(dx: style.id == "stacked" ? 8 : 2, dy: style.id == "stacked" ? 8 : 2)
        if style.id == "stacked" {
            for offset: CGFloat in [7, 4] {
                let under = NSBezierPath(roundedRect: rect.offsetBy(dx: offset, dy: -offset), xRadius: cornerRadius, yRadius: cornerRadius)
                accent.withAlphaComponent(offset == 7 ? 0.13 : 0.24).setFill(); under.fill()
            }
        }
        let cardRect = style.id == "bubble" ? rect.insetBy(dx: 0, dy: 7) : rect
        let shape = NSBezierPath(roundedRect: cardRect, xRadius: cornerRadius, yRadius: cornerRadius)
        let base = light ? NSColor(srgbRed: 0.97, green: 0.98, blue: 0.99, alpha: 1) : NSColor(srgbRed: 0.055, green: 0.07, blue: 0.09, alpha: 1)
        base.setFill(); shape.fill()
        if ["frosted-glass", "neon", "bubble"].contains(style.id) {
            NSGraphicsContext.saveGraphicsState(); shape.addClip()
            let gradient = NSGradient(colors: [palette[0].withAlphaComponent(style.id == "neon" ? 0.18 : 0.12), palette.last!.withAlphaComponent(0.03)])!
            gradient.draw(in: cardRect, angle: -25)
            NSGraphicsContext.restoreGraphicsState()
        }
        let stroke = style.id == "outline" || style.id == "neon" ? 1.8 : 0.8
        (light ? accent.withAlphaComponent(0.55) : accent.withAlphaComponent(style.id == "minimal" ? 0.18 : 0.55)).setStroke()
        shape.lineWidth = stroke; shape.stroke()
        if style.id == "terminal" {
            NSColor.white.withAlphaComponent(0.08).setFill(); NSRect(x: rect.minX, y: rect.maxY - 33, width: rect.width, height: 1).fill()
            for (index, color) in [NSColor.systemRed, .systemYellow, .systemGreen].enumerated() {
                color.withAlphaComponent(0.85).setFill(); NSBezierPath(ovalIn: NSRect(x: CGFloat(17 + index * 13), y: rect.maxY - 21, width: 6, height: 6)).fill()
            }
        }
        if style.id == "minimal" {
            accent.setFill(); NSRect(x: 23, y: cardRect.maxY - 9, width: 44, height: 2).fill()
        }
        if style.id == "ticket" {
            let dash = NSBezierPath(); dash.move(to: NSPoint(x: 57, y: 18)); dash.line(to: NSPoint(x: 57, y: bounds.height - 18))
            dash.setLineDash([3, 4], count: 2, phase: 0); dash.lineWidth = 1
            accent.withAlphaComponent(0.4).setStroke(); dash.stroke()
            for y in [cardRect.midY - 6] {
                NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current?.compositingOperation = .copy
                NSColor.clear.setFill(); NSBezierPath(ovalIn: NSRect(x: cardRect.minX - 6, y: y, width: 12, height: 12)).fill()
                NSBezierPath(ovalIn: NSRect(x: cardRect.maxX - 6, y: y, width: 12, height: 12)).fill()
                NSGraphicsContext.restoreGraphicsState()
            }
        }
        if style.id == "bubble" {
            let tail = NSBezierPath(); tail.move(to: NSPoint(x: 43, y: 13)); tail.line(to: NSPoint(x: 29, y: 3)); tail.line(to: NSPoint(x: 58, y: 13)); tail.close()
            base.setFill(); tail.fill()
        }
    }

    private func build(target: AnyObject?) {
        let width = bounds.width, height = bounds.height
        let compact = style.id == "compact", pill = style.id == "pill", terminal = style.id == "terminal", minimal = style.id == "minimal"
        let textColor: NSColor = light ? NSColor(srgbRed: 0.1, green: 0.13, blue: 0.17, alpha: 1) : .white
        let subdued = light ? NSColor.black.withAlphaComponent(0.6) : NSColor.white.withAlphaComponent(0.66)
        let x: CGFloat = minimal || terminal ? 24 : style.id == "ticket" ? 78 : compact ? 61 : 72
        let iconSize: CGFloat = pill ? 27 : compact ? 26 : 34
        icon.image = NSImage(systemSymbolName: event.symbol, accessibilityDescription: event.label)
        icon.contentTintColor = accent
        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: iconSize - 3, weight: .medium)
        icon.frame = NSRect(x: compact ? 22 : 23, y: height - (pill ? 59 : compact ? 59 : 67), width: iconSize, height: iconSize)
        icon.isHidden = terminal || minimal
        addSubview(icon)
        statusLabel.stringValue = terminal ? "> \(event.providerLabel.lowercased())  /  \(event.label)" : "\(event.providerLabel.uppercased())  ·  \(event.label)"
        statusLabel.font = terminal ? .monospacedSystemFont(ofSize: 11 * style.textScale, weight: .medium) : .systemFont(ofSize: (compact || pill ? 10 : 11.5) * style.textScale, weight: .semibold)
        statusLabel.textColor = accent
        statusLabel.frame = NSRect(x: x, y: height - (terminal ? 58 : pill ? 33 : 31), width: width - x - 42, height: 18 * style.textScale)
        addSubview(statusLabel)
        conversationLabel.stringValue = event.title
        conversationLabel.font = terminal ? .monospacedSystemFont(ofSize: 17 * style.textScale, weight: .semibold) : .systemFont(ofSize: (compact || pill ? 16 : 19) * style.textScale, weight: .semibold)
        conversationLabel.textColor = textColor; conversationLabel.lineBreakMode = .byTruncatingTail; conversationLabel.toolTip = event.title
        let titleY = statusLabel.frame.minY - 27 * style.textScale - (terminal ? 4 : pill ? 5 : 7)
        conversationLabel.frame = NSRect(x: x, y: titleY, width: width - x - (pill ? 106 : 45), height: 27 * style.textScale)
        addSubview(conversationLabel)
        messageLabel.stringValue = event.message.isEmpty ? "回到这个对话查看最新进度。" : event.message
        messageLabel.font = terminal ? .monospacedSystemFont(ofSize: 11.5 * style.textScale, weight: .regular) : .systemFont(ofSize: 12 * style.textScale)
        messageLabel.textColor = subdued; messageLabel.maximumNumberOfLines = compact ? 1 : 2; messageLabel.lineBreakMode = .byWordWrapping
        let messageY: CGFloat = terminal ? 47 : compact ? 32 : minimal ? 38 : style.id == "outline" ? 43 : 49
        let messageHeight: CGFloat = compact ? 18 : minimal ? 26 : style.id == "outline" ? 32 : 38
        messageLabel.frame = NSRect(x: x, y: messageY, width: width - x - 42, height: messageHeight * style.textScale)
        messageLabel.isHidden = pill || !style.showMessage; addSubview(messageLabel)
        openButton.title = compact || pill ? "打开 ↗" : "打开对话 ↗"
        openButton.bezelStyle = .inline; openButton.contentTintColor = accent
        openButton.font = .systemFont(ofSize: 11.5 * style.textScale, weight: .semibold)
        openButton.target = target; openButton.action = #selector(CueController.openCurrent)
        openButton.frame = NSRect(x: pill ? width - 103 : compact ? width - 99 : x - 1, y: pill ? 22 : compact ? 8 : 13, width: pill || compact ? 78 : 112, height: 25)
        openButton.isEnabled = event.provider == "codex" && !event.threadID.isEmpty
        openButton.isHidden = !style.showOpenButton
        addSubview(openButton)
        closeButton.bezelStyle = .inline; closeButton.contentTintColor = subdued; closeButton.font = .systemFont(ofSize: 22, weight: .light)
        closeButton.target = target; closeButton.action = #selector(CueController.dismissCurrent)
        closeButton.frame = NSRect(x: width - 36, y: height - (pill ? 31 : 35), width: 26, height: 28)
        closeButton.setAccessibilityLabel("关闭提醒"); addSubview(closeButton)
        setAccessibilityElement(true); setAccessibilityLabel("\(event.providerLabel) 提醒：\(event.label)，\(event.title)")
    }
}
