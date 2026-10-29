import AppKit
import Foundation

private final class CueEngineeringSettingsSurface: NSView {
    var onLayout: (() -> Void)?
    override var isFlipped: Bool { true }
    override func layout() { super.layout(); onLayout?() }
    override func draw(_ dirtyRect: NSRect) { NSColor.windowBackgroundColor.setFill(); dirtyRect.fill() }
}

private final class CueEngineeringVersionCard: NSBox {
    let name = NSTextField(labelWithString: "")
    let state = NSTextField(labelWithString: "等待检查")
    let versions = NSTextField(labelWithString: "当前 —    最新 —")
    let dates = NSTextField(labelWithString: "尚未检查")
    let message = NSTextField(labelWithString: "")
    let path = NSTextField(labelWithString: "")
    init(tool: String) {
        super.init(frame: .zero)
        boxType = .custom; borderWidth = 1; cornerRadius = 10
        borderColor = .separatorColor; fillColor = .controlBackgroundColor
        name.stringValue = tool; name.font = .systemFont(ofSize: 15, weight: .semibold)
        state.font = .systemFont(ofSize: 11.5, weight: .medium); state.alignment = .right
        versions.font = .monospacedDigitSystemFont(ofSize: 12, weight: .medium)
        dates.font = .systemFont(ofSize: 10.5); dates.textColor = .secondaryLabelColor
        message.font = .systemFont(ofSize: 11); message.textColor = .secondaryLabelColor
        path.font = .monospacedSystemFont(ofSize: 9.5, weight: .regular); path.textColor = .tertiaryLabelColor
        path.lineBreakMode = .byTruncatingMiddle
        for field in [name, state, versions, dates, message, path] {
            field.isSelectable = true; field.lineBreakMode = field === path ? .byTruncatingMiddle : .byTruncatingTail
            addSubview(field)
        }
        identifier = NSUserInterfaceItemIdentifier("engineering.settings.tool.\(tool)")
        setAccessibilityLabel("\(tool) 版本和更新状态")
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override var isFlipped: Bool { true }
    override func layout() {
        super.layout()
        let width = bounds.width - 28
        name.frame = NSRect(x: 14, y: 10, width: 90, height: 21)
        state.frame = NSRect(x: max(110, bounds.width - 230), y: 12, width: min(214, width - 100), height: 18)
        versions.frame = NSRect(x: 14, y: 35, width: width, height: 18)
        dates.frame = NSRect(x: 14, y: 57, width: width, height: 16)
        message.frame = NSRect(x: 14, y: 77, width: width, height: 16)
        path.frame = NSRect(x: 14, y: 97, width: width, height: 15)
    }
}

/// Small native settings surface. Activation only asks for a metadata snapshot;
/// network/update work is an explicit check or the app-owned maintenance task.
final class CueEngineeringSettingsController: NSWindowController {
    private let home: URL
    private let root = CueEngineeringSettingsSurface(frame: NSRect(x: 0, y: 0, width: 960, height: 650))
    private let titleField = NSTextField(labelWithString: "工具设置")
    private let detail = NSTextField(wrappingLabelWithString: "统一管理 mise、Hurl、lnav 的版本和自动更新。兼容性验证通过后才切换，失败保留可用版本。")
    private let automatic = NSButton(checkboxWithTitle: "自动检查并更新", target: nil, action: nil)
    private let interval = NSPopUpButton()
    private let intervalLabel = NSTextField(labelWithString: "检查频率")
    private let nextCheck = NSTextField(labelWithString: "下次检查：读取中")
    private let refreshButton = NSButton(title: "刷新状态", target: nil, action: nil)
    private let updateButton = NSButton(title: "检查并更新", target: nil, action: nil)
    private let spinner = NSProgressIndicator()
    private let summary = NSTextField(wrappingLabelWithString: "进入页面只读取本机版本与状态，不会启动更新。")
    private let footer = NSTextField(wrappingLabelWithString: "后台低优先级运行 · 工程任务执行时等待空闲 · 不重启 App · 可选择并复制版本和路径")
    private let tools = ["mise", "hurl", "lnav"]
    private var cards: [String: CueEngineeringVersionCard] = [:]
    private var intervals = [6, 12, 24, 72, 168]
    private var active = false, stopped = false, busy = false
    private var generation = 0, statusRequests = 0, updateRequests = 0, configurationRequests = 0, applications = 0, clientsCreated = 0
    private var client: CueUsageClient?
    private var currentCommand: String?
    private var snapshot: [String: Any]?
    private var snapshotRevision: Int64 = 0
    private var snapshotStateRevision: Int64 = 0
    private var snapshotSignature: String?
    private var appliedSignature: String?
    private var lastLayoutSize: NSSize?
    private var fixturePresentation: Bool?
    private var fixtureRun: ((String, [String: Any], @escaping (Result<[String: Any], Error>) -> Void) -> Void)?
    var didChange: (([String: Any]) -> Void)?

    private var presentationVisible: Bool {
        if let fixturePresentation { return active && fixturePresentation }
        guard let window = root.window else { return false }
        return active && window.isVisible && !window.isMiniaturized && window.occlusionState.contains(.visible)
    }
    var performanceSnapshot: [String: Any] {
        ["created": true, "active": active, "visible": presentationVisible, "busy": busy,
         "status_requests": statusRequests, "update_requests": updateRequests,
         "configuration_requests": configurationRequests, "result_applications": applications,
         "client_allocated": client != nil, "clients_created": clientsCreated,
         "scheduled_jobs": 0, "tool_rows": cards.count]
    }

    init(home: URL) {
        self.home = home
        super.init(window: nil)
        root.autoresizingMask = [.width, .height]
        titleField.font = .systemFont(ofSize: 21, weight: .semibold)
        detail.font = .systemFont(ofSize: 12); detail.textColor = .secondaryLabelColor; detail.maximumNumberOfLines = 2
        automatic.state = .on; automatic.target = self; automatic.action = #selector(saveSettings)
        automatic.identifier = NSUserInterfaceItemIdentifier("engineering.settings.automatic")
        interval.target = self; interval.action = #selector(saveSettings)
        interval.identifier = NSUserInterfaceItemIdentifier("engineering.settings.interval")
        interval.setAccessibilityLabel("工程工具自动检查间隔")
        populateIntervals(selected: 24)
        intervalLabel.font = .systemFont(ofSize: 11.5)
        nextCheck.font = .systemFont(ofSize: 11); nextCheck.textColor = .secondaryLabelColor
        nextCheck.isSelectable = true
        for button in [refreshButton, updateButton] {
            button.bezelStyle = .rounded; button.font = .systemFont(ofSize: 11.5); button.target = self
        }
        refreshButton.action = #selector(refreshStatus); updateButton.action = #selector(checkAndUpdate)
        refreshButton.identifier = NSUserInterfaceItemIdentifier("engineering.settings.refresh")
        updateButton.identifier = NSUserInterfaceItemIdentifier("engineering.settings.update")
        updateButton.toolTip = "从官方发布检查新版本；下载、校验和兼容性验证通过后自动切换。"
        spinner.style = .spinning; spinner.controlSize = .small; spinner.isDisplayedWhenStopped = false
        summary.font = .systemFont(ofSize: 11.5); summary.maximumNumberOfLines = 3; summary.isSelectable = true
        summary.identifier = NSUserInterfaceItemIdentifier("engineering.settings.summary")
        footer.font = .systemFont(ofSize: 10.5); footer.textColor = .secondaryLabelColor; footer.maximumNumberOfLines = 2
        [titleField, detail, automatic, interval, intervalLabel, nextCheck, refreshButton, updateButton, spinner, summary, footer].forEach { root.addSubview($0) }
        for name in tools { let card = CueEngineeringVersionCard(tool: name); cards[name] = card; root.addSubview(card) }
        root.onLayout = { [weak self] in self?.layout() }
        layout()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    deinit { client?.stop() }
    func embeddedContentView() -> NSView { root }
    func activateEmbedded() {
        guard !stopped else { return }
        let wasActive = active; active = true
        applyCachedSnapshot()
        if !wasActive { request("status") }
    }
    @discardableResult func deactivateEmbedded() -> Bool {
        active = false; cancelOwnedWork(); return true
    }
    func stop() { active = false; stopped = true; cancelOwnedWork() }

    /// App-owned automatic maintenance may hand its already-read result here.
    /// Hidden pages cache it without doing TextKit or launching another query.
    func updateSnapshot(_ data: [String: Any]) {
        _ = acceptSnapshot(data)
    }
    @discardableResult private func acceptSnapshot(_ data: [String: Any]) -> Bool {
        guard Self.valid(data) else { return false }
        let revision = (data["config_revision"] as? NSNumber)?.int64Value ?? 0
        let stateRevision = (data["state_revision"] as? NSNumber)?.int64Value ?? 0
        guard revision >= snapshotRevision,
              revision > snapshotRevision || stateRevision >= snapshotStateRevision else { return false }
        snapshot = data; snapshotRevision = revision; snapshotStateRevision = stateRevision
        snapshotSignature = Self.signature(data)
        applyCachedSnapshot(); return true
    }
    private func layout() {
        guard root.bounds.size != lastLayoutSize else { return }
        lastLayoutSize = root.bounds.size
        let w = root.bounds.width, h = root.bounds.height, width = max(500, w - 40)
        titleField.frame = NSRect(x: 20, y: 18, width: max(200, width - 280), height: 28)
        refreshButton.frame = NSRect(x: w - 258, y: 20, width: 100, height: 28)
        updateButton.frame = NSRect(x: w - 150, y: 20, width: 130, height: 28)
        spinner.frame = NSRect(x: w - 285, y: 26, width: 16, height: 16)
        detail.frame = NSRect(x: 20, y: 54, width: width, height: 34)
        automatic.frame = NSRect(x: 20, y: 101, width: 205, height: 24)
        intervalLabel.frame = NSRect(x: 244, y: 105, width: 62, height: 18)
        interval.frame = NSRect(x: 310, y: 99, width: 132, height: 27)
        nextCheck.frame = NSRect(x: 20, y: 138, width: width, height: 18)
        for (index, name) in tools.enumerated() {
            cards[name]?.frame = NSRect(x: 20, y: 175 + CGFloat(index) * 122, width: width, height: 116)
        }
        summary.frame = NSRect(x: 20, y: 547, width: width, height: min(40, max(24, h - 582)))
        footer.frame = NSRect(x: 20, y: max(574, h - 32), width: width, height: 22)
    }
    private func populateIntervals(selected: Int) {
        intervals = [6, 12, 24, 72, 168]
        if !intervals.contains(selected) { intervals.append(selected); intervals.sort() }
        interval.removeAllItems()
        interval.addItems(withTitles: intervals.map { $0 == 24 ? "每天" : $0 == 168 ? "每周" : "每 \($0) 小时" })
        interval.selectItem(at: intervals.firstIndex(of: selected) ?? 2)
    }
    private func setBusy(_ value: Bool) {
        busy = value
        automatic.isEnabled = !value; interval.isEnabled = !value && automatic.state == .on
        refreshButton.isEnabled = !value; updateButton.isEnabled = !value
        if value { spinner.startAnimation(nil) } else { spinner.stopAnimation(nil) }
    }
    @objc private func refreshStatus() { request("status") }
    @objc private func checkAndUpdate() { request("check") }
    @objc private func saveSettings() {
        let selected = max(0, min(interval.indexOfSelectedItem, intervals.count - 1))
        request("configure", payload: ["automatic_updates": automatic.state == .on, "interval_hours": intervals[selected]])
    }
    private func request(_ command: String, payload: [String: Any] = [:]) {
        guard active && !stopped && !busy else { return }
        generation += 1; let expected = generation; currentCommand = command
        if command == "configure" { appliedSignature = nil }
        if command == "status" { statusRequests += 1 }
        else if command == "configure" { configurationRequests += 1 }
        else { updateRequests += 1 }
        setBusy(true)
        summary.textColor = .secondaryLabelColor
        summary.stringValue = command == "check" ? "正在后台检查、验证兼容性并更新；工程任务运行时会等待空闲。" : command == "configure" ? "正在保存更新设置…" : "正在读取本机版本和更新状态…"
        let completion: (Result<[String: Any], Error>) -> Void = { [weak self] result in
            guard let self, self.generation == expected, !self.stopped else { return }
            self.currentCommand = nil; self.setBusy(false)
            switch result {
            case .success(let data):
                if Self.controlledError(data) {
                    let reason = Self.text(data["summary"], limit: 300)
                    self.showError(reason.isEmpty ? "操作未完成；保留上次显示。" : reason, command: command)
                    return
                }
                guard Self.valid(data) else { self.showError("工具状态返回了无法识别的数据；保留上次显示。", command: command); return }
                guard self.acceptSnapshot(data) else { self.applyCachedSnapshot(); return }
                if command != "status" { self.didChange?(data) }
            case .failure(let error): self.showError(Self.text(error.localizedDescription, limit: 300), command: command)
            }
        }
        if let fixtureRun { fixtureRun(command, payload, completion); return }
        if client == nil { client = CueUsageClient(home: home); clientsCreated += 1 }
        let input = try? JSONSerialization.data(withJSONObject: payload)
        client?.run([command], module: "vibecue.engineering_maintenance", input: input, completion: completion)
    }
    private func cancelOwnedWork() {
        guard busy || client != nil else { return }
        let wasConfigure = currentCommand == "configure"
        generation += 1; client?.stop(); client = nil; currentCommand = nil; setBusy(false)
        if wasConfigure { appliedSignature = nil }
    }
    private func showError(_ message: String, command: String) {
        appliedSignature = nil
        if presentationVisible {
            applyCachedSnapshot()
            summary.textColor = .systemOrange
            summary.stringValue = (command == "configure" ? "设置尚未确认保存。" : "状态暂不可读。") + message
        }
    }
    private func applyCachedSnapshot() {
        guard presentationVisible, let data = snapshot,
              snapshotSignature != appliedSignature, let config = data["config"] as? [String: Any] else { return }
        applications += 1; appliedSignature = snapshotSignature
        automatic.state = (config["automatic_updates"] as? Bool) == true ? .on : .off
        populateIntervals(selected: (config["interval_hours"] as? NSNumber)?.intValue ?? 24)
        interval.isEnabled = !busy && automatic.state == .on
        let next = Self.dateText(data["next_check_at"], empty: "等待后台调度")
        nextCheck.stringValue = automatic.state == .on ? "下次检查：\(next)" : "自动更新已关闭；仍可手动检查并更新。"
        let items = (data["tools"] as? [[String: Any]] ?? []).prefix(12)
        for name in tools {
            guard let card = cards[name] else { continue }
            let item = items.first { ($0["name"] as? String)?.lowercased() == name } ?? [:]
            let rawState = Self.text(item["status"], limit: 50)
            let state = Self.state(rawState)
            card.state.stringValue = state.0; card.state.textColor = state.1
            let installed = Self.text(item["installed_version"], limit: 100)
            let available = Self.text(item["available_version"], limit: 100)
            card.versions.stringValue = "当前 \(installed.isEmpty ? "未安装" : installed)    最新 \(available.isEmpty ? "尚未检查" : available)"
            card.dates.stringValue = "上次检查 \(Self.dateText(item["checked_at"], empty: "—"))    上次更新 \(Self.dateText(item["updated_at"], empty: "—"))"
            if let checked = item["compatibility_checked_at"] as? NSNumber, checked.doubleValue > 0 {
                card.dates.stringValue += "    兼容验证 \(Self.dateText(checked, empty: "—"))"
            }
            let message = Self.text(item["message"], limit: 400)
            card.message.stringValue = message.isEmpty ? "官方稳定版 · 校验及兼容性验证通过后才启用" : message
            card.message.toolTip = message.isEmpty ? nil : message
            card.path.stringValue = Self.text(item["path"], limit: 500)
            card.path.toolTip = card.path.stringValue
        }
        summary.textColor = .secondaryLabelColor
        summary.stringValue = Self.text(data["summary"], limit: 400)
        if summary.stringValue.isEmpty { summary.stringValue = "版本来自本机托管清单；更新状态保留最后一次检查结果。" }
    }
    private static func valid(_ data: [String: Any]) -> Bool {
        guard (data["schema_version"] as? NSNumber)?.intValue == 1,
              data["kind"] as? String == "engineering_settings",
              let config = data["config"] as? [String: Any], config["automatic_updates"] is Bool,
              let interval = config["interval_hours"] as? NSNumber,
              interval.doubleValue.isFinite, (6...168).contains(interval.intValue),
              let rows = data["tools"] as? [[String: Any]], rows.count <= 12 else { return false }
        for key in ["config_revision", "state_revision"] {
            if let revision = data[key] as? NSNumber {
                guard revision.doubleValue.isFinite, revision.doubleValue >= 0, revision.doubleValue < Double(Int64.max) else { return false }
            }
        }
        return true
    }
    private static func controlledError(_ data: [String: Any]) -> Bool {
        (data["schema_version"] as? NSNumber)?.intValue == 1 && data["kind"] as? String == "engineering_settings" && data["status"] as? String == "error"
    }
    private static func signature(_ data: [String: Any]) -> String {
        // Fixed, small display projection. Avoid sorting or encoding unbounded
        // backend diagnostics merely to decide whether three rows changed.
        let config = data["config"] as? [String: Any] ?? [:]
        var parts = [text(config["automatic_updates"], limit: 8), text(config["interval_hours"], limit: 8), text(data["summary"], limit: 400), text(data["next_check_at"], limit: 80)]
        for item in (data["tools"] as? [[String: Any]] ?? []).prefix(12) {
            parts += ["name", "installed_version", "available_version", "status", "message", "checked_at", "updated_at", "path", "compatibility_checked_at", "compatible"].map { text(item[$0], limit: $0 == "path" ? 500 : 400) }
        }
        return parts.map { "\($0.utf8.count):\($0)" }.joined()
    }
    private static func text(_ value: Any?, limit: Int) -> String {
        let value = (value as? String) ?? (value as? NSNumber)?.stringValue ?? ""
        return String(value.unicodeScalars.filter { $0.value >= 32 && $0.value != 127 }.prefix(limit))
    }
    private static func dateText(_ value: Any?, empty: String) -> String {
        var date: Date?
        if let number = value as? NSNumber, number.doubleValue.isFinite && number.doubleValue > 0 { date = Date(timeIntervalSince1970: number.doubleValue) }
        else if let string = value as? String, !string.isEmpty {
            let parser = ISO8601DateFormatter(); parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            date = parser.date(from: string)
            if date == nil { parser.formatOptions = [.withInternetDateTime]; date = parser.date(from: string) }
            if date == nil, let epoch = Double(string), epoch.isFinite && epoch > 0 { date = Date(timeIntervalSince1970: epoch) }
        }
        guard let date else { return empty }
        let formatter = DateFormatter(); formatter.locale = Locale(identifier: "zh_CN"); formatter.dateFormat = "MM-dd HH:mm"
        return formatter.string(from: date)
    }
    private static func state(_ value: String) -> (String, NSColor) {
        switch value {
        case "updated": return ("已更新并验证", .systemGreen)
        case "current", "up_to_date", "compatible", "ready": return ("当前可用", .systemGreen)
        case "checking": return ("检查中", .systemBlue)
        case "downloading", "installing", "validating", "updating": return ("验证并更新中", .systemBlue)
        case "deferred", "waiting", "busy": return ("等待空闲", .secondaryLabelColor)
        case "incompatible", "error", "failed", "blocked", "verification_failed": return ("保留当前版本", .systemOrange)
        case "available", "update_available": return ("有可用更新", .systemBlue)
        case "missing", "not_installed": return ("尚未安装", .secondaryLabelColor)
        case "disabled": return ("自动更新已关闭", .secondaryLabelColor)
        case "cancelled": return ("已取消，稍后重试", .secondaryLabelColor)
        case "unchecked": return ("尚未检查", .secondaryLabelColor)
        default: return ("尚未检查", .secondaryLabelColor)
        }
    }

    static func validationReport() -> [String: Bool] {
        let controller = Self(home: URL(fileURLWithPath: "/tmp/vcgo-engineering-settings-fixture"))
        let initial = controller.client == nil && controller.window == nil && controller.statusRequests == 0 && controller.updateRequests == 0
        var commands: [String] = [], payloads: [[String: Any]] = [], completions: [(Result<[String: Any], Error>) -> Void] = []
        controller.fixtureRun = { command, payload, completion in commands.append(command); payloads.append(payload); completions.append(completion) }
        let data: [String: Any] = ["schema_version": 1, "kind": "engineering_settings", "config": ["automatic_updates": true, "interval_hours": 24], "summary": "三个工具状态已读取", "next_check_at": 1_800_000_000,
            "tools": ["mise", "hurl", "lnav"].map { ["name": $0, "installed_version": "1.2.3", "available_version": "1.2.4", "status": "incompatible", "message": "新版本未通过；保留可用版本", "path": "/private/app-tools/\($0)", "checked_at": 1_799_900_000, "compatibility_checked_at": 1_799_900_100] }]
        controller.fixturePresentation = true; controller.activateEmbedded(); controller.activateEmbedded()
        let statusOnly = commands == ["status"] && controller.updateRequests == 0 && controller.client == nil
        completions[0](.success(data))
        let inventory = controller.cards["hurl"]?.dates.stringValue.contains("兼容验证") == true && controller.cards.count == 3 && controller.cards["mise"]?.versions.stringValue.contains("1.2.3") == true && controller.cards["lnav"]?.versions.stringValue.contains("1.2.4") == true
        let preserved = controller.cards.values.allSatisfy { $0.state.stringValue == "保留当前版本" && $0.message.stringValue.contains("未通过") }
        let applied = controller.applications; controller.updateSnapshot(data); controller.activateEmbedded()
        let reused = controller.applications == applied && commands == ["status"]
        controller.automatic.state = .off; controller.saveSettings()
        let configured = commands.last == "configure" && payloads.last?["automatic_updates"] as? Bool == false && payloads.last?["interval_hours"] as? Int == 24
        var disabled = data; disabled["config"] = ["automatic_updates": false, "interval_hours": 12]
        completions.last?(.success(disabled))
        let saved = controller.automatic.state == .off && controller.interval.titleOfSelectedItem == "每 12 小时" && !controller.interval.isEnabled
        var callbacks = 0; controller.didChange = { _ in callbacks += 1 }
        controller.checkAndUpdate(); completions.last?(.success(disabled))
        let manual = commands.last == "check" && controller.updateRequests == 1 && callbacks == 1
        let applications = controller.applications; controller.deactivateEmbedded()
        var hidden = disabled; hidden["summary"] = "后台已完成新的检查"; hidden["config"] = ["automatic_updates": false, "interval_hours": 72]
        controller.updateSnapshot(hidden)
        let cached = controller.applications == applications && controller.snapshot?["summary"] as? String == "后台已完成新的检查"
        controller.activateEmbedded()
        let restored = controller.interval.titleOfSelectedItem == "每 72 小时" && controller.applications > applications && commands.last == "status"
        let pending = completions.last; controller.deactivateEmbedded(); pending?(.success(data))
        let staleRejected = controller.automatic.state == .off && !controller.busy
        var newer = hidden; newer["config_revision"] = 2; controller.updateSnapshot(newer)
        var older = data; older["config_revision"] = 1; controller.updateSnapshot(older)
        let revisionGuard = controller.snapshotRevision == 2 && (controller.snapshot?["config"] as? [String: Any])?["interval_hours"] as? Int == 72
        // A status process read during an update must not put an older checking
        // state back after the app-owned updater hands in its final result.
        let racing = Self(home: URL(fileURLWithPath: "/tmp/vcgo-engineering-settings-race-fixture"))
        var raceCompletion: ((Result<[String: Any], Error>) -> Void)?
        racing.fixturePresentation = true
        racing.fixtureRun = { _, _, completion in raceCompletion = completion }
        racing.activateEmbedded()
        var finalState = data; finalState["config_revision"] = 4; finalState["state_revision"] = 12; finalState["summary"] = "更新已完成，新版本可用"
        finalState["tools"] = ["mise", "hurl", "lnav"].map { ["name": $0, "installed_version": "2.0.0", "available_version": "2.0.0", "status": "updated"] }
        racing.updateSnapshot(finalState)
        var checkingState = data; checkingState["config_revision"] = 4; checkingState["state_revision"] = 11; checkingState["summary"] = "正在检查旧状态"
        raceCompletion?(.success(checkingState))
        let stateRevisionGuard = racing.snapshotStateRevision == 12 && racing.cards["mise"]?.versions.stringValue.contains("2.0.0") == true && racing.summary.stringValue == "更新已完成，新版本可用"
        racing.refreshStatus()
        let controlled = ["schema_version": 1, "kind": "engineering_settings", "status": "error", "summary": "工具设置文件无法读取，请保留当前版本。"] as [String: Any]
        raceCompletion?(.success(controlled))
        let controlledCause = racing.summary.stringValue.contains("工具设置文件无法读取") && !racing.summary.stringValue.contains("无法识别的数据") && racing.cards["mise"]?.versions.stringValue.contains("2.0.0") == true && racing.snapshotStateRevision == 12
        let compact = Self(home: URL(fileURLWithPath: "/tmp/vcgo-engineering-settings-size-fixture"))
        compact.root.frame.size = NSSize(width: 860, height: 600); compact.layout()
        let fits = compact.cards.values.allSatisfy { $0.frame.maxX <= 860 && $0.frame.maxY <= compact.summary.frame.minY } && compact.footer.frame.maxY <= 600
        return ["engineering_settings_construction_has_no_window_client_or_jobs": initial,
                "engineering_settings_activation_only_reads_metadata_without_network_update": statusOnly,
                "engineering_settings_three_tools_show_versions_and_failure_preservation": inventory && preserved,
                "engineering_settings_same_snapshot_and_visibility_do_not_repeat_work": reused,
                "engineering_settings_changes_save_explicit_parameters_and_readback": configured && saved,
                "engineering_settings_manual_check_works_when_automatic_is_disabled": manual,
                "engineering_settings_hidden_updates_cache_and_visible_restoration_applies": cached && restored,
                "engineering_settings_cancelled_page_rejects_late_completion": staleRejected,
                "engineering_settings_late_snapshot_cannot_revert_newer_configuration": revisionGuard,
                "engineering_settings_delayed_status_cannot_replace_newer_automatic_update": stateRevisionGuard,
                "engineering_settings_controlled_errors_keep_cause_and_last_good_versions": controlledCause,
                "engineering_settings_compact_native_cards_fit_workbench": fits,
                "engineering_settings_no_extra_polling_or_duplicate_rows": controller.cards.count == 3 && controller.performanceSnapshot["scheduled_jobs"] as? Int == 0]
    }
}
