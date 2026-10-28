import AppKit
import Foundation

struct CueRecentRecord {
    let event: CueEvent
    var openedAt: Date?
    var resolved = false
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

    init(home: URL) {
        file = home.appendingPathComponent("recent-alerts.json")
        load()
    }

    @discardableResult func append(_ event: CueEvent) -> Bool {
        guard !event.isPreview, !records.contains(where: { $0.event.id == event.id }) else { return false }
        records.insert(CueRecentRecord(event: event), at: 0)
        // Receive order breaks timestamp ties and tolerates delayed delivery.
        records = records.enumerated().sorted {
            $0.element.event.date == $1.element.event.date ? $0.offset < $1.offset : $0.element.event.date > $1.element.event.date
        }.prefix(Self.capacity).map(\.element)
        save(); return true
    }

    @discardableResult func markOpened(id: String, didOpen: Bool) -> Bool {
        guard didOpen, let index = records.firstIndex(where: { $0.event.id == id }), records[index].canOpen else { return false }
        records[index].openedAt = Date(); save(); return true
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
            var row: [String: Any] = ["event": eventJSON(record.event), "resolved": record.resolved]
            if let date = record.openedAt { row["opened_at"] = date.timeIntervalSince1970 }
            return row
        }
        do {
            try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            let data = try JSONSerialization.data(withJSONObject: ["version": 1, "records": rows], options: [.sortedKeys])
            try data.write(to: file, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
            lastError = ""
        } catch { lastError = "最近提醒未能保存，重启后可能无法恢复。" }
    }

    private func load() {
        guard FileManager.default.fileExists(atPath: file.path) else { return }
        do {
            let data = try Data(contentsOf: file)
            guard data.count <= 1_048_576,
                  let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  json["version"] as? Int == 1, let rows = json["records"] as? [[String: Any]] else {
                throw CocoaError(.fileReadCorruptFile)
            }
            var ids = Set<String>()
            records = rows.compactMap { row -> CueRecentRecord? in
                guard let raw = row["event"] as? [String: Any],
                      let data = try? JSONSerialization.data(withJSONObject: raw), let event = CueEvent(data),
                      !event.isPreview, ids.insert(event.id).inserted else { return nil }
                let opened = (row["opened_at"] as? Double).flatMap { $0.isFinite && $0 > 0 ? Date(timeIntervalSince1970: $0) : nil }
                return CueRecentRecord(event: event, openedAt: opened, resolved: row["resolved"] as? Bool ?? false)
            }.prefix(Self.capacity).map { $0 }
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
        for id in 0..<55 { history.append(event(id)) }
        results["history_keeps_50_in_date_order"] = history.records.count == 50 && history.records.first?.event.id == "event-54" && history.records.last?.event.id == "event-5"
        let count = history.records.count
        history.append(event(54)); history.append(CueEvent(kind: "complete", title: "预览", message: ""))
        results["duplicates_and_previews_do_not_enter_history"] = history.records.count == count
        results["failed_open_never_marks_event"] = !history.markOpened(id: "event-54", didOpen: false) && history.unopenedCount == 50
        history.markOpened(id: "event-54", didOpen: true)
        history.append(event(55))
        results["new_event_same_chat_remains_unopened"] = history.records.first?.isOpened == false && history.records.first(where: { $0.event.id == "event-54" })?.isOpened == true
        history.resolve(id: "event-53")
        let restored = CueRecentHistory(home: root)
        results["opening_and_resolution_survive_restart"] = restored.records.count == 50 && restored.records.first(where: { $0.event.id == "event-54" })?.isOpened == true && restored.records.first(where: { $0.event.id == "event-53" })?.resolved == true
        results["resolved_is_not_opened"] = restored.records.first(where: { $0.event.id == "event-53" })?.isOpened == false
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
}

private final class CueRecentRowButton: NSButton {
    let record: CueRecentRecord
    private var hovered = false
    private var tracking: NSTrackingArea?
    init(record: CueRecentRecord, width: CGFloat) {
        self.record = record
        super.init(frame: NSRect(x: 8, y: 0, width: width - 16, height: 54))
        title = ""; isBordered = false; setButtonType(.momentaryPushIn); focusRingType = .exterior
        isEnabled = record.canOpen
        let tint: NSColor = record.isOpened ? .secondaryLabelColor : .systemBlue
        let icon = NSImageView(frame: NSRect(x: 11, y: 16, width: 20, height: 20))
        icon.image = NSImage(systemSymbolName: record.isOpened ? "arrow.up.forward.app" : "circle.fill", accessibilityDescription: nil)
        icon.contentTintColor = tint; addSubview(icon)
        let heading = NSTextField(labelWithString: record.event.title)
        heading.frame = NSRect(x: 42, y: 6, width: frame.width - 168, height: 21)
        heading.font = .systemFont(ofSize: 13, weight: record.isOpened ? .regular : .semibold)
        heading.textColor = record.isOpened ? .secondaryLabelColor : .labelColor
        heading.lineBreakMode = .byTruncatingTail; addSubview(heading)
        let state = record.isOpened ? "已从 VibeCue 打开" : record.canOpen ? "尚未打开" : "无可用对话入口"
        let kind = record.resolved ? "人工批准已解除" : record.event.label
        let date = DateFormatter(); date.locale = Locale(identifier: "zh_CN"); date.dateFormat = Calendar.current.isDateInToday(record.event.date) ? "HH:mm" : "MM-dd HH:mm"
        let metadata = NSTextField(labelWithString: "\(kind) · \(date.string(from: record.event.date))")
        metadata.frame = NSRect(x: 42, y: 29, width: frame.width - 56, height: 17)
        metadata.font = .systemFont(ofSize: 10.5); metadata.textColor = .secondaryLabelColor; metadata.lineBreakMode = .byTruncatingTail; addSubview(metadata)
        let stateLabel = NSTextField(labelWithString: record.isOpened ? "已打开" : record.canOpen ? "未打开" : "不可跳转")
        stateLabel.frame = NSRect(x: frame.width - 104, y: 8, width: 89, height: 18)
        stateLabel.alignment = .right; stateLabel.font = .systemFont(ofSize: 10.5, weight: .medium); stateLabel.textColor = tint; addSubview(stateLabel)
        toolTip = "\(record.event.title)\n\(kind) · \(state)"
        setAccessibilityLabel("\(state)，\(kind)，\(record.event.title)，\(date.string(from: record.event.date))")
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override var isFlipped: Bool { true }
    override func hitTest(_ point: NSPoint) -> NSView? {
        // The labels and symbol are part of one row action, so clicking text
        // has the same effect as clicking its surrounding highlight.
        super.hitTest(point) == nil ? nil : self
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
        tint.withAlphaComponent(isHighlighted ? 0.20 : hovered ? 0.13 : record.isOpened ? 0.025 : 0.075).setFill()
        NSBezierPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), xRadius: 8, yRadius: 8).fill()
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
    func update(status: String, enabled: Bool, soundEnabled: Bool, paused: Bool, maximumHeight: CGFloat = 900) {
        self.status = status; self.enabled = enabled; self.soundEnabled = soundEnabled
        self.paused = paused; self.maximumHeight = maximumHeight
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
        view.subviews.forEach { $0.removeFromSuperview() }
        let fixedHeight: CGFloat = 219
        let maxHeight = max(390, min(900, maximumHeight))
        let capacity = max(3, min(12, Int((maxHeight - fixedHeight) / 56)))
        let rowCount = min(capacity, max(3, history.records.count))
        let viewport = CGFloat(rowCount) * 56
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
        label("最近提醒", NSRect(x: 22, y: 163, width: 130, height: 23), size: 13, weight: .semibold)
        label("\(history.unopenedCount) 条未打开 · 共 \(history.records.count) / 50 条", NSRect(x: 205, y: 165, width: 349, height: 20), size: 11, color: history.unopenedCount > 0 ? .systemBlue : .secondaryLabelColor)
        scroll.frame = NSRect(x: 10, y: 193, width: panelWidth - 20, height: viewport)
        scroll.drawsBackground = false; scroll.hasVerticalScroller = true; scroll.scrollerStyle = .legacy
        scroll.borderType = .noBorder; scroll.autohidesScrollers = false
        let document = CueFlippedView(frame: NSRect(x: 0, y: 0, width: panelWidth - 35, height: max(viewport, CGFloat(history.records.count) * 56)))
        for (index, record) in history.records.enumerated() {
            let row = CueRecentRowButton(record: record, width: document.frame.width)
            row.frame.origin.y = CGFloat(index) * 56; row.target = self; row.action = #selector(openRow(_:))
            row.identifier = NSUserInterfaceItemIdentifier(record.event.id); document.addSubview(row)
        }
        if history.records.isEmpty {
            let empty = NSTextField(wrappingLabelWithString: "新的任务提醒会保存在这里。\n蓝色表示尚未通过 VibeCue 打开，灰色表示已打开。")
            empty.frame = NSRect(x: 24, y: 41, width: 490, height: 62)
            empty.font = .systemFont(ofSize: 12); empty.textColor = .secondaryLabelColor; document.addSubview(empty)
        }
        recentDocument = document; scroll.documentView = document; view.addSubview(scroll)
        scroll.contentView.scroll(to: scroll.contentView.constrainBoundsRect(NSRect(origin: origin, size: scroll.contentView.bounds.size)).origin)
        scroll.reflectScrolledClipView(scroll.contentView)
        label(history.lastError.isEmpty ? "蓝色未打开 · 灰色已打开。状态只记录从 VibeCue 发起的跳转。" : history.lastError,
              NSRect(x: 22, y: height - 20, width: 535, height: 16), size: 10, color: history.lastError.isEmpty ? .tertiaryLabelColor : .systemOrange)
    }
    @objc private func openRow(_ sender: NSButton) {
        guard let id = sender.identifier?.rawValue, let record = history.records.first(where: { $0.event.id == id }), record.canOpen else { return }
        actions.openEvent(record.event)
    }
    @objc private func openSettings() { actions.openSettings() }
    @objc private func openUsage() { actions.openUsage() }
    @objc private func toggleEnabled() { actions.toggleEnabled() }
    @objc private func toggleSound() { actions.toggleSound() }
    @objc private func pause() { actions.pause() }
    @objc private func demo() { actions.demo() }
    @objc private func quit() { actions.quit() }
}
