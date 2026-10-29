import AppKit
import Foundation

/// Identity rules are constructed once, never once per scalar in a hot loop.
enum CueIdentifier {
    private static let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_"))
    static func isValid(_ value: String, maxLength: Int = 160) -> Bool {
        !value.isEmpty && value.count <= maxLength && value.unicodeScalars.allSatisfy { allowed.contains($0) }
    }
}

final class CueActivityLease {
    let key: String
    private var cancellation: (() -> Void)?
    private let validity: () -> Bool
    var isValid: Bool { cancellation != nil && validity() }
    fileprivate init(key: String, validity: @escaping () -> Bool, cancel: @escaping () -> Void) {
        self.key = key; self.validity = validity; cancellation = cancel
    }
    func cancel() { let action = cancellation; cancellation = nil; action?() }
    deinit { cancel() }
}

/// One adaptive main-run-loop clock for recurring feature work. Each feature
/// declares identity, cadence and visibility; ownership of a lease is its lifetime.
final class CueActivityScheduler {
    static let shared = CueActivityScheduler()
    private final class Entry {
        let id = UUID(), key: String, interval: TimeInterval
        let isActive: () -> Bool, tick: () -> Void
        var due: TimeInterval
        init(key: String, interval: TimeInterval, now: TimeInterval, isActive: @escaping () -> Bool, tick: @escaping () -> Void) {
            self.key = key; self.interval = max(0.05, interval); due = now + self.interval
            self.isActive = isActive; self.tick = tick
        }
    }
    private var entries: [String: Entry] = [:]
    private var timer: Timer?, pulsing = false
    private let clock: () -> TimeInterval, automatic: Bool, maxJobs: Int, budget: TimeInterval
    private var pulses = 0, callbacks = 0, deferred = 0, slowCallbacks = 0
    private var maximumCallback: TimeInterval = 0
    init(automatic: Bool = true, maxJobs: Int = 4, budget: TimeInterval = 0.006, clock: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }) {
        self.automatic = automatic; self.maxJobs = max(1, maxJobs); self.budget = budget; self.clock = clock
    }
    @discardableResult func observe(key: String, interval: TimeInterval, isActive: @escaping () -> Bool, tick: @escaping () -> Void) -> CueActivityLease {
        precondition(Thread.isMainThread)
        let entry = Entry(key: key, interval: interval, now: clock(), isActive: isActive, tick: tick)
        entries[key] = entry; arm()
        return CueActivityLease(key: key, validity: { [weak self] in self?.entries[key]?.id == entry.id }, cancel: { [weak self] in
            let cancel = { [weak self] in
                guard let self, self.entries[key]?.id == entry.id else { return }
                self.entries.removeValue(forKey: key); self.arm()
            }
            if Thread.isMainThread { cancel() } else { DispatchQueue.main.async(execute: cancel) }
        })
    }
    func poke(key: String) {
        precondition(Thread.isMainThread)
        entries[key]?.due = clock(); arm()
    }
    private func arm() {
        guard !pulsing else { return }
        timer?.invalidate(); timer = nil
        guard automatic, let next = entries.values.map(\.due).min() else { return }
        let delay = max(0.01, next - clock())
        let timer = Timer(timeInterval: delay, repeats: false) { [weak self] _ in self?.pulse() }
        timer.tolerance = min(0.04, delay * 0.1)
        RunLoop.main.add(timer, forMode: .common); self.timer = timer
    }
    fileprivate func pulse() {
        precondition(Thread.isMainThread)
        guard !pulsing else { return }; pulsing = true
        timer?.invalidate(); timer = nil; pulses += 1
        defer { pulsing = false; arm() }
        let start = clock(); var completed = 0
        let due = entries.values.filter { $0.due <= start }.sorted { $0.due == $1.due ? $0.key < $1.key : $0.due < $1.due }
        for entry in due {
            guard entries[entry.key]?.id == entry.id else { continue }
            guard entry.isActive() else { entry.due = clock() + max(1, entry.interval); deferred += 1; continue }
            guard completed < maxJobs, completed == 0 || clock() - start < budget else { deferred += 1; continue }
            let began = clock(); entry.tick(); let duration = max(0, clock() - began)
            callbacks += 1; completed += 1; maximumCallback = max(maximumCallback, duration)
            if duration > 0.016 { slowCallbacks += 1 }
            if entries[entry.key]?.id == entry.id { entry.due = clock() + entry.interval }
        }
    }
    var metrics: [String: Any] {
        ["registered": entries.count, "pulses": pulses, "callbacks": callbacks, "deferred": deferred,
         "slow_callbacks": slowCallbacks, "maximum_callback_ms": maximumCallback * 1000, "timer_active": timer?.isValid == true]
    }
    static func validationReport() -> [String: Bool] {
        var now: TimeInterval = 0, count = 0
        let scheduler = CueActivityScheduler(automatic: false, maxJobs: 1, clock: { now })
        let old = scheduler.observe(key: "same", interval: 1, isActive: { true }) { count += 100 }
        let replacement = scheduler.observe(key: "same", interval: 1, isActive: { true }) { count += 1 }
        now = 1; scheduler.pulse()
        var report = ["scheduler_same_key_replaces_old_subscription": count == 1 && !old.isValid && replacement.isValid]
        old.cancel(); report["scheduler_old_lease_cannot_cancel_replacement"] = replacement.isValid
        let second = scheduler.observe(key: "second", interval: 1, isActive: { true }) { count += 10 }
        now = 2; scheduler.pulse(); scheduler.pulse()
        report["scheduler_fair_budget_defers_without_dropping_work"] = count == 12
        let inactive = scheduler.observe(key: "inactive", interval: 0.1, isActive: { false }) { count += 1000 }
        now = 2.2; scheduler.pulse(); report["scheduler_hidden_work_never_runs"] = count == 12
        now = 100; scheduler.pulse(); report["scheduler_no_catchup_burst_after_sleep"] = count <= 22
        replacement.cancel(); second.cancel(); inactive.cancel()
        report["scheduler_no_orphan_jobs_after_owner_cancellation"] = scheduler.entries.isEmpty
        report["identifiers_preserve_existing_unicode_and_reject_paths"] = CueIdentifier.isValid("中文-A_1") && !CueIdentifier.isValid("../secret") && !CueIdentifier.isValid("") && !CueIdentifier.isValid(String(repeating: "a", count: 161))
        return report
    }
}

/// Private JSON snapshots are read/decoded on a single utility queue. Repeated
/// requests coalesce, unchanged files reuse the immutable decoded snapshot.
final class CueJSONSnapshotReader {
    private let queue = DispatchQueue(label: "local.vibecue.snapshot-reader", qos: .utility)
    private var inFlight = false, pending: (() -> Void)?
    private var cachedStamp = "", cached: [String: Any] = [:]
    private(set) var reads = 0, cacheHits = 0
    func read(_ url: URL, limit: Int, completion: @escaping ([String: Any]) -> Void) {
        precondition(Thread.isMainThread)
        guard !inFlight else { pending = { [weak self] in self?.read(url, limit: limit, completion: completion) }; return }
        inFlight = true
        queue.async { [weak self] in
            guard let self else { return }
            var result: [String: Any] = [:]
            if let attrs = try? FileManager.default.attributesOfItem(atPath: url.path),
               let size = attrs[.size] as? NSNumber, size.intValue <= limit {
                let stamp = url.path + ":" + String(describing: attrs[.systemFileNumber]) + ":" + size.stringValue + ":" + String((attrs[.modificationDate] as? Date)?.timeIntervalSinceReferenceDate ?? 0)
                if stamp == self.cachedStamp { result = self.cached; self.cacheHits += 1 }
                else if let data = try? Data(contentsOf: url), data.count <= limit,
                        let parsed = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    self.cachedStamp = stamp; self.cached = parsed; result = parsed; self.reads += 1
                }
            }
            DispatchQueue.main.async {
                self.inFlight = false; completion(result)
                let next = self.pending; self.pending = nil; next?()
            }
        }
    }
}

struct CueInboxBatch {
    let rows: [(URL, Data)]
    let settingsRequested: Bool
}

/// Claim/read on utility work, then acknowledge only after main-thread delivery.
/// Shutdown keeps unacknowledged claims recoverable at the next launch.
final class CueEventInboxReader {
    private let queue = DispatchQueue(label: "local.vibecue.event-inbox", qos: .utility)
    private var busy = false
    func read(home: URL, events: URL, inflight: URL, synchronous: Bool = false, completion: @escaping (CueInboxBatch) -> Void) {
        guard !busy else { return }; busy = true
        let work = {
            let request = home.appendingPathComponent("settings.request")
            let requested = FileManager.default.fileExists(atPath: request.path)
            var rows: [(URL, Data)] = [], bytes = 0
            let files = (try? FileManager.default.contentsOfDirectory(at: events, includingPropertiesForKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey])) ?? []
            for file in files.filter({ $0.pathExtension == "json" }).sorted(by: { $0.lastPathComponent < $1.lastPathComponent }).prefix(100) {
                guard let properties = try? file.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey]), properties.isRegularFile == true, properties.isSymbolicLink != true, let size = properties.fileSize, size <= 1_048_576 else { continue }
                if bytes + size > 8_388_608 { break }
                let claim = inflight.appendingPathComponent(UUID().uuidString + ".json")
                do {
                    try FileManager.default.moveItem(at: file, to: claim)
                    let data = try Data(contentsOf: claim)
                    if data.count <= 1_048_576 { rows.append((claim, data)); bytes += data.count }
                    else { try? FileManager.default.removeItem(at: claim) }
                } catch { /* An unacknowledged claim is recovered after restart. */ }
            }
            return CueInboxBatch(rows: rows, settingsRequested: requested)
        }
        if synchronous { let batch = work(); busy = false; completion(batch) }
        else { queue.async { let batch = work(); DispatchQueue.main.async { self.busy = false; completion(batch) } } }
    }
    func acknowledge(_ batch: CueInboxBatch, home: URL, synchronous: Bool = false) {
        let work = {
            for (url, _) in batch.rows { try? FileManager.default.removeItem(at: url) }
            if batch.settingsRequested { try? FileManager.default.removeItem(at: home.appendingPathComponent("settings.request")) }
        }
        if synchronous { work() } else { queue.async(execute: work) }
    }
}

enum CueWorkPipelineChecks {
    static func validationReport() -> [String: Bool] {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("vcgo-work-check-" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let events = root.appendingPathComponent("events"), inflight = root.appendingPathComponent("inflight")
        try! FileManager.default.createDirectory(at: events, withIntermediateDirectories: true)
        try! FileManager.default.createDirectory(at: inflight, withIntermediateDirectories: true)
        try! Data("{\"id\":\"one\"}".utf8).write(to: events.appendingPathComponent("one.json"))
        let external = root.appendingPathComponent("external.json"); try! Data("private fixture".utf8).write(to: external)
        try! FileManager.default.createSymbolicLink(at: events.appendingPathComponent("link.json"), withDestinationURL: external)
        try! Data("open".utf8).write(to: root.appendingPathComponent("settings.request"))
        let inbox = CueEventInboxReader(); var result: [String: Bool] = [:]
        inbox.read(home: root, events: events, inflight: inflight, synchronous: true) { batch in
            result["work_inbox_ignores_symlinks_and_preserves_unacknowledged_claims"] = batch.rows.count == 1 && batch.settingsRequested && FileManager.default.fileExists(atPath: batch.rows[0].0.path) && FileManager.default.fileExists(atPath: root.appendingPathComponent("settings.request").path)
            inbox.acknowledge(batch, home: root, synchronous: true)
            result["work_inbox_acknowledges_only_after_delivery"] = !FileManager.default.fileExists(atPath: batch.rows[0].0.path) && !FileManager.default.fileExists(atPath: root.appendingPathComponent("settings.request").path) && FileManager.default.fileExists(atPath: external.path)
        }
        let file = root.appendingPathComponent("state.json"), reader = CueJSONSnapshotReader()
        try! Data("{\"value\":1}".utf8).write(to: file, options: .atomic)
        func fetch(_ limit: Int = 1024) -> [String: Any]? {
            var value: [String: Any]?
            reader.read(file, limit: limit) { value = $0 }
            let deadline = Date().addingTimeInterval(1)
            while value == nil && deadline.timeIntervalSinceNow > 0 { RunLoop.main.run(until: Date().addingTimeInterval(0.002)) }
            return value
        }
        let first = fetch(), second = fetch()
        result["work_snapshot_reuses_unchanged_decoded_state"] = first?["value"] as? Int == 1 && second?["value"] as? Int == 1 && reader.reads == 1 && reader.cacheHits == 1
        try! Data("{\"value\":2}".utf8).write(to: file, options: .atomic)
        result["work_snapshot_atomic_replacement_invalidates_cache"] = fetch()?["value"] as? Int == 2 && reader.reads == 2
        result["work_snapshot_respects_size_limit_without_stale_success"] = fetch(2)?.isEmpty == true
        return result
    }
}
