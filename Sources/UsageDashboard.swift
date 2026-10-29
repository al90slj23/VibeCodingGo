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
                for key in ["HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "OPENCODE_DB", "VIBECUE_CC_SWITCH_QUOTA_FILE"] {
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

private final class CueUsageHeatmap: NSView {
    struct MonthLabel { let value: String; let frame: NSRect }
    struct Geometry {
        let grid: NSRect
        let cell: CGFloat
        let gap: CGFloat
        let monthLabels: [MonthLabel]
        var stride: CGFloat { cell + gap }
    }
    var days: [[String: Any]] = [] { didSet { needsDisplay = true } }
    var currency = "CNY" { didSet { needsDisplay = true } }
    var referenceDate: Date?
    private var tooltips: [Int: String] = [:]
    override var isFlipped: Bool { true }
    static func cellSize(width: CGFloat) -> CGFloat { min(22, max(9, (width - 56 - 52 * 3) / 53)) }
    static func preferredHeight(width: CGFloat) -> CGFloat { ceil(22 + cellSize(width: width) * 7 + 6 * 3 + 30) }
    private func range() -> (Calendar, Date, Date) {
        let calendar = Calendar.current
        let today = calendar.startOfDay(for: referenceDate ?? Date())
        let weekday = (calendar.component(.weekday, from: today) + 5) % 7
        let monday = calendar.date(byAdding: .day, value: -weekday, to: today)!
        return (calendar, calendar.date(byAdding: .day, value: -52 * 7, to: monday)!, today)
    }
    var rangeDescription: String {
        let (_, start, end) = range()
        let formatter = DateFormatter(); formatter.locale = Locale(identifier: "en_US_POSIX"); formatter.dateFormat = "yyyy.MM.dd"
        return formatter.string(from: start) + " — " + formatter.string(from: end)
    }
    func geometry(width: CGFloat) -> Geometry {
        let (calendar, start, _) = range(), gap: CGFloat = 3
        let cell = Self.cellSize(width: width), gridWidth = 53 * cell + 52 * gap
        let grid = NSRect(x: 40 + max(0, (width - 56 - gridWidth) / 2), y: 22, width: gridWidth, height: 7 * cell + 6 * gap)
        let attributes: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 9.5), .foregroundColor: NSColor.secondaryLabelColor]
        var candidates: [MonthLabel] = [], priorMonth = -1
        for week in 0..<53 {
            let date = calendar.date(byAdding: .day, value: week * 7, to: start)!
            let month = calendar.component(.month, from: date)
            guard month != priorMonth else { continue }
            priorMonth = month
            let text = month == 1 ? "\(calendar.component(.year, from: date))年1月" : "\(month)月"
            let labelWidth = ceil((text as NSString).size(withAttributes: attributes).width)
            let x = min(grid.maxX - labelWidth, grid.minX + CGFloat(week) * (cell + gap))
            candidates.append(MonthLabel(value: text, frame: NSRect(x: x, y: 3, width: labelWidth, height: 15)))
        }
        var accepted: [MonthLabel] = []
        for (index, label) in candidates.enumerated() {
            // A one-week partial first month should not collide with the first
            // full month. Keep the full month label instead of squeezing text.
            if index == 0, candidates.count > 1, label.frame.maxX + 8 > candidates[1].frame.minX { continue }
            if let previous = accepted.last, previous.frame.maxX + 8 > label.frame.minX { continue }
            accepted.append(label)
        }
        return Geometry(grid: grid, cell: cell, gap: gap, monthLabels: accepted)
    }
    override func draw(_ dirtyRect: NSRect) {
        removeAllToolTips(); tooltips = [:]
        NSColor.controlBackgroundColor.withAlphaComponent(0.45).setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 10, yRadius: 10).fill()
        let (calendar, start, today) = range(), layout = geometry(width: bounds.width)
        let formatter = DateFormatter(); formatter.locale = Locale(identifier: "en_US_POSIX"); formatter.dateFormat = "yyyy-MM-dd"
        let values = Dictionary(days.compactMap { row -> (String, [String: Any])? in guard let date = row["date"] as? String else { return nil }; return (date, row) }, uniquingKeysWith: { _, new in new })
        let firstKey = formatter.string(from: start), lastKey = formatter.string(from: today)
        let maximum = max(1, values.filter { $0.key >= firstKey && $0.key <= lastKey }.values.map { max(0, ($0["tokens"] as? NSNumber)?.doubleValue ?? 0) }.filter(\.isFinite).max() ?? 1)
        let labelAttributes: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 9.5), .foregroundColor: NSColor.secondaryLabelColor]
        for label in layout.monthLabels { (label.value as NSString).draw(in: label.frame, withAttributes: labelAttributes) }
        for (row, title) in [(0, "周一"), (2, "周三"), (4, "周五"), (6, "周日")] {
            let paragraph = NSMutableParagraphStyle(); paragraph.alignment = .right
            (title as NSString).draw(in: NSRect(x: layout.grid.minX - 31, y: layout.grid.minY + CGFloat(row) * layout.stride + (layout.cell - 13) / 2, width: 24, height: 15), withAttributes: [.font: NSFont.systemFont(ofSize: 9), .foregroundColor: NSColor.secondaryLabelColor, .paragraphStyle: paragraph])
        }
        for week in 0..<53 {
            for day in 0..<7 {
                let date = calendar.date(byAdding: .day, value: week * 7 + day, to: start)!
                let rect = NSRect(x: layout.grid.minX + CGFloat(week) * layout.stride, y: layout.grid.minY + CGFloat(day) * layout.stride, width: layout.cell, height: layout.cell)
                let future = date > today
                let key = formatter.string(from: date), row = values[key] ?? [:]
                let rawCount = (row["tokens"] as? NSNumber)?.doubleValue ?? 0
                let count = rawCount.isFinite ? max(0, rawCount) : 0
                let requests = max(0, (row["requests"] as? NSNumber)?.int64Value ?? 0)
                let level = count <= 0 ? 0 : min(1, max(0.15, sqrt(count / maximum)))
                let color = future ? NSColor.quaternaryLabelColor.withAlphaComponent(0.035) : count <= 0 ? NSColor.quaternaryLabelColor.withAlphaComponent(0.16) : NSColor.controlAccentColor.withAlphaComponent(CGFloat(0.22 + level * 0.74))
                color.setFill(); NSBezierPath(roundedRect: rect, xRadius: 2.5, yRadius: 2.5).fill()
                guard !future else { continue }
                let tag = addToolTip(rect, owner: self, userData: nil)
                var message = "\(key)\n\(CueUsageDashboardController.integer(count)) Tokens · \(CueUsageDashboardController.integer(Double(requests))) 条用量记录"
                if row["cost_usd"] is NSNumber || row["cost_cny"] is NSNumber { message += "\nAPI 参考费用：" + CueUsageMoney.display(row, currency: currency) }
                tooltips[tag] = message
            }
        }
        let legendY = layout.grid.maxY + 12
        ("颜色越深，用量越高" as NSString).draw(at: NSPoint(x: layout.grid.minX, y: legendY), withAttributes: labelAttributes)
        let legendX = layout.grid.maxX - 113
        ("少" as NSString).draw(at: NSPoint(x: legendX, y: legendY), withAttributes: labelAttributes)
        for index in 0..<5 {
            let rect = NSRect(x: legendX + 19 + CGFloat(index) * 14, y: legendY + 1, width: 10, height: 10)
            (index == 0 ? NSColor.quaternaryLabelColor.withAlphaComponent(0.16) : NSColor.controlAccentColor.withAlphaComponent(CGFloat(0.22 + Double(index) / 4 * 0.74))).setFill()
            NSBezierPath(roundedRect: rect, xRadius: 2, yRadius: 2).fill()
        }
        ("多" as NSString).draw(at: NSPoint(x: legendX + 97, y: legendY), withAttributes: labelAttributes)
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
    private let moneyButton = NSButton(title: "统计设置…", target: nil, action: nil)
    private let accountsButton = NSButton(title: "Codex 账号额度…", target: nil, action: nil)
    private let syncLabel = NSTextField(labelWithString: "尚未同步")
    private let status = NSTextField(wrappingLabelWithString: "")
    private let metrics = [CueUsageMetric(title: "Tokens 总量"), CueUsageMetric(title: "新增输入"), CueUsageMetric(title: "输出"), CueUsageMetric(title: "缓存命中"), CueUsageMetric(title: "用量记录"), CueUsageMetric(title: "API 参考费用")]
    private let heatmapTitle = NSTextField(labelWithString: "最近 53 周")
    private let heatmapRange = NSTextField(labelWithString: "")
    private let heatmap = CueUsageHeatmap()
    private let tabs = NSSegmentedControl(labels: ["请求日志", "模型", "来源"], trackingMode: .selectOne, target: nil, action: nil)
    private let table = NSTableView(), scroll = CueUsageScrollView()
    private let listSection = CueUsageListSection(), paginationBar = CueUsagePaginationBar()
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
    private var currentView: String { ["logs", "models", "sources"][max(0, min(2, tabs.selectedSegment))] }

    init(home: URL) {
        self.home = home; client = CueUsageClient(home: home); schedule = CueUsageSchedulePreferences(home: home)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1120, height: 850), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "VibeCue · 用量统计"; window.isReleasedWhenClosed = false
        window.contentMinSize = NSSize(width: 1000, height: 760); window.center()
        super.init(window: window); window.delegate = self; window.contentView = surface
        titleLabel.font = .systemFont(ofSize: 23, weight: .semibold)
        subtitle.font = .systemFont(ofSize: 12); subtitle.textColor = .secondaryLabelColor
        syncLabel.font = .systemFont(ofSize: 11); syncLabel.textColor = .secondaryLabelColor; syncLabel.alignment = .right
        status.font = .systemFont(ofSize: 11); status.maximumNumberOfLines = 2
        heatmapTitle.font = .systemFont(ofSize: 12, weight: .medium); heatmapTitle.textColor = .secondaryLabelColor
        heatmapRange.font = .monospacedDigitSystemFont(ofSize: 10.5, weight: .regular); heatmapRange.textColor = .secondaryLabelColor; heatmapRange.alignment = .right
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
        table.usesAlternatingRowBackgroundColors = true; table.style = .plain
        table.allowsMultipleSelection = false; table.columnAutoresizingStyle = .noColumnAutoresizing
        table.intercellSpacing = NSSize(width: 4, height: 2)
        table.setAccessibilityLabel("历史用量请求日志")
        scroll.documentView = table; scroll.hasVerticalScroller = true; scroll.hasHorizontalScroller = true
        // Reserved native scroller tracks keep the final row and final header
        // visible; overlay scrollers previously covered the numbers themselves.
        scroll.scrollerStyle = .legacy; scroll.autohidesScrollers = false; scroll.borderType = .noBorder
        scroll.automaticallyAdjustsContentInsets = false; scroll.contentInsets = NSEdgeInsetsZero
        listSection.setAccessibilityElement(true); listSection.setAccessibilityRole(.group)
        paginationBar.setAccessibilityElement(true); paginationBar.setAccessibilityRole(.group)
        paginationBar.setAccessibilityLabel("当前列表分页")
        listSection.addSubview(scroll); listSection.addSubview(paginationBar)
        [pageLabel, previous, next].forEach { paginationBar.addSubview($0) }
        let views: [NSView] = [titleLabel, subtitle, range, app, model, source, syncButton, infoButton, moneyButton, accountsButton, syncLabel, status, heatmapTitle, heatmapRange, heatmap, tabs, listSection, detail] + metrics
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
        backgroundTimer?.invalidate(); backgroundTimer = nil; visibleTimer?.invalidate(); visibleTimer = nil
        pricingTimer?.invalidate(); pricingTimer = nil; client.stop()
    }
    func windowWillClose(_ notification: Notification) { visibleTimer?.invalidate(); visibleTimer = nil }

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
    static func captureFixture(to url: URL, dark: Bool, size: NSSize = NSSize(width: 1120, height: 850), currency: String = "CNY") throws {
        let controller = fixtureController(size: size, currency: currency, dark: dark)
        defer { controller.window?.orderOut(nil) }
        try controller.capture(snapshot: controller.snapshot, to: url, dark: dark)
    }

    static func validationReport() -> [String: Bool] {
        var report = CueUsageScheduleController.validationReport()
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
                report[prefix + ".sectionSpacing"] = controller.metrics.allSatisfy { $0.frame.maxY + 12 <= controller.heatmapTitle.frame.minY } && controller.heatmap.frame.maxY + 12 <= controller.tabs.frame.minY && controller.tabs.frame.maxY + 10 <= controller.listSection.frame.minY && controller.listSection.frame.maxY + 8 <= controller.detail.frame.minY
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
                report[prefix + ".tableViewport"] = controller.scroll.contentSize.height >= 115 && controller.table.frame.height >= CGFloat(controller.rows.count) * (controller.table.rowHeight + controller.table.intercellSpacing.height)
                if let cost = controller.table.tableColumns.first(where: { $0.identifier.rawValue == "cost" }) {
                    report[prefix + ".tableCurrencyFits"] = controller.rows.allSatisfy { (CueUsageMoney.display($0, currency: currency) as NSString).size(withAttributes: [.font: NSFont.monospacedDigitSystemFont(ofSize: 11.5, weight: .regular)]).width + 8 <= cost.width }
                }
            }
        }
        let paging = fixtureController(size: NSSize(width: 1120, height: 850), currency: "CNY", dark: true)
        let overview = paging.metrics.map { $0.value.stringValue }, calendarDays = paging.heatmap.days.count, message = paging.status.stringValue
        paging.queryInFlight = true // Exercise invalidation without starting a worker.
        paging.nextPage()
        report["usage_pagination_changes_only_list_loading"] = paging.page == 2 && paging.rows.isEmpty && paging.pageLabel.stringValue.contains("第 2 页") && paging.metrics.map { $0.value.stringValue } == overview && paging.heatmap.days.count == calendarDays && paging.status.stringValue == message
        return report
    }

    private static func fixtureController(size: NSSize, currency: String, dark: Bool) -> CueUsageDashboardController {
        let controller = CueUsageDashboardController(home: FileManager.default.temporaryDirectory.appendingPathComponent("vibecue-usage-fixture-\(UUID().uuidString)", isDirectory: true))
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
            return ["date": formatter.string(from: date), "tokens": index < 80 ? (index % 17 == 0 ? 200_000 : 0) : (1_000_000 + (index * 2_321_123) % 800_000_000), "requests": (index * 37) % 4500]
        }
        var logs: [[String: Any]] = []
        for index in 0..<50 {
            var row: [String: Any] = ["app": "codex", "source": "codex_session", "thread_id": "offline-fixture", "priced_requests": 1, "unpriced_requests": 0]
            row["timestamp"] = reference.timeIntervalSince1970 - Double(index * 13)
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
            "pagination": ["page": 1, "pages": 15453, "total": 772639],
            "money": ["prefs": ["currency": currency], "fx": ["rate": 7.10364, "date": "2026-10-29", "stale": false]],
            "sync": ["last_synced_at": reference.timeIntervalSince1970, "errors": [], "warnings": []]
        ])
        controller.updateStatus(); controller.surface.layoutSubtreeIfNeeded(); controller.scroll.tile()
        return controller
    }

    private func layout() {
        let width = surface.bounds.width, height = surface.bounds.height, right = width - 24
        titleLabel.frame = NSRect(x: 24, y: 23, width: 260, height: 32)
        subtitle.frame = NSRect(x: 25, y: 62, width: max(300, width - 400), height: 20)
        syncButton.frame = NSRect(x: right - 112, y: 23, width: 112, height: 32)
        infoButton.frame = NSRect(x: right - 214, y: 23, width: 96, height: 32)
        moneyButton.frame = NSRect(x: right - 348, y: 23, width: 128, height: 32)
        accountsButton.frame = NSRect(x: right - 505, y: 23, width: 151, height: 32)
        syncLabel.frame = NSRect(x: right - 330, y: 65, width: 330, height: 18)
        let controls = [range, app, model, source]
        let filterWidth = width - 48 - 36
        let sizes: [CGFloat] = [0.16, 0.19, 0.35, 0.30].map { filterWidth * $0 }
        var x: CGFloat = 24
        for (index, popup) in controls.enumerated() { popup.frame = NSRect(x: x, y: 100, width: sizes[index], height: 29); x += sizes[index] + 12 }
        let metricWidth = (width - 48 - 50) / 6.4
        var metricX: CGFloat = 24
        for (index, metric) in metrics.enumerated() {
            let cardWidth = metricWidth * (index == 5 ? 1.4 : 1)
            metric.frame = NSRect(x: metricX, y: 142, width: cardWidth, height: 101)
            metric.needsLayout = true; metric.layoutSubtreeIfNeeded(); metricX += cardWidth + 10
        }
        heatmapTitle.frame = NSRect(x: 25, y: 260, width: max(350, width - 340), height: 19)
        heatmapRange.frame = NSRect(x: width - 285, y: 260, width: 260, height: 19)
        heatmapRange.stringValue = heatmap.rangeDescription
        let calendarHeight = CueUsageHeatmap.preferredHeight(width: width - 48)
        heatmap.frame = NSRect(x: 24, y: 287, width: width - 48, height: calendarHeight)
        let tabsY = heatmap.frame.maxY + 16
        tabs.frame = NSRect(x: 24, y: tabsY, width: 284, height: 29)
        status.frame = NSRect(x: 326, y: tabsY, width: width - 350, height: 29)
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
        pageLabel.frame = NSRect(x: 12, y: 10, width: max(300, pagerWidth - 215), height: 18)
        previous.frame = NSRect(x: pagerWidth - 189, y: 5, width: 85, height: 28)
        next.frame = NSRect(x: pagerWidth - 97, y: 5, width: 85, height: 28)
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
        heatmap.currency = currency
        heatmapTitle.stringValue = "每日用量 · 最近 53 周"
        heatmap.setAccessibilityLabel("最近 53 周每日 Token 用量日历，" + heatmap.rangeDescription)
        heatmapRange.stringValue = heatmap.rangeDescription
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
        let listTitle = ["请求日志", "模型", "来源"][max(0, min(2, tabs.selectedSegment))]
        listSection.setAccessibilityLabel(listTitle + "列表与分页")
        previous.setAccessibilityLabel(listTitle + "上一页"); next.setAccessibilityLabel(listTitle + "下一页")
        previous.toolTip = "查看当前" + listTitle + "列表的上一页"; next.toolTip = "查看当前" + listTitle + "列表的下一页"
        pageLabel.stringValue = "\(listTitle) · 共 \(Self.integer(Double(total))) 条 · 第 \(page) / \(pages) 页 · 每页 50 条"
        previous.isEnabled = page > 1; next.isEnabled = page < pages
        detail.stringValue = rows.isEmpty ? "当前筛选暂无记录。可点“立即同步”，或查看“数据说明”。" : "用量来自本机会话日志；原日志未记录供应商时，来源显示为“会话日志”。"
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

    @objc private func changeFilter(_ sender: NSPopUpButton) { page = 1; query(invalidate: true) }
    @objc private func changeTab() { page = 1; configureColumns(); query(invalidate: true) }
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
