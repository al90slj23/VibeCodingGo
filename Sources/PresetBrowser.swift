import AppKit

enum CuePresetCategory: String {
    case effect, card, sound
    var title: String { self == .effect ? "屏幕特效" : self == .card ? "提示框" : "提示音" }
    var settingKey: String { self == .effect ? "effect" : self == .card ? "card_style" : "sound" }
    var entries: [(id: String, name: String)] {
        switch self {
        case .effect: return CueEffectCatalog.presets.map { ($0.id, $0.name) }
        case .card: return CueCardCatalog.presets.map { ($0.id, $0.name) }
        case .sound: return CueSoundCatalog.presets.map { ($0.id, $0.name) }
        }
    }
    var choices: [(id: String, name: String)] { [("random", "随机")] + entries }
}

/// A static catalogue plus one live selection. Fifty tiles never start fifty clocks.
final class CuePresetBrowserController: NSWindowController, NSWindowDelegate {
    let category: CuePresetCategory
    var fields: [String: String]
    let accent: NSColor
    let onSelect: (String) -> (feedback: String, previewID: String?)
    let shouldAnimate: () -> Bool
    var selectedID: String
    var sampleID: String
    var tileButtons: [String: NSButton] = [:]
    let previewHost = NSView(frame: NSRect(x: 0, y: 0, width: 282, height: 190))
    let selectedLabel = NSTextField(wrappingLabelWithString: "")
    let feedbackLabel = NSTextField(wrappingLabelWithString: "")
    var effectView: CueEdgeEffectView?
    var clock: Timer?
    var startedAt = Date()

    init(category: CuePresetCategory, fields: [String: String], color: NSColor, selectedID: String,
         eventName: String, shouldAnimate: @escaping () -> Bool,
         onSelect: @escaping (String) -> (feedback: String, previewID: String?)) {
        self.category = category; self.fields = fields; accent = color
        self.selectedID = selectedID; self.onSelect = onSelect; self.shouldAnimate = shouldAnimate
        sampleID = selectedID == "random" ? category.entries.randomElement()!.id : selectedID
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 960, height: 670), styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "50 种\(category.title) + 随机 · \(eventName) · 点选即预览"
        window.appearance = NSAppearance(named: .darkAqua); window.isReleasedWhenClosed = false
        super.init(window: window); window.delegate = self
        let root = CueFlippedView(frame: NSRect(x: 0, y: 0, width: 960, height: 670))
        root.wantsLayer = true; root.layer?.backgroundColor = NSColor(srgbRed: 0.08, green: 0.09, blue: 0.11, alpha: 1).cgColor
        let scroll = NSScrollView(frame: NSRect(x: 12, y: 14, width: 628, height: 642))
        scroll.hasVerticalScroller = true; scroll.scrollerStyle = .legacy; scroll.drawsBackground = false
        let rows = Int(ceil(Double(category.choices.count) / 3))
        let doc = CueFlippedView(frame: NSRect(x: 0, y: 0, width: 610, height: CGFloat(rows) * 126 + 8))
        for (index, entry) in category.choices.enumerated() {
            let button = NSButton(title: entry.id == "random" ? "随机 · 每次重新抽取" : String(format: "%02d  %@", index, entry.name), target: self, action: #selector(choose(_:)))
            button.frame = NSRect(x: 3 + CGFloat(index % 3) * 201, y: 4 + CGFloat(index / 3) * 126, width: 194, height: 118)
            button.bezelStyle = .regularSquare; button.isBordered = false; button.wantsLayer = true
            button.layer?.cornerRadius = 10; button.font = .systemFont(ofSize: 11, weight: .medium)
            button.image = thumbnail(id: entry.id, size: NSSize(width: 182, height: 79))
            button.imagePosition = .imageAbove; button.imageScaling = .scaleProportionallyDown
            button.identifier = NSUserInterfaceItemIdentifier(entry.id)
            button.setAccessibilityLabel(entry.name); button.toolTip = "\(entry.name) · 点击选择并预览"
            doc.addSubview(button); tileButtons[entry.id] = button
        }
        scroll.documentView = doc; root.addSubview(scroll)
        selectedLabel.frame = NSRect(x: 660, y: 24, width: 282, height: 48)
        selectedLabel.font = .systemFont(ofSize: 19, weight: .semibold); selectedLabel.textColor = .white
        root.addSubview(selectedLabel)
        previewHost.setFrameOrigin(NSPoint(x: 660, y: 88)); previewHost.wantsLayer = true
        previewHost.layer?.backgroundColor = NSColor.black.withAlphaComponent(0.3).cgColor
        previewHost.layer?.cornerRadius = 12; root.addSubview(previewHost)
        feedbackLabel.frame = NSRect(x: 662, y: 298, width: 278, height: 125)
        feedbackLabel.font = .systemFont(ofSize: 12); feedbackLabel.textColor = .secondaryLabelColor
        feedbackLabel.stringValue = category == .sound ? "点击任一音效即可选择并试听。\n不会连续播放整套声音。" : "点击任一样式即可选择并自动预览。\n可以连续切换，图库会保持打开。"
        root.addSubview(feedbackLabel)
        let replay = NSButton(title: category == .sound ? "再次试听" : "再次预览", target: self, action: #selector(replay))
        replay.bezelStyle = .rounded; replay.frame = NSRect(x: 660, y: 438, width: 282, height: 34); root.addSubview(replay)
        let hint = NSTextField(wrappingLabelWithString: "按提醒类型独立保存。预览遵循全局暂停、静音和音量设置；真实提醒优先显示。")
        hint.frame = NSRect(x: 662, y: 497, width: 278, height: 95); hint.font = .systemFont(ofSize: 11)
        hint.textColor = .secondaryLabelColor; root.addSubview(hint)
        window.contentView = root; updateSelection(); window.center()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    deinit { clock?.invalidate() }

    func thumbnail(id: String, size: NSSize) -> NSImage {
        if id == "random" { return NSImage(systemSymbolName: "shuffle", accessibilityDescription: "随机") ?? NSImage(size: size) }
        switch category {
        case .effect:
            var styleFields = fields; styleFields["effect"] = id; styleFields["width"] = "5"; styleFields["inset"] = "6"
            let rep = CueEdgeEffectView.bitmap(style: CueVisualStyle(fields: styleFields, fallback: accent), size: size, time: 1.35, background: true)
            let image = NSImage(size: size); image.addRepresentation(rep); return image
        case .card: return CueCardCatalog.previewImage(for: CueCardCatalog.preset(id: id), size: size, fields: fields, color: accent)
        case .sound:
            guard let preset = CueSoundCatalog.preset(id: id) else { return NSImage(size: size) }
            return CueSoundCatalog.waveformImage(for: preset, size: size)
        }
    }
    @objc func choose(_ sender: NSButton) {
        guard let id = sender.identifier?.rawValue else { return }
        selectedID = id; fields[category.settingKey] = id
        selectAndPreview()
    }
    @objc func replay() { selectAndPreview() }
    func selectAndPreview() {
        let result = onSelect(selectedID)
        sampleID = result.previewID ?? (selectedID == "random" ? category.entries.randomElement()!.id : selectedID)
        updateSelection(); feedbackLabel.stringValue = result.feedback
    }

    func updateSelection() {
        clock?.invalidate(); clock = nil; effectView = nil
        previewHost.subviews.forEach { $0.removeFromSuperview() }
        let name = category.entries.first { $0.id == sampleID }?.name ?? "自选音效"
        selectedLabel.stringValue = selectedID == "random" ? "随机 · \(name)" : name
        for (id, button) in tileButtons {
            button.layer?.backgroundColor = (id == selectedID ? accent.withAlphaComponent(0.2) : NSColor.white.withAlphaComponent(0.045)).cgColor
            button.layer?.borderWidth = id == selectedID ? 1.5 : 0
            button.layer?.borderColor = accent.withAlphaComponent(0.8).cgColor
            button.setAccessibilityValue(id == selectedID ? "已选中" : "")
        }
        if category == .effect {
            var previewFields = fields; previewFields["effect"] = sampleID; previewFields["width"] = "5"; previewFields["inset"] = "6"
            let effect = CueEdgeEffectView(frame: previewHost.bounds.insetBy(dx: 8, dy: 8), style: CueVisualStyle(fields: previewFields, fallback: accent))
            previewHost.addSubview(effect); effectView = effect; startedAt = Date(); effect.advance(to: 0)
            if shouldAnimate(), !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
                clock = Timer.scheduledTimer(withTimeInterval: 1.0 / 24, repeats: true) { [weak self] _ in
                    guard let self, self.window?.isVisible == true, self.shouldAnimate() else { return }
                    self.effectView?.advance(to: Date().timeIntervalSince(self.startedAt))
                }
                RunLoop.main.add(clock!, forMode: .common)
            }
        } else {
            let image = NSImageView(frame: previewHost.bounds.insetBy(dx: 10, dy: 10))
            image.image = thumbnail(id: sampleID, size: NSSize(width: 262, height: 166))
            image.imageScaling = .scaleProportionallyUpOrDown; previewHost.addSubview(image)
        }
    }
    func windowWillClose(_ notification: Notification) { clock?.invalidate(); clock = nil }
}
