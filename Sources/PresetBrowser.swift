import AppKit

struct CueLibraryTag {
    let id: String
    let name: String
    let count: Int
}

enum CuePresetCategory: String {
    case effect, card, sound
    var title: String { self == .effect ? "屏幕光效" : self == .card ? "提示卡" : "声音" }
    var settingKey: String { self == .effect ? "effect" : self == .card ? "card_style" : "sound" }
    var entries: [(id: String, name: String)] {
        switch self {
        case .effect: return CueEffectCatalog.presets.map { ($0.id, $0.name) }
        case .card: return CueCardCatalog.presets.map { ($0.id, $0.name) }
        case .sound: return CueSoundCatalog.presets.map { ($0.id, $0.name) }
        }
    }
    var tagNames: [(id: String, name: String)] {
        switch self {
        case .effect: return CueEffectCatalog.tags.map { ($0.id, $0.name) }
        case .card: return CueCardCatalog.tags.map { ($0.id, $0.name) }
        case .sound: return CueSoundCatalog.tags.map { ($0.id, $0.name) }
        }
    }
    func tagIDs(for id: String) -> [String] {
        switch self {
        case .effect: return CueEffectCatalog.presets.first { $0.id == id }?.tagIDs ?? []
        case .card: return CueCardCatalog.presets.first { $0.id == id }?.tags ?? []
        case .sound: return CueSoundCatalog.preset(id: id)?.tags ?? []
        }
    }
    func filtered(tags: Set<String>, search: String = "") -> [(id: String, name: String)] {
        entries.filter { item in
            (tags.isEmpty || tags.contains("all") || !tags.isDisjoint(with: tagIDs(for: item.id))) &&
            (search.isEmpty || item.name.localizedCaseInsensitiveContains(search) || tagIDs(for: item.id).contains { $0.localizedCaseInsensitiveContains(search) })
        }
    }
    var defaultTag: String { self == .effect ? "recommended" : self == .card ? "native" : "synthesized" }
    var tags: [CueLibraryTag] {
        [CueLibraryTag(id: "all", name: "全部", count: entries.count)] + tagNames.filter { $0.id != "all" }.map { CueLibraryTag(id: $0.id, name: $0.name, count: filtered(tags: [$0.id]).count) }.filter { $0.count > 0 }
    }
    var choices: [(id: String, name: String)] { [("random", "随机")] + entries }
}

final class CuePresetBrowserController: NSWindowController, NSWindowDelegate, NSSearchFieldDelegate {
    let category: CuePresetCategory
    var fields: [String: String]
    let accent: NSColor
    let onSelect: (String, [String]) -> (feedback: String, previewID: String?)
    let shouldAnimate: () -> Bool
    var selectedID: String
    var sampleID: String
    var selectedTags: Set<String>
    var tileButtons: [String: NSButton] = [:]
    var tagButtons: [String: NSButton] = [:]
    let scroll = NSScrollView()
    let searchField = NSSearchField()
    let previewHost = CueFlippedView(frame: NSRect(x: 0, y: 0, width: 278, height: 184))
    let selectedLabel = NSTextField(wrappingLabelWithString: "")
    let feedbackLabel = NSTextField(wrappingLabelWithString: "")
    let countLabel = NSTextField(labelWithString: "")
    var effectView: CueEdgeEffectView?
    var clock: Timer?
    var startedAt = Date()

    init(category: CuePresetCategory, fields: [String: String], color: NSColor, selectedID: String,
         eventName: String, shouldAnimate: @escaping () -> Bool,
         onSelect: @escaping (String, [String]) -> (feedback: String, previewID: String?)) {
        self.category = category; self.fields = fields; accent = color
        self.selectedID = selectedID; self.onSelect = onSelect; self.shouldAnimate = shouldAnimate
        let scope: Set<String>
        if let saved = fields[category.settingKey + "_random_tags"] {
            let valid = Set(category.tagNames.map(\.id) + ["all"])
            let parsed = Set(saved.split(separator: ",").map { value in
                category == .effect && value == "subtle" ? "recommended" : category == .effect && value == "vivid" ? "classic" : String(value)
            }).intersection(valid)
            scope = saved.isEmpty || parsed.contains("all") ? ["all"] : parsed.isEmpty ? [category.defaultTag] : parsed
        } else { scope = [category.defaultTag] }
        selectedTags = scope
        sampleID = selectedID == "random" ? category.filtered(tags: scope).first?.id ?? category.entries[0].id : selectedID
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 960, height: 650), styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "\(category.title) · \(eventName)"; window.isReleasedWhenClosed = false
        super.init(window: window); window.delegate = self
        let root = CueFlippedView(frame: NSRect(x: 0, y: 0, width: 960, height: 650))
        root.backgroundColor = .windowBackgroundColor
        let sidebar = NSVisualEffectView(frame: NSRect(x: 0, y: 0, width: 158, height: 650))
        sidebar.material = .sidebar; sidebar.blendingMode = .withinWindow; root.addSubview(sidebar)
        let categoryLabel = NSTextField(labelWithString: "分类")
        categoryLabel.frame = NSRect(x: 18, y: 20, width: 124, height: 20); categoryLabel.font = .systemFont(ofSize: 11, weight: .semibold); categoryLabel.textColor = .secondaryLabelColor; root.addSubview(categoryLabel)
        for (index, tag) in category.tags.enumerated() {
            let button = NSButton(checkboxWithTitle: "\(tag.name)  \(tag.count)", target: self, action: #selector(filterTag(_:)))
            button.frame = NSRect(x: 16, y: 52 + CGFloat(index) * 35, width: 136, height: 28)
            button.font = .systemFont(ofSize: 12); button.identifier = NSUserInterfaceItemIdentifier(tag.id)
            button.state = selectedTags.contains(tag.id) ? .on : .off; root.addSubview(button); tagButtons[tag.id] = button
        }
        let multiHint = NSTextField(wrappingLabelWithString: "可同时选择多个分类。\n随机只从所选分类抽取。")
        multiHint.frame = NSRect(x: 18, y: 544, width: 124, height: 62); multiHint.font = .systemFont(ofSize: 10.5); multiHint.textColor = .secondaryLabelColor; root.addSubview(multiHint)
        searchField.frame = NSRect(x: 177, y: 18, width: 448, height: 28)
        searchField.placeholderString = "搜索\(category.title)"; searchField.delegate = self; searchField.setAccessibilityLabel("搜索样式")
        root.addSubview(searchField)
        scroll.frame = NSRect(x: 171, y: 60, width: 466, height: 546)
        scroll.hasVerticalScroller = true; scroll.scrollerStyle = .overlay; scroll.drawsBackground = false; root.addSubview(scroll)
        countLabel.frame = NSRect(x: 181, y: 620, width: 450, height: 18); countLabel.font = .systemFont(ofSize: 11); countLabel.textColor = .secondaryLabelColor; root.addSubview(countLabel)
        selectedLabel.frame = NSRect(x: 659, y: 24, width: 278, height: 42); selectedLabel.font = .systemFont(ofSize: 16, weight: .semibold); selectedLabel.textColor = .labelColor; root.addSubview(selectedLabel)
        previewHost.setFrameOrigin(NSPoint(x: 659, y: 86)); previewHost.backgroundColor = .controlBackgroundColor
        previewHost.wantsLayer = true; previewHost.layer?.cornerRadius = 12; root.addSubview(previewHost)
        feedbackLabel.frame = NSRect(x: 661, y: 293, width: 274, height: 126); feedbackLabel.font = .systemFont(ofSize: 12); feedbackLabel.textColor = .secondaryLabelColor
        feedbackLabel.stringValue = "选择后立即\(category == .sound ? "试听" : "预览")，并保存到当前提醒类型。"; root.addSubview(feedbackLabel)
        let replay = NSButton(title: category == .sound ? "试听" : "预览", target: self, action: #selector(replay))
        replay.bezelStyle = .rounded; replay.frame = NSRect(x: 659, y: 435, width: 278, height: 32); root.addSubview(replay)
        let hint = NSTextField(wrappingLabelWithString: "真实任务提醒优先。预览遵循暂停、静音与音量设置。")
        hint.frame = NSRect(x: 661, y: 493, width: 274, height: 66); hint.font = .systemFont(ofSize: 11); hint.textColor = .secondaryLabelColor; root.addSubview(hint)
        root.appearanceDidChange = { [weak self] in self?.rebuildTiles(); self?.updateSelection() }
        window.contentView = root; rebuildTiles(); updateSelection(); window.center()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    deinit { clock?.invalidate() }

    @objc func filterTag(_ sender: NSButton) {
        guard let id = sender.identifier?.rawValue else { return }
        if id == "all" { selectedTags = ["all"] }
        else {
            selectedTags.remove("all")
            if sender.state == .on { selectedTags.insert(id) } else { selectedTags.remove(id) }
            if selectedTags.isEmpty { selectedTags = [category.defaultTag] }
        }
        tagButtons.forEach { $0.value.state = selectedTags.contains($0.key) ? .on : .off }
        rebuildTiles()
    }
    func controlTextDidChange(_ obj: Notification) { rebuildTiles() }
    func rebuildTiles() {
        let items = category.filtered(tags: selectedTags, search: searchField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines))
        let choices = [(id: "random", name: "随机当前分类")] + items
        let rows = Int(ceil(Double(choices.count) / 2))
        let document = CueFlippedView(frame: NSRect(x: 0, y: 0, width: 446, height: max(536, CGFloat(rows) * 120 + 8)))
        tileButtons = [:]
        for (index, entry) in choices.enumerated() {
            let button = NSButton(title: entry.name, target: self, action: #selector(choose(_:)))
            button.frame = NSRect(x: 6 + CGFloat(index % 2) * 218, y: 5 + CGFloat(index / 2) * 120, width: 210, height: 110)
            button.bezelStyle = .regularSquare; button.isBordered = false; button.wantsLayer = true; button.layer?.cornerRadius = 10
            button.font = .systemFont(ofSize: 11, weight: .medium); button.image = thumbnail(id: entry.id, size: NSSize(width: 192, height: 72))
            button.imagePosition = .imageAbove; button.imageScaling = .scaleProportionallyDown; button.identifier = NSUserInterfaceItemIdentifier(entry.id)
            button.setAccessibilityLabel(entry.name); document.addSubview(button); tileButtons[entry.id] = button
        }
        if items.isEmpty {
            let label = NSTextField(labelWithString: "没有匹配的样式，试试其他分类或关键词。")
            label.frame = NSRect(x: 16, y: 145, width: 412, height: 24); label.textColor = .secondaryLabelColor; label.font = .systemFont(ofSize: 12); document.addSubview(label)
        }
        scroll.documentView = document
        countLabel.stringValue = "\(items.count) 项 · \(selectedTags.contains("all") ? "全部分类" : "所选分类")"
        refreshTileSelection()
    }
    func thumbnail(id: String, size: NSSize) -> NSImage {
        if id == "random" { return NSImage(systemSymbolName: "shuffle", accessibilityDescription: "随机当前分类") ?? NSImage(size: size) }
        switch category {
        case .effect:
            var styleFields = fields; styleFields["effect"] = id; styleFields["width"] = "3"; styleFields["inset"] = "5"
            let rep = CueEdgeEffectView.bitmap(style: CueVisualStyle(fields: styleFields, fallback: accent), size: size, time: 1.35, background: false)
            let image = NSImage(size: size); image.addRepresentation(rep); return image
        case .card: return CueCardCatalog.previewImage(for: CueCardCatalog.preset(id: id), size: size, fields: fields, color: accent)
        case .sound:
            guard let preset = CueSoundCatalog.preset(id: id) else { return NSImage(size: size) }
            return CueSoundCatalog.waveformImage(for: preset, size: size)
        }
    }
    @objc func choose(_ sender: NSButton) {
        guard let id = sender.identifier?.rawValue else { return }
        selectedID = id; fields[category.settingKey] = id; selectAndPreview()
    }
    @objc func replay() { selectAndPreview() }
    func selectAndPreview() {
        let result = onSelect(selectedID, Array(selectedTags).sorted())
        sampleID = result.previewID ?? (selectedID == "random" ? category.filtered(tags: selectedTags).randomElement()?.id ?? category.entries[0].id : selectedID)
        updateSelection(); updateFeedback(result.feedback)
    }
    func updateFeedback(_ text: String) { feedbackLabel.stringValue = text + soundDescription() }
    func soundDescription() -> String {
        guard category == .sound, let preset = CueSoundCatalog.preset(id: sampleID) else { return "" }
        return "\n\n" + preset.sourceDescription + String(format: " · %.1f 秒\n", preset.duration) + preset.description
    }
    func refreshTileSelection() {
        for (id, button) in tileButtons {
            button.layer?.backgroundColor = (id == selectedID ? NSColor.controlAccentColor.withAlphaComponent(0.10) : NSColor.controlBackgroundColor).cgColor
            button.layer?.borderWidth = id == selectedID ? 1 : 0.5
            button.layer?.borderColor = (id == selectedID ? NSColor.controlAccentColor.withAlphaComponent(0.6) : NSColor.separatorColor.withAlphaComponent(0.4)).cgColor
            button.setAccessibilityValue(id == selectedID ? "已选中" : "")
        }
    }
    func updateSelection() {
        clock?.invalidate(); clock = nil; effectView = nil; previewHost.subviews.forEach { $0.removeFromSuperview() }
        let name = category.entries.first { $0.id == sampleID }?.name ?? "自选音效"
        selectedLabel.stringValue = selectedID == "random" ? "随机 · \(name)" : name
        refreshTileSelection()
        if category == .effect {
            var previewFields = fields; previewFields["effect"] = sampleID; previewFields["width"] = "3"; previewFields["inset"] = "5"
            let effect = CueEdgeEffectView(frame: previewHost.bounds.insetBy(dx: 8, dy: 8), style: CueVisualStyle(fields: previewFields, fallback: accent))
            previewHost.addSubview(effect); effectView = effect; startedAt = Date(); effect.advance(to: 0)
            if shouldAnimate(), !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
                clock = Timer.scheduledTimer(withTimeInterval: 1.0 / 24, repeats: true) { [weak self] _ in
                    guard let self, self.window?.isVisible == true, self.shouldAnimate() else { return }
                    if NSWorkspace.shared.accessibilityDisplayShouldReduceMotion { self.clock?.invalidate(); self.clock = nil; self.effectView?.advance(to: 0); return }
                    self.effectView?.advance(to: Date().timeIntervalSince(self.startedAt))
                }
                RunLoop.main.add(clock!, forMode: .common)
            }
        } else {
            let image = NSImageView(frame: previewHost.bounds.insetBy(dx: 8, dy: 8)); image.image = thumbnail(id: sampleID, size: NSSize(width: 262, height: 166)); image.imageScaling = .scaleProportionallyUpOrDown; previewHost.addSubview(image)
        }
    }
    func windowWillClose(_ notification: Notification) { clock?.invalidate(); clock = nil }
}
