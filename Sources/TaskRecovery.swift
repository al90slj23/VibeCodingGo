import Foundation

/// A stale observer is not proof that Codex stopped. Keep its navigation
/// targets with an explicit unknown state until the watcher reconciles them.
enum CueTaskRecoverySnapshot {
    static func read(home: URL, state: [String: Any], live: Bool, now: Date = Date()) -> (tasks: [CueRunningTask], details: [String: CueTaskDetail]) {
        var rows = state["interrupted_tasks"] as? [[String: Any]] ?? []
        if !live {
            rows += state["running_tasks"] as? [[String: Any]] ?? []
            let file = home.appendingPathComponent("task-recovery.json")
            if let data = try? Data(contentsOf: file), data.count <= 8_388_608,
               let ledger = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let records = ledger["records"] as? [String: [String: Any]] { rows += Array(records.values) }
        }
        var tasks: [String: CueRunningTask] = [:], details: [String: CueTaskDetail] = [:]
        let detailRows = state["task_details"] as? [String: [String: Any]] ?? [:]
        for row in rows {
            var raw = row
            let status = raw["status"] as? String ?? "running"
            if !["interrupted", "unconfirmed"].contains(status) {
                guard !live else { continue }
                raw["status"] = "unconfirmed"
                raw["reason_text"] = "监控暂时断开，保留上次运行记录；是否中断尚未确认。"
                raw["interrupted_at"] = raw["last_seen_at"] ?? raw["last_activity"] ?? now.timeIntervalSince1970
            }
            guard let task = CueRunningTask(raw: raw), task.needsRecovery else { continue }
            if let previous = tasks[task.threadID] {
                if previous.recoveryStatus == "interrupted" && task.recoveryStatus != "interrupted" { continue }
                if previous.recoveryStatus == task.recoveryStatus && (previous.interruptedAt ?? previous.updatedDate) >= (task.interruptedAt ?? task.updatedDate) { continue }
            }
            tasks[task.threadID] = task
            var detail = detailRows[task.threadID] ?? [:]
            for (key, value) in raw where !["messages", "last_message"].contains(key) { detail[key] = value }
            detail["status"] = task.recoveryStatus
            detail["finished_at"] = task.interruptedAt?.timeIntervalSince1970 ?? task.updatedDate.timeIntervalSince1970
            details[task.threadID] = CueTaskDetail(raw: detail)
        }
        return (tasks.values.sorted { ($0.interruptedAt ?? $0.updatedDate) > ($1.interruptedAt ?? $1.updatedDate) }, details)
    }

    static func validationReport() -> [String: Bool] {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent("vibecue-recovery-native-" + UUID().uuidString)
        let active: [String: Any] = ["thread_id": "retained-thread", "title": "保留任务", "started_at": 1000.0, "last_activity": 1060.0]
        let state: [String: Any] = ["running_tasks": [active], "task_details": ["retained-thread": ["started_at": 1000.0, "status": "running", "total_tokens": 120, "tokens_status": "complete"]]]
        let offline = read(home: home, state: state, live: false)
        let live = read(home: home, state: state, live: true)
        var interrupted = active; interrupted["status"] = "interrupted"; interrupted["interrupted_at"] = 1070.0
        let confirmed = read(home: home, state: ["running_tasks": [active], "interrupted_tasks": [interrupted]], live: false)
        return ["offline_running_navigation_is_retained_as_unconfirmed": offline.tasks.count == 1 && offline.tasks.first?.recoveryStatus == "unconfirmed",
                "live_running_is_not_duplicated_as_interrupted": live.tasks.isEmpty,
                "confirmed_interruption_is_not_downgraded_by_stale_running": confirmed.tasks.count == 1 && confirmed.tasks.first?.recoveryStatus == "interrupted",
                "interrupted_elapsed_stops_growing": offline.details["retained-thread"]?.elapsed(now: Date(timeIntervalSince1970: 9000)) == 60]
    }
}
