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
    // The legacy branch is used only by isolated differential fixtures. The
    // live reader always composes its signature from already encoded details.
    private let referenceSignatureEncoding: Bool
    private static let contentKeys: Set<String> = ["last_message", "last_message_at", "last_role", "messages", "content_truncated"]
    private(set) var parsedDetails = 0, detailHits = 0, parsedContents = 0, contentHits = 0
    private var jsonEncodings = 0, jsonEncodedBytes = 0, signatureDetailReencodes = 0
    init(referenceSignatureEncoding: Bool = false) { self.referenceSignatureEncoding = referenceSignatureEncoding }
    var metrics: [String: Int] { ["details_parsed": parsedDetails, "details_reused": detailHits,
        "contents_parsed": parsedContents, "contents_reused": contentHits, "cached_details": entries.count,
        "json_encodings": jsonEncodings, "json_encoded_bytes": jsonEncodedBytes,
        "signature_detail_reencodes": signatureDetailReencodes] }
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
    private func json(_ value: Any) -> Data {
        jsonEncodings += 1
        let data = (try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])) ?? Data()
        jsonEncodedBytes += data.count
        return data
    }
    /// Lengths are unsigned 64-bit big endian; both the type/domain and payload
    /// are framed. No delimiter escaping, hash collisions or bool/int bridging.
    fileprivate static func appendFrame(_ domain: String, _ payload: Data, to target: inout Data) {
        func appendLength(_ length: Int) {
            var value = UInt64(length).bigEndian
            withUnsafeBytes(of: &value) { target.append(contentsOf: $0) }
        }
        let tag = Data(domain.utf8)
        appendLength(tag.count); target.append(tag)
        appendLength(payload.count); target.append(payload)
    }
    func detail(_ raw: [String: Any], id: String) -> CueTaskDetail { projectedDetail(raw, id: id).detail }
    private func projectedDetail(_ raw: [String: Any], id: String) -> (detail: CueTaskDetail, canonical: Data) {
        let full = json(raw)
        if let cached = entries[id], !full.isEmpty, cached.raw == full { detailHits += 1; return (cached.detail, full) }
        let tail = json(raw.filter { Self.contentKeys.contains($0.key) })
        let content: CueTaskDetailContent
        if let cached = entries[id], !tail.isEmpty, cached.contentRaw == tail { content = cached.content; contentHits += 1 }
        else { content = CueTaskDetailContent(raw: raw); parsedContents += 1 }
        let result = CueTaskDetail(raw: raw, preparedContent: content); parsedDetails += 1
        if entries[id] != nil || entries.count < capacity {
            entries[id] = Entry(raw: full, contentRaw: tail, detail: result, content: content)
        }
        return (result, full)
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
        var details: [String: CueTaskDetail] = [:], canonicalDetails: [String: Data] = [:]
        if live {
            for (id, row) in rows {
                let projected = projectedDetail(row, id: id)
                details[id] = projected.detail; canonicalDetails[id] = projected.canonical
            }
        }
        let taskRows = live ? state["running_tasks"] as? [[String: Any]] ?? [] : []
        let running = taskRows.compactMap { CueRunningTask(raw: $0) }
        var signature: Data
        if referenceSignatureEncoding {
            signatureDetailReencodes += live ? rows.count : 0
            signature = live ? json(["tasks": taskRows, "details": rows]) : Data()
        } else {
            signature = Data("vcgo-watcher-projection-v2".utf8)
            Self.appendFrame("live:bool", Data([live ? 1 : 0]), to: &signature)
            if live {
                // Array order is significant; dictionary insertion order is not.
                Self.appendFrame("tasks:json-array", json(taskRows), to: &signature)
                for id in canonicalDetails.keys.sorted() {
                    var framed = Data()
                    Self.appendFrame("id:utf8", Data(id.utf8), to: &framed)
                    Self.appendFrame("detail:canonical-json", canonicalDetails[id]!, to: &framed)
                    Self.appendFrame("detail-entry", framed, to: &signature)
                }
            }
        }
        var sideState = state["side_chats"] as? [String: Any] ?? [:]
        if !live { sideState["state"] = "disconnected" }
        let sideChats = CueSideChat.grouped(raw: sideState, now: now)
        let sideData = json(sideState)
        if referenceSignatureEncoding { signature.append(sideData) }
        else { Self.appendFrame("side-chats:json-object", sideData, to: &signature) }
        let recovery = CueTaskRecoverySnapshot.read(home: home, state: state, live: live, now: now)
        let active = Set(running.map(\.threadID))
        let interrupted = recovery.tasks.filter { !active.contains($0.threadID) }
        for task in interrupted { if let value = recovery.details[task.threadID] { details[task.threadID] = value } }
        let recoveryRows = interrupted.map { ["thread_id": $0.threadID, "title": $0.title,
            "status": $0.recoveryStatus, "reason": $0.recoveryReason,
            "interrupted_at": $0.interruptedAt?.timeIntervalSince1970 ?? 0,
            "last_activity": $0.updatedDate.timeIntervalSince1970] as [String: Any] }
        let recoveryData = json(recoveryRows)
        if referenceSignatureEncoding { signature.append(recoveryData) }
        else { Self.appendFrame("recovery:json-array", recoveryData, to: &signature) }
        let temporal = Self.temporalState(details: details, sideChats: sideChats, now: now)
        let temporalData = json(temporal)
        if referenceSignatureEncoding { signature.append(temporalData) }
        else { Self.appendFrame("temporal:json-object", temporalData, to: &signature) }
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

    /// Observable raw and derived fields, excluding intentionally different
    /// signature bytes and diagnostic counters. Only isolated fixtures call it.
    private static func featureData(_ snapshot: CueWatcherPreparedSnapshot, now: Date) -> Data {
        func date(_ value: Date?) -> String { value.map { String($0.timeIntervalSince1970) } ?? "nil" }
        func integer(_ value: Int64?) -> String { value.map { String($0) } ?? "nil" }
        func task(_ value: CueRunningTask) -> [String] {
            [value.threadID, value.title, value.provider, date(value.startDate), date(value.updatedDate),
             value.turnID, value.recoveryStatus, date(value.interruptedAt), value.recoveryReason]
        }
        var detailRows: [[String: Any]] = []
        for id in snapshot.details.keys.sorted() {
            guard let value = snapshot.details[id] else { continue }
            let scalar: [String] = [value.status, value.turnID, date(value.startedAt), date(value.finishedAt),
                date(value.lastActivity), value.elapsedSeconds.map { String($0) } ?? "nil", value.elapsedStatus,
                value.model, value.reasoningEffort, value.serviceTier, value.currentSettingsStatus,
                value.currentSettingsSource, date(value.currentSettingsObservedAt), integer(value.totalTokens),
                integer(value.inputTokens), integer(value.outputTokens), integer(value.cacheReadTokens),
                integer(value.cacheCreationTokens), value.tokensStatus, value.lastMessage,
                date(value.lastMessageAt), value.lastRole, String(value.contentTruncated),
                value.modelSelectionLine, value.speedSelectionLine, value.settingsTooltip,
                String(value.hasReadableCurrentSettings(at: now))]
            let usage: [String] = [value.turnUsage.tokenLine, value.turnUsage.costLine, value.turnUsage.tooltip,
                value.historyUsage.tokenLine, value.historyUsage.costLine, value.historyUsage.tooltip]
            let messages = value.messages.map { [$0.role, $0.text, date($0.date)] }
            detailRows.append(["id": id, "scalar": scalar, "usage": usage, "messages": messages])
        }
        var sideRows: [[String]] = []
        for parent in snapshot.sideChats.keys.sorted() {
            for child in snapshot.sideChats[parent] ?? [] {
                sideRows.append([parent, child.threadID, child.parentThreadID, child.title, child.source,
                    date(child.observedAt), date(child.verifiedAt), date(child.expiresAt), child.status(at: now),
                    String(child.canOpen(at: now))])
            }
        }
        let fields: [String: Any] = ["state": snapshot.state, "launch": snapshot.launchState, "live": snapshot.live,
            "running": snapshot.running.map(task), "interrupted": snapshot.interrupted.map(task),
            "details": detailRows, "side_chats": sideRows, "temporal": snapshot.temporalState]
        return (try? JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys])) ?? Data()
    }

    private static func longSnapshotFixture(now: Date, count: Int = 8) -> [String: Any] {
        var details: [String: [String: Any]] = [:], tasks: [[String: Any]] = [], settings: [String: [String: Any]] = [:]
        let tail = String(repeating: "长正文👩🏽‍💻\u{0001}\n\"\\/", count: 512)
        for index in 0..<count {
            let id = "fixture-\(index)"
            let token = Int64(9_007_199_254_740_993) + Int64(index)
            details[id] = ["thread_id": id, "turn_id": "turn-\(index)", "status": "running", "started_at": now.timeIntervalSince1970 - 20,
                "last_activity": now.timeIntervalSince1970, "total_tokens": token, "tokens_status": "complete",
                "last_message": tail, "last_message_at": now.timeIntervalSince1970, "last_role": "assistant",
                "messages": [["role": "user", "text": "任务 \(index)" + tail, "created_at": now.timeIntervalSince1970 - 10],
                             ["role": "assistant", "text": tail, "created_at": now.timeIntervalSince1970]],
                "turn_usage": ["total_tokens": token, "tokens_status": "complete", "cost_status": "complete", "cost_usd": 0.1234, "currency": "USD"],
                "history_usage": ["total_tokens": token, "tokens_status": "partial", "cost_status": "partial", "cost_cny": 1.2345, "currency": "CNY"]]
            tasks.append(["thread_id": id, "turn_id": "turn-\(index)", "title": "任务 \(index)", "started_at": now.timeIntervalSince1970 - 20, "last_activity": now.timeIntervalSince1970])
            settings[id] = ["status": "current", "source": "codex_desktop", "model": "fixture-model", "reasoning_effort": "high",
                "verified_at": now.timeIntervalSince1970, "expires_at": now.addingTimeInterval(60).timeIntervalSince1970]
        }
        return ["running": true, "heartbeat": now.timeIntervalSince1970, "task_details": details, "running_tasks": tasks, "current_model_settings": settings]
    }

    private static func signatureValidation(now: Date, home: URL) -> [String: Bool] {
        let raw = longSnapshotFixture(now: now, count: 2)
        let originalRows = raw["task_details"] as! [String: [String: Any]]
        var cases: [([String: Any], Date)] = [(raw, now)]
        var reordered = raw
        reordered["task_details"] = Dictionary(uniqueKeysWithValues: originalRows.keys.sorted().reversed().map { ($0, originalRows[$0]!) })
        cases.append((reordered, now))
        var modelChanged = raw, settings = raw["current_model_settings"] as! [String: [String: Any]]
        settings["fixture-0"]?["model"] = "another-model"; modelChanged["current_model_settings"] = settings
        cases.append((modelChanged, now))
        for value: Any in [true, 1, NSNull()] {
            var next = raw, rows = originalRows
            rows["fixture-0"]?["total_tokens"] = value; next["task_details"] = rows
            cases.append((next, now))
        }
        var taskOrder = raw
        taskOrder["running_tasks"] = Array((raw["running_tasks"] as! [[String: Any]]).reversed())
        cases.append((taskOrder, now))
        var renamed = raw, renamedRows = originalRows
        renamedRows["fixture-00"] = renamedRows.removeValue(forKey: "fixture-0")
        renamed["task_details"] = renamedRows; cases.append((renamed, now))
        var removed = raw, removedRows = originalRows
        removedRows.removeValue(forKey: "fixture-0"); removed["task_details"] = removedRows; cases.append((removed, now))
        var nullAdded = raw, nullRows = originalRows
        nullRows["fixture-0"]?["unknown_future_field"] = NSNull(); nullAdded["task_details"] = nullRows; cases.append((nullAdded, now))
        var expiry = raw, expiringSettings = raw["current_model_settings"] as! [String: [String: Any]]
        expiringSettings["fixture-0"]?["expires_at"] = now.addingTimeInterval(1).timeIntervalSince1970
        expiry["current_model_settings"] = expiringSettings
        cases.append((expiry, now)); cases.append((expiry, now.addingTimeInterval(2)))
        cases.append((raw, now.addingTimeInterval(16)))
        let reference = CueWatcherProjectionBuilder(referenceSignatureEncoding: true), composed = CueWatcherProjectionBuilder()
        var oldSignatures: [Data] = [], newSignatures: [Data] = [], featuresEqual = true
        for (state, date) in cases {
            let old = reference.prepare(state, home: home, now: date), new = composed.prepare(state, home: home, now: date)
            oldSignatures.append(old.signature); newSignatures.append(new.signature)
            featuresEqual = featuresEqual && featureData(old, now: date) == featureData(new, now: date)
        }
        var sameChangeDecisions = true
        for left in cases.indices {
            for right in cases.indices {
                sameChangeDecisions = sameChangeDecisions && ((oldSignatures[left] == oldSignatures[right]) == (newSignatures[left] == newSignatures[right]))
            }
        }
        var first = Data(), second = Data(), third = Data()
        CueWatcherProjectionBuilder.appendFrame("id", Data("a".utf8), to: &first)
        CueWatcherProjectionBuilder.appendFrame("json", Data("bc".utf8), to: &first)
        CueWatcherProjectionBuilder.appendFrame("id", Data("ab".utf8), to: &second)
        CueWatcherProjectionBuilder.appendFrame("json", Data("c".utf8), to: &second)
        CueWatcherProjectionBuilder.appendFrame("i", Data("da".utf8), to: &third)
        CueWatcherProjectionBuilder.appendFrame("json", Data("bc".utf8), to: &third)
        return ["watcher_composed_signature_matches_reference_change_decisions": sameChangeDecisions,
            "watcher_composed_signature_preserves_all_observed_features": featuresEqual,
            "watcher_signature_ignores_dictionary_insertion_order": newSignatures[0] == newSignatures[1],
            "watcher_signature_detects_model_only_change": newSignatures[0] != newSignatures[2],
            "watcher_signature_keeps_bool_integer_and_null_distinct": newSignatures[3] != newSignatures[4] && newSignatures[4] != newSignatures[5] && newSignatures[3] != newSignatures[5],
            "watcher_signature_keeps_task_array_order_and_detail_ids": newSignatures[0] != newSignatures[6] && newSignatures[0] != newSignatures[7],
            "watcher_signature_keeps_removed_and_unknown_null_fields": newSignatures[0] != newSignatures[8] && newSignatures[0] != newSignatures[9],
            "watcher_signature_reused_json_observes_clock_expiry": newSignatures[10] != newSignatures[11],
            "watcher_signature_frames_cannot_alias_id_payload_or_domain": first != second && first != third,
            "watcher_composed_signature_never_reencodes_detail_payloads": reference.metrics["signature_detail_reencodes", default: 0] > 0 && composed.metrics["signature_detail_reencodes"] == 0]
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
        var report = ["watcher_content_projection_preserves_exact_unicode_tail": bodyEqual,
            "watcher_unchanged_detail_reuses_full_projection": unchanged,
            "watcher_numeric_change_reuses_only_immutable_content": numericExact,
            "watcher_content_change_invalidates_tail_cache": changedBody,
            "watcher_json_bool_cannot_reuse_numeric_token_value": invalid.totalTokens == nil,
            "watcher_expired_snapshot_cannot_reuse_live_model_settings": live.live && !expired.live && expired.details.isEmpty,
            "watcher_latest_only_projection_discards_superseded_completions": delivered == [2] && reader.metrics["completed"] == 1 && reader.metrics["superseded"] == 1,
            "watcher_time_expiry_invalidates_identical_raw_model_and_sidechat_snapshot": expiryInvalidates,
            "watcher_projection_cache_is_bounded": builder.metrics["cached_details", default: 0] <= 512]
        report.merge(signatureValidation(now: now, home: home)) { _, new in new }
        return report
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
        let now = Date(), home = FileManager.default.temporaryDirectory.appendingPathComponent("vcgo-watcher-signature-" + UUID().uuidString)
        let fixture = longSnapshotFixture(now: now)
        let reference = CueWatcherProjectionBuilder(referenceSignatureEncoding: true), composed = CueWatcherProjectionBuilder()
        var states: [[String: Any]] = []
        for number in 0..<loops {
            var state = fixture, rows = fixture["task_details"] as! [String: [String: Any]]
            for id in rows.keys {
                rows[id]?["elapsed_seconds"] = number
                rows[id]?["total_tokens"] = Int64(9_007_199_254_740_993) + Int64(number)
            }
            state["task_details"] = rows; states.append(state)
        }
        var referenceSnapshots: [CueWatcherPreparedSnapshot] = [], composedSnapshots: [CueWatcherPreparedSnapshot] = []
        let referenceStart = ProcessInfo.processInfo.systemUptime
        for state in states { referenceSnapshots.append(reference.prepare(state, home: home, now: now)) }
        let referenceWall = ProcessInfo.processInfo.systemUptime - referenceStart
        let composedStart = ProcessInfo.processInfo.systemUptime
        for state in states { composedSnapshots.append(composed.prepare(state, home: home, now: now)) }
        let composedWall = ProcessInfo.processInfo.systemUptime - composedStart
        let featuresEqual = zip(referenceSnapshots, composedSnapshots).allSatisfy { featureData($0.0, now: now) == featureData($0.1, now: now) }
        let counters = ["reference": reference.metrics, "composed": composed.metrics]
        return ["iterations":loops, "direct_wall_ms":direct * 1000, "reused_wall_ms":reused * 1000,
            "contents_parsed":builder.parsedContents, "contents_reused":builder.contentHits,
            "exact_output_checksum_equal":directChecksum == reusedChecksum && featuresEqual,
            "snapshot_detail_count": 8, "snapshot_reference_wall_ms": referenceWall * 1000, "snapshot_composed_wall_ms": composedWall * 1000,
            "snapshot_exact_features_equal": featuresEqual, "snapshot_encoding_counters": counters,
            "snapshot_signature_reencoding_eliminated": reference.metrics["signature_detail_reencodes"] == 8 * loops && composed.metrics["signature_detail_reencodes"] == 0,
            "snapshot_encoded_bytes_reduced": composed.metrics["json_encoded_bytes", default: 0] < reference.metrics["json_encoded_bytes", default: 0],
            "scope":"isolated Unicode tails, one-chat parser reuse plus eight-detail exact snapshot signatures over sixteen integer updates"]
    }
}
