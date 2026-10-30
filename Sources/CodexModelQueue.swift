import Foundation

/// Driven by the existing watcher heartbeat. No timer, resident child process,
/// or window navigation is owned by the deferred settings queue. A finite
/// native worker also handles unloaded chats without waiting for a window owner.
final class CueCodexModelQueue {
    private let home: URL
    private let snapshots = CueJSONSnapshotReader()
    private var client: CueCodexControlClient?
    private var inFlight = false
    private var signature = ""
    private(set) var pendingIDs: [String] = []
    private(set) var latestResults: [[String: Any]] = []
    private var nextAttempt = Date.distantPast
    private var evaluations = 0
    private var executeOverride: ((String, @escaping (Result<[String: Any], Error>) -> Void) -> Void)?
    init(home: URL) { self.home = home }

    func update(readyIDs: Set<String>, changed: @escaping ([[String: Any]]) -> Void) {
        snapshots.read(home.appendingPathComponent("codex-model-queue.json"), limit: 1_048_576) { [weak self] data in
            guard let self else { return }
            self.evaluations += 1
            let records = data["records"] as? [[String: Any]] ?? []
            let pending = records.filter { $0["status"] as? String == "queued" }
            var ids = pending.compactMap { $0["thread_id"] as? String }.filter { CueIdentifier.isValid($0) }
            if ids.count > 48 {
                let offset = (Int(Date().timeIntervalSince1970 / 30) * 48) % ids.count
                ids = Array(ids[offset...]) + Array(ids[..<offset])
            }
            let orderChanged = self.pendingIDs != ids
            self.pendingIDs = ids
            let signature = records.map { "\($0["queue_id"] ?? ""):\($0["status"] ?? ""):\($0["updated_at"] ?? "")" }.joined(separator: "|")
            if signature != self.signature || orderChanged {
                self.signature = signature
                self.latestResults = records.compactMap { $0["result"] as? [String: Any] }
                changed(self.latestResults)
            }
            guard !self.inFlight, Date() >= self.nextAttempt else { return }
            let now = Date().timeIntervalSince1970
            let ordered = pending.filter { readyIDs.contains($0["thread_id"] as? String ?? "") }
                + pending.filter { !readyIDs.contains($0["thread_id"] as? String ?? "") }
            guard let id = ordered.first(where: { row in
                guard let id = row["thread_id"] as? String else { return false }
                return CueIdentifier.isValid(id) && (row["retry_at"] as? Double ?? 0) <= now
            })?["thread_id"] as? String else { return }
            self.inFlight = true
            let completion: (Result<[String: Any], Error>) -> Void = { [weak self] result in
                guard let self else { return }; self.inFlight = false
                if case .success(let data) = result {
                    if data["busy"] as? Bool == true { self.nextAttempt = Date().addingTimeInterval(10) }
                    for row in data["results"] as? [[String: Any]] ?? [] {
                        if let index = self.latestResults.firstIndex(where: {
                            $0["thread_id"] as? String == row["thread_id"] as? String &&
                            $0["queue_id"] as? String == row["queue_id"] as? String
                        }) { self.latestResults[index] = row }
                    }
                    // Publish the full queue: a short completed-item response
                    // must not replace buffered results for other pending jobs.
                    changed(self.latestResults)
                } else { self.nextAttempt = Date().addingTimeInterval(30) }
            }
            if let execute = self.executeOverride { execute(id, completion) }
            else {
                if self.client == nil { self.client = CueCodexControlClient(home: self.home) }
                self.client?.run("queue-drain", input: ["thread_ids": [id]], completion: completion)
            }
        }
    }
    static func validationReport() -> [String: Bool] {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent("vcgo-model-queue-" + UUID().uuidString)
        try! FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: home) }
        let monitor = CueCodexModelQueue(home: home)
        var starts: [String] = [], done: ((Result<[String: Any], Error>) -> Void)?
        monitor.executeOverride = { id, finish in starts.append(id); done = finish }
        func evaluate(_ ready: Set<String>) {
            let previous = monitor.evaluations
            monitor.update(readyIDs: ready, changed: { _ in })
            let deadline = Date().addingTimeInterval(1.5)
            while monitor.evaluations == previous && Date() < deadline { _ = RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.005)) }
        }
        evaluate([])
        var result = ["model_queue_empty_state_never_constructs_a_client_or_process": starts.isEmpty && monitor.client == nil && !monitor.inFlight]
        let row: [String: Any] = ["thread_id": "waiting-thread", "queue_id": "fixture-queue", "status": "queued", "retry_at": 0.0, "updated_at": 1.0,
            "result": ["thread_id": "waiting-thread", "queue_id": "fixture-queue", "status": "queued"]]
        try! JSONSerialization.data(withJSONObject: ["schema_version": 1, "records": [row]]).write(to: home.appendingPathComponent("codex-model-queue.json"))
        evaluate(["other-thread"])
        result["model_queue_admits_unloaded_chats_without_waiting_for_window_owners"] = starts == ["waiting-thread"] && monitor.pendingIDs == ["waiting-thread"]
        evaluate(["waiting-thread"]); evaluate(["waiting-thread"])
        result["model_queue_admits_only_one_finite_job_from_existing_health_updates"] = starts == ["waiting-thread"] && monitor.inFlight
        done?(.success(["busy": true, "results": []])); evaluate(["waiting-thread"])
        result["model_queue_busy_foreground_batch_causes_backoff_without_tight_retries"] = starts.count == 1 && !monitor.inFlight && monitor.nextAttempt > Date()
        monitor.nextAttempt = .distantPast; evaluate(["waiting-thread"])
        done?(.success(["results": [["thread_id": "waiting-thread", "queue_id": "fixture-queue", "status": "verified"]]]))
        result["model_queue_completed_job_updates_the_cached_result_before_next_file_refresh"] = monitor.latestResults.first?["status"] as? String == "verified" && !monitor.inFlight
        return result
    }
}
