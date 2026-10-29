import AppKit
import Foundation

/// Conversation counts, rather than alert counts: a chat can have several
/// reminders and a follow-up while occupying only one place in this summary.
struct CueMenuBarWork {
    let running: Int
    let pending: Int
    let unread: Int
    let followups: Int
    let interrupted: Int

    init(records: [CueRecentRecord], running: [CueRunningTask], interrupted: [CueRunningTask], followups: [CueThreadFollowup]) {
        let active = Set(running.map(\.threadID))
        let unread = Set(records.filter { !$0.isOpened && $0.canOpen }.map { $0.event.threadID }).union(followups.filter(\.unread).map(\.threadID))
        let retained = Set(followups.map(\.threadID))
        let recovery = Set(interrupted.filter(\.needsRecovery).map(\.threadID)).subtracting(active)
        self.running = active.count
        self.pending = unread.union(retained).union(recovery).subtracting(active).count
        self.unread = unread.count; self.followups = retained.count; self.interrupted = recovery.count
    }
}

struct CueMenuBarUsage {
    let raw: [String: Any]
    let receivedAt: Date
    let error: String
    let schedule: CueUsageSchedulePreferences

    init(raw: [String: Any] = [:], receivedAt: Date = Date(), error: String = "", schedule: CueUsageSchedulePreferences = CueUsageSchedulePreferences()) {
        self.raw = raw; self.receivedAt = receivedAt; self.error = error; self.schedule = schedule
    }
    static func day(_ date: Date) -> String {
        let formatter = DateFormatter(); formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"; return formatter.string(from: date)
    }
    func isCurrent(now: Date) -> Bool {
        guard raw["available"] as? Bool == true, raw["date"] as? String == Self.day(now),
              let start = Self.number(raw["day_start"]), let end = Self.number(raw["day_end"]),
              now.timeIntervalSince1970 >= start, now.timeIntervalSince1970 < end,
              let tokens = Self.number(summary["total_tokens"]), tokens >= 0 else { return false }
        // A timezone change can preserve the date label but change its boundary.
        return abs(start - Calendar.current.startOfDay(for: now).timeIntervalSince1970) < 1
    }
    var summary: [String: Any] { raw["summary"] as? [String: Any] ?? [:] }
    var money: [String: Any] { raw["money"] as? [String: Any] ?? [:] }
    var currency: String { (money["prefs"] as? [String: Any])?["currency"] as? String ?? "CNY" }
    static func number(_ raw: Any?) -> Double? {
        guard let value = raw as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(), value.doubleValue.isFinite else { return nil }
        return value.doubleValue
    }
    static func tokens(_ value: Double) -> String {
        for (suffix, divisor) in [("B", 1_000_000_000.0), ("M", 1_000_000.0), ("K", 1000.0)] {
            if value >= divisor { return String(format: "%.1f%@", value / divisor, suffix) }
        }
        return CueUsageDashboardController.integer(value)
    }
    func cost(now: Date) -> String {
        guard isCurrent(now: now) else { return "—" }
        let value = CueUsageMoney.display(summary, currency: currency, compact: true)
        guard Self.number(summary["cost_usd"]) != nil else { return value }
        let partial = (Self.number(summary["unpriced_requests"]) ?? 0) > 0
        return (value == "汇率未获取" ? "" : "≈") + value + (partial ? "*" : "")
    }
    func title(work: CueMenuBarWork, now: Date = Date()) -> String {
        let tokens = isCurrent(now: now) ? Self.tokens(Self.number(summary["total_tokens"])!) : "—"
        return " 运行 \(work.running) · 待办 \(work.pending)  |  今日 \(tokens) T · \(cost(now: now))"
    }
    func tooltip(work: CueMenuBarWork, health: String, now: Date = Date()) -> String {
        var lines = ["VibeCue · \(health)", "运行中 \(work.running) 个对话 · 待处理 \(work.pending) 个对话", "待处理 = 未读、待续和中断对话去重；正在运行的另计。", "未读 \(work.unread) · 待续 \(work.followups) · 中断／待确认 \(work.interrupted)"]
        if isCurrent(now: now) {
            lines += ["今天（本机时区）· 全部已接入应用", "\(CueUsageDashboardController.integer(Self.number(summary["total_tokens"]) ?? 0)) Tokens · \(CueUsageDashboardController.integer(Self.number(summary["requests"]) ?? 0)) 条用量记录", "API 参考费用：\(CueUsageMoney.display(summary, currency: currency))"]
            let unpriced = Self.number(summary["unpriced_requests"]) ?? 0
            if unpriced > 0 { lines.append("* 含 \(CueUsageDashboardController.integer(unpriced)) 条未定价记录，费用仅为已定价小计。") }
            let fx = money["fx"] as? [String: Any] ?? [:]
            if currency != "USD", let rate = Self.number(fx["rate"]) {
                lines.append("1 USD = \(rate) CNY · \(fx["date"] as? String ?? "")" + (fx["stale"] as? Bool == true ? " · 缓存汇率可能已过期" : ""))
            }
            let sync = raw["sync"] as? [String: Any] ?? [:]
            if let timestamp = Self.number(sync["last_synced_at"]) {
                let formatter = DateFormatter(); formatter.dateFormat = "MM-dd HH:mm:ss"
                lines.append("账本同步于 \(formatter.string(from: Date(timeIntervalSince1970: timestamp)))" + (sync["running"] as? Bool == true ? " · 正在同步" : ""))
            }
            if !(sync["errors"] as? [String] ?? []).isEmpty || !(sync["warnings"] as? [String] ?? []).isEmpty { lines.append("部分来源存在读取或覆盖提示，请在用量统计查看数据说明。") }
        } else { lines.append(raw["message"] as? String ?? "今日用量正在读取，尚未确认的数字显示为 —。") }
        if !error.isEmpty { lines.append("最近查询未完成：\(error)；显示上次已确认的本日数据。") }
        lines.append("与用量统计共用本地账本；" + schedule.summary + "费用为 API 估算。")
        return lines.joined(separator: "\n")
    }
}

/// Only queries the existing numeric ledger. No scan or network request is
/// introduced by this timer; completed scans and preference saves refresh early.
final class CueMenuBarUsageMonitor {
    private let client: CueUsageClient
    private let home: URL
    private var schedule = CueUsageSchedulePreferences()
    private var started = false
    private var timer: Timer?
    private var observers: [NSObjectProtocol] = []
    private var wakeObserver: NSObjectProtocol?
    private var inFlight = false, pending = false, stopped = false
    private(set) var snapshot = CueMenuBarUsage()
    var onChange: (() -> Void)?
    init(home: URL) { self.home = home; client = CueUsageClient(home: home); schedule = CueUsageSchedulePreferences(home: home) }
    func start() {
        guard !started, !stopped else { return }; started = true
        configureTimer()
        observers.append(NotificationCenter.default.addObserver(forName: .cueUsageScheduleDidChange, object: nil, queue: .main) { [weak self] note in
            guard let self, note.userInfo?["home"] as? String == self.home.path else { return }
            self.configureTimer(); self.refresh()
        })
        for name in [Notification.Name.NSCalendarDayChanged, .NSSystemTimeZoneDidChange] {
            observers.append(NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in self?.refresh() })
        }
        wakeObserver = NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in self?.refresh() }
        refresh()
    }
    private func configureTimer() {
        timer?.invalidate(); timer = nil
        schedule = CueUsageSchedulePreferences(home: home)
        snapshot = CueMenuBarUsage(raw: snapshot.raw, receivedAt: snapshot.receivedAt, error: snapshot.error, schedule: schedule)
        guard started, !stopped else { return }
        let timer = Timer(timeInterval: schedule.displayInterval, repeats: true) { [weak self] _ in self?.refresh() }
        RunLoop.main.add(timer, forMode: .common); self.timer = timer
    }
    func refresh() {
        guard !stopped else { return }
        onChange?() // Immediately discard a prior day's visible totals.
        guard !inFlight else { pending = true; return }
        inFlight = true
        client.run(["today-summary"]) { [weak self] result in
            guard let self, !self.stopped else { return }; self.inFlight = false
            switch result {
            case .success(let data): self.snapshot = CueMenuBarUsage(raw: data, schedule: self.schedule)
            case .failure(let error): self.snapshot = CueMenuBarUsage(raw: self.snapshot.raw, receivedAt: self.snapshot.receivedAt, error: error.localizedDescription, schedule: self.schedule)
            }
            self.onChange?()
            if self.pending { self.pending = false; self.refresh() }
        }
    }
    func stop() {
        stopped = true; timer?.invalidate(); timer = nil; client.stop()
        observers.forEach { NotificationCenter.default.removeObserver($0) }; observers = []
        if let wakeObserver { NSWorkspace.shared.notificationCenter.removeObserver(wakeObserver) }; wakeObserver = nil
    }
    deinit { stop() }

    static func validationReport() -> [String: Bool] {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent("vibecue-menu-schedule-" + UUID().uuidString)
        try? FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: home) }
        let monitor = CueMenuBarUsageMonitor(home: home)
        defer { monitor.stop() }
        let prefs = CueUsageSchedulePreferences(autoSync: false, syncInterval: 1200, displayInterval: 17)
        let data = try! JSONSerialization.data(withJSONObject: prefs.dictionary)
        try? data.write(to: home.appendingPathComponent("usage-schedule.json"), options: .atomic)
        monitor.started = true; monitor.configureTimer()
        let prior = monitor.timer
        let custom = prior?.timeInterval == 17 && monitor.snapshot.schedule.autoSync == false
        let changed = CueUsageSchedulePreferences(autoSync: false, syncInterval: 1200, displayInterval: 23)
        try? JSONSerialization.data(withJSONObject: changed.dictionary).write(to: home.appendingPathComponent("usage-schedule.json"), options: .atomic)
        monitor.configureTimer()
        return ["menu_usage_display_timer_uses_custom_frequency_in_manual_mode": custom,
                "menu_usage_frequency_change_replaces_old_timer": prior?.isValid == false && monitor.timer?.timeInterval == 23]
    }
}

extension CueMenuBarUsage {
    static func validationReport() -> [String: Bool] {
        let now = Date(), start = Calendar.current.startOfDay(for: now)
        let end = Calendar.current.date(byAdding: .day, value: 1, to: start)!
        let active = CueRunningTask(raw: ["thread_id": "active", "started_at": now.timeIntervalSince1970, "last_activity": now.timeIntervalSince1970])!
        let recovery = CueRunningTask(raw: ["thread_id": "recover", "status": "interrupted", "started_at": now.timeIntervalSince1970, "last_activity": now.timeIntervalSince1970])!
        let unread = ["active", "pending", "pending"].map { CueRecentRecord(event: CueEvent(runningThreadID: $0, title: $0, date: now)) }
        let work = CueMenuBarWork(records: unread, running: [active, active], interrupted: [recovery, recovery], followups: [])
        let raw: [String: Any] = ["available": true, "date": day(now), "day_start": start.timeIntervalSince1970, "day_end": end.timeIntervalSince1970, "summary": ["total_tokens": 1_234_567, "requests": 10, "cost_usd": 1.25, "cost_cny": 8.75, "unpriced_requests": 2], "money": ["prefs": ["currency": "CNY"]]]
        let snapshot = CueMenuBarUsage(raw: raw)
        var usd = raw; usd["money"] = ["prefs": ["currency": "USD"]]
        var both = raw; both["money"] = ["prefs": ["currency": "both"]]
        var invalid = raw; invalid["summary"] = ["total_tokens": true]
        var unavailable = raw; unavailable["available"] = false
        var shifted = raw; shifted["day_start"] = start.addingTimeInterval(-3600).timeIntervalSince1970
        var zero = raw; zero["summary"] = ["total_tokens": 0, "cost_usd": 0, "cost_cny": 0]
        var large = raw; large["summary"] = ["total_tokens": 1_234_567, "cost_usd": 55_000, "cost_cny": 390_700]
        var report = ["menu_counts_deduplicate_and_exclude_active_pending": work.running == 1 && work.pending == 2 && work.unread == 2 && work.interrupted == 1,
                "menu_today_tokens_compact": snapshot.title(work: work, now: now).contains("1.2M T"),
                "menu_follows_cny_currency_with_partial_marker": snapshot.cost(now: now) == "≈8.75 ¥*",
                "menu_follows_usd_currency": CueMenuBarUsage(raw: usd).cost(now: now) == "≈$1.25*",
                "menu_follows_dual_currency": CueMenuBarUsage(raw: both).cost(now: now) == "≈8.75 ¥ / $1.25*",
                "menu_large_cost_keeps_yuan_without_compact_notation": CueMenuBarUsage(raw: large).cost(now: now) == "≈390,700.00 ¥",
                "menu_midnight_invalidates_yesterday": !snapshot.isCurrent(now: end),
                "menu_timezone_boundary_change_invalidates": !CueMenuBarUsage(raw: shifted).isCurrent(now: now),
                "menu_unknown_does_not_show_zero": CueMenuBarUsage().title(work: work, now: now).contains("今日 — T · —"),
                "menu_unavailable_does_not_use_old_totals": !CueMenuBarUsage(raw: unavailable).isCurrent(now: now),
                "menu_invalid_token_boolean_rejected": !CueMenuBarUsage(raw: invalid).isCurrent(now: now),
                "menu_confirmed_zero_stays_zero": CueMenuBarUsage(raw: zero).title(work: work, now: now).contains("今日 0 T"),
                "menu_tooltip_has_exact_tokens_and_estimation_scope": snapshot.tooltip(work: work, health: "测试", now: now).contains("1,234,567") && snapshot.tooltip(work: work, health: "测试", now: now).contains("API 参考费用")]
        report.merge(CueMenuBarUsageMonitor.validationReport()) { _, new in new }
        return report
    }
}
