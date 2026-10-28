import AppKit
import Foundation

/// A live navigation target from the watcher, separate from the alert ledger.
struct CueRunningTask {
    let threadID: String
    let title: String
    let provider: String
    let startDate: Date
    let updatedDate: Date
    let turnID: String

    init?(raw: [String: Any]) {
        guard let threadID = raw["thread_id"] as? String, !threadID.isEmpty, threadID.count <= 160,
              threadID.unicodeScalars.allSatisfy({ CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_")).contains($0) }),
              (raw["provider"] as? String ?? "codex") == "codex",
              let startDate = Self.date(raw["started_at"]),
              let updatedDate = Self.date(raw["last_activity"]) else { return nil }
        self.threadID = threadID
        self.title = String((raw["title"] as? String ?? "Codex 对话").prefix(180))
        self.provider = "codex"; self.startDate = startDate; self.updatedDate = updatedDate
        self.turnID = String((raw["turn_id"] as? String ?? "").prefix(160))
    }

    private static func date(_ raw: Any?) -> Date? {
        if let value = raw as? Double, value.isFinite, value > 0 { return Date(timeIntervalSince1970: value) }
        guard let value = raw as? String else { return nil }
        let formatter = ISO8601DateFormatter(); formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    }

    var navigationID: String { "running:\(threadID)" }
    var ephemeralCueEvent: CueEvent { CueEvent(runningThreadID: threadID, title: title, date: startDate) }
}

/// A derived view only: running tasks never mutate, replace or consume the ledger.
private struct CueStatusListSnapshot {
    let pinned: [CueRecentRecord]
    let running: [CueRunningTask]
    let ordinary: [CueRecentRecord]
    let activeThreadIDs: Set<String>
    let activeCount: Int
    var rowCount: Int { pinned.count + running.count + ordinary.count }
    var sectionCount: Int { [!pinned.isEmpty, !running.isEmpty, !ordinary.isEmpty].filter { $0 }.count }

    init(records: [CueRecentRecord], runningTasks: [CueRunningTask]) {
        var latestByThread: [String: CueRunningTask] = [:]
        for task in runningTasks {
            if let previous = latestByThread[task.threadID], previous.updatedDate >= task.updatedDate { continue }
            latestByThread[task.threadID] = task
        }
        let active = latestByThread.values.sorted {
            if $0.updatedDate != $1.updatedDate { return $0.updatedDate > $1.updatedDate }
            return $0.threadID < $1.threadID
        }
        let activeIDs = Set(active.map(\.threadID))
        activeThreadIDs = activeIDs; activeCount = active.count
        pinned = records.filter(\.isPinned)
        let pinnedThreadIDs = Set(pinned.filter { $0.event.provider == "codex" }.map { $0.event.threadID })
        running = active.filter { !pinnedThreadIDs.contains($0.threadID) }
        ordinary = records.filter { !$0.isPinned && !($0.event.provider == "codex" && activeIDs.contains($0.event.threadID)) }
    }
}

struct CueRecentRecord {
    let event: CueEvent
    var openedAt: Date?
    var resolved = false
    var manualPinned = false
    var isPinned: Bool { manualPinned || !isOpened }
    var isOpened: Bool { openedAt != nil }
    var canOpen: Bool {
        event.provider == "codex" && !event.threadID.isEmpty &&
        event.threadID.unicodeScalars.allSatisfy { CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_")).contains($0) }
    }
}

/// This is an opening ledger, not an assertion that a chat was read or handled.
/// Entries retain their own state when another event arrives for the same chat.
final class CueRecentHistory {
    static let capacity = 50
    let file: URL
    private(set) var records: [CueRecentRecord] = []
    private(set) var lastError = ""
    var events: [CueEvent] { records.map(\.event) }
    var unopenedCount: Int { records.filter { !$0.isOpened && $0.canOpen }.count }
    var pinnedCount: Int { records.filter(\.isPinned).count }
    var ordinaryCount: Int { records.filter { !$0.isPinned }.count }

    init(home: URL) {
        file = home.appendingPathComponent("recent-alerts.json")
        load()
    }

    @discardableResult func append(_ event: CueEvent) -> Bool {
        guard event.kind != "running", !event.isPreview, !records.contains(where: { $0.event.id == event.id }) else { return false }
        records.insert(CueRecentRecord(event: event), at: 0)
        normalize()
        save(); return true
    }

    @discardableResult func markOpened(id: String, didOpen: Bool) -> Bool {
        guard didOpen, let index = records.firstIndex(where: { $0.event.id == id }), records[index].canOpen else { return false }
        records[index].openedAt = Date(); normalize(); save(); return true
    }

    @discardableResult func markUnread(id: String) -> Bool {
        guard let index = records.firstIndex(where: { $0.event.id == id }) else { return false }
        records[index].openedAt = nil; normalize(); save(); return true
    }

    @discardableResult func setPinned(id: String, pinned: Bool) -> Bool {
        guard let index = records.firstIndex(where: { $0.event.id == id }) else { return false }
        records[index].manualPinned = pinned; normalize(); save(); return true
    }

    private func normalize() {
        // Unread alerts are always pinned, then explicitly pinned read alerts,
        // then ordinary history. Pinned alerts never consume ordinary slots.
        func group(_ record: CueRecentRecord) -> Int { !record.isOpened ? 0 : record.manualPinned ? 1 : 2 }
        let sorted = records.enumerated().sorted {
            let a = $0.element, b = $1.element
            if group(a) != group(b) { return group(a) < group(b) }
            let aDate = group(a) == 2 ? (a.openedAt ?? a.event.date) : a.event.date
            let bDate = group(b) == 2 ? (b.openedAt ?? b.event.date) : b.event.date
            if aDate != bDate { return aDate > bDate }
            return $0.offset < $1.offset
        }.map(\.element)
        var ordinary = 0
        records = sorted.filter { record in
            if record.isPinned { return true }
            ordinary += 1; return ordinary <= Self.capacity
        }
    }

    @discardableResult func resolve(id: String) -> Bool {
        guard let index = records.firstIndex(where: { $0.event.id == id }) else { return false }
        records[index].resolved = true; save(); return true
    }

    func remove(id: String) {
        records.removeAll { $0.event.id == id }; save()
    }

    private func eventJSON(_ event: CueEvent) -> [String: Any] {
        ["id": event.id, "kind": event.kind, "title": event.title, "thread_id": event.threadID,
         "message": event.message, "created_at": event.date.timeIntervalSince1970, "provider": event.provider,
         "source": event.source, "requires_user_action": event.requiresUserAction]
    }

    private func save() {
        let rows = records.map { record -> [String: Any] in
            var row: [String: Any] = ["event": eventJSON(record.event), "resolved": record.resolved, "manual_pinned": record.manualPinned]
            if let date = record.openedAt { row["opened_at"] = date.timeIntervalSince1970 }
            return row
        }
        do {
            try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            let data = try JSONSerialization.data(withJSONObject: ["version": 2, "records": rows], options: [.sortedKeys])
            try data.write(to: file, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
            lastError = ""
        } catch { lastError = "最近提醒未能保存，重启后可能无法恢复。" }
    }

    private func load() {
        guard FileManager.default.fileExists(atPath: file.path) else { return }
        do {
            let data = try Data(contentsOf: file)
            guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  [1, 2].contains(json["version"] as? Int ?? 0), let rows = json["records"] as? [[String: Any]] else {
                throw CocoaError(.fileReadCorruptFile)
            }
            var ids = Set<String>()
            records = rows.compactMap { row -> CueRecentRecord? in
                guard let raw = row["event"] as? [String: Any],
                      let data = try? JSONSerialization.data(withJSONObject: raw), let event = CueEvent(data),
                      !event.isPreview, ids.insert(event.id).inserted else { return nil }
                let opened = (row["opened_at"] as? Double).flatMap { $0.isFinite && $0 > 0 ? Date(timeIntervalSince1970: $0) : nil }
                return CueRecentRecord(event: event, openedAt: opened, resolved: row["resolved"] as? Bool ?? false, manualPinned: row["manual_pinned"] as? Bool ?? false)
            }
            normalize()
            lastError = ""
        } catch { lastError = "最近提醒记录暂时无法读取。" }
    }

    static func validationReport() -> [String: Bool] {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("vibecue-recent-" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let history = CueRecentHistory(home: root)
        func event(_ id: Int, thread: String = "chat-one") -> CueEvent {
            let raw: [String: Any] = ["id": "event-\(id)", "kind": "complete", "title": "测试对话 \(id)", "thread_id": thread,
                                      "source": "fixture", "created_at": Double(1000 + id)]
            return CueEvent(try! JSONSerialization.data(withJSONObject: raw))!
        }
        var results: [String: Bool] = [:]
        for id in 0..<55 { history.append(event(id)); history.markOpened(id: "event-\(id)", didOpen: true) }
        results["ordinary_history_keeps_50_in_date_order"] = history.ordinaryCount == 50 && history.records.first?.event.id == "event-54" && history.records.last?.event.id == "event-5"
        let count = history.records.count
        history.append(event(54)); history.append(CueEvent(kind: "complete", title: "预览", message: ""))
        results["duplicates_and_previews_do_not_enter_history"] = history.records.count == count
        history.markUnread(id: "event-5")
        results["unread_always_sorts_before_newer_read_history"] = history.records.first?.event.id == "event-5" && history.records.first?.isPinned == true
        results["failed_open_never_marks_event"] = !history.markOpened(id: "event-5", didOpen: false) && history.unopenedCount == 1
        history.append(event(55))
        results["new_event_same_chat_remains_unread_and_pinned"] = history.records.first?.event.id == "event-55" && history.records.first?.isOpened == false && history.records.first?.isPinned == true
        history.markOpened(id: "event-55", didOpen: true)
        results["opening_releases_only_automatic_pin"] = history.records.first(where: { $0.event.id == "event-55" })?.isPinned == false && history.records.first?.event.id == "event-5"
        history.setPinned(id: "event-53", pinned: true); history.markOpened(id: "event-53", didOpen: true)
        history.resolve(id: "event-5")
        let restored = CueRecentHistory(home: root)
        results["manual_pin_and_opening_survive_restart"] = restored.records.first(where: { $0.event.id == "event-53" })?.manualPinned == true && restored.records.first(where: { $0.event.id == "event-53" })?.isOpened == true
        results["resolution_does_not_mark_unread_opened"] = restored.records.first(where: { $0.event.id == "event-5" })?.resolved == true && restored.records.first(where: { $0.event.id == "event-5" })?.isOpened == false
        restored.markUnread(id: "event-53")
        results["mark_unread_restores_unread_state"] = restored.records.first(where: { $0.event.id == "event-53" })?.openedAt == nil && restored.records.first(where: { $0.event.id == "event-53" })?.isPinned == true
        restored.setPinned(id: "event-53", pinned: false)
        results["unread_cannot_lose_automatic_pin"] = restored.records.first(where: { $0.event.id == "event-53" })?.isPinned == true
        let large = CueRecentHistory(home: root.appendingPathComponent("large"))
        for id in 100..<180 { large.append(event(id)) }
        for id in 200..<260 { large.append(event(id)); large.markOpened(id: "event-\(id)", didOpen: true) }
        results["pinned_records_do_not_consume_50_ordinary_slots"] = large.pinnedCount == 80 && large.ordinaryCount == 50 && large.records.count == 130
        let largeRestored = CueRecentHistory(home: root.appendingPathComponent("large"))
        results["all_pinned_records_survive_restart_without_total_cap"] = largeRestored.records.count == 130 && largeRestored.pinnedCount == 80 && largeRestored.ordinaryCount == 50
        largeRestored.remove(id: "event-150")
        results["deletion_removes_only_target_event"] = largeRestored.records.count == 129 && !largeRestored.records.contains(where: { $0.event.id == "event-150" }) && largeRestored.records.contains(where: { $0.event.id == "event-151" })
        let oldInbox = CueRecentHistory(home: root.appendingPathComponent("old-unread"))
        oldInbox.append(event(1))
        for id in 300..<350 { oldInbox.append(event(id)); oldInbox.markOpened(id: "event-\(id)", didOpen: true) }
        oldInbox.markOpened(id: "event-1", didOpen: true)
        results["opening_old_unread_keeps_it_in_recent_read_history"] = oldInbox.records.first?.event.id == "event-1" && oldInbox.records.count == 50 && oldInbox.ordinaryCount == 50
        oldInbox.markUnread(id: "event-1")
        results["old_opened_alert_can_be_marked_unread_again"] = oldInbox.records.first?.event.id == "event-1" && oldInbox.records.first?.isOpened == false && oldInbox.records.first?.isPinned == true
        let legacyHome = root.appendingPathComponent("legacy")
        try? FileManager.default.createDirectory(at: legacyHome, withIntermediateDirectories: true)
        let legacyRows: [[String: Any]] = [["event": history.eventJSON(event(700)), "opened_at": 2000.0], ["event": history.eventJSON(event(600))]]
        try? JSONSerialization.data(withJSONObject: ["version": 1, "records": legacyRows]).write(to: legacyHome.appendingPathComponent("recent-alerts.json"))
        let legacy = CueRecentHistory(home: legacyHome)
        results["version_1_migration_preserves_open_state_and_pins_unread"] = legacy.records.count == 2 && legacy.records.first?.event.id == "event-600" && legacy.records.first?.isPinned == true && legacy.records.last?.isOpened == true
        let oversizedHome = root.appendingPathComponent("large-file")
        try? FileManager.default.createDirectory(at: oversizedHome, withIntermediateDirectories: true)
        let oversizedRows: [[String: Any]] = (4000..<10000).map { ["event": history.eventJSON(event($0)), "resolved": false, "manual_pinned": false] }
        let oversizedData = try! JSONSerialization.data(withJSONObject: ["version": 2, "records": oversizedRows])
        try? oversizedData.write(to: oversizedHome.appendingPathComponent("recent-alerts.json"))
        let oversizedHistory = CueRecentHistory(home: oversizedHome)
        results["pinned_ledger_larger_than_1mib_is_not_truncated"] = oversizedData.count > 1_048_576 && oversizedHistory.records.count == 6000 && oversizedHistory.pinnedCount == 6000 && oversizedHistory.lastError.isEmpty
        let permissions = try? FileManager.default.attributesOfItem(atPath: history.file.path)[.posixPermissions] as? NSNumber
        results["history_file_is_private"] = permissions?.intValue == 0o600
        let invalid = event(90, thread: "bad/path")
        restored.append(invalid)
        results["invalid_chat_cannot_be_marked_open"] = !restored.markOpened(id: invalid.id, didOpen: true)
        let blocked = root.appendingPathComponent("blocked")
        try? Data("file".utf8).write(to: blocked)
        let failed = CueRecentHistory(home: blocked); failed.append(event(100))
        results["write_error_is_reported"] = !failed.lastError.isEmpty
        let corruptHome = root.appendingPathComponent("corrupt")
        try? FileManager.default.createDirectory(at: corruptHome, withIntermediateDirectories: true)
        try? Data("not json".utf8).write(to: corruptHome.appendingPathComponent("recent-alerts.json"))
        let corrupt = CueRecentHistory(home: corruptHome)
        results["corrupt_history_does_not_invent_events"] = corrupt.records.isEmpty && !corrupt.lastError.isEmpty
        results.merge(CueStatusPopoverController.validationReport()) { _, next in next }
        return results
    }
}

struct CueStatusPopoverActions {
    let openSettings: () -> Void
    let openUsage: () -> Void
    let toggleEnabled: () -> Void
    let toggleSound: () -> Void
    let pause: () -> Void
    let demo: () -> Void
    let quit: () -> Void
    let openEvent: (CueEvent) -> Void
    var historyChanged: () -> Void = {}
}

private final class CueRecentRowButton: NSButton {
    let record: CueRecentRecord
    let isRunning: Bool
    var selected = false { didSet { needsDisplay = true } }
    var selectRow: (() -> Void)?
    var contextMenu: (() -> NSMenu)?
    private var hovered = false
    private var tracking: NSTrackingArea?
    init(record: CueRecentRecord, width: CGFloat, isRunning: Bool = false) {
        self.record = record; self.isRunning = isRunning
        super.init(frame: NSRect(x: 8, y: 0, width: width - 16, height: 54))
        title = ""; isBordered = false; setButtonType(.momentaryPushIn); focusRingType = .exterior
        isEnabled = true
        let tint: NSColor = record.isOpened ? .secondaryLabelColor : .systemBlue
        let icon = NSImageView(frame: NSRect(x: 11, y: 16, width: 20, height: 20))
        icon.image = NSImage(systemSymbolName: record.isOpened ? (record.manualPinned ? "pin.fill" : "arrow.up.forward.app") : "circle.fill", accessibilityDescription: nil)
        icon.contentTintColor = tint; addSubview(icon)
        let heading = NSTextField(labelWithString: record.event.title)
        heading.frame = NSRect(x: 42, y: 6, width: frame.width - 168, height: 21)
        heading.font = .systemFont(ofSize: 13, weight: record.isOpened ? .regular : .semibold)
        heading.textColor = record.isOpened ? .secondaryLabelColor : .labelColor
        heading.lineBreakMode = .byTruncatingTail; addSubview(heading)
        let state = record.isOpened ? "已从 VibeCue 打开" : record.canOpen ? "未读" : "无可用对话入口"
        let kind = record.resolved ? "人工批准已解除" : record.event.label
        let date = DateFormatter(); date.locale = Locale(identifier: "zh_CN"); date.dateFormat = Calendar.current.isDateInToday(record.event.date) ? "HH:mm" : "MM-dd HH:mm"
        let metadata = NSTextField(labelWithString: "\(isRunning ? "上次提醒：" : "")\(kind) · \(date.string(from: record.event.date))\(record.isPinned ? " · 置顶" : "")")
        metadata.frame = NSRect(x: 42, y: 29, width: frame.width - 56, height: 17)
        metadata.font = .systemFont(ofSize: 10.5); metadata.textColor = .secondaryLabelColor; metadata.lineBreakMode = .byTruncatingTail; addSubview(metadata)
        let stateLabel = NSTextField(labelWithString: isRunning ? (record.isOpened ? "运行中 · 已打开" : "运行中 · 未读") : record.isOpened ? (record.manualPinned ? "已打开 · 置顶" : "已打开") : record.canOpen ? "未读 · 置顶" : "不可跳转")
        stateLabel.frame = NSRect(x: frame.width - 104, y: 8, width: 89, height: 18)
        stateLabel.alignment = .right; stateLabel.font = .systemFont(ofSize: 10.5, weight: .medium); stateLabel.textColor = isRunning ? .systemGreen : tint; addSubview(stateLabel)
        toolTip = "\(record.event.title)\n\(kind) · \(state)\(isRunning ? " · 当前正在运行" : "")"
        setAccessibilityLabel("\(isRunning ? "当前正在运行，" : "")\(state)，\(record.isPinned ? "已置顶，" : "")\(kind)，\(record.event.title)，\(date.string(from: record.event.date))")
        setAccessibilityHelp("点击打开对话；右键可标为未读、置顶或从列表删除。")
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override var isFlipped: Bool { true }
    override func hitTest(_ point: NSPoint) -> NSView? {
        // The labels and symbol are part of one row action, so clicking text
        // has the same effect as clicking its surrounding highlight.
        super.hitTest(point) == nil ? nil : self
    }
    override func mouseDown(with event: NSEvent) {
        if event.modifierFlags.contains(.control) { rightMouseDown(with: event); return }
        selectRow?(); super.mouseDown(with: event)
    }
    override func rightMouseDown(with event: NSEvent) {
        selectRow?()
        if let menu = contextMenu?() { NSMenu.popUpContextMenu(menu, with: event, for: self) }
    }
    override func menu(for event: NSEvent) -> NSMenu? {
        selectRow?(); return contextMenu?()
    }
    override func updateTrackingAreas() {
        super.updateTrackingAreas(); if let tracking { removeTrackingArea(tracking) }
        let next = NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeInActiveApp, .inVisibleRect], owner: self)
        addTrackingArea(next); tracking = next
    }
    override func mouseEntered(with event: NSEvent) { hovered = true; needsDisplay = true }
    override func mouseExited(with event: NSEvent) { hovered = false; needsDisplay = true }
    override func draw(_ dirtyRect: NSRect) {
        let tint = record.isOpened ? NSColor.secondaryLabelColor : NSColor.systemBlue
        tint.withAlphaComponent(isHighlighted || selected ? 0.20 : hovered ? 0.13 : record.isOpened ? 0.025 : 0.075).setFill()
        NSBezierPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), xRadius: 8, yRadius: 8).fill()
        if selected {
            NSColor.controlAccentColor.withAlphaComponent(0.7).setStroke()
            NSBezierPath(roundedRect: bounds.insetBy(dx: 1.5, dy: 1.5), xRadius: 8, yRadius: 8).stroke()
        }
        super.draw(dirtyRect)
    }
}

private final class CueRunningRowButton: NSButton {
    let task: CueRunningTask
    var selected = false { didSet { needsDisplay = true } }
    var selectRow: (() -> Void)?
    private var hovered = false
    private var tracking: NSTrackingArea?
    init(task: CueRunningTask, width: CGFloat) {
        self.task = task
        super.init(frame: NSRect(x: 8, y: 0, width: width - 16, height: 54))
        title = ""; isBordered = false; setButtonType(.momentaryPushIn); focusRingType = .exterior
        let icon = NSImageView(frame: NSRect(x: 11, y: 16, width: 20, height: 20))
        icon.image = NSImage(systemSymbolName: "bolt.horizontal.circle", accessibilityDescription: nil)
        icon.contentTintColor = .systemGreen; addSubview(icon)
        let heading = NSTextField(labelWithString: task.title)
        heading.frame = NSRect(x: 42, y: 6, width: frame.width - 140, height: 21)
        heading.font = .systemFont(ofSize: 13, weight: .medium); heading.lineBreakMode = .byTruncatingTail; addSubview(heading)
        let date = DateFormatter(); date.locale = Locale(identifier: "zh_CN")
        date.dateFormat = Calendar.current.isDateInToday(task.startDate) ? "HH:mm" : "MM-dd HH:mm"
        let start = date.string(from: task.startDate)
        date.dateFormat = "HH:mm"
        let metadata = NSTextField(labelWithString: "Codex · 开始 \(start) · 最近活动 \(date.string(from: task.updatedDate))")
        metadata.frame = NSRect(x: 42, y: 29, width: frame.width - 56, height: 17)
        metadata.font = .systemFont(ofSize: 10.5); metadata.textColor = .secondaryLabelColor
        metadata.lineBreakMode = .byTruncatingTail; addSubview(metadata)
        let state = NSTextField(labelWithString: "● 运行中")
        state.frame = NSRect(x: frame.width - 86, y: 8, width: 71, height: 18)
        state.alignment = .right; state.font = .systemFont(ofSize: 10.5, weight: .medium); state.textColor = .systemGreen; addSubview(state)
        identifier = NSUserInterfaceItemIdentifier(task.navigationID)
        toolTip = "\(task.title)\n正在运行 · 点击打开 Codex 对话"
        setAccessibilityLabel("正在运行，\(task.title)，开始于 \(start)")
        setAccessibilityHelp("点击打开正在运行的 Codex 对话。")
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override var isFlipped: Bool { true }
    override func hitTest(_ point: NSPoint) -> NSView? { super.hitTest(point) == nil ? nil : self }
    override func mouseDown(with event: NSEvent) {
        if event.modifierFlags.contains(.control) { rightMouseDown(with: event); return }
        selectRow?(); super.mouseDown(with: event)
    }
    override func rightMouseDown(with event: NSEvent) { selectRow?() }
    override func updateTrackingAreas() {
        super.updateTrackingAreas(); if let tracking { removeTrackingArea(tracking) }
        let next = NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeInActiveApp, .inVisibleRect], owner: self)
        addTrackingArea(next); tracking = next
    }
    override func mouseEntered(with event: NSEvent) { hovered = true; needsDisplay = true }
    override func mouseExited(with event: NSEvent) { hovered = false; needsDisplay = true }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.systemGreen.withAlphaComponent(isHighlighted || selected ? 0.16 : hovered ? 0.10 : 0.045).setFill()
        NSBezierPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), xRadius: 8, yRadius: 8).fill()
        if selected {
            NSColor.controlAccentColor.withAlphaComponent(0.7).setStroke()
            NSBezierPath(roundedRect: bounds.insetBy(dx: 1.5, dy: 1.5), xRadius: 8, yRadius: 8).stroke()
        }
        super.draw(dirtyRect)
    }
}

final class CueStatusPopoverController: NSViewController {
    let history: CueRecentHistory
    let actions: CueStatusPopoverActions
    private let scroll = NSScrollView()
    private var enabled = true, soundEnabled = true, paused = false
    private var status = "本机监控中"
    private var maximumHeight: CGFloat = 900
    private let panelWidth: CGFloat = 580
    private var recentDocument: CueFlippedView?
    private var selectedID: String?
    private var runningTasks: [CueRunningTask] = []

    init(history: CueRecentHistory, actions: CueStatusPopoverActions) {
        self.history = history; self.actions = actions
        super.init(nibName: nil, bundle: nil)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func loadView() {
        let root = CueFlippedView(frame: NSRect(x: 0, y: 0, width: panelWidth, height: 660))
        root.backgroundColor = .windowBackgroundColor; view = root
        rebuild()
    }
    func update(status: String, enabled: Bool, soundEnabled: Bool, paused: Bool, maximumHeight: CGFloat = 900, runningTasks: [CueRunningTask] = []) {
        self.status = status; self.enabled = enabled; self.soundEnabled = soundEnabled
        self.paused = paused; self.maximumHeight = maximumHeight; self.runningTasks = runningTasks
        if isViewLoaded { rebuild() }
    }
    private func label(_ text: String, _ rect: NSRect, size: CGFloat = 12, color: NSColor = .labelColor, weight: NSFont.Weight = .regular) {
        let label = NSTextField(labelWithString: text); label.frame = rect
        label.font = .systemFont(ofSize: size, weight: weight); label.textColor = color
        label.lineBreakMode = .byTruncatingTail; view.addSubview(label)
    }
    private func button(_ title: String, _ rect: NSRect, _ selector: Selector) {
        let button = NSButton(title: title, target: self, action: selector)
        button.bezelStyle = .rounded; button.frame = rect; view.addSubview(button)
    }
    private func rebuild() {
        let origin = scroll.contentView.bounds.origin
        let snapshot = CueStatusListSnapshot(records: history.records, runningTasks: runningTasks)
        view.subviews.forEach { $0.removeFromSuperview() }
        let fixedHeight: CGFloat = 219
        let maxHeight = max(390, min(900, maximumHeight))
        let capacity = max(3, min(12, Int((maxHeight - fixedHeight) / 56)))
        let contentHeight = CGFloat(snapshot.rowCount) * 56 + CGFloat(snapshot.sectionCount) * 28
        let viewport = min(CGFloat(capacity) * 56, max(168, contentHeight))
        let height = fixedHeight + viewport
        view.frame.size = NSSize(width: panelWidth, height: height)
        preferredContentSize = view.frame.size
        label("VibeCue", NSRect(x: 22, y: 16, width: 260, height: 27), size: 18, weight: .semibold)
        label(status, NSRect(x: 22, y: 47, width: 535, height: 19), size: 11, color: .secondaryLabelColor)
        button("打开面板", NSRect(x: 18, y: 76, width: 139, height: 30), #selector(openSettings))
        button("用量统计", NSRect(x: 164, y: 76, width: 139, height: 30), #selector(openUsage))
        button("测试提醒", NSRect(x: 310, y: 76, width: 119, height: 30), #selector(demo))
        button("结束", NSRect(x: 436, y: 76, width: 119, height: 30), #selector(quit))
        let enable = NSButton(checkboxWithTitle: "启用提醒", target: self, action: #selector(toggleEnabled))
        enable.frame = NSRect(x: 22, y: 119, width: 123, height: 25); enable.state = enabled ? .on : .off; view.addSubview(enable)
        let sound = NSButton(checkboxWithTitle: "播放提示音", target: self, action: #selector(toggleSound))
        sound.frame = NSRect(x: 158, y: 119, width: 143, height: 25); sound.state = soundEnabled ? .on : .off; view.addSubview(sound)
        button(paused ? "恢复提醒" : "暂停 30 分钟", NSRect(x: 351, y: 116, width: 204, height: 30), #selector(pause))
        label("任务与提醒", NSRect(x: 22, y: 163, width: 130, height: 23), size: 13, weight: .semibold)
        label("\(history.unopenedCount) 条未读 · \(snapshot.activeCount) 项运行中 · 历史 \(history.ordinaryCount) / 50", NSRect(x: 173, y: 165, width: 381, height: 20), size: 11, color: history.unopenedCount > 0 ? .systemBlue : .secondaryLabelColor)
        scroll.frame = NSRect(x: 10, y: 193, width: panelWidth - 20, height: viewport)
        scroll.drawsBackground = false; scroll.hasVerticalScroller = true; scroll.scrollerStyle = .legacy
        scroll.borderType = .noBorder; scroll.autohidesScrollers = false
        let document = CueFlippedView(frame: NSRect(x: 0, y: 0, width: panelWidth - 35, height: max(viewport, contentHeight)))
        var position: CGFloat = 0
        func section(_ title: String, id: String, count: Int, hint: String, color: NSColor = .secondaryLabelColor) {
            let heading = NSTextField(labelWithString: "\(title) · \(count)")
            heading.identifier = NSUserInterfaceItemIdentifier("section-" + id)
            heading.frame = NSRect(x: 19, y: position + 5, width: 260, height: 19)
            heading.font = .systemFont(ofSize: 11, weight: .semibold); heading.textColor = color
            document.addSubview(heading)
            let detail = NSTextField(labelWithString: hint)
            detail.frame = NSRect(x: 290, y: position + 6, width: document.frame.width - 311, height: 17)
            detail.alignment = .right; detail.font = .systemFont(ofSize: 10); detail.textColor = .tertiaryLabelColor
            detail.lineBreakMode = .byTruncatingTail; document.addSubview(detail)
            position += 28
        }
        func reminderRow(_ record: CueRecentRecord) {
            let isRunning = record.event.provider == "codex" && snapshot.activeThreadIDs.contains(record.event.threadID)
            let row = CueRecentRowButton(record: record, width: document.frame.width, isRunning: isRunning)
            row.frame.origin.y = position; position += 56
            row.target = self; row.action = #selector(openRow(_:))
            let id = record.event.id
            row.identifier = NSUserInterfaceItemIdentifier(id); row.selected = selectedID == id
            row.setAccessibilityValue(row.selected ? "已选中" : "")
            row.selectRow = { [weak self] in self?.select(id: id) }
            row.contextMenu = { [weak self] in self?.contextMenu(id: id) ?? NSMenu() }
            document.addSubview(row)
        }
        if !snapshot.pinned.isEmpty {
            section("置顶提醒", id: "pinned", count: snapshot.pinned.count, hint: "未读优先 · 不占历史额度", color: .systemBlue)
            snapshot.pinned.forEach(reminderRow)
        }
        if !snapshot.running.isEmpty {
            section("正在运行", id: "running", count: snapshot.running.count, hint: "实时任务 · 点击打开对话", color: .systemGreen)
            for task in snapshot.running {
                let row = CueRunningRowButton(task: task, width: document.frame.width)
                row.frame.origin.y = position; position += 56
                row.target = self; row.action = #selector(openRunningRow(_:)); row.selected = selectedID == task.navigationID
                row.setAccessibilityValue(row.selected ? "已选中" : "")
                row.selectRow = { [weak self] in self?.select(id: task.navigationID) }
                document.addSubview(row)
            }
        }
        if !snapshot.ordinary.isEmpty {
            section("历史提醒", id: "history", count: snapshot.ordinary.count, hint: "已打开 · 右键可重新标为未读")
            snapshot.ordinary.forEach(reminderRow)
        }
        if snapshot.rowCount == 0 {
            let empty = NSTextField(wrappingLabelWithString: "正在运行的任务与新的提醒会显示在这里。\n未读提醒自动置顶；右键可重新标为未读、置顶或删除。")
            empty.frame = NSRect(x: 24, y: 41, width: 490, height: 62)
            empty.font = .systemFont(ofSize: 12); empty.textColor = .secondaryLabelColor; document.addSubview(empty)
        }
        recentDocument = document; scroll.documentView = document; view.addSubview(scroll)
        scroll.contentView.scroll(to: scroll.contentView.constrainBoundsRect(NSRect(origin: origin, size: scroll.contentView.bounds.size)).origin)
        scroll.reflectScrolledClipView(scroll.contentView)
        label(history.lastError.isEmpty ? "未读自动置顶；置顶与运行任务不占 50 条历史额度。右键可管理提醒。" : history.lastError,
              NSRect(x: 22, y: height - 20, width: 535, height: 16), size: 10, color: history.lastError.isEmpty ? .tertiaryLabelColor : .systemOrange)
    }
    static func validationReport() -> [String: Bool] {
        _ = NSApplication.shared
        let home = FileManager.default.temporaryDirectory.appendingPathComponent("vibecue-context-" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        let history = CueRecentHistory(home: home)
        for id in ["one", "two"] {
            let raw: [String: Any] = ["id": id, "kind": "complete", "title": "测试 \(id)", "thread_id": id, "created_at": Date().timeIntervalSince1970]
            history.append(CueEvent(try! JSONSerialization.data(withJSONObject: raw))!)
            history.markOpened(id: id, didOpen: true)
        }
        var openCalls = 0, refreshCalls = 0
        var actions = CueStatusPopoverActions(openSettings: {}, openUsage: {}, toggleEnabled: {}, toggleSound: {}, pause: {}, demo: {}, quit: {}, openEvent: { _ in openCalls += 1 })
        actions.historyChanged = { refreshCalls += 1 }
        let controller = CueStatusPopoverController(history: history, actions: actions)
        _ = controller.view
        controller.select(id: "two")
        let menu = controller.contextMenu(id: "two")
        var results = ["context_selection_does_not_open_chat": openCalls == 0 && controller.selectedID == "two",
                       "context_menu_contains_requested_actions": menu.items.contains(where: { $0.title == "标为未读" }) && menu.items.contains(where: { $0.title == "置顶" }) && menu.items.contains(where: { $0.title == "删除" })]
        controller.select(id: "one")
        if let item = menu.items.first(where: { $0.title == "标为未读" }) { controller.markUnread(item) }
        results["context_unread_targets_original_row"] = history.records.first(where: { $0.event.id == "two" })?.isOpened == false && history.records.first(where: { $0.event.id == "one" })?.isOpened == true
        if let item = menu.items.first(where: { $0.title == "置顶" }) { controller.togglePin(item) }
        results["context_pin_targets_original_row"] = history.records.first(where: { $0.event.id == "two" })?.manualPinned == true && history.records.first(where: { $0.event.id == "one" })?.manualPinned == false
        if let item = menu.items.first(where: { $0.title == "删除" }) { controller.deleteRow(item) }
        results["context_delete_targets_original_row"] = !history.records.contains(where: { $0.event.id == "two" }) && history.records.contains(where: { $0.event.id == "one" }) && refreshCalls == 3 && openCalls == 0
        let liveHistory = CueRecentHistory(home: home.appendingPathComponent("running"))
        func alert(_ id: String, thread: String, opened: Bool = false, pin: Bool = false) {
            let raw: [String: Any] = ["id": id, "kind": "complete", "title": "提醒 \(id)", "thread_id": thread, "created_at": 1000.0]
            liveHistory.append(CueEvent(try! JSONSerialization.data(withJSONObject: raw))!)
            if opened { liveHistory.markOpened(id: id, didOpen: true) }
            if pin { liveHistory.setPinned(id: id, pinned: true) }
        }
        func running(_ thread: String, time: Double = 2000, title: String = "正在执行") -> CueRunningTask {
            CueRunningTask(raw: ["thread_id": thread, "title": title, "provider": "codex", "started_at": 1000.0, "last_activity": time, "turn_id": "turn-" + thread])!
        }
        alert("pin-unread", thread: "shared-pinned")
        alert("pin-read", thread: "manual-pinned", opened: true, pin: true)
        alert("ordinary-active", thread: "active-thread", opened: true)
        alert("ordinary-finished", thread: "finished-thread", opened: true)
        let tasks = [running("shared-pinned", time: 2003), running("active-thread", time: 2002, title: "新活动"), running("only-running", time: 2001), running("active-thread", time: 2000, title: "旧活动")]
        let snapshot = CueStatusListSnapshot(records: liveHistory.records, runningTasks: tasks)
        results["running_parse_rejects_invalid_navigation_targets"] = CueRunningTask(raw: ["thread_id": "bad/path", "started_at": 1.0, "last_activity": 2.0]) == nil && CueRunningTask(raw: ["thread_id": "valid", "provider": "other", "started_at": 1.0, "last_activity": 2.0]) == nil && CueRunningTask(raw: ["thread_id": "valid", "started_at": Double.infinity, "last_activity": 2.0]) == nil
        results["running_parse_accepts_iso8601_and_turn_id"] = CueRunningTask(raw: ["thread_id": "iso-task", "started_at": "2026-10-28T00:00:00Z", "last_activity": "2026-10-28T00:01:00.123Z", "turn_id": "turn-iso"])?.turnID == "turn-iso"
        results["running_deduplicates_threads_using_latest_activity"] = snapshot.activeCount == 3 && snapshot.running.first?.title == "新活动"
        results["running_pinned_chat_uses_badge_without_duplicate_live_row"] = snapshot.pinned.count == 2 && snapshot.activeThreadIDs.contains("shared-pinned") && !snapshot.running.contains(where: { $0.threadID == "shared-pinned" })
        results["running_temporarily_hides_ordinary_alert_without_deleting_ledger"] = snapshot.ordinary.map { $0.event.id } == ["ordinary-finished"] && liveHistory.records.count == 4 && liveHistory.ordinaryCount == 2
        let ledgerBefore = try? Data(contentsOf: liveHistory.file)
        var navigationEvents: [CueEvent] = []
        let liveActions = CueStatusPopoverActions(openSettings: {}, openUsage: {}, toggleEnabled: {}, toggleSound: {}, pause: {}, demo: {}, quit: {}, openEvent: { navigationEvents.append($0) })
        let liveController = CueStatusPopoverController(history: liveHistory, actions: liveActions)
        liveController.update(status: "测试运行状态", enabled: true, soundEnabled: true, paused: false, runningTasks: tasks)
        _ = liveController.view
        let sectionIDs = liveController.recentDocument?.subviews.compactMap(\.identifier).map(\.rawValue).filter { $0.hasPrefix("section-") }
        results["running_sections_keep_pinned_running_history_priority"] = sectionIDs == ["section-pinned", "section-running", "section-history"]
        let pinnedRow = liveController.recentDocument?.subviews.compactMap { $0 as? CueRecentRowButton }.first { $0.record.event.id == "pin-unread" }
        results["running_badge_is_attached_to_existing_pinned_alert"] = pinnedRow?.isRunning == true
        let runningRow = liveController.recentDocument?.subviews.compactMap { $0 as? CueRunningRowButton }.first { $0.task.threadID == "active-thread" }
        if let row = runningRow { liveController.openRunningRow(row) }
        results["running_click_only_navigates_and_never_marks_alert_opened"] = navigationEvents.count == 1 && navigationEvents.first?.threadID == "active-thread" && navigationEvents.first?.kind == "running" && navigationEvents.first?.requiresUserAction == false && (try? Data(contentsOf: liveHistory.file)) == ledgerBefore
        results["running_navigation_event_cannot_enter_alert_ledger"] = !liveHistory.append(tasks[0].ephemeralCueEvent) && liveHistory.records.count == 4
        liveController.select(id: "pin-unread")
        let liveMenu = liveController.contextMenu(id: "pin-unread")
        liveController.select(id: "running:active-thread")
        if let item = liveMenu.items.first(where: { $0.title.hasPrefix("置顶（") }) { liveController.togglePin(item) }
        results["running_rows_do_not_change_alert_context_menu_target"] = liveHistory.records.first(where: { $0.event.id == "pin-unread" })?.manualPinned == true && liveHistory.records.first(where: { $0.event.id == "ordinary-active" })?.manualPinned == false && navigationEvents.count == 1
        liveController.update(status: "测试运行状态", enabled: true, soundEnabled: true, paused: false, runningTasks: tasks)
        results["running_selection_survives_status_refresh"] = liveController.recentDocument?.subviews.compactMap { $0 as? CueRunningRowButton }.first(where: { $0.task.threadID == "active-thread" })?.selected == true
        liveController.update(status: "任务结束", enabled: true, soundEnabled: true, paused: false, runningTasks: [])
        results["running_end_restores_original_history_without_synthetic_alert"] = liveController.recentDocument?.subviews.compactMap { $0 as? CueRunningRowButton }.isEmpty == true && liveController.recentDocument?.subviews.compactMap { $0 as? CueRecentRowButton }.count == 4 && liveHistory.records.count == 4
        let manyTasks = (0..<70).map { running("many-\($0)", time: Double(3000 + $0)) }
        liveController.update(status: "多任务测试", enabled: true, soundEnabled: true, paused: false, maximumHeight: 900, runningTasks: manyTasks)
        liveController.scroll.contentView.scroll(to: NSPoint(x: 0, y: 200))
        liveController.update(status: "多任务测试", enabled: true, soundEnabled: true, paused: false, maximumHeight: 900, runningTasks: manyTasks)
        results["running_tasks_scroll_without_consuming_history_capacity"] = liveController.view.bounds.height <= 900 && liveController.scroll.frame.height <= 12 * 56 && liveController.recentDocument!.frame.height > liveController.scroll.frame.height && liveController.scroll.contentView.bounds.origin.y == 200 && liveHistory.records.count == 4 && liveHistory.ordinaryCount == 2
        return results
    }

    /// Isolated native screenshot fixture; no watcher or real chat is touched.
    static func captureFixture(to output: URL) -> Bool {
        _ = NSApplication.shared
        let home = FileManager.default.temporaryDirectory.appendingPathComponent("vibecue-inbox-capture-" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        let history = CueRecentHistory(home: home)
        let now = Date()
        func alert(_ id: String, title: String, thread: String, minutes: Double, opened: Bool = false, pin: Bool = false) {
            let raw: [String: Any] = ["id": id, "kind": id == "capture-unread" ? "input" : "complete", "title": title, "thread_id": thread, "created_at": now.addingTimeInterval(-minutes * 60).timeIntervalSince1970]
            guard let event = CueEvent(try! JSONSerialization.data(withJSONObject: raw)) else { return }
            history.append(event)
            if opened { history.markOpened(id: event.id, didOpen: true) }
            if pin { history.setPinned(id: event.id, pinned: true) }
        }
        alert("capture-unread", title: "VibeCue｜任务运行状态与提醒列表", thread: "pinned-running", minutes: 2)
        alert("capture-manual", title: "ALV｜发布验证与后续检查", thread: "pinned-read", minutes: 12, opened: true, pin: true)
        alert("capture-active-history", title: "NACP｜增量同步与恢复验证", thread: "active-lint", minutes: 20, opened: true)
        for number in 0..<8 {
            alert("capture-history-\(number)", title: ["KLS｜首页视觉与交互调整", "ALV｜模型用量记录核对", "VibeCue｜提示音与光效设置", "NACP｜备份状态检查"][number % 4], thread: "history-\(number)", minutes: Double(30 + number * 4), opened: true)
        }
        let tasks: [CueRunningTask] = [("pinned-running", "VibeCue｜任务运行状态与提醒列表", 6.0), ("active-lint", "NACP｜增量同步与恢复验证", 12.0), ("active-build", "ALV｜构建发布与验收检查", 4.0)].compactMap { thread, title, minutes in
            CueRunningTask(raw: ["thread_id": thread, "title": title, "provider": "codex", "started_at": now.addingTimeInterval(-minutes * 60).timeIntervalSince1970, "last_activity": now.timeIntervalSince1970, "turn_id": "fixture-" + thread])
        }
        let actions = CueStatusPopoverActions(openSettings: {}, openUsage: {}, toggleEnabled: {}, toggleSound: {}, pause: {}, demo: {}, quit: {}, openEvent: { _ in })
        let panel = CueStatusPopoverController(history: history, actions: actions)
        panel.update(status: "本机监控中 · 3 个对话正在运行", enabled: true, soundEnabled: true, paused: false, maximumHeight: 900, runningTasks: tasks)
        let view = panel.view
        let window = NSWindow(contentRect: view.bounds, styleMask: .borderless, backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: .darkAqua); window.contentView = view; window.displayIfNeeded()
        guard let representation = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return false }
        view.cacheDisplay(in: view.bounds, to: representation)
        guard let data = representation.representation(using: .png, properties: [:]) else { return false }
        do { try data.write(to: output); return true } catch { return false }
    }

    private func select(id: String) {
        selectedID = id
        for row in recentDocument?.subviews.compactMap({ $0 as? CueRecentRowButton }) ?? [] {
            row.selected = row.record.event.id == id
            row.setAccessibilityValue(row.selected ? "已选中" : "")
        }
        for row in recentDocument?.subviews.compactMap({ $0 as? CueRunningRowButton }) ?? [] {
            row.selected = row.task.navigationID == id
            row.setAccessibilityValue(row.selected ? "已选中" : "")
        }
    }
    private func contextMenu(id: String) -> NSMenu {
        let menu = NSMenu(); menu.autoenablesItems = false
        guard let record = history.records.first(where: { $0.event.id == id }) else { return menu }
        let unread = NSMenuItem(title: "标为未读", action: #selector(markUnread(_:)), keyEquivalent: "")
        unread.target = self; unread.representedObject = id; unread.isEnabled = record.isOpened
        menu.addItem(unread)
        let pinTitle = record.manualPinned ? (record.isOpened ? "取消置顶" : "取消手动置顶") : (record.isOpened ? "置顶" : "置顶（打开后仍保留）")
        let pin = NSMenuItem(title: pinTitle, action: #selector(togglePin(_:)), keyEquivalent: "")
        pin.target = self; pin.representedObject = id; pin.state = record.manualPinned ? .on : .off
        menu.addItem(pin)
        if !record.isOpened {
            let hint = NSMenuItem(title: "未读已自动置顶；手动置顶可在打开后继续保留", action: nil, keyEquivalent: "")
            hint.isEnabled = false; menu.addItem(hint)
        }
        menu.addItem(.separator())
        let remove = NSMenuItem(title: "删除", action: #selector(deleteRow(_:)), keyEquivalent: "")
        remove.target = self; remove.representedObject = id
        remove.toolTip = "仅从 VibeCue 提醒列表移除，不删除原对话。"
        menu.addItem(remove)
        return menu
    }
    private func historyDidChange() {
        if !history.records.contains(where: { $0.event.id == selectedID }) && !runningTasks.contains(where: { $0.navigationID == selectedID }) { selectedID = nil }
        rebuild(); actions.historyChanged()
    }
    @objc private func markUnread(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        history.markUnread(id: id); historyDidChange()
    }
    @objc private func togglePin(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String,
              let record = history.records.first(where: { $0.event.id == id }) else { return }
        history.setPinned(id: id, pinned: !record.manualPinned); historyDidChange()
    }
    @objc private func deleteRow(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        history.remove(id: id); historyDidChange()
    }
    @objc private func openRow(_ sender: NSButton) {
        guard let id = sender.identifier?.rawValue,
              let record = history.records.first(where: { $0.event.id == id }) else { return }
        select(id: id)
        if record.canOpen { actions.openEvent(record.event) }
    }
    @objc private func openRunningRow(_ sender: CueRunningRowButton) {
        select(id: sender.task.navigationID)
        actions.openEvent(sender.task.ephemeralCueEvent)
    }
    @objc private func openSettings() { actions.openSettings() }
    @objc private func openUsage() { actions.openUsage() }
    @objc private func toggleEnabled() { actions.toggleEnabled() }
    @objc private func toggleSound() { actions.toggleSound() }
    @objc private func pause() { actions.pause() }
    @objc private func demo() { actions.demo() }
    @objc private func quit() { actions.quit() }
}
