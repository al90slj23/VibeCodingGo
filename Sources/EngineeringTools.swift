import AppKit
import Foundation

private enum CueEngineeringKind: String, CaseIterable {
    case environment, api, logs
    var title: String { switch self { case .environment: return "环境体检"; case .api: return "接口调试"; case .logs: return "日志诊断" } }
    var detail: String { switch self {
        case .environment: return "核对项目声明与本机工具版本；只读体检，按需复用 mise。"
        case .api: return "发送一条 HTTP 请求或执行 Hurl 集合；只有点击运行才发送。"
        case .logs: return "读取所选日志的有限尾部，按文字与级别筛选；按需复用 lnav。"
    } }
    var helpTitle: String { switch self { case .environment: return "mise 使用说明 ↗"; case .api: return "Hurl 使用说明 ↗"; case .logs: return "lnav 使用说明 ↗" } }
    var helpURL: URL { URL(string: self == .environment ? "https://mise.jdx.dev/" : self == .api ? "https://hurl.dev/docs/manual.html" : "https://docs.lnav.org/en/latest/")! }
}

private final class CueEngineeringSurface: NSView {
    var onLayout: (() -> Void)?
    override var isFlipped: Bool { true }
    override func layout() { super.layout(); onLayout?() }
    override func draw(_ dirtyRect: NSRect) { NSColor.windowBackgroundColor.setFill(); dirtyRect.fill() }
}

/// Native scroller visibility changes alter the text viewport after content
/// application. Invalidate geometry once on a real viewport change, not scroll.
private final class CueEngineeringScrollView: NSScrollView {
    var viewportChanged: (() -> Void)?
    private var lastViewport = NSSize.zero
    override func tile() {
        super.tile()
        let next = contentSize
        guard next != lastViewport else { return }
        lastViewport = next; viewportChanged?()
    }
}

/// One native workspace page, no independent window, polling, discovery or
/// background engine until an explicit Run action. Native drafts/results are cached in memory; optional parsers may use private temporary snapshots.
final class CueEngineeringToolsController: NSWindowController, NSWindowDelegate {
    private let home: URL
    private let root = CueEngineeringSurface(frame: NSRect(x: 0, y: 0, width: 960, height: 650))
    private let titleField = NSTextField(labelWithString: "")
    private let detailField = NSTextField(wrappingLabelWithString: "")
    private let stateField = NSTextField(labelWithString: "等待运行")
    private let disclosure = NSTextField(wrappingLabelWithString: "")
    private let footer = NSTextField(labelWithString: "按需运行 · 输入与结果不保存为历史 · 可选择并复制结果")
    private let runButton = NSButton(title: "运行体检", target: nil, action: nil)
    private let stopButton = NSButton(title: "停止", target: nil, action: nil)
    private let copyButton = NSButton(title: "复制结果", target: nil, action: nil)
    private let helpButton = NSButton(title: "", target: nil, action: nil)
    private let spinner = NSProgressIndicator()
    private let projectLabel = NSTextField(labelWithString: "项目文件夹")
    private let project = NSTextField(string: "")
    private let chooseProjectButton = NSButton(title: "选择文件夹…", target: nil, action: nil)
    private let modeLabel = NSTextField(labelWithString: "运行方式")
    private let mode = NSPopUpButton(), method = NSPopUpButton()
    private let timeoutLabel = NSTextField(labelWithString: "超时（秒）")
    private let timeout = NSTextField(string: "15")
    private let urlLabel = NSTextField(labelWithString: "请求地址")
    private let url = NSTextField(string: "")
    private let collection = NSTextField(string: "")
    private let chooseCollectionButton = NSButton(title: "选择 .hurl…", target: nil, action: nil)
    private let headersLabel = NSTextField(labelWithString: "请求头 · 每行名称: 值")
    private let bodyLabel = NSTextField(labelWithString: "请求正文")
    private let headers = NSTextView(), body = NSTextView()
    private let headersScroll = CueEngineeringScrollView(), bodyScroll = CueEngineeringScrollView()
    private let filesLabel = NSTextField(labelWithString: "日志文件 · 最多 5 个")
    private let files = NSTextField(labelWithString: "尚未选择文件")
    private let chooseLogsButton = NSButton(title: "选择日志…", target: nil, action: nil)
    private let queryLabel = NSTextField(labelWithString: "筛选文字 · 按原文匹配")
    private let query = NSTextField(string: "")
    private let level = NSPopUpButton(), limit = NSPopUpButton()
    private let results = NSTextView(), resultScroll = CueEngineeringScrollView()
    private var selected: CueEngineeringKind = .environment
    private var logPaths: [String] = []
    private var client: CueUsageClient?
    private var generation = 0, busy = false, active = false, stopped = false
    private var requestsStarted = 0, resultApplications = 0, reportsPrepared = 0, cancelledCount = 0, clientsCreated = 0
    private struct LayoutState: Equatable {
        let size: NSSize
        let kind: CueEngineeringKind
        let hurl: Bool
        let busy: Bool
        let viewports: [NSSize]
    }
    private var layingOut = false
    private var lastLayoutState: LayoutState?
    private var layoutRequests = 0, layoutPasses = 0, geometryWrites = 0, activationRequests = 0, selectionApplications = 0
    private let reportQueue = DispatchQueue(label: "local.vibecue.engineering-report", qos: .utility)
    private struct Report {
        let document: String, state: String, status: String
    }
    private var reports: [CueEngineeringKind: Report] = [:]
    private var displayedDocument: String?
    private var fixtureRun: ((String, [String: Any], @escaping (Result<[String: Any], Error>) -> Void) -> Void)?
    private static func canPresent(active: Bool, windowVisible: Bool, miniaturized: Bool, occluded: Bool) -> Bool {
        active && windowVisible && !miniaturized && !occluded
    }
    private var presentationVisible: Bool {
        guard let host = root.window else { return false }
        return Self.canPresent(active: active, windowVisible: host.isVisible, miniaturized: host.isMiniaturized,
                               occluded: !host.occlusionState.contains(.visible))
    }
    var performanceSnapshot: [String: Any] { ["requests_started": requestsStarted, "reports_prepared": reportsPrepared,
        "result_applications": resultApplications, "worker_active": busy, "busy": busy, "client_allocated": client != nil,
        "client_created": clientsCreated > 0, "clients_created": clientsCreated, "cancelled": cancelledCount,
        "selected_kind": selected.rawValue, "active": active, "visible": presentationVisible, "scheduled_jobs": 0,
        "layout_requests": layoutRequests, "layout_passes": layoutPasses, "geometry_writes": geometryWrites,
        "activation_requests": activationRequests, "selection_applications": selectionApplications] }

    init(home: URL) {
        self.home = home
        super.init(window: nil)
        titleField.font = .systemFont(ofSize: 21, weight: .semibold)
        detailField.font = .systemFont(ofSize: 12); detailField.textColor = .secondaryLabelColor; detailField.maximumNumberOfLines = 2
        stateField.font = .systemFont(ofSize: 11.5); stateField.textColor = .secondaryLabelColor
        stateField.identifier = NSUserInterfaceItemIdentifier("engineering.status")
        disclosure.font = .systemFont(ofSize: 10.5); disclosure.textColor = .secondaryLabelColor; disclosure.maximumNumberOfLines = 3
        footer.font = .systemFont(ofSize: 10); footer.textColor = .tertiaryLabelColor
        [projectLabel, modeLabel, timeoutLabel, urlLabel, headersLabel, bodyLabel, filesLabel, queryLabel].forEach { $0.font = .systemFont(ofSize: 11, weight: .medium) }
        for field in [project, url, collection, query] { field.font = .monospacedSystemFont(ofSize: 11.5, weight: .regular) }
        project.placeholderString = "选择项目文件夹（只读检查）"; url.placeholderString = "https://example.com 或 http://localhost:3000"
        collection.placeholderString = "选择要执行的 .hurl 请求集合"; query.placeholderString = "可留空；不是正则表达式或 SQL"
        files.lineBreakMode = .byTruncatingMiddle; files.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        files.isSelectable = true; files.textColor = .secondaryLabelColor
        project.identifier = NSUserInterfaceItemIdentifier("engineering.project"); project.setAccessibilityLabel("环境体检项目文件夹")
        url.identifier = NSUserInterfaceItemIdentifier("engineering.api.url"); url.setAccessibilityLabel("HTTP 请求地址")
        collection.identifier = NSUserInterfaceItemIdentifier("engineering.api.collection"); collection.setAccessibilityLabel("Hurl 集合路径")
        query.identifier = NSUserInterfaceItemIdentifier("engineering.logs.query"); query.setAccessibilityLabel("日志原文筛选")
        mode.addItems(withTitles: ["单条 HTTP 请求", "Hurl 请求集合"]); mode.target = self; mode.action = #selector(changeAPIMode)
        mode.identifier = NSUserInterfaceItemIdentifier("engineering.api.mode")
        method.addItems(withTitles: ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
        method.target = self; method.action = #selector(changeMethod)
        method.identifier = NSUserInterfaceItemIdentifier("engineering.api.method")
        timeout.font = .monospacedDigitSystemFont(ofSize: 12, weight: .regular); timeout.alignment = .right
        timeout.setAccessibilityLabel("请求超时秒数，1 到 30")
        level.addItems(withTitles: ["全部级别", "仅错误", "仅警告"])
        level.identifier = NSUserInterfaceItemIdentifier("engineering.logs.level")
        limit.addItems(withTitles: ["最多 200 行", "最多 500 行", "最多 1000 行"])
        limit.identifier = NSUserInterfaceItemIdentifier("engineering.logs.limit")
        configureText(headers, scroll: headersScroll, editable: true, name: "请求头，可复制粘贴")
        configureText(body, scroll: bodyScroll, editable: true, name: "请求正文，可复制粘贴")
        configureText(results, scroll: resultScroll, editable: false, name: "工程工具结果，可选择并复制")
        [headersScroll, bodyScroll, resultScroll].forEach { scroll in
            scroll.viewportChanged = { [weak self] in self?.root.needsLayout = true }
        }
        results.identifier = NSUserInterfaceItemIdentifier("engineering.results")
        results.font = .monospacedSystemFont(ofSize: 11.5, weight: .regular)
        for button in [runButton, stopButton, copyButton, helpButton, chooseProjectButton, chooseCollectionButton, chooseLogsButton] {
            button.bezelStyle = .rounded; button.target = self; button.font = .systemFont(ofSize: 11.5)
        }
        runButton.action = #selector(runSelected); stopButton.action = #selector(cancelSelected); copyButton.action = #selector(copyResult)
        chooseProjectButton.action = #selector(chooseProject); chooseCollectionButton.action = #selector(chooseCollection); chooseLogsButton.action = #selector(chooseLogs)
        helpButton.action = #selector(openHelp)
        runButton.identifier = NSUserInterfaceItemIdentifier("engineering.run"); stopButton.identifier = NSUserInterfaceItemIdentifier("engineering.stop")
        copyButton.identifier = NSUserInterfaceItemIdentifier("engineering.copy")
        spinner.style = .spinning; spinner.controlSize = .small; spinner.isDisplayedWhenStopped = false
        let views: [NSView] = [titleField, detailField, stateField, disclosure, footer, runButton, stopButton, copyButton, helpButton, spinner,
            projectLabel, project, chooseProjectButton, modeLabel, mode, method, timeoutLabel, timeout, urlLabel, url, collection, chooseCollectionButton,
            headersLabel, bodyLabel, headersScroll, bodyScroll, filesLabel, files, chooseLogsButton, queryLabel, query, level, limit, resultScroll]
        views.forEach { root.addSubview($0) }; root.autoresizingMask = [.width, .height]
        root.onLayout = { [weak self] in self?.layout() }
        applySelection(); layout()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    deinit { client?.stop() }
    private func configureText(_ view: NSTextView, scroll: NSScrollView, editable: Bool, name: String) {
        view.isEditable = editable; view.isSelectable = true; view.isRichText = false; view.importsGraphics = false
        view.allowsUndo = editable; view.usesFindPanel = true; view.isAutomaticQuoteSubstitutionEnabled = false; view.isAutomaticDashSubstitutionEnabled = false
        view.font = .monospacedSystemFont(ofSize: 11.5, weight: .regular); view.textColor = .labelColor
        view.backgroundColor = .textBackgroundColor; view.drawsBackground = true; view.textContainerInset = NSSize(width: 12, height: 10)
        view.isVerticallyResizable = true; view.isHorizontallyResizable = false; view.autoresizingMask = [.width]
        view.textContainer?.widthTracksTextView = true; view.textContainer?.containerSize = NSSize(width: 600, height: CGFloat.greatestFiniteMagnitude)
        view.layoutManager?.allowsNonContiguousLayout = true; view.setAccessibilityLabel(name)
        scroll.documentView = view; scroll.hasVerticalScroller = true; scroll.autohidesScrollers = true; scroll.borderType = .bezelBorder
    }
    func embeddedContentView() -> NSView { root }
    func selectKind(_ value: String) {
        guard let next = CueEngineeringKind(rawValue: value), selected != next else { return }
        cancelWork(markCancelled: true); selected = next; applySelection(); layout()
    }
    func activateEmbedded() {
        guard !stopped else { return }; active = true; activationRequests += 1
        // A selected tag may belong to a minimized or fully occluded window.
        // Lifecycle restoration consumes the latest cached report once visible.
        if presentationVisible { applySelection() }
    }
    @discardableResult func deactivateEmbedded() -> Bool { active = false; cancelWork(markCancelled: true); return true }
    func stop() { active = false; stopped = true; cancelWork(markCancelled: true) }
    private func applySelection() {
        selectionApplications += 1
        titleField.stringValue = selected.title; detailField.stringValue = selected.detail; helpButton.title = selected.helpTitle
        let environment = selected == .environment, api = selected == .api, logs = selected == .logs, hurl = mode.indexOfSelectedItem == 1
        [projectLabel, project, chooseProjectButton].forEach { $0.isHidden = !environment }
        [modeLabel, mode, method, timeoutLabel, timeout, urlLabel].forEach { $0.isHidden = !api }
        [url, headersLabel, bodyLabel, headersScroll, bodyScroll].forEach { $0.isHidden = !api || hurl }
        [collection, chooseCollectionButton].forEach { $0.isHidden = !api || !hurl }; method.isHidden = !api || hurl
        [filesLabel, files, chooseLogsButton, queryLabel, query, level, limit].forEach { $0.isHidden = !logs }
        urlLabel.stringValue = hurl ? "请求集合 · 执行文件内全部请求" : "请求地址"
        disclosure.stringValue = environment ? "只读取版本与项目声明，不运行项目脚本、不安装工具。未安装 mise 时使用本机只读检查。" : logs ? "每次只读取所选文件的有限尾部；不会持续跟踪或扫描整个目录。结果会标明截断和引擎，敏感字段会脱敏。" : hurl ? "运行集合会执行文件内的请求，可能修改目标服务数据。不会自动重试；未安装 Hurl 时，单条 HTTP 请求仍可用。" : "核对目标与方法后再运行。请求头和正文临时传给本机执行器，结果脱敏；默认不跟随重定向，不会自动重试。"
        if let report = reports[selected] { showReport(report) }
        else { stateField.stringValue = "等待运行 · 切换页面不会自动检测"; stateField.textColor = .secondaryLabelColor; setDocument("尚未运行\n\n" + selected.detail + "\n\n核对输入后点击右上角运行按钮。可选引擎的实际可用状态会在运行结果中显示。") }
        if busy {
            stateField.stringValue = selected == .api ? "正在执行所选请求 · 不会自动重试" : "正在读取所选输入…"
            stateField.textColor = .secondaryLabelColor
        }
        setBusy(busy); layout()
    }
    private func layout() {
        layoutRequests += 1
        guard !layingOut else { return }
        func state() -> LayoutState {
            LayoutState(size:root.bounds.size,kind:selected,hurl:mode.indexOfSelectedItem == 1,busy:busy,
                        viewports:[headersScroll.contentSize,bodyScroll.contentSize,resultScroll.contentSize])
        }
        guard lastLayoutState != state() else { return }
        layingOut = true; layoutPasses += 1
        defer { lastLayoutState = state(); layingOut = false }
        let w = root.bounds.width, h = root.bounds.height, x: CGFloat = 20, width = max(280, w - 40)
        func frame(_ view: NSView, _ next: NSRect) {
            guard view.frame != next else { return }
            geometryWrites += 1; view.frame = next
        }
        frame(titleField, NSRect(x: x, y: 14, width: max(100, width - 300), height: 29))
        frame(detailField, NSRect(x: x + 1, y: 49, width: width - 2, height: 32))
        frame(runButton, NSRect(x: w - 122, y: 14, width: 102, height: 30))
        frame(stopButton, NSRect(x: w - 190, y: 14, width: 62, height: 30))
        frame(copyButton, NSRect(x: w - 287, y: 14, width: 91, height: 30))
        var resultY: CGFloat = 190
        if selected == .environment {
            frame(projectLabel, NSRect(x: x + 1, y: 87, width: width, height: 18))
            frame(project, NSRect(x: x, y: 110, width: width - 127, height: 28))
            frame(chooseProjectButton, NSRect(x: w - 140, y: 108, width: 120, height: 31))
            frame(disclosure, NSRect(x: x + 1, y: 148, width: width - 2, height: 34))
            resultY = 218
        } else if selected == .logs {
            frame(filesLabel, NSRect(x: x + 1, y: 85, width: width, height: 18))
            frame(files, NSRect(x: x + 1, y: 111, width: width - 132, height: 23))
            frame(chooseLogsButton, NSRect(x: w - 140, y: 107, width: 120, height: 31))
            frame(queryLabel, NSRect(x: x + 1, y: 147, width: width, height: 18))
            frame(query, NSRect(x: x, y: 170, width: max(110, width - 254), height: 28))
            frame(level, NSRect(x: w - 267, y: 169, width: 112, height: 29)); frame(limit, NSRect(x: w - 149, y: 169, width: 129, height: 29))
            frame(disclosure, NSRect(x: x + 1, y: 209, width: width - 2, height: 36))
            resultY = 279
        } else {
            frame(modeLabel, NSRect(x: x + 1, y: 86, width: 104, height: 18))
            frame(mode, NSRect(x: x + 106, y: 80, width: 171, height: 30))
            frame(method, NSRect(x: x + 286, y: 80, width: 108, height: 30))
            frame(timeoutLabel, NSRect(x: w - 167, y: 87, width: 85, height: 18))
            frame(timeout, NSRect(x: w - 76, y: 82, width: 56, height: 27))
            frame(urlLabel, NSRect(x: x + 1, y: 121, width: width, height: 18))
            let hurl = mode.indexOfSelectedItem == 1
            frame(url, NSRect(x: x, y: 145, width: width, height: 28))
            frame(collection, NSRect(x: x, y: 145, width: width - 134, height: 28))
            frame(chooseCollectionButton, NSRect(x: w - 147, y: 143, width: 127, height: 31))
            if hurl { frame(disclosure, NSRect(x: x + 1, y: 183, width: width - 2, height: 37)); resultY = 254 }
            else {
                let gap: CGFloat = 14, half = (width - gap) / 2, inputHeight = max(55, min(106, h - 400))
                frame(headersLabel, NSRect(x: x + 1, y: 184, width: half - 2, height: 18))
                frame(bodyLabel, NSRect(x: x + half + gap + 1, y: 184, width: half - 2, height: 18))
                frame(headersScroll, NSRect(x: x, y: 208, width: half, height: inputHeight))
                frame(bodyScroll, NSRect(x: x + half + gap, y: 208, width: half, height: inputHeight))
                frame(disclosure, NSRect(x: x + 1, y: 218 + inputHeight, width: width - 2, height: 36))
                resultY = 288 + inputHeight
            }
        }
        frame(stateField, NSRect(x: x + (busy ? 23 : 1), y: resultY - 24, width: width - 24, height: 19))
        frame(spinner, NSRect(x: x, y: resultY - 23, width: 16, height: 16))
        frame(resultScroll, NSRect(x: x, y: resultY, width: width, height: max(50, h - resultY - 44)))
        for scroll in [headersScroll, bodyScroll, resultScroll] {
            if let text = scroll.documentView as? NSTextView {
                let minimum = NSSize(width:0,height:scroll.contentSize.height)
                let maximum = NSSize(width:CGFloat.greatestFiniteMagnitude,height:CGFloat.greatestFiniteMagnitude)
                let size = NSSize(width:max(50,scroll.contentSize.width),height:max(text.frame.height,scroll.contentSize.height))
                if text.minSize != minimum { text.minSize = minimum; geometryWrites += 1 }
                if text.maxSize != maximum { text.maxSize = maximum; geometryWrites += 1 }
                if text.frame.size != size { text.setFrameSize(size); geometryWrites += 1 }
            }
        }
        frame(helpButton, NSRect(x: w - 182, y: h - 34, width: 162, height: 27))
        frame(footer, NSRect(x: x + 1, y: h - 29, width: max(100, width - 176), height: 19))
    }
    private func setBusy(_ value: Bool) {
        busy = value; runButton.isEnabled = !value && !stopped; stopButton.isEnabled = value
        runButton.title = value ? "运行中…" : selected == .environment ? "运行体检" : selected == .logs ? "读取日志" : mode.indexOfSelectedItem == 1 ? "执行集合" : "发送 " + (method.titleOfSelectedItem ?? "GET")
        [project, url, collection, query, timeout].forEach { $0.isEnabled = !value }
        [mode, method, level, limit].forEach { $0.isEnabled = !value }
        [chooseProjectButton, chooseCollectionButton, chooseLogsButton].forEach { $0.isEnabled = !value }
        headers.isEditable = !value; body.isEditable = !value
        if value { spinner.startAnimation(nil) } else { spinner.stopAnimation(nil) }
        layout()
    }
    private func payload() -> [String: Any]? {
        func clean(_ value: String) -> String { value.trimmingCharacters(in: .whitespacesAndNewlines) }
        switch selected {
        case .environment:
            let path = clean(project.stringValue); guard !path.isEmpty, path.utf8.count <= 8192, !path.contains("\0") else { return nil }
            return ["project_path": path]
        case .logs:
            guard !logPaths.isEmpty, logPaths.count <= 5, query.stringValue.unicodeScalars.count <= 512 else { return nil }
            return ["paths": logPaths, "query": query.stringValue, "level": ["all", "error", "warning"][max(0, level.indexOfSelectedItem)], "maximum_lines": [200, 500, 1000][max(0, limit.indexOfSelectedItem)]]
        case .api:
            guard let seconds = Int(timeout.stringValue), (1...30).contains(seconds) else { return nil }
            if mode.indexOfSelectedItem == 1 {
                let path = clean(collection.stringValue); guard !path.isEmpty, path.utf8.count <= 8192, !path.contains("\0") else { return nil }
                return ["mode": "hurl", "collection_path": path, "timeout_seconds": seconds]
            }
            let target = clean(url.stringValue)
            guard !target.isEmpty, target.utf8.count <= 8192, headers.string.utf8.count <= 32_768, body.string.utf8.count <= 131_072,
                  !target.contains(where: { $0.isNewline || $0 == "\0" }) else { return nil }
            return ["mode": "request", "method": method.titleOfSelectedItem ?? "GET", "url": target, "headers": headers.string, "body": body.string, "timeout_seconds": seconds]
        }
    }
    @objc private func runSelected() {
        guard active, !stopped, !busy else { return }
        guard let payload = payload(), let input = try? JSONSerialization.data(withJSONObject: payload), input.count <= 262_144 else {
            stateField.stringValue = selected == .environment ? "请选择项目文件夹。" : selected == .logs ? "请选择 1–5 个日志文件，筛选文字最多 512 字符。" : "请核对请求地址或集合路径、1–30 秒超时，以及输入长度。"
            stateField.textColor = .systemOrange; return
        }
        generation += 1; let expected = generation, kind = selected
        requestsStarted += 1; setBusy(true)
        stateField.stringValue = selected == .api ? "正在执行所选请求 · 不会自动重试" : "正在读取所选输入…"; stateField.textColor = .secondaryLabelColor
        let completion: (Result<[String: Any], Error>) -> Void = { [weak self] result in self?.receive(result, generation: expected, kind: kind) }
        if let fixtureRun { fixtureRun(kind.rawValue, payload, completion) }
        else {
            let worker = CueUsageClient(home: home); client = worker; clientsCreated += 1
            worker.run([kind.rawValue], module: "vibecue.engineering_tools", input: input, completion: completion)
        }
    }
    private func receive(_ value: Result<[String: Any], Error>, generation expected: Int, kind: CueEngineeringKind) {
        guard generation == expected, selected == kind, !stopped else { return }
        busy = false; client = nil; spinner.stopAnimation(nil)
        if presentationVisible { setBusy(false) }
        switch value {
        case .failure:
            let report = Report(document: "本次操作未完成。请核对输入和工具状态后再运行；这里不会自动重试。", state: "本次操作未完成 · 可重新运行", status: "error")
            reports[kind] = report; if presentationVisible { showReport(report) }
        case .success(let data):
            guard data["schema_version"] as? Int == 1, data["kind"] as? String == kind.rawValue else {
                let report = Report(document: "结果的类型或版本无法核对，没有显示其他操作的结果。", state: "结果未通过核对", status: "error")
                reports[kind] = report; if presentationVisible { showReport(report) }; return
            }
            reportQueue.async { [weak self] in
                let report = Self.prepare(data, kind: kind)
                DispatchQueue.main.async {
                    guard let self, self.generation == expected, self.selected == kind, !self.stopped else { return }
                    self.reportsPrepared += 1; self.reports[kind] = report
                    if self.presentationVisible { self.showReport(report) }
                }
            }
        }
    }
    private func showReport(_ value: Report) {
        stateField.stringValue = value.state
        stateField.textColor = value.status == "success" ? .secondaryLabelColor : .systemOrange
        setDocument(value.document)
    }
    private func setDocument(_ value: String) {
        guard displayedDocument != value else { return }
        displayedDocument = value; resultApplications += 1; results.string = value; root.needsLayout = true
        results.scrollRangeToVisible(NSRange(location: 0, length: 0)); copyButton.isEnabled = !value.isEmpty
    }
    private static func prepare(_ data: [String: Any], kind: CueEngineeringKind) -> Report {
        let maximum = 98_304
        var pieces: [String] = [], count = 0, truncated = false
        func text(_ raw: Any?, limit: Int = 4096) -> String {
            if let value = raw as? String { return String(value.prefix(limit)) }
            if let value = raw as? NSNumber { return value.stringValue }
            return ""
        }
        func add(_ value: String) {
            guard count < maximum else { truncated = true; return }
            let bytes = Array(value.utf8.prefix(maximum - count))
            let clipped = String(decoding: bytes, as: UTF8.self)
            if bytes.count < value.utf8.count { truncated = true }
            pieces.append(clipped); count += bytes.count
        }
        let status = text(data["status"], limit: 40)
        let states = ["success": "已完成", "partial": "部分结果", "error": "检测失败", "cancelled": "已停止"]
        var state = states[status] ?? "返回状态未确认"
        let metrics = data["metrics"] as? [String: Any] ?? [:]
        if let milliseconds = metrics["duration_ms"] as? NSNumber { state += " · " + String(format: "%.0f ms", milliseconds.doubleValue) }
        add(kind.title + "\n" + text(data["summary"], limit: 4096) + "\n\n")
        if let engine = data["engine"] as? String { add("执行引擎：" + String(engine.prefix(256)) + "\n\n") }
        else if let engine = data["engine"] as? [String: Any] { add("执行引擎：" + text(engine["name"]) + " " + text(engine["version"]) + "\n\n") }
        let engineDetail = text(data["engine_detail"], limit: 4096)
        if !engineDetail.isEmpty { add(engineDetail + "\n\n") }
        for item in (data["items"] as? [[String: Any]] ?? []).prefix(128) {
            let mark = ["success":"✓", "warning":"!", "error":"×", "missing":"—", "available":"✓", "unavailable":"—", "unknown":"?", "info":"•"][text(item["status"], limit: 40)] ?? "•"
            add(mark + " " + text(item["label"], limit: 160) + "  " + text(item["value"], limit: 4096) + "\n")
            let detail = text(item["detail"], limit: 4096); if !detail.isEmpty { add("  " + detail + "\n") }; add("\n")
        }
        if let response = data["response"] as? [String: Any] {
            if let code = response["status_code"] as? NSNumber { add("HTTP 状态：" + code.stringValue + "\n") }
            if let ms = response["timing_ms"] as? NSNumber { add("请求耗时：" + String(format:"%.1f ms", ms.doubleValue) + "\n") }
            if let bytes = response["bytes"] as? NSNumber { add("响应大小：" + bytes.stringValue + " bytes\n") }
            add("\n响应头\n")
            if let rows = response["headers"] as? [[String: Any]] { for row in rows.prefix(128) { add(text(row["name"], limit:256) + ": " + text(row["value"], limit:4096) + "\n") } }
            else { add(text(response["headers"], limit:16_384) + "\n") }
            add("\n响应正文\n" + text(response["body"], limit:maximum) + "\n")
            if response["truncated"] as? Bool == true { add("\n响应内容已按执行器上限截断。\n") }
        }
        let rows = data["rows"] as? [[String: Any]] ?? []
        if !rows.isEmpty { add("\n" + (kind == .logs ? "日志记录" : "执行明细") + "\n") }
        for row in rows.prefix(1000) {
            let line = text(row["line"], limit:30)
            let position = line.isEmpty ? "" : (row["line_scope"] as? String == "tail" ? "尾部第 " : "第 ") + line + " 行"
            add([text(row["timestamp"], limit:80), text(row["level"], limit:30).uppercased(), text(row["source"], limit:200), position].filter { !$0.isEmpty }.joined(separator: " · ") + "\n")
            add(text(row["text"], limit:8192) + "\n\n")
        }
        let warnings = data["warnings"] as? [String] ?? []
        if !warnings.isEmpty { add("\n提示与范围\n"); for warning in warnings.prefix(32) { add("• " + String(warning.prefix(4096)) + "\n") } }
        if metrics["truncated"] as? Bool == true { add("\n本次只读取有限尾部或输出，结果不代表全部历史。\n") }
        let capabilities = data["capabilities"] as? [String: [String: Any]] ?? [:]
        if !capabilities.isEmpty {
            add("\n可选开源引擎\n")
            for name in ["mise", "hurl", "lnav"] {
                guard let capability = capabilities[name] else { continue }
                let fallback = name == "hurl" ? "未安装；单条 HTTP 可用，集合需 Hurl" : name == "mise" ? "未安装；使用本机静态体检" : "未安装；使用原生有界尾读"
                add(name + "：" + (capability["available"] as? Bool == true ? "可用 " + text(capability["version"], limit:200) : fallback) + "\n")
            }
        }
        var document = pieces.joined()
        if truncated { document += "\n\n界面已截断到 96 KiB。请缩小日志范围或请求内容后重新运行。" }
        return Report(document: document, state: state, status: status)
    }
    private func cancelWork(markCancelled: Bool) {
        let wasBusy = busy; generation += 1; client?.stop(); client = nil; setBusy(false)
        if markCancelled && wasBusy {
            cancelledCount += 1
            let message = selected == .api ? "本次操作已停止，本机不会自动重试。HTTP 请求可能已被服务端接收，重新发送前请先核对服务状态。" : "本次读取已停止，输入保留在当前页面。需要最新结果时可再次点击运行。"
            let report = Report(document: message, state: "已停止 · 未自动重新执行", status: "cancelled")
            reports[selected] = report; if active { showReport(report) }
        }
    }
    @objc private func cancelSelected() { cancelWork(markCancelled: true) }
    @objc private func changeAPIMode() { applySelection() }
    @objc private func changeMethod() { setBusy(busy) }
    @objc private func copyResult() { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(results.string, forType: .string) }
    @objc private func openHelp() { NSWorkspace.shared.open(selected.helpURL) }
    private func chooseFiles(directory: Bool, multiple: Bool, completion: @escaping ([URL]) -> Void) {
        let panel = NSOpenPanel(); panel.canChooseDirectories = directory; panel.canChooseFiles = !directory; panel.allowsMultipleSelection = multiple
        panel.prompt = "选择"; panel.message = directory ? "选择要只读检查的项目文件夹。" : multiple ? "选择最多 5 个日志文件；只读取有限尾部。" : "选择 .hurl 请求集合；选择文件不会执行请求。"
        let complete: (NSApplication.ModalResponse) -> Void = { result in if result == .OK { completion(panel.urls) } }
        if let host = root.window { panel.beginSheetModal(for: host, completionHandler: complete) }
        else { panel.begin(completionHandler: complete) }
    }
    @objc private func chooseProject() { chooseFiles(directory: true, multiple: false) { [weak self] urls in self?.project.stringValue = urls.first?.path ?? "" } }
    @objc private func chooseCollection() { chooseFiles(directory: false, multiple: false) { [weak self] urls in
        guard let self, let path = urls.first, path.pathExtension.lowercased() == "hurl" else { self?.stateField.stringValue = "请选择扩展名为 .hurl 的请求集合。"; return }
        self.collection.stringValue = path.path
    } }
    @objc private func chooseLogs() { chooseFiles(directory: false, multiple: true) { [weak self] urls in
        guard let self else { return }
        guard (1...5).contains(urls.count) else { self.stateField.stringValue = "每次请选择 1–5 个日志文件。"; return }
        self.logPaths = urls.map(\.path); self.files.stringValue = urls.map(\.lastPathComponent).joined(separator:" · "); self.files.toolTip = self.logPaths.joined(separator:"\n")
    } }
    func windowWillClose(_ notification: Notification) { _ = deactivateEmbedded() }

    /// Offline review image. No file is opened and no engine is invoked.
    static func captureFixture(to url: URL, kind: String) -> Bool {
        _ = NSApplication.shared
        let controller = CueEngineeringToolsController(home: FileManager.default.temporaryDirectory.appendingPathComponent("vcgo-engineering-capture"))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 980, height: 720), styleMask: [.titled], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        let view = controller.embeddedContentView(); window.contentView = view
        controller.selectKind(kind); controller.activateEmbedded()
        controller.project.stringValue = "/Users/developer/Projects/WebApp"
        controller.url.stringValue = "http://localhost:3000/api/health"; controller.headers.string = "Accept: application/json"
        controller.logPaths = ["/tmp/webapp-api.log", "/tmp/webapp-worker.log"]; controller.files.stringValue = "webapp-api.log · webapp-worker.log"
        controller.query.stringValue = "request"
        var fixture: [String: Any] = ["schema_version":1,"kind":controller.selected.rawValue,"status":"success","summary":"已完成本次检查。以下为离线界面样例。","engine":"builtin","engine_detail":"开源引擎可选；当前使用内置有界后备。",
            "metrics":["duration_ms":24,"bytes_read":4096],"items":[["label":"Node.js","value":"v24.0.0","status":"success","detail":"项目声明 >=22；本机版本符合。"],["label":"可选引擎","value":"未安装","status":"missing","detail":"不会自动安装；后备功能可正常使用。"]]]
        if controller.selected == .api {
            fixture["engine"] = "curl"
            fixture["items"] = [["label":"目标","value":"GET http://localhost:3000/api/health","status":"success"]]
            fixture["response"] = ["status_code":200,"timing_ms":24.5,"bytes":57,"headers":[["name":"content-type","value":"application/json"]],"body":"{\n  \"status\": \"ok\",\n  \"database\": \"connected\"\n}","truncated":false]
        } else if controller.selected == .logs {
            fixture["items"] = [["label":"读取范围","value":"2 个文件 · 每次有限尾部","status":"success"]]
            fixture["rows"] = [["timestamp":"2026-10-30 12:34:56","level":"info","source":"webapp-api.log","line":12,"line_scope":"tail","text":"request complete · GET /api/health · 200 · 24 ms"],["timestamp":"2026-10-30 12:34:57","level":"warning","source":"webapp-worker.log","line":13,"line_scope":"tail","text":"request queue delayed · 1.2 s"]]
        }
        controller.showReport(prepare(fixture,kind:controller.selected)); controller.layout(); view.layoutSubtreeIfNeeded()
        guard let bitmap = view.bitmapImageRepForCachingDisplay(in:view.bounds) else { return false }
        view.cacheDisplay(in:view.bounds,to:bitmap)
        guard let data = bitmap.representation(using:.png,properties:[:]) else { return false }
        do { try data.write(to:url); controller.stop(); window.orderOut(nil); return controller.requestsStarted == 0 }
        catch { controller.stop(); window.orderOut(nil); return false }
    }

    static func validationReport() -> [String: Bool] {
        _ = NSApplication.shared
        let controller = CueEngineeringToolsController(home: FileManager.default.temporaryDirectory.appendingPathComponent("vcgo-engineering-fixture"))
        let view = controller.embeddedContentView()
        let idle = controller.client == nil && controller.requestsStarted == 0 && controller.window == nil
        controller.project.stringValue = "/tmp/project-fixture"; controller.selectKind("api")
        controller.url.stringValue = "http://localhost:3000/fixture"; controller.headers.string = "X-Fixture: only"; controller.body.string = "draft fixture"
        let apiPayload = controller.payload()
        controller.selectKind("logs"); controller.logPaths = ["/tmp/fixture.log"]; controller.query.stringValue = "literal [text]"
        let logPayload = controller.payload()
        controller.selectKind("environment")
        let drafts = controller.project.stringValue == "/tmp/project-fixture" && controller.headers.string == "X-Fixture: only" && controller.body.string == "draft fixture" && controller.logPaths.count == 1
        let noAutomatic = controller.requestsStarted == 0 && controller.client == nil
        var fits = true
        for kind in CueEngineeringKind.allCases {
            controller.selectKind(kind.rawValue)
            for size in [NSSize(width:760,height:460), NSSize(width:1120,height:760)] {
                view.frame.size = size; controller.layout()
                fits = fits && controller.resultScroll.frame.maxX <= size.width && controller.resultScroll.frame.maxY <= controller.helpButton.frame.minY && controller.resultScroll.frame.height >= 50 && controller.runButton.frame.maxX <= size.width
            }
        }
        controller.selectKind("api"); controller.mode.selectItem(at:1); controller.changeAPIMode()
        let hurlFit = controller.headersScroll.isHidden && controller.bodyScroll.isHidden && !controller.collection.isHidden && controller.runButton.title == "执行集合"
        controller.mode.selectItem(at:0); controller.changeAPIMode(); controller.activateEmbedded()
        var callback: ((Result<[String: Any], Error>) -> Void)?
        controller.fixtureRun = { _, _, completion in callback = completion }
        controller.runSelected(); let started = controller.requestsStarted == 1 && controller.busy && controller.client == nil
        let renders = controller.resultApplications, oldGeneration = controller.generation
        _ = controller.deactivateEmbedded()
        callback?(.success(["schema_version":1,"kind":"api","status":"success","summary":"must-not-render-late"]))
        let hidden = !controller.busy && controller.client == nil && controller.generation > oldGeneration && controller.resultApplications == renders && !controller.results.string.contains("must-not-render-late")
        let fixture: [String: Any] = ["schema_version":1,"kind":"api","status":"partial","summary":"受控样例","engine":"curl · 内置后备", "items":[["label":"引擎","value":"Hurl 未安装","status":"missing"]], "response":["status_code":404,"headers":[["name":"Authorization","value":"[redacted]"]],"body":"fixture body","truncated":true,"timing_ms":12.25,"bytes":42],"warnings":["受控说明"],"metrics":["duration_ms":15]]
        let report = prepare(fixture, kind:.api); controller.showReport(report)
        let rendered = controller.results.string.contains("404") && controller.results.string.contains("fixture body") && controller.results.string.contains("[redacted]") && controller.results.string.contains("Hurl 未安装") && controller.stateField.stringValue.contains("部分结果")
        let beforeSame = controller.resultApplications; controller.showReport(report)
        let unchanged = controller.resultApplications == beforeSame
        let huge = prepare(["status":"success","response":["body":String(repeating:"长内容👩🏽‍💻",count:50_000)]],kind:.api)
        let bounded = huge.document.utf8.count < 99_000 && huge.document.contains("96 KiB")
        let manyRows = prepare(["status":"success","engine":"builtin","rows":(0..<1000).map { ["source":"fixture.log","line":$0 + 1,"line_scope":"tail","level":"info","text":String(repeating:"bounded log ",count:30)] as [String:Any] }],kind:.logs)
        let rowsBounded = manyRows.document.utf8.count < 99_000 && manyRows.document.contains("96 KiB") && manyRows.document.contains("尾部第 1 行")
        controller.activateEmbedded(); let beforeWrong = controller.reportsPrepared, beforeWrongApplications = controller.resultApplications
        controller.receive(.success(["schema_version":2,"kind":"logs","summary":"wrong-kind"]),generation:controller.generation,kind:.api)
        let wrong = controller.reportsPrepared == beforeWrong && controller.reports[.api]?.state == "结果未通过核对" && controller.resultApplications == beforeWrongApplications
        let visibility = canPresent(active:true,windowVisible:true,miniaturized:false,occluded:false)
            && !canPresent(active:false,windowVisible:true,miniaturized:false,occluded:false)
            && !canPresent(active:true,windowVisible:false,miniaturized:false,occluded:false)
            && !canPresent(active:true,windowVisible:true,miniaturized:true,occluded:false)
            && !canPresent(active:true,windowVisible:true,miniaturized:false,occluded:true)
        let occluded = Self(home:FileManager.default.temporaryDirectory.appendingPathComponent("vcgo-engineering-hidden-fixture"))
        occluded.selectKind("api"); occluded.activateEmbedded()
        let occludedApplications = occluded.resultApplications
        var hiddenFixture = fixture; hiddenFixture["summary"] = "hidden-completion-cache-fixture"
        occluded.receive(.success(hiddenFixture),generation:occluded.generation,kind:.api)
        let deadline = Date().addingTimeInterval(2)
        while occluded.reportsPrepared == 0 && Date() < deadline { RunLoop.main.run(until:Date().addingTimeInterval(0.002)) }
        let completionCached = occluded.active && !occluded.presentationVisible && occluded.reportsPrepared == 1
            && occluded.reports[.api]?.document.contains("hidden-completion-cache-fixture") == true
            && occluded.resultApplications == occludedApplications && !occluded.results.string.contains("hidden-completion-cache-fixture")
        // Invoke the same cached selection apply used by visible lifecycle
        // restoration without showing a real fixture window or launching work.
        occluded.applySelection()
        let restoration = occluded.results.string.contains("hidden-completion-cache-fixture") && occluded.requestsStarted == 0 && occluded.client == nil
        let environmentSchema = prepare(["status":"partial","engine":"builtin+mise","engine_detail":"实际探测与项目静态要求", "items":[["label":"node · package.json","value":"要求 >=22","status":"unknown","detail":"当前已找到，版本未核实"],["label":"go · go.mod","value":"要求 1.24","status":"error","detail":"当前 1.23 · 不符合"]]],kind:.environment)
        let environmentStatuses = environmentSchema.document.contains("? node · package.json") && environmentSchema.document.contains("× go · go.mod") && environmentSchema.document.contains("版本未核实") && environmentSchema.document.contains("builtin+mise")
        let logSchema = prepare(["status":"partial","engine":"builtin+lnav","engine_detail":"lnav 解析 1 行；其余保留原生识别。", "rows":[["timestamp":NSNull(),"level":"unknown","source":"/tmp/sample.log","line":3,"line_scope":"tail","text":"timestamp unavailable"],["timestamp":"2026-10-30T12:00:00","level":"error","source":"/tmp/sample.log","line":4,"line_scope":"file","text":"actual error"]],"metrics":["truncated":true],"warnings":["文件在读取时发生变化"]],kind:.logs)
        let logSchemaShown = logSchema.document.contains("UNKNOWN") && logSchema.document.contains("尾部第 3 行") && logSchema.document.contains("第 4 行") && logSchema.document.contains("文件在读取时发生变化") && logSchema.document.contains("lnav 解析 1 行")
        var errorFixture = fixture; errorFixture["status"] = "error"; errorFixture["summary"] = "HTTP 404，未自动重试。"
        let apiError = prepare(errorFixture,kind:.api)
        let apiErrorShown = apiError.status == "error" && apiError.document.contains("HTTP 404，未自动重试。") && apiError.document.contains("HTTP 状态：404") && apiError.document.contains("fixture body")
        let idleLayout = Self(home:FileManager.default.temporaryDirectory.appendingPathComponent("vcgo-engineering-idle-layout"))
        idleLayout.selectKind("logs"); idleLayout.root.frame.size = NSSize(width:960,height:720); idleLayout.layout()
        let layoutPassesBefore = idleLayout.layoutPasses, writesBefore = idleLayout.geometryWrites, documentsBefore = idleLayout.resultApplications
        for _ in 0..<100 {
            idleLayout.layout(); idleLayout.setBusy(false)
        }
        let noIdleLayout = idleLayout.layoutPasses == layoutPassesBefore && idleLayout.geometryWrites == writesBefore && idleLayout.resultApplications == documentsBefore
        idleLayout.root.frame.size.width += 40; idleLayout.layout()
        let resizeInvalidates = idleLayout.layoutPasses == layoutPassesBefore + 1 && idleLayout.resultScroll.frame.width == idleLayout.root.bounds.width - 40
        let afterResize = idleLayout.layoutPasses
        idleLayout.selectKind("api"); idleLayout.mode.selectItem(at:1); idleLayout.changeAPIMode()
        let modeInvalidates = idleLayout.layoutPasses > afterResize && idleLayout.headersScroll.isHidden && !idleLayout.collection.isHidden
        controller.stop(); occluded.stop(); idleLayout.stop()
        return ["engineering_page_initialization_has_no_worker_window_or_polling":idle,
            "engineering_navigation_and_activation_do_not_start_engines":noAutomatic && controller.requestsStarted == 1,
            "engineering_three_kinds_retain_in_memory_drafts":drafts,
            "engineering_api_default_get_is_explicit_and_stdin_payload_bounded":apiPayload?["method"] as? String == "GET" && apiPayload?["body"] as? String == "draft fixture" && apiPayload?["timeout_seconds"] as? Int == 15,
            "engineering_logs_use_literal_filter_and_bounded_selected_files":logPayload?["query"] as? String == "literal [text]" && logPayload?["maximum_lines"] as? Int == 200,
            "engineering_native_pages_fit_compact_and_large_workspaces":fits,
            "engineering_hurl_mode_exposes_collection_and_hides_request_editors":hurlFit,
            "engineering_work_starts_only_from_run_and_is_single_flight":started,
            "engineering_hiding_cancels_only_owned_work_and_rejects_late_results":hidden,
            "engineering_results_show_partial_http_errors_and_engine_status":rendered,
            "engineering_same_report_does_not_relayout_text":unchanged,
            "engineering_large_report_is_bounded_before_main_thread_apply":bounded,
            "engineering_thousand_log_rows_have_total_display_cap_and_honest_tail_numbers":rowsBounded,
            "engineering_wrong_kind_or_protocol_is_not_rendered":wrong,
            "engineering_selected_tag_requires_visible_unminimized_unoccluded_window":visibility,
            "engineering_occluded_completion_caches_report_without_textkit_application":completionCached,
            "engineering_visible_restoration_consumes_cache_without_starting_worker":restoration,
            "engineering_environment_unknown_and_mismatch_statuses_match_backend_schema":environmentStatuses,
            "engineering_logs_keep_unknown_time_tail_scope_rotation_warning_and_engine_detail":logSchemaShown,
            "engineering_api_http_error_keeps_status_and_response_body":apiErrorShown,
            "engineering_idle_geometry_does_not_repeat_textkit_size_or_view_frame_writes":noIdleLayout,
            "engineering_resize_invalidates_geometry_exactly_once":resizeInvalidates,
            "engineering_page_and_api_mode_changes_invalidate_geometry":modeInvalidates,
            "engineering_editors_support_native_plain_text_selection":controller.headers.isSelectable && controller.headers.allowsUndo && !controller.headers.isRichText && controller.results.isSelectable && !controller.results.isEditable]
    }
}
