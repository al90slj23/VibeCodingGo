import Foundation

/// One cached deadline on the existing activity clock. No file reads, hashes,
/// discovery or subprocesses occur while an update is not due.
final class CueEngineeringMaintenanceMonitor {
    typealias Runner = ([String], @escaping (Result<[String: Any], Error>) -> Void) -> Void
    private let home: URL
    private let clock: () -> TimeInterval
    private let idle: () -> Bool
    private let fixtureRun: Runner?
    private var client: CueUsageClient?
    private var lease: CueActivityLease?
    private var started = false, busy = false
    private var nextAttempt: TimeInterval
    private var generation = 0, runs = 0, failures = 0
    private var configRevision: Int64 = -1, stateRevision: Int64 = -1
    private var automatic = true
    private(set) var snapshot: [String: Any]?
    var didUpdate: (([String: Any]) -> Void)?

    init(home: URL, clock: @escaping () -> TimeInterval = { Date().timeIntervalSince1970 }, idle: @escaping () -> Bool = {
        let metrics = CueCollectorScheduler.shared.metrics
        return metrics["engineering_active", default: 0] == 0 && metrics["engineering_queued", default: 0] == 0 && metrics["maintenance_active", default: 0] == 0 && metrics["maintenance_queued", default: 0] == 0 && metrics["network_active", default: 0] == 0 && metrics["network_queued", default: 0] == 0
    }, runner: Runner? = nil) {
        self.home = home; self.clock = clock; self.idle = idle; fixtureRun = runner
        nextAttempt = clock() + 120
    }
    func start(schedule: Bool = true) {
        guard !started else { return }; started = true
        if schedule {
            lease = CueActivityScheduler.shared.observe(key: "engineering.maintenance", interval: 60, isActive: { [weak self] in self?.isDue == true }) { [weak self] in self?.pulse() }
        }
    }
    private var isDue: Bool { started && !busy && automatic && clock() >= nextAttempt }
    func pulse() {
        guard isDue, idle() else { return }
        busy = true; runs += 1; generation += 1
        let expected = generation, revisionAtStart = configRevision
        nextAttempt = clock() + 900
        let completion: (Result<[String: Any], Error>) -> Void = { [weak self] value in
            guard let self, self.started, self.generation == expected else { return }
            self.busy = false
            switch value {
            case .success(let data):
                if self.consumeSnapshot(data) { self.failures = 0 }
                else if self.configRevision == revisionAtStart { self.retryLater() }
            case .failure: if self.configRevision == revisionAtStart { self.retryLater() }
            }
        }
        if let fixtureRun { fixtureRun(["maintain"], completion) }
        else {
            if client == nil { client = CueUsageClient(home: home) }
            client?.run(["maintain"], module: "vibecue.engineering_maintenance", completion: completion)
        }
    }
    private func retryLater() {
        failures = min(5, failures + 1)
        nextAttempt = clock() + min(21600, 900 * pow(2, Double(failures - 1)))
    }
    /// Configuration and explicit check responses use the same authoritative
    /// schedule, without a second status process or main-thread disk read.
    @discardableResult func consumeSnapshot(_ data: [String: Any]) -> Bool {
        guard data["schema_version"] as? Int == 1, data["kind"] as? String == "engineering_settings",
              let config = data["config"] as? [String: Any], let enabled = config["automatic_updates"] as? Bool else { return false }
        let revision = (data["config_revision"] as? NSNumber)?.int64Value ?? 0
        let state = (data["state_revision"] as? NSNumber)?.int64Value ?? 0
        if revision < configRevision || (revision == configRevision && state < stateRevision) { return true }
        configRevision = revision; stateRevision = state
        automatic = enabled; snapshot = data
        if !enabled { nextAttempt = .greatestFiniteMagnitude }
        else if let stamp = (data["next_check_at"] as? NSNumber)?.doubleValue, stamp.isFinite, stamp > 0 {
            nextAttempt = min(clock() + 8 * 86400, max(clock() + 60, stamp))
        } else { nextAttempt = clock() + 3600 }
        didUpdate?(data)
        return true
    }
    func stop() {
        started = false; generation += 1; busy = false
        lease?.cancel(); lease = nil; client?.stop(); client = nil
    }
    var performanceSnapshot: [String: Any] {
        ["started": started, "busy": busy, "automatic": automatic, "client_created": client != nil,
         "checks_started": runs, "next_attempt_at": automatic ? nextAttempt : 0, "failure_backoff": failures]
    }
    static func validationReport() -> [String: Bool] {
        var now: TimeInterval = 1000, canRun = true, callbacks: [(Result<[String: Any], Error>) -> Void] = [], commands: [[String]] = []
        let monitor = CueEngineeringMaintenanceMonitor(home: URL(fileURLWithPath: "/tmp/vcgo-update-fixture"), clock: { now }, idle: { canRun }, runner: { args, completion in commands.append(args); callbacks.append(completion) })
        let lazy = monitor.client == nil && monitor.runs == 0 && monitor.lease == nil
        monitor.start(schedule: false); monitor.pulse(); now = 1119; monitor.pulse()
        let startup = commands.isEmpty
        now = 1120; canRun = false; monitor.pulse()
        let idle = commands.isEmpty
        canRun = true; monitor.pulse(); monitor.pulse()
        let one = commands == [["maintain"]] && monitor.busy
        let data: [String: Any] = ["schema_version":1, "kind":"engineering_settings", "config":["automatic_updates":true, "interval_hours":24], "next_check_at": now + 86400, "tools":[]]
        callbacks[0](.success(data)); now += 86399; monitor.pulse()
        let cached = commands.count == 1 && monitor.snapshot != nil
        now += 1; monitor.pulse()
        let due = commands.count == 2
        callbacks[1](.failure(NSError(domain:"fixture",code:1))); monitor.pulse()
        let backoff = commands.count == 2 && monitor.nextAttempt == now + 900
        let disabled: [String: Any] = ["schema_version":1,"kind":"engineering_settings","config":["automatic_updates":false,"interval_hours":24],"tools":[]]
        _ = monitor.consumeSnapshot(disabled); now += 1000000; monitor.pulse()
        let off = commands.count == 2 && !monitor.automatic
        _ = monitor.consumeSnapshot(data); now += 100; monitor.pulse()
        let enabled = commands.count == 3
        monitor.stop(); var late = data; late["summary"] = "late"; callbacks[2](.success(late)); now += 1000000; monitor.pulse()
        let ordered = CueEngineeringMaintenanceMonitor(home: URL(fileURLWithPath:"/tmp/vcgo-update-order-fixture"), clock:{ now }, runner:{ _, _ in fatalError("ordering started work") })
        var checking = data; checking["config_revision"] = 2; checking["state_revision"] = 10; checking["summary"] = "checking"
        var completed = checking; completed["state_revision"] = 11; completed["summary"] = "completed"; completed["next_check_at"] = now + 9000
        _ = ordered.consumeSnapshot(completed); _ = ordered.consumeSnapshot(checking)
        let orderedState = ordered.snapshot?["summary"] as? String == "completed" && ordered.nextAttempt == now + 9000
        var disabledNew = completed; disabledNew["config_revision"] = 3; disabledNew["config"] = ["automatic_updates":false,"interval_hours":24]
        _ = ordered.consumeSnapshot(disabledNew); checking["state_revision"] = 99; _ = ordered.consumeSnapshot(checking)
        let orderedConfig = !ordered.automatic && ordered.configRevision == 3
        return ["engineering_updates_late_state_and_configuration_cannot_regress_schedule":orderedState && orderedConfig,
                "engineering_updates_construct_without_io_client_or_jobs":lazy,
                "engineering_updates_delay_startup_and_use_one_cached_deadline":startup && cached,
                "engineering_updates_defer_while_engineering_history_or_network_lane_is_busy":idle,
                "engineering_updates_are_single_flight_on_existing_activity_clock":one,
                "engineering_updates_run_once_when_due_without_catchup_burst":due,
                "engineering_updates_transport_failures_back_off_without_polling":backoff,
                "engineering_updates_disabled_setting_stops_automatic_checks":off,
                "engineering_updates_setting_response_resumes_without_status_process":enabled,
                "engineering_updates_stop_rejects_late_completion":monitor.snapshot?["summary"] as? String != "late" && !monitor.started && !monitor.busy && commands.count == 3]
    }
}
