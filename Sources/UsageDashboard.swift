import AppKit
import Foundation

/// Local numeric usage only. The collector runs off the AppKit thread and never
/// receives a shell command, a prompt, or an authentication credential.
final class CueUsageClient {
    let home: URL
    private let lock = NSLock()
    private var active: [UUID: Process] = [:]
    private var stopped = false
    init(home: URL) { self.home = home }

    func stop() {
        lock.lock(); stopped = true; let processes = Array(active.values); lock.unlock()
        for process in processes where process.isRunning { process.terminate() }
    }

    func run(_ arguments: [String], module: String = "vibecue.usage", input: Data? = nil, completion: @escaping (Result<[String: Any], Error>) -> Void) {
        let home = self.home
        DispatchQueue.global(qos: .utility).async {
            let id = UUID()
            defer { self.lock.lock(); self.active.removeValue(forKey: id); self.lock.unlock() }
            do {
                let runtime = try Self.runtime(home: home)
                let process = Process(), output = Pipe(), errors = Pipe()
                process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
                process.arguments = ["-m", module] + arguments
                process.currentDirectoryURL = runtime
                let inherited = ProcessInfo.processInfo.environment
                // Keep path/locale settings needed to find logs, but do not
                // propagate unrelated API keys or authorization variables.
                var environment: [String: String] = [:]
                for key in ["HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "OPENCODE_DB"] {
                    if let value = inherited[key] { environment[key] = value }
                }
                environment["VIBECUE_HOME"] = home.path
                environment["PYTHONPATH"] = runtime.path
                environment["PYTHONUNBUFFERED"] = "1"
                environment["PYTHONDONTWRITEBYTECODE"] = "1"
                environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
                process.environment = environment
                process.standardOutput = output; process.standardError = errors
                let inputPipe = Pipe()
                process.standardInput = input == nil ? FileHandle.nullDevice : inputPipe
                self.lock.lock()
                if self.stopped { self.lock.unlock(); throw Self.error("用量采集已停止。") }
                do { try process.run(); self.active[id] = process; self.lock.unlock() }
                catch { self.lock.unlock(); throw error }
                if let input { inputPipe.fileHandleForWriting.write(input); try? inputPipe.fileHandleForWriting.close() }
                // Drain stderr concurrently: an unread error pipe must never
                // stall the collector while stdout is being read.
                let group = DispatchGroup(), stderr = CueUsageByteBuffer(limit: 16_384)
                group.enter()
                DispatchQueue.global(qos: .utility).async {
                    while true {
                        let chunk = errors.fileHandleForReading.readData(ofLength: 4096)
                        if chunk.isEmpty { break }; stderr.append(chunk)
                    }
                    group.leave()
                }
                let timeout = DispatchWorkItem { if process.isRunning { process.terminate() } }
                DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + (arguments.first == "sync" ? 1800 : 60), execute: timeout)
                var bytes = Data(), exceeded = false
                while true {
                    let chunk = output.fileHandleForReading.readData(ofLength: 65_536)
                    if chunk.isEmpty { break }
                    if bytes.count + chunk.count <= 8_388_608 { bytes.append(chunk) }
                    else { exceeded = true; if process.isRunning { process.terminate() } }
                }
                process.waitUntilExit(); timeout.cancel(); group.wait()
                guard !exceeded else { throw Self.error("用量查询返回的数据过大，请缩小筛选范围。") }
                guard process.terminationStatus == 0 else {
                    // The backend emits controlled diagnostics, never log bodies.
                    let message = String(data: stderr.data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                    throw Self.error(message.isEmpty ? "用量模块未完成（退出代码 \(process.terminationStatus)）。" : String(message.prefix(600)))
                }
                guard let object = try JSONSerialization.jsonObject(with: bytes) as? [String: Any] else { throw Self.error("用量模块返回了无法识别的数据。") }
                DispatchQueue.main.async { completion(.success(object)) }
            } catch { DispatchQueue.main.async { completion(.failure(error)) } }
        }
    }

    private static func runtime(home: URL) throws -> URL {
        var candidates = [home.appendingPathComponent("runtime", isDirectory: true)]
        if let data = try? Data(contentsOf: home.appendingPathComponent("install.json")),
           let info = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let source = info["source_dir"] as? String {
            candidates.append(URL(fileURLWithPath: source, isDirectory: true))
        }
        candidates.append(Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent())
        candidates.append(URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true))
        guard let found = candidates.first(where: { FileManager.default.fileExists(atPath: $0.appendingPathComponent("vibecue/usage.py").path) }) else {
            throw error("本机用量模块尚未安装，请重新安装最新版 VibeCue。")
        }
        return found
    }
    private static func error(_ text: String) -> NSError { NSError(domain: "VibeCue.Usage", code: 1, userInfo: [NSLocalizedDescriptionKey: text]) }
}

private final class CueUsageByteBuffer {
    let limit: Int
    private var bytes = Data()
    private let lock = NSLock()
    init(limit: Int) { self.limit = limit }
    func append(_ data: Data) { lock.lock(); defer { lock.unlock() }; bytes.append(data.prefix(max(0, limit - bytes.count))) }
    var data: Data { lock.lock(); defer { lock.unlock() }; return bytes }
}

private final class CueUsageSurface: NSView {
    var onLayout: (() -> Void)?
    var onAppearance: (() -> Void)?
    override var isFlipped: Bool { true }
    override func layout() { super.layout(); onLayout?() }
    override func draw(_ dirtyRect: NSRect) { NSColor.windowBackgroundColor.setFill(); dirtyRect.fill() }
    override func viewDidChangeEffectiveAppearance() { super.viewDidChangeEffectiveAppearance(); needsDisplay = true; onAppearance?() }
}

private final class CueUsageMetric: NSView {
    let title = NSTextField(labelWithString: ""), value = NSTextField(labelWithString: "—"), detail = NSTextField(labelWithString: "")
    override var isFlipped: Bool { true }
    init(title: String) {
        super.init(frame: .zero)
        self.title.stringValue = title
        self.title.font = .systemFont(ofSize: 11, weight: .medium); self.title.textColor = .secondaryLabelColor
        value.font = .monospacedDigitSystemFont(ofSize: 25, weight: .semibold)
        detail.font = .systemFont(ofSize: 10); detail.textColor = .secondaryLabelColor
        detail.lineBreakMode = .byTruncatingTail
        [self.title, value, detail].forEach { addSubview($0) }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func layout() {
        super.layout(); title.frame = NSRect(x: 16, y: 12, width: bounds.width - 28, height: 17)
        value.frame = NSRect(x: 16, y: 34, width: bounds.width - 28, height: 33)
        detail.frame = NSRect(x: 16, y: 73, width: bounds.width - 28, height: 16)
    }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.controlBackgroundColor.setFill(); NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: 10, yRadius: 10).fill()
        NSColor.separatorColor.withAlphaComponent(0.45).setStroke(); NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: 10, yRadius: 10).stroke()
    }
}

private final class CueUsageHeatmap: NSView {
    var days: [[String: Any]] = [] { didSet { needsDisplay = true } }
    private var tooltips: [Int: String] = [:]
    override var isFlipped: Bool { true }
    override func draw(_ dirtyRect: NSRect) {
        removeAllToolTips(); tooltips = [:]
        let calendar = Calendar.current, formatter = DateFormatter(); formatter.dateFormat = "yyyy-MM-dd"
        let today = calendar.startOfDay(for: Date())
        // Exactly 53 calendar-week columns, including the current partial week.
        let weekday = (calendar.component(.weekday, from: today) + 5) % 7
        guard let monday = calendar.date(byAdding: .day, value: -weekday, to: today),
              let start = calendar.date(byAdding: .day, value: -52 * 7, to: monday) else { return }
        let values = Dictionary(days.compactMap { row -> (String, [String: Any])? in guard let date = row["date"] as? String else { return nil }; return (date, row) }, uniquingKeysWith: { _, new in new })
        let maximum = max(1, days.map { ($0["tokens"] as? NSNumber)?.doubleValue ?? 0 }.max() ?? 1)
        let weeks = 53
        let gap: CGFloat = 3, cell = min(15, max(9, (bounds.width - 45) / CGFloat(weeks) - gap))
        let rowLabels = [(0, "一"), (2, "三"), (4, "五"), (6, "日")]
        for (row, text) in rowLabels {
            text.draw(at: NSPoint(x: 1, y: 23 + CGFloat(row) * (cell + gap)), withAttributes: [.font: NSFont.systemFont(ofSize: 9), .foregroundColor: NSColor.tertiaryLabelColor])
        }
        var priorMonth = -1
        for week in 0..<weeks {
            for day in 0..<7 {
                let index = week * 7 + day
                guard let date = calendar.date(byAdding: .day, value: index, to: start), date <= today else { continue }
                let key = formatter.string(from: date), row = values[key] ?? [:]
                let count = (row["tokens"] as? NSNumber)?.doubleValue ?? 0
                let requests = (row["requests"] as? NSNumber)?.int64Value ?? 0
                let level = count <= 0 ? 0 : min(1, max(0.15, sqrt(count / maximum)))
                let rect = NSRect(x: 26 + CGFloat(week) * (cell + gap), y: 23 + CGFloat(day) * (cell + gap), width: cell, height: cell)
                let color = count <= 0 ? NSColor.quaternaryLabelColor.withAlphaComponent(0.10) : NSColor.controlAccentColor.withAlphaComponent(CGFloat(0.20 + level * 0.78))
                color.setFill(); NSBezierPath(roundedRect: rect, xRadius: 2, yRadius: 2).fill()
                let tag = addToolTip(rect, owner: self, userData: nil)
                tooltips[tag] = "\(key) · \(CueUsageDashboardController.integer(count)) Tokens · \(requests) 条用量记录"
                if day == 0 {
                    let month = calendar.component(.month, from: date)
                    if month != priorMonth {
                        "\(month)月".draw(at: NSPoint(x: rect.minX, y: 2), withAttributes: [.font: NSFont.systemFont(ofSize: 9), .foregroundColor: NSColor.secondaryLabelColor]); priorMonth = month
                    }
                }
            }
        }
    }
    @objc func view(_ view: NSView, stringForToolTip tag: NSView.ToolTipTag, point: NSPoint, userData data: UnsafeMutableRawPointer?) -> String { tooltips[tag] ?? "" }
}

final class CueUsageDashboardController: NSWindowController, NSWindowDelegate, NSTableViewDataSource, NSTableViewDelegate {
    let home: URL
    var usageDidChange: (() -> Void)?
    private let client: CueUsageClient
    private let surface = CueUsageSurface()
    private let titleLabel = NSTextField(labelWithString: "用量统计")
    private let subtitle = NSTextField(labelWithString: "本机历史记录 · Codex、Claude Code 与 OpenCode")
    private let range = NSPopUpButton(), app = NSPopUpButton(), model = NSPopUpButton(), source = NSPopUpButton()
    private let syncButton = NSButton(title: "立即同步", target: nil, action: nil)
    private let infoButton = NSButton(title: "数据说明", target: nil, action: nil)
    private let moneyButton = NSButton(title: "费用与汇率…", target: nil, action: nil)
    private let accountsButton = NSButton(title: "CC Switch 额度…", target: nil, action: nil)
    private let syncLabel = NSTextField(labelWithString: "尚未同步")
    private let status = NSTextField(wrappingLabelWithString: "")
    private let metrics = [CueUsageMetric(title: "Tokens 总量"), CueUsageMetric(title: "新增输入"), CueUsageMetric(title: "输出"), CueUsageMetric(title: "缓存命中"), CueUsageMetric(title: "用量记录"), CueUsageMetric(title: "API 参考费用")]
    private let heatmapTitle = NSTextField(labelWithString: "最近 53 周")
    private let heatmap = CueUsageHeatmap()
    private let tabs = NSSegmentedControl(labels: ["请求日志", "模型", "来源"], trackingMode: .selectOne, target: nil, action: nil)
    private let table = NSTableView(), scroll = NSScrollView()
    private let pageLabel = NSTextField(labelWithString: ""), detail = NSTextField(labelWithString: "")
    private let previous = NSButton(title: "上一页", target: nil, action: nil), next = NSButton(title: "下一页", target: nil, action: nil)
    private var rows: [[String: Any]] = []
    private var snapshot: [String: Any] = [:]
    private var page = 1, pages = 1
    private var queryID = 0
    private var queryInFlight = false
    private var queryPending = false
    private var syncing = false
    private var backendRunning = false
    private var syncMetadata: [String: Any] = [:]
    private var backgroundTimer: Timer?, visibleTimer: Timer?
    private var lastSync = Date.distantPast
    private var syncError = ""
    private var queryError = ""
    private var warnings: [String] = []
    private var preferencesController: CueUsagePreferencesController?
    private var exchangeRefreshing = false
    private var lastExchangeRefresh = Date.distantPast
    private var accountController: CueCCAccountsController?
    private var currentView: String { ["logs", "models", "sources"][max(0, min(2, tabs.selectedSegment))] }

    init(home: URL) {
        self.home = home; client = CueUsageClient(home: home)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1120, height: 850), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "VibeCue · 用量统计"; window.isReleasedWhenClosed = false
        window.contentMinSize = NSSize(width: 1000, height: 760); window.center()
        super.init(window: window); window.delegate = self; window.contentView = surface
        titleLabel.font = .systemFont(ofSize: 23, weight: .semibold)
        subtitle.font = .systemFont(ofSize: 12); subtitle.textColor = .secondaryLabelColor
        syncLabel.font = .systemFont(ofSize: 11); syncLabel.textColor = .secondaryLabelColor; syncLabel.alignment = .right
        status.font = .systemFont(ofSize: 11); status.maximumNumberOfLines = 2
        heatmapTitle.font = .systemFont(ofSize: 12, weight: .medium); heatmapTitle.textColor = .secondaryLabelColor
        pageLabel.font = .systemFont(ofSize: 11); pageLabel.textColor = .secondaryLabelColor
        detail.font = .systemFont(ofSize: 10.5); detail.textColor = .secondaryLabelColor; detail.lineBreakMode = .byTruncatingTail
        for control in [syncButton, infoButton, moneyButton, accountsButton, previous, next] { control.bezelStyle = .rounded; control.target = self }
        syncButton.action = #selector(syncNow); infoButton.action = #selector(showInfo)
        moneyButton.action = #selector(showMoneyPreferences); accountsButton.action = #selector(showAccounts)
        previous.action = #selector(previousPage); next.action = #selector(nextPage)
        syncButton.identifier = NSUserInterfaceItemIdentifier("usage.sync")
        for (control, key) in [(range, "range"), (app, "app"), (model, "model"), (source, "source")] {
            control.target = self; control.action = #selector(changeFilter(_:)); control.identifier = NSUserInterfaceItemIdentifier("usage.\(key)")
        }
        fill(range, ["today": "今天", "7d": "近 7 天", "30d": "近 30 天", "all": "全部历史"], order: ["today", "7d", "30d", "all"], selected: "all")
        fill(app, ["all": "全部应用"], selected: "all"); fill(model, ["all": "全部模型"], selected: "all"); fill(source, ["all": "全部来源"], selected: "all")
        range.setAccessibilityLabel("统计时间范围"); app.setAccessibilityLabel("应用筛选"); model.setAccessibilityLabel("模型筛选"); source.setAccessibilityLabel("数据来源筛选")
        tabs.selectedSegment = 0; tabs.target = self; tabs.action = #selector(changeTab)
        tabs.identifier = NSUserInterfaceItemIdentifier("usage.tabs")
        table.delegate = self; table.dataSource = self; table.rowHeight = 27
        table.usesAlternatingRowBackgroundColors = true; table.style = .inset
        table.allowsMultipleSelection = false; table.columnAutoresizingStyle = .noColumnAutoresizing
        table.intercellSpacing = NSSize(width: 4, height: 2)
        table.setAccessibilityLabel("历史用量请求日志")
        scroll.documentView = table; scroll.hasVerticalScroller = true; scroll.hasHorizontalScroller = true
        scroll.autohidesScrollers = true; scroll.borderType = .bezelBorder
        let views: [NSView] = [titleLabel, subtitle, range, app, model, source, syncButton, infoButton, moneyButton, accountsButton, syncLabel, status, heatmapTitle, heatmap, tabs, scroll, pageLabel, previous, next, detail] + metrics
        views.forEach { surface.addSubview($0) }
        surface.onLayout = { [weak self] in self?.layout() }
        surface.onAppearance = { [weak self] in self?.metrics.forEach { $0.needsDisplay = true }; self?.heatmap.needsDisplay = true }
        configureColumns(); layout(); updateStatus()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    deinit { backgroundTimer?.invalidate(); visibleTimer?.invalidate() }

    override func showWindow(_ sender: Any?) {
        super.showWindow(sender); window?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        query()
        refreshExchange()
        if Date().timeIntervalSince(lastSync) > 60 { synchronize() }
        visibleTimer?.invalidate()
        visibleTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            guard let self, self.window?.isVisible == true, !self.window!.isMiniaturized else { return }
            self.query()
            self.refreshExchange()
            if Date().timeIntervalSince(self.lastSync) > 60 { self.synchronize() }
        }
        if let visibleTimer { RunLoop.main.add(visibleTimer, forMode: .common) }
    }
    func startBackgroundSync() {
        guard backgroundTimer == nil else { return }
        synchronize()
        backgroundTimer = Timer.scheduledTimer(withTimeInterval: 600, repeats: true) { [weak self] _ in self?.synchronize() }
        if let backgroundTimer { RunLoop.main.add(backgroundTimer, forMode: .common) }
    }
    func stopBackgroundSync() { backgroundTimer?.invalidate(); backgroundTimer = nil; visibleTimer?.invalidate(); visibleTimer = nil; client.stop() }
    func windowWillClose(_ notification: Notification) { visibleTimer?.invalidate(); visibleTimer = nil }

    /// Explicit offline visual QA: renders a supplied numeric snapshot without
    /// launching the collector or touching the user's statistics database.
    func capture(snapshot: [String: Any], to url: URL, dark: Bool) throws {
        window?.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        apply(snapshot); updateStatus(); surface.layoutSubtreeIfNeeded()
        guard let bitmap = surface.bitmapImageRepForCachingDisplay(in: surface.bounds) else { throw CocoaError(.fileWriteUnknown) }
        surface.cacheDisplay(in: surface.bounds, to: bitmap)
        guard let png = bitmap.representation(using: .png, properties: [:]) else { throw CocoaError(.fileWriteUnknown) }
        try png.write(to: url)
    }

    private func layout() {
        let width = surface.bounds.width, height = surface.bounds.height, right = width - 24
        titleLabel.frame = NSRect(x: 24, y: 23, width: 260, height: 32)
        subtitle.frame = NSRect(x: 25, y: 62, width: 650, height: 20)
        syncButton.frame = NSRect(x: right - 112, y: 23, width: 112, height: 32)
        infoButton.frame = NSRect(x: right - 214, y: 23, width: 96, height: 32)
        moneyButton.frame = NSRect(x: right - 348, y: 23, width: 128, height: 32)
        accountsButton.frame = NSRect(x: right - 505, y: 23, width: 151, height: 32)
        syncLabel.frame = NSRect(x: right - 330, y: 65, width: 330, height: 18)
        let sizes: [CGFloat] = [150, 175, 290, 250], controls = [range, app, model, source]
        var x: CGFloat = 24
        for (index, popup) in controls.enumerated() { popup.frame = NSRect(x: x, y: 100, width: sizes[index], height: 29); x += sizes[index] + 12 }
        let metricWidth = (width - 48 - 50) / 6
        for (index, metric) in metrics.enumerated() { metric.frame = NSRect(x: 24 + CGFloat(index) * (metricWidth + 10), y: 145, width: metricWidth, height: 101) }
        heatmapTitle.frame = NSRect(x: 25, y: 264, width: 800, height: 19)
        heatmap.frame = NSRect(x: 25, y: 292, width: width - 50, height: 155)
        tabs.frame = NSRect(x: 24, y: 453, width: 284, height: 29)
        status.frame = NSRect(x: 326, y: 451, width: width - 350, height: 34)
        scroll.frame = NSRect(x: 24, y: 495, width: width - 48, height: max(165, height - 583))
        let tableWidth = table.tableColumns.reduce(CGFloat(0)) { $0 + $1.width + table.intercellSpacing.width } + 24
        table.setFrameSize(NSSize(width: max(scroll.contentView.bounds.width, tableWidth), height: table.frame.height))
        detail.frame = NSRect(x: 27, y: height - 77, width: width - 54, height: 17)
        pageLabel.frame = NSRect(x: 27, y: height - 43, width: 600, height: 22)
        previous.frame = NSRect(x: right - 176, y: height - 46, width: 85, height: 29)
        next.frame = NSRect(x: right - 85, y: height - 46, width: 85, height: 29)
    }

    @objc private func syncNow() { synchronize() }
    private func synchronize() {
        guard !syncing, !backendRunning else { return }
        syncing = true; syncError = ""; updateStatus()
        client.run(["sync"]) { [weak self] result in
            guard let self else { return }; self.syncing = false
            switch result {
            case .success(let data):
                self.lastSync = Date(); self.readSync(data)
            case .failure(let error): self.syncError = error.localizedDescription
            }
            self.updateStatus(); self.query(); self.usageDidChange?()
        }
    }
    private func query(invalidate: Bool = false) {
        if invalidate {
            queryID += 1
            // The old numbers describe the previous filter. Do not present
            // them under a new range or a newly selected table's columns.
            rows = []; table.reloadData()
            metrics.forEach { $0.value.stringValue = "…" }
            pageLabel.stringValue = "正在查询所选范围…"; detail.stringValue = ""
            previous.isEnabled = false; next.isEnabled = false
            status.stringValue = "正在查询所选范围…"; status.textColor = .secondaryLabelColor
        }
        guard !queryInFlight else { queryPending = true; return }
        queryInFlight = true
        let identifier = queryID
        let arguments = ["query", "--range", selected(range), "--app", selected(app), "--model", selected(model), "--source", selected(source), "--page", String(page), "--page-size", "50", "--view", currentView]
        client.run(arguments) { [weak self] result in
            guard let self else { return }
            self.queryInFlight = false
            if self.queryID == identifier {
                switch result {
                case .success(let data): self.queryError = ""; self.apply(data)
                case .failure(let error): self.queryError = error.localizedDescription
                }
                self.updateStatus()
            }
            if self.queryPending { self.queryPending = false; self.query() }
        }
    }
    private func readSync(_ data: [String: Any]) {
        let sync = data["sync"] as? [String: Any] ?? [:]
        syncMetadata = sync; backendRunning = sync["running"] as? Bool ?? false
        if let time = sync["last_synced_at"] as? NSNumber, time.doubleValue > 0 { lastSync = Date(timeIntervalSince1970: time.doubleValue) }
        warnings = (sync["errors"] as? [String] ?? []) + (sync["warnings"] as? [String] ?? [])
    }
    private func apply(_ data: [String: Any]) {
        snapshot = data; readSync(data)
        let filters = data["filters"] as? [String: Any] ?? [:]
        updatePopup(app, rows: filters["apps"] as? [[String: Any]] ?? [], all: "全部应用")
        updatePopup(model, rows: filters["models"] as? [[String: Any]] ?? [], all: "全部模型")
        updatePopup(source, rows: filters["sources"] as? [[String: Any]] ?? [], all: "全部来源")
        let summary = data["summary"] as? [String: Any] ?? [:]
        let keys = ["total_tokens", "input_tokens", "output_tokens", "cache_read_tokens", "requests"]
        for (index, key) in keys.enumerated() {
            let number = number(summary, key)
            metrics[index].value.stringValue = Self.compact(number)
            metrics[index].value.toolTip = Self.integer(number)
            metrics[index].detail.stringValue = Self.integer(number)
        }
        metrics[0].detail.stringValue = "输入 + 输出 + 缓存读写"
        metrics[1].detail.stringValue = "不含缓存的输入 Tokens"
        metrics[2].detail.stringValue = "按日志的输出口径"
        let hit = number(summary, "cache_hit_rate")
        metrics[3].detail.stringValue = String(format: "命中率 %.1f%% · 写入 %@", max(0, min(1, hit)) * 100, Self.compact(number(summary, "cache_creation_tokens")))
        metrics[4].detail.stringValue = "已解析的请求用量记录"
        let money = data["money"] as? [String: Any] ?? [:], prefs = money["prefs"] as? [String: Any] ?? [:]
        let currency = prefs["currency"] as? String ?? "CNY"
        metrics[5].value.font = .monospacedDigitSystemFont(ofSize: currency == "both" ? 14 : 21, weight: .semibold)
        metrics[5].value.stringValue = CueUsageMoney.display(summary, currency: currency, compact: true)
        let unpriced = Int(number(summary, "unpriced_requests"))
        metrics[5].detail.stringValue = unpriced > 0 ? "\(Self.integer(Double(unpriced))) 条未定价 · 已定价小计" : "按模型 API 单价估算"
        let fx = money["fx"] as? [String: Any] ?? [:]
        metrics[5].value.toolTip = CueUsageMoney.display(summary, currency: "both") + "\nAPI 参考估算，非订阅扣费。\n" + ((fx["rate"] as? NSNumber).map { "1 USD = \($0.stringValue) CNY · \(fx["date"] as? String ?? "")" } ?? "尚无人民币换算汇率")
        if fx["stale"] as? Bool == true {
            metrics[5].value.toolTip = (metrics[5].value.toolTip ?? "") + "\n缓存汇率，可能已过期。" + (fx["error"] as? String ?? "")
            if currency != "USD" { metrics[5].detail.stringValue += " · 缓存汇率" }
        }
        heatmap.days = data["days"] as? [[String: Any]] ?? []
        heatmapTitle.stringValue = "最近 53 周 · 当前应用、模型和来源"
        rows = data[currentView] as? [[String: Any]] ?? []
        let pagination = data["pagination"] as? [String: Any] ?? [:]
        page = max(1, Int(number(pagination, "page"))); pages = max(1, Int(number(pagination, "pages")))
        let total = Int(number(pagination, "total"))
        pageLabel.stringValue = "共 \(Self.integer(Double(total))) 条 · 第 \(page) / \(pages) 页 · 每页 50 条"
        previous.isEnabled = page > 1; next.isEnabled = page < pages
        detail.stringValue = rows.isEmpty ? "当前筛选暂无记录。可点“立即同步”，或查看“数据说明”。" : "用量来自本机会话日志；原日志未记录供应商时，来源显示为“会话日志”。"
        table.reloadData()
    }
    private func updateStatus() {
        let busy = syncing || backendRunning
        let rebuilding = syncMetadata["rebuild"] as? Bool == true
        syncButton.isEnabled = !busy; syncButton.title = busy ? (rebuilding ? "正在重建…" : "正在同步…") : "立即同步"
        let formatter = DateFormatter(); formatter.dateFormat = "MM-dd HH:mm:ss"
        if busy {
            switch syncMetadata["phase"] as? String ?? "" {
            case "preparing": syncLabel.stringValue = "准备历史记录 \(Int(number(syncMetadata, "headers_scanned")))"
            case "importing": syncLabel.stringValue = "\(rebuilding ? "重建" : "扫描") \(Int(number(syncMetadata, "files_scanned"))) / \(Int(number(syncMetadata, "files_total"))) · 已导入 \(Self.integer(number(syncMetadata, "imported")))"
            default: syncLabel.stringValue = "正在读取本机历史，已有统计仍可查看"
            }
        } else { syncLabel.stringValue = lastSync == .distantPast ? "尚未同步" : "上次同步 \(formatter.string(from: lastSync))" }
        let error = queryError.isEmpty ? syncError : queryError
        if !error.isEmpty { status.stringValue = error; status.textColor = .systemRed; status.toolTip = error }
        else if rebuilding && busy { status.stringValue = "正在按分叉历史去重规则重建 Codex 用量，已导入部分仍可查看。"; status.textColor = .secondaryLabelColor; status.toolTip = syncMetadata["rebuild_reason"] as? String }
        else if !warnings.isEmpty { status.stringValue = "部分记录未导入 · \(warnings.count) 项，请查看数据说明"; status.textColor = .systemOrange; status.toolTip = warnings.joined(separator: "\n") }
        else { status.stringValue = busy ? "同步在后台进行，已有统计持续更新。" : "本机用量记录 · 费用为 API 参考估算"; status.textColor = .secondaryLabelColor; status.toolTip = nil }
    }

    @objc private func changeFilter(_ sender: NSPopUpButton) { page = 1; query(invalidate: true) }
    @objc private func changeTab() { page = 1; configureColumns(); query(invalidate: true) }
    @objc private func previousPage() { guard page > 1 else { return }; page -= 1; query(invalidate: true) }
    @objc private func nextPage() { guard page < pages else { return }; page += 1; query(invalidate: true) }
    @objc private func showMoneyPreferences() {
        if preferencesController == nil {
            let filters = snapshot["filters"] as? [String: Any] ?? [:]
            let known = (filters["models"] as? [[String: Any]] ?? []).compactMap { $0["id"] as? String }
            preferencesController = CueUsagePreferencesController(home: home, knownModels: known) { [weak self] in self?.query(invalidate: true); self?.usageDidChange?() }
        }
        preferencesController?.showWindow(nil)
    }
    @objc private func showAccounts() {
        if accountController == nil { accountController = CueCCAccountsController(home: home) }
        accountController?.showWindow(nil)
    }
    private func refreshExchange() {
        guard !exchangeRefreshing, Date().timeIntervalSince(lastExchangeRefresh) > 900 else { return }
        exchangeRefreshing = true; lastExchangeRefresh = Date()
        client.run(["refresh"], module: "vibecue.usage_money") { [weak self] _ in self?.exchangeRefreshing = false; self?.query(); self?.usageDidChange?() }
    }
    @objc private func showInfo() {
        let sync = snapshot["sync"] as? [String: Any] ?? [:]
        let coverage = sync["coverage"] as? [[String: Any]] ?? []
        let details = coverage.map { row -> String in
            let name = appName(row["app"] as? String ?? "")
            let available = row["available"] as? Bool ?? false
            return "\(name)：\(available ? "可读取" : "未找到记录或无法读取") · \(Int(number(row, "files"))) 个文件"
        }
        let alert = NSAlert(); alert.messageText = "本机历史用量来源"
        var text = "读取 Codex、Claude Code 与 OpenCode 已保留的本地用量记录，不修改源数据。OpenCode 支持本机 SQLite 与历史消息文件，只读取用量和模型字段。不同模型按日志字段区分；没有模型信息时保留 unknown。\n\nTokens 总量 = 新增输入 + 输出 + 缓存读取 + 缓存创建；OpenCode 单独记录的 reasoning 归入输出。OpenCode 来源显示日志中的 provider ID，其他未记录供应商时显示会话日志；这不是供应商账单或订阅扣费。删除的日志、没有 usage 的记录无法还原。\n\n" + details.joined(separator: "\n")
        if !warnings.isEmpty {
            text += "\n\n导入提示：\n" + warnings.prefix(8).joined(separator: "\n")
            text += "\n暂缓文件：\(Int(number(sync, "deferred_files")))；损坏记录：\(Int(number(sync, "malformed_records")))；超过读取上限：\(Int(number(sync, "oversized_records")))。"
        }
        text += "\n\nAPI 参考费用按当前 CC Switch 或自定义模型单价重新估算：新增输入 × 输入单价 + 输出 × 输出单价 + 缓存读取 × 读取单价 + 缓存创建 × 写入单价。四项单价均为美元／百万 Tokens，不重复计算缓存。未知模型保持未定价，汇总仅包含已定价小计；日志未记录的长上下文、服务档位及协议优惠差价不自动推断。此金额不能换算为订阅每周额度。\n\n人民币可使用自定义汇率或 Frankfurter 公开日参考汇率；自动每 6 小时检查，失败时保留带日期的缓存并标记过期。"
        alert.informativeText = text; alert.addButton(withTitle: "知道了")
        if let window { alert.beginSheetModal(for: window) } else { alert.runModal() }
    }

    private func configureColumns() {
        for column in table.tableColumns { table.removeTableColumn(column) }
        var columns: [(String, String, CGFloat)]
        if currentView == "logs" { columns = [("timestamp", "时间", 145), ("app", "应用", 95), ("source", "来源", 118), ("model", "模型", 170)] }
        else if currentView == "models" { columns = [("model", "模型", 225), ("requests", "记录数", 95)] }
        else { columns = [("app", "应用", 110), ("source", "来源", 210), ("requests", "记录数", 95)] }
        columns += [("cost", "API 参考费用", 150), ("input_tokens", "新增输入", 91), ("output_tokens", "输出", 86), ("cache_read_tokens", "缓存读取", 96), ("cache_creation_tokens", "缓存写入", 96), ("total_tokens", "总 Tokens", 100)]
        for (key, title, width) in columns {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(key)); column.title = title
            column.width = width; column.minWidth = min(70, width); column.resizingMask = .userResizingMask
            if key.hasSuffix("tokens") || key == "requests" { column.headerCell.alignment = .right }
            table.addTableColumn(column)
        }
        table.setAccessibilityLabel(currentView == "logs" ? "历史用量请求日志" : currentView == "models" ? "按模型汇总用量" : "按来源汇总用量")
    }
    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard rows.indices.contains(row), let key = tableColumn?.identifier.rawValue else { return nil }
        let identifier = NSUserInterfaceItemIdentifier("usage.cell.\(key)")
        let cell = tableView.makeView(withIdentifier: identifier, owner: self) as? NSTableCellView ?? NSTableCellView()
        cell.identifier = identifier
        if cell.textField == nil {
            let text = NSTextField(labelWithString: ""); text.font = .systemFont(ofSize: 11.5); text.lineBreakMode = .byTruncatingMiddle
            text.translatesAutoresizingMaskIntoConstraints = false; cell.addSubview(text); cell.textField = text
            NSLayoutConstraint.activate([text.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4), text.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4), text.centerYAnchor.constraint(equalTo: cell.centerYAnchor)])
        }
        let data = rows[row], field = cell.textField!
        if key == "cost" {
            let money = snapshot["money"] as? [String: Any] ?? [:], prefs = money["prefs"] as? [String: Any] ?? [:]
            field.stringValue = CueUsageMoney.display(data, currency: prefs["currency"] as? String ?? "CNY")
            if number(data, "unpriced_requests") > 0, number(data, "priced_requests") > 0 { field.stringValue += " *" }
            field.alignment = .right; field.font = .monospacedDigitSystemFont(ofSize: 11.5, weight: .regular)
        } else if key.hasSuffix("tokens") || key == "requests" {
            field.stringValue = Self.integer(number(data, key)); field.alignment = .right; field.font = .monospacedDigitSystemFont(ofSize: 11.5, weight: .regular)
        } else {
            field.alignment = .left; field.font = .systemFont(ofSize: 11.5)
            if key == "timestamp" {
                let formatter = DateFormatter(); formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
                let timestamp = number(data, key); field.stringValue = timestamp > 0 ? formatter.string(from: Date(timeIntervalSince1970: timestamp)) : "—"
            } else if key == "app" { field.stringValue = appName(data[key] as? String ?? "") }
            else if key == "source" { field.stringValue = sourceName(data[key] as? String ?? "") }
            else { field.stringValue = data[key] as? String ?? "unknown" }
        }
        field.toolTip = field.stringValue
        field.textColor = .labelColor
        return cell
    }
    func tableViewSelectionDidChange(_ notification: Notification) {
        guard rows.indices.contains(table.selectedRow) else { return }
        let row = rows[table.selectedRow]
        if let thread = row["thread_id"] as? String, !thread.isEmpty { detail.stringValue = "会话 \(thread) · \(Self.integer(number(row, "total_tokens"))) Tokens" }
    }

    private func selected(_ popup: NSPopUpButton) -> String { popup.selectedItem?.representedObject as? String ?? "all" }
    private func updatePopup(_ popup: NSPopUpButton, rows: [[String: Any]], all: String) {
        let choice = selected(popup)
        var items: [String: String] = ["all": all], order = ["all"]
        for row in rows {
            guard let id = row["id"] as? String, id != "all", items[id] == nil else { continue }
            items[id] = row["name"] as? String ?? id; order.append(id)
        }
        // Preserve an active filter even if the new result has no matching rows.
        if choice != "all", items[choice] == nil { items[choice] = popup.titleOfSelectedItem ?? choice; order.append(choice) }
        fill(popup, items, order: order, selected: choice)
    }
    private func fill(_ popup: NSPopUpButton, _ items: [String: String], order: [String]? = nil, selected: String) {
        let order = order ?? items.keys.sorted()
        if popup.itemArray.count == order.count,
           zip(popup.itemArray, order).allSatisfy({ $0.0.representedObject as? String == $0.1 && $0.0.title == (items[$0.1] ?? $0.1) }) { return }
        popup.removeAllItems()
        for id in order { popup.addItem(withTitle: items[id] ?? id); popup.lastItem?.representedObject = id }
        if let item = popup.itemArray.first(where: { $0.representedObject as? String == selected }) { popup.select(item) } else { popup.selectItem(at: 0) }
    }
    private func number(_ row: [String: Any], _ key: String) -> Double { (row[key] as? NSNumber)?.doubleValue ?? 0 }
    private func appName(_ id: String) -> String { ["codex": "Codex", "claude": "Claude Code", "opencode": "OpenCode"][id] ?? id }
    private func sourceName(_ id: String) -> String {
        if id.hasPrefix("opencode_provider:") { return "OpenCode · " + String(id.dropFirst("opencode_provider:".count)) }
        return ["codex_session": "Codex 会话日志", "claude_session": "Claude 会话日志", "opencode_session": "OpenCode 会话日志"][id] ?? id
    }
    static func integer(_ value: Double) -> String {
        let formatter = NumberFormatter(); formatter.numberStyle = .decimal; formatter.maximumFractionDigits = 0
        return formatter.string(from: NSNumber(value: value)) ?? "0"
    }
    private static func compact(_ value: Double) -> String {
        for (suffix, divisor) in [("B", 1_000_000_000.0), ("M", 1_000_000.0), ("K", 1000.0)] {
            if value >= divisor { return String(format: value / divisor >= 100 ? "%.0f%@" : "%.1f%@", value / divisor, suffix) }
        }
        return integer(value)
    }
}
