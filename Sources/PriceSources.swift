import AppKit
import Foundation

/// Anonymous public price catalogs. Order is the fallback policy, independent
/// of usage history and any account authorization.
final class CuePriceSourcesController: NSWindowController, NSTableViewDataSource, NSTableViewDelegate {
    private let surface = CuePriceSourcesSurface()
    private var isEmbedded = false
    private var presentationWindow: NSWindow? { surface.window ?? (isEmbedded ? nil : window) }
    private let client: CueUsageClient
    private let onChange: () -> Void
    private let table = NSTableView(), scroll = NSScrollView()
    private let detail = NSTextField(wrappingLabelWithString: "选择来源后可以编辑或调整优先级。")
    private let feedback = NSTextField(wrappingLabelWithString: "")
    private let interval = NSTextField(string: "6")
    private let addButton = NSButton(title: "添加来源…", target: nil, action: nil)
    private let editButton = NSButton(title: "编辑…", target: nil, action: nil)
    private let removeButton = NSButton(title: "删除", target: nil, action: nil)
    private let upButton = NSButton(title: "上移", target: nil, action: nil)
    private let downButton = NSButton(title: "下移", target: nil, action: nil)
    private let defaultsButton = NSButton(title: "恢复默认队列", target: nil, action: nil)
    private let saveButton = NSButton(title: "保存队列", target: nil, action: nil)
    private let refreshButton = NSButton(title: "立即更新价格", target: nil, action: nil)
    private let formatButton = NSButton(title: "格式说明…", target: nil, action: nil)
    private var sources: [[String: Any]] = []
    private var defaults: [[String: Any]] = []
    private var statuses: [String: [String: Any]] = [:]
    private var busy = false, loaded = false, dirty = false
    private var editor: CuePriceSourceEditor?
    private var heading = NSTextField(labelWithString: "模型价格来源")
    private var subtitle = NSTextField(wrappingLabelWithString: "从上到下按模型查找；当前来源没有该模型时，使用下一个已启用的来源。")
    private let intervalCaption = NSTextField(labelWithString: "公开价格自动更新间隔")
    private let intervalUnit = NSTextField(labelWithString: "小时")

    init(home: URL, onChange: @escaping () -> Void) {
        client = CueUsageClient(home: home); self.onChange = onChange
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 860, height: 650), styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.title = "模型价格来源"; window.minSize = NSSize(width: 800, height: 620)
        window.isReleasedWhenClosed = false; window.center()
        super.init(window: window)
        window.contentView = surface
        heading.font = .systemFont(ofSize: 21, weight: .semibold)
        subtitle.font = .systemFont(ofSize: 12); subtitle.textColor = .secondaryLabelColor
        detail.font = .systemFont(ofSize: 11); detail.textColor = .secondaryLabelColor
        feedback.font = .systemFont(ofSize: 11); feedback.textColor = .secondaryLabelColor
        interval.font = .monospacedDigitSystemFont(ofSize: 12, weight: .regular)
        interval.setAccessibilityLabel("公开模型价格自动更新间隔，小时")
        intervalCaption.font = .systemFont(ofSize: 12); intervalUnit.font = .systemFont(ofSize: 12)
        table.dataSource = self; table.delegate = self; table.rowHeight = 34
        table.style = .plain; table.usesAlternatingRowBackgroundColors = true
        table.allowsMultipleSelection = false; table.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        table.target = self; table.doubleAction = #selector(editSelected)
        table.setAccessibilityLabel("模型价格来源优先级队列")
        for (id, title, width) in [("priority", "优先级", 53.0), ("enabled", "启用", 45.0), ("name", "来源", 178.0), ("kind", "格式", 105.0), ("count", "模型数", 70.0), ("status", "更新状态", 310.0)] {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(id)); column.title = title
            column.width = width; column.minWidth = id == "status" ? 160 : width; table.addTableColumn(column)
        }
        scroll.documentView = table; scroll.hasVerticalScroller = true; scroll.hasHorizontalScroller = true
        scroll.borderType = .lineBorder; scroll.autohidesScrollers = true
        let buttons = [addButton, editButton, removeButton, upButton, downButton, defaultsButton, saveButton, refreshButton, formatButton]
        buttons.forEach { $0.bezelStyle = .rounded; $0.target = self }
        addButton.action = #selector(addSource); editButton.action = #selector(editSelected); removeButton.action = #selector(removeSelected)
        upButton.action = #selector(moveSourceUp); downButton.action = #selector(moveSourceDown); defaultsButton.action = #selector(restoreDefaults)
        saveButton.action = #selector(saveQueue); refreshButton.action = #selector(refreshPrices); formatButton.action = #selector(showFormatHelp)
        saveButton.identifier = NSUserInterfaceItemIdentifier("pricing.save-queue")
        refreshButton.identifier = NSUserInterfaceItemIdentifier("pricing.refresh")
        [heading, subtitle, scroll, detail, feedback, intervalCaption, interval, intervalUnit].forEach { surface.addSubview($0) }
        buttons.forEach { surface.addSubview($0) }
        surface.onLayout = { [weak self] in self?.layout() }; layout(); updateControls()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    func embeddedContentView() -> NSView {
        if !isEmbedded { isEmbedded = true; window?.orderOut(nil); window?.contentView = nil }
        return surface
    }
    func activateEmbedded() { if !loaded && !busy { load() } }
    func deactivateEmbedded() { /* Preserve unsaved order, sources, and interval. */ }
    override func showWindow(_ sender: Any?) {
        if isEmbedded { activateEmbedded(); presentationWindow?.makeKeyAndOrderFront(nil); return }
        super.showWindow(sender); window?.makeKeyAndOrderFront(nil); load()
    }
    private func layout() {
        let width = surface.bounds.width, height = surface.bounds.height
        heading.frame = NSRect(x: 24, y: 22, width: width - 230, height: 29)
        formatButton.frame = NSRect(x: width - 143, y: 23, width: 119, height: 29)
        subtitle.frame = NSRect(x: 25, y: 61, width: width - 50, height: 36)
        scroll.frame = NSRect(x: 24, y: 110, width: width - 48, height: max(185, height - 370))
        let actionsY = scroll.frame.maxY + 12
        addButton.frame = NSRect(x: 24, y: actionsY, width: 116, height: 29)
        editButton.frame = NSRect(x: 148, y: actionsY, width: 82, height: 29)
        removeButton.frame = NSRect(x: 238, y: actionsY, width: 73, height: 29)
        upButton.frame = NSRect(x: 327, y: actionsY, width: 70, height: 29)
        downButton.frame = NSRect(x: 405, y: actionsY, width: 70, height: 29)
        defaultsButton.frame = NSRect(x: width - 158, y: actionsY, width: 134, height: 29)
        detail.frame = NSRect(x: 27, y: actionsY + 43, width: width - 54, height: 49)
        intervalCaption.frame = NSRect(x: 27, y: height - 150, width: 182, height: 22)
        interval.frame = NSRect(x: 213, y: height - 153, width: 82, height: 26)
        intervalUnit.frame = NSRect(x: 305, y: height - 150, width: 50, height: 22)
        feedback.frame = NSRect(x: 27, y: height - 111, width: width - 54, height: 40)
        saveButton.frame = NSRect(x: width - 282, y: height - 49, width: 116, height: 30)
        refreshButton.frame = NSRect(x: width - 158, y: height - 49, width: 134, height: 30)
    }
    private func load() {
        setBusy(true); feedback.stringValue = "正在读取价格来源…"
        client.run(["settings"], module: "vibecue.usage_money") { [weak self] result in
            guard let self else { return }; self.setBusy(false)
            switch result {
            case .success(let data): self.apply(data); self.feedback.stringValue = "更改后点“保存队列”。“立即更新价格”会先保存当前队列。"
            case .failure(let error): self.feedback.stringValue = error.localizedDescription; self.feedback.textColor = .systemRed
            }
        }
    }
    private func apply(_ data: [String: Any]) {
        let prefs = data["prefs"] as? [String: Any] ?? [:]
        sources = prefs["pricing_sources"] as? [[String: Any]] ?? []
        defaults = data["default_pricing_sources"] as? [[String: Any]] ?? []
        statuses = Dictionary((data["source_status"] as? [[String: Any]] ?? []).compactMap { row -> (String, [String: Any])? in
            guard let id = row["id"] as? String else { return nil }; return (id, row)
        }, uniquingKeysWith: { _, last in last })
        let seconds = (prefs["pricing_refresh_seconds"] as? NSNumber)?.doubleValue ?? 21600
        interval.stringValue = Self.hourText(seconds)
        loaded = true; dirty = false; feedback.stringValue = "更改后点“保存队列”。“立即更新价格”会先保存当前队列。"; feedback.textColor = .secondaryLabelColor; table.reloadData(); updateControls(); updateDetail()
    }
    private static func hourText(_ seconds: Double) -> String { String(format: "%.4f", seconds / 3600).replacingOccurrences(of: #"\.?0+$"#, with: "", options: .regularExpression) }
    private func setBusy(_ value: Bool) { busy = value; updateControls() }
    private func updateControls() {
        let selected = sources.indices.contains(table.selectedRow)
        [addButton, defaultsButton, saveButton, refreshButton].forEach { $0.isEnabled = loaded && !busy }
        addButton.isEnabled = loaded && !busy && sources.count < 20
        defaultsButton.isEnabled = loaded && !busy && !defaults.isEmpty
        [editButton, removeButton].forEach { $0.isEnabled = selected && !busy }
        upButton.isEnabled = selected && table.selectedRow > 0 && !busy
        downButton.isEnabled = selected && table.selectedRow < sources.count - 1 && !busy
        interval.isEnabled = loaded && !busy; table.isEnabled = loaded && !busy
        saveButton.title = dirty ? "保存队列 *" : "保存队列"
    }
    private func changed(select index: Int? = nil) {
        dirty = true; table.reloadData()
        if let index, sources.indices.contains(index) { table.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false) }
        feedback.stringValue = "队列尚未保存。数字越小，优先级越高。"; feedback.textColor = .secondaryLabelColor
        updateControls(); updateDetail()
    }
    func numberOfRows(in tableView: NSTableView) -> Int { sources.count }
    func tableViewSelectionDidChange(_ notification: Notification) { updateControls(); updateDetail() }
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard sources.indices.contains(row), let key = tableColumn?.identifier.rawValue else { return nil }
        let source = sources[row], id = source["id"] as? String ?? "", status = statuses[id] ?? [:]
        if key == "enabled" {
            let button = NSButton(checkboxWithTitle: "", target: self, action: #selector(toggleSource(_:)))
            button.state = source["enabled"] as? Bool == false ? .off : .on; button.tag = row; button.isEnabled = !busy
            button.setAccessibilityLabel("启用价格来源 " + (source["name"] as? String ?? id))
            let cell = NSView(frame: NSRect(x: 0, y: 0, width: tableColumn!.width, height: 34))
            button.frame = NSRect(x: 10, y: 7, width: 23, height: 20); cell.addSubview(button); return cell
        }
        let text: String
        switch key {
        case "priority": text = String(row + 1)
        case "name": text = source["name"] as? String ?? id
        case "kind": text = CuePriceSourceEditor.title(for: source["kind"] as? String ?? "json")
        case "count": text = (status["model_count"] as? NSNumber)?.stringValue ?? "—"
        default: text = Self.statusText(source: source, status: status)
        }
        let field = NSTextField(labelWithString: text); field.font = .systemFont(ofSize: 11.5)
        field.lineBreakMode = .byTruncatingTail; field.toolTip = text
        if key == "priority" || key == "count" { field.alignment = .right; field.font = .monospacedDigitSystemFont(ofSize: 11.5, weight: .regular) }
        if source["enabled"] as? Bool == false { field.textColor = .tertiaryLabelColor }
        else if key == "status", status["status"] as? String == "error" { field.textColor = .systemOrange }
        let cell = NSTableCellView(frame: NSRect(x: 0, y: 0, width: tableColumn!.width, height: 34))
        field.frame = NSRect(x: 4, y: 8, width: max(20, tableColumn!.width - 8), height: 18)
        field.autoresizingMask = [.width, .minYMargin, .maxYMargin]
        cell.textField = field; cell.addSubview(field); return cell
    }
    private static func statusText(source: [String: Any], status: [String: Any]) -> String {
        if source["enabled"] as? Bool == false { return "已停用" }
        let kind = source["kind"] as? String ?? ""
        if let timestamp = status["updated_at"] as? NSNumber, timestamp.doubleValue.isFinite, timestamp.doubleValue > 0 {
            let date = DateFormatter(); date.locale = Locale(identifier: "zh_CN"); date.dateFormat = "MM-dd HH:mm"
            return (status["stale"] as? Bool == true ? "缓存 · " : "已更新 · ") + date.string(from: Date(timeIntervalSince1970: timestamp.doubleValue))
        }
        if let message = status["message"] as? String, !message.isEmpty { return message }
        if ["custom", "ccswitch"].contains(kind) { return status["status"] as? String == "unavailable" ? "本地来源暂不可读" : "本机价格" }
        return "尚未同步"
    }
    private func updateDetail() {
        guard sources.indices.contains(table.selectedRow) else { detail.stringValue = "价格按精确模型标识匹配。缺少模型时继续查找下一个来源；已有模型的未提供单价保持未知。"; return }
        let source = sources[table.selectedRow], id = source["id"] as? String ?? ""
        let url = source["url"] as? String ?? ""
        let status = statuses[id] ?? [:]
        detail.stringValue = (url.isEmpty ? ((source["kind"] as? String == "custom") ? "手动单价可在统计设置中的“模型单价”编辑。" : "使用 CC Switch 已有的模型单价。") : url) + "\n" + Self.statusText(source: source, status: status)
        if let message = status["message"] as? String, !message.isEmpty, !detail.stringValue.contains(message) { detail.stringValue += "\n" + message }
        detail.toolTip = detail.stringValue
    }
    @objc private func toggleSource(_ sender: NSButton) {
        guard !busy, sources.indices.contains(sender.tag) else { return }; sources[sender.tag]["enabled"] = sender.state == .on; changed(select: sender.tag)
    }
    @objc private func addSource() { presentEditor(nil) }
    @objc private func editSelected() { guard sources.indices.contains(table.selectedRow) else { return }; presentEditor(table.selectedRow) }
    private func presentEditor(_ index: Int?) {
        guard !busy, let window = presentationWindow, window.attachedSheet == nil else { return }
        let original = index.map { sources[$0] }
        let editor = CuePriceSourceEditor(source: original) { [weak self] newSource in
            guard let self else { return }
            if let index, self.sources.indices.contains(index) { self.sources[index] = newSource; self.statuses[newSource["id"] as? String ?? ""] = nil; self.changed(select: index) }
            else { self.sources.append(newSource); self.changed(select: self.sources.count - 1) }
            self.editor = nil
        }
        self.editor = editor; window.beginSheet(editor.window!)
    }
    @objc private func removeSelected() { guard !busy, sources.indices.contains(table.selectedRow) else { return }; let row = table.selectedRow; sources.remove(at: row); changed(select: min(row, sources.count - 1)) }
    @objc private func moveSourceUp() { move(-1) }
    @objc private func moveSourceDown() { move(1) }
    private func move(_ offset: Int) {
        let row = table.selectedRow, target = row + offset
        guard !busy, sources.indices.contains(row), sources.indices.contains(target) else { return }
        sources.swapAt(row, target); changed(select: target)
    }
    @objc private func restoreDefaults() { guard !busy else { return }; sources = defaults; interval.stringValue = "6"; changed(select: 0) }
    @objc private func saveQueue() { persist(refresh: false) }
    @objc private func refreshPrices() { persist(refresh: true) }
    private func persist(refresh: Bool) {
        guard !busy, let hours = Double(interval.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)), hours.isFinite, hours >= 0.5, hours <= 168 else { feedback.stringValue = "价格更新间隔请填写 0.5 至 168 小时。"; return }
        let patch: [String: Any] = ["pricing_sources": sources, "pricing_refresh_seconds": Int((hours * 3600).rounded())]
        guard let input = try? JSONSerialization.data(withJSONObject: patch) else { return }
        setBusy(true); feedback.stringValue = refresh ? "正在保存队列并更新公开价格…" : "正在保存来源队列…"
        client.run(["save"], module: "vibecue.usage_money", input: input) { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let data):
                self.apply(data); self.onChange()
                if refresh { self.fetchPrices() }
                else { self.setBusy(false); self.feedback.stringValue = "来源队列已保存；后续估算使用此优先级。" }
            case .failure(let error): self.setBusy(false); self.feedback.stringValue = error.localizedDescription; self.feedback.textColor = .systemRed
            }
        }
    }
    private func fetchPrices() {
        setBusy(true)
        client.run(["refresh-pricing", "--force"], module: "vibecue.usage_money") { [weak self] result in
            guard let self else { return }; self.setBusy(false)
            switch result {
            case .success(let data):
                self.apply(data); self.onChange()
                let warnings = self.statuses.values.filter { ["error", "unavailable"].contains($0["status"] as? String ?? "") }.count
                self.feedback.stringValue = warnings > 0 ? "价格更新完成；\(warnings) 个来源存在读取提示，详情见列表。已有缓存会标记保留。" : "公开模型价格已更新。"
                self.feedback.textColor = warnings > 0 ? .systemOrange : .secondaryLabelColor
            case .failure(let error): self.feedback.stringValue = error.localizedDescription; self.feedback.textColor = .systemRed
            }
        }
    }
    @objc private func showFormatHelp() {
        let alert = NSAlert(); alert.messageText = "模型价格来源格式"
        alert.informativeText = "BaseLLM、LiteLLM、Models.dev、OpenRouter、Vercel 使用各自公开接口格式，程序会换算为 USD / 百万 Tokens。\n\n自定义 JSON 示例：\n{\"models\":{\"example-model\":{\"input\":2,\"output\":10,\"cache_read\":0.2,\"cache_creation\":2.5}}}\n\n模型标识需精确一致。未提供的缓存单价保持未知，不能代替免费。只读取公开 HTTPS JSON，不发送用量、账号或认证资料。"
        alert.addButton(withTitle: "知道了"); alert.addButton(withTitle: "复制 JSON 示例")
        if let window = presentationWindow { alert.beginSheetModal(for: window) { response in
            if response == .alertSecondButtonReturn { NSPasteboard.general.clearContents(); NSPasteboard.general.setString("{\"models\":{\"example-model\":{\"input\":2,\"output\":10,\"cache_read\":0.2,\"cache_creation\":2.5}}}", forType: .string) }
        } }
    }
    static func captureFixture(to url: URL, dark: Bool) throws {
        let controller = CuePriceSourcesController(home: URL(fileURLWithPath: "/private/tmp/vibecue-pricing-offline-fixture"), onChange: {})
        controller.window?.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        controller.apply(fixture)
        controller.table.selectRowIndexes(IndexSet(integer: 2), byExtendingSelection: false)
        guard let root = controller.window?.contentView else { throw CocoaError(.fileReadUnknown) }
        root.layoutSubtreeIfNeeded(); root.displayIfNeeded()
        guard let bitmap = root.bitmapImageRepForCachingDisplay(in: root.bounds) else { throw CocoaError(.fileWriteUnknown) }
        root.effectiveAppearance.performAsCurrentDrawingAppearance { root.cacheDisplay(in: root.bounds, to: bitmap) }
        guard let data = bitmap.representation(using: .png, properties: [:]) else { throw CocoaError(.fileWriteUnknown) }; try data.write(to: url)
    }
    private static var fixture: [String: Any] {
        let rows: [[String: Any]] = [["id": "custom", "kind": "custom", "name": "手动模型单价", "enabled": true], ["id": "ccswitch", "kind": "ccswitch", "name": "CC Switch 本机价格", "enabled": true], ["id": "example", "kind": "json", "name": "公开价格来源样例", "url": "https://example.com/models.json", "enabled": true]]
        return ["prefs": ["pricing_sources": rows, "pricing_refresh_seconds": 21600], "default_pricing_sources": rows,
                "source_status": [["id": "custom", "model_count": 3, "status": "available"], ["id": "ccswitch", "model_count": 141, "status": "available"], ["id": "example", "model_count": 1460, "status": "available", "updated_at": 1_793_236_800.0, "stale": false]]]
    }
    static func validationReport() -> [String: Bool] {
        let controller = CuePriceSourcesController(home: URL(fileURLWithPath: "/private/tmp/vibecue-pricing-offline-fixture"), onChange: {})
        controller.apply(fixture); controller.table.selectRowIndexes(IndexSet(integer: 2), byExtendingSelection: false)
        controller.move(-1)
        let moved = (controller.sources[1]["id"] as? String == "example") && controller.table.selectedRow == 1
        let toggle = NSButton(); toggle.tag = 1; toggle.state = .off; controller.toggleSource(toggle)
        let disabled = controller.sources[1]["enabled"] as? Bool == false
        controller.removeSelected()
        let removed = !controller.sources.contains { $0["id"] as? String == "example" }
        controller.restoreDefaults()
        let host = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 940, height: 720), styleMask: [.titled], backing: .buffered, defer: false)
        let view = controller.embeddedContentView(); host.contentView = view
        view.setFrameSize(NSSize(width: 940, height: 720)); controller.layout()
        controller.deactivateEmbedded(); controller.activateEmbedded()
        let embeddedDraft = controller.dirty && controller.sources.count == 3 && !controller.busy
        let embeddedLayout = abs(controller.refreshButton.frame.maxX - (view.bounds.width - 24)) < 0.1 && abs(controller.refreshButton.frame.maxY - (view.bounds.height - 19)) < 0.1
        let embeddedHost = controller.presentationWindow === host && controller.embeddedContentView() === view && controller.window?.contentView !== view
        host.contentView = nil
        return ["pricing_embedded_canvas_uses_host_bounds_and_sheet_parent": embeddedHost && embeddedLayout,
                "pricing_tag_switch_keeps_unsaved_source_priority": embeddedDraft,
"pricing_queue_reorder_preserves_selected_source": moved,
                "pricing_queue_toggle_does_not_delete_config": disabled,
                "pricing_queue_delete_removes_only_selected_source": removed && controller.sources.count == 3,
                "pricing_queue_restore_defaults_is_unsaved_until_commit": controller.dirty && controller.sources.first?["id"] as? String == "custom",
                "pricing_queue_empty_selection_disables_row_actions": { controller.table.deselectAll(nil); controller.updateControls(); return !controller.removeButton.isEnabled && !controller.upButton.isEnabled }(),
                "pricing_update_interval_preserves_subhour_value": hourText(1800) == "0.5",
                "pricing_source_description_never_hides_stale_timestamp": { let text = statusText(source: ["kind": "json", "enabled": true], status: ["message": "接口价格说明", "updated_at": 1_793_236_800.0, "stale": true]); return text.hasPrefix("缓存 · ") && text.contains("10-29") }(),
                "pricing_source_editor_local_source_has_no_url_input": CuePriceSourceEditor(source: ["id": "custom", "kind": "custom", "name": "手动"], completion: { _ in }).urlEnabled == false]
    }
}

private final class CuePriceSourceEditor: NSWindowController {
    static let kinds = [("json", "标准 JSON"), ("basellm", "BaseLLM / NewAPI"), ("litellm", "LiteLLM"), ("models_dev", "Models.dev"), ("openrouter", "OpenRouter"), ("vercel", "Vercel AI Gateway"), ("custom", "手动价格"), ("ccswitch", "CC Switch")]
    static func title(for kind: String) -> String { kinds.first { $0.0 == kind }?.1 ?? kind }
    private let name = NSTextField(string: ""), url = NSTextField(string: ""), format = NSPopUpButton()
    private let feedback = NSTextField(wrappingLabelWithString: "")
    private let completion: ([String: Any]) -> Void
    private let original: [String: Any]
    private let local: Bool
    var urlEnabled: Bool { url.isEnabled }
    init(source: [String: Any]?, completion: @escaping ([String: Any]) -> Void) {
        original = source ?? ["id": "source-" + UUID().uuidString.lowercased(), "kind": "json", "name": "", "enabled": true]
        local = ["custom", "ccswitch"].contains(original["kind"] as? String ?? "")
        self.completion = completion
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 610, height: 330), styleMask: [.titled], backing: .buffered, defer: false)
        window.title = source == nil ? "添加模型价格来源" : "编辑模型价格来源"; window.isReleasedWhenClosed = false
        super.init(window: window)
        let root = CuePriceSourcesSurface(frame: window.contentLayoutRect); window.contentView = root
        func label(_ value: String, _ y: CGFloat) { let label = NSTextField(labelWithString: value); label.frame = NSRect(x: 24, y: y, width: 100, height: 22); root.addSubview(label) }
        label("来源名称", 27); label("接口格式", 75); label("公开 JSON URL", 123)
        name.frame = NSRect(x: 139, y: 24, width: 447, height: 26); name.stringValue = original["name"] as? String ?? ""
        format.frame = NSRect(x: 139, y: 70, width: 247, height: 30)
        for (kind, title) in Self.kinds where !["custom", "ccswitch"].contains(kind) || local { format.addItem(withTitle: title); format.lastItem?.representedObject = kind }
        if let item = format.itemArray.first(where: { $0.representedObject as? String == original["kind"] as? String }) { format.select(item) }
        format.isEnabled = !local
        url.frame = NSRect(x: 139, y: 120, width: 447, height: 26); url.stringValue = original["url"] as? String ?? ""
        url.placeholderString = local ? "本机来源无需 URL" : "https://example.com/models.json"; url.isEnabled = !local
        feedback.frame = NSRect(x: 25, y: 166, width: 560, height: 74); feedback.font = .systemFont(ofSize: 11); feedback.textColor = .secondaryLabelColor
        feedback.stringValue = "名称和顺序可自由调整。公开接口不需要账号授权；接口返回价格会统一换算为 USD / 百万 Tokens。"
        let cancel = NSButton(title: "取消", target: self, action: #selector(cancelEdit)), save = NSButton(title: "确定", target: self, action: #selector(commitEdit))
        cancel.bezelStyle = .rounded; save.bezelStyle = .rounded; cancel.frame = NSRect(x: 392, y: 276, width: 90, height: 30); save.frame = NSRect(x: 492, y: 276, width: 94, height: 30)
        cancel.keyEquivalent = "\u{1b}"; save.keyEquivalent = "\r"
        [name, format, url, feedback, cancel, save].forEach { root.addSubview($0) }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    @objc private func cancelEdit() { if let window { window.sheetParent?.endSheet(window); window.orderOut(nil) } }
    @objc private func commitEdit() {
        let label = name.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !label.isEmpty, label.count <= 100 else { feedback.stringValue = "请填写 1 至 100 字的来源名称。"; feedback.textColor = .systemRed; return }
        var source = original; source["name"] = label
        if !local {
            let address = url.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            guard let parsed = URLComponents(string: address), parsed.scheme?.lowercased() == "https", parsed.host != nil, parsed.user == nil, parsed.password == nil, parsed.fragment == nil, address.count <= 2048 else {
                feedback.stringValue = "请填写不含用户名、密码或片段的公开 HTTPS JSON 地址。"; feedback.textColor = .systemRed; return
            }
            source["url"] = address; source["kind"] = format.selectedItem?.representedObject as? String ?? "json"
        }
        cancelEdit(); completion(source)
    }
}

private final class CuePriceSourcesSurface: NSView {
    var onLayout: (() -> Void)?
    override var isFlipped: Bool { true }
    override func layout() { super.layout(); onLayout?() }
    override func draw(_ dirtyRect: NSRect) { NSColor.windowBackgroundColor.setFill(); dirtyRect.fill() }
}
