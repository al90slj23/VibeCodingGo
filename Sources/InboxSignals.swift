import AppKit
import Foundation
import Darwin

/// Kernel directory signals replace repeated empty inbox scans. The existing
/// health clock remains a recovery path if a directory is replaced.
final class CueInboxDirectorySignals {
    private let queue = DispatchQueue(label: "local.vibecue.inbox-signals", qos: .utility)
    private let lock = NSLock()
    private var active = false, revision = 0
    private var counts = ["sources": 0, "notifications": 0, "coalesced": 0, "checks": 0, "repairs": 0]
    private var sources: [String: DispatchSourceFileSystemObject] = [:]
    private var identities: [String: String] = [:]
    private var deliveryPending = false, dirtyDuringDelivery = false
    private let home: URL, events: URL
    private let onChange: () -> Void
    init(home: URL, events: URL, onChange: @escaping () -> Void) {
        self.home = home; self.events = events; self.onChange = onChange
    }
    private func current(_ expected: Int) -> Bool {
        lock.lock(); defer { lock.unlock() }; return active && revision == expected
    }
    private func metric(_ key: String, value: Int? = nil) {
        lock.lock(); counts[key] = value ?? ((counts[key] ?? 0) + 1); lock.unlock()
    }
    var metrics: [String: Int] { lock.lock(); defer { lock.unlock() }; return counts }
    func start() {
        lock.lock(); guard !active else { lock.unlock(); return }
        active = true; revision += 1; let expected = revision; lock.unlock()
        queue.async { [weak self] in self?.repair(expected) }
    }
    func reconcile() {
        lock.lock(); let expected = revision; lock.unlock()
        queue.async { [weak self] in self?.repair(expected) }
    }
    func stop() {
        lock.lock(); active = false; revision += 1; lock.unlock()
        queue.async { [weak self] in
            guard let self else { return }
            self.sources.values.forEach { $0.cancel() }; self.sources.removeAll(); self.identities.removeAll()
            self.deliveryPending = false; self.dirtyDuringDelivery = false; self.metric("sources", value: 0)
        }
    }
    deinit { sources.values.forEach { $0.cancel() } }
    private static func identity(_ url: URL) -> String? {
        guard let info = try? FileManager.default.attributesOfItem(atPath: url.path),
              info[.type] as? FileAttributeType == .typeDirectory else { return nil }
        return String(describing: info[.systemNumber]) + ":" + String(describing: info[.systemFileNumber])
    }
    private func repair(_ expected: Int) {
        guard current(expected) else { return }
        metric("checks")
        for (kind, path) in [("events", events), ("settings", home)] {
            let identity = Self.identity(path)
            if let identity, identity == identities[kind], sources[kind] != nil { continue }
            sources.removeValue(forKey: kind)?.cancel(); identities.removeValue(forKey: kind)
            guard let identity else { continue }
            let fd = Darwin.open(path.path, O_EVTONLY | O_CLOEXEC)
            guard fd >= 0 else { continue }
            let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd,
                eventMask: [.write, .rename, .delete, .revoke], queue: queue)
            source.setCancelHandler { Darwin.close(fd) }
            source.setEventHandler { [weak self] in
                guard let self, self.current(expected) else { return }
                // Home also contains heartbeats and ledgers. Those writes must
                // not wake main unless the explicit settings request exists.
                if kind == "events" || FileManager.default.fileExists(atPath: self.home.appendingPathComponent("settings.request").path) {
                    self.notify(expected)
                }
            }
            sources[kind] = source; identities[kind] = identity; source.resume(); metric("repairs")
        }
        metric("sources", value: sources.count)
    }
    private func notify(_ expected: Int) {
        guard current(expected) else { return }
        if deliveryPending { dirtyDuringDelivery = true; metric("coalesced"); return }
        deliveryPending = true; metric("notifications")
        DispatchQueue.main.async { [weak self] in
            guard let self, self.current(expected) else { return }
            self.onChange()
            self.queue.async { [weak self] in
                guard let self, self.current(expected) else { return }
                self.deliveryPending = false
                if self.dirtyDuringDelivery { self.dirtyDuringDelivery = false; self.notify(expected) }
            }
        }
    }
    static func validationReport() -> [String: Bool] {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("vcgo-inbox-signals-" + UUID().uuidString)
        let events = root.appendingPathComponent("events")
        try! FileManager.default.createDirectory(at: events, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        var calls = 0
        let monitor = Self(home: root, events: events) { calls += 1 }
        func wait(_ condition: () -> Bool) {
            let end = Date().addingTimeInterval(1.5)
            while !condition(), Date() < end { RunLoop.current.run(until: Date().addingTimeInterval(0.005)) }
        }
        monitor.start(); wait { monitor.metrics["sources"] == 2 }
        let originalRepairs = monitor.metrics["repairs"]
        for _ in 0..<10 { monitor.reconcile() }
        wait { (monitor.metrics["checks"] ?? 0) >= 11 }
        let stableSources = originalRepairs == 2 && monitor.metrics["repairs"] == originalRepairs
        let initial = calls
        try! Data("heartbeat".utf8).write(to: root.appendingPathComponent("watcher-state.json"), options: .atomic)
        RunLoop.current.run(until: Date().addingTimeInterval(0.04))
        let ignoresHeartbeat = calls == initial
        try! Data("{}".utf8).write(to: events.appendingPathComponent("one.json"), options: .atomic)
        wait { calls > initial }; let eventArrived = calls > initial
        let previous = calls
        try! Data("open".utf8).write(to: root.appendingPathComponent("settings.request"), options: .atomic)
        wait { calls > previous }; let settingsArrived = calls > previous
        try? FileManager.default.removeItem(at: root.appendingPathComponent("settings.request"))
        try! FileManager.default.moveItem(at: events, to: root.appendingPathComponent("old-events"))
        try! FileManager.default.createDirectory(at: events, withIntermediateDirectories: true)
        monitor.reconcile(); wait { (monitor.metrics["repairs"] ?? 0) > (originalRepairs ?? 0) }
        let replacementStart = calls
        try! Data("{}".utf8).write(to: events.appendingPathComponent("two.json"), options: .atomic)
        wait { calls > replacementStart }; let replaced = calls > replacementStart
        monitor.stop(); let stopped = calls
        try! Data("{}".utf8).write(to: events.appendingPathComponent("three.json"), options: .atomic)
        RunLoop.current.run(until: Date().addingTimeInterval(0.04))
        return ["inbox_unchanged_directories_reuse_sources_across_health_checks": stableSources,
                "inbox_kernel_signals_deliver_atomic_event_and_settings_requests": eventArrived && settingsArrived,
                "inbox_other_home_heartbeats_do_not_wake_main": ignoresHeartbeat,
                "inbox_directory_replacement_rearms_kernel_source": replaced,
                "inbox_cancel_rejects_late_notifications_and_releases_sources": calls == stopped && monitor.metrics["sources"] == 0]
    }
}

/// Preserve the original priority membership before canonicalizing within a
/// group; activity ordering alone must not rewrite the model subscription list.
enum CueModelTargetSelection {
    static func select(_ groups: [[String]], limit: Int = 256) -> [String] {
        var seen = Set<String>(), result: [String] = []
        for group in groups {
            let remaining = max(0, limit - result.count)
            guard remaining > 0 else { break }
            var selected: [String] = []
            for value in group where CueIdentifier.isValid(value) && seen.insert(value).inserted {
                selected.append(value)
                if selected.count == remaining { break }
            }
            result.append(contentsOf: selected.sorted())
        }
        return result
    }
    static func validationReport() -> [String: Bool] {
        let first = select([["z", "a"], ["c", "a"], ["b"]])
        return ["model_target_activity_order_does_not_rewrite_same_priority_members": first == select([["a", "z"], ["c", "a"], ["b"]]),
                "model_target_canonicalization_preserves_priority_membership_and_limit": select([["z", "a", "b"], ["c"]], limit: 2) == ["a", "z"],
                "model_target_selection_deduplicates_and_rejects_invalid_paths": first == ["a", "z", "c", "b"] && select([["../x", "", "a", "a"]]) == ["a"]]
    }
}
