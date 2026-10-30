import Foundation

/// One admission owner for finite collector processes. No result cache, write
/// merging or replay: every accepted request still executes against live data.
final class CueCollectorScheduler {
    enum Lane: String, CaseIterable { case interactive, status, read, maintenance, network, control, engineering }
    enum Discard { case cancelled, saturated }
    typealias Work = (@escaping () -> Void) -> Void
    private struct Job {
        let id: UUID, owner: UUID
        let lane: Lane
        let submitted: TimeInterval
        let work: Work
        let discard: (Discard) -> Void
    }
    static let shared = CueCollectorScheduler()
    private let lock = NSLock()
    private let execute: (Lane, @escaping () -> Void) -> Void
    private let limits: [Lane: Int]
    private let queueLimit: Int
    private var pending: [Job] = []
    private var active: [UUID: Job] = [:]
    private var starts: [UUID: TimeInterval] = [:]
    private var completed = 0, cancelled = 0, saturated = 0, peak = 0
    private var maximumQueueMilliseconds = 0

    init(queueLimit: Int = 256, limits: [Lane: Int] = [:], execute: ((Lane, @escaping () -> Void) -> Void)? = nil) {
        self.queueLimit = max(1, queueLimit)
        var configured: [Lane: Int] = [.interactive: 2, .status: 2, .read: 2, .maintenance: 1, .network: 2, .control: 1, .engineering: 1]
        for (lane, value) in limits { configured[lane] = max(1, value) }
        self.limits = configured
        self.execute = execute ?? { lane, work in
            let qos: DispatchQoS.QoSClass = [.interactive, .status].contains(lane) ? .userInitiated : .utility
            DispatchQueue.global(qos: qos).async(execute: work)
        }
    }
    static func lane(module: String, arguments: [String]) -> Lane {
        let command = arguments.first ?? ""
        if module == "vibecue.engineering_tools" { return .engineering }
        if module == "vibecue.engineering_maintenance" {
            if ["maintain", "check"].contains(command) { return .maintenance }
            return command == "configure" ? .interactive : .read
        }
        if module == "vibecue.codex_chat" { return command == "status" ? .status : .interactive }
        if module == "vibecue.codex_control" { return ["apply", "queue-drain"].contains(command) ? .control : .status }
        if module == "vibecue.usage", command == "sync" { return .maintenance }
        if module == "vibecue.usage", command == "sync-status" { return .status }
        if module == "vibecue.network_tests" ||
            (["vibecue.usage_money", "vibecue.account_quota"].contains(module) && ["refresh", "refresh-pricing"].contains(command)) { return .network }
        if command == "save" { return .interactive }
        return .read
    }
    func submit(owner: UUID, lane: Lane, discard: @escaping (Discard) -> Void, work: @escaping Work) {
        let job = Job(id: UUID(), owner: owner, lane: lane, submitted: ProcessInfo.processInfo.systemUptime, work: work, discard: discard)
        lock.lock()
        if pending.count >= queueLimit && active.values.filter({ $0.lane == lane }).count >= limits[lane]! {
            saturated += 1; lock.unlock(); discard(.saturated); return
        }
        pending.append(job)
        let ready = admitLocked(); lock.unlock()
        start(ready)
    }
    /// Cancelling one closed view removes only its queued requests. The client
    /// retains ownership of its already running process and mutation receipt.
    func cancel(owner: UUID) {
        lock.lock()
        let removed = pending.filter { $0.owner == owner }
        pending.removeAll { $0.owner == owner }; cancelled += removed.count
        lock.unlock()
        for job in removed { job.discard(.cancelled) }
    }
    private func admitLocked() -> [Job] {
        var counts = Dictionary(grouping: active.values, by: \.lane).mapValues(\.count)
        var ready: [Job] = []
        while let index = pending.firstIndex(where: { counts[$0.lane, default: 0] < limits[$0.lane]! }) {
            let job = pending.remove(at: index)
            active[job.id] = job; counts[job.lane, default: 0] += 1
            let now = ProcessInfo.processInfo.systemUptime
            starts[job.id] = now
            maximumQueueMilliseconds = max(maximumQueueMilliseconds, Int(max(0, now - job.submitted) * 1000))
            ready.append(job)
        }
        peak = max(peak, active.count); return ready
    }
    private func start(_ jobs: [Job]) {
        for job in jobs {
            execute(job.lane) { [weak self] in job.work { [weak self] in self?.finish(job.id) } }
        }
    }
    private func finish(_ id: UUID) {
        lock.lock()
        guard active.removeValue(forKey: id) != nil else { lock.unlock(); return }
        starts.removeValue(forKey: id); completed += 1
        let ready = admitLocked(); lock.unlock()
        start(ready)
    }
    var metrics: [String: Int] {
        lock.lock(); defer { lock.unlock() }
        var result = ["active": active.count, "queued": pending.count, "completed": completed,
                      "cancelled_queued": cancelled, "rejected_before_execution": saturated,
                      "peak_active": peak, "maximum_queue_ms": maximumQueueMilliseconds]
        for lane in Lane.allCases {
            result[lane.rawValue + "_active"] = active.values.filter { $0.lane == lane }.count
            result[lane.rawValue + "_queued"] = pending.filter { $0.lane == lane }.count
            result[lane.rawValue + "_limit"] = limits[lane]
        }
        return result
    }
    static func validationReport() -> [String: Bool] {
        let scheduler = CueCollectorScheduler(execute: { _, action in action() })
        let reader = UUID(), user = UUID()
        var callbacks: [() -> Void] = [], discarded = 0, started = 0
        for _ in 0..<12 {
            scheduler.submit(owner: reader, lane: .read, discard: { _ in discarded += 1 }) { done in started += 1; callbacks.append(done) }
        }
        var results = ["collector_reads_have_one_shared_finite_admission_budget": started == 2 && scheduler.metrics["read_active"] == 2 && scheduler.metrics["read_queued"] == 10]
        scheduler.submit(owner: user, lane: .maintenance, discard: { _ in }) { done in callbacks.append(done) }
        scheduler.submit(owner: user, lane: .status, discard: { _ in }) { done in callbacks.append(done) }
        results["collector_status_does_not_wait_behind_history_reads_or_sync"] = scheduler.metrics["status_active"] == 1 && scheduler.metrics["maintenance_active"] == 1
        var sends = 0
        for _ in 0..<2 { scheduler.submit(owner: user, lane: .interactive, discard: { _ in }) { done in sends += 1; callbacks.append(done) } }
        results["collector_user_operations_are_reserved_and_never_merged"] = sends == 2 && scheduler.metrics["interactive_active"] == 2
        scheduler.cancel(owner: reader)
        results["collector_close_cancels_only_queued_requests_of_that_owner"] = discarded == 10 && scheduler.metrics["read_active"] == 2 && scheduler.metrics["queued"] == 0 && scheduler.metrics["status_active"] == 1
        callbacks.first?(); callbacks.first?()
        results["collector_completion_is_idempotent"] = scheduler.metrics["completed"] == 1 && scheduler.metrics["active"] == 5
        callbacks.dropFirst().forEach { $0() }
        results["collector_leases_release_after_all_finite_work"] = scheduler.metrics["active"] == 0 && scheduler.metrics["completed"] == 6
        let small = CueCollectorScheduler(queueLimit: 2, limits: [.read: 1], execute: { _, action in action() })
        var rejected = 0, done: (() -> Void)?
        for _ in 0..<4 { small.submit(owner: reader, lane: .read, discard: { if case .saturated = $0 { rejected += 1 } }) { _ in } }
        small.submit(owner: user, lane: .interactive, discard: { _ in }) { done = $0 }
        results["collector_saturation_is_bounded_and_cannot_block_reserved_user_lane"] = rejected == 1 && small.metrics["queued"] == 2 && small.metrics["interactive_active"] == 1
        done?(); small.cancel(owner: reader)
        let concurrent = CueCollectorScheduler()
        let group = DispatchGroup()
        for _ in 0..<40 {
            group.enter()
            concurrent.submit(owner: reader, lane: .read, discard: { _ in group.leave() }) { finished in finished(); group.leave() }
        }
        let drained = group.wait(timeout: .now() + 3) == .success
        results["collector_parallel_completions_preserve_global_bounds_and_do_not_lose_work"] = drained && concurrent.metrics["completed"] == 40 && (concurrent.metrics["peak_active"] ?? 99) <= 2 && concurrent.metrics["queued"] == 0
        results["collector_classification_separates_writes_network_and_numeric_reads"] = lane(module: "vibecue.codex_chat", arguments: ["send"]) == .interactive && lane(module: "vibecue.codex_chat", arguments: ["status"]) == .status && lane(module: "vibecue.codex_control", arguments: ["apply"]) == .control && lane(module: "vibecue.usage", arguments: ["query"]) == .read && lane(module: "vibecue.usage", arguments: ["sync"]) == .maintenance && lane(module: "vibecue.usage_money", arguments: ["refresh"]) == .network
        let engineering = CueCollectorScheduler(execute: { _, action in action() })
        let toolOwner = UUID(), other = UUID()
        var toolStarts = 0, toolDiscards = 0, toolDone: [() -> Void] = []
        for _ in 0..<3 {
            engineering.submit(owner: toolOwner, lane: .engineering, discard: { _ in toolDiscards += 1 }) { done in toolStarts += 1; toolDone.append(done) }
        }
        results["engineering_collectors_share_one_background_slot"] = toolStarts == 1 && engineering.metrics["engineering_queued"] == 2
        for lane in [Lane.status, .interactive] { engineering.submit(owner: other, lane: lane, discard: { _ in }) { done in toolDone.append(done) } }
        results["engineering_work_cannot_occupy_chat_or_status_slots"] = engineering.metrics["status_active"] == 1 && engineering.metrics["interactive_active"] == 1
        engineering.cancel(owner: toolOwner)
        results["engineering_close_removes_its_queued_work_without_touching_other_owners"] = toolDiscards == 2 && engineering.metrics["queued"] == 0 && engineering.metrics["active"] == 3
        toolDone.forEach { $0() }
        results["engineering_finished_work_releases_budget_and_classifies_all_tools"] = engineering.metrics["active"] == 0 && ["environment", "api", "logs"].allSatisfy { lane(module: "vibecue.engineering_tools", arguments: [$0]) == .engineering }
        results["engineering_update_processes_use_background_budget_and_keep_status_read_only"] = lane(module:"vibecue.engineering_maintenance",arguments:["maintain"]) == .maintenance && lane(module:"vibecue.engineering_maintenance",arguments:["check"]) == .maintenance && lane(module:"vibecue.engineering_maintenance",arguments:["status"]) == .read && lane(module:"vibecue.engineering_maintenance",arguments:["configure"]) == .interactive
        return results
    }
}
