import AppKit
import Foundation

/// Numeric snapshots only; Codex and CC Switch retain ownership of login.
final class CueAccountQuotaMonitor {
    private static var monitors: [String: CueAccountQuotaMonitor] = [:]
    static func shared(home: URL) -> CueAccountQuotaMonitor {
        let key = home.standardizedFileURL.path
        if let value = monitors[key] { return value }
        let value = CueAccountQuotaMonitor(home: home); monitors[key] = value; return value
    }
    private let client: CueUsageClient
    private var timer: Timer?
    private var observers: [UUID: () -> Void] = [:]
    private(set) var snapshot: [String: Any] = [:]
    private(set) var refreshing = false
    private var stopped = false
    private init(home: URL) { client = CueUsageClient(home: home) }
    func observe(_ callback: @escaping () -> Void) -> UUID { let id = UUID(); observers[id] = callback; return id }
    func removeObserver(_ id: UUID) { observers.removeValue(forKey: id) }
    private func changed() { Array(observers.values).forEach { $0() } }
    func start() {
        guard timer == nil, !stopped else { return }
        client.run(["status"], module: "vibecue.account_quota") { [weak self] result in
            guard let self, !self.stopped else { return }
            if case .success(let data) = result { self.snapshot = data; self.changed() }
            self.refresh()
        }
        let timer = Timer(timeInterval: 60, repeats: true) { [weak self] _ in self?.refresh() }
        RunLoop.main.add(timer, forMode: .common); self.timer = timer
    }
    func refresh(force: Bool = false) {
        guard !refreshing, !stopped else { return }
        refreshing = true; changed()
        client.run(force ? ["refresh", "--force"] : ["refresh"], module: "vibecue.account_quota") { [weak self] result in
            guard let self, !self.stopped else { return }
            self.refreshing = false
            switch result {
            case .success(let data): self.snapshot = data
            case .failure:
                self.snapshot["message"] = "当前额度读取失败，已保存的数据仅供查看；稍后会自动重试。"
                self.snapshot["available"] = false
                if let rows = self.snapshot["accounts"] as? [[String: Any]] {
                    self.snapshot["accounts"] = rows.map { row in var value = row; value["current_account"] = false; value["is_live"] = false; value["status"] = "last_read"; return value }
                }
            }
            self.changed()
        }
    }
    func stop() { stopped = true; timer?.invalidate(); timer = nil; client.stop() }
}

private final class CueQuotaSurface: NSView {
    var onLayout: (() -> Void)?
    var fillColor: NSColor?
    var radius: CGFloat = 0
    override var isFlipped: Bool { true }
    override func layout() { super.layout(); onLayout?() }
    override func draw(_ dirtyRect: NSRect) {
        if let fillColor { fillColor.setFill(); NSBezierPath(roundedRect: bounds, xRadius: radius, yRadius: radius).fill() }
    }
}

final class CueCCAccountsController: NSWindowController {
    private let monitor: CueAccountQuotaMonitor
    private var observer: UUID?
    private let root = CueQuotaSurface()
    private var isEmbedded = false
    private var presentationWindow: NSWindow? { root.window ?? (isEmbedded ? nil : window) }
    private let titleField = NSTextField(labelWithString: "Codex 账号额度")
    private let subtitle = NSTextField(labelWithString: "每 60 秒自动刷新 · 支持 CC Switch 多账号")
    private let message = NSTextField(wrappingLabelWithString: "正在读取账号的订阅额度…")
    private let status = NSTextField(wrappingLabelWithString: "各账号独立显示读取状态和时间。")
    private let scroll = NSScrollView()
    private let quotaDocument = CueQuotaSurface()
    private let refreshButton = NSButton(title: "刷新", target: nil, action: nil)
    private let openButton = NSButton(title: "打开 CC Switch", target: nil, action: nil)
    private let infoButton = NSButton(title: "", target: nil, action: nil)
    private let fixture: [String: Any]?
    init(home: URL, fixture: [String: Any]? = nil) {
        monitor = .shared(home: home); self.fixture = fixture
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 740, height: 640), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "VibeCoding Go · Codex 账号额度"; window.isReleasedWhenClosed = false; window.contentMinSize = NSSize(width: 640, height: 420); window.center()
        super.init(window: window); window.contentView = root
        root.fillColor = .windowBackgroundColor
        titleField.font = .systemFont(ofSize: 19, weight: .semibold)
        subtitle.font = .systemFont(ofSize: 11); subtitle.textColor = .secondaryLabelColor
        message.font = .systemFont(ofSize: 11); message.textColor = .secondaryLabelColor; message.maximumNumberOfLines = 2
        status.font = .systemFont(ofSize: 10.5); status.textColor = .secondaryLabelColor; status.maximumNumberOfLines = 2
        refreshButton.target = self; refreshButton.action = #selector(refresh); refreshButton.bezelStyle = .rounded
        openButton.target = self; openButton.action = #selector(openCCSwitch); openButton.bezelStyle = .rounded
        infoButton.target = self; infoButton.action = #selector(showInfo); infoButton.bezelStyle = .helpButton; infoButton.setAccessibilityLabel("账号额度来源说明")
        scroll.documentView = quotaDocument; scroll.hasVerticalScroller = true; scroll.autohidesScrollers = true; scroll.borderType = .noBorder; scroll.drawsBackground = false
        [titleField, subtitle, message, scroll, status, refreshButton, openButton, infoButton].forEach { root.addSubview($0) }
        root.onLayout = { [weak self] in self?.layout(); self?.renderAccounts() }
        observer = monitor.observe { [weak self] in self?.update() }
        layout(); update()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    deinit { if let observer { monitor.removeObserver(observer) } }
    func embeddedContentView() -> NSView {
        if !isEmbedded { isEmbedded = true; window?.orderOut(nil); window?.contentView = nil }
        return root
    }
    func activateEmbedded() { if fixture == nil { monitor.start() }; update() }
    func deactivateEmbedded() { /* The shared quota monitor stays active for all tags. */ }
    override func showWindow(_ sender: Any?) {
        if isEmbedded { activateEmbedded(); presentationWindow?.makeKeyAndOrderFront(nil); return }
        super.showWindow(sender); window?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        activateEmbedded()
    }
    private var data: [String: Any] { fixture ?? monitor.snapshot }
    private func layout() {
        let width = root.bounds.width, height = root.bounds.height
        titleField.frame = NSRect(x: 24, y: 21, width: width - 174, height: 28)
        subtitle.frame = NSRect(x: 25, y: 55, width: width - 50, height: 18)
        refreshButton.frame = NSRect(x: width - 120, y: 24, width: 96, height: 30)
        message.frame = NSRect(x: 25, y: 83, width: width - 50, height: 38)
        scroll.frame = NSRect(x: 20, y: 131, width: width - 40, height: max(180, height - 212))
        openButton.frame = NSRect(x: width - 182, y: height - 52, width: 158, height: 30)
        infoButton.frame = NSRect(x: width - 219, y: height - 49, width: 26, height: 26)
        status.frame = NSRect(x: 25, y: height - 57, width: max(250, width - 264), height: 43)
    }
    private func update() {
        refreshButton.isEnabled = fixture == nil && !monitor.refreshing
        refreshButton.title = monitor.refreshing && fixture == nil ? "读取中…" : "刷新"
        message.stringValue = data["message"] as? String ?? "正在读取账号的订阅额度…"
        let maintenance = data["ccswitch_maintenance"] as? [String: Any] ?? [:]
        if maintenance["enabled"] as? Bool == true, maintenance["recovering"] as? Bool == true {
            message.stringValue = maintenance["message"] as? String ?? "正在自动恢复额度共享，上次额度已保留。"
        }
        let rows = data["accounts"] as? [[String: Any]] ?? []
        let liveCount = rows.filter { Self.isLive($0) }.count
        status.stringValue = rows.isEmpty ? "复用已有登录，无需在 VibeCoding Go 重新授权。" : "已显示 \(rows.count) 个账号 · \(liveCount) 个实时更新。"
        if maintenance["enabled"] as? Bool == true, maintenance["phase"] as? String == "healthy" { status.stringValue += " 升级自动维护已开启。" }
        renderAccounts()
    }
    @discardableResult private func label(_ value: String, x: CGFloat, y: CGFloat, width: CGFloat, size: CGFloat = 11, weight: NSFont.Weight = .regular, color: NSColor = .labelColor, in parent: NSView) -> NSTextField {
        let field = NSTextField(labelWithString: value); field.font = .systemFont(ofSize: size, weight: weight); field.textColor = color
        field.frame = NSRect(x: x, y: y, width: max(10, width), height: size + 8); field.lineBreakMode = .byTruncatingTail; field.toolTip = value; field.isSelectable = true; parent.addSubview(field); return field
    }
    private func renderAccounts() {
        let origin = scroll.contentView.bounds.origin
        quotaDocument.subviews.forEach { $0.removeFromSuperview() }
        let width = max(560, scroll.contentSize.width)
        var y: CGFloat = 0
        let rows = data["accounts"] as? [[String: Any]] ?? []
        if rows.isEmpty {
            label("尚未取得可归属到账号的订阅额度", x: 16, y: 24, width: width - 32, size: 13, weight: .medium, in: quotaDocument)
            label("请先在 Codex 中确认已登录 ChatGPT 账号，再点击刷新。", x: 16, y: 56, width: width - 32, color: .secondaryLabelColor, in: quotaDocument)
            y = 120
        }
        for row in rows.prefix(20) {
            let current = row["current_account"] as? Bool == true
            let live = Self.isLive(row), shared = row["source"] as? String == "ccswitch_quota_bridge"
            let card = CueQuotaSurface(); card.radius = 12
            card.fillColor = live ? NSColor.controlAccentColor.withAlphaComponent(0.07) : NSColor.secondaryLabelColor.withAlphaComponent(0.045)
            label(row["label"] as? String ?? "Codex 账号", x: 16, y: 13, width: width - 220, size: 14, weight: .semibold, in: card)
            let badge = live ? (shared ? "CC Switch · 实时读取" : "当前登录 · 实时读取") : row["status"] as? String == "unavailable" ? "暂不可用" : "上次读取 · 非实时"
            label(badge, x: width - 203, y: 14, width: 184, color: live ? .systemGreen : .secondaryLabelColor, in: card).alignment = .right
            let plan = row["plan_type"] as? String ?? ""
            label((plan.isEmpty ? "Codex" : plan.capitalized) + (current && shared ? " · 当前 Codex 登录" : "") + " · 更新于 " + Self.date(row["observed_at"]), x: 16, y: 39, width: width - 32, size: 10.5, color: .secondaryLabelColor, in: card)
            var inside: CGFloat = 70
            if let error = row["read_error"] as? String, !error.isEmpty {
                label(error, x: 16, y: inside, width: width - 32, color: .systemOrange, in: card); inside += 28
            }
            let buckets = row["buckets"] as? [[String: Any]] ?? []
            for bucket in buckets.prefix(32) {
                let bucketName = bucket["limit_name"] as? String ?? ""
                let name = bucketName.isEmpty ? (bucket["limit_id"] as? String ?? "Codex") : bucketName
                label(name == "codex" ? "Codex" : name, x: 16, y: inside, width: width - 32, size: 11, weight: .medium, in: card)
                inside += 25
                let windows = bucket["windows"] as? [[String: Any]] ?? []
                if !windows.contains(where: { $0["kind"] as? String == "weekly" }) {
                    label("本周额度未提供", x: 16, y: inside, width: width - 32, color: .secondaryLabelColor, in: card); inside += 26
                }
                for quota in windows {
                    guard let remaining = quota["remaining_percent"] as? Double, remaining.isFinite else { continue }
                    let weekly = quota["kind"] as? String == "weekly", minutes = quota["window_minutes"] as? Int ?? 0
                    let scope = weekly ? "本周" : minutes > 0 ? (minutes < 60 ? "\(minutes)分钟窗口" : "\(minutes / 60)小时窗口") : "额度窗口"
                    label("\(scope)\(live ? "剩余" : "上次剩余") \(Self.percent(remaining))", x: 16, y: inside, width: width / 2 - 22, size: 14, weight: weekly ? .semibold : .medium, in: card)
                    let reset: String
                    if quota["reset_status"] as? String == "not_started" { reset = "尚未开始计时" }
                    else if quota["reset_status"] as? String == "elapsed" { reset = "等待重置结果更新" }
                    else if quota["resets_at"] is NSNumber { reset = "重置 " + Self.date(quota["resets_at"]) }
                    else { reset = "重置时间未提供" }
                    label(reset, x: width / 2, y: inside + 2, width: width / 2 - 18, size: 10.5, color: .secondaryLabelColor, in: card).alignment = .right
                    let bar = NSProgressIndicator(frame: NSRect(x: 16, y: inside + 28, width: width - 32, height: 6))
                    bar.style = .bar; bar.isIndeterminate = false; bar.minValue = 0; bar.maxValue = 100; bar.doubleValue = min(100, max(0, remaining)); bar.setAccessibilityLabel("\(scope)剩余额度 \(Self.percent(remaining))")
                    card.addSubview(bar); inside += 52
                }
                inside += 5
            }
            card.frame = NSRect(x: 0, y: y, width: width, height: max(117, inside + 10)); quotaDocument.addSubview(card); y += card.frame.height + 12
        }
        quotaDocument.frame = NSRect(x: 0, y: 0, width: width, height: max(scroll.contentSize.height, y))
        scroll.contentView.scroll(to: NSPoint(x: 0, y: min(origin.y, max(0, y - scroll.contentSize.height)))); scroll.reflectScrolledClipView(scroll.contentView)
    }
    private static func isLive(_ row: [String: Any]) -> Bool {
        let markedLive = row["is_live"] as? Bool ?? (row["current_account"] as? Bool == true && row["status"] as? String == "current")
        return markedLive && row["status"] as? String == "current" && (row["expires_at"] as? Double ?? 0) >= Date().timeIntervalSince1970
    }
    private static func percent(_ value: Double) -> String { String(format: value.rounded() == value ? "%.0f%%" : "%.1f%%", value) }
    private static func date(_ raw: Any?) -> String {
        guard let seconds = raw as? Double, seconds.isFinite, seconds > 0 else { return "—" }
        let formatter = DateFormatter(); formatter.locale = Locale(identifier: "zh_CN"); formatter.dateFormat = "MM-dd HH:mm"
        return formatter.string(from: Date(timeIntervalSince1970: seconds))
    }
    @objc private func refresh() { monitor.refresh(force: true) }
    @objc private func showInfo() {
        let alert = NSAlert(); alert.messageText = "订阅额度的来源"
        alert.informativeText = "当前登录账号由 Codex 官方接口读取。CC Switch 账号通过本机额度共享读取，授权和刷新始终由 CC Switch 自己管理，只共享账号名称、额度数字和更新时间。\n\nCC Switch 升级后自动检测变化，核对新版接口、构建并验证共享模块，再等待后台空闲时恢复；不会降级或强制结束应用。若新版结构暂不兼容，会保留官方新版及上次额度，后台稍后重试。\n\n无需重新授权或切换账号。读取失败和过期值会明确标记。订阅额度与 API 参考费用不能互相换算。"; alert.addButton(withTitle: "知道了")
        if let window = presentationWindow { alert.beginSheetModal(for: window) }
    }
    @objc private func openCCSwitch() {
        guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.ccswitch.desktop") else { message.stringValue = "未找到 CC Switch，请从应用程序中打开。"; return }
        NSWorkspace.shared.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration()) { _, _ in }
    }

    private static func fixtureData() -> [String: Any] {
        let now = Date().timeIntervalSince1970
        func account(_ email: String, current: Bool, remaining: Double) -> [String: Any] {
            ["label": email, "plan_type": "pro", "current_account": current, "status": current ? "current" : "last_read", "observed_at": now - (current ? 3 : 3600), "expires_at": now + 120,
             "buckets": [["limit_id": "codex", "limit_name": "Codex", "windows": [["kind": "weekly", "remaining_percent": remaining, "window_minutes": 10080, "resets_at": now + 86400, "reset_status": "scheduled"]]]]]
        }
        var shared = account("account-two@example.com", current: false, remaining: 42)
        shared["source"] = "ccswitch_quota_bridge"; shared["is_live"] = true; shared["status"] = "current"; shared["observed_at"] = now - 4
        return ["available": true, "message": "已读取 2 个账号的当前额度，无需切换账号。", "accounts": [account("account-one@example.com", current: true, remaining: 78), shared]]
    }
    static func validationReport() -> [String: Bool] {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent("vibecue-quota-ui-" + UUID().uuidString)
        let controller = CueCCAccountsController(home: home, fixture: fixtureData())
        let labels = controller.quotaDocument.subviews.flatMap(\.subviews).compactMap { ($0 as? NSTextField)?.stringValue }
        var historical = fixtureData()["accounts"] as! [[String: Any]]
        historical[1]["is_live"] = true; historical[1]["status"] = "last_read"
        let older = CueCCAccountsController(home: home, fixture: ["accounts": historical])
        let oldLabels = older.quotaDocument.subviews.flatMap(\.subviews).compactMap { ($0 as? NSTextField)?.stringValue }
        var repairingData = fixtureData()
        repairingData["ccswitch_maintenance"] = ["enabled": true, "recovering": true, "phase": "building", "message": "正在自动恢复新版额度共享，上次额度已保留。"]
        let repairing = CueCCAccountsController(home: home, fixture: repairingData)
        let host = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 740, height: 640), styleMask: [.titled], backing: .buffered, defer: false)
        let view = controller.embeddedContentView(); host.contentView = view
        controller.activateEmbedded(); controller.deactivateEmbedded()
        let sharedCanvas = view === controller.embeddedContentView() && controller.presentationWindow === host && controller.window?.contentView !== view
        let monitorShared = controller.monitor === older.monitor && !controller.monitor.refreshing
        host.contentView = nil
        return ["quota_embedded_page_retains_canvas_and_uses_main_host": sharedCanvas,
                "quota_embedded_page_reuses_existing_monitor": monitorShared,
"quota_ui_distinguishes_live_and_previous_account": labels.contains("CC Switch · 实时读取") && oldLabels.contains("上次读取 · 非实时") && oldLabels.contains("本周上次剩余 42%"),
                "quota_ui_explains_automatic_upgrade_recovery": repairing.message.stringValue.contains("自动恢复") && repairing.message.stringValue.contains("上次额度"),
                "quota_ui_shows_two_live_accounts_without_switching": labels.contains("当前登录 · 实时读取") && labels.contains("CC Switch · 实时读取") && controller.status.stringValue.contains("2 个实时更新"),
                "quota_ui_failed_read_clears_the_live_badge": !Self.isLive(historical[1]) && !oldLabels.contains("CC Switch · 实时读取"),
                "quota_ui_shows_week_remaining_and_local_reset_time": labels.contains("本周剩余 78%") && labels.contains("本周剩余 42%") && labels.contains(where: { $0.hasPrefix("重置 ") }),
                "quota_ui_fixture_never_starts_official_helper": controller.monitor.snapshot.isEmpty && !controller.monitor.refreshing,
                "quota_ui_retains_ccswitch_management_entry": controller.openButton.title == "打开 CC Switch"]
    }
    static func captureFixture(to url: URL, dark: Bool) -> Bool {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent("vibecue-quota-capture-" + UUID().uuidString)
        let controller = CueCCAccountsController(home: home, fixture: fixtureData())
        controller.window?.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        controller.window?.setFrameOrigin(NSPoint(x: -10000, y: -10000)); controller.window?.orderFrontRegardless()
        defer { controller.window?.orderOut(nil) }
        controller.layout(); controller.update(); controller.root.layoutSubtreeIfNeeded()
        guard let bitmap = controller.root.bitmapImageRepForCachingDisplay(in: controller.root.bounds) else { return false }
        controller.root.effectiveAppearance.performAsCurrentDrawingAppearance { controller.root.cacheDisplay(in: controller.root.bounds, to: bitmap) }
        guard let png = bitmap.representation(using: .png, properties: [:]) else { return false }
        do { try png.write(to: url); return true } catch { return false }
    }
}
