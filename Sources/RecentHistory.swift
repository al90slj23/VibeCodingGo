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
    let recoveryStatus: String
    let interruptedAt: Date?
    let recoveryReason: String
    private let titleSortKey: String

    init?(raw: [String: Any]) {
        guard let threadID = raw["thread_id"] as? String, !threadID.isEmpty, threadID.count <= 160,
              threadID.unicodeScalars.allSatisfy({ CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_")).contains($0) }),
              (raw["provider"] as? String ?? "codex") == "codex",
              let startDate = Self.date(raw["started_at"]),
              let updatedDate = Self.date(raw["last_activity"]) else { return nil }
        self.threadID = threadID
        self.title = String((raw["title"] as? String ?? "Codex 对话").prefix(180))
        self.titleSortKey = (self.title.applyingTransform(.toLatin, reverse: false) ?? self.title)
            .folding(options: [.caseInsensitive, .diacriticInsensitive, .widthInsensitive], locale: Locale(identifier: "en_US_POSIX"))
        self.provider = "codex"; self.startDate = startDate; self.updatedDate = updatedDate
        self.turnID = String((raw["turn_id"] as? String ?? "").prefix(160))
        self.recoveryStatus = ["interrupted", "unconfirmed"].contains(raw["status"] as? String ?? "") ? (raw["status"] as? String ?? "") : "running"
        self.interruptedAt = Self.date(raw["interrupted_at"])
        self.recoveryReason = String((raw["reason_text"] as? String ?? "").prefix(300))
    }

    private static func date(_ raw: Any?) -> Date? {
        if let value = raw as? Double, value.isFinite, value > 0 { return Date(timeIntervalSince1970: value) }
        guard let value = raw as? String else { return nil }
        let formatter = ISO8601DateFormatter(); formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    }

    var needsRecovery: Bool { recoveryStatus != "running" }
    var recoveryLabel: String { recoveryStatus == "interrupted" ? "中断待续" : "运行待确认" }
    var navigationID: String { "\(needsRecovery ? "interrupted" : "running"):\(threadID)" }
    var ephemeralCueEvent: CueEvent { CueEvent(runningThreadID: threadID, title: title, date: startDate) }

    /// Shared by the popover and shortcut menu. Activity updates never move a
    /// row; Chinese titles use pinyin and numeric suffixes follow Finder order.
    static func titleAscending(_ lhs: CueRunningTask, _ rhs: CueRunningTask) -> Bool {
        let order = lhs.titleSortKey.compare(rhs.titleSortKey, options: [.numeric], locale: Locale(identifier: "en_US_POSIX"))
        return order == .orderedSame ? lhs.threadID < rhs.threadID : order == .orderedAscending
    }
}

/// A derived view only: running tasks never mutate, replace or consume the ledger.
private struct CueStatusListSnapshot {
    let pinned: [CueRecentRecord]
    let running: [CueRunningTask]
    let interrupted: [CueRunningTask]
    let followups: [CueThreadFollowup]
    let pinnedFollowups: [CueThreadFollowup]
    let ordinary: [CueRecentRecord]
    let activeThreadIDs: Set<String>
    let activeCount: Int
    let recoveryByThread: [String: CueRunningTask]
    let deferredCount: Int
    var rowCount: Int { pinned.count + pinnedFollowups.count + running.count + interrupted.count + followups.count + ordinary.count }
    var sectionCount: Int { [!pinned.isEmpty || !pinnedFollowups.isEmpty, !running.isEmpty, !interrupted.isEmpty, !followups.isEmpty, !ordinary.isEmpty].filter { $0 }.count }

    init(records: [CueRecentRecord], runningTasks: [CueRunningTask], interruptedTasks: [CueRunningTask] = [], followups: [CueThreadFollowup] = []) {
        var latestByThread: [String: CueRunningTask] = [:]
        for task in runningTasks {
            if let previous = latestByThread[task.threadID], previous.updatedDate >= task.updatedDate { continue }
            latestByThread[task.threadID] = task
        }
        let active = latestByThread.values.sorted(by: CueRunningTask.titleAscending)
        let activeIDs = Set(active.map(\.threadID))
        activeThreadIDs = activeIDs; activeCount = active.count
        deferredCount = Set(followups.map(\.threadID)).subtracting(activeIDs).count
        pinned = records.filter(\.isPinned)
        let alertPinnedThreadIDs = Set(pinned.filter { $0.event.provider == "codex" }.map { $0.event.threadID })
        pinnedFollowups = followups.filter { $0.isPinned && !alertPinnedThreadIDs.contains($0.threadID) }
        let pinnedThreadIDs = alertPinnedThreadIDs.union(pinnedFollowups.map(\.threadID))
        running = active.filter { !pinnedThreadIDs.contains($0.threadID) }
        var recovery: [String: CueRunningTask] = [:]
        for task in interruptedTasks where task.needsRecovery && !activeIDs.contains(task.threadID) {
            if let old = recovery[task.threadID], (old.interruptedAt ?? old.updatedDate) >= (task.interruptedAt ?? task.updatedDate) { continue }
            recovery[task.threadID] = task
        }
        recoveryByThread = recovery
        interrupted = recovery.values.filter { !pinnedThreadIDs.contains($0.threadID) }.sorted { ($0.interruptedAt ?? $0.updatedDate) > ($1.interruptedAt ?? $1.updatedDate) }
        self.followups = followups.filter { !$0.isPinned && !pinnedThreadIDs.contains($0.threadID) && !activeIDs.contains($0.threadID) && recovery[$0.threadID] == nil }
        let followupIDs = Set(followups.map(\.threadID))
        ordinary = records.filter { !$0.isPinned && !($0.event.provider == "codex" && (activeIDs.contains($0.event.threadID) || recovery[$0.event.threadID] != nil || followupIDs.contains($0.event.threadID))) }
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
    let followups: CueThreadFollowups
    private(set) var records: [CueRecentRecord] = []
    private(set) var lastError = ""
    var events: [CueEvent] { records.map(\.event) }
    var unopenedCount: Int {
        let alerts = records.filter { !$0.isOpened && $0.canOpen }
        let threads = Set(alerts.map { $0.event.threadID })
        return alerts.count + followups.records.filter { $0.unread && !threads.contains($0.threadID) }.count
    }
    var pinnedCount: Int { records.filter(\.isPinned).count }
    var ordinaryCount: Int { records.filter { !$0.isPinned && !( $0.event.provider == "codex" && followups.contains($0.event.threadID)) }.count }

    init(home: URL) {
        file = home.appendingPathComponent("recent-alerts.json")
        followups = CueThreadFollowups(home: home)
        load()
    }

    /// Entering the local conversation viewer observes a reminder but never
    /// claims its underlying work is handled. Closing needs no compensating UI.
    @discardableResult func beginReview(event: CueEvent) -> Bool {
        guard !event.isPreview, followups.retain(threadID: event.threadID, title: event.title, provider: event.provider) else { return false }
        _ = followups.setUnread(threadID: event.threadID, unread: false)
        _ = markOpened(id: event.id, didOpen: true)
        return true
    }
    @discardableResult func markThreadHandled(threadID: String) -> Bool {
        let removed = followups.markHandled(threadID: threadID)
        for index in records.indices where records[index].event.threadID == threadID && records[index].event.provider == "codex" {
            records[index].openedAt = records[index].openedAt ?? Date()
        }
        normalize(); save(); return removed
    }
    @discardableResult func removeFollowup(threadID: String) -> Bool {
        let removed = followups.markHandled(threadID: threadID); normalize(); save(); return removed
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

    /// A task without a reminder gets explicit workflow state, never a fake
    /// completion event. Existing reminders keep their original provenance.
    @discardableResult func markThreadUnread(event: CueEvent) -> Bool {
        guard event.provider == "codex", CueThreadFollowups.valid(event.threadID) else { return false }
        if let record = records.filter({ $0.event.threadID == event.threadID && $0.canOpen }).max(by: { $0.event.date < $1.event.date }) {
            return markUnread(id: record.event.id)
        }
        guard followups.retain(threadID: event.threadID, title: event.title, provider: event.provider) else { return false }
        return followups.setUnread(threadID: event.threadID, unread: true)
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
            if record.event.provider == "codex", followups.contains(record.event.threadID) { return true }
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
        let reviewHome = root.appendingPathComponent("review-workflow")
        let review = CueRecentHistory(home: reviewHome)
        let firstReview = event(800, thread: "needs-later")
        review.append(firstReview); _ = review.beginReview(event: firstReview)
        let afterPassiveClose = CueRecentHistory(home: reviewHome)
        results["reviewing_then_closing_keeps_distinct_pending_state"] = afterPassiveClose.records.first?.isOpened == true && afterPassiveClose.followups.contains("needs-later") && afterPassiveClose.unopenedCount == 0
        let synthetic = CueEvent(runningThreadID: "running-review", title: "运行任务", date: Date())
        review.beginReview(event: synthetic); review.beginReview(event: synthetic)
        results["review_running_task_is_retained_once_without_synthetic_alert"] = review.followups.records.filter { $0.threadID == "running-review" }.count == 1 && review.records.count == 1
        review.markThreadHandled(threadID: "needs-later")
        let handled = CueRecentHistory(home: reviewHome)
        results["explicit_handled_only_releases_selected_followup_after_restart"] = !handled.followups.contains("needs-later") && handled.followups.contains("running-review") && handled.records.count == 1
        for number in 0..<80 { review.followups.retain(threadID: "later-\(number)", title: "待续 \(number)") }
        results["followup_retention_does_not_consume_alert_capacity"] = review.followups.records.count == 81 && review.ordinaryCount == 1 && CueRecentHistory(home: reviewHome).followups.records.count == 81
        let protected = try? FileManager.default.attributesOfItem(atPath: review.followups.file.path)[.posixPermissions] as? NSNumber
        results["followup_ledger_is_private"] = protected?.intValue == 0o600
        results["legacy_read_history_is_not_implicitly_migrated_into_pending"] = legacy.followups.records.isEmpty
        let pendingCapacity = CueRecentHistory(home: root.appendingPathComponent("pending-capacity"))
        for number in 900..<960 { let item = event(number, thread: "pending-\(number)"); pendingCapacity.append(item); pendingCapacity.beginReview(event: item) }
        for number in 1000..<1060 { pendingCapacity.append(event(number)); pendingCapacity.markOpened(id: "event-\(number)", didOpen: true) }
        let pendingRestart = CueRecentHistory(home: root.appendingPathComponent("pending-capacity"))
        results["opened_pending_alerts_survive_fifty_new_history_records"] = pendingRestart.followups.records.count == 60 && pendingRestart.ordinaryCount == 50 && pendingRestart.records.count == 110
        let explicit = CueRecentHistory(home: root.appendingPathComponent("explicit-unread"))
        let noAlert = CueEvent(runningThreadID: "no-alert", title: "正在运行的任务", date: Date())
        explicit.markThreadUnread(event: noAlert)
        let reloadedUnread = CueRecentHistory(home: explicit.file.deletingLastPathComponent())
        results["manual_task_unread_is_persisted_without_fabricating_alert"] = reloadedUnread.records.isEmpty && reloadedUnread.unopenedCount == 1 && reloadedUnread.followups.records.first?.unread == true && reloadedUnread.followups.records.first?.isPinned == true
        explicit.followups.setPinned(threadID: "no-alert", pinned: true)
        explicit.beginReview(event: noAlert)
        let reread = CueRecentHistory(home: explicit.file.deletingLastPathComponent())
        results["review_clears_task_unread_but_keeps_manual_pin"] = reread.followups.records.first?.unread == false && reread.followups.records.first?.pinned == true && reread.unopenedCount == 0
        let existingAlert = event(1200, thread: "real-alert")
        explicit.append(existingAlert); explicit.markOpened(id: existingAlert.id, didOpen: true)
        explicit.markThreadUnread(event: CueEvent(runningThreadID: "real-alert", title: "原始提醒", date: Date()))
        results["mark_task_unread_reuses_original_alert_provenance"] = explicit.records.count == 1 && explicit.records.first?.event.id == existingAlert.id && explicit.records.first?.isOpened == false
        results["old_followup_records_default_to_read"] = afterPassiveClose.followups.records.allSatisfy { !$0.unread }
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
    var batchConfigure: ([CueBatchTarget]) -> Void = { _ in }
    var connectCodex: () -> Void = {}
    var dismissInterrupted: (CueRunningTask) -> Void = { _ in }
    var openInCodex: (CueEvent) -> Void = { _ in }
    var threadAction: (String, String) -> Void = { _, _ in }
    var openSideChat: (CueSideChat) -> Void = { _ in }
}

/// An independent native control inside a navigation row. Its hit target is
/// preserved by the row, so quick actions never also open the local viewer.
private final class CueRowQuickButton: NSButton {
    var invoke: (() -> Void)?
    init(command: String) {
        super.init(frame: .zero)
        identifier = NSUserInterfaceItemIdentifier("quick-" + command)
        title = ""; isBordered = false; imagePosition = .imageOnly
        setButtonType(.momentaryPushIn); target = self; action = #selector(trigger)
        focusRingType = .exterior
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    @objc private func trigger() { invoke?() }
}

/// Shared geometry keeps navigation, accounting and live state separate.
/// The rightmost controls form a compact 2 × 2 cluster.
private struct CueTaskRowLayout {
    static let height: CGFloat = 78
    static let pitch: CGFloat = 80
    let width: CGFloat
    var columnWidth: CGFloat { min(130, max(100, (width - 330) / 3)) }
    var actionsX: CGFloat { width - 62 }
    var historyX: CGFloat { actionsX - 16 - columnWidth }
    var turnX: CGFloat { historyX - 16 - columnWidth }
    var leftMaxX: CGFloat { turnX - 16 }
    var state: NSRect { NSRect(x: leftMaxX - 130, y: 7, width: 130, height: 18) }
    var elapsed: NSRect { NSRect(x: state.minX, y: 29, width: state.width, height: 15) }
    var heading: NSRect { NSRect(x: 42, y: 6, width: max(100, state.minX - 54), height: 21) }
    var metadata: NSRect { NSRect(x: 42, y: 28, width: heading.width, height: 15) }
    var model: NSRect { NSRect(x: 42, y: 52, width: max(100, leftMaxX - 180), height: 16) }
    var speed: NSRect { NSRect(x: leftMaxX - 130, y: 52, width: 130, height: 16) }
    func action(_ index: Int) -> NSRect {
        NSRect(x: actionsX + CGFloat(index % 2) * 28, y: 12 + CGFloat(index / 2) * 29, width: 25, height: 25)
    }
}

private final class CueRecentRowButton: NSButton {
    private(set) var record: CueRecentRecord
    private(set) var isRunning: Bool
    private var recovery: CueRunningTask?
    private var pendingFollowup = false
    var selected = false { didSet { needsDisplay = true } }
    var selectRow: (() -> Void)?
    var contextMenu: (() -> NSMenu)?
    var hoverChanged: ((Bool) -> Void)?
    private var hovered = false
    private var tracking: NSTrackingArea?
    init(record: CueRecentRecord, width: CGFloat, isRunning: Bool = false) {
        self.record = record; self.isRunning = isRunning
        super.init(frame: NSRect(x: 8, y: 0, width: width - 16, height: CueTaskRowLayout.height))
        title = ""; isBordered = false; setButtonType(.momentaryPushIn); focusRingType = .exterior
        isEnabled = true
        let tint: NSColor = record.isOpened ? .secondaryLabelColor : .systemBlue
        let icon = NSImageView(frame: NSRect(x: 11, y: 16, width: 20, height: 20))
        icon.identifier = NSUserInterfaceItemIdentifier("row-icon")
        icon.image = NSImage(systemSymbolName: record.isOpened ? (record.manualPinned ? "pin.fill" : "arrow.up.forward.app") : "circle.fill", accessibilityDescription: nil)
        icon.contentTintColor = tint; addSubview(icon)
        let heading = NSTextField(labelWithString: record.event.title)
        heading.identifier = NSUserInterfaceItemIdentifier("row-title")
        heading.frame = NSRect(x: 42, y: 6, width: frame.width - 322, height: 21)
        heading.font = .systemFont(ofSize: 13, weight: record.isOpened ? .regular : .semibold)
        heading.textColor = record.isOpened ? .secondaryLabelColor : .labelColor
        heading.lineBreakMode = .byTruncatingTail; addSubview(heading)
        let state = record.isOpened ? "已从 VibeCue 打开" : record.canOpen ? "未读" : "无可用对话入口"
        let kind = record.resolved ? (record.event.kind == "permission" ? "人工批准已解除" : "等待已解除") : record.event.label
        let date = DateFormatter(); date.locale = Locale(identifier: "zh_CN"); date.dateFormat = Calendar.current.isDateInToday(record.event.date) ? "HH:mm" : "MM-dd HH:mm"
        let metadata = NSTextField(labelWithString: "\(isRunning ? "上次提醒：" : "")\(kind) · \(date.string(from: record.event.date))\(record.isPinned ? " · 置顶" : "")")
        metadata.identifier = NSUserInterfaceItemIdentifier("row-metadata")
        metadata.frame = NSRect(x: 42, y: 25, width: frame.width - 322, height: 15)
        metadata.font = .systemFont(ofSize: 10.5); metadata.textColor = .secondaryLabelColor; metadata.lineBreakMode = .byTruncatingTail; addSubview(metadata)
        let stateLabel = NSTextField(labelWithString: isRunning ? (record.isOpened ? "运行中 · 已打开" : "运行中 · 未读") : record.isOpened ? (record.manualPinned ? "已打开 · 置顶" : "已打开") : record.canOpen ? "未读 · 置顶" : "不可跳转")
        stateLabel.identifier = NSUserInterfaceItemIdentifier("row-state")
        stateLabel.frame = NSRect(x: frame.width - 270, y: 8, width: 145, height: 18)
        stateLabel.alignment = .right; stateLabel.font = .systemFont(ofSize: 10.5, weight: .medium); stateLabel.textColor = isRunning ? .systemGreen : tint; addSubview(stateLabel)
        toolTip = "\(record.event.title)\n\(kind) · \(state)\(isRunning ? " · 当前正在运行" : "")"
        setAccessibilityLabel("\(isRunning ? "当前正在运行，" : "")\(state)，\(record.isPinned ? "已置顶，" : "")\(kind)，\(record.event.title)，\(date.string(from: record.event.date))")
        setAccessibilityHelp("点击打开对话；右键可标为未读、置顶或从列表删除。")
    }
    func refresh(record: CueRecentRecord?, isRunning: Bool, recovery: CueRunningTask? = nil, pending: Bool = false) {
        if let record { self.record = record }
        self.isRunning = isRunning
        self.recovery = isRunning ? nil : recovery
        pendingFollowup = pending
        let record = self.record
        let tint: NSColor = record.isOpened ? .secondaryLabelColor : .systemBlue
        let kind = record.resolved ? (record.event.kind == "permission" ? "人工批准已解除" : "等待已解除") : record.event.label
        let date = DateFormatter(); date.locale = Locale(identifier: "zh_CN"); date.dateFormat = Calendar.current.isDateInToday(record.event.date) ? "HH:mm" : "MM-dd HH:mm"
        for view in subviews {
            if let field = view as? NSTextField {
                switch field.identifier?.rawValue {
                case "row-title": field.stringValue = record.event.title; field.textColor = record.isOpened ? .secondaryLabelColor : .labelColor
                case "row-metadata": field.stringValue = "\(isRunning ? "上次提醒：" : "")\(kind) · \(date.string(from: record.event.date))\(record.isPinned ? " · 置顶" : "")"
                case "row-state": field.stringValue = recovery?.recoveryLabel ?? (isRunning ? (record.isOpened ? "运行中 · 已打开" : "运行中 · 未读") : pending && record.isOpened ? "待续 · 置顶" : record.isOpened ? (record.manualPinned ? "已打开 · 置顶" : "已打开") : record.canOpen ? "未读 · 置顶" : "不可跳转"); field.textColor = recovery != nil ? .systemOrange : isRunning ? .systemGreen : pending && record.isOpened ? .systemPurple : tint
                default: break
                }
            } else if let icon = view as? NSImageView, icon.identifier?.rawValue == "row-icon" {
                icon.image = NSImage(systemSymbolName: record.isOpened ? (record.manualPinned ? "pin.fill" : "arrow.up.forward.app") : "circle.fill", accessibilityDescription: nil); icon.contentTintColor = tint
            }
        }
        let state = pending && record.isOpened && !isRunning ? "待续" : record.isOpened ? "已查看" : "未读"
        setAccessibilityLabel("\(isRunning ? "当前正在运行，" : "")\(state)，\(record.isPinned ? "已置顶，" : "")\(kind)，\(record.event.title)，\(date.string(from: record.event.date))")
        if let recovery { toolTip = "\(record.event.title)\n\(recovery.recoveryLabel)：\(recovery.recoveryReason)\n打开对话不会自动清除此标记；继续运行后自动更新。" }
        needsDisplay = true
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override var isFlipped: Bool { true }
    override func accessibilityChildren() -> [Any]? {
        subviews.filter { ($0 is CueRowQuickButton || $0 is CueSideChatButton) && !$0.isHidden }
    }
    override func hitTest(_ point: NSPoint) -> NSView? {
        // The labels and symbol are part of one row action, so clicking text
        // has the same effect as clicking its surrounding highlight.
        guard let hit = super.hitTest(point) else { return nil }
        var target: NSView? = hit
        while let candidate = target, candidate !== self {
            if candidate is CueRowQuickButton || candidate is CueSideChatButton { return candidate }
            target = candidate.superview
        }
        return self
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
        let next = NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self)
        addTrackingArea(next); tracking = next
    }
    override func mouseEntered(with event: NSEvent) { hovered = true; needsDisplay = true; hoverChanged?(true) }
    override func mouseExited(with event: NSEvent) { hovered = false; needsDisplay = true; hoverChanged?(false) }
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
    private(set) var task: CueRunningTask
    private(set) var followup: CueThreadFollowup?
    var navigationID: String { followup?.navigationID ?? task.navigationID }
    private var live = true
    var selected = false { didSet { needsDisplay = true } }
    var selectRow: (() -> Void)?
    var hoverChanged: ((Bool) -> Void)?
    var contextMenu: (() -> NSMenu)?
    private var hovered = false
    private var tracking: NSTrackingArea?
    init(task: CueRunningTask, width: CGFloat) {
        self.task = task
        super.init(frame: NSRect(x: 8, y: 0, width: width - 16, height: CueTaskRowLayout.height))
        title = ""; isBordered = false; setButtonType(.momentaryPushIn); focusRingType = .exterior
        let icon = NSImageView(frame: NSRect(x: 11, y: 16, width: 20, height: 20))
        icon.identifier = NSUserInterfaceItemIdentifier("row-icon")
        icon.image = NSImage(systemSymbolName: "bolt.horizontal.circle", accessibilityDescription: nil)
        icon.contentTintColor = .systemGreen; addSubview(icon)
        let heading = NSTextField(labelWithString: task.title)
        heading.identifier = NSUserInterfaceItemIdentifier("row-title")
        heading.frame = NSRect(x: 42, y: 6, width: frame.width - 322, height: 21)
        heading.font = .systemFont(ofSize: 13, weight: .medium); heading.lineBreakMode = .byTruncatingTail; addSubview(heading)
        let date = DateFormatter(); date.locale = Locale(identifier: "zh_CN")
        date.dateFormat = Calendar.current.isDateInToday(task.startDate) ? "HH:mm" : "MM-dd HH:mm"
        let start = date.string(from: task.startDate)
        date.dateFormat = "HH:mm"
        let metadata = NSTextField(labelWithString: "Codex · 开始 \(start) · 最近活动 \(date.string(from: task.updatedDate))")
        metadata.identifier = NSUserInterfaceItemIdentifier("row-metadata")
        metadata.frame = NSRect(x: 42, y: 25, width: frame.width - 322, height: 15)
        metadata.font = .systemFont(ofSize: 10.5); metadata.textColor = .secondaryLabelColor
        metadata.lineBreakMode = .byTruncatingTail; addSubview(metadata)
        let state = NSTextField(labelWithString: "● 运行中")
        state.identifier = NSUserInterfaceItemIdentifier("row-state")
        state.frame = NSRect(x: frame.width - 270, y: 8, width: 145, height: 18)
        state.alignment = .right; state.font = .systemFont(ofSize: 10.5, weight: .medium); state.textColor = .systemGreen; addSubview(state)
        identifier = NSUserInterfaceItemIdentifier(task.navigationID)
        toolTip = "\(task.title)\n正在运行 · 点击打开 Codex 对话"
        setAccessibilityLabel("正在运行，\(task.title)，开始于 \(start)")
        setAccessibilityHelp("点击打开正在运行的 Codex 对话。")
    }
    convenience init(followup: CueThreadFollowup, width: CGFloat) {
        let task = CueRunningTask(raw: ["thread_id": followup.threadID, "title": followup.title, "started_at": followup.createdAt.timeIntervalSince1970, "last_activity": followup.reviewedAt.timeIntervalSince1970])!
        self.init(task: task, width: width); self.followup = followup
        identifier = NSUserInterfaceItemIdentifier(followup.navigationID)
        refreshFollowup(followup, running: false, recovery: nil)
    }
    func refreshFollowup(_ followup: CueThreadFollowup, running: Bool, recovery: CueRunningTask?) {
        self.followup = followup; live = running
        let state = running ? (followup.unread ? "运行中 · 未读" : "运行中") : recovery != nil ? (followup.unread ? "中断 · 未读" : "中断 · 待续") : followup.unread ? "未读 · 待续" : "待续"
        let tint: NSColor = followup.unread ? .systemBlue : running ? .systemGreen : recovery != nil ? .systemOrange : .systemPurple
        let date = DateFormatter(); date.locale = Locale(identifier: "zh_CN"); date.dateFormat = Calendar.current.isDateInToday(followup.reviewedAt) ? "HH:mm" : "MM-dd HH:mm"
        for view in subviews {
            if let field = view as? NSTextField {
                switch field.identifier?.rawValue {
                case "row-title": field.stringValue = followup.title
                case "row-state": field.stringValue = state; field.textColor = tint
                case "row-metadata": field.stringValue = "\(followup.unread ? "标为未读" : "保留待续") \(date.string(from: followup.reviewedAt)) · 留到方便时继续"
                default: break
                }
            } else if let icon = view as? NSImageView { icon.image = NSImage(systemSymbolName: followup.isPinned ? "pin.fill" : "bookmark.fill", accessibilityDescription: nil); icon.contentTintColor = tint }
        }
        setAccessibilityLabel("\(state)，\(followup.title)，关闭查看窗口后仍保留")
        needsDisplay = true
    }
    func refresh(task: CueRunningTask?, detail: CueTaskDetail?) {
        if let task { self.task = task }
        live = task != nil && task?.needsRecovery != true
        var status = detail?.status ?? "unknown"
        if live { status = "running" }
        else if status == "running" { status = "unknown" }
        let recovering = task?.needsRecovery == true
        let label = recovering ? self.task.recoveryLabel : ["running": "● 运行中", "waiting": "待处理", "complete": "已完成", "aborted": "已中止"][status] ?? "状态待确认"
        let tint: NSColor = recovering ? .systemOrange : live ? .systemGreen : status == "waiting" ? .systemOrange : .secondaryLabelColor
        let date = DateFormatter(); date.locale = Locale(identifier: "zh_CN"); date.dateFormat = "HH:mm"
        let origin = detail?.elapsedStatus == "complete" ? "开始" : "观测起点"
        for view in subviews {
            if let field = view as? NSTextField {
                switch field.identifier?.rawValue {
                case "row-title": field.stringValue = self.task.title
                case "row-metadata": field.stringValue = recovering ? "Codex · 最后活动 \(date.string(from: self.task.updatedDate)) · 点击打开后手动继续" : "Codex · \(origin) \(date.string(from: self.task.startDate)) · 最近活动 \(date.string(from: task?.updatedDate ?? detail?.lastActivity ?? self.task.updatedDate))"
                case "row-state": field.stringValue = label; field.textColor = tint
                default: break
                }
            } else if let icon = view as? NSImageView { icon.contentTintColor = tint; icon.image = NSImage(systemSymbolName: recovering ? "exclamationmark.arrow.triangle.2.circlepath" : "bolt.horizontal.circle", accessibilityDescription: nil) }
        }
        setAccessibilityLabel("\(label)，\(self.task.title)，\(origin) \(date.string(from: self.task.startDate))")
        if recovering { toolTip = "\(self.task.title)\n\(self.task.recoveryLabel)：\(self.task.recoveryReason)\n仅打开对话；手动继续后由真实运行记录更新状态。" }
        needsDisplay = true
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override var isFlipped: Bool { true }
    override func accessibilityChildren() -> [Any]? {
        subviews.filter { ($0 is CueRowQuickButton || $0 is CueSideChatButton) && !$0.isHidden }
    }
    override func hitTest(_ point: NSPoint) -> NSView? {
        guard let hit = super.hitTest(point) else { return nil }
        var target: NSView? = hit
        while let candidate = target, candidate !== self {
            if candidate is CueRowQuickButton || candidate is CueSideChatButton { return candidate }
            target = candidate.superview
        }
        return self
    }
    override func mouseDown(with event: NSEvent) {
        if event.modifierFlags.contains(.control) { rightMouseDown(with: event); return }
        selectRow?(); super.mouseDown(with: event)
    }
    override func rightMouseDown(with event: NSEvent) { selectRow?(); if let menu = contextMenu?() { NSMenu.popUpContextMenu(menu, with: event, for: self) } }
    override func menu(for event: NSEvent) -> NSMenu? { selectRow?(); return contextMenu?() }
    override func updateTrackingAreas() {
        super.updateTrackingAreas(); if let tracking { removeTrackingArea(tracking) }
        let next = NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self)
        addTrackingArea(next); tracking = next
    }
    override func mouseEntered(with event: NSEvent) { hovered = true; needsDisplay = true; hoverChanged?(true) }
    override func mouseExited(with event: NSEvent) { hovered = false; needsDisplay = true; hoverChanged?(false) }
    override func draw(_ dirtyRect: NSRect) {
        (followup?.unread == true ? NSColor.systemBlue : followup != nil ? NSColor.systemPurple : task.needsRecovery ? NSColor.systemOrange : live ? NSColor.systemGreen : .secondaryLabelColor).withAlphaComponent(isHighlighted || selected ? 0.16 : hovered ? 0.10 : 0.045).setFill()
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
    private let panelWidth: CGFloat = min(960, max(680, (NSScreen.main?.visibleFrame.width ?? 1000) - 40))
    private var recentDocument: CueFlippedView?
    private var selectedID: String?
    private var runningTasks: [CueRunningTask] = []
    private var interruptedTasks: [CueRunningTask] = []
    private var taskDetails: [String: CueTaskDetail] = [:]
    private var sideChats: [String: [CueSideChat]] = [:]
    private let sideChatsController = CueSideChatsPopoverController()
    private var attentionConnected = false
    private var connectionSummary: String?
    private var connectionState = "legacy"
    private var showConnectionAction: Bool?
    private let hoverController = CueTaskHoverController()
    private var batchMode = false
    private var selectedThreads = Set<String>()
    private var batchTargets: [String: CueBatchTarget] = [:]
    private var frozenBatchSnapshot: CueStatusListSnapshot?
    private var frozenPinnedTargets: [CueBatchTarget] = []
    private var frozenRunningTargets: [CueBatchTarget] = []
    private var frozenAllTargets: [CueBatchTarget] = []
    private var pointerTimer: Timer?
    private var elapsedTimer: Timer?
    private weak var pointerAnchor: NSView?
    // Nil is used only by isolated event-handler fixtures. Production always
    // reads the global pointer, even when another application remains active.
    private var pointerLocation: () -> NSPoint? = { NSEvent.mouseLocation }

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
    func update(status: String, enabled: Bool, soundEnabled: Bool, paused: Bool, maximumHeight: CGFloat = 900, runningTasks: [CueRunningTask] = [], interruptedTasks: [CueRunningTask] = [], taskDetails: [String: CueTaskDetail] = [:], attentionConnected: Bool = false, connectionSummary: String? = nil, connectionState: String = "legacy", showConnectionAction: Bool? = nil, sideChats: [String: [CueSideChat]] = [:]) {
        self.status = status; self.enabled = enabled; self.soundEnabled = soundEnabled
        self.paused = paused; self.maximumHeight = maximumHeight; self.runningTasks = runningTasks; self.taskDetails = taskDetails
        self.interruptedTasks = interruptedTasks
        self.attentionConnected = attentionConnected
        self.connectionSummary = connectionSummary
        self.connectionState = ["legacy", "connected", "reconnecting", "disconnected", "unsupported"].contains(connectionState) ? connectionState : "disconnected"
        self.showConnectionAction = showConnectionAction
        self.sideChats = sideChats
        sideChatsController.refresh(groups: sideChats)
        hoverController.refresh()
        if isViewLoaded {
            if batchMode || hoverController.isEngaged || sideChatsController.isShown { refreshInPlace() }
            else { rebuild() }
        }
    }
    override func viewDidAppear() {
        super.viewDidAppear(); pointerTimer?.invalidate(); elapsedTimer?.invalidate()
        let timer = Timer(timeInterval: 0.12, repeats: true) { [weak self] _ in self?.reconcilePointerHover() }
        RunLoop.main.add(timer, forMode: .common); pointerTimer = timer
        let elapsed = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
            guard let self, self.isViewLoaded, self.view.window?.isVisible == true else { return }
            self.refreshElapsedLabels(now: Date())
        }
        RunLoop.main.add(elapsed, forMode: .common); elapsedTimer = elapsed
        refreshElapsedLabels(now: Date())
        DispatchQueue.main.async { [weak self] in self?.reconcilePointerHover() }
    }
    override func viewDidDisappear() { super.viewDidDisappear(); pointerTimer?.invalidate(); pointerTimer = nil; elapsedTimer?.invalidate(); elapsedTimer = nil; pointerAnchor = nil; hoverController.hide(); sideChatsController.close() }
    deinit { pointerTimer?.invalidate(); elapsedTimer?.invalidate() }

    /// Status-item popovers can be visible while Codex remains the active app.
    /// Tracking is activeAlways, and this geometry check also handles a pointer
    /// already inside a row when a non-key popover first appears or scrolls.
    private func reconcilePointerHover() {
        guard !batchMode, !sideChatsController.isShown, isViewLoaded, let window = view.window, window.isVisible, let document = recentDocument,
              let screenPoint = pointerLocation() else { return }
        let point = window.convertPoint(fromScreen: screenPoint)
        let clipPoint = scroll.contentView.convert(point, from: nil)
        let inViewport = scroll.contentView.bounds.contains(clipPoint)
        let location = document.convert(point, from: nil)
        let found = inViewport ? document.subviews.first(where: { ($0 is CueRecentRowButton || $0 is CueRunningRowButton) && !$0.isHidden && $0.frame.contains(location) }) : nil
        guard found !== pointerAnchor else { return }
        if pointerAnchor != nil { hoverController.leaveRow() }
        pointerAnchor = found
        if let row = found as? CueRecentRowButton { hover(true, threadID: row.record.event.threadID, title: row.record.event.title, row: row) }
        else if let row = found as? CueRunningRowButton { hover(true, threadID: row.task.threadID, title: row.followup?.title ?? row.task.title, row: row) }
    }
    @discardableResult private func label(_ text: String, _ rect: NSRect, size: CGFloat = 12, color: NSColor = .labelColor, weight: NSFont.Weight = .regular) -> NSTextField {
        let label = NSTextField(labelWithString: text); label.frame = rect
        label.font = .systemFont(ofSize: size, weight: weight); label.textColor = color
        label.lineBreakMode = .byTruncatingTail; view.addSubview(label); return label
    }
    @discardableResult private func button(_ title: String, _ rect: NSRect, _ selector: Selector) -> NSButton {
        let button = NSButton(title: title, target: self, action: selector)
        button.bezelStyle = .rounded; button.frame = rect; view.addSubview(button); return button
    }
    private var showsConnectionAction: Bool { showConnectionAction ?? (connectionSummary == nil && !attentionConnected) }
    private var footerText: String {
        if !history.lastError.isEmpty { return history.lastError }
        if !history.followups.lastError.isEmpty { return history.followups.lastError }
        if let connectionSummary { return connectionSummary }
        return attentionConnected ? "看过不等于处理完；待续记录不占 50 条历史额度。" : "浏览器批准提醒与批量应用尚未接通。"
    }
    private var footerColor: NSColor {
        if !history.lastError.isEmpty || !history.followups.lastError.isEmpty { return .systemOrange }
        if connectionSummary != nil {
            return connectionState == "connected" ? .secondaryLabelColor : connectionState == "reconnecting" ? .secondaryLabelColor : .systemOrange
        }
        return attentionConnected ? .tertiaryLabelColor : .systemOrange
    }
    private func rebuild() {
        sideChatsController.close()
        let origin = scroll.contentView.bounds.origin
        let snapshot = batchMode ? (frozenBatchSnapshot ?? CueStatusListSnapshot(records: history.records, runningTasks: runningTasks, interruptedTasks: interruptedTasks, followups: history.followups.records)) : CueStatusListSnapshot(records: history.records, runningTasks: runningTasks, interruptedTasks: interruptedTasks, followups: history.followups.records)
        view.subviews.forEach { $0.removeFromSuperview() }
        let fixedHeight: CGFloat = batchMode ? 255 : 219
        let maxHeight = max(390, min(1100, maximumHeight))
        let capacity = max(3, min(12, Int((maxHeight - fixedHeight) / CueTaskRowLayout.pitch)))
        let contentHeight = CGFloat(snapshot.rowCount) * CueTaskRowLayout.pitch + CGFloat(snapshot.sectionCount) * 28
        let viewport = min(CGFloat(capacity) * CueTaskRowLayout.pitch, max(180, contentHeight))
        let height = fixedHeight + viewport
        view.frame.size = NSSize(width: panelWidth, height: height)
        preferredContentSize = view.frame.size
        label("VibeCue", NSRect(x: 22, y: 16, width: 260, height: 27), size: 18, weight: .semibold)
        label(status, NSRect(x: 22, y: 47, width: 535, height: 19), size: 11, color: .secondaryLabelColor).identifier = NSUserInterfaceItemIdentifier("monitor-status")
        let toolbarWidth = (panelWidth - 60) / 4
        for (index, item) in [("打开面板", #selector(openSettings)), ("用量统计", #selector(openUsage)), ("测试提醒", #selector(demo)), ("结束", #selector(quit))].enumerated() {
            button(item.0, NSRect(x: 18 + CGFloat(index) * (toolbarWidth + 8), y: 76, width: toolbarWidth, height: 30), item.1)
        }
        let enable = NSButton(checkboxWithTitle: "启用提醒", target: self, action: #selector(toggleEnabled))
        enable.frame = NSRect(x: 22, y: 119, width: 123, height: 25); enable.state = enabled ? .on : .off; view.addSubview(enable)
        enable.identifier = NSUserInterfaceItemIdentifier("setting-enabled")
        let sound = NSButton(checkboxWithTitle: "播放提示音", target: self, action: #selector(toggleSound))
        sound.frame = NSRect(x: 158, y: 119, width: 143, height: 25); sound.state = soundEnabled ? .on : .off; view.addSubview(sound)
        sound.identifier = NSUserInterfaceItemIdentifier("setting-sound")
        button(paused ? "恢复提醒" : "暂停 30 分钟", NSRect(x: panelWidth - 229, y: 116, width: 204, height: 30), #selector(pause)).identifier = NSUserInterfaceItemIdentifier("pause-button")
        label("任务与提醒", NSRect(x: 22, y: 163, width: 130, height: 23), size: 13, weight: .semibold)
        label(batchMode ? "已选 \(selectedThreads.count) 个对话 · 已去重" : "\(history.unopenedCount) 未读 · \(snapshot.activeCount) 运行 · \(snapshot.recoveryByThread.count) 中断 · \(snapshot.deferredCount) 待续 · 历史 \(history.ordinaryCount)/50", NSRect(x: 155, y: 165, width: panelWidth - 290, height: 20), size: 11, color: history.unopenedCount > 0 || batchMode ? .systemBlue : .secondaryLabelColor).identifier = NSUserInterfaceItemIdentifier("task-counts")
        button(batchMode ? "退出批量" : "批量模式", NSRect(x: panelWidth - 122, y: 157, width: 98, height: 29), #selector(toggleBatchMode))
        if batchMode {
            let pinnedIDs = Set(frozenPinnedTargets.map(\.threadID))
            let activeIDs = Set(frozenRunningTargets.map(\.threadID))
            for (title, ids, x, selector) in [("置顶", pinnedIDs, CGFloat(22), #selector(selectPinned)), ("运行中", activeIDs, 116, #selector(selectRunning))] {
                let checkbox = NSButton(checkboxWithTitle: title, target: self, action: selector)
                checkbox.frame = NSRect(x: x, y: 192, width: 94, height: 25); checkbox.allowsMixedState = true
                checkbox.state = ids.isEmpty || selectedThreads.isDisjoint(with: ids) ? .off : ids.isSubset(of: selectedThreads) ? .on : .mixed
                checkbox.identifier = NSUserInterfaceItemIdentifier(title == "置顶" ? "batch-scope-pinned" : "batch-scope-running")
                checkbox.isEnabled = !ids.isEmpty; view.addSubview(checkbox)
            }
            button("全选", NSRect(x: 213, y: 190, width: 66, height: 28), #selector(selectAllBatchThreads))
            button("清空", NSRect(x: 283, y: 190, width: 66, height: 28), #selector(clearBatchSelection))
            let apply = NSButton(title: "批量操作 (\(selectedThreads.count))", target: self, action: #selector(configureSelected))
            apply.frame = NSRect(x: panelWidth - 211, y: 190, width: 187, height: 28); apply.isEnabled = !selectedThreads.isEmpty
            apply.identifier = NSUserInterfaceItemIdentifier("batch-apply")
            view.addSubview(apply)
        }
        scroll.frame = NSRect(x: 10, y: batchMode ? 229 : 193, width: panelWidth - 20, height: viewport)
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
            let target = batchTarget(record.event)
            let row = CueRecentRowButton(record: record, width: document.frame.width - (batchMode ? 28 : 0), isRunning: isRunning)
            row.refresh(record: record, isRunning: isRunning, recovery: snapshot.recoveryByThread[record.event.threadID], pending: history.followups.contains(record.event.threadID))
            row.frame.origin.y = position
            if batchMode { row.frame.origin.x += 28; batchCheckbox(target, y: position, in: document) }
            position += CueTaskRowLayout.pitch
            row.target = self; row.action = #selector(openRow(_:))
            let id = record.event.id
            row.identifier = NSUserInterfaceItemIdentifier(id); row.selected = batchMode ? selectedThreads.contains(record.event.threadID) : selectedID == id
            row.setAccessibilityValue(row.selected ? "已选中" : "")
            row.selectRow = { [weak self] in self?.select(id: id) }
            row.contextMenu = { [weak self] in self?.contextMenu(id: id) ?? NSMenu() }
            addDetails(to: row, threadID: record.event.threadID, title: record.event.title, eventDate: record.event.date)
            row.hoverChanged = { [weak self, weak row] inside in if let row { self?.hover(inside, threadID: record.event.threadID, title: record.event.title, row: row) } }
            document.addSubview(row)
        }
        func followupRow(_ followup: CueThreadFollowup) {
            let row = CueRunningRowButton(followup: followup, width: document.frame.width - (batchMode ? 28 : 0))
            row.refreshFollowup(followup, running: snapshot.activeThreadIDs.contains(followup.threadID), recovery: snapshot.recoveryByThread[followup.threadID])
            row.frame.origin.y = position
            if batchMode { row.frame.origin.x += 28; batchCheckbox(CueBatchTarget(threadID: followup.threadID, title: followup.title), y: position, in: document) }
            position += CueTaskRowLayout.pitch
            row.target = self; row.action = #selector(openRunningRow(_:)); row.selected = batchMode ? selectedThreads.contains(followup.threadID) : selectedID == followup.navigationID
            row.selectRow = { [weak self] in self?.select(id: followup.navigationID) }
            row.contextMenu = { [weak self] in self?.navigationContextMenu(followup.event, followup: followup) ?? NSMenu() }
            addDetails(to: row, threadID: followup.threadID, title: followup.title, eventDate: nil)
            row.hoverChanged = { [weak self, weak row] inside in if let row { self?.hover(inside, threadID: followup.threadID, title: followup.title, row: row) } }
            document.addSubview(row)
        }
        if !snapshot.pinned.isEmpty || !snapshot.pinnedFollowups.isEmpty {
            section("置顶提醒", id: "pinned", count: snapshot.pinned.count + snapshot.pinnedFollowups.count, hint: "未读优先 · 不占历史额度", color: .systemBlue)
            snapshot.pinned.filter { !$0.isOpened }.forEach(reminderRow)
            snapshot.pinnedFollowups.filter(\.unread).forEach(followupRow)
            snapshot.pinned.filter(\.isOpened).forEach(reminderRow)
            snapshot.pinnedFollowups.filter { !$0.unread }.forEach(followupRow)
        }
        if !snapshot.running.isEmpty {
            section(batchMode ? "本次运行范围" : "正在运行", id: "running", count: snapshot.running.count, hint: batchMode ? "范围固定 · 状态持续更新" : "标题 A–Z · 点击查看对话", color: .systemGreen)
            for task in snapshot.running {
                let row = CueRunningRowButton(task: task, width: document.frame.width - (batchMode ? 28 : 0))
                row.refresh(task: task, detail: taskDetails[task.threadID])
                row.frame.origin.y = position
                if batchMode { row.frame.origin.x += 28; batchCheckbox(CueBatchTarget(threadID: task.threadID, title: task.title), y: position, in: document) }
                position += CueTaskRowLayout.pitch
                row.target = self; row.action = #selector(openRunningRow(_:)); row.selected = batchMode ? selectedThreads.contains(task.threadID) : selectedID == task.navigationID
                row.setAccessibilityValue(row.selected ? "已选中" : "")
                row.selectRow = { [weak self] in self?.select(id: task.navigationID) }
                row.contextMenu = { [weak self] in self?.navigationContextMenu(task.ephemeralCueEvent) ?? NSMenu() }
                addDetails(to: row, threadID: task.threadID, title: task.title, eventDate: nil)
                row.hoverChanged = { [weak self, weak row] inside in if let row { self?.hover(inside, threadID: task.threadID, title: task.title, row: row) } }
                document.addSubview(row)
            }
        }
        if !snapshot.interrupted.isEmpty {
            section("中断待续", id: "interrupted", count: snapshot.interrupted.count, hint: "记录保留 · 点击打开后手动继续", color: .systemOrange)
            for task in snapshot.interrupted {
                let row = CueRunningRowButton(task: task, width: document.frame.width - (batchMode ? 28 : 0))
                row.refresh(task: task, detail: taskDetails[task.threadID]); row.frame.origin.y = position
                if batchMode { row.frame.origin.x += 28; batchCheckbox(CueBatchTarget(threadID: task.threadID, title: task.title), y: position, in: document) }
                position += CueTaskRowLayout.pitch
                row.target = self; row.action = #selector(openRunningRow(_:)); row.selected = batchMode ? selectedThreads.contains(task.threadID) : selectedID == task.navigationID
                row.selectRow = { [weak self] in self?.select(id: task.navigationID) }
                row.contextMenu = { [weak self] in self?.recoveryContextMenu(task) ?? NSMenu() }
                addDetails(to: row, threadID: task.threadID, title: task.title, eventDate: nil)
                row.hoverChanged = { [weak self, weak row] inside in if let row { self?.hover(inside, threadID: task.threadID, title: task.title, row: row) } }
                document.addSubview(row)
            }
        }
        if !snapshot.followups.isEmpty {
            section("待续", id: "followup", count: snapshot.followups.count, hint: "已查看 · 留到方便时继续处理", color: .systemPurple)
            snapshot.followups.forEach(followupRow)
        }
        if !snapshot.ordinary.isEmpty {
            section("历史提醒", id: "history", count: snapshot.ordinary.count, hint: "已打开 · 右键可重新标为未读")
            snapshot.ordinary.forEach(reminderRow)
        }
        if snapshot.rowCount == 0 {
            let empty = NSTextField(wrappingLabelWithString: "正在运行、被中断的任务与新的提醒会显示在这里。\n中断记录保留到实际续跑或完成，也可右键移除。")
            empty.frame = NSRect(x: 24, y: 41, width: 490, height: 62)
            empty.font = .systemFont(ofSize: 12); empty.textColor = .secondaryLabelColor; document.addSubview(empty)
        }
        recentDocument = document; scroll.documentView = document; view.addSubview(scroll)
        scroll.contentView.scroll(to: scroll.contentView.constrainBoundsRect(NSRect(origin: origin, size: scroll.contentView.bounds.size)).origin)
        scroll.reflectScrolledClipView(scroll.contentView)
        let footer = label(footerText, NSRect(x: 22, y: height - 20, width: showsConnectionAction ? panelWidth - 168 : panelWidth - 44, height: 16), size: 10, color: footerColor)
        footer.identifier = NSUserInterfaceItemIdentifier("panel-footer"); footer.toolTip = footerText
        // Keep this control available for an in-place health transition while
        // a person is selecting or hovering, but hide it during auto recovery.
        let connectionAction = button(connectionSummary == nil ? "接通 Codex…" : "连接诊断…", NSRect(x: panelWidth - 142, y: height - 25, width: 118, height: 24), #selector(connectCodex))
        connectionAction.identifier = NSUserInterfaceItemIdentifier("connect-codex"); connectionAction.isHidden = !showsConnectionAction
        reconcilePointerHover()
    }

    /// Preserve controls and row geometry while a person is selecting or reading.
    /// Telemetry is still refreshed; only membership and ordering are deferred.
    private func refreshInPlace() {
        let latest = CueStatusListSnapshot(records: history.records, runningTasks: runningTasks, interruptedTasks: interruptedTasks, followups: history.followups.records)
        var tasks: [String: CueRunningTask] = [:]
        for task in runningTasks {
            if let previous = tasks[task.threadID], previous.updatedDate >= task.updatedDate { continue }
            tasks[task.threadID] = task
        }
        for task in interruptedTasks where tasks[task.threadID] == nil { tasks[task.threadID] = task }
        let records = Dictionary(uniqueKeysWithValues: history.records.map { ($0.event.id, $0) })
        for child in view.subviews {
            if let field = child as? NSTextField {
                switch field.identifier?.rawValue {
                case "monitor-status": field.stringValue = status
                case "task-counts":
                    field.stringValue = batchMode ? "已选 \(selectedThreads.count) 个对话 · 已去重" : "\(history.unopenedCount) 未读 · \(latest.activeCount) 运行 · \(latest.recoveryByThread.count) 中断 · \(latest.deferredCount) 待续 · 历史 \(history.ordinaryCount)/50"
                    field.textColor = history.unopenedCount > 0 || batchMode ? .systemBlue : .secondaryLabelColor
                case "panel-footer":
                    field.stringValue = footerText; field.textColor = footerColor
                    field.frame.size.width = showsConnectionAction ? panelWidth - 168 : panelWidth - 44
                    field.toolTip = field.stringValue
                default: break
                }
            } else if let button = child as? NSButton {
                switch button.identifier?.rawValue {
                case "setting-enabled": button.state = enabled ? .on : .off
                case "setting-sound": button.state = soundEnabled ? .on : .off
                case "pause-button": button.title = paused ? "恢复提醒" : "暂停 30 分钟"
                case "connect-codex": button.isHidden = !showsConnectionAction; button.title = connectionSummary == nil ? "接通 Codex…" : "连接诊断…"
                case "batch-apply": button.title = "批量操作 (\(selectedThreads.count))"; button.isEnabled = !selectedThreads.isEmpty
                case "batch-scope-pinned", "batch-scope-running":
                    let targets = button.identifier?.rawValue == "batch-scope-pinned" ? frozenPinnedTargets : frozenRunningTargets
                    let ids = Set(targets.map(\.threadID))
                    button.state = ids.isEmpty || selectedThreads.isDisjoint(with: ids) ? .off : ids.isSubset(of: selectedThreads) ? .on : .mixed
                    button.isEnabled = !ids.isEmpty
                default: break
                }
            }
        }
        for child in recentDocument?.subviews ?? [] {
            if let row = child as? CueRecentRowButton {
                row.refresh(record: records[row.record.event.id], isRunning: row.record.event.provider == "codex" && latest.activeThreadIDs.contains(row.record.event.threadID), recovery: latest.recoveryByThread[row.record.event.threadID], pending: history.followups.contains(row.record.event.threadID))
                refreshDetails(in: row, threadID: row.record.event.threadID, eventDate: row.record.event.date)
                if batchMode { row.selected = selectedThreads.contains(row.record.event.threadID); row.setAccessibilityValue(row.selected ? "已选中" : "") }
            } else if let row = child as? CueRunningRowButton {
                if let followup = history.followups.records.first(where: { $0.threadID == row.followup?.threadID }) {
                    row.refreshFollowup(followup, running: latest.activeThreadIDs.contains(followup.threadID), recovery: latest.recoveryByThread[followup.threadID])
                } else { row.refresh(task: tasks[row.task.threadID], detail: taskDetails[row.task.threadID]) }
                refreshDetails(in: row, threadID: row.task.threadID, eventDate: nil)
                if batchMode { row.selected = selectedThreads.contains(row.task.threadID); row.setAccessibilityValue(row.selected ? "已选中" : "") }
            } else if let checkbox = child as? NSButton, let id = checkbox.identifier?.rawValue {
                checkbox.state = selectedThreads.contains(id) ? .on : .off
            }
        }
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
        results["running_deduplicates_threads_using_latest_activity"] = snapshot.activeCount == 3 && snapshot.running.first(where: { $0.threadID == "active-thread" })?.title == "新活动"
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
        var batchCalls: [[CueBatchTarget]] = []
        var batchActions = liveActions; batchActions.batchConfigure = { batchCalls.append($0) }
        let batchController = CueStatusPopoverController(history: liveHistory, actions: batchActions)
        batchController.update(status: "批量测试", enabled: true, soundEnabled: true, paused: false, runningTasks: tasks)
        _ = batchController.view; batchController.toggleBatchMode(); batchController.selectPinned(); batchController.selectRunning()
        results["batch_group_selection_deduplicates_pinned_and_running_threads"] = batchController.selectedThreads.count == 4
        let batchRow = batchController.recentDocument?.subviews.compactMap { $0 as? CueRecentRowButton }.first { $0.record.event.id == "pin-unread" }
        if let row = batchRow { batchController.openRow(row) }
        results["batch_row_click_toggles_without_opening_conversation"] = !batchController.selectedThreads.contains("shared-pinned") && navigationEvents.count == 1
        batchController.configureSelected()
        results["batch_dialog_receives_unique_selected_threads_only"] = batchCalls.count == 1 && batchCalls[0].count == 3 && Set(batchCalls[0].map(\.threadID)) == batchController.selectedThreads
        let selection = batchController.selectedThreads
        batchController.update(status: "后台状态刷新", enabled: true, soundEnabled: true, paused: false, runningTasks: [])
        results["batch_selection_survives_task_completion_refresh"] = batchController.selectedThreads == selection
        batchController.toggleBatchMode()
        results["batch_exit_clears_selection_without_touching_ledger"] = batchController.selectedThreads.isEmpty && liveHistory.records.count == 4
        let stable = CueStatusPopoverController(history: liveHistory, actions: batchActions)
        // This fixture intentionally drives row enter/leave directly. It must
        // not race a person's unrelated real pointer during the delayed tip.
        stable.pointerLocation = { nil }
        stable.update(status: "稳定选择测试", enabled: true, soundEnabled: true, paused: false, runningTasks: tasks)
        _ = stable.view; stable.toggleBatchMode(); stable.selectRunning()
        let stableCheckbox = stable.recentDocument?.subviews.compactMap { $0 as? NSButton }.first { $0.identifier?.rawValue == "active-thread" }
        let stableRow = stable.recentDocument?.subviews.compactMap { $0 as? CueRunningRowButton }.first { $0.task.threadID == "active-thread" }
        let stableMetrics = stableRow?.subviews.compactMap { $0 as? NSTextField }.first { $0.identifier?.rawValue == "task-metrics:active-thread" }
        let stableUsage = stableRow?.subviews.compactMap { $0 as? NSTextField }.first { $0.identifier?.rawValue == "usage-turn-tokens" }
        let checkboxFrame = stableCheckbox?.frame
        let selectedBeforeRefresh = stable.selectedThreads
        let navigationBeforeRefresh = navigationEvents.count
        let completed = CueTaskDetail(raw: ["status": "complete", "started_at": 1000.0, "finished_at": 1060.0, "elapsed_status": "complete", "total_tokens": 2345, "tokens_status": "complete", "messages": [["role": "assistant", "text": "稳定刷新验收回复"]]])
        stable.update(status: "后台更新", enabled: true, soundEnabled: true, paused: false, runningTasks: [running("new-background")], taskDetails: ["active-thread": completed])
        let updatedCheckbox = stable.recentDocument?.subviews.compactMap { $0 as? NSButton }.first { $0.identifier?.rawValue == "active-thread" }
        let updatedMetrics = stableRow?.subviews.compactMap { $0 as? NSTextField }.first { $0.identifier?.rawValue == "task-metrics:active-thread" }
        results["batch_background_refresh_preserves_checkbox_identity_and_frame"] = stableCheckbox != nil && stableCheckbox === updatedCheckbox && stableCheckbox?.frame == checkboxFrame
        results["batch_background_refresh_preserves_metrics_identity_and_updates_value"] = stableMetrics != nil && stableMetrics === updatedMetrics && stableMetrics?.stringValue == completed.modelSelectionLine
        let updatedUsage = stableRow?.subviews.compactMap { $0 as? NSTextField }.first { $0.identifier?.rawValue == "usage-turn-tokens" }
        results["batch_background_refresh_updates_usage_without_replacing_the_column"] = stableUsage != nil && stableUsage === updatedUsage && updatedUsage?.stringValue == completed.turnUsage.tokenLine
        results["batch_completed_task_keeps_selection_and_shows_completion_in_place"] = stable.selectedThreads == selectedBeforeRefresh && stableRow?.subviews.compactMap { $0 as? NSTextField }.first { $0.identifier?.rawValue == "row-state" }?.stringValue == "已完成"
        stable.clearBatchSelection(); stable.selectRunning()
        results["batch_selection_scope_stays_frozen_until_exit"] = stable.selectedThreads == selectedBeforeRefresh && !stable.selectedThreads.contains("new-background") && stableCheckbox === updatedCheckbox
        results["batch_background_refresh_never_opens_a_conversation"] = navigationEvents.count == navigationBeforeRefresh
        stable.toggleBatchMode()
        results["batch_exit_rebuilds_latest_running_membership"] = stable.recentDocument?.subviews.compactMap { $0 as? CueRunningRowButton }.map { $0.task.threadID } == ["new-background"] && stable.selectedThreads.isEmpty
        let orderA = [running("a", time: 2000, title: "ALV｜UCP"), running("b", time: 1000, title: "ALV｜AOC"), running("c", time: 3000, title: "ALV｜第二代")]
        let orderB = [running("c", time: 5000, title: "ALV｜第二代"), running("b", time: 4000, title: "ALV｜AOC"), running("a", time: 3000, title: "ALV｜UCP")]
        results["running_order_is_title_ascending_despite_time_or_input_changes"] = CueStatusListSnapshot(records: [], runningTasks: orderA).running.map(\.threadID) == ["b", "c", "a"] && CueStatusListSnapshot(records: [], runningTasks: orderB).running.map(\.threadID) == ["b", "c", "a"]
        let hoverDetail = CueTaskDetail(raw: ["status": "running", "started_at": 1000.0, "elapsed_status": "partial", "total_tokens": 789, "tokens_status": "partial", "messages": [["role": "assistant", "text": "隔离悬停验收内容"]]])
        stable.update(status: "悬停测试", enabled: true, soundEnabled: true, paused: false, runningTasks: [running("new-background")], taskDetails: ["new-background": hoverDetail])
        let hoverWindow = NSWindow(contentRect: stable.view.bounds, styleMask: .borderless, backing: .buffered, defer: false)
        hoverWindow.contentView = stable.view
        let hoverRow = stable.recentDocument?.subviews.compactMap { $0 as? CueRunningRowButton }.first { $0.task.threadID == "new-background" }
        if let hoverRow { stable.hover(true, threadID: "new-background", title: "隔离悬停验收", row: hoverRow) }
        stable.update(status: "悬停等待期间刷新", enabled: true, soundEnabled: true, paused: false, runningTasks: [running("new-background")], taskDetails: ["new-background": hoverDetail])
        results["pending_hover_keeps_anchor_during_background_refresh"] = hoverRow === stable.recentDocument?.subviews.compactMap { $0 as? CueRunningRowButton }.first { $0.task.threadID == "new-background" }
        RunLoop.current.run(until: Date().addingTimeInterval(0.52))
        func visiblePreviewText(_ view: NSView?) -> String {
            guard let view else { return "" }
            return ((view as? NSTextView)?.string ?? "") + view.subviews.map { visiblePreviewText($0) }.joined(separator: "\n")
        }
        let previewBody = NSApplication.shared.windows.filter { $0.isVisible && $0.className.contains("CueTaskHoverPanel") }.map { visiblePreviewText($0.contentView) }.joined()
        results["row_hover_opens_delayed_native_preview_with_current_provider"] = stable.hoverController.isVisible && previewBody.contains("隔离悬停验收内容")
        stable.update(status: "悬停后台更新", enabled: true, soundEnabled: true, paused: false, runningTasks: [], taskDetails: ["new-background": completed])
        let afterHoverRow = stable.recentDocument?.subviews.compactMap { $0 as? CueRunningRowButton }.first { $0.task.threadID == "new-background" }
        results["hover_background_refresh_preserves_row_identity_and_preview"] = hoverRow != nil && hoverRow === afterHoverRow && stable.hoverController.isVisible
        let observedMetadata = hoverRow?.subviews.compactMap { $0 as? NSTextField }.first { $0.identifier?.rawValue == "row-metadata" }
        if let hoverRow { hoverRow.refresh(task: running("new-background"), detail: hoverDetail) }
        results["running_partial_time_labels_observation_start"] = observedMetadata?.stringValue.contains("观测起点") == true
        stable.hoverController.hide(); hoverWindow.orderOut(nil)
        func lost(_ id: String, status: String = "interrupted") -> CueRunningTask {
            CueRunningTask(raw: ["thread_id": id, "title": "中断样例 " + id, "provider": "codex", "started_at": 1000.0, "last_activity": 1060.0, "interrupted_at": 1070.0, "status": status, "reason_text": "Codex 进程已结束", "turn_id": "old-turn"])!
        }
        let lostTasks = [lost("lost-thread"), lost("shared-pinned"), lost("lost-thread", status: "unconfirmed")]
        let recoverySnapshot = CueStatusListSnapshot(records: liveHistory.records, runningTasks: tasks, interruptedTasks: lostTasks)
        results["recovery_snapshot_deduplicates_and_running_wins"] = recoverySnapshot.recoveryByThread.count == 1 && recoverySnapshot.interrupted.count == 1 && recoverySnapshot.recoveryByThread["shared-pinned"] == nil
        var dismissals: [String] = [], recoveryNavigation: [String] = []
        let recoveryActions = CueStatusPopoverActions(openSettings: {}, openUsage: {}, toggleEnabled: {}, toggleSound: {}, pause: {}, demo: {}, quit: {}, openEvent: { recoveryNavigation.append($0.threadID) }, dismissInterrupted: { dismissals.append($0.threadID) })
        let recoveryPanel = CueStatusPopoverController(history: liveHistory, actions: recoveryActions)
        recoveryPanel.update(status: "中断恢复测试", enabled: true, soundEnabled: true, paused: false, runningTasks: [running("active-thread")], interruptedTasks: lostTasks)
        _ = recoveryPanel.view
        let recoverySections = recoveryPanel.recentDocument?.subviews.compactMap(\.identifier).map(\.rawValue).filter { $0.hasPrefix("section-") }
        results["recovery_section_keeps_pinned_running_interrupted_history_order"] = recoverySections == ["section-pinned", "section-running", "section-interrupted", "section-history"]
        let lossRow = recoveryPanel.recentDocument?.subviews.compactMap { $0 as? CueRunningRowButton }.first { $0.task.threadID == "lost-thread" }
        if let lossRow { recoveryPanel.openRunningRow(lossRow) }
        results["opening_recovery_only_navigates_and_keeps_record"] = recoveryNavigation == ["lost-thread"] && recoveryPanel.interruptedTasks.count == 3 && dismissals.isEmpty
        let recoveryMenu = recoveryPanel.recoveryContextMenu(lost("lost-thread"))
        if let item = recoveryMenu.items.first(where: { $0.title == "移除此中断记录" }) { recoveryPanel.dismissRecoveryFromMenu(item) }
        results["recovery_dismissal_targets_selected_turn_only"] = dismissals == ["lost-thread"] && recoveryNavigation.count == 1
        let pinnedLoss = recoveryPanel.recentDocument?.subviews.compactMap { $0 as? CueRecentRowButton }.first { $0.record.event.threadID == "shared-pinned" }
        results["pinned_interrupted_task_has_special_state_without_duplicate_row"] = pinnedLoss?.subviews.compactMap { $0 as? NSTextField }.first { $0.identifier?.rawValue == "row-state" }?.stringValue == "中断待续" && recoveryPanel.recentDocument?.subviews.compactMap { $0 as? CueRunningRowButton }.filter { $0.task.threadID == "shared-pinned" }.isEmpty == true
        let manyLost = (0..<70).map { lost("lost-\($0)") }
        let retained = CueStatusListSnapshot(records: liveHistory.records, runningTasks: [], interruptedTasks: manyLost)
        results["interrupted_tasks_do_not_use_ordinary_fifty_record_capacity"] = retained.interrupted.count == 70 && liveHistory.ordinaryCount == 2 && liveHistory.records.count == 4
        let followHome = home.appendingPathComponent("followup-panel")
        let followHistory = CueRecentHistory(home: followHome)
        let followEvent = CueEvent(runningThreadID: "follow-one", title: "晚些时候继续", date: Date())
        followHistory.beginReview(event: followEvent)
        var pathCalls: [String] = [], externalCalls: [String] = []
        var followActions = liveActions
        followActions.threadAction = { id, command in pathCalls.append(id + ":" + command) }
        followActions.openInCodex = { externalCalls.append($0.threadID) }
        let followPanel = CueStatusPopoverController(history: followHistory, actions: followActions)
        followPanel.update(status: "待续测试", enabled: true, soundEnabled: true, paused: false)
        _ = followPanel.view
        let followRows = followPanel.recentDocument?.subviews.compactMap { $0 as? CueRunningRowButton }.filter { $0.followup != nil }
        results["followup_has_its_own_visible_section"] = followRows?.count == 1 && followPanel.recentDocument?.subviews.contains(where: { $0.identifier?.rawValue == "section-followup" }) == true
        let fullMenu = followPanel.navigationContextMenu(followEvent, followup: followHistory.followups.records.first)
        let fileMenu = fullMenu.items.first(where: { $0.title == "项目与会话文件" })?.submenu
        results["all_navigation_rows_expose_copy_and_finder_actions"] = ["复制标题", "复制对话 ID", "复制对话链接", "查看对话", "在 Codex 中打开", "标记处理完成"].allSatisfy { title in fullMenu.items.contains { $0.title == title } } && fileMenu?.items.contains(where: { $0.title == "在访达中打开项目文件夹" }) == true
        if let item = fileMenu?.items.first(where: { $0.title == "复制项目路径" }) { followPanel.pathActionFromMenu(item) }
        if let item = fullMenu.items.first(where: { $0.title == "在 Codex 中打开" }) { followPanel.openCodexFromMenu(item) }
        results["context_path_and_external_navigation_retain_original_target"] = pathCalls == ["follow-one:copy_project_path"] && externalCalls == ["follow-one"] && followHistory.followups.contains("follow-one")
        followHistory.followups.setPinned(threadID: "follow-one", pinned: true)
        let pinnedSnapshot = CueStatusListSnapshot(records: [], runningTasks: [running("follow-one")], interruptedTasks: [], followups: followHistory.followups.records)
        results["pinned_followup_deduplicates_live_running_row"] = pinnedSnapshot.pinnedFollowups.count == 1 && pinnedSnapshot.running.isEmpty && pinnedSnapshot.followups.isEmpty
        results["coordinated_running_chat_is_not_counted_as_deferred_and_keeps_bookmark"] = pinnedSnapshot.deferredCount == 0 && followHistory.followups.contains("follow-one")
        let visible = followRows?.first
        visible?.updateTrackingAreas()
        results["inactive_status_popover_rows_receive_hover_tracking"] = visible?.trackingAreas.contains(where: { $0.options.contains(.activeAlways) }) == true
        let geometry = CueStatusPopoverController(history: followHistory, actions: followActions)
        geometry.pointerLocation = { nil }
        geometry.update(status: "无点击悬停测试", enabled: true, soundEnabled: true, paused: false, runningTasks: [running("first-hover")], taskDetails: ["first-hover": hoverDetail])
        let geometryView = geometry.view
        // This owned, off-screen, nonactivating fixture never focuses or moves
        // another app's windows and never moves the physical mouse pointer.
        let geometryWindow = NSPanel(contentRect: NSRect(x: -10000, y: -10000, width: geometryView.bounds.width, height: geometryView.bounds.height), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        geometryWindow.isReleasedWhenClosed = false; geometryWindow.contentView = geometryView
        let keyBefore = NSApp.keyWindow, activeBefore = NSApp.isActive
        geometryWindow.orderFrontRegardless()
        let geometryRow = geometry.recentDocument?.subviews.compactMap { $0 as? CueRunningRowButton }.first { $0.task.threadID == "first-hover" }
        if let row = geometryRow {
            let center = row.convert(NSPoint(x: row.bounds.midX, y: row.bounds.midY), to: nil)
            let virtualPointer = geometryWindow.convertPoint(toScreen: center)
            geometry.pointerLocation = { virtualPointer }; geometry.reconcilePointerHover()
        }
        results["nonkey_popover_first_pointer_entry_starts_hover_without_click"] = geometryRow != nil && geometry.pointerAnchor === geometryRow && geometry.hoverController.isEngaged && !geometryWindow.isKeyWindow && NSApp.keyWindow === keyBefore && NSApp.isActive == activeBefore
        geometry.pointerLocation = { NSPoint(x: 100000, y: 100000) }; geometry.reconcilePointerHover()
        results["nonkey_pointer_exit_releases_pending_hover"] = geometry.pointerAnchor == nil && !geometry.hoverController.isEngaged
        geometry.hoverController.hide(); geometryWindow.orderOut(nil); geometry.pointerTimer?.invalidate(); geometry.pointerTimer = nil
        let clockHistory = CueRecentHistory(home: home.appendingPathComponent("live-second-clock"))
        let clockNow = Date(timeIntervalSince1970: floor(Date().timeIntervalSince1970))
        for id in ["clock-pin", "clock-done", "clock-wait"] {
            let raw: [String: Any] = ["id": id, "kind": "complete", "title": id, "thread_id": id, "created_at": clockNow.addingTimeInterval(-70).timeIntervalSince1970]
            clockHistory.append(CueEvent(try! JSONSerialization.data(withJSONObject: raw))!)
            if id != "clock-pin" { clockHistory.markOpened(id: id, didOpen: true) }
        }
        clockHistory.followups.retain(threadID: "clock-follow", title: "待续运行", now: clockNow)
        clockHistory.followups.setPinned(threadID: "clock-follow", pinned: true)
        let clockTasks = ["clock-pin", "clock-follow", "clock-run"].map { id in
            CueRunningTask(raw: ["thread_id": id, "title": id, "started_at": clockNow.addingTimeInterval(-65).timeIntervalSince1970, "last_activity": clockNow.timeIntervalSince1970])!
        }
        let clockDetail = CueTaskDetail(raw: ["status": "running", "started_at": clockNow.addingTimeInterval(-65).timeIntervalSince1970, "last_activity": clockNow.timeIntervalSince1970, "elapsed_status": "complete", "total_tokens": 1024, "tokens_status": "complete"])
        let clockDone = CueTaskDetail(raw: ["status": "complete", "started_at": clockNow.addingTimeInterval(-65).timeIntervalSince1970, "finished_at": clockNow.addingTimeInterval(-5).timeIntervalSince1970, "elapsed_status": "complete"])
        let clockWait = CueTaskDetail(raw: ["status": "waiting", "started_at": clockNow.addingTimeInterval(-65).timeIntervalSince1970, "last_activity": clockNow.addingTimeInterval(-5).timeIntervalSince1970, "elapsed_seconds": 65, "elapsed_status": "complete"])
        let clockLost = CueRunningTask(raw: ["thread_id": "clock-lost", "started_at": clockNow.addingTimeInterval(-65).timeIntervalSince1970, "last_activity": clockNow.addingTimeInterval(-5).timeIntervalSince1970, "status": "interrupted"])!
        let clock = CueStatusPopoverController(history: clockHistory, actions: liveActions)
        clock.pointerLocation = { nil }
        clock.update(status: "秒级时钟验收", enabled: true, soundEnabled: true, paused: false, runningTasks: clockTasks, interruptedTasks: [clockLost], taskDetails: ["clock-pin": clockDetail, "clock-follow": clockDetail, "clock-done": clockDone, "clock-wait": clockWait])
        _ = clock.view
        func clockField(_ id: String) -> NSTextField? {
            clock.recentDocument?.subviews.flatMap(\.subviews).compactMap { $0 as? NSTextField }.first { $0.identifier?.rawValue == "task-elapsed:" + id }
        }
        let clockDocument = clock.recentDocument
        let pinTime = clockField("clock-pin"), runTime = clockField("clock-run"), followTime = clockField("clock-follow")
        let pinBefore = pinTime?.stringValue, runBefore = runTime?.stringValue, followBefore = followTime?.stringValue
        let doneBefore = clockField("clock-done")?.stringValue, waitBefore = clockField("clock-wait")?.stringValue, lostBefore = clockField("clock-lost")?.stringValue
        clock.select(id: "clock-pin")
        let clockWindow = NSPanel(contentRect: NSRect(x: -10000, y: -10000, width: clock.view.bounds.width, height: clock.view.bounds.height), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        clockWindow.isReleasedWhenClosed = false; clockWindow.contentView = clock.view; clockWindow.orderFrontRegardless()
        clock.viewDidAppear()
        let originBeforeClock = clock.scroll.contentView.bounds.origin
        RunLoop.current.run(until: Date().addingTimeInterval(1.15))
        results["visible_clock_ticks_each_second_for_pinned_running_and_followup_rows"] = pinBefore != pinTime?.stringValue && runBefore != runTime?.stringValue && followBefore != followTime?.stringValue
        results["second_clock_keeps_waiting_completed_and_interrupted_times_fixed"] = doneBefore == clockField("clock-done")?.stringValue && waitBefore == clockField("clock-wait")?.stringValue && lostBefore == clockField("clock-lost")?.stringValue
        results["second_clock_preserves_document_row_identity_selection_and_scroll"] = clock.recentDocument === clockDocument && pinTime === clockField("clock-pin") && runTime === clockField("clock-run") && followTime === clockField("clock-follow") && clock.selectedID == "clock-pin" && clock.scroll.contentView.bounds.origin == originBeforeClock
        let stateFrame = pinTime?.superview?.subviews.first(where: { $0.identifier?.rawValue == "row-state" })?.frame ?? .zero
        results["row_elapsed_stays_under_state_in_left_information_area"] = pinTime?.alignment == .right && pinTime?.frame.maxX == stateFrame.maxX && (pinTime?.frame.minY ?? 0) >= stateFrame.maxY && (pinTime?.frame.minX ?? 0) > 300
        results["list_clock_uses_common_runloop_timer_only_while_visible"] = clock.elapsedTimer?.isValid == true && clock.elapsedTimer?.timeInterval == 1
        let staleComplete = CueTaskDetail(raw: ["status": "complete", "started_at": clockNow.addingTimeInterval(-1000).timeIntervalSince1970, "finished_at": clockNow.addingTimeInterval(-500).timeIntervalSince1970])
        clock.taskDetails["clock-run"] = staleComplete
        results["new_turn_clock_ignores_previous_turn_completed_details"] = clock.durationLine(threadID: "clock-run", eventDate: nil, now: clockNow) == "本轮 ≥1分5秒"
        clock.viewDidDisappear(); clockWindow.orderOut(nil)
        results["hidden_list_invalidates_second_clock"] = clock.elapsedTimer == nil
        let reconnect = CueStatusPopoverController(history: liveHistory, actions: liveActions)
        reconnect.pointerLocation = { nil }
        reconnect.update(status: "连接状态验收", enabled: true, soundEnabled: true, paused: false, connectionSummary: "Codex 自动重连中…", connectionState: "reconnecting", showConnectionAction: false)
        _ = reconnect.view
        let connectionFooter = reconnect.view.subviews.compactMap { $0 as? NSTextField }.first { $0.identifier?.rawValue == "panel-footer" }
        let connectionAction = reconnect.view.subviews.compactMap { $0 as? NSButton }.first { $0.identifier?.rawValue == "connect-codex" }
        results["automatic_reconnect_footer_never_requires_manual_codex_exit"] = connectionFooter?.stringValue == "Codex 自动重连中…" && connectionAction?.isHidden == true
        reconnect.toggleBatchMode()
        let stableFooter = reconnect.view.subviews.compactMap { $0 as? NSTextField }.first { $0.identifier?.rawValue == "panel-footer" }
        reconnect.update(status: "连接状态验收", enabled: true, soundEnabled: true, paused: false, connectionSummary: "Codex 已自动连接 · 回复可用；批准提醒仍在恢复", connectionState: "connected", showConnectionAction: false)
        results["connection_recovery_updates_in_place_without_claiming_all_capabilities"] = stableFooter === reconnect.view.subviews.compactMap { $0 as? NSTextField }.first { $0.identifier?.rawValue == "panel-footer" } && stableFooter?.stringValue == "Codex 已自动连接 · 回复可用；批准提醒仍在恢复" && reconnect.view.subviews.compactMap { $0 as? NSButton }.first { $0.identifier?.rawValue == "connect-codex" }?.isHidden == true
        reconnect.update(status: "连接状态验收", enabled: true, soundEnabled: true, paused: false, connectionSummary: "当前 Codex 版本尚未适配", connectionState: "unsupported", showConnectionAction: true)
        results["unsupported_connection_offers_diagnostics_instead_of_restart_prompt"] = reconnect.view.subviews.compactMap { $0 as? NSButton }.first { $0.identifier?.rawValue == "connect-codex" }?.title == "连接诊断…" && reconnect.view.subviews.compactMap { $0 as? NSButton }.first { $0.identifier?.rawValue == "connect-codex" }?.isHidden == false && stableFooter?.stringValue == "当前 Codex 版本尚未适配"
        let quickHistory = CueRecentHistory(home: home.appendingPathComponent("row-quick-actions"))
        let quickRaw: [String: Any] = ["id": "quick-history", "kind": "complete", "title": "历史快捷操作", "thread_id": "quick-history-thread", "created_at": 1000.0]
        quickHistory.append(CueEvent(try! JSONSerialization.data(withJSONObject: quickRaw))!)
        quickHistory.markOpened(id: "quick-history", didOpen: true)
        quickHistory.followups.retain(threadID: "quick-follow", title: "待续快捷操作")
        var quickExternal: [String] = [], quickLocal = 0
        var quickActions = CueStatusPopoverActions(openSettings: {}, openUsage: {}, toggleEnabled: {}, toggleSound: {}, pause: {}, demo: {}, quit: {}, openEvent: { _ in quickLocal += 1 })
        quickActions.openInCodex = { quickExternal.append($0.threadID) }
        let quick = CueStatusPopoverController(history: quickHistory, actions: quickActions)
        let quickDetail = CueTaskDetail(raw: ["status": "running", "model": "gpt-6.1-sol", "reasoning_effort": "high", "service_tier": "fast", "total_tokens": 12000, "tokens_status": "complete", "turn_usage": ["total_tokens":12000,"tokens_status":"complete","cost_usd":0.12,"cost_cny":0.86,"cost_status":"complete","currency":"both"], "history_usage":["total_tokens":9876543210,"tokens_status":"partial","cost_usd":173456.78,"cost_cny":1234567.89,"cost_status":"partial","currency":"both"]])
        quick.update(status: "快捷操作验收", enabled: true, soundEnabled: true, paused: false, runningTasks: [running("quick-running")], interruptedTasks: [lost("quick-interrupted")], taskDetails: ["quick-running": quickDetail])
        _ = quick.view
        func quickButton(_ row: NSView?, _ action: String) -> CueRowQuickButton? { row?.subviews.compactMap { $0 as? CueRowQuickButton }.first { $0.identifier?.rawValue == "quick-" + action } }
        let quickRows = quick.recentDocument!.subviews.filter { $0 is CueRecentRowButton || $0 is CueRunningRowButton }
        results["every_row_kind_has_four_native_quick_actions"] = quickRows.count == 4 && quickRows.allSatisfy { $0.subviews.compactMap { $0 as? CueRowQuickButton }.count == 4 }
        results["row_accessibility_exposes_all_four_quick_controls"] = quickRows.allSatisfy { row in
            guard let children = row.accessibilityChildren() else { return false }
            return children.count == 4 && children.allSatisfy { ($0 as? CueRowQuickButton)?.isAccessibilityElement() == true }
        }
        let quickReminder = quickRows.compactMap { $0 as? CueRecentRowButton }.first
        if let row = quickReminder, let button = quickButton(row, "open-codex") {
            let point = button.convert(NSPoint(x: button.bounds.midX, y: button.bounds.midY), to: row.superview)
            results["quick_action_hit_target_does_not_become_parent_row"] = row.hitTest(point) === button
            button.invoke?()
        } else { results["quick_action_hit_target_does_not_become_parent_row"] = false }
        results["direct_codex_shortcut_bypasses_local_conversation_viewer"] = quickExternal == ["quick-history-thread"] && quickLocal == 0 && quickHistory.records.first?.isOpened == true
        quick.select(id: "unrelated-selection")
        quickButton(quickReminder, "pin")?.invoke?()
        results["quick_pin_targets_its_row_without_opening_it"] = quickHistory.records.first?.manualPinned == true && quickLocal == 0
        let quickRunning = quick.recentDocument!.subviews.compactMap { $0 as? CueRunningRowButton }.first { $0.task.threadID == "quick-running" }
        let metricsText = quickRunning?.subviews.compactMap { $0 as? NSTextField }.first { $0.identifier?.rawValue == "task-metrics:quick-running" }?.stringValue ?? ""
        let turnTokensText = quickRunning?.subviews.compactMap { $0 as? NSTextField }.first { $0.identifier?.rawValue == "usage-turn-tokens" }?.stringValue ?? ""
        let speedText = quickRunning?.subviews.compactMap { $0 as? NSTextField }.first { $0.identifier?.rawValue == "task-speed:quick-running" }?.stringValue ?? ""
        results["row_separates_tokens_model_strength_and_speed"] = turnTokensText.contains("12.0K") && !metricsText.contains("Tokens") && metricsText.contains("GPT-6.1 Sol") && metricsText.contains("High") && !metricsText.contains("Fast") && speedText == "仅有历史档位"
        func usageField(_ part: String) -> NSTextField? { quickRunning?.subviews.compactMap { $0 as? NSTextField }.first { $0.identifier?.rawValue == part } }
        let turnColumn = usageField("usage-turn-tokens"), historyColumn = usageField("usage-history-tokens")
        let actionFrames = quickRunning?.subviews.compactMap { $0 as? CueRowQuickButton }.map(\.frame) ?? []
        results["row_shortcuts_use_two_compact_rows"] = Set(actionFrames.map(\.minX)).count == 2 && Set(actionFrames.map(\.minY)).count == 2 && actionFrames.allSatisfy { $0.width == 25 && $0.height == 25 }
        results["row_right_columns_are_turn_history_then_shortcuts"] = (turnColumn?.frame.maxX ?? 0) + 12 <= (historyColumn?.frame.minX ?? 0) && (historyColumn?.frame.maxX ?? 0) + 12 <= (actionFrames.map(\.minX).min() ?? 0) && historyColumn?.alignment == .right && turnColumn?.alignment == .right
        let modelField = quickRunning?.subviews.compactMap { $0 as? NSTextField }.first { $0.identifier?.rawValue == "task-metrics:quick-running" }
        results["row_model_and_status_stay_left_of_accounting"] = (modelField?.frame.maxX ?? 0) + 10 < (turnColumn?.frame.minX ?? 0) && (quickRunning?.subviews.first { $0.identifier?.rawValue == "row-state" }?.frame.maxX ?? 0) < (turnColumn?.frame.minX ?? 0)
        let speedField = quickRunning?.subviews.compactMap { $0 as? NSTextField }.first { $0.identifier?.rawValue == "task-speed:quick-running" }
        results["row_speed_badge_is_separate_from_model_and_accounting"] = (modelField?.frame.maxX ?? 0) + 7 < (speedField?.frame.minX ?? 0) && (speedField?.frame.maxX ?? 0) + 10 < (turnColumn?.frame.minX ?? 0)
        results["row_accounting_scopes_have_independent_headers_and_values"] = usageField("usage-turn-label")?.stringValue == "本轮" && usageField("usage-history-label")?.stringValue == "历史累计" && turnColumn?.stringValue == "12.0K Tokens" && historyColumn?.stringValue == "≥9.9B Tokens"
        let historyMoney = usageField("usage-history-cost")
        results["row_full_currency_and_dual_currency_fit_their_column"] = historyMoney?.stringValue.contains("1,234,567.89 ¥") == true && historyMoney?.stringValue.contains("\n") == true && historyMoney.map { field in field.stringValue.components(separatedBy: "\n").allSatisfy { ($0 as NSString).size(withAttributes: [.font: field.font!]).width <= field.frame.width } } == true
        quickButton(quickRunning, "unread")?.invoke?()
        let quickUnread = quickHistory.followups.records.first { $0.threadID == "quick-running" }
        results["running_unread_shortcut_pins_workflow_without_fake_completion"] = quickUnread?.unread == true && quickUnread?.isPinned == true && quickHistory.records.count == 1 && quickLocal == 0
        let pinnedQuickRow = quick.recentDocument!.subviews.compactMap { $0 as? CueRunningRowButton }.first { $0.task.threadID == "quick-running" }
        results["already_unread_shortcut_is_visible_and_disabled"] = quickButton(pinnedQuickRow, "unread")?.isEnabled == false && quickButton(pinnedQuickRow, "unread")?.isHidden == false
        quick.toggleBatchMode()
        results["batch_mode_hides_row_actions_and_preserves_checkbox_hits"] = quick.recentDocument!.subviews.filter { $0 is CueRecentRowButton || $0 is CueRunningRowButton }.allSatisfy { $0.subviews.compactMap { $0 as? CueRowQuickButton }.allSatisfy(\.isHidden) }
        results["batch_mode_omits_hidden_actions_from_accessibility"] = quick.recentDocument!.subviews.filter { $0 is CueRecentRowButton || $0 is CueRunningRowButton }.allSatisfy { $0.accessibilityChildren()?.isEmpty == true }
        let callsBeforeBatch = quickExternal.count
        quickButton(quickReminder, "open-codex")?.invoke?()
        results["stale_quick_actions_do_nothing_in_batch_mode"] = quickExternal.count == callsBeforeBatch && quickLocal == 0
        results.merge(CueSideChat.validationReport()) { _, next in next }
        results.merge(CueSideChatsPopoverController.validationReport()) { _, next in next }
        let sideHistory = CueRecentHistory(home: home.appendingPathComponent("side-chats"))
        for (id, opened, pinned) in [("side-unread", false, false), ("side-pinned", true, true), ("side-history", true, false)] {
            let raw: [String: Any] = ["id": id, "kind": "complete", "title": "父对话 " + id, "thread_id": id, "created_at": 1000.0]
            sideHistory.append(CueEvent(try! JSONSerialization.data(withJSONObject: raw))!)
            if opened { sideHistory.markOpened(id: id, didOpen: true) }
            if pinned { sideHistory.setPinned(id: id, pinned: true) }
        }
        sideHistory.followups.retain(threadID: "side-followup", title: "待续父对话")
        sideHistory.followups.retain(threadID: "side-pinned-followup", title: "置顶待续父对话")
        sideHistory.followups.setPinned(threadID: "side-pinned-followup", pinned: true)
        let sideParents = ["side-unread", "side-pinned", "side-history", "side-followup", "side-pinned-followup", "side-running", "side-interrupted"]
        let sideNow = Date()
        let sideGroups = CueSideChat.grouped(raw: ["state": "connected", "by_parent": Dictionary(uniqueKeysWithValues: sideParents.map { parent in
            (parent, [CueSideChat.fixtureRaw(parent: parent, child: "child-" + parent, title: "核对侧边聊天", status: "running", now: sideNow)])
        })])
        var sideOpened: [String] = [], sideLocalOpens = 0, sideOrdinaryOpens = 0
        var sideActions = CueStatusPopoverActions(openSettings: {}, openUsage: {}, toggleEnabled: {}, toggleSound: {}, pause: {}, demo: {}, quit: {}, openEvent: { _ in sideLocalOpens += 1 })
        sideActions.openInCodex = { _ in sideOrdinaryOpens += 1 }; sideActions.openSideChat = { sideOpened.append($0.threadID) }
        let sidePanel = CueStatusPopoverController(history: sideHistory, actions: sideActions)
        sidePanel.update(status: "侧边聊天验收", enabled: true, soundEnabled: true, paused: false, runningTasks: [running("side-running")], interruptedTasks: [lost("side-interrupted")], sideChats: sideGroups)
        _ = sidePanel.view
        let sideLedger = try? Data(contentsOf: sideHistory.file), sideFollowups = try? Data(contentsOf: sideHistory.followups.file)
        let sideRows = sidePanel.recentDocument!.subviews.filter { $0 is CueRecentRowButton || $0 is CueRunningRowButton }
        let sideButtons = sideRows.compactMap { $0.subviews.compactMap { $0 as? CueSideChatButton }.first }
        results["side_chat_disclosure_exists_for_all_parent_row_kinds"] = sideRows.count == 7 && sideButtons.count == 7 && sideButtons.allSatisfy { !$0.isHidden && $0.title == "侧边聊天 1 ›" }
        results["side_chat_disclosure_keeps_parent_row_height_and_accounting_geometry"] = sideRows.allSatisfy { row in
            guard let button = row.subviews.compactMap({ $0 as? CueSideChatButton }).first,
                  let model = row.subviews.first(where: { $0.identifier?.rawValue.hasPrefix("task-metrics:") == true }),
                  let elapsed = row.subviews.first(where: { $0.identifier?.rawValue.hasPrefix("task-elapsed:") == true }),
                  let metadata = row.subviews.first(where: { $0.identifier?.rawValue == "row-metadata" }) else { return false }
            return row.frame.height == CueTaskRowLayout.height && button.frame.maxY < model.frame.minY && button.frame.maxX < elapsed.frame.minX && metadata.frame.maxX < button.frame.minX
        }
        results["side_chat_disclosure_has_independent_hit_and_accessibility_target"] = sideButtons.allSatisfy { button in
            guard let row = button.superview else { return false }
            let point = button.convert(NSPoint(x: button.bounds.midX, y: button.bounds.midY), to: row.superview)
            return row.hitTest(point) === button && row.accessibilityChildren()?.contains { ($0 as? NSView) === button } == true
        }
        sidePanel.select(id: "side-history")
        let sideMenu = sidePanel.navigationContextMenu(sideHistory.records.first { $0.event.threadID == "side-unread" }!.event)
        let sideMenuItems = sideMenu.items.first { $0.title == "侧边聊天 1" }?.submenu?.items ?? []
        if let item = sideMenuItems.first { sidePanel.openSideChatFromMenu(item) }
        results["side_chat_open_uses_child_callback_without_marking_parent_read_or_retaining_child"] = sideOpened == ["child-side-unread"] && sideLocalOpens == 0 && sideOrdinaryOpens == 0 && sidePanel.selectedID == "side-history" && sideHistory.records.first { $0.event.threadID == "side-unread" }?.isOpened == false && (try? Data(contentsOf: sideHistory.file)) == sideLedger && (try? Data(contentsOf: sideHistory.followups.file)) == sideFollowups
        let sideWindow = NSWindow(contentRect: sidePanel.view.bounds, styleMask: .borderless, backing: .buffered, defer: false)
        sideWindow.isReleasedWhenClosed = false; sideWindow.contentView = sidePanel.view
        sideWindow.setFrameOrigin(NSPoint(x: -10000, y: -10000)); sideWindow.orderFrontRegardless()
        if let disclosure = sideButtons.first { disclosure.invoke?() }
        let originalSideRow = sideRows.first
        let wasSidePopoverShown = sidePanel.sideChatsController.isShown
        sidePanel.update(status: "侧边聊天后台刷新", enabled: true, soundEnabled: true, paused: false, runningTasks: [running("side-running")], interruptedTasks: [lost("side-interrupted")], sideChats: sideGroups)
        results["side_chat_live_refresh_preserves_open_popover_and_parent_anchor"] = wasSidePopoverShown && sidePanel.sideChatsController.isShown && sidePanel.recentDocument?.subviews.first(where: { $0 is CueRecentRowButton || $0 is CueRunningRowButton }) === originalSideRow
        sidePanel.toggleBatchMode()
        results["side_chat_batch_mode_closes_popover_and_hides_child_actions"] = !sidePanel.sideChatsController.isShown && sidePanel.recentDocument!.subviews.filter { $0 is CueRecentRowButton || $0 is CueRunningRowButton }.allSatisfy { $0.subviews.compactMap { $0 as? CueSideChatButton }.allSatisfy(\.isHidden) }
        let sideOpenCount = sideOpened.count
        sideButtons.first?.invoke?()
        if let child = sideGroups["side-unread"]?.first { sidePanel.openSideChat(child) }
        results["side_chat_stale_controls_do_not_open_or_toggle_selection_in_batch"] = sideOpened.count == sideOpenCount && sidePanel.selectedThreads.isEmpty
        sidePanel.sideChatsController.close(); sideWindow.orderOut(nil)
        return results
    }

    /// Isolated native screenshot fixture; no watcher or real chat is touched.
    static func captureFixture(to output: URL, batchMode: Bool = false, dark: Bool = true) -> Bool {
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
        history.followups.retain(threadID: "fixture-followup", title: "VibeCue｜下次继续完善声音与任务列表", now: now.addingTimeInterval(-300))
        let tasks: [CueRunningTask] = [("pinned-running", "VibeCue｜任务运行状态与提醒列表", 6.0), ("active-lint", "NACP｜增量同步与恢复验证", 12.0), ("active-build", "ALV｜构建发布与验收检查", 4.0)].compactMap { thread, title, minutes in
            CueRunningTask(raw: ["thread_id": thread, "title": title, "provider": "codex", "started_at": now.addingTimeInterval(-minutes * 60).timeIntervalSince1970, "last_activity": now.timeIntervalSince1970, "turn_id": "fixture-" + thread])
        }
        let actions = CueStatusPopoverActions(openSettings: {}, openUsage: {}, toggleEnabled: {}, toggleSound: {}, pause: {}, demo: {}, quit: {}, openEvent: { _ in })
        let panel = CueStatusPopoverController(history: history, actions: actions)
        func captureUsage(_ tokens: Int, historical: Bool = false, index: Int = 0) -> [String: Any] {
            ["total_tokens": tokens, "tokens_status": index == 1 ? "partial" : "complete", "cost_usd": historical ? 12345.67 : 0.1742 + Double(index), "cost_cny": historical ? 88543.76 : 1.2493 + Double(index), "cost_status": index == 1 ? "partial" : "complete", "currency": index == 2 ? "both" : "CNY", "updated_at": now.addingTimeInterval(-120).timeIntervalSince1970, "provenance": historical ? "synced_history" : "current_turn"]
        }
        var details = Dictionary(uniqueKeysWithValues: tasks.enumerated().map { index, task in
            (task.threadID, CueTaskDetail(raw: ["status":"running", "started_at":task.startDate.timeIntervalSince1970, "last_activity":now.timeIntervalSince1970, "elapsed_status":"complete", "turn_usage":captureUsage(24750 + index * 12300, index:index), "history_usage":captureUsage(234567890 + index * 1230000, historical:true, index:index), "total_tokens":24750 + index * 12300, "input_tokens":2100, "output_tokens":1250, "cache_read_tokens":21400 + index * 12300, "cache_creation_tokens":0, "tokens_status":"complete", "model":"gpt-6.1-sol", "reasoning_effort":"high", "service_tier":"priority", "current_settings":["status":"current","source":"codex_desktop","model":"gpt-6.1-sol","model_display_name":"GPT-6.1-Sol","model_status":"explicit","reasoning_effort":"ultra","service_tier":index == 1 ? "default" : "priority","speed_kind":index == 1 ? "standard" : "fast","speed_source":"codex_saved_thread_settings","observed_at":now.timeIntervalSince1970,"verified_at":now.timeIntervalSince1970,"expires_at":now.addingTimeInterval(60).timeIntervalSince1970], "messages":[["role":"assistant","text":"正在核对本轮变更，完成后会报告验证结果。","created_at":now.timeIntervalSince1970]]]))
        })
        let lost = CueRunningTask(raw: ["thread_id": "fixture-interrupted", "title": "ALV｜重启前的任务，等待手动继续", "started_at": now.addingTimeInterval(-1800).timeIntervalSince1970, "last_activity": now.addingTimeInterval(-600).timeIntervalSince1970, "interrupted_at": now.addingTimeInterval(-590).timeIntervalSince1970, "status": "interrupted", "reason_text": "Codex 进程已结束，保留任务记录。", "turn_id": "fixture-old-turn"])!
        for id in ["fixture-followup", "fixture-interrupted", "pinned-read", "history-0", "history-1"] {
            details[id] = CueTaskDetail(raw: ["status": id == "fixture-interrupted" ? "interrupted" : "complete", "started_at": now.addingTimeInterval(-760).timeIntervalSince1970, "finished_at": now.addingTimeInterval(-300).timeIntervalSince1970, "elapsed_status": "complete", "turn_usage":captureUsage(154820), "history_usage":captureUsage(234567890, historical:true), "total_tokens": 154820, "input_tokens": 7820, "output_tokens": 12000, "cache_read_tokens": 135000, "tokens_status": "complete", "model": "gpt-6.1-sol", "reasoning_effort": "ultra", "service_tier": "standard"])
        }
        let sideChats = CueSideChat.grouped(raw: ["state": "connected", "by_parent": ["pinned-running": [
            CueSideChat.fixtureRaw(parent: "pinned-running", child: "fixture-side-running", title: "核对侧边聊天入口与状态", status: "running", now: now),
            CueSideChat.fixtureRaw(parent: "pinned-running", child: "fixture-side-completed", title: "确认原生按钮与列表布局", status: "completed", now: now),
        ]]])
        panel.update(status: "界面样例 · 3 个运行 · 1 个中断待续", enabled: true, soundEnabled: true, paused: false, maximumHeight: 1100, runningTasks: tasks, interruptedTasks: [lost], taskDetails: details, attentionConnected: true, sideChats: sideChats)
        if batchMode { panel.toggleBatchMode(); panel.selectPinned(); panel.selectRunning() }
        let view = panel.view
        let window = NSWindow(contentRect: view.bounds, styleMask: .borderless, backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.appearance = NSAppearance(named: dark ? .darkAqua : .aqua); window.contentView = view
        window.setFrameOrigin(NSPoint(x: -10000, y: -10000)); window.orderFrontRegardless(); window.displayIfNeeded()
        defer { window.orderOut(nil) }
        guard let representation = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return false }
        view.effectiveAppearance.performAsCurrentDrawingAppearance { view.cacheDisplay(in: view.bounds, to: representation) }
        guard let data = representation.representation(using: .png, properties: [:]) else { return false }
        do { try data.write(to: output); return true } catch { return false }
    }

    private func batchTarget(_ event: CueEvent) -> CueBatchTarget? {
        CueBatchTarget(threadID: event.threadID, title: event.title, provider: event.provider)
    }
    private func availableBatchTargets() -> [CueBatchTarget] {
        if batchMode { return frozenAllTargets }
        return CueBatchTarget.unique(history.records.compactMap { batchTarget($0.event) } + (runningTasks + interruptedTasks).compactMap { CueBatchTarget(threadID: $0.threadID, title: $0.title) } + history.followups.records.compactMap { CueBatchTarget(threadID: $0.threadID, title: $0.title) })
    }
    private func batchCheckbox(_ target: CueBatchTarget?, y: CGFloat, in document: NSView) {
        guard let target else { return }
        batchTargets[target.threadID] = target
        let check = NSButton(checkboxWithTitle: "", target: self, action: #selector(toggleBatchCheckbox(_:)))
        check.frame = NSRect(x: 7, y: y + 17, width: 24, height: 25)
        check.identifier = NSUserInterfaceItemIdentifier(target.threadID)
        check.state = selectedThreads.contains(target.threadID) ? .on : .off
        check.setAccessibilityLabel("批量选择：" + target.title); document.addSubview(check)
    }
    private func toggleBatchThread(_ id: String) {
        guard let target = availableBatchTargets().first(where: { $0.threadID == id }) ?? batchTargets[id] else { return }
        batchTargets[id] = target
        if selectedThreads.contains(id) { selectedThreads.remove(id) } else { selectedThreads.insert(id) }
        refreshInPlace()
    }
    private func toggleBatchScope(_ targets: [CueBatchTarget]) {
        let unique = CueBatchTarget.unique(targets)
        unique.forEach { batchTargets[$0.threadID] = $0 }
        let ids = Set(unique.map(\.threadID))
        if ids.isSubset(of: selectedThreads) { selectedThreads.subtract(ids) } else { selectedThreads.formUnion(ids) }
        refreshInPlace()
    }
    @objc private func toggleBatchMode() {
        if batchMode {
            batchMode = false; selectedThreads.removeAll(); batchTargets.removeAll()
            frozenBatchSnapshot = nil; frozenPinnedTargets = []; frozenRunningTargets = []; frozenAllTargets = []
        } else {
            frozenBatchSnapshot = CueStatusListSnapshot(records: history.records, runningTasks: runningTasks, interruptedTasks: interruptedTasks, followups: history.followups.records)
            frozenPinnedTargets = pinnedBatchTargets()
            frozenRunningTargets = CueBatchTarget.unique(runningTasks.compactMap { CueBatchTarget(threadID: $0.threadID, title: $0.title) })
            frozenAllTargets = availableBatchTargets()
            frozenAllTargets.forEach { batchTargets[$0.threadID] = $0 }
            batchMode = true
        }
        hoverController.hide()
        rebuild()
    }
    @objc private func toggleBatchCheckbox(_ sender: NSButton) {
        if let id = sender.identifier?.rawValue { toggleBatchThread(id) }
    }
    private func pinnedBatchTargets() -> [CueBatchTarget] {
        CueBatchTarget.unique(history.records.filter(\.isPinned).compactMap { batchTarget($0.event) } + history.followups.records.filter(\.isPinned).compactMap { CueBatchTarget(threadID: $0.threadID, title: $0.title) })
    }
    @objc private func selectPinned() { toggleBatchScope(batchMode ? frozenPinnedTargets : pinnedBatchTargets()) }
    @objc private func selectRunning() { toggleBatchScope(batchMode ? frozenRunningTargets : runningTasks.compactMap { CueBatchTarget(threadID: $0.threadID, title: $0.title) }) }
    @objc private func selectAllBatchThreads() {
        availableBatchTargets().forEach { batchTargets[$0.threadID] = $0; selectedThreads.insert($0.threadID) }
        refreshInPlace()
    }
    @objc private func clearBatchSelection() { selectedThreads.removeAll(); refreshInPlace() }
    @objc private func configureSelected() {
        let targets = selectedThreads.sorted().compactMap { batchTargets[$0] }
        guard !targets.isEmpty else { return }
        hoverController.hide(); actions.batchConfigure(CueBatchTarget.unique(targets))
    }
    @objc private func connectCodex() { hoverController.hide(); actions.connectCodex() }
    private func recoveryContextMenu(_ task: CueRunningTask) -> NSMenu {
        let menu = navigationContextMenu(task.ephemeralCueEvent)
        menu.addItem(.separator())
        let remove = NSMenuItem(title: "移除此中断记录", action: #selector(dismissRecoveryFromMenu(_:)), keyEquivalent: "")
        remove.target = self; remove.representedObject = task; remove.toolTip = "仅移除 VibeCue 的中断标记，不删除原对话。"; menu.addItem(remove)
        return menu
    }
    private func navigationContextMenu(_ event: CueEvent, followup: CueThreadFollowup? = nil, includePin: Bool = true) -> NSMenu {
        let menu = NSMenu(); menu.autoenablesItems = false
        @discardableResult func item(_ title: String, _ action: Selector, value: Any? = nil) -> NSMenuItem {
            let next = NSMenuItem(title: title, action: action, keyEquivalent: ""); next.target = self
            next.representedObject = value ?? event; menu.addItem(next); return next
        }
        item("查看对话", #selector(reviewFromMenu(_:)))
        item("在 Codex 中打开", #selector(openCodexFromMenu(_:)))
        if event.provider == "codex" { addSideChatsMenu(to: menu, parentID: event.threadID) }
        menu.addItem(.separator())
        item("复制标题", #selector(copyThreadValue(_:)), value: ["kind": "title", "event": event])
        item("复制对话 ID", #selector(copyThreadValue(_:)), value: ["kind": "id", "event": event])
        item("复制对话链接", #selector(copyThreadValue(_:)), value: ["kind": "link", "event": event])
        let paths = NSMenu(); paths.autoenablesItems = false
        for (title, command) in [("复制项目路径", "copy_project_path"), ("复制会话记录路径", "copy_log_path"), ("在访达中打开项目文件夹", "reveal_project"), ("在访达中定位会话记录", "reveal_log"), ("在终端中打开项目文件夹", "open_terminal")] {
            let action = NSMenuItem(title: title, action: #selector(pathActionFromMenu(_:)), keyEquivalent: "")
            action.target = self; action.representedObject = ["thread_id": event.threadID, "command": command]; paths.addItem(action)
        }
        let pathItem = NSMenuItem(title: "项目与会话文件", action: nil, keyEquivalent: ""); pathItem.submenu = paths; menu.addItem(pathItem)
        menu.addItem(.separator())
        let pending = history.followups.records.first { $0.threadID == event.threadID }
        item(pending == nil ? "保留为待续" : "继续保留为待续", #selector(deferFromMenu(_:)))
        item("标记处理完成", #selector(handleFromMenu(_:)))
        if includePin {
            let unread = item(pending?.unread == true ? "已是未读" : "标为未读", #selector(markTaskUnreadFromMenu(_:)))
            unread.isEnabled = pending?.unread != true
            let pin = item(pending?.pinned == true ? "取消置顶" : "置顶", #selector(pinFollowupFromMenu(_:)))
            pin.state = pending?.pinned == true ? .on : .off
        }
        if followup != nil {
            menu.addItem(.separator())
            item("移除待续记录", #selector(removeFollowupFromMenu(_:))).toolTip = "仅移除 VibeCue 的待续记录，不删除原对话。"
        }
        return menu
    }
    @objc private func reviewFromMenu(_ sender: NSMenuItem) { if let event = sender.representedObject as? CueEvent { hoverController.hide(); actions.openEvent(event) } }
    @objc private func openCodexFromMenu(_ sender: NSMenuItem) { if let event = sender.representedObject as? CueEvent { hoverController.hide(); actions.openInCodex(event) } }
    private func addSideChatsMenu(to menu: NSMenu, parentID: String) {
        let children = sideChats[parentID]?.filter { $0.parentThreadID == parentID } ?? []
        guard !children.isEmpty else { return }
        let submenu = NSMenu(); submenu.autoenablesItems = false
        for child in children {
            let item = NSMenuItem(title: "\(child.title) · \(child.statusLabel)", action: #selector(openSideChatFromMenu(_:)), keyEquivalent: "")
            item.target = self; item.representedObject = child; item.isEnabled = child.canOpen
            item.toolTip = "在 Codex 打开 · " + child.threadID; submenu.addItem(item)
        }
        let item = NSMenuItem(title: "侧边聊天 \(children.count)", action: nil, keyEquivalent: "")
        item.submenu = submenu; menu.addItem(item)
    }
    @objc private func openSideChatFromMenu(_ sender: NSMenuItem) {
        if let child = sender.representedObject as? CueSideChat { openSideChat(child) }
    }
    private func openSideChat(_ child: CueSideChat) {
        guard !batchMode, let latest = sideChats[child.parentThreadID]?.first(where: { $0.threadID == child.threadID && $0.parentThreadID == child.parentThreadID }), latest.canOpen else { return }
        hoverController.hide(); sideChatsController.close(); actions.openSideChat(latest)
    }
    @objc private func copyThreadValue(_ sender: NSMenuItem) {
        guard let payload = sender.representedObject as? [String: Any], let event = payload["event"] as? CueEvent else { return }
        let kind = payload["kind"] as? String
        var text = kind == "title" ? event.title : event.threadID
        if kind == "link" {
            var url = URLComponents(); url.scheme = "codex"; url.host = "threads"; url.path = "/" + event.threadID
            guard let link = url.url?.absoluteString else { return }; text = link
        }
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(text, forType: .string)
    }
    @objc private func pathActionFromMenu(_ sender: NSMenuItem) {
        guard let payload = sender.representedObject as? [String: String], let id = payload["thread_id"], let command = payload["command"] else { return }
        actions.threadAction(id, command)
    }
    @objc private func deferFromMenu(_ sender: NSMenuItem) {
        guard let event = sender.representedObject as? CueEvent else { return }
        history.followups.retain(threadID: event.threadID, title: event.title, provider: event.provider); historyDidChange()
    }
    @objc private func handleFromMenu(_ sender: NSMenuItem) {
        guard let event = sender.representedObject as? CueEvent else { return }
        history.markThreadHandled(threadID: event.threadID)
        if let task = interruptedTasks.first(where: { $0.threadID == event.threadID }) { actions.dismissInterrupted(task) }
        historyDidChange()
    }
    @objc private func markTaskUnreadFromMenu(_ sender: NSMenuItem) {
        guard let event = sender.representedObject as? CueEvent else { return }
        history.markThreadUnread(event: event); historyDidChange()
    }
    @objc private func pinFollowupFromMenu(_ sender: NSMenuItem) {
        guard let event = sender.representedObject as? CueEvent else { return }
        let previous = history.followups.records.first { $0.threadID == event.threadID }?.pinned ?? false
        history.followups.retain(threadID: event.threadID, title: event.title, provider: event.provider)
        history.followups.setPinned(threadID: event.threadID, pinned: !previous); historyDidChange()
    }
    @objc private func removeFollowupFromMenu(_ sender: NSMenuItem) {
        guard let event = sender.representedObject as? CueEvent else { return }
        history.removeFollowup(threadID: event.threadID); historyDidChange()
    }
    @objc private func openRecoveryFromMenu(_ sender: NSMenuItem) { if let task = sender.representedObject as? CueRunningTask { actions.openEvent(task.ephemeralCueEvent) } }
    @objc private func dismissRecoveryFromMenu(_ sender: NSMenuItem) { if let task = sender.representedObject as? CueRunningTask { actions.dismissInterrupted(task) } }
    private func addDetails(to row: NSButton, threadID: String, title: String, eventDate: Date?) {
        let metrics = NSTextField(labelWithString: rowInformation(threadID: threadID, eventDate: eventDate))
        metrics.font = .systemFont(ofSize: 10); metrics.textColor = .secondaryLabelColor
        metrics.lineBreakMode = .byTruncatingTail; metrics.identifier = NSUserInterfaceItemIdentifier("task-metrics:" + threadID)
        row.addSubview(metrics)
        let speed = NSTextField(labelWithString: "")
        speed.font = .systemFont(ofSize: 9.5, weight: .medium); speed.alignment = .right
        speed.lineBreakMode = .byTruncatingHead; speed.identifier = NSUserInterfaceItemIdentifier("task-speed:" + threadID)
        row.addSubview(speed)
        let elapsed = NSTextField(labelWithString: "")
        elapsed.font = .monospacedDigitSystemFont(ofSize: 10, weight: .regular); elapsed.textColor = .secondaryLabelColor
        elapsed.alignment = .right; elapsed.lineBreakMode = .byTruncatingHead
        elapsed.identifier = NSUserInterfaceItemIdentifier("task-elapsed:" + threadID)
        row.addSubview(elapsed); row.toolTip = nil
        for scope in ["turn", "history"] {
            for part in ["label", "tokens", "cost"] {
                let field = NSTextField(labelWithString: "")
                field.identifier = NSUserInterfaceItemIdentifier("usage-" + scope + "-" + part)
                field.alignment = .right; field.lineBreakMode = .byClipping
                field.textColor = part == "label" ? .tertiaryLabelColor : part == "cost" ? .secondaryLabelColor : .labelColor
                row.addSubview(field)
            }
            let separator = NSBox(); separator.boxType = .separator
            separator.identifier = NSUserInterfaceItemIdentifier("usage-" + scope + "-separator")
            row.addSubview(separator)
        }
        refreshDetails(in: row, threadID: threadID, eventDate: eventDate)
    }
    private func refreshDetails(in row: NSButton, threadID: String, eventDate: Date?) {
        let layout = CueTaskRowLayout(width: row.bounds.width)
        let line = rowInformation(threadID: threadID, eventDate: eventDate)
        let elapsed = durationLine(threadID: threadID, eventDate: eventDate, now: Date())
        let detail = taskDetails[threadID] ?? CueTaskDetail(raw: [:])
        for child in row.subviews {
            guard let field = child as? NSTextField else { continue }
            switch field.identifier?.rawValue {
            case "row-title": field.frame = layout.heading
            case "row-metadata": field.frame = layout.metadata
            case "row-state": field.frame = layout.state
            case "task-metrics:" + threadID: field.stringValue = line; field.frame = layout.model; field.toolTip = detail.settingsTooltip
            case "task-elapsed:" + threadID: field.stringValue = elapsed; field.frame = layout.elapsed
            case "task-speed:" + threadID:
                field.stringValue = detail.speedSelectionLine; field.frame = layout.speed
                field.textColor = detail.speedSelectionIsFast ? .systemOrange : .secondaryLabelColor
                field.toolTip = detail.settingsTooltip
            default: break
            }
        }
        let active = runningTasks.first { $0.threadID == threadID && !$0.needsRecovery }
        let oldTurn = active.map { !$0.turnID.isEmpty && !detail.turnID.isEmpty && $0.turnID != detail.turnID } ?? false
        let turn = oldTurn ? CueTaskDetail(raw: [:]).turnUsage : detail.turnUsage
        for (scope, label, value, x) in [("turn", oldTurn ? "本轮" : detail.turnUsageLabel(eventDate: eventDate), turn, layout.turnX), ("history", "历史累计", detail.historyUsage, layout.historyX)] {
            for child in row.subviews {
                if let separator = child as? NSBox, separator.identifier?.rawValue == "usage-" + scope + "-separator" {
                    separator.frame = NSRect(x: x - 8, y: 12, width: 1, height: 54)
                }
                guard let field = child as? NSTextField else { continue }
                let prefix = "usage-" + scope + "-"
                switch field.identifier?.rawValue {
                case prefix + "label": field.stringValue = label; field.frame = NSRect(x: x, y: 6, width: layout.columnWidth, height: 14); field.font = .systemFont(ofSize: 9, weight: .medium)
                case prefix + "tokens": field.stringValue = value.tokenLine; field.frame = NSRect(x: x, y: 24, width: layout.columnWidth, height: 17); field.font = .monospacedDigitSystemFont(ofSize: 11, weight: .medium)
                case prefix + "cost":
                    field.stringValue = value.costLine; field.frame = NSRect(x: x, y: 44, width: layout.columnWidth, height: 28)
                    field.maximumNumberOfLines = 2; field.usesSingleLineMode = false
                    var size: CGFloat = 10.5
                    while size > 8 && value.costLine.components(separatedBy: "\n").contains(where: { ($0 as NSString).size(withAttributes: [.font: NSFont.monospacedDigitSystemFont(ofSize: size, weight: .regular)]).width > layout.columnWidth }) { size -= 0.5 }
                    field.font = .monospacedDigitSystemFont(ofSize: size, weight: .regular)
                default: continue
                }
                field.toolTip = value.tooltip
            }
        }
        row.setAccessibilityHelp(line + "，" + detail.speedSelectionLine + "，" + elapsed + "。本轮 " + turn.tokenLine + "，" + turn.costLine + "。历史累计 " + detail.historyUsage.tokenLine + "，" + detail.historyUsage.costLine + "。悬停查看详情；点击打开对应对话。")
        configureQuickActions(in: row)
        configureSideChats(in: row, threadID: threadID, title: (row as? CueRecentRowButton)?.record.event.title ?? (row as? CueRunningRowButton)?.followup?.title ?? (row as? CueRunningRowButton)?.task.title ?? "Codex 对话")
    }
    private func configureSideChats(in row: NSButton, threadID: String, title: String) {
        let children = (sideChats[threadID] ?? []).filter { $0.parentThreadID == threadID }
        let existing = row.subviews.compactMap { $0 as? CueSideChatButton }.first
        guard !children.isEmpty, ((row as? CueRecentRowButton)?.record.event.provider ?? "codex") == "codex" else {
            existing?.isHidden = true; return
        }
        let button = existing ?? CueSideChatButton()
        if button.superview == nil { row.addSubview(button) }
        let metadata = CueTaskRowLayout(width: row.bounds.width).metadata
        let width: CGFloat = min(106, metadata.width)
        button.frame = NSRect(x: metadata.maxX - width, y: metadata.minY - 3, width: width, height: 21)
        button.title = "侧边聊天 \(children.count) ›"; button.isHidden = batchMode
        button.setAccessibilityLabel("侧边聊天 \(children.count)：" + title)
        button.toolTip = "查看此对话的临时侧边聊天"
        if let field = row.subviews.first(where: { $0.identifier?.rawValue == "row-metadata" }) as? NSTextField, !batchMode {
            field.frame.size.width = max(0, button.frame.minX - field.frame.minX - 6)
        }
        button.invoke = { [weak self, weak button] in
            guard let self, !self.batchMode, let button, !button.isHidden,
                  let latest = self.sideChats[threadID]?.filter({ $0.parentThreadID == threadID }), !latest.isEmpty else { return }
            self.hoverController.hide(); self.pointerAnchor = nil
            self.sideChatsController.show(parentID: threadID, parentTitle: title, children: latest, anchor: button) { [weak self] child in self?.openSideChat(child) }
        }
    }
    private func rowInformation(threadID: String, eventDate: Date?) -> String {
        taskDetails[threadID]?.modelSelectionLine ?? "当前设置不可读"
    }
    private func configureQuickActions(in row: NSButton) {
        let event: CueEvent, recordID: String?
        if let reminder = row as? CueRecentRowButton { event = reminder.record.event; recordID = reminder.record.event.id }
        else if let task = row as? CueRunningRowButton { event = task.followup?.event ?? task.task.ephemeralCueEvent; recordID = nil }
        else { return }
        let record = recordID.flatMap { id in history.records.first { $0.event.id == id } }
        let followup = history.followups.records.first { $0.threadID == event.threadID }
        let unread = record.map { !$0.isOpened } ?? (followup?.unread == true || history.records.contains { $0.event.threadID == event.threadID && !$0.isOpened })
        let manualPin = record?.manualPinned ?? followup?.pinned ?? false
        let effectivePin = record?.isPinned ?? followup?.isPinned ?? false
        let valid = CueBatchTarget(threadID: event.threadID, title: event.title, provider: event.provider) != nil
        let specifications = [("open-codex", "arrow.up.forward", "直接在 Codex 打开"),
                              ("pin", effectivePin ? "pin.fill" : "pin", manualPin ? "取消手动置顶" : unread ? "未读已自动置顶；点击手动固定" : "置顶"),
                              ("unread", unread ? "envelope.badge.fill" : "envelope.badge", unread ? "已是未读" : "标为未读"),
                              ("more", "ellipsis", "更多操作")]
        for (index, specification) in specifications.enumerated() {
            let (command, symbol, label) = specification
            let button = row.subviews.first { $0.identifier?.rawValue == "quick-" + command } as? CueRowQuickButton ?? CueRowQuickButton(command: command)
            if button.superview == nil { row.addSubview(button) }
            button.frame = CueTaskRowLayout(width: row.bounds.width).action(index)
            button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)?.withSymbolConfiguration(.init(pointSize: 12, weight: .regular))
            button.contentTintColor = command == "pin" && effectivePin ? .controlAccentColor : command == "unread" && unread ? .systemBlue : .secondaryLabelColor
            button.toolTip = label; button.setAccessibilityElement(true); button.setAccessibilityLabel(label + "：" + event.title)
            button.isEnabled = valid && !(command == "unread" && unread)
            button.isHidden = batchMode
            button.invoke = { [weak self, weak button] in
                guard let self, !self.batchMode else { return }
                self.hoverController.hide()
                if command == "open-codex" { self.actions.openInCodex(event); return }
                if command == "more" {
                    guard let button else { return }
                    let menu: NSMenu
                    if let recordID { menu = self.contextMenu(id: recordID) }
                    else if let recovery = self.interruptedTasks.first(where: { $0.threadID == event.threadID }) { menu = self.recoveryContextMenu(recovery) }
                    else { menu = self.navigationContextMenu(event, followup: self.history.followups.records.first { $0.threadID == event.threadID }) }
                    menu.popUp(positioning: nil, at: NSPoint(x: button.bounds.maxX, y: button.bounds.minY), in: button)
                    return
                }
                if command == "unread" {
                    if let recordID { self.history.markUnread(id: recordID) }
                    else { self.history.markThreadUnread(event: event) }
                } else if command == "pin" {
                    if let recordID, let latest = self.history.records.first(where: { $0.event.id == recordID }) { self.history.setPinned(id: recordID, pinned: !latest.manualPinned) }
                    else {
                        let pinned = self.history.followups.records.first { $0.threadID == event.threadID }?.pinned ?? false
                        self.history.followups.retain(threadID: event.threadID, title: event.title, provider: event.provider)
                        self.history.followups.setPinned(threadID: event.threadID, pinned: !pinned)
                    }
                }
                self.historyDidChange()
            }
        }
    }

    private func durationLine(threadID: String, eventDate: Date?, now: Date) -> String {
        let active = runningTasks.filter { $0.threadID == threadID && !$0.needsRecovery }.max { $0.updatedDate < $1.updatedDate }
        let recovery = active == nil ? interruptedTasks.filter { $0.threadID == threadID }.max { $0.updatedDate < $1.updatedDate } : nil
        if let detail = taskDetails[threadID] {
            let sameTurn = active == nil || active?.turnID.isEmpty == true || detail.turnID.isEmpty || active?.turnID == detail.turnID
            let oldCompletion = active.map { task in detail.finishedAt.map { $0 < task.startDate.addingTimeInterval(-1) } ?? false } ?? false
            if sameTurn && !oldCompletion {
                return detail.durationLine(now: now, eventDate: eventDate, running: active != nil, stoppedAt: recovery?.updatedDate)
            }
        }
        if let active { return "本轮 ≥" + CueTaskDetail.duration(max(0, now.timeIntervalSince(active.startDate))) }
        if let recovery { return "已记录 ≥" + CueTaskDetail.duration(max(0, recovery.updatedDate.timeIntervalSince(recovery.startDate))) }
        return "耗时待同步"
    }
    /// Update text only: no list rebuild, row reorder, hover or selection changes.
    private func refreshElapsedLabels(now: Date) {
        for child in recentDocument?.subviews ?? [] {
            let threadID: String, eventDate: Date?
            if let row = child as? CueRecentRowButton { threadID = row.record.event.threadID; eventDate = row.record.event.date }
            else if let row = child as? CueRunningRowButton { threadID = row.task.threadID; eventDate = nil }
            else { continue }
            guard let field = child.subviews.first(where: { $0.identifier?.rawValue == "task-elapsed:" + threadID }) as? NSTextField else { continue }
            let value = durationLine(threadID: threadID, eventDate: eventDate, now: now)
            if field.stringValue != value { field.stringValue = value }
        }
    }
    private func hover(_ inside: Bool, threadID: String, title: String, row: NSView) {
        guard !sideChatsController.isShown else { hoverController.hide(); return }
        if inside {
            pointerAnchor = row
            hoverController.hover(threadID: threadID, title: title, anchor: row, provider: { [weak self] in self?.taskDetails[threadID] })
        } else { if pointerAnchor === row { pointerAnchor = nil }; hoverController.leaveRow() }
    }
    private func select(id: String) {
        selectedID = id
        if batchMode { return }
        for row in recentDocument?.subviews.compactMap({ $0 as? CueRecentRowButton }) ?? [] {
            row.selected = row.record.event.id == id
            row.setAccessibilityValue(row.selected ? "已选中" : "")
        }
        for row in recentDocument?.subviews.compactMap({ $0 as? CueRunningRowButton }) ?? [] {
            row.selected = row.navigationID == id
            row.setAccessibilityValue(row.selected ? "已选中" : "")
        }
    }
    private func contextMenu(id: String) -> NSMenu {
        guard let record = history.records.first(where: { $0.event.id == id }) else { return NSMenu() }
        let menu = navigationContextMenu(record.event, includePin: false); menu.addItem(.separator())
        let unread = NSMenuItem(title: "标为未读", action: #selector(markUnread(_:)), keyEquivalent: "")
        unread.target = self; unread.representedObject = id; unread.isEnabled = record.isOpened
        menu.addItem(unread)
        let read = NSMenuItem(title: "标为已读", action: #selector(markRead(_:)), keyEquivalent: "")
        read.target = self; read.representedObject = id; read.isEnabled = !record.isOpened; menu.addItem(read)
        let pinTitle = record.manualPinned ? (record.isOpened ? "取消置顶" : "取消手动置顶") : (record.isOpened ? "置顶" : "置顶（打开后仍保留）")
        let pin = NSMenuItem(title: pinTitle, action: #selector(togglePin(_:)), keyEquivalent: "")
        pin.target = self; pin.representedObject = id; pin.state = record.manualPinned ? .on : .off
        menu.addItem(pin)
        if !record.isOpened {
            let hint = NSMenuItem(title: "未读已自动置顶；手动置顶可在打开后继续保留", action: nil, keyEquivalent: "")
            hint.isEnabled = false; menu.addItem(hint)
        }
        if let task = interruptedTasks.first(where: { $0.threadID == record.event.threadID }) {
            let removeRecovery = NSMenuItem(title: "移除此对话的中断标记", action: #selector(dismissRecoveryFromMenu(_:)), keyEquivalent: "")
            removeRecovery.target = self; removeRecovery.representedObject = task; menu.addItem(removeRecovery)
        }
        menu.addItem(.separator())
        let remove = NSMenuItem(title: "删除", action: #selector(deleteRow(_:)), keyEquivalent: "")
        remove.target = self; remove.representedObject = id
        remove.toolTip = "仅从 VibeCue 提醒列表移除，不删除原对话。"
        menu.addItem(remove)
        return menu
    }
    private func historyDidChange() {
        if !history.records.contains(where: { $0.event.id == selectedID }) && !(runningTasks + interruptedTasks).contains(where: { $0.navigationID == selectedID }) && !history.followups.records.contains(where: { $0.navigationID == selectedID }) { selectedID = nil }
        rebuild(); actions.historyChanged()
    }
    @objc private func markUnread(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        history.markUnread(id: id); historyDidChange()
    }
    @objc private func markRead(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String, let record = history.records.first(where: { $0.event.id == id }) else { return }
        history.beginReview(event: record.event); historyDidChange()
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
        if batchMode, let row = sender as? CueRecentRowButton { toggleBatchThread(row.record.event.threadID); return }
        guard let id = sender.identifier?.rawValue,
              let record = history.records.first(where: { $0.event.id == id }) else { return }
        select(id: id)
        if record.canOpen { actions.openEvent(record.event) }
    }
    @objc private func openRunningRow(_ sender: CueRunningRowButton) {
        if batchMode { toggleBatchThread(sender.task.threadID); return }
        select(id: sender.navigationID)
        actions.openEvent(sender.followup?.event ?? sender.task.ephemeralCueEvent)
    }
    @objc private func openSettings() { actions.openSettings() }
    @objc private func openUsage() { actions.openUsage() }
    @objc private func toggleEnabled() { actions.toggleEnabled() }
    @objc private func toggleSound() { actions.toggleSound() }
    @objc private func pause() { actions.pause() }
    @objc private func demo() { actions.demo() }
    @objc private func quit() { actions.quit() }
}
