import Foundation

/// Immutable view of one exact watcher snapshot. Numeric data is never added to
/// a previous projection; every changed value replaces its previous value.
struct CueWatcherPreparedSnapshot {
    let state: [String: Any]
    let launchState: [String: Any]
    let live: Bool
    let running: [CueRunningTask]
    let interrupted: [CueRunningTask]
    let details: [String: CueTaskDetail]
    let sideChats: [String: [CueSideChat]]
    let signature: Data
    let temporalState: [String: String]
    let metrics: [String: Int]
}

/// Queue-confined cache. A whole detail may change for numeric/current-setting
/// telemetry while its bounded conversation tail remains byte-for-byte equal.
final class CueWatcherProjectionBuilder {
    private struct Entry {
        let raw: Data
        let contentRaw: Data
        let detail: CueTaskDetail
        let content: CueTaskDetailContent
    }
    private var entries: [String: Entry] = [:]
    private let capacity = 512
    private(set) var parsedDetails = 0, detailHits = 0, parsedContents = 0, contentHits = 0
    var metrics: [String: Int] { ["details_parsed": parsedDetails, "details_reused": detailHits,
        "contents_parsed": parsedContents, "contents_reused": contentHits, "cached_details": entries.count] }
    static func isLive(_ state: [String: Any], now: Date = Date()) -> Bool {
        guard state["running"] as? Bool == true, let stamp = state["heartbeat"] as? Double else { return false }
        return (0...15).contains(now.timeIntervalSince1970 - stamp)
    }
    /// Stable expiry bits, never the continuously advancing wall clock. They
    /// change only when a getter changes its actual readability/navigation state.
    static func temporalState(details: [String: CueTaskDetail], sideChats: [String: [CueSideChat]], now: Date = Date()) -> [String: String] {
        var values: [String: String] = [:]
        for (id, detail) in details { values["detail:" + id] = detail.hasReadableCurrentSettings(at: now) ? "readable" : "unavailable" }
        for (parent, chats) in sideChats {
            for chat in chats { values["side:" + parent + ":" + chat.threadID] = chat.status(at: now) + ":" + String(chat.canOpen(at: now)) }
        }
        return values
    }
    private static func json(_ value: Any) -> Data { (try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])) ?? Data() }
    func detail(_ raw: [String: Any], id: String) -> CueTaskDetail {
        let full = Self.json(raw)
        if let cached = entries[id], !full.isEmpty, cached.raw == full { detailHits += 1; return cached.detail }
        let contentKeys: Set<String> = ["last_message", "last_message_at", "last_role", "messages", "content_truncated"]
        let tail = Self.json(raw.filter { contentKeys.contains($0.key) })
        let content: CueTaskDetailContent
        if let cached = entries[id], !tail.isEmpty, cached.contentRaw == tail { content = cached.content; contentHits += 1 }
        else { content = CueTaskDetailContent(raw: raw); parsedContents += 1 }
        let result = CueTaskDetail(raw: raw, preparedContent: content); parsedDetails += 1
        if entries[id] != nil || entries.count < capacity {
            entries[id] = Entry(raw: full, contentRaw: tail, detail: result, content: content)
        }
        return result
    }
    func prepare(_ rawState: [String: Any], home: URL, now: Date = Date()) -> CueWatcherPreparedSnapshot {
        var state = rawState
        let live = Self.isLive(state, now: now)
        var rows = state["task_details"] as? [String: [String: Any]] ?? [:]
        if live {
            let settings = state["current_model_settings"] as? [String: [String: Any]] ?? [:]
            for (id, selected) in settings.prefix(256) where CueThreadFollowups.valid(id) {
                var row = rows[id] ?? ["thread_id": id, "status": "unknown"]
                row["current_settings"] = selected; rows[id] = row
            }
        } else {
            rows = rows.mapValues { row in
                var value = row
                value["current_settings"] = ["status": "unavailable", "source": "codex_desktop", "reason": "本机监控连接暂不可用"]
                return value
            }
        }
        state["task_details"] = rows
        entries = entries.filter { rows[$0.key] != nil }
        var details: [String: CueTaskDetail] = [:]
        if live { for (id, row) in rows { details[id] = detail(row, id: id) } }
        let taskRows = live ? state["running_tasks"] as? [[String: Any]] ?? [] : []
        let running = taskRows.compactMap { CueRunningTask(raw: $0) }
        var signature = live ? Self.json(["tasks": taskRows, "details": rows]) : Data()
        var sideState = state["side_chats"] as? [String: Any] ?? [:]
        if !live { sideState["state"] = "disconnected" }
        let sideChats = CueSideChat.grouped(raw: sideState, now: now)
        signature.append(Self.json(sideState))
        let recovery = CueTaskRecoverySnapshot.read(home: home, state: state, live: live, now: now)
        let active = Set(running.map(\.threadID))
        let interrupted = recovery.tasks.filter { !active.contains($0.threadID) }
        for task in interrupted { if let value = recovery.details[task.threadID] { details[task.threadID] = value } }
        let recoveryRows = interrupted.map { ["thread_id": $0.threadID, "title": $0.title,
            "status": $0.recoveryStatus, "reason": $0.recoveryReason,
            "interrupted_at": $0.interruptedAt?.timeIntervalSince1970 ?? 0,
            "last_activity": $0.updatedDate.timeIntervalSince1970] as [String: Any] }
        signature.append(Self.json(recoveryRows))
        let temporal = Self.temporalState(details: details, sideChats: sideChats, now: now)
        signature.append(Self.json(temporal))
        var launch: [String: Any] = [:]
        if let data = try? Data(contentsOf: home.appendingPathComponent("bridge-launch-state.json")), data.count < 65_536,
           let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] { launch = value }
        return CueWatcherPreparedSnapshot(state: state, launchState: launch, live: live, running: running,
            interrupted: interrupted, details: details, sideChats: sideChats, signature: signature, temporalState: temporal, metrics: metrics)
    }
}

/// At most one utility projection and one newest pending request. Obsolete
/// completions are not committed to AppKit after a newer state was requested.
final class CueWatcherProjectionReader {
    private let queue = DispatchQueue(label: "local.vibecue.watcher-projection", qos: .utility)
    private let builder = CueWatcherProjectionBuilder()
    private var generation = 0, inFlight = false
    private var pending: (() -> Void)?
    private var counts: [String: Int] = ["requested": 0, "completed": 0, "superseded": 0, "coalesced": 0]
    var metrics: [String: Int] { counts }
    func recordMainCommit(started: TimeInterval) {
        precondition(Thread.isMainThread)
        let milliseconds = Int(max(0, ProcessInfo.processInfo.systemUptime - started) * 1000)
        counts["main_commits", default: 0] += 1
        counts["maximum_main_commit_ms"] = max(counts["maximum_main_commit_ms", default: 0], milliseconds)
    }
    func project(_ state: [String: Any], home: URL, completion: @escaping (CueWatcherPreparedSnapshot) -> Void) {
        precondition(Thread.isMainThread)
        generation += 1; let requested = generation; counts["requested", default: 0] += 1
        let launch = { [weak self] in
            guard let self else { return }; self.inFlight = true
            self.queue.async {
                let began = ProcessInfo.processInfo.systemUptime
                let prepared = self.builder.prepare(state, home: home)
                let projectionMs = Int(max(0, ProcessInfo.processInfo.systemUptime - began) * 1000)
                DispatchQueue.main.async {
                    self.inFlight = false
                    self.counts["maximum_projection_ms"] = max(self.counts["maximum_projection_ms", default: 0], projectionMs)
                    for (key, value) in prepared.metrics { self.counts[key] = value }
                    if requested == self.generation { self.counts["completed", default: 0] += 1; completion(prepared) }
                    else { self.counts["superseded", default: 0] += 1 }
                    let next = self.pending; self.pending = nil; next?()
                }
            }
        }
        if inFlight { counts["coalesced", default: 0] += 1; pending = launch }
        else { launch() }
    }

    static func validationReport() -> [String: Bool] {
        let builder = CueWatcherProjectionBuilder()
        let now = Date(), home = FileManager.default.temporaryDirectory.appendingPathComponent("vcgo-watcher-projection-" + UUID().uuidString)
        let tail = String(repeating: "精确测试👩🏽‍💻\u{0001}\n", count: 500)
        var row: [String: Any] = ["status": "running", "total_tokens": 12, "tokens_status": "complete", "last_role": "assistant", "last_message": tail,
            "messages": [["role": "assistant", "text": tail, "created_at": 1000.0]]]
        let original = CueTaskDetail(raw: row), first = builder.detail(row, id: "one")
        let bodyEqual = original.messages.map(\.text) == first.messages.map(\.text) && original.lastMessage == first.lastMessage && original.contentTruncated == first.contentTruncated
        _ = builder.detail(row, id: "one")
        let unchanged = builder.parsedContents == 1 && builder.parsedDetails == 1 && builder.detailHits == 1
        row["total_tokens"] = 13
        let next = builder.detail(row, id: "one")
        let numericExact = next.totalTokens == 13 && next.messages.map(\.text) == first.messages.map(\.text) && builder.parsedContents == 1 && builder.contentHits == 1
        row["messages"] = [["role": "user", "text": "改变后的正文", "created_at": 1001.0]]
        let changed = builder.detail(row, id: "one")
        let changedBody = changed.messages.first?.text == "改变后的正文" && builder.parsedContents == 2
        row["total_tokens"] = true
        let invalid = builder.detail(row, id: "one")
        let state: [String: Any] = ["running": true, "heartbeat": now.timeIntervalSince1970, "task_details": ["one": row],
            "current_model_settings": ["one": ["status": "current", "source": "codex_desktop", "model": "verified", "verified_at": now.timeIntervalSince1970]]]
        let live = builder.prepare(state, home: home, now: now)
        let expired = builder.prepare(state, home: home, now: now.addingTimeInterval(16))
        var temporalRaw = state
        temporalRaw["current_model_settings"] = ["one": ["status": "current", "source": "codex_desktop", "model": "verified", "expires_at": now.addingTimeInterval(1).timeIntervalSince1970]]
        var child = CueSideChat.fixtureRaw(parent: "one", child: "temporary-child", title: "精确到期", status: "running", now: now)
        child["expires_at"] = now.addingTimeInterval(1).timeIntervalSince1970
        temporalRaw["side_chats"] = ["state":"connected", "by_parent":["one":[child]]]
        let beforeExpiry = builder.prepare(temporalRaw, home: home, now: now)
        let later = now.addingTimeInterval(2)
        let afterExpiry = builder.prepare(temporalRaw, home: home, now: later)
        let expiryInvalidates = beforeExpiry.signature != afterExpiry.signature && beforeExpiry.temporalState["detail:one"] == "readable" && afterExpiry.temporalState["detail:one"] == "unavailable" && beforeExpiry.temporalState["side:one:temporary-child"] == "running:true" && afterExpiry.temporalState["side:one:temporary-child"] == "unavailable:false" && beforeExpiry.temporalState != CueWatcherProjectionBuilder.temporalState(details: beforeExpiry.details, sideChats: beforeExpiry.sideChats, now: later)
        let reader = CueWatcherProjectionReader(); var delivered: [Int] = []
        for number in 0..<3 { reader.project(["sequence": number], home: home) { prepared in delivered.append(prepared.state["sequence"] as? Int ?? -1) } }
        let deadline = Date().addingTimeInterval(2)
        while reader.metrics["completed"] != 1 && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.002)) }
        return ["watcher_content_projection_preserves_exact_unicode_tail": bodyEqual,
            "watcher_unchanged_detail_reuses_full_projection": unchanged,
            "watcher_numeric_change_reuses_only_immutable_content": numericExact,
            "watcher_content_change_invalidates_tail_cache": changedBody,
            "watcher_json_bool_cannot_reuse_numeric_token_value": invalid.totalTokens == nil,
            "watcher_expired_snapshot_cannot_reuse_live_model_settings": live.live && !expired.live && expired.details.isEmpty,
            "watcher_latest_only_projection_discards_superseded_completions": delivered == [2] && reader.metrics["completed"] == 1 && reader.metrics["superseded"] == 1,
            "watcher_time_expiry_invalidates_identical_raw_model_and_sidechat_snapshot": expiryInvalidates,
            "watcher_projection_cache_is_bounded": builder.metrics["cached_details", default: 0] <= 512]
    }

    static func performanceMeasurement() -> [String: Any] {
        let row: [String: Any] = ["status":"running", "total_tokens":1234567, "tokens_status":"complete",
            "messages":[["role":"assistant", "text":String(repeating:"缓存正文\n👩🏽‍💻", count:800), "created_at":1000.0]]]
        let loops = 16
        var directChecksum = 0, reusedChecksum = 0
        let start = ProcessInfo.processInfo.systemUptime
        for number in 0..<loops {
            var raw = row; raw["total_tokens"] = number
            let detail = CueTaskDetail(raw: raw)
            directChecksum += Int(detail.totalTokens ?? 0) + (detail.messages.first?.text.count ?? 0)
        }
        let direct = ProcessInfo.processInfo.systemUptime - start
        let builder = CueWatcherProjectionBuilder(), next = ProcessInfo.processInfo.systemUptime
        for number in 0..<loops {
            var raw = row; raw["total_tokens"] = number
            let detail = builder.detail(raw, id: "bounded-fixture")
            reusedChecksum += Int(detail.totalTokens ?? 0) + (detail.messages.first?.text.count ?? 0)
        }
        let reused = ProcessInfo.processInfo.systemUptime - next
        return ["iterations":loops, "direct_wall_ms":direct * 1000, "reused_wall_ms":reused * 1000,
            "contents_parsed":builder.parsedContents, "contents_reused":builder.contentHits,
            "exact_output_checksum_equal":directChecksum == reusedChecksum,
            "scope":"isolated one-chat immutable Unicode tail with changing exact integer telemetry"]
    }
}
