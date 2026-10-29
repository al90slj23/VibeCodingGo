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
                process.environment = Self.childEnvironment(inherited: inherited, module: module, home: home, runtime: runtime)
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
                let outputLimit = module == "vibecue.usage_money" ? 25_165_824 : 8_388_608
                var bytes = Data(), exceeded = false
                while true {
                    let chunk = output.fileHandleForReading.readData(ofLength: 65_536)
                    if chunk.isEmpty { break }
                    if bytes.count + chunk.count <= outputLimit { bytes.append(chunk) }
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

    /// Network tests inherit only configured proxy routing, never unrelated API credentials.
    static func childEnvironment(inherited: [String: String], module: String, home: URL, runtime: URL) -> [String: String] {
        var environment: [String: String] = [:]
        var allowed = ["HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "OPENCODE_DB", "VIBECUE_CC_SWITCH_QUOTA_FILE"]
        if module == "vibecue.network_tests" {
            allowed += ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"]
        }
        for key in allowed { if let value = inherited[key] { environment[key] = value } }
        environment["VIBECUE_HOME"] = home.path; environment["PYTHONPATH"] = runtime.path
        environment["PYTHONUNBUFFERED"] = "1"; environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        return environment
    }
    static func networkEnvironmentValidationReport() -> [String: Bool] {
        let fixture = ["HTTPS_PROXY": "http://proxy.example:8080", "all_proxy": "socks5h://proxy.example:1080", "NO_PROXY": "localhost", "OPENAI_API_KEY": "fixture-only", "AUTHORIZATION": "fixture-only"]
        let home = URL(fileURLWithPath: "/tmp/vcgo-env-fixture"), runtime = URL(fileURLWithPath: "/tmp/vcgo-env-fixture/runtime")
        let network = childEnvironment(inherited: fixture, module: "vibecue.network_tests", home: home, runtime: runtime)
        let usage = childEnvironment(inherited: fixture, module: "vibecue.usage", home: home, runtime: runtime)
        return ["network_transport_inherits_existing_proxy_and_bypass_configuration": network["HTTPS_PROXY"] == fixture["HTTPS_PROXY"] && network["all_proxy"] == fixture["all_proxy"] && network["NO_PROXY"] == "localhost",
                "network_transport_never_inherits_unrelated_api_credentials": network["OPENAI_API_KEY"] == nil && network["AUTHORIZATION"] == nil,
                "network_proxy_forwarding_does_not_expand_other_collectors_environment": usage["HTTPS_PROXY"] == nil && usage["all_proxy"] == nil && usage["NO_PROXY"] == nil]
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
            throw error("本机用量模块尚未安装，请重新安装最新版 VibeCoding Go。")
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

private final class CueUsageScrollView: NSScrollView {
    override func tile() {
        super.tile()
        guard scrollerStyle == .legacy, let table = documentView as? NSTableView else { return }
        // AppKit can expand the clip view behind the header and scrollers when
        // a table is wider than its viewport. Keep a real reserved viewport so
        // the last visible row never sits underneath a horizontal scroller.
        let border: CGFloat = borderType == .noBorder ? 0 : 1
        let top = border + (table.headerView?.frame.height ?? 0)
        let right = verticalScroller.flatMap { $0.isHidden ? nil : $0.frame.minX } ?? (bounds.width - border)
        let bottom = horizontalScroller.flatMap { $0.isHidden ? nil : $0.frame.minY } ?? (bounds.height - border)
        contentView.automaticallyAdjustsContentInsets = false; contentView.contentInsets = NSEdgeInsetsZero
        contentView.frame = NSRect(x: border, y: top, width: max(0, right - border), height: max(0, bottom - top))
        contentView.setBoundsOrigin(NSPoint(x: max(0, contentView.bounds.minX), y: max(0, contentView.bounds.minY)))
        reflectScrolledClipView(contentView)
    }
}

/// The table and its pager share one section, rather than a window-level footer.
private final class CueUsageListSection: NSView {
    override var isFlipped: Bool { true }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.controlBackgroundColor.setFill(); bounds.fill()
        NSColor.separatorColor.setStroke()
        let border = NSBezierPath(rect: bounds.insetBy(dx: 0.5, dy: 0.5)); border.lineWidth = 1; border.stroke()
    }
}

private final class CueUsagePaginationBar: NSView {
    override var isFlipped: Bool { true }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.controlBackgroundColor.setFill(); bounds.fill()
        NSColor.separatorColor.setStroke()
        let line = NSBezierPath(); line.move(to: NSPoint(x: 0, y: 0.5)); line.line(to: NSPoint(x: bounds.width, y: 0.5)); line.lineWidth = 1; line.stroke()
    }
}

private final class CueUsageMetric: NSView {
    let title = NSTextField(labelWithString: ""), value = NSTextField(labelWithString: "—"), detail = NSTextField(labelWithString: "")
    var preferredValueSize: CGFloat = 25
    var onSelect: (() -> Void)?
    override func mouseDown(with event: NSEvent) { onSelect?() }
    override var isFlipped: Bool { true }
    init(title: String) {
        super.init(frame: .zero)
        self.title.stringValue = title
        self.title.font = .systemFont(ofSize: 11, weight: .medium); self.title.textColor = .secondaryLabelColor
        value.font = .monospacedDigitSystemFont(ofSize: 25, weight: .semibold)
        value.maximumNumberOfLines = 2; value.lineBreakMode = .byClipping
        value.cell?.wraps = true; value.cell?.usesSingleLineMode = false
        detail.font = .systemFont(ofSize: 10); detail.textColor = .secondaryLabelColor
        detail.lineBreakMode = .byTruncatingTail
        [self.title, value, detail].forEach { addSubview($0) }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func layout() {
        super.layout()
        let contentWidth = max(20, bounds.width - 28)
        title.frame = NSRect(x: 14, y: 12, width: contentWidth, height: 17)
        let lines = value.stringValue.components(separatedBy: "\n")
        var size = preferredValueSize
        while size > 12 {
            let font = NSFont.monospacedDigitSystemFont(ofSize: size, weight: .semibold)
            if lines.allSatisfy({ ($0 as NSString).size(withAttributes: [.font: font]).width <= contentWidth }) { break }
            size -= 0.5
        }
        value.font = .monospacedDigitSystemFont(ofSize: size, weight: .semibold)
        value.frame = NSRect(x: 14, y: lines.count > 1 ? 31 : 34, width: contentWidth, height: lines.count > 1 ? 43 : 32)
        detail.frame = NSRect(x: 14, y: bounds.height - 24, width: contentWidth, height: 17)
        detail.toolTip = detail.stringValue
    }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.controlBackgroundColor.withAlphaComponent(0.70).setFill()
        NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: 10, yRadius: 10).fill()
        NSColor.separatorColor.withAlphaComponent(0.28).setStroke()
        let border = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: 10, yRadius: 10)
        border.lineWidth = 0.5; border.stroke()
    }
}


final class CueUsageDashboardController: NSWindowController, NSWindowDelegate, NSTableViewDataSource, NSTableViewDelegate, NSMenuDelegate {
    let home: URL
    var usageDidChange: (() -> Void)?
    private let client: CueUsageClient
    private let surface = CueUsageSurface()
    private let titleLabel = NSTextField(labelWithString: "用量统计")
    private let subtitle = NSTextField(labelWithString: "本机历史与 CC Switch 数值记录")
    private let range = NSPopUpButton(), app = NSPopUpButton(), model = NSPopUpButton(), source = NSPopUpButton(), provider = NSPopUpButton()
    private let statusFilter = NSPopUpButton(), heatmapMetric = NSPopUpButton()
    private let chartStyle = NSPopUpButton()
    private let columnsButton = NSButton(title: "显示列…", target: nil, action: nil), detailsButton = NSButton(title: "查看详情…", target: nil, action: nil)
    private let moreHistory = NSButton(title: "显示更多 ▾", target: nil, action: nil), lessHistory = NSButton(title: "收起", target: nil, action: nil)
    private let clearDateButton = NSButton(title: "清除日期", target: nil, action: nil)
    private let summaryButton = NSButton(title: "摘要明细…", target: nil, action: nil)
    private let syncButton = NSButton(title: "立即同步", target: nil, action: nil)
    private let infoButton = NSButton(title: "数据说明", target: nil, action: nil)
    private let moneyButton = NSButton(title: "统计设置…", target: nil, action: nil)
    private let accountsButton = NSButton(title: "Codex 账号额度…", target: nil, action: nil)
    private let syncLabel = NSTextField(labelWithString: "尚未同步")
    private let status = NSTextField(wrappingLabelWithString: "")
    private let metrics = [CueUsageMetric(title: "Tokens 总量"), CueUsageMetric(title: "新增输入"), CueUsageMetric(title: "输出"), CueUsageMetric(title: "缓存命中率"), CueUsageMetric(title: "用量记录"), CueUsageMetric(title: "API 参考费用")]
    private let heatmapTitle = NSTextField(labelWithString: "最近 53 周")
    private let heatmapRange = NSTextField(labelWithString: "")
    private let heatmapSummary = NSTextField(labelWithString: "")
    private let heatmap = CueUsageHeatmap()
    private let heatmapScroll = NSScrollView()
    private let tabs = NSSegmentedControl(labels: ["请求日志", "供应商", "模型", "来源"], trackingMode: .selectOne, target: nil, action: nil)
    private let table = NSTableView(), scroll = CueUsageScrollView()
    private let listSection = CueUsageListSection(), paginationBar = CueUsagePaginationBar()
    private let pageLabel = NSTextField(labelWithString: ""), detail = NSTextField(labelWithString: "")
    private let pageSizePopup = NSPopUpButton()
    private let previous = NSButton(title: "上一页", target: nil, action: nil), next = NSButton(title: "下一页", target: nil, action: nil)
    private var rows: [[String: Any]] = []
    private var snapshot: [String: Any] = [:]
    private var page = 1, pages = 1
    private var pageSize = 50
    private var queryID = 0
    private var queryInFlight = false
    private var queryPending = false
    private var syncing = false
    private var backendRunning = false
    private var syncMetadata: [String: Any] = [:]
    private var backgroundTimer: Timer?, visibleTimer: Timer?
    private var backgroundStarted = false, stopped = false
    private var schedule = CueUsageSchedulePreferences()
    private var scheduleObserver: NSObjectProtocol?
    private var lastSyncAttempt: Date?
    private var initialAutomaticCheck = false
    private var pricingTimer: Timer?
    private var pricingRefreshing = false
    private var pricingNextRefreshAt: Date?
    private var pricingScheduleKnown = false
    private var pricingRetryAfter: Date?
    private var lastSync = Date.distantPast
    private var syncError = ""
    private var queryError = ""
    private var warnings: [String] = []
    private var preferencesController: CueUsagePreferencesController?
    private var exchangeRefreshing = false
    private var lastExchangeRefresh = Date.distantPast
    private var accountController: CueCCAccountsController?
    private var detailController: CueUsageLogDetailsController?
    private var heatmapWeeks = 53
    private var selectedStart: String?, selectedEnd: String?
    private var previousRange = "all"
    private var columnOverrides: [String: Bool] = [:]
    private let useSavedColumns: Bool
    private var currentView: String { ["logs", "providers", "models", "sources"][max(0, min(3, tabs.selectedSegment))] }

    init(home: URL, useSavedColumns: Bool = true) {
        self.useSavedColumns = useSavedColumns
        self.home = home; client = CueUsageClient(home: home); schedule = CueUsageSchedulePreferences(home: home)
        if useSavedColumns { columnOverrides = UserDefaults.standard.dictionary(forKey: "VibeCueUsageLogColumns") as? [String: Bool] ?? [:] }
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1120, height: 850), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "VibeCoding Go · 用量统计"; window.isReleasedWhenClosed = false
        window.contentMinSize = NSSize(width: 1000, height: 760); window.center()
        super.init(window: window); window.delegate = self; window.contentView = surface
        titleLabel.font = .systemFont(ofSize: 23, weight: .semibold)
        subtitle.font = .systemFont(ofSize: 12); subtitle.textColor = .secondaryLabelColor
        syncLabel.font = .systemFont(ofSize: 11); syncLabel.textColor = .secondaryLabelColor; syncLabel.alignment = .right
        status.font = .systemFont(ofSize: 11); status.maximumNumberOfLines = 2
        heatmapTitle.font = .systemFont(ofSize: 12, weight: .medium); heatmapTitle.textColor = .secondaryLabelColor
        heatmapRange.font = .monospacedDigitSystemFont(ofSize: 10.5, weight: .regular); heatmapRange.textColor = .secondaryLabelColor; heatmapRange.alignment = .right
        heatmapSummary.font = .monospacedDigitSystemFont(ofSize: 10.5, weight: .regular); heatmapSummary.textColor = .secondaryLabelColor; heatmapSummary.lineBreakMode = .byTruncatingTail
        pageLabel.font = .systemFont(ofSize: 11); pageLabel.textColor = .secondaryLabelColor
        detail.font = .systemFont(ofSize: 10.5); detail.textColor = .secondaryLabelColor; detail.lineBreakMode = .byTruncatingTail
        for control in [syncButton, infoButton, moneyButton, accountsButton, previous, next, columnsButton, detailsButton, moreHistory, lessHistory, clearDateButton, summaryButton] { control.bezelStyle = .rounded; control.target = self }
        syncButton.action = #selector(syncNow); infoButton.action = #selector(showInfo)
        moneyButton.action = #selector(showMoneyPreferences); accountsButton.action = #selector(showAccounts)
        previous.action = #selector(previousPage); next.action = #selector(nextPage)
        columnsButton.action = #selector(showColumns); detailsButton.action = #selector(showDetails)
        moreHistory.action = #selector(expandHistory); lessHistory.action = #selector(collapseHistory)
        clearDateButton.action = #selector(clearDate); summaryButton.action = #selector(showSummary)
        syncButton.identifier = NSUserInterfaceItemIdentifier("usage.sync")
        for (control, key) in [(range, "range"), (app, "app"), (provider, "provider"), (model, "model"), (source, "source"), (statusFilter, "status")] {
            control.target = self; control.action = #selector(changeFilter(_:)); control.identifier = NSUserInterfaceItemIdentifier("usage.\(key)")
        }
        fill(range, ["today": "今天", "yesterday": "昨天", "7d": "近 7 天", "month": "本月", "30d": "近 30 天", "90d": "近 90 天", "year": "本年", "all": "全部历史", "custom": "自定义日期…"], order: ["today", "yesterday", "7d", "month", "30d", "90d", "year", "all", "custom"], selected: "all")
        range.action = #selector(changeRange)
        fill(app, ["all": "全部应用"], selected: "all"); fill(provider, ["all": "全部供应商"], selected: "all"); fill(model, ["all": "全部模型"], selected: "all"); fill(source, ["all": "全部来源"], selected: "all")
        fill(statusFilter, ["all": "全部状态", "success": "成功", "error": "错误", "unknown": "状态未记录", "200": "HTTP 200", "400": "HTTP 400", "401": "HTTP 401", "429": "HTTP 429", "500": "HTTP 500"], order: ["all", "success", "error", "unknown", "200", "400", "401", "429", "500"], selected: "all")
        fill(pageSizePopup, ["10": "每页 10 条", "20": "每页 20 条", "50": "每页 50 条"], order: ["10", "20", "50"], selected: "50")
        pageSizePopup.target = self; pageSizePopup.action = #selector(changePageSize); pageSizePopup.setAccessibilityLabel("当前列表每页记录数")
        fill(heatmapMetric, ["tokens": "Tokens", "requests": "请求数", "cost": "API 费用", "cache_hit_rate": "缓存命中率"], order: ["tokens", "requests", "cost", "cache_hit_rate"], selected: "tokens")
        heatmapMetric.target = self; heatmapMetric.action = #selector(changeHeatmapMetric)
        fill(chartStyle, ["heatmap": "日历热图", "bar": "柱形趋势图", "area": "曲线面积图"], order: ["heatmap", "bar", "area"], selected: "heatmap")
        chartStyle.target = self; chartStyle.action = #selector(changeChartStyle)
        if useSavedColumns {
            if let saved = UserDefaults.standard.string(forKey: "VibeCueUsageChartStyle"), ["heatmap", "bar", "area"].contains(saved) { select(chartStyle, saved); heatmap.chartStyle = saved }
            if let saved = UserDefaults.standard.string(forKey: "VibeCueUsageChartMetric"), ["tokens", "requests", "cost", "cache_hit_rate"].contains(saved) { select(heatmapMetric, saved); heatmap.metric = saved }
        }
        range.setAccessibilityLabel("统计时间范围"); app.setAccessibilityLabel("应用筛选"); model.setAccessibilityLabel("模型筛选"); source.setAccessibilityLabel("数据来源筛选")
        tabs.selectedSegment = 0; tabs.target = self; tabs.action = #selector(changeTab)
        if useSavedColumns, let choice = UserDefaults.standard.dictionary(forKey: "VibeCueUsageDashboardChoice") {
            if let tab = choice["tab"] as? Int, (0..<4).contains(tab) { tabs.selectedSegment = tab }
            if let size = choice["page_size"] as? Int, [10, 20, 50].contains(size) { pageSize = size; select(pageSizePopup, String(size)) }
            if let preset = choice["range"] as? String, ["today", "yesterday", "7d", "month", "30d", "90d", "year", "all"].contains(preset) { select(range, preset); previousRange = preset }
            if let saved = choice["status"] as? String, ["all", "success", "error", "unknown", "200", "400", "401", "429", "500"].contains(saved) { select(statusFilter, saved) }
        }
        tabs.identifier = NSUserInterfaceItemIdentifier("usage.tabs")
        table.delegate = self; table.dataSource = self; table.rowHeight = 27
        table.usesAlternatingRowBackgroundColors = true; table.style = .plain
        table.allowsMultipleSelection = false; table.columnAutoresizingStyle = .noColumnAutoresizing
        table.intercellSpacing = NSSize(width: 4, height: 2)
        table.setAccessibilityLabel("历史用量请求日志")
        table.target = self; table.doubleAction = #selector(showDetails)
        let rowMenu = NSMenu()
        rowMenu.delegate = self
        for (title, action) in [("查看详情…", #selector(showDetails)), ("复制所选行", #selector(copyRow)), ("导出当前页 CSV…", #selector(exportPage))] { let item = NSMenuItem(title: title, action: action, keyEquivalent: ""); item.target = self; rowMenu.addItem(item) }
        table.menu = rowMenu
        heatmapScroll.documentView = heatmap; heatmapScroll.hasVerticalScroller = true; heatmapScroll.autohidesScrollers = true; heatmapScroll.drawsBackground = false
        heatmap.onSelectDate = { [weak self] day in self?.filterDay(day) }
        for (index, metric) in metrics.enumerated() {
            metric.toolTip = ["输入、输出、缓存读取与缓存写入相加，不重复计算缓存。", "不含缓存的新增输入 Tokens。", "日志记录的输出 Tokens。", "缓存读取 /（新增输入 + 缓存读取 + 缓存写入）。", "当前筛选下的用量记录数。", "按当前来源队列基础单价估算；未定价记录单列。"][index]
            metric.onSelect = { [weak self] in self?.selectHeatmapMetric(["tokens", "tokens", "tokens", "cache_hit_rate", "requests", "cost"][index]) }
        }
        scroll.documentView = table; scroll.hasVerticalScroller = true; scroll.hasHorizontalScroller = true
        // Reserved native scroller tracks keep the final row and final header
        // visible; overlay scrollers previously covered the numbers themselves.
        scroll.scrollerStyle = .legacy; scroll.autohidesScrollers = false; scroll.borderType = .noBorder
        scroll.automaticallyAdjustsContentInsets = false; scroll.contentInsets = NSEdgeInsetsZero
        listSection.setAccessibilityElement(true); listSection.setAccessibilityRole(.group)
        paginationBar.setAccessibilityElement(true); paginationBar.setAccessibilityRole(.group)
        paginationBar.setAccessibilityLabel("当前列表分页")
        listSection.addSubview(scroll); listSection.addSubview(paginationBar)
        [pageLabel, pageSizePopup, previous, next].forEach { paginationBar.addSubview($0) }
        let views: [NSView] = [titleLabel, subtitle, range, app, provider, model, source, syncButton, infoButton, moneyButton, accountsButton, syncLabel, status, heatmapTitle, heatmapRange, heatmapSummary, heatmapMetric, heatmapScroll, tabs, listSection, detail, columnsButton, detailsButton, moreHistory, lessHistory, clearDateButton, chartStyle, summaryButton, statusFilter] + metrics
        views.forEach { surface.addSubview($0) }
        surface.onLayout = { [weak self] in self?.layout() }
        surface.onAppearance = { [weak self] in self?.metrics.forEach { $0.needsDisplay = true }; self?.heatmap.needsDisplay = true }
        configureColumns(); layout(); updateStatus()
        scheduleObserver = NotificationCenter.default.addObserver(forName: .cueUsageScheduleDidChange, object: nil, queue: .main) { [weak self] note in
            guard let self, note.userInfo?["home"] as? String == self.home.path else { return }
            self.schedule = CueUsageSchedulePreferences(home: self.home)
            self.configureBackgroundTimer(); self.configureVisibleTimer(); self.query(); self.usageDidChange?()
        }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    deinit {
        backgroundTimer?.invalidate(); visibleTimer?.invalidate(); pricingTimer?.invalidate()
        if let scheduleObserver { NotificationCenter.default.removeObserver(scheduleObserver) }
    }

    override func showWindow(_ sender: Any?) {
        super.showWindow(sender); window?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        query(); refreshExchange(); configureVisibleTimer()
    }
    private func configureVisibleTimer() {
        visibleTimer?.invalidate(); visibleTimer = nil
        guard !stopped, window?.isVisible == true else { return }
        let timer = Timer(timeInterval: schedule.displayInterval, repeats: true) { [weak self] _ in
            guard let self, self.window?.isVisible == true, !self.window!.isMiniaturized else { return }
            self.query(); self.refreshExchange()
        }
        RunLoop.main.add(timer, forMode: .common); visibleTimer = timer
    }
    private func configureBackgroundTimer() {
        backgroundTimer?.invalidate(); backgroundTimer = nil
        guard backgroundStarted, !stopped,
              let due = schedule.nextSync(lastSync: lastSync == .distantPast ? nil : lastSync, lastAttempt: lastSyncAttempt) else { return }
        // A separate process can already own the import lock. Poll its numeric
        // status without rapidly retrying the expensive history scan.
        let fire = (syncing || backendRunning) ? Date().addingTimeInterval(max(5, min(schedule.displayInterval, schedule.syncInterval))) : max(Date().addingTimeInterval(0.2), due)
        let timer = Timer(fire: fire, interval: 0, repeats: false) { [weak self] _ in
            guard let self else { return }; self.backgroundTimer = nil
            if self.syncing || self.backendRunning { self.query() }
            else { self.synchronize() }
            self.configureBackgroundTimer()
        }
        RunLoop.main.add(timer, forMode: .common); backgroundTimer = timer
    }
    func startBackgroundSync() {
        guard !backgroundStarted, !stopped else { return }; backgroundStarted = true
        initialAutomaticCheck = true
        // Read the persisted ledger timestamp before deciding whether a scan is
        // due, so restarting the app respects the selected history interval.
        query(); refreshPricing()
    }
    func stopBackgroundSync() {
        stopped = true; backgroundStarted = false
        heatmap.hidePreview()
        backgroundTimer?.invalidate(); backgroundTimer = nil; visibleTimer?.invalidate(); visibleTimer = nil
        pricingTimer?.invalidate(); pricingTimer = nil; client.stop()
    }
    func windowWillClose(_ notification: Notification) { visibleTimer?.invalidate(); visibleTimer = nil; heatmap.hidePreview() }
    func windowWillMiniaturize(_ notification: Notification) { heatmap.hidePreview() }
    func windowDidResignKey(_ notification: Notification) { heatmap.hidePreview() }

    /// Explicit offline visual QA: renders a supplied numeric snapshot without
    /// launching the collector or touching the user's statistics database.
    func capture(snapshot: [String: Any], to url: URL, dark: Bool) throws {
        window?.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        apply(snapshot); updateStatus(); surface.layoutSubtreeIfNeeded()
        guard let bitmap = surface.bitmapImageRepForCachingDisplay(in: surface.bounds) else { throw CocoaError(.fileWriteUnknown) }
        surface.effectiveAppearance.performAsCurrentDrawingAppearance { surface.cacheDisplay(in: surface.bounds, to: bitmap) }
        guard let png = bitmap.representation(using: .png, properties: [:]) else { throw CocoaError(.fileWriteUnknown) }
        try png.write(to: url)
    }

    /// Deterministic offline fixture. No collector, network request, timer, or
    /// user database is started by creating or rendering this controller.
    static func captureFixture(to url: URL, dark: Bool, size: NSSize = NSSize(width: 1120, height: 850), currency: String = "CNY", historyWeeks: Int = 53, chartStyle: String = "heatmap", metric: String = "tokens", hourly: Bool = false) throws {
        let controller = fixtureController(size: size, currency: currency, dark: dark)
        controller.heatmapWeeks = max(53, min(530, historyWeeks)); controller.heatmap.weeks = controller.heatmapWeeks
        controller.select(controller.chartStyle, chartStyle); controller.heatmap.chartStyle = chartStyle
        controller.select(controller.heatmapMetric, metric); controller.heatmap.metric = metric
        if hourly {
            let reference = controller.heatmap.referenceDate ?? Date(); let start = Calendar.current.startOfDay(for: reference)
            controller.snapshot["trend_granularity"] = "hour"
            var points: [[String: Any]] = []
            for hour in 0..<24 {
                var point: [String: Any] = ["label": String(format: "%02d:00", hour), "cache_creation_tokens": 0, "cache_hit_rate": 0.91, "unpriced_requests": 0]
                point["timestamp"] = start.addingTimeInterval(Double(hour * 3600)).timeIntervalSince1970
                point["tokens"] = 10000 + hour * 12000; point["requests"] = hour + 1
                point["cost_usd"] = Double(hour) * 0.2; point["cost_cny"] = Double(hour) * 1.42
                point["input_tokens"] = hour * 100; point["output_tokens"] = hour * 20; point["cache_read_tokens"] = hour * 2000; point["priced_requests"] = hour + 1
                points.append(point)
            }
            controller.snapshot["trend_points"] = points
            var total: [String: Any] = [:]
            for key in ["requests", "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "cost_usd", "cost_cny", "priced_requests", "unpriced_requests"] { total[key] = points.reduce(0.0) { $0 + (($1[key] as? NSNumber)?.doubleValue ?? 0) } }
            total["total_tokens"] = points.reduce(0.0) { $0 + (($1["tokens"] as? NSNumber)?.doubleValue ?? 0) }; total["cache_hit_rate"] = 0.91
            controller.snapshot["summary"] = total
        }
        defer { controller.window?.orderOut(nil) }
        try controller.capture(snapshot: controller.snapshot, to: url, dark: dark)
    }

    static func validationReport() -> [String: Bool] {
        var report = CueUsageScheduleController.validationReport()
        report.merge(CueUsageLogDetailsController.validationReport()) { _, new in new }
        report.merge(CueUsageHeatmap.validationReport()) { _, new in new }
        let scheduled = fixtureController(size: NSSize(width: 1120, height: 850), currency: "CNY", dark: true)
        scheduled.backgroundStarted = true
        scheduled.schedule = CueUsageSchedulePreferences(autoSync: false)
        scheduled.configureBackgroundTimer()
        report["usage_schedule_manual_has_no_history_timer"] = scheduled.backgroundTimer == nil
        scheduled.lastSync = Date().addingTimeInterval(-100)
        scheduled.schedule = CueUsageSchedulePreferences(syncInterval: 1200, displayInterval: 17)
        scheduled.configureBackgroundTimer()
        let prior = scheduled.backgroundTimer
        report["usage_schedule_history_timer_respects_custom_interval"] = abs((prior?.fireDate.timeIntervalSinceNow ?? 0) - 1100) < 1
        scheduled.schedule = CueUsageSchedulePreferences(syncInterval: 300, displayInterval: 17)
        scheduled.configureBackgroundTimer()
        report["usage_schedule_reconfiguration_replaces_timer"] = prior?.isValid == false && abs((scheduled.backgroundTimer?.fireDate.timeIntervalSinceNow ?? 0) - 200) < 1
        scheduled.schedule = CueUsageSchedulePreferences(autoSync: false)
        scheduled.configureBackgroundTimer()
        report["usage_schedule_manual_toggle_cancels_existing_timer"] = scheduled.backgroundTimer == nil
        scheduled.backgroundStarted = false
        let priced = fixtureController(size: NSSize(width: 1120, height: 850), currency: "CNY", dark: true)
        priced.backgroundStarted = true
        let dueMetadata: [String: Any] = ["pricing_next_refresh_at": Date().addingTimeInterval(-1).timeIntervalSince1970, "pricing_refresh_due": true]
        priced.readPricingSchedule(dueMetadata)
        let priceTimer = priced.pricingTimer, priceDate = priceTimer?.fireDate
        priced.readPricingSchedule(dueMetadata)
        report["usage_schedule_repeated_queries_do_not_delay_price_refresh"] = priceTimer != nil && priceTimer === priced.pricingTimer && priceDate == priced.pricingTimer?.fireDate
        priced.pricingTimer?.invalidate(); priced.pricingTimer = nil
        priced.pricingRetryAfter = Date().addingTimeInterval(900)
        priced.readPricingSchedule(dueMetadata)
        report["usage_schedule_failed_price_worker_keeps_retry_delay"] = (priced.pricingTimer?.fireDate.timeIntervalSinceNow ?? 0) > 899
        priced.readPricingSchedule(["pricing_next_refresh_at": NSNull(), "pricing_refresh_due": false])
        report["usage_schedule_no_remote_sources_cancel_price_timer"] = priced.pricingTimer == nil && priced.pricingNextRefreshAt == nil && priceTimer?.isValid == false
        priced.backgroundStarted = false
        for size in [NSSize(width: 1000, height: 760), NSSize(width: 1120, height: 850), NSSize(width: 1440, height: 900)] {
            for currency in ["CNY", "USD", "both"] {
                let controller = fixtureController(size: size, currency: currency, dark: true)
                defer { controller.window?.orderOut(nil) }
                let prefix = "usage.layout.\(Int(size.width))x\(Int(size.height)).\(currency)"
                let chart = controller.heatmap.geometry(width: controller.heatmap.bounds.width)
                report[prefix + ".monthsSeparated"] = zip(chart.monthLabels, chart.monthLabels.dropFirst()).allSatisfy { $0.0.frame.maxX + 7.9 <= $0.1.frame.minX }
                report[prefix + ".monthsWithinCalendar"] = chart.monthLabels.allSatisfy { $0.frame.minX >= chart.grid.minX && $0.frame.maxX <= chart.grid.maxX + 0.1 }
                report[prefix + ".calendar53x7"] = abs(chart.grid.width - (53 * chart.cell + 52 * chart.gap)) < 0.1 && abs(chart.grid.height - (7 * chart.cell + 6 * chart.gap)) < 0.1
                report[prefix + ".calendarLegendFits"] = chart.grid.maxY + 27 <= controller.heatmap.bounds.height
                report[prefix + ".sectionSpacing"] = controller.metrics.allSatisfy { $0.frame.maxY + 12 <= controller.heatmapTitle.frame.minY } && controller.heatmapScroll.frame.maxY + 12 <= controller.tabs.frame.minY && controller.tabs.frame.maxY + 10 <= controller.listSection.frame.minY && controller.listSection.frame.maxY + 8 <= controller.detail.frame.minY
                report[prefix + ".paginationBelongsToList"] = controller.scroll.superview === controller.listSection && controller.paginationBar.superview === controller.listSection && [controller.pageLabel, controller.previous, controller.next].allSatisfy { $0.superview === controller.paginationBar } && controller.detail.superview === controller.surface
                report[prefix + ".paginationBelowTable"] = controller.scroll.frame.maxY <= controller.paginationBar.frame.minY && [controller.pageLabel, controller.previous, controller.next].allSatisfy { controller.paginationBar.bounds.contains($0.frame) }
                report[prefix + ".filterSpacing"] = zip([controller.range, controller.app, controller.model], [controller.app, controller.model, controller.source]).allSatisfy { $0.0.frame.maxX + 11 <= $0.1.frame.minX } && abs(controller.source.frame.maxX - (size.width - 24)) < 0.1
                report[prefix + ".metricSpacing"] = zip(controller.metrics, controller.metrics.dropFirst()).allSatisfy { $0.0.frame.maxX + 9 <= $0.1.frame.minX } && controller.metrics.last!.frame.maxX <= size.width - 23
                let value = controller.metrics[5].value, font = controller.metrics[5].value.font!
                report[prefix + ".fullCurrencyFits"] = value.stringValue.components(separatedBy: "\n").allSatisfy { ($0 as NSString).size(withAttributes: [.font: font]).width <= value.bounds.width + 0.1 } && !value.stringValue.contains("K") && !value.stringValue.contains("M") && !value.stringValue.contains("…")
                report[prefix + ".currencyLines"] = value.stringValue.components(separatedBy: "\n").count == (currency == "both" ? 2 : 1)
                controller.scroll.tile()
                let clip = controller.scroll.contentView.frame
                report[prefix + ".scrollerReserved"] = controller.scroll.scrollerStyle == .legacy && controller.scroll.horizontalScroller.map { !$0.isHidden && !$0.frame.intersects(clip) } == true && controller.scroll.verticalScroller.map { !$0.isHidden && !$0.frame.intersects(clip) } == true
                report[prefix + ".verticalScrollRange"] = controller.scroll.verticalScroller.map { $0.isEnabled && $0.knobProportion > 0 && $0.knobProportion < 1 } == true
                report[prefix + ".horizontalScrollRange"] = controller.table.frame.width <= clip.width || controller.scroll.horizontalScroller.map { $0.isEnabled && $0.knobProportion > 0 && $0.knobProportion < 1 } == true
                report[prefix + ".tableViewport"] = controller.scroll.contentSize.height >= 90 && controller.table.frame.height >= CGFloat(controller.rows.count) * (controller.table.rowHeight + controller.table.intercellSpacing.height)
                if let cost = controller.table.tableColumns.first(where: { $0.identifier.rawValue == "cost" }) {
                    report[prefix + ".tableCurrencyFits"] = controller.rows.allSatisfy { (CueUsageMoney.display($0, currency: currency) as NSString).size(withAttributes: [.font: NSFont.monospacedDigitSystemFont(ofSize: 11.5, weight: .regular)]).width + 8 <= cost.width }
                }
            }
        }
        let paging = fixtureController(size: NSSize(width: 1120, height: 850), currency: "CNY", dark: true)
        report["usage_calendar_and_trend_styles_are_available"] = paging.chartStyle.numberOfItems == 3 && paging.heatmapMetric.numberOfItems == 4
        report["usage_provider_status_and_custom_date_filters_available"] = paging.provider.numberOfItems >= 1 && paging.statusFilter.numberOfItems == 9 && paging.statusFilter.itemArray.contains { $0.representedObject as? String == "429" } && paging.range.itemArray.contains { $0.representedObject as? String == "custom" }
        report["usage_page_sizes_match_cc_switch"] = paging.pageSizePopup.itemArray.compactMap { $0.representedObject as? String } == ["10", "20", "50"] && paging.pageSize == 50 && paging.pageSizePopup.superview === paging.paginationBar
        report["usage_default_columns_include_cc_performance_fields"] = paging.table.tableColumns.map { $0.identifier.rawValue }.contains("speed_tps") && paging.table.tableColumns.map { $0.identifier.rawValue }.contains("first_token_ms")
        paging.columnOverrides["status_code"] = true; paging.configureColumns()
        report["usage_log_columns_can_be_customized_without_losing_time"] = paging.table.tableColumns.contains { $0.identifier.rawValue == "status_code" } && paging.table.tableColumns.first?.identifier.rawValue == "timestamp"
        paging.tabs.selectedSegment = 1; paging.configureColumns()
        report["usage_providers_have_their_own_grouping_table"] = paging.currentView == "providers" && paging.table.tableColumns.first?.identifier.rawValue == "provider_name"
        paging.tabs.selectedSegment = 0; paging.configureColumns()
        let overview = paging.metrics.map { $0.value.stringValue }, calendarDays = paging.heatmap.days.count, message = paging.status.stringValue
        paging.queryInFlight = true // Exercise invalidation without starting a worker.
        paging.nextPage()
        report["usage_pagination_changes_only_list_loading"] = paging.page == 2 && paging.rows.isEmpty && paging.pageLabel.stringValue.contains("第 2 页") && paging.metrics.map { $0.value.stringValue } == overview && paging.heatmap.days.count == calendarDays && paging.status.stringValue == message
        return report
    }

    private static func fixtureController(size: NSSize, currency: String, dark: Bool) -> CueUsageDashboardController {
        let controller = CueUsageDashboardController(home: FileManager.default.temporaryDirectory.appendingPathComponent("vibecue-usage-fixture-\(UUID().uuidString)", isDirectory: true), useSavedColumns: false)
        controller.window?.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        controller.window?.setContentSize(size)
        controller.surface.frame = NSRect(origin: .zero, size: size)
        var calendar = Calendar(identifier: .gregorian); calendar.timeZone = .current
        let reference = calendar.date(from: DateComponents(year: 2026, month: 10, day: 29, hour: 12))!
        controller.heatmap.referenceDate = reference
        let formatter = DateFormatter(); formatter.locale = Locale(identifier: "en_US_POSIX"); formatter.dateFormat = "yyyy-MM-dd"
        let first = calendar.date(byAdding: .day, value: -367, to: reference)!
        let days: [[String: Any]] = (0..<368).map { index in
            let date = calendar.date(byAdding: .day, value: index, to: first)!
            let tokens = index < 80 ? (index % 17 == 0 ? 200_000 : 0) : (1_000_000 + (index * 2_321_123) % 800_000_000)
            return ["date": formatter.string(from: date), "tokens": tokens, "requests": (index * 37) % 4500, "input_tokens": tokens / 20, "output_tokens": tokens / 50, "cache_read_tokens": tokens * 9 / 10, "cache_creation_tokens": 0, "cost_usd": Double(tokens) / 1_000_000, "cost_cny": Double(tokens) / 1_000_000 * 7.10364, "cache_hit_rate": 0.967, "priced_requests": (index * 37) % 4500, "unpriced_requests": 0]
        }
        var logs: [[String: Any]] = []
        for index in 0..<50 {
            var row: [String: Any] = ["app": "codex", "source": "codex_session", "thread_id": "offline-fixture", "priced_requests": 1, "unpriced_requests": 0]
            row["timestamp"] = reference.timeIntervalSince1970 - Double(index * 13)
            row["provider_name"] = index % 6 == 0 ? "会话日志" : "OpenAI"
            row["provider_id"] = index % 6 == 0 ? "" : "openai"
            if index % 3 != 0 { row["duration_ms"] = 14000 + index * 200; row["first_token_ms"] = 1400 + index * 80; row["speed_tps"] = 45.4; row["timing_estimated"] = index % 2 == 0 }
            if index % 4 == 0 { row["status_code"] = index == 4 ? 503 : 200 }
            row["model"] = index % 6 == 0 ? "codex-auto-review" : "gpt-6.1-sol"
            row["input_tokens"] = 4702 + index * 32; row["output_tokens"] = 502 + index * 7
            row["cache_read_tokens"] = 169728 + index * 11; row["cache_creation_tokens"] = 0
            row["total_tokens"] = 174932 + index * 50
            row["cost_usd"] = index == 2 ? 55000.0 : 0.0454 + Double(index) / 100
            row["cost_cny"] = index == 2 ? 390700.22 : 0.3224 + Double(index) * 0.071
            logs.append(row)
        }
        controller.apply([
            "summary": ["total_tokens": 126_000_000_000.0, "input_tokens": 3_600_000_000.0, "output_tokens": 372_000_000, "cache_read_tokens": 121_000_000_000.0, "cache_creation_tokens": 468_000_000, "cache_hit_rate": 0.967, "requests": 772639, "cost_usd": 55000.0, "cost_cny": 390700.22, "priced_requests": 698063, "unpriced_requests": 74576],
            "days": days, "logs": logs,
            "heatmap_summary": ["total_tokens": 126_000_000_000.0, "requests": 772639, "cost_usd": 55000.0, "cost_cny": 390700.22, "priced_requests": 698063, "unpriced_requests": 74576],
            "group_counts": ["logs": 772639, "providers": 4, "models": 36, "sources": 5],
            "pagination": ["page": 1, "pages": 15453, "total": 772639],
            "money": ["prefs": ["currency": currency], "fx": ["rate": 7.10364, "date": "2026-10-29", "stale": false]],
            "sync": ["last_synced_at": reference.timeIntervalSince1970, "errors": [], "warnings": []]
        ])
        controller.updateStatus(); controller.surface.layoutSubtreeIfNeeded(); controller.scroll.tile()
        return controller
    }

    private func layout() {
        let width = surface.bounds.width, height = surface.bounds.height, right = width - 24
        titleLabel.frame = NSRect(x: 24, y: 23, width: 140, height: 32)
        summaryButton.frame = NSRect(x: 170, y: 25, width: 105, height: 28)
        subtitle.frame = NSRect(x: 25, y: 62, width: max(300, width - 400), height: 20)
        syncButton.frame = NSRect(x: right - 112, y: 23, width: 112, height: 32)
        infoButton.frame = NSRect(x: right - 214, y: 23, width: 96, height: 32)
        moneyButton.frame = NSRect(x: right - 348, y: 23, width: 128, height: 32)
        accountsButton.frame = NSRect(x: right - 505, y: 23, width: 151, height: 32)
        syncLabel.frame = NSRect(x: right - 330, y: 65, width: 330, height: 18)
        let controls = [range, app, provider, model, source]
        let filterWidth = width - 48 - 48
        let sizes: [CGFloat] = [0.14, 0.16, 0.22, 0.27, 0.21].map { filterWidth * $0 }
        var x: CGFloat = 24
        for (index, popup) in controls.enumerated() { popup.frame = NSRect(x: x, y: 100, width: sizes[index], height: 29); x += sizes[index] + 12 }
        let metricWidth = (width - 48 - 50) / 6.4
        var metricX: CGFloat = 24
        for (index, metric) in metrics.enumerated() {
            let cardWidth = metricWidth * (index == 5 ? 1.4 : 1)
            metric.frame = NSRect(x: metricX, y: 142, width: cardWidth, height: 101)
            metric.needsLayout = true; metric.layoutSubtreeIfNeeded(); metricX += cardWidth + 10
        }
        heatmapTitle.frame = NSRect(x: 25, y: 260, width: 135, height: 19)
        heatmapMetric.frame = NSRect(x: 158, y: 254, width: 122, height: 28)
        heatmapSummary.frame = NSRect(x: 292, y: 260, width: max(120, width - 590), height: 19)
        heatmapRange.frame = NSRect(x: width - 285, y: 260, width: 260, height: 19)
        heatmapRange.stringValue = heatmap.rangeDescription
        let calendarHeight = CueUsageHeatmap.preferredHeight(width: width - 48)
        heatmapScroll.frame = NSRect(x: 24, y: 287, width: width - 48, height: calendarHeight)
        heatmap.frame = NSRect(x: 0, y: 0, width: heatmapScroll.contentSize.width, height: heatmap.contentHeight(width: heatmapScroll.contentSize.width))
        heatmapScroll.tile()
        moreHistory.frame = NSRect(x: 24, y: heatmapScroll.frame.maxY + 3, width: 110, height: 26)
        lessHistory.frame = NSRect(x: 141, y: heatmapScroll.frame.maxY + 3, width: 66, height: 26)
        clearDateButton.frame = NSRect(x: 217, y: heatmapScroll.frame.maxY + 3, width: 105, height: 26)
        chartStyle.frame = NSRect(x: width - 180, y: heatmapScroll.frame.maxY + 3, width: 156, height: 26)
        moreHistory.isHidden = heatmap.chartStyle != "heatmap"; lessHistory.isHidden = heatmapWeeks == 53 || heatmap.chartStyle != "heatmap"; clearDateButton.isHidden = selectedStart == nil
        let tabsY = heatmapScroll.frame.maxY + 42
        tabs.frame = NSRect(x: 24, y: tabsY, width: 374, height: 29)
        status.frame = NSRect(x: 410, y: tabsY, width: max(120, width - 756), height: 29)
        statusFilter.frame = NSRect(x: width - 334, y: tabsY, width: 116, height: 29)
        columnsButton.frame = NSRect(x: width - 212, y: tabsY, width: 88, height: 29)
        detailsButton.frame = NSRect(x: width - 118, y: tabsY, width: 94, height: 29)
        let tableY = tabsY + 41
        listSection.frame = NSRect(x: 24, y: tableY, width: width - 48, height: max(160, height - tableY - 42))
        let sectionWidth = listSection.bounds.width, sectionHeight = listSection.bounds.height
        scroll.frame = NSRect(x: 1, y: 1, width: sectionWidth - 2, height: sectionHeight - 40)
        paginationBar.frame = NSRect(x: 1, y: sectionHeight - 39, width: sectionWidth - 2, height: 38)
        let tableWidth = table.tableColumns.reduce(CGFloat(0)) { $0 + $1.width } + CGFloat(max(0, table.tableColumns.count - 1)) * table.intercellSpacing.width
        table.setFrameSize(NSSize(width: max(scroll.contentView.bounds.width, tableWidth), height: max(scroll.contentSize.height, CGFloat(rows.count) * (table.rowHeight + table.intercellSpacing.height))))
        scroll.tile()
        detail.frame = NSRect(x: 27, y: height - 29, width: width - 54, height: 17)
        let pagerWidth = paginationBar.bounds.width
        pageLabel.frame = NSRect(x: 12, y: 10, width: max(300, pagerWidth - 330), height: 18)
        pageSizePopup.frame = NSRect(x: pagerWidth - 310, y: 5, width: 105, height: 28)
        previous.frame = NSRect(x: pagerWidth - 189, y: 5, width: 85, height: 28)
        next.frame = NSRect(x: pagerWidth - 97, y: 5, width: 85, height: 28)
        updateChartHeader()
    }

    @objc private func syncNow() { synchronize() }
    private func synchronize() {
        guard !stopped, !syncing, !backendRunning else { return }
        syncing = true; lastSyncAttempt = Date(); syncError = ""; configureBackgroundTimer(); updateStatus()
        client.run(["sync"]) { [weak self] result in
            guard let self else { return }; self.syncing = false
            switch result {
            case .success(let data):
                self.lastSync = Date(); self.readSync(data)
            case .failure(let error): self.syncError = error.localizedDescription
            }
            self.updateStatus(); self.configureBackgroundTimer(); self.query(); self.usageDidChange?()
        }
    }
    private func query(invalidate: Bool = false, tableOnly: Bool = false) {
        if invalidate {
            queryID += 1
            // The old numbers describe the previous filter. Do not present
            // them under a new range or a newly selected table's columns.
            rows = []; table.reloadData()
            if !tableOnly { metrics.forEach { $0.value.stringValue = "…" } }
            pageLabel.stringValue = tableOnly ? "正在读取第 \(page) 页…" : "正在查询所选列表…"; detail.stringValue = ""
            previous.isEnabled = false; next.isEnabled = false
            if !tableOnly { status.stringValue = "正在查询所选范围…"; status.textColor = .secondaryLabelColor }
        }
        guard !queryInFlight else { queryPending = true; return }
        queryInFlight = true
        let identifier = queryID
        var arguments = ["query", "--range", selected(range), "--app", selected(app), "--provider", selected(provider), "--model", selected(model), "--source", selected(source), "--status", selected(statusFilter), "--heatmap-weeks", String(heatmapWeeks), "--page", String(page), "--page-size", String(pageSize), "--view", currentView]
        if selected(range) == "custom", let start = selectedStart, let end = selectedEnd { arguments += ["--start-date", start, "--end-date", end] }
        client.run(arguments) { [weak self] result in
            guard let self else { return }
            self.queryInFlight = false
            if self.queryID == identifier {
                switch result {
                case .success(let data): self.queryError = ""; self.apply(data)
                case .failure(let error): self.queryError = error.localizedDescription
                }
                self.updateStatus()
                if self.initialAutomaticCheck { self.initialAutomaticCheck = false; self.configureBackgroundTimer() }
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
        if backgroundStarted { configureBackgroundTimer() }
        readPricingSchedule(data["money"] as? [String: Any] ?? [:])
        let filters = data["filters"] as? [String: Any] ?? [:]
        updatePopup(app, rows: filters["apps"] as? [[String: Any]] ?? [], all: "全部应用")
        updatePopup(provider, rows: filters["providers"] as? [[String: Any]] ?? [], all: "全部供应商")
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
        metrics[3].value.stringValue = String(format: "%.1f%%", max(0, min(1, hit)) * 100)
        metrics[3].value.toolTip = metrics[3].toolTip
        metrics[3].detail.stringValue = "读取 \(Self.compact(number(summary, "cache_read_tokens"))) · 写入 \(Self.compact(number(summary, "cache_creation_tokens")))"
        metrics[4].detail.stringValue = "已解析的请求用量记录"
        let money = data["money"] as? [String: Any] ?? [:], prefs = money["prefs"] as? [String: Any] ?? [:]
        let currency = prefs["currency"] as? String ?? "CNY"
        metrics[5].preferredValueSize = currency == "both" ? 18 : 21
        metrics[5].value.stringValue = CueUsageMoney.display(summary, currency: currency).replacingOccurrences(of: " / ", with: "\n")
        let unpriced = Int(number(summary, "unpriced_requests"))
        metrics[5].detail.stringValue = unpriced > 0 ? "\(Self.integer(Double(unpriced))) 条未定价 · 已定价小计" : "按模型 API 单价估算"
        let fx = money["fx"] as? [String: Any] ?? [:]
        metrics[5].value.toolTip = CueUsageMoney.display(summary, currency: "both") + "\nAPI 参考估算，非订阅扣费。\n" + ((fx["rate"] as? NSNumber).map { "1 USD = \($0.stringValue) CNY · \(fx["date"] as? String ?? "")" } ?? "尚无人民币换算汇率")
        if fx["stale"] as? Bool == true {
            metrics[5].value.toolTip = (metrics[5].value.toolTip ?? "") + "\n缓存汇率，可能已过期。" + (fx["error"] as? String ?? "")
            if currency != "USD" { metrics[5].detail.stringValue += " · 缓存汇率" }
        }
        heatmap.days = data["days"] as? [[String: Any]] ?? []
        heatmap.trendGranularity = data["trend_granularity"] as? String ?? "day"
        heatmap.trendPoints = data["trend_points"] as? [[String: Any]]
        heatmap.currency = currency
        if heatmap.weeks != heatmapWeeks { heatmap.weeks = heatmapWeeks }
        heatmap.selectedDate = selectedStart == selectedEnd ? selectedStart : nil
        let heatRange = data["heatmap_range"] as? [String: Any] ?? [:]
        heatmap.periodStart = heatRange["selected_start_date"] as? String ?? heatRange["start_date"] as? String
        heatmap.periodEnd = heatRange["selected_end_date"] as? String ?? heatRange["end_date"] as? String
        heatmap.setAccessibilityLabel("每日用量日历与趋势图，" + heatmap.rangeDescription)
        let counts = data["group_counts"] as? [String: Any] ?? [:]
        for (index, item) in ["logs", "providers", "models", "sources"].enumerated() { tabs.setLabel(["请求日志", "供应商", "模型", "来源"][index] + ((counts[item] as? NSNumber).map { " \(Self.compact($0.doubleValue))" } ?? ""), forSegment: index) }
        tabs.toolTip = ["logs", "providers", "models", "sources"].enumerated().map { index, key in ["请求日志", "供应商", "模型", "来源"][index] + "：" + ((counts[key] as? NSNumber).map { Self.integer($0.doubleValue) } ?? "—") }.joined(separator: "\n")
        rows = data[currentView] as? [[String: Any]] ?? []
        // Give complete currency strings their measured native column width;
        // horizontal scrolling is preferable to silently clipping an amount.
        if let column = table.tableColumns.first(where: { $0.identifier.rawValue == "cost" }) {
            let font = NSFont.monospacedDigitSystemFont(ofSize: 11.5, weight: .regular)
            let amountWidth = rows.map { (CueUsageMoney.display($0, currency: currency) as NSString).size(withAttributes: [.font: font]).width }.max() ?? 0
            column.width = max(column.width, ceil(amountWidth) + 20)
        }
        let pagination = data["pagination"] as? [String: Any] ?? [:]
        page = max(1, Int(number(pagination, "page"))); pages = max(1, Int(number(pagination, "pages")))
        let total = Int(number(pagination, "total"))
        let listTitle = ["请求日志", "供应商", "模型", "来源"][max(0, min(3, tabs.selectedSegment))]
        listSection.setAccessibilityLabel(listTitle + "列表与分页")
        previous.setAccessibilityLabel(listTitle + "上一页"); next.setAccessibilityLabel(listTitle + "下一页")
        previous.toolTip = "查看当前" + listTitle + "列表的上一页"; next.toolTip = "查看当前" + listTitle + "列表的下一页"
        pageLabel.stringValue = "\(listTitle) · 共 \(Self.integer(Double(total))) 条 · 第 \(page) / \(pages) 页"
        previous.isEnabled = page > 1; next.isEnabled = page < pages
        detailsButton.isEnabled = rows.indices.contains(table.selectedRow)
        columnsButton.isEnabled = currentView == "logs"; statusFilter.isEnabled = true
        moreHistory.isEnabled = heatmapWeeks < 530 && heatRange["has_older"] as? Bool != false
        detail.stringValue = rows.isEmpty ? "当前筛选暂无记录。可点“立即同步”，或查看“数据说明”。" : "用量来自本机历史与 CC Switch 数值记录；未记录的字段不推断。"
        table.reloadData()
        layout()
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

    private func rememberChoices() { if useSavedColumns { UserDefaults.standard.set(["tab": tabs.selectedSegment, "range": selected(range) == "custom" ? "all" : selected(range), "status": selected(statusFilter), "page_size": pageSize], forKey: "VibeCueUsageDashboardChoice") } }
    @objc private func changePageSize() { pageSize = Int(selected(pageSizePopup)) ?? 50; page = 1; rememberChoices(); query(invalidate: true, tableOnly: true) }
    @objc private func changeFilter(_ sender: NSPopUpButton) { page = 1; rememberChoices(); query(invalidate: true) }
    @objc private func changeRange() {
        if selected(range) != "custom" { previousRange = selected(range); selectedStart = nil; selectedEnd = nil; page = 1; rememberChoices(); query(invalidate: true); return }
        let alert = NSAlert(); alert.messageText = "选择统计日期"; alert.informativeText = "按本机时区，包含开始和结束日期。"
        let root = NSView(frame: NSRect(x: 0, y: 0, width: 430, height: 65))
        let start = NSDatePicker(frame: NSRect(x: 0, y: 22, width: 190, height: 30)), end = NSDatePicker(frame: NSRect(x: 225, y: 22, width: 190, height: 30))
        for field in [start, end] { field.datePickerStyle = .textFieldAndStepper; field.datePickerElements = [.yearMonthDay]; field.locale = Locale(identifier: "zh_CN"); field.maxDate = Date(); root.addSubview(field) }
        let formatter = DateFormatter(); formatter.dateFormat = "yyyy-MM-dd"
        start.dateValue = selectedStart.flatMap { formatter.date(from: $0) } ?? Calendar.current.date(byAdding: .day, value: -6, to: Date())!
        end.dateValue = selectedEnd.flatMap { formatter.date(from: $0) } ?? Date()
        alert.accessoryView = root; alert.addButton(withTitle: "应用日期"); alert.addButton(withTitle: "取消")
        guard let window else { return }
        alert.beginSheetModal(for: window) { [weak self] response in
            guard let self else { return }
            if response == .alertFirstButtonReturn, start.dateValue <= end.dateValue {
                self.selectedStart = formatter.string(from: start.dateValue); self.selectedEnd = formatter.string(from: end.dateValue); self.page = 1; self.query(invalidate: true)
            } else { self.select(self.range, self.previousRange) }
        }
    }
    private func filterDay(_ day: String) { selectedStart = day; selectedEnd = day; select(range, "custom"); page = 1; tabs.selectedSegment = 0; configureColumns(); query(invalidate: true) }
    @objc private func clearDate() { selectedStart = nil; selectedEnd = nil; select(range, "all"); previousRange = "all"; page = 1; query(invalidate: true) }
    @objc private func expandHistory() { guard heatmapWeeks < 530 else { return }; heatmapWeeks += 53; heatmap.weeks = heatmapWeeks; layout(); query() }
    @objc private func collapseHistory() { heatmapWeeks = 53; heatmap.weeks = 53; heatmapScroll.contentView.scroll(to: .zero); layout(); query() }
    private func updateChartHeader() {
        let calendar = heatmap.chartStyle == "heatmap", summary = snapshot[calendar ? "heatmap_summary" : "summary"] as? [String: Any] ?? [:]
        let money = snapshot["money"] as? [String: Any] ?? [:], currency = (money["prefs"] as? [String: Any])?["currency"] as? String ?? "CNY"
        heatmapTitle.stringValue = calendar ? "每日用量 · \(heatmapWeeks) 周" : heatmap.trendGranularity == "hour" ? "按小时趋势" : "按日趋势"
        var span = heatmap.rangeDescription
        if !calendar, let points = snapshot["trend_points"] as? [[String: Any]], let first = points.first?["timestamp"] as? NSNumber, let last = points.last?["timestamp"] as? NSNumber {
            let date = DateFormatter(); date.dateFormat = "yyyy.MM.dd"
            span = date.string(from: Date(timeIntervalSince1970: first.doubleValue)) + " — " + date.string(from: Date(timeIntervalSince1970: last.doubleValue))
        }
        heatmapRange.stringValue = span
        heatmapSummary.stringValue = summary.isEmpty ? "区间汇总暂未取得" : "\(Self.compact(number(summary, "total_tokens"))) Tokens · \(Self.compact(number(summary, "requests"))) 次 · \(CueUsageMoney.display(summary, currency: currency))"
        heatmapSummary.toolTip = (calendar ? "热力图区间合计" : "当前筛选趋势区间合计") + "\n" + span + "\n\(Self.integer(number(summary, "total_tokens"))) Tokens · \(Self.integer(number(summary, "requests"))) 条记录\n" + CueUsageMoney.display(summary, currency: currency) + (number(summary, "unpriced_requests") > 0 ? "\n含未定价记录，费用仅为已定价小计。" : "")
    }
    @objc private func changeHeatmapMetric() { heatmap.metric = selected(heatmapMetric); if useSavedColumns { UserDefaults.standard.set(heatmap.metric, forKey: "VibeCueUsageChartMetric") } }
    @objc private func changeChartStyle() { heatmap.chartStyle = selected(chartStyle); heatmapScroll.contentView.scroll(to: .zero); if useSavedColumns { UserDefaults.standard.set(heatmap.chartStyle, forKey: "VibeCueUsageChartStyle") }; layout() }
    private func selectHeatmapMetric(_ value: String) { select(heatmapMetric, value); changeHeatmapMetric() }
    @objc private func showSummary() {
        let summary = snapshot["summary"] as? [String: Any] ?? [:], money = snapshot["money"] as? [String: Any] ?? [:]
        let currency = (money["prefs"] as? [String: Any])?["currency"] as? String ?? "CNY"
        let alert = NSAlert(); alert.messageText = "当前筛选的用量摘要"
        var lines = ["总 Tokens：" + Self.integer(number(summary, "total_tokens")), "新增输入：" + Self.integer(number(summary, "input_tokens")), "输出：" + Self.integer(number(summary, "output_tokens")), "缓存读取：" + Self.integer(number(summary, "cache_read_tokens")), "缓存写入：" + Self.integer(number(summary, "cache_creation_tokens")), String(format: "缓存命中率：%.1f%%", number(summary, "cache_hit_rate") * 100), "请求记录：" + Self.integer(number(summary, "requests")), "API 参考费用：" + CueUsageMoney.display(summary, currency: currency), "已定价 / 未定价：\(Self.integer(number(summary, "priced_requests"))) / \(Self.integer(number(summary, "unpriced_requests")))"]
        if let apps = snapshot["summary_by_app"] as? [[String: Any]], !apps.isEmpty { lines += ["", "按应用"] + apps.map { "\(appName($0["app"] as? String ?? ""))：\(Self.integer(number($0, "requests"))) 次 · \(Self.compact(number($0, "total_tokens"))) Tokens · \(CueUsageMoney.display($0, currency: currency))" } }
        lines += ["", "缓存命中率以输入与缓存总量为分母，不能相加不同应用的百分比。费用仅按当前基础单价估算，未定价部分不当作零。"]
        alert.informativeText = lines.joined(separator: "\n"); alert.addButton(withTitle: "知道了"); if let window { alert.beginSheetModal(for: window) }
    }
    @objc private func changeTab() { page = 1; rememberChoices(); configureColumns(); query(invalidate: true) }
    @objc private func previousPage() { guard page > 1 else { return }; page -= 1; query(invalidate: true, tableOnly: true) }
    @objc private func nextPage() { guard page < pages else { return }; page += 1; query(invalidate: true, tableOnly: true) }
    @objc private func showMoneyPreferences() {
        if preferencesController == nil {
            let filters = snapshot["filters"] as? [String: Any] ?? [:]
            let known = (filters["models"] as? [[String: Any]] ?? []).compactMap { $0["id"] as? String }
            preferencesController = CueUsagePreferencesController(home: home, knownModels: known) { [weak self] in self?.query(invalidate: true); self?.usageDidChange?() }
        }
        preferencesController?.showWindow(nil)
    }
    @objc func showAccounts() {
        if accountController == nil { accountController = CueCCAccountsController(home: home) }
        accountController?.showWindow(nil)
    }
    private func readPricingSchedule(_ data: [String: Any]) {
        guard data.keys.contains("pricing_next_refresh_at") || data["pricing_refresh_due"] is Bool else { return }
        pricingScheduleKnown = true
        if let raw = data["pricing_next_refresh_at"] as? NSNumber, raw.doubleValue.isFinite, raw.doubleValue > 0 {
            pricingNextRefreshAt = Date(timeIntervalSince1970: raw.doubleValue)
        } else if data["pricing_refresh_due"] as? Bool == true { pricingNextRefreshAt = Date() }
        else { pricingNextRefreshAt = nil }
        configurePricingTimer()
    }
    private func configurePricingTimer() {
        guard backgroundStarted, !stopped, !pricingRefreshing, !(pricingScheduleKnown && pricingNextRefreshAt == nil) else {
            pricingTimer?.invalidate(); pricingTimer = nil; return
        }
        let due = max(max(Date().addingTimeInterval(0.2), pricingNextRefreshAt ?? Date().addingTimeInterval(900)), pricingRetryAfter ?? .distantPast)
        // A display query can arrive every five seconds. Keep an already due
        // refresh rather than pushing its deadline forward on every query.
        if let pricingTimer, pricingTimer.isValid, pricingTimer.fireDate <= due { return }
        pricingTimer?.invalidate(); pricingTimer = nil
        let timer = Timer(fire: due, interval: 0, repeats: false) { [weak self] _ in self?.refreshPricing() }
        RunLoop.main.add(timer, forMode: .common); pricingTimer = timer
    }
    private func refreshPricing() {
        guard !stopped, !pricingRefreshing else { return }
        pricingTimer?.invalidate(); pricingTimer = nil; pricingRefreshing = true
        client.run(["refresh-pricing"], module: "vibecue.usage_money") { [weak self] result in
            guard let self, !self.stopped else { return }; self.pricingRefreshing = false
            switch result {
            case .success(let data): self.pricingRetryAfter = nil; self.pricingNextRefreshAt = nil; self.readPricingSchedule(data)
            case .failure:
                self.pricingScheduleKnown = true; self.pricingRetryAfter = Date().addingTimeInterval(900)
                self.pricingNextRefreshAt = self.pricingRetryAfter; self.configurePricingTimer()
            }
            self.query(); self.usageDidChange?()
        }
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
        var text = "读取 Codex、Claude Code 与 OpenCode 已保留的本地用量记录，不修改源数据。OpenCode 支持本机 SQLite 与历史消息文件，只读取用量和模型字段。不同模型按日志字段区分；没有模型信息时保留 unknown。CC Switch 仅读取请求数值白名单，补充可唯一匹配的耗时、状态码及其他工具已有用量；不读取正文或授权。原生三种工具不重复导入 CC 计数，其他工具归档日汇总只计入汇总，不伪造请求明细。\n\nTokens 总量 = 新增输入 + 输出 + 缓存读取 + 缓存创建；OpenCode 单独记录的 reasoning 归入输出。OpenCode 来源显示日志中的 provider ID，其他未记录供应商时显示会话日志；这不是供应商账单或订阅扣费。删除的日志、没有 usage 的记录无法还原。\n\n" + details.joined(separator: "\n")
        if !warnings.isEmpty {
            text += "\n\n导入提示：\n" + warnings.prefix(8).joined(separator: "\n")
            text += "\n暂缓文件：\(Int(number(sync, "deferred_files")))；损坏记录：\(Int(number(sync, "malformed_records")))；超过读取上限：\(Int(number(sync, "oversized_records")))。"
        }
        text += "\n\nAPI 参考费用按当前自定义来源优先队列的模型基础单价重新估算：新增输入 × 输入单价 + 输出 × 输出单价 + 缓存读取 × 读取单价 + 缓存创建 × 写入单价。四项单价均为美元／百万 Tokens，不重复计算缓存。未知模型保持未定价，汇总仅包含已定价小计；日志未记录的长上下文、服务档位及协议优惠差价不自动推断。此金额不能换算为订阅每周额度。\n\n人民币可使用自定义汇率或 Frankfurter 公开日参考汇率；自动每 6 小时检查，失败时保留带日期的缓存并标记过期。"
        alert.informativeText = text; alert.addButton(withTitle: "知道了")
        if let window { alert.beginSheetModal(for: window) } else { alert.runModal() }
    }

    private static let logColumns: [(String, String, CGFloat, Bool)] = [("timestamp", "时间", 132, true), ("app", "应用", 82, true), ("provider_name", "供应商", 110, true), ("source", "来源", 118, false), ("model", "模型", 145, true), ("cost", "API 参考费用", 132, true), ("input_tokens", "新增输入", 80, true), ("output_tokens", "输出", 74, true), ("cache_read_tokens", "缓存读取", 90, true), ("cache_creation_tokens", "缓存写入", 96, false), ("cache_hit_rate", "缓存命中率", 96, false), ("speed_tps", "速度 ⓘ", 88, true), ("first_token_ms", "首字 ⓘ", 82, true), ("duration_ms", "总耗时", 96, false), ("status_code", "状态码", 80, false), ("total_tokens", "总 Tokens", 100, false)]
    @objc private func showColumns() {
        let menu = NSMenu()
        for (key, title, _, visible) in Self.logColumns {
            let item = NSMenuItem(title: title.replacingOccurrences(of: " ⓘ", with: ""), action: #selector(toggleColumn(_:)), keyEquivalent: "")
            item.target = self; item.representedObject = key; item.state = (columnOverrides[key] ?? visible) ? .on : .off; item.isEnabled = key != "timestamp"; menu.addItem(item)
        }
        menu.addItem(.separator()); let reset = NSMenuItem(title: "恢复默认列", action: #selector(resetColumns), keyEquivalent: ""); reset.target = self; menu.addItem(reset)
        let export = NSMenuItem(title: "导出当前页 CSV…", action: #selector(exportPage), keyEquivalent: ""); export.target = self; menu.addItem(export)
        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: columnsButton.bounds.maxY + 4), in: columnsButton)
    }
    @objc private func toggleColumn(_ sender: NSMenuItem) { guard let key = sender.representedObject as? String, key != "timestamp", let definition = Self.logColumns.first(where: { $0.0 == key }) else { return }; columnOverrides[key] = !(columnOverrides[key] ?? definition.3); saveColumns() }
    @objc private func resetColumns() { columnOverrides = [:]; saveColumns() }
    private func saveColumns() { if useSavedColumns { UserDefaults.standard.set(columnOverrides, forKey: "VibeCueUsageLogColumns") }; configureColumns(); table.reloadData(); layout() }
    @objc private func showDetails() {
        let index = table.selectedRow >= 0 ? table.selectedRow : table.clickedRow
        guard rows.indices.contains(index) else { return }
        let money = snapshot["money"] as? [String: Any] ?? [:], currency = (money["prefs"] as? [String: Any])?["currency"] as? String ?? "CNY"
        detailController = CueUsageLogDetailsController(row: rows[index], currency: currency, title: currentView == "logs" ? "请求详情" : "分组用量详情"); detailController?.showWindow(nil)
    }
    func menuNeedsUpdate(_ menu: NSMenu) { if table.clickedRow >= 0 { table.selectRowIndexes(IndexSet(integer: table.clickedRow), byExtendingSelection: false) } }
    private func rowTSV(_ row: [String: Any]) -> String { table.tableColumns.map { display(row, key: $0.identifier.rawValue).replacingOccurrences(of: "\t", with: " ").replacingOccurrences(of: "\n", with: " ") }.joined(separator: "\t") }
    @objc private func copyRow() { let index = table.selectedRow >= 0 ? table.selectedRow : table.clickedRow; guard rows.indices.contains(index) else { return }; NSPasteboard.general.clearContents(); NSPasteboard.general.setString(rowTSV(rows[index]), forType: .string) }
    @objc private func exportPage() {
        let panel = NSSavePanel(); panel.nameFieldStringValue = "VibeCoding Go-\(currentView)-page-\(page).csv"; panel.canCreateDirectories = true
        guard let window else { return }
        panel.beginSheetModal(for: window) { [weak self] response in
            guard response == .OK, let self, let url = panel.url else { return }
            func quote(_ value: String) -> String { let safe = value.first.map { "=+-@".contains($0) } == true ? "'" + value : value; return "\"" + safe.replacingOccurrences(of: "\"", with: "\"\"") + "\"" }
            let header = self.table.tableColumns.map { quote($0.title) }.joined(separator: ",")
            let lines = self.rows.map { row in self.table.tableColumns.map { quote(self.display(row, key: $0.identifier.rawValue)) }.joined(separator: ",") }
            do { try (([header] + lines).joined(separator: "\r\n") + "\r\n").write(to: url, atomically: true, encoding: .utf8) }
            catch { self.detail.stringValue = "当前页导出未完成，请检查目标文件夹权限。" }
        }
    }

    private func configureColumns() {
        for column in table.tableColumns { table.removeTableColumn(column) }
        var columns: [(String, String, CGFloat)]
        if currentView == "logs" { columns = Self.logColumns.filter { $0.0 == "timestamp" || (columnOverrides[$0.0] ?? $0.3) }.map { ($0.0, $0.1, $0.2) } }
        else if currentView == "models" { columns = [("model", "模型", 225), ("requests", "记录数", 95)] }
        else if currentView == "providers" { columns = [("provider_name", "供应商", 225), ("app", "应用", 105), ("requests", "记录数", 95)] }
        else { columns = [("app", "应用", 110), ("source", "来源", 210), ("requests", "记录数", 95)] }
        if currentView != "logs" { columns += [("cost", "API 参考费用", 150), ("input_tokens", "新增输入", 91), ("output_tokens", "输出", 86), ("cache_read_tokens", "缓存读取", 96), ("cache_creation_tokens", "缓存写入", 96), ("cache_hit_rate", "缓存命中率", 96), ("total_tokens", "总 Tokens", 100)] }
        for (key, title, width) in columns {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(key)); column.title = title
            column.width = width; column.minWidth = min(70, width); column.resizingMask = .userResizingMask
            switch key {
            case "speed_tps": column.headerToolTip = "输出速度。代理记录按输出 Tokens ÷（总耗时 − 首字耗时）；会话时序估算带 ≈，缺少可靠时序时显示 —。"
            case "first_token_ms": column.headerToolTip = "从请求开始到首个输出 Token 的耗时。≈ 表示基于会话时序估算；未记录时显示 —。"
            case "duration_ms": column.headerToolTip = "请求总耗时。≈ 表示基于会话时序估算；未记录时显示 —。"
            case "cache_hit_rate": column.headerToolTip = "缓存读取 Tokens ÷（新增输入 + 缓存读取 + 缓存写入）。输出不计入分母。"
            default: break
            }
            if key.hasSuffix("tokens") || key == "requests" { column.headerCell.alignment = .right }
            table.addTableColumn(column)
        }
        table.setAccessibilityLabel(currentView == "logs" ? "历史用量请求日志" : currentView == "models" ? "按模型汇总用量" : currentView == "providers" ? "按供应商汇总用量" : "按来源汇总用量")
    }
    private func display(_ row: [String: Any], key: String) -> String {
        if key == "cost" {
            let money = snapshot["money"] as? [String: Any] ?? [:], currency = (money["prefs"] as? [String: Any])?["currency"] as? String ?? "CNY"
            return CueUsageMoney.display(row, currency: currency) + (number(row, "unpriced_requests") > 0 && number(row, "priced_requests") > 0 ? " *" : "")
        }
        if key.hasSuffix("tokens") || key == "requests" { return (row[key] as? NSNumber).map { Self.integer($0.doubleValue) } ?? "—" }
        if ["speed_tps", "first_token_ms", "duration_ms"].contains(key) { return CueUsageLogDetailsController.performance(row, key: key) }
        if key == "status_code" { return (row[key] as? NSNumber)?.stringValue ?? "—" }
        if key == "cache_hit_rate" {
            let denominator = number(row, "input_tokens") + number(row, "cache_read_tokens") + number(row, "cache_creation_tokens")
            guard denominator > 0 else { return "—" }; return String(format: "%.1f%%", max(0, min(1, number(row, "cache_read_tokens") / denominator)) * 100)
        }
        if key == "timestamp" { let date = DateFormatter(); date.dateFormat = "yyyy-MM-dd HH:mm:ss"; return (row[key] as? NSNumber).map { date.string(from: Date(timeIntervalSince1970: $0.doubleValue)) } ?? "—" }
        if key == "app" { return appName(row[key] as? String ?? "") }
        if key == "source" { return sourceName(row[key] as? String ?? "") }
        if key == "provider_name" { return (row["provider_name"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "会话日志" }
        return row[key] as? String ?? "—"
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
        field.stringValue = display(data, key: key)
        let numeric = key.hasSuffix("tokens") || ["requests", "cost", "cache_hit_rate", "speed_tps", "first_token_ms", "duration_ms", "status_code"].contains(key)
        field.alignment = numeric ? .right : .left; field.font = numeric ? .monospacedDigitSystemFont(ofSize: 11.5, weight: .regular) : .systemFont(ofSize: 11.5)
        field.toolTip = key == "provider_name" && data["provider_name"] as? String == nil ? "原始会话没有记录供应商，不能根据模型猜测。" : ["speed_tps", "first_token_ms", "duration_ms"].contains(key) ? field.stringValue + "\n" + (data["timing_estimated"] as? Bool == true ? "≈ 基于会话时序估算，包含日志间隔。" : "原始数值字段；未记录时显示 —。") : field.stringValue
        field.textColor = .labelColor
        if key == "status_code", number(data, key) >= 400 { field.textColor = .systemOrange }
        return cell
    }
    func tableViewSelectionDidChange(_ notification: Notification) {
        detailsButton.isEnabled = rows.indices.contains(table.selectedRow)
        guard rows.indices.contains(table.selectedRow) else { return }
        let row = rows[table.selectedRow]
        if let thread = row["thread_id"] as? String, !thread.isEmpty { detail.stringValue = "会话 \(thread) · \(Self.integer(number(row, "total_tokens"))) Tokens" }
    }

    private func selected(_ popup: NSPopUpButton) -> String { popup.selectedItem?.representedObject as? String ?? "all" }
    private func select(_ popup: NSPopUpButton, _ value: String) { if let item = popup.itemArray.first(where: { $0.representedObject as? String == value }) { popup.select(item) } }
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
    private func appName(_ id: String) -> String { let filters = snapshot["filters"] as? [String: Any] ?? [:]; return (filters["apps"] as? [[String: Any]])?.first(where: { $0["id"] as? String == id })?["name"] as? String ?? ["codex": "Codex", "claude": "Claude Code", "opencode": "OpenCode", "gemini": "Gemini CLI", "grok": "Grok Build", "openclaw": "OpenClaw", "hermes": "Hermes", "pi": "Pi", "mcode": "MiniMax Code", "kimi": "Kimi Code", "agy": "Antigravity", "omp": "Oh My Pi"][id] ?? id }
    private func sourceName(_ id: String) -> String {
        if let filters = snapshot["filters"] as? [String: Any], let sources = filters["sources"] as? [[String: Any]], let name = sources.first(where: { $0["id"] as? String == id })?["name"] as? String { return name }
        if id.hasPrefix("opencode_provider:") { return "OpenCode · " + String(id.dropFirst("opencode_provider:".count)) }
        return ["codex_session": "Codex 会话日志", "claude_session": "Claude 会话日志", "opencode_session": "OpenCode 会话日志"][id] ?? id
    }
    static func integer(_ value: Double) -> String {
        let formatter = NumberFormatter(); formatter.numberStyle = .decimal; formatter.maximumFractionDigits = 0
        return formatter.string(from: NSNumber(value: value)) ?? "0"
    }
    static func compact(_ value: Double) -> String {
        for (suffix, divisor) in [("B", 1_000_000_000.0), ("M", 1_000_000.0), ("K", 1000.0)] {
            if value >= divisor { return String(format: value / divisor >= 100 ? "%.0f%@" : "%.1f%@", value / divisor, suffix) }
        }
        return integer(value)
    }
}
