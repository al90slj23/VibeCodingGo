import AppKit
import CoreFoundation
import Foundation

/// Narrow native actions used after the maintenance worker has prepared and
/// verified a replacement. This helper never reads credentials or kills an app.
enum CueCCSwitchMaintenance {
    private static let identifier = "com.ccswitch.desktop"
    private static let applicationURL = URL(fileURLWithPath: "/Applications/CC Switch.app", isDirectory: true)
    private static let waitSeconds: TimeInterval = 15

    static func run(action: String, payload: [String: Any]) -> [String: Any] {
        guard ["probe", "quit", "relaunch"].contains(action) else {
            return result(action, status: "invalid_request", reason: "unknown_action")
        }
        let allowed: Set<String> = action == "quit" ? ["expected_pid"] : []
        guard Set(payload.keys).isSubset(of: allowed) else {
            return result(action, status: "invalid_request", reason: "unsupported_fields")
        }
        let expectedPID = action == "quit" ? processIdentifier(payload["expected_pid"]) : nil
        if action == "quit", expectedPID == nil {
            return result(action, status: "invalid_request", reason: "expected_pid_required")
        }
        guard Bundle(url: applicationURL)?.bundleIdentifier == identifier else {
            return result(action, status: "unavailable", reason: "application_identity_unverified")
        }
        let applications = NSRunningApplication.runningApplications(withBundleIdentifier: identifier).filter { !$0.isTerminated }
        guard applications.count <= 1 else {
            return result(action, status: "waiting_idle", reason: "multiple_instances", running: true)
        }
        let application = applications.first
        if let application, !sameApplication(application) {
            return result(action, status: "waiting_idle", reason: "application_path_changed", application: application)
        }
        switch action {
        case "probe":
            return result(action, status: application == nil ? "not_running" : "running", running: application != nil, application: application)
        case "quit":
            guard let application else {
                return result(action, status: "not_running", reason: "expected_process_already_exited", running: false)
            }
            guard application.processIdentifier == expectedPID else {
                return result(action, status: "waiting_idle", reason: "process_changed", application: application)
            }
            guard !application.isActive else {
                return result(action, status: "waiting_idle", reason: "application_frontmost", application: application)
            }
            guard application.terminate() else {
                return result(action, status: "waiting_exit", reason: "normal_exit_not_accepted", application: application)
            }
            let deadline = Date().addingTimeInterval(waitSeconds)
            while !application.isTerminated, Date() < deadline {
                RunLoop.current.run(until: min(deadline, Date().addingTimeInterval(0.05)))
            }
            guard application.isTerminated else {
                return result(action, status: "waiting_exit", reason: "normal_exit_pending", application: application)
            }
            // An updater or the person may have relaunched the app while the
            // original process was exiting. Never terminate that new process.
            let remaining = NSRunningApplication.runningApplications(withBundleIdentifier: identifier).filter { !$0.isTerminated }
            guard remaining.isEmpty else {
                return result(action, status: "waiting_idle", reason: "process_relaunched", application: remaining.first)
            }
            return result(action, status: "stopped", running: false)
        default:
            if let application {
                return result(action, status: "running", reason: "already_running", application: application)
            }
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = false
            configuration.hides = true
            configuration.addsToRecentItems = false
            configuration.createsNewApplicationInstance = false
            var completed = false
            var launched: NSRunningApplication?
            var launchFailed = false
            NSWorkspace.shared.openApplication(at: applicationURL, configuration: configuration) { application, error in
                launched = application
                launchFailed = error != nil
                completed = true
            }
            let deadline = Date().addingTimeInterval(waitSeconds)
            while !completed, Date() < deadline {
                RunLoop.current.run(until: min(deadline, Date().addingTimeInterval(0.05)))
            }
            guard completed else {
                return result(action, status: "waiting_launch", reason: "normal_launch_pending")
            }
            guard !launchFailed, let launched, sameApplication(launched), !launched.isTerminated else {
                return result(action, status: "unavailable", reason: "normal_launch_failed")
            }
            return result(action, status: "started", application: launched)
        }
    }

    private static func sameApplication(_ application: NSRunningApplication) -> Bool {
        application.bundleIdentifier == identifier && application.bundleURL?.standardizedFileURL.resolvingSymlinksInPath() == applicationURL.standardizedFileURL.resolvingSymlinksInPath()
    }

    private static func processIdentifier(_ raw: Any?) -> pid_t? {
        guard let value = raw as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID() else { return nil }
        let number = value.doubleValue
        guard number.isFinite, number > 1, number <= Double(Int32.max), number.rounded(.towardZero) == number else { return nil }
        return pid_t(number)
    }

    private static func result(_ action: String, status: String, reason: String = "", running: Bool? = nil, application: NSRunningApplication? = nil) -> [String: Any] {
        let isRunning = application.map { !$0.isTerminated } ?? running
        var document: [String: Any] = ["action": action, "status": status, "reason": reason,
                                       "app_path": applicationURL.path,
                                       "is_running": NSNull(), "pid": NSNull(), "is_frontmost": NSNull()]
        if let isRunning { document["is_running"] = isRunning }
        if let application {
            document["pid"] = application.processIdentifier
            document["is_frontmost"] = application.isActive
        } else if isRunning == false {
            document["is_frontmost"] = false
        }
        return document
    }

    static func validationReport() -> [String: Bool] {
        ["ccswitch_maintenance_rejects_unknown_actions": run(action: "delete", payload: [:])["reason"] as? String == "unknown_action",
         "ccswitch_maintenance_requires_the_expected_process": run(action: "quit", payload: [:])["reason"] as? String == "expected_pid_required",
         "ccswitch_maintenance_rejects_boolean_pid": run(action: "quit", payload: ["expected_pid": true])["reason"] as? String == "expected_pid_required",
         "ccswitch_maintenance_rejects_fractional_pid": run(action: "quit", payload: ["expected_pid": 2.5])["reason"] as? String == "expected_pid_required",
         "ccswitch_maintenance_does_not_accept_an_arbitrary_app": run(action: "relaunch", payload: ["app_path": "/Applications/Other.app"])["reason"] as? String == "unsupported_fields"]
    }
}
