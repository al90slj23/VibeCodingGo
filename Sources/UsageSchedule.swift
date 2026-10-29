import AppKit
import Foundation
import CoreFoundation

extension Notification.Name {
    static let cueUsageScheduleDidChange = Notification.Name("VibeCueUsageScheduleDidChange")
}

/// Shared by both statistics timers. Reading preferences never starts a scan.
struct CueUsageSchedulePreferences {
    let autoSync: Bool
    let syncInterval: TimeInterval
    let displayInterval: TimeInterval

    init(autoSync: Bool = true, syncInterval: TimeInterval = 600, displayInterval: TimeInterval = 30) {
        self.autoSync = autoSync; self.syncInterval = syncInterval; self.displayInterval = displayInterval
    }
    init(raw: [String: Any]) {
        func seconds(_ key: String, fallback: Double, range: ClosedRange<Double>) -> Double {
            guard let value = raw[key] as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(),
                  value.doubleValue.isFinite, value.doubleValue.rounded() == value.doubleValue,
                  range.contains(value.doubleValue) else { return fallback }
            return value.doubleValue
        }
        if let value = raw["auto_sync"] as? NSNumber, CFGetTypeID(value) == CFBooleanGetTypeID() { autoSync = value.boolValue }
        else { autoSync = true }
        syncInterval = seconds("sync_interval_seconds", fallback: 600, range: 30...86400)
        displayInterval = seconds("display_interval_seconds", fallback: 30, range: 5...3600)
    }
    init(home: URL) {
        let url = home.appendingPathComponent("usage-schedule.json")
        guard let size = try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize, size <= 16384,
              let data = try? Data(contentsOf: url), data.count <= 16384,
              let raw = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(raw.keys).isSubset(of: ["auto_sync", "sync_interval_seconds", "display_interval_seconds"]),
              raw.allSatisfy({ key, value in
                  guard let number = value as? NSNumber else { return false }
                  if key == "auto_sync" { return CFGetTypeID(number) == CFBooleanGetTypeID() }
                  let limits: ClosedRange<Double> = key == "sync_interval_seconds" ? 30...86400 : 5...3600
                  return CFGetTypeID(number) != CFBooleanGetTypeID() && number.doubleValue.isFinite && number.doubleValue.rounded() == number.doubleValue && limits.contains(number.doubleValue)
              }) else { self.init(); return }
        self.init(raw: raw)
    }
    var dictionary: [String: Any] { ["auto_sync": autoSync, "sync_interval_seconds": Int(syncInterval), "display_interval_seconds": Int(displayInterval)] }
    static func duration(_ seconds: TimeInterval) -> String {
        let value = Int(seconds)
        if value % 3600 == 0 { return "\(value / 3600) 小时" }
        if value % 60 == 0 { return "\(value / 60) 分钟" }
        return "\(value) 秒"
    }
    var summary: String {
        "每 \(Self.duration(displayInterval))刷新统计显示；" + (autoSync ? "每 \(Self.duration(syncInterval))同步本机历史。" : "历史用量仅手动同步。")
    }
    func nextSync(now: Date = Date(), lastSync: Date?, lastAttempt: Date?) -> Date? {
        guard autoSync else { return nil }
        let prior = [lastSync, lastAttempt].compactMap { $0 }.max()
        return prior.map { max(now, $0.addingTimeInterval(syncInterval)) } ?? now
    }
}

private final class CueUsageScheduleSurface: NSView {
    override var isFlipped: Bool { true }
    override func draw(_ dirtyRect: NSRect) { NSColor.windowBackgroundColor.setFill(); dirtyRect.fill() }
}

final class CueUsageScheduleController: NSWindowController {
    private let root = CueUsageScheduleSurface(frame: NSRect(x: 0, y: 0, width: 620, height: 385))
    private var isEmbedded = false
    private var loaded = false
    private let home: URL
    private let client: CueUsageClient
    private let onChange: () -> Void
    private let autoSync = NSButton(checkboxWithTitle: "自动同步本机历史用量", target: nil, action: nil)
    private let syncSeconds = NSTextField(string: "600"), displaySeconds = NSTextField(string: "30")
    private let feedback = NSTextField(wrappingLabelWithString: "")
    private let saveButton = NSButton(title: "保存频率设置", target: nil, action: nil)
    private let defaultsButton = NSButton(title: "恢复默认值", target: nil, action: nil)
    private let cancelButton = NSButton(title: "取消", target: nil, action: nil)
    private var busy = false

    init(home: URL, onChange: @escaping () -> Void) {
        self.home = home; self.client = CueUsageClient(home: home); self.onChange = onChange
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 620, height: 385), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "用量统计频率"; window.isReleasedWhenClosed = false; window.center()
        super.init(window: window)
        window.contentView = root
        func label(_ value: String, x: CGFloat = 26, y: CGFloat, width: CGFloat = 568, height: CGFloat = 22, size: CGFloat = 12, weight: NSFont.Weight = .regular, secondary: Bool = false) {
            let field = NSTextField(wrappingLabelWithString: value); field.font = .systemFont(ofSize: size, weight: weight)
            field.textColor = secondary ? .secondaryLabelColor : .labelColor
            field.frame = NSRect(x: x, y: y, width: width, height: height); root.addSubview(field)
        }
        label("用量统计频率", y: 23, size: 20, weight: .semibold)
        label("同步更新本机用量账本；显示刷新只重新读取现有统计数字。", y: 59, secondary: true)
        autoSync.frame = NSRect(x: 25, y: 101, width: 540, height: 25)
        autoSync.target = self; autoSync.action = #selector(toggleAutomatic)
        autoSync.setAccessibilityLabel("自动同步本机历史用量")
        label("历史同步间隔", y: 147)
        syncSeconds.frame = NSRect(x: 198, y: 143, width: 130, height: 27)
        label("秒（30 ～ 86,400）", x: 344, y: 147, width: 240, secondary: true)
        label("统计显示刷新间隔", y: 193)
        displaySeconds.frame = NSRect(x: 198, y: 189, width: 130, height: 27)
        label("秒（5 ～ 3,600）", x: 344, y: 193, width: 240, secondary: true)
        syncSeconds.setAccessibilityLabel("历史同步间隔秒数"); displaySeconds.setAccessibilityLabel("统计显示刷新间隔秒数")
        label("关闭自动同步后，仍可随时点击“立即同步”。此设置不改变任务的秒级计时、账号额度或价格来源的更新周期。", y: 239, height: 43, size: 11, secondary: true)
        feedback.frame = NSRect(x: 27, y: 289, width: 565, height: 32); feedback.font = .systemFont(ofSize: 11); feedback.textColor = .secondaryLabelColor
        defaultsButton.frame = NSRect(x: 22, y: 335, width: 120, height: 30)
        cancelButton.frame = NSRect(x: 325, y: 335, width: 100, height: 30)
        saveButton.frame = NSRect(x: 432, y: 335, width: 163, height: 30)
        for button in [saveButton, defaultsButton, cancelButton] { button.bezelStyle = .rounded; button.target = self }
        saveButton.action = #selector(save); saveButton.keyEquivalent = "\r"
        defaultsButton.action = #selector(defaults); cancelButton.action = #selector(cancel); cancelButton.keyEquivalent = "\u{1b}"
        [autoSync, syncSeconds, displaySeconds, feedback, defaultsButton, cancelButton, saveButton].forEach { root.addSubview($0) }
        apply(CueUsageSchedulePreferences(home: home)); updateControls()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    func embeddedContentView() -> NSView {
        if !isEmbedded {
            isEmbedded = true; window?.orderOut(nil); window?.contentView = nil
            cancelButton.title = "撤销更改"
        }
        return root
    }
    func activateEmbedded() { if !loaded && !busy { load() } }
    func deactivateEmbedded() { /* Keep unsubmitted frequency choices. */ }
    override func showWindow(_ sender: Any?) {
        if isEmbedded { activateEmbedded(); root.window?.makeKeyAndOrderFront(nil); return }
        super.showWindow(sender); window?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        load()
    }
    private func load() {
        guard !busy else { return }; setBusy(true)
        client.run(["settings"], module: "vibecue.usage_schedule") { [weak self] result in
            guard let self else { return }; self.setBusy(false)
            switch result {
            case .success(let data): self.loaded = true; self.apply(CueUsageSchedulePreferences(raw: data["prefs"] as? [String: Any] ?? [:])); self.feedback.stringValue = data["settings_warning"] as? String ?? "设置在保存后立即生效。"
            case .failure(let error): self.feedback.stringValue = error.localizedDescription
            }
        }
    }
    private func apply(_ prefs: CueUsageSchedulePreferences) {
        autoSync.state = prefs.autoSync ? .on : .off
        syncSeconds.stringValue = String(Int(prefs.syncInterval)); displaySeconds.stringValue = String(Int(prefs.displayInterval)); updateControls()
    }
    private func setBusy(_ value: Bool) { busy = value; updateControls() }
    private func updateControls() {
        cancelButton.isEnabled = !busy
        autoSync.isEnabled = !busy; syncSeconds.isEnabled = !busy && autoSync.state == .on
        displaySeconds.isEnabled = !busy; saveButton.isEnabled = !busy; defaultsButton.isEnabled = !busy
    }
    @objc private func toggleAutomatic() { updateControls() }
    @objc private func defaults() { apply(CueUsageSchedulePreferences()); feedback.stringValue = "已填入默认值，点击保存后生效。" }
    @objc private func cancel() {
        guard !busy else { return }
        if isEmbedded {
            apply(CueUsageSchedulePreferences(home: home))
            feedback.stringValue = "已撤销未保存的频率更改。"; feedback.textColor = .secondaryLabelColor
        } else { window?.close() }
    }
    @objc private func save() {
        guard !busy else { return }
        func integer(_ field: NSTextField, range: ClosedRange<Int>) -> Int? {
            let raw = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            guard let value = Int(raw), range.contains(value) else { return nil }; return value
        }
        let sync = integer(syncSeconds, range: 30...86400) ?? (autoSync.state == .off ? Int(CueUsageSchedulePreferences(home: home).syncInterval) : nil)
        guard let sync, let display = integer(displaySeconds, range: 5...3600) else {
            feedback.stringValue = "请填写范围内的整数秒数。"; feedback.textColor = .systemRed; return
        }
        let prefs = CueUsageSchedulePreferences(autoSync: autoSync.state == .on, syncInterval: TimeInterval(sync), displayInterval: TimeInterval(display))
        guard let input = try? JSONSerialization.data(withJSONObject: prefs.dictionary) else { return }
        setBusy(true)
        client.run(["save"], module: "vibecue.usage_schedule", input: input) { [weak self] result in
            guard let self else { return }; self.setBusy(false)
            switch result {
            case .success(let data):
                self.loaded = true
                self.apply(CueUsageSchedulePreferences(raw: data["prefs"] as? [String: Any] ?? [:]))
                self.feedback.stringValue = "频率设置已保存并立即生效。"; self.feedback.textColor = .secondaryLabelColor
                NotificationCenter.default.post(name: .cueUsageScheduleDidChange, object: nil, userInfo: ["home": self.home.path])
                self.onChange()
            case .failure(let error): self.feedback.stringValue = error.localizedDescription; self.feedback.textColor = .systemRed
            }
        }
    }
    static func captureFixture(to url: URL, dark: Bool) throws {
        let controller = CueUsageScheduleController(home: URL(fileURLWithPath: "/private/tmp/vibecue-schedule-offline-fixture"), onChange: {})
        controller.window?.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        controller.apply(CueUsageSchedulePreferences(autoSync: true, syncInterval: 300, displayInterval: 15))
        guard let root = controller.window?.contentView else { throw CocoaError(.fileWriteUnknown) }
        root.layoutSubtreeIfNeeded()
        guard let bitmap = root.bitmapImageRepForCachingDisplay(in: root.bounds) else { throw CocoaError(.fileWriteUnknown) }
        root.effectiveAppearance.performAsCurrentDrawingAppearance { root.cacheDisplay(in: root.bounds, to: bitmap) }
        guard let png = bitmap.representation(using: .png, properties: [:]) else { throw CocoaError(.fileWriteUnknown) }
        try png.write(to: url)
    }
    static func validationReport() -> [String: Bool] {
        let controller = CueUsageScheduleController(home: URL(fileURLWithPath: "/private/tmp/vibecue-schedule-offline-fixture"), onChange: {})
        controller.apply(CueUsageSchedulePreferences(autoSync: false, syncInterval: 1234, displayInterval: 17))
        let manualDisabled = !controller.syncSeconds.isEnabled && controller.displaySeconds.isEnabled
        controller.autoSync.state = .on; controller.toggleAutomatic()
        let automaticEnabled = controller.syncSeconds.isEnabled
        let custom = CueUsageSchedulePreferences(raw: ["auto_sync": false, "sync_interval_seconds": 1234, "display_interval_seconds": 17])
        let invalid = CueUsageSchedulePreferences(raw: ["auto_sync": 1, "sync_interval_seconds": true, "display_interval_seconds": 0])
        let now = Date(timeIntervalSince1970: 10000)
        let prefs = CueUsageSchedulePreferences(syncInterval: 100)
        let host = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 620, height: 385), styleMask: [.titled], backing: .buffered, defer: false)
        let view = controller.embeddedContentView(); host.contentView = view
        controller.loaded = true; controller.syncSeconds.stringValue = "1234"
        controller.deactivateEmbedded(); controller.activateEmbedded()
        let draftRetained = controller.syncSeconds.stringValue == "1234" && !controller.busy
        controller.cancel()
        let cancelledInPanel = controller.feedback.stringValue.contains("已撤销") && view.window === host && controller.cancelButton.title == "撤销更改"
        let stableCanvas = controller.embeddedContentView() === view && controller.window?.contentView !== view
        host.contentView = nil
        return ["usage_schedule_embedded_canvas_is_reusable": stableCanvas,
                "usage_schedule_tag_switch_preserves_unsaved_frequency": draftRetained,
                "usage_schedule_embedded_cancel_never_closes_main_panel": cancelledInPanel,
"usage_schedule_manual_disables_sync_field_only": manualDisabled && automaticEnabled,
                "usage_schedule_custom_intervals_are_preserved": !custom.autoSync && custom.syncInterval == 1234 && custom.displayInterval == 17,
                "usage_schedule_invalid_numeric_boolean_defaults": invalid.autoSync && invalid.syncInterval == 600 && invalid.displayInterval == 30,
                "usage_schedule_manual_never_has_automatic_due_date": custom.nextSync(now: now, lastSync: nil, lastAttempt: nil) == nil,
                "usage_schedule_uses_recent_success_for_next_sync": prefs.nextSync(now: now, lastSync: now.addingTimeInterval(-20), lastAttempt: nil) == now.addingTimeInterval(80),
                "usage_schedule_failed_attempt_waits_full_interval": prefs.nextSync(now: now, lastSync: now.addingTimeInterval(-1000), lastAttempt: now.addingTimeInterval(-10)) == now.addingTimeInterval(90),
                "usage_schedule_first_import_can_run_immediately": prefs.nextSync(now: now, lastSync: nil, lastAttempt: nil) == now]
    }
}
