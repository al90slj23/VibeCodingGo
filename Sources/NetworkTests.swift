import AppKit
import Foundation

/// Only request limits persist. Targets, IP addresses and results stay in this window.
enum CueNetworkTestPreferences {
    static func validProbeLimit(_ value: Int) -> Int { [3, 5, 6].contains(value) ? value : 3 }
    static func validDownloadMiB(_ value: Int) -> Int { [1, 3, 5].contains(value) ? value : 1 }
    static var probeLimit: Int {
        get { validProbeLimit(UserDefaults.standard.integer(forKey: "VCGoNetworkTestProbeLimit")) }
        set { UserDefaults.standard.set(validProbeLimit(newValue), forKey: "VCGoNetworkTestProbeLimit") }
    }
    static var downloadMiB: Int {
        get { validDownloadMiB(UserDefaults.standard.integer(forKey: "VCGoNetworkTestDownloadMiB")) }
        set { UserDefaults.standard.set(validDownloadMiB(newValue), forKey: "VCGoNetworkTestDownloadMiB") }
    }
}

private enum CueNetworkTestKind: String, CaseIterable {
    case ipReputation = "ip_reputation", domain, ipTrace = "ip_trace"
    case globalPing = "global_ping", globalHTTP = "global_http", speed
    var title: String {
        switch self {
        case .ipReputation: return "IP 信誉"
        case .domain: return "域名检测"
        case .ipTrace: return "出口与痕迹"
        case .globalPing: return "全球 Ping"
        case .globalHTTP: return "全球 HTTP"
        case .speed: return "下载测速"
        }
    }
    var symbol: String {
        switch self {
        case .ipReputation: return "checkmark.shield"
        case .domain: return "network"
        case .ipTrace: return "location.magnifyingglass"
        case .globalPing: return "globe"
        case .globalHTTP: return "globe.americas"
        case .speed: return "speedometer"
        }
    }
    var detail: String {
        switch self {
        case .ipReputation: return "查看出口类型、信誉与风险信号，保留检测来源与不确定性。"
        case .domain: return "核对 DNS、HTTPS 与公开检测信号，帮助判断访问异常。"
        case .ipTrace: return "对照本机应用的公网出口；浏览器 WebRTC 与 DNS 需要在浏览器中另行核对。"
        case .globalPing: return "从不同地区的公共探针观察同一个目标的延迟与丢包。"
        case .globalHTTP: return "从不同地区的公共探针比较目标网站的 HTTP 可达性。"
        case .speed: return "有限下载估算当前链路吞吐量；不会自动执行上传测试。"
        }
    }
    var requiresTarget: Bool { self == .domain || self == .globalPing || self == .globalHTTP }
    var acceptsTarget: Bool { self != .ipTrace && self != .speed }
    var disclosure: String {
        switch self {
        case .ipReputation: return "留空时由 ipify / Cloudflare 识别当前出口；待查 IP 会发送给 ipapi.is 和 proxycheck.io。风险字段来自来源数据库，不能保证所有平台都接受此 IP。"
        case .domain: return "运行后核对系统 DNS、Cloudflare / Google 公共解析，并访问目标 HTTPS。域名会交给这些解析来源；单次失败或地区差异不能单独证明被封禁。"
        case .ipTrace: return "运行后向 ipify 和 Cloudflare 查询本应用的出口 IP。结果不代表浏览器 WebRTC、DNS 或所有代理路径，不能证明真实地址从未泄露。"
        case .globalPing, .globalHTTP: return "运行后会将目标发送给 Globalping 公共探针。目标与测量结果可能公开；关闭窗口只停止本机等待，已提交的测量可能继续。"
        case .speed: return "运行后会从 Cloudflare 下载有限测试数据，消耗所选下载流量，并向服务暴露当前公网出口。结果仅代表本次链路。"
        }
    }
    var placeholder: String {
        switch self {
        case .ipReputation: return "留空检测当前出口，或填写公网 IP"
        case .domain: return "例如 example.com（只填域名）"
        case .globalPing: return "公网 IP 或域名"
        case .globalHTTP: return "例如 example.com（只填域名）"
        default: return "此项不需要输入目标"
        }
    }
}

private final class CueNetworkTestSurface: NSView {
    var fillColor: NSColor = .windowBackgroundColor
    var onLayout: (() -> Void)?
    override var isFlipped: Bool { true }
    override func layout() { super.layout(); onLayout?() }
    override func viewDidMoveToWindow() { super.viewDidMoveToWindow(); onLayout?() }
    override func draw(_ dirtyRect: NSRect) { fillColor.setFill(); dirtyRect.fill() }
}

/// Explicit, one-shot diagnostics. Constructing or navigating the window does no I/O.
final class CueNetworkTestsController: NSWindowController, NSWindowDelegate, NSTextViewDelegate, NSTextFieldDelegate {
    private let home: URL
    private var client: CueUsageClient?
    private let root = CueNetworkTestSurface(), sidebar = CueNetworkTestSurface()
    private let titleField = NSTextField(labelWithString: "")
    private let detailField = NSTextField(wrappingLabelWithString: "")
    private let targetLabel = NSTextField(labelWithString: "检测目标")
    private let target = NSTextField(string: "")
    private let runButton = NSButton(title: "运行检测", target: nil, action: nil)
    private let stopButton = NSButton(title: "停止", target: nil, action: nil)
    private let copyButton = NSButton(title: "复制结果", target: nil, action: nil)
    private let refreshMeasurementButton = NSButton(title: "刷新此次测量", target: nil, action: nil)
    private let spinner = NSProgressIndicator()
    private let stateField = NSTextField(labelWithString: "等待运行")
    private let disclosure = NSTextField(wrappingLabelWithString: "")
    private let resultScroll = NSScrollView(), results = NSTextView()
    private let browserWebRTC = NSButton(title: "浏览器 WebRTC 检测 ↗", target: nil, action: nil)
    private let browserDNS = NSButton(title: "浏览器 DNS 检测 ↗", target: nil, action: nil)
    private let footer = NSTextField(labelWithString: "结果仅在本次 App 内存中保留 · 可选择并复制")
    private var categoryButtons: [NSButton] = []
    private var selected: CueNetworkTestKind = .ipReputation
    private var drafts: [String: String] = [:]
    private var generation = 0
    private var busy = false
    private var embedded = false, active = true
    private var resultApplications = 0, requestsStarted = 0, reportsPrepared = 0, layoutPasses = 0
    private var observerTokens: [NSObjectProtocol] = []
    private let reportQueue = DispatchQueue(label: "local.vibecue.network-report", qos: .utility)
    private struct Piece { let text: String, style: Int; let link: URL? }
    private struct Report { let pieces: [Piece], document: String, state: String, status: String }
    private var reports: [CueNetworkTestKind: Report] = [:]
    private var displayedDocument: String?
    private struct Measurement { let id: String, target: String, canonicalTarget: String, kind: CueNetworkTestKind, regions: [String], probeLimit: Int, retryUntil: TimeInterval }
    private struct StoppedReceipt {
        let callbackGeneration: Int, stoppedGeneration: Int, kind: CueNetworkTestKind, target: String, probeLimit: Int
        let previousMeasurement: Measurement?
    }
    private var stoppedReceipt: StoppedReceipt?
    private var acceptedStopReceipts = 0
    private var requestProbeLimit = 3
    private var measurement: Measurement?
    private var measurementRefreshes = 0
    private var fixturePresentation: Bool?
    private var fixtureRun: (([String: Any], @escaping (Result<[String: Any], Error>) -> Void) -> Void)?
    private struct LayoutState: Equatable { let size: NSSize, kind: CueNetworkTestKind, busy: Bool, embedded: Bool, resume: Bool, viewport: NSSize }
    private var lastLayout: LayoutState?, layingOut = false
    private var presentationVisible: Bool {
        if let fixturePresentation { return active && fixturePresentation }
        guard let host = root.window else { return false }
        return active && host.isVisible && !host.isMiniaturized && host.occlusionState.contains(.visible)
    }
    var performanceSnapshot: [String: Any] {
        ["created":true,"requests_started":requestsStarted,"reports_prepared":reportsPrepared,"result_applications":resultApplications,
         "active":active,"visible":presentationVisible,"busy":busy,"client_created":client != nil,
         "cached_reports":reports.count,"layout_passes":layoutPasses,"measurement_refreshes":measurementRefreshes,"accepted_stop_receipts":acceptedStopReceipts,"scheduled_jobs":0]
    }

    // Documentation and manually opened browser tests only. Never trust a URL
    // supplied by a diagnostic response unless it exactly matches this list.
    private static let sourceURLs: Set<String> = [
        "https://www.ipify.org/", "https://ipapi.is/free-tier.html", "https://proxycheck.io/api/",
        "https://globalping.io/docs/api.globalping.io", "https://github.com/cloudflare/speedtest",
        "https://browserleaks.com/webrtc", "https://www.dnsleaktest.com/",
        "https://www.rfc-editor.org/rfc/rfc8828",
        "https://developers.cloudflare.com/fundamentals/reference/cdn-cgi-endpoint/",
        "https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/",
        "https://developers.google.com/speed/public-dns/docs/doh/json",
        "https://blog.torproject.org/changes-tor-exit-list-service/"
    ]
    private static func trustedSource(_ raw: String) -> URL? {
        guard sourceURLs.contains(raw), let url = URL(string: raw), url.scheme == "https",
              url.user == nil, url.password == nil, url.port == nil else { return nil }
        return url
    }

    init(home: URL) {
        self.home = home
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 940, height: 700), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "VibeCoding Go · Test 网络检测"; window.isReleasedWhenClosed = false
        window.contentMinSize = NSSize(width: 820, height: 620); window.center()
        super.init(window: window); window.contentView = root; window.delegate = self
        sidebar.fillColor = .underPageBackgroundColor; root.addSubview(sidebar)
        let module = NSTextField(labelWithString: "Test")
        module.font = .systemFont(ofSize: 20, weight: .semibold); module.frame = NSRect(x: 20, y: 24, width: 132, height: 28); sidebar.addSubview(module)
        let subtitle = NSTextField(labelWithString: "网络与环境检测")
        subtitle.font = .systemFont(ofSize: 11); subtitle.textColor = .secondaryLabelColor
        subtitle.frame = NSRect(x: 20, y: 60, width: 132, height: 18); sidebar.addSubview(subtitle)
        for (index, kind) in CueNetworkTestKind.allCases.enumerated() {
            let button = NSButton(title: kind.title, target: self, action: #selector(changeKind(_:)))
            button.image = NSImage(systemSymbolName: kind.symbol, accessibilityDescription: nil)
            button.imagePosition = .imageLeading; button.imageScaling = .scaleProportionallyDown
            button.alignment = .left; button.isBordered = false; button.tag = index
            button.font = .systemFont(ofSize: 12, weight: .medium)
            button.frame = NSRect(x: 12, y: 108 + index * 45, width: 148, height: 35)
            button.identifier = NSUserInterfaceItemIdentifier("network.kind." + kind.rawValue)
            button.wantsLayer = true; button.layer?.cornerRadius = 7
            sidebar.addSubview(button); categoryButtons.append(button)
        }
        titleField.font = .systemFont(ofSize: 21, weight: .semibold)
        detailField.font = .systemFont(ofSize: 12); detailField.textColor = .secondaryLabelColor; detailField.maximumNumberOfLines = 2
        targetLabel.font = .systemFont(ofSize: 11.5, weight: .medium)
        target.font = .monospacedSystemFont(ofSize: 12, weight: .regular); target.placeholderString = selected.placeholder
        target.delegate = self
        target.identifier = NSUserInterfaceItemIdentifier("network.target"); target.setAccessibilityLabel("网络检测目标")
        runButton.target = self; runButton.action = #selector(runSelected); runButton.bezelStyle = .rounded
        runButton.identifier = NSUserInterfaceItemIdentifier("network.run")
        stopButton.target = self; stopButton.action = #selector(stopSelected); stopButton.bezelStyle = .rounded
        stopButton.identifier = NSUserInterfaceItemIdentifier("network.stop"); stopButton.isEnabled = false
        copyButton.target = self; copyButton.action = #selector(copyResults); copyButton.bezelStyle = .rounded
        copyButton.identifier = NSUserInterfaceItemIdentifier("network.copy"); copyButton.isEnabled = false
        refreshMeasurementButton.target = self; refreshMeasurementButton.action = #selector(refreshMeasurement)
        refreshMeasurementButton.bezelStyle = .rounded; refreshMeasurementButton.font = .systemFont(ofSize:11)
        refreshMeasurementButton.identifier = NSUserInterfaceItemIdentifier("network.refresh-measurement")
        refreshMeasurementButton.toolTip = "只读取已有测量结果，不重新提交公共探针任务。"; refreshMeasurementButton.isHidden = true
        spinner.style = .spinning; spinner.controlSize = .small; spinner.isDisplayedWhenStopped = false
        stateField.font = .systemFont(ofSize: 11.5); stateField.textColor = .secondaryLabelColor
        stateField.identifier = NSUserInterfaceItemIdentifier("network.status")
        disclosure.font = .systemFont(ofSize: 11); disclosure.textColor = .secondaryLabelColor; disclosure.maximumNumberOfLines = 4
        results.isEditable = false; results.isSelectable = true; results.isRichText = true; results.importsGraphics = false
        results.usesFindPanel = true; results.drawsBackground = true; results.backgroundColor = .textBackgroundColor
        results.textContainerInset = NSSize(width: 16, height: 15); results.delegate = self
        results.isVerticallyResizable = true; results.isHorizontallyResizable = false; results.autoresizingMask = [.width]
        results.textContainer?.widthTracksTextView = true; results.textContainer?.containerSize = NSSize(width: 600, height: CGFloat.greatestFiniteMagnitude)
        results.identifier = NSUserInterfaceItemIdentifier("network.results"); results.setAccessibilityLabel("网络检测结果，可选择并复制")
        resultScroll.documentView = results; resultScroll.hasVerticalScroller = true; resultScroll.autohidesScrollers = true
        resultScroll.drawsBackground = true; resultScroll.backgroundColor = .textBackgroundColor
        resultScroll.borderType = .bezelBorder
        for button in [browserWebRTC, browserDNS] {
            button.target = self; button.bezelStyle = .rounded; button.font = .systemFont(ofSize: 11)
        }
        browserWebRTC.action = #selector(openWebRTC); browserDNS.action = #selector(openDNS)
        browserWebRTC.identifier = NSUserInterfaceItemIdentifier("network.browser.webrtc")
        browserDNS.identifier = NSUserInterfaceItemIdentifier("network.browser.dns")
        browserWebRTC.toolTip = "手动打开 BrowserLeaks；检测浏览器出口，结果不等同于本应用的出口。"
        browserDNS.toolTip = "手动打开 DNSLeakTest；仅打开网页，不替你启动其中的检测。"
        footer.font = .systemFont(ofSize: 10.5); footer.textColor = .tertiaryLabelColor
        [titleField, detailField, targetLabel, target, runButton, stopButton, copyButton, refreshMeasurementButton, spinner, stateField, disclosure, resultScroll, browserWebRTC, browserDNS, footer].forEach { root.addSubview($0) }
        root.onLayout = { [weak self] in self?.layout(); self?.presentCachedReport() }
        applySelection(); layout()
        for name in [NSWindow.didChangeOcclusionStateNotification, NSWindow.didDeminiaturizeNotification] {
            observerTokens.append(NotificationCenter.default.addObserver(forName:name, object:nil, queue:.main) { [weak self] note in
                guard let self, let host = note.object as? NSWindow, host === self.root.window else { return }
                self.presentCachedReport()
            })
        }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    deinit { client?.stop(); observerTokens.forEach(NotificationCenter.default.removeObserver) }

    func selectKind(_ kind: String) {
        let canonical = kind == "egress" ? "ip_trace" : kind == "download" ? "speed" : kind
        guard let next = CueNetworkTestKind(rawValue: canonical), next != selected else { return }
        drafts[selected.rawValue] = target.stringValue
        measurement = nil; stoppedReceipt = nil
        generation += 1; client?.stop(); client = nil; busy = false
        selected = next; applySelection(); layout(); presentCachedReport()
    }
    /// Main-shell navigation supplies the module and category tags. Detach
    /// this native view from the hidden compatibility window without showing it.
    func embeddedContentView() -> NSView {
        active = true
        if !embedded {
            embedded = true
            window?.orderOut(nil)
            if window?.contentView === root { window?.contentView = NSView(frame: .zero) }
            sidebar.isHidden = true
            root.autoresizingMask = [.width, .height]
            titleField.isHidden = true
            disclosure.font = .systemFont(ofSize: 10.5)
            layout()
        }
        presentCachedReport()
        return root
    }

    func activateEmbedded() { active = true; presentCachedReport() }

    /// Call on leaving this module or closing the main panel, not on redraw.
    func stopEmbeddedWork() {
        active = false
        cancelCurrentWork()
    }
    private func cancelCurrentWork(acceptMeasurementReceipt: Bool = false) {
        drafts[selected.rawValue] = target.stringValue
        let wasBusy = busy, previousGeneration = generation
        let canReceive = acceptMeasurementReceipt && wasBusy && presentationVisible
        generation += 1
        stoppedReceipt = canReceive ? StoppedReceipt(callbackGeneration:previousGeneration,stoppedGeneration:generation,kind:selected,target:target.stringValue.trimmingCharacters(in:.whitespacesAndNewlines),probeLimit:requestProbeLimit,previousMeasurement:measurement) : nil
        client?.stop(); client = nil; setBusy(false)
        if wasBusy {
            let summary = selected == .globalPing || selected == .globalHTTP
                ? "本次检测尚未完成。已停止本机等待；已提交的公共测量可能继续，这里不会自动重新发送。"
                : "本次检测尚未完成。已停止本次请求，不会自动重试或沿用上次结果。"
            reports[selected] = Self.prepare(["status":"cancelled","title":selected.title,"summary":summary,"rows":[]])
            presentCachedReport()
        }
    }

    @objc private func stopSelected() { cancelCurrentWork(acceptMeasurementReceipt:true) }
    @objc private func copyResults() {
        guard let document = displayedDocument, !document.isEmpty else { return }
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(document, forType:.string)
    }
    @objc private func changeKind(_ sender: NSButton) {
        guard CueNetworkTestKind.allCases.indices.contains(sender.tag) else { return }
        selectKind(CueNetworkTestKind.allCases[sender.tag].rawValue)
    }
    private func applySelection() {
        titleField.stringValue = selected.title; detailField.stringValue = selected.detail
        disclosure.stringValue = selected.disclosure; target.placeholderString = selected.placeholder
        target.stringValue = drafts[selected.rawValue] ?? ""; target.isEnabled = selected.acceptsTarget
        targetLabel.stringValue = selected == .ipReputation ? "IP 地址（可留空）" : "检测目标"
        browserWebRTC.isHidden = selected != .ipTrace; browserDNS.isHidden = selected != .ipTrace
        for (index, button) in categoryButtons.enumerated() {
            let active = CueNetworkTestKind.allCases[index] == selected
            button.layer?.backgroundColor = (active ? NSColor.controlAccentColor.withAlphaComponent(0.12) : .clear).cgColor
            button.contentTintColor = active ? .labelColor : .secondaryLabelColor
            button.state = active ? .on : .off
        }
        setBusy(false); stateField.stringValue = "等待运行 · 不会自动连接检测服务"; stateField.textColor = .secondaryLabelColor
        showPlain("尚未运行\n\n选择检测项目，核对目标与下方的数据来源说明，再点击“运行检测”。\n\nIP 信誉、可达性与泄露信号都具有观测范围；没有观测到异常，不等于所有场景均安全。")
    }
    private func layout() {
        guard !layingOut else { return }
        let state = LayoutState(size:root.bounds.size,kind:selected,busy:busy,embedded:embedded,resume:measurement != nil,viewport:resultScroll.contentSize)
        guard state != lastLayout else { return }
        layingOut = true; defer { layingOut = false; lastLayout = LayoutState(size:root.bounds.size,kind:selected,busy:busy,embedded:embedded,resume:measurement != nil,viewport:resultScroll.contentSize) }
        layoutPasses += 1
        let width = root.bounds.width, height = root.bounds.height
        sidebar.frame = embedded ? .zero : NSRect(x: 0, y: 0, width: 174, height: height)
        let x: CGFloat = embedded ? 22 : 199
        let available = max(260, width - x - 26)
        let noInput = embedded && !selected.acceptsTarget
        titleField.isHidden = embedded
        targetLabel.isHidden = noInput; target.isHidden = noInput
        let detailY: CGFloat = embedded ? 12 : 65
        let targetY: CGFloat = embedded ? 79 : 148
        let stateY: CGFloat = embedded ? (noInput ? 96 : 119) : 189
        let disclosureY: CGFloat = embedded ? (noInput ? 128 : 151) : 220
        let resultY: CGFloat = embedded ? (noInput ? 196 : 219) : 294
        titleField.frame = NSRect(x: x, y: 24, width: available, height: 29)
        detailField.frame = NSRect(x: x + 1, y: detailY, width: available - 2, height: embedded ? 35 : 43)
        targetLabel.frame = NSRect(x: x + 1, y: embedded ? 56 : 121, width: available, height: 19)
        target.frame = NSRect(x: x, y: targetY, width: max(120, available - 296), height: 29)
        let buttonY = noInput ? 57 : targetY - 2
        runButton.frame = NSRect(x:x + available - 278,y:buttonY,width:112,height:33)
        stopButton.frame = NSRect(x:x + available - 158,y:buttonY,width:70,height:33)
        copyButton.frame = NSRect(x:x + available - 80,y:buttonY,width:80,height:33)
        spinner.frame = NSRect(x: x, y: stateY + 2, width: 16, height: 16)
        refreshMeasurementButton.isHidden = measurement == nil
        refreshMeasurementButton.isEnabled = !busy && measurement != nil
        refreshMeasurementButton.frame = NSRect(x:x + available - 150,y:stateY - 3,width:150,height:26)
        stateField.frame = NSRect(x: x + (busy ? 23 : 1), y: stateY, width: available - (measurement == nil ? 24 : 168), height: 20)
        disclosure.frame = NSRect(x: x + 1, y: disclosureY, width: available - 2, height: embedded ? 54 : 64)
        let bottom: CGFloat = selected == .ipTrace ? (embedded ? 73 : 93) : (embedded ? 35 : 53)
        resultScroll.frame = NSRect(x: x, y: resultY, width: available, height: max(60, height - resultY - bottom))
        let textWidth = max(100, resultScroll.contentSize.width)
        results.frame.size = NSSize(width: textWidth, height: max(results.frame.height, resultScroll.contentSize.height))
        results.minSize = NSSize(width: 0, height: resultScroll.contentSize.height)
        results.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        browserWebRTC.frame = NSRect(x: x - 3, y: height - (embedded ? 69 : 78), width: 218, height: 30)
        browserDNS.frame = NSRect(x: x + 226, y: height - (embedded ? 69 : 78), width: 211, height: 30)
        footer.frame = NSRect(x: x + 1, y: height - (embedded ? 27 : 32), width: available - 2, height: 18)
    }
    private func setBusy(_ value: Bool) {
        busy = value; runButton.isEnabled = !value; runButton.title = value ? "运行中…" : "运行检测"
        stopButton.isEnabled = value
        target.isEnabled = !value && selected.acceptsTarget
        if value { spinner.startAnimation(nil) } else { spinner.stopAnimation(nil) }
        layout()
    }
    private static func request(kind: CueNetworkTestKind, target: String, probeLimit: Int, downloadMiB: Int) -> [String: Any]? {
        let value = target.trimmingCharacters(in: .whitespacesAndNewlines)
        guard value.utf8.count <= 2048, !value.contains(where: { $0.isNewline || $0 == "\0" }), !kind.requiresTarget || !value.isEmpty else { return nil }
        return ["kind": kind.rawValue, "target": kind.acceptsTarget ? value : "", "probe_limit": CueNetworkTestPreferences.validProbeLimit(probeLimit), "download_bytes": CueNetworkTestPreferences.validDownloadMiB(downloadMiB) * 1_048_576]
    }
    func controlTextDidChange(_ notification: Notification) {
        guard notification.object as? NSTextField === target else { return }
        generation += 1; stoppedReceipt = nil
        if busy { client?.stop(); client = nil; setBusy(false) }
        measurement = nil; reports.removeValue(forKey:selected)
        stateField.stringValue = "目标已改变 · 尚未检测"; stateField.textColor = .secondaryLabelColor
        showPlain("尚未对新目标运行检测。核对目标后点击“运行检测”。")
        layout()
    }
    @objc private func refreshMeasurement() {
        guard let measurement, measurement.kind == selected,
              measurement.target == target.stringValue.trimmingCharacters(in:.whitespacesAndNewlines) else { return }
        let remaining = measurement.retryUntil - Date().timeIntervalSince1970
        if remaining > 0 {
            stateField.stringValue = "来源限流 · 约 \(Int(ceil(remaining))) 秒后可再次刷新"
            stateField.textColor = .systemOrange
            return
        }
        startRequest(measurementID:measurement.id)
    }
    @objc private func runSelected() { startRequest() }
    private func startRequest(measurementID: String? = nil) {
        guard !busy else { return }
        guard var payload = Self.request(kind: selected, target: target.stringValue, probeLimit: CueNetworkTestPreferences.probeLimit, downloadMiB: CueNetworkTestPreferences.downloadMiB) else {
            stateField.stringValue = selected.requiresTarget ? "请先填写检测目标；目标格式还会由检测模块核对。" : "目标输入过长或含有无效字符。"
            stateField.textColor = .systemOrange; return
        }
        if let measurementID, let measurement {
            payload["measurement_id"] = measurementID; payload["target"] = measurement.canonicalTarget
            payload["probe_limit"] = measurement.probeLimit
            if !measurement.regions.isEmpty { payload["regions"] = measurement.regions }
            measurementRefreshes += 1
        } else { measurement = nil }
        requestProbeLimit = payload["probe_limit"] as? Int ?? 3
        guard let input = try? JSONSerialization.data(withJSONObject:payload) else { return }
        stoppedReceipt = nil
        active = true; drafts[selected.rawValue] = target.stringValue
        reports.removeValue(forKey:selected)
        generation += 1; requestsStarted += 1
        let expectedGeneration = generation, expectedKind = selected
        client?.stop(); client = nil
        setBusy(true); stateField.stringValue = "正在运行 · 可随时停止本机等待"; stateField.textColor = .secondaryLabelColor
        showPlain("正在检测…\n\n本次只执行当前选择的项目，可点击停止。关闭窗口或切换项目会停止本机等待；已提交的公共测量可能继续。")
        let completion: (Result<[String: Any], Error>) -> Void = { [weak self] result in
            self?.receive(result,generation:expectedGeneration,kind:expectedKind)
        }
        if let fixtureRun { fixtureRun(payload,completion); return }
        let worker = CueUsageClient(home:home); client = worker
        worker.run(["run"],module:"vibecue.network_tests",input:input,completion:completion)
    }
    private func receive(_ value: Result<[String: Any], Error>, generation expected: Int, kind: CueNetworkTestKind) {
        guard generation == expected, selected == kind else {
            receiveStoppedMeasurement(value,generation:expected,kind:kind)
            return
        }
        setBusy(false); client = nil
        switch value {
        case .success(let data):
            guard data["schema_version"] as? Int == 1, data["kind"] as? String == kind.rawValue else {
                reports[kind] = Self.prepare(["status":"error","title":kind.title,"summary":"结果与所选检测或接口版本不一致，未显示旧结果。可手动重新运行。","rows":[]])
                presentCachedReport(); return
            }
            measurement = Self.measurementHandle(data,kind:kind,input:target.stringValue.trimmingCharacters(in:.whitespacesAndNewlines),previous:measurement,probeLimit:requestProbeLimit)
            layout()
            reportQueue.async { [weak self] in
                let report = Self.prepare(data)
                DispatchQueue.main.async {
                    guard let self, self.generation == expected, self.selected == kind else { return }
                    self.reportsPrepared += 1; self.reports[kind] = report
                    self.presentCachedReport()
                }
            }
        case .failure:
            let report = Self.prepare(["status":"error","title":kind.title,"summary":"检测执行器没有返回完整结果。可手动重试；上次结果不会被当作本次检测。","rows":[]])
            reports[kind] = report; presentCachedReport()
        }
    }
    private func receiveStoppedMeasurement(_ value: Result<[String:Any],Error>, generation expected: Int, kind: CueNetworkTestKind) {
        guard let receipt = stoppedReceipt, receipt.callbackGeneration == expected,
              receipt.stoppedGeneration == generation, receipt.kind == kind, selected == kind,
              presentationVisible, !busy, target.stringValue.trimmingCharacters(in:.whitespacesAndNewlines) == receipt.target,
              case .success(let data) = value, data["schema_version"] as? Int == 1,
              data["kind"] as? String == kind.rawValue, data["status"] as? String == "cancelled",
              let handle = Self.measurementHandle(data,kind:kind,input:receipt.target,previous:receipt.previousMeasurement,probeLimit:receipt.probeLimit) else { return }
        // Only the reusable identity survives explicit Stop. Late result rows,
        // success conclusions and target changes never re-enter the display.
        stoppedReceipt = nil; acceptedStopReceipts += 1; measurement = handle
        let information = data["measurement"] as? [String:Any] ?? [:]
        let cancelled:[String:Any] = ["status":"cancelled","title":kind.title,"summary":"已停止本机等待。此次公共测量的编号已保留；可点击“刷新此次测量”只读取已有结果，不重新提交探针。","measurement":information,"checked_at":data["checked_at"] ?? Date().timeIntervalSince1970,"rows":[]]
        reports[kind] = Self.prepare(cancelled); layout(); presentCachedReport()
    }
    private static func measurementHandle(_ data:[String:Any],kind:CueNetworkTestKind,input:String,previous:Measurement?,probeLimit:Int) -> Measurement? {
        guard kind == .globalPing || kind == .globalHTTP,
              let info = data["measurement"] as? [String:Any], info["complete"] as? Bool == false,
              info["resume_allowed"] as? Bool != false,
              let id = info["id"] as? String, validMeasurementID(id),
              let measuredTarget = info["target"] as? String, measuredTarget.utf8.count <= 253,
              !measuredTarget.contains(where: { $0.isWhitespace || $0.isNewline || $0 == "\0" }) else { return nil }
        let original = info["input_target"] as? String ?? measuredTarget
        let normalized:(String)->String = { $0.trimmingCharacters(in:CharacterSet(charactersIn:".")).lowercased() }
        guard normalized(original) == normalized(input) || (previous?.target == input && previous?.canonicalTarget == measuredTarget) else { return nil }
        let regions = (info["regions"] as? [String] ?? []).prefix(6).filter { ["US","DE","SG","JP","AU","BR"].contains($0) }
        let requested = info["requested_probes"] as? Int ?? probeLimit
        let now = Date().timeIntervalSince1970
        let checked = (data["checked_at"] as? NSNumber)?.doubleValue ?? now
        let delay = (info["retry_after_seconds"] as? NSNumber)?.doubleValue ?? 0
        let retryUntil = checked.isFinite && abs(checked-now) <= 86400 && delay.isFinite && (0...86400).contains(delay) ? checked + delay : 0
        return Measurement(id:id,target:input,canonicalTarget:measuredTarget,kind:kind,regions:Array(NSOrderedSet(array:Array(regions))) as? [String] ?? [],probeLimit:[3,5,6].contains(requested) ? requested : probeLimit,retryUntil:retryUntil)
    }
    private func showPlain(_ text: String) {
        setDocument(Report(pieces:[Piece(text:text,style:0,link:nil)],document:text,state:stateField.stringValue,status:"info"))
        copyButton.isEnabled = false
    }
    private static func validMeasurementID(_ value:String) -> Bool {
        !value.isEmpty && value.utf8.count <= 100 && value.utf8.allSatisfy { (65...90).contains($0) || (97...122).contains($0) || (48...57).contains($0) || $0 == 45 || $0 == 95 }
    }
    private static func text(_ value: Any?, limit: Int = 1600) -> String {
        let value = (value as? String) ?? (value as? NSNumber)?.stringValue ?? "未提供"
        return String(value.unicodeScalars.filter { $0 == "\n" || $0 == "\t" || ($0.value >= 32 && $0.value != 127) }.prefix(limit))
    }
    private func presentCachedReport() {
        guard presentationVisible, !busy, let report = reports[selected] else { return }
        showReport(report)
    }
    private func showReport(_ report: Report) {
        stateField.stringValue = report.state
        stateField.textColor = report.status == "ok" || report.status == "success" ? .secondaryLabelColor : .systemOrange
        setDocument(report)
    }
    private func setDocument(_ report: Report) {
        guard displayedDocument != report.document else { return }
        displayedDocument = report.document; resultApplications += 1
        let document = NSMutableAttributedString(string: "")
        let paragraph = NSMutableParagraphStyle(); paragraph.lineSpacing = 4; paragraph.paragraphSpacing = 7
        for piece in report.pieces {
            let style = piece.style
            var attributes: [NSAttributedString.Key: Any] = [.font:NSFont.systemFont(ofSize:style == 1 ? 16 : style == 3 ? 10.5 : style == 4 ? 11 : 12.5,weight:style == 1 || style == 2 ? .semibold : .regular),.foregroundColor:style == 3 || style == 4 ? NSColor.secondaryLabelColor : NSColor.labelColor,.paragraphStyle:paragraph]
            if let link = piece.link { attributes[.link] = link; attributes[.foregroundColor] = NSColor.linkColor }
            document.append(NSAttributedString(string:piece.text,attributes:attributes))
        }
        results.textStorage?.setAttributedString(document); results.scrollRangeToVisible(NSRange(location:0,length:0))
        copyButton.isEnabled = !report.document.isEmpty
        lastLayout = nil; root.needsLayout = true
    }
    private static func prepare(_ data: [String: Any]) -> Report {
        let state = text(data["status"],limit:40)
        let labels = ["ok":"检测已完成","success":"检测已完成","partial":"部分结果 · 存在失败或未确认项","error":"本次检测未完成","cancelled":"已停止本机等待","rate_limited":"来源限流 · 未取得完整结果","unavailable":"来源暂不可用","inconclusive":"观测不足 · 不能下结论"]
        var label = labels[state] ?? "返回状态未确认"
        let metrics = data["metrics"] as? [String:Any] ?? [:]
        if let milliseconds = metrics["duration_ms"] as? NSNumber, milliseconds.doubleValue.isFinite, milliseconds.doubleValue >= 0 {
            label += " · " + String(format:"%.0f ms",milliseconds.doubleValue)
        }
        var pieces:[Piece] = [], count = 0, truncated = false
        let truncation = "\n结果已按界面显示上限截断；不代表检测覆盖完整。\n"
        let maximum = 98_304 - truncation.utf8.count - 3
        func add(_ value:String, style:Int = 0, link:URL? = nil) {
            guard count < maximum else { truncated = true; return }
            let allowed = maximum-count
            let clipped = value.utf8.count > allowed ? String(decoding:value.utf8.prefix(allowed),as:UTF8.self) : value
            count += clipped.utf8.count
            if clipped != value { truncated = true }
            pieces.append(Piece(text:clipped,style:style,link:link))
        }
        add(text(data["title"],limit:160)+"\n",style:1)
        add(label+"\n",style:3)
        add(text(data["summary"],limit:2400)+"\n\n")
        if let checked = data["checked_at"] {
            let timestamp:String
            if let number = checked as? NSNumber,number.doubleValue.isFinite,(0...4_000_000_000).contains(number.doubleValue) {
                let formatter = DateFormatter(); formatter.dateStyle = .medium; formatter.timeStyle = .medium
                timestamp = formatter.string(from:Date(timeIntervalSince1970:number.doubleValue))
            } else { timestamp = text(checked,limit:80) }
            add("检测时间："+timestamp+" · 单次观测\n\n",style:3)
        }
        if let measurement = data["measurement"] as? [String:Any], let id = measurement["id"] as? String, validMeasurementID(id) {
            add("公共测量："+id+" · "+((measurement["complete"] as? Bool) == true ? "已完成" : "尚未完整返回")+"\n",style:3)
            if let created = measurement["created_at"] ?? measurement["createdAt"] { add("测量创建时间："+text(created,limit:80)+"\n",style:3) }
            add("\n")
        }
        let rows = data["rows"] as? [[String:Any]] ?? []
        if rows.isEmpty { add("本次没有可核对的检测字段。\n\n",style:4) }
        for row in rows.prefix(100) {
            let status = text(row["status"],limit:40)
            let mark = ["ok":"✓ ","success":"✓ ","partial":"! ","error":"× ","unknown":"? ","warning":"! ","rate_limited":"! "][status] ?? ""
            add(mark+text(row["label"],limit:120)+"\n",style:2)
            add(text(row["value"],limit:2400)+"\n")
            if let detail = row["detail"] as? String,!detail.isEmpty { add(text(detail,limit:2400)+"\n",style:4) }
            add("\n")
        }
        if rows.count > 100 { truncated = true }
        if let sources = data["sources"] as? [[String:Any]],!sources.isEmpty {
            add("检测来源\n",style:2)
            for source in sources.prefix(12) {
                let name = text(source["name"],limit:160)
                if let raw = source["url"] as? String,let url = trustedSource(raw) { add(name+" ↗\n",link:url) }
                else { add(name+"（来源链接未核对）\n",style:4) }
            }
            add("\n")
        }
        if let notes = data["notes"] as? [String],!notes.isEmpty {
            add("范围与限制\n",style:2)
            for note in notes.prefix(16) { add("• "+text(note,limit:2400)+"\n",style:4) }
        }
        if truncated { pieces.append(Piece(text:truncation,style:4,link:nil)) }
        return Report(pieces:pieces,document:pieces.map(\.text).joined(),state:label,status:state)
    }
    private func render(_ data: [String:Any]) {
        let report = Self.prepare(data); reports[selected] = report
        showReport(report)
    }
    func textView(_ textView: NSTextView, clickedOnLink link: Any, at charIndex: Int) -> Bool {
        let raw = (link as? URL)?.absoluteString ?? (link as? String ?? "")
        if let trusted = Self.trustedSource(raw) { NSWorkspace.shared.open(trusted) }
        return true
    }
    @objc private func openWebRTC() { if let url = Self.trustedSource("https://browserleaks.com/webrtc") { NSWorkspace.shared.open(url) } }
    @objc private func openDNS() { if let url = Self.trustedSource("https://www.dnsleaktest.com/") { NSWorkspace.shared.open(url) } }
    func windowWillClose(_ notification: Notification) {
        stopEmbeddedWork(); reports.removeAll(); measurement = nil
        stateField.stringValue = "等待运行 · 不会自动连接检测服务"; stateField.textColor = .secondaryLabelColor
        showPlain("本窗口未保留上次检测结果。点击“运行检测”获取本次结果。")
    }

    static func validationReport() -> [String: Bool] {
        _ = NSApplication.shared
        let controller = Self(home: FileManager.default.temporaryDirectory.appendingPathComponent("vcgo-network-ui-fixture"))
        let initial = controller.generation == 0 && !controller.busy && controller.client == nil
        controller.target.stringValue = "example.com"; controller.selectKind("global_ping")
        let emptyPublicTarget = request(kind: .globalPing, target: "", probeLimit: 3, downloadMiB: 1) == nil
        controller.target.stringValue = "example.net"; controller.selectKind("ip_trace")
        let browserScope = !controller.browserWebRTC.isHidden && !controller.browserDNS.isHidden && !controller.target.isEnabled
        controller.selectKind("global_ping")
        let retainedInput = controller.target.stringValue == "example.net" && controller.client == nil
        controller.render(["kind": "global_ping", "status": "partial", "title": "样例检测", "summary": "部分节点未返回", "rows": [["label": "观测", "value": "未确认", "detail": "仅为受控样例"]], "sources": [["name": "Globalping", "url": "https://globalping.io/docs/api.globalping.io"]], "notes": ["未执行真实网络测量。"]])
        let rendered = controller.results.string.contains("部分节点未返回") && controller.results.string.contains("未确认") && controller.results.isSelectable && !controller.results.isEditable && controller.stateField.stringValue.contains("部分结果")
        controller.render(["kind": "global_ping", "status": "future-state", "title": "未知状态", "summary": "尚未核对", "rows": []])
        let unknown = controller.stateField.stringValue == "返回状态未确认"
        let priorGeneration = controller.generation
        controller.windowWillClose(Notification(name: NSWindow.willCloseNotification))
        let closeInvalidates = controller.generation == priorGeneration + 1 && !controller.results.string.contains("尚未核对")
        let embeddedView = controller.embeddedContentView()
        embeddedView.frame.size = NSSize(width: 760, height: 430)
        controller.selectKind("ip_trace"); controller.layout()
        let embeddedFit = controller.sidebar.isHidden && controller.titleField.isHidden && controller.target.isHidden
            && controller.resultScroll.frame.minX == 22 && controller.resultScroll.frame.height >= 100
            && controller.resultScroll.frame.maxY <= controller.browserWebRTC.frame.minY
            && controller.browserDNS.frame.maxX <= embeddedView.bounds.maxX
        let stoppedGeneration = controller.generation
        controller.stopEmbeddedWork()
        let worker = Self(home:FileManager.default.temporaryDirectory.appendingPathComponent("vcgo-network-worker-fixture"))
        worker.selectKind("global_ping"); worker.target.stringValue = "example.com"; worker.fixturePresentation = true
        var callback:((Result<[String:Any],Error>)->Void)?
        worker.fixtureRun = { _,completion in callback = completion }
        worker.runSelected()
        let stopAvailable = worker.busy && worker.stopButton.isEnabled && !worker.runButton.isEnabled && worker.client == nil
        let queuedGeneration = worker.generation; worker.stopSelected()
        callback?(.success(["schema_version":1,"kind":"global_ping","status":"ok","summary":"must-not-render-cancelled"]))
        let cancellation = worker.generation > queuedGeneration && !worker.busy && !worker.stopButton.isEnabled && !worker.results.string.contains("must-not-render-cancelled") && worker.stateField.stringValue.contains("已停止")
        let malformedApplications = worker.resultApplications
        worker.fixturePresentation = false
        worker.receive(.success(["schema_version":2,"kind":"global_http","status":"ok"]),generation:worker.generation,kind:.globalPing)
        let malformed = worker.resultApplications == malformedApplications && worker.reports[.globalPing]?.status == "error"
        worker.fixturePresentation = true; worker.presentCachedReport()
        let malformedVisible = worker.results.string.contains("接口版本不一致")

        let hidden = Self(home:FileManager.default.temporaryDirectory.appendingPathComponent("vcgo-network-hidden-fixture"))
        hidden.selectKind("global_ping"); hidden.target.stringValue = "example.com"; hidden.fixturePresentation = false
        var payloads:[[String:Any]] = [], hiddenCallbacks:[(Result<[String:Any],Error>)->Void] = []
        hidden.fixtureRun = { value, completion in payloads.append(value); hiddenCallbacks.append(completion) }
        hidden.runSelected(); let beforeHidden = hidden.resultApplications
        let partial:[String:Any] = ["schema_version":1,"kind":"global_ping","status":"partial","title":"公共探针","summary":"正在等待部分探针","checked_at":1_800_000_000,"metrics":["duration_ms":210],"rows":[["label":"日本","value":"未返回","status":"unknown"]],"measurement":["id":"fixture_measurement_123","target":"example.com","input_target":"example.com","regions":["US","DE","SG"],"requested_probes":3,"complete":false,"resume_allowed":true]]
        hiddenCallbacks[0](.success(partial))
        let deadline = Date().addingTimeInterval(2)
        while hidden.reportsPrepared == 0 && Date() < deadline { RunLoop.current.run(until:Date().addingTimeInterval(0.005)) }
        let hiddenCached = hidden.reportsPrepared == 1 && hidden.reports[.globalPing] != nil && hidden.resultApplications == beforeHidden
        hidden.fixturePresentation = true; hidden.presentCachedReport()
        let visible = hidden.results.string.contains("fixture_measurement_123") && hidden.stateField.stringValue.contains("210 ms") && !hidden.refreshMeasurementButton.isHidden
        let once = hidden.resultApplications; hidden.presentCachedReport(); hidden.presentCachedReport()
        let reused = hidden.resultApplications == once
        hidden.refreshMeasurement()
        let refreshReadOnly = payloads.count == 2 && payloads[1]["measurement_id"] as? String == "fixture_measurement_123" && payloads[1]["target"] as? String == "example.com" && hidden.measurementRefreshes == 1 && payloads[1]["regions"] as? [String] == ["US","DE","SG"] && payloads[1]["probe_limit"] as? Int == 3
        hidden.stopSelected(); hidden.target.stringValue = "new.example"
        hidden.controlTextDidChange(Notification(name:NSControl.textDidChangeNotification,object:hidden.target)); hidden.refreshMeasurement()
        let changedTarget = hidden.measurement == nil && payloads.count == 2 && hidden.results.string.contains("尚未对新目标")
        hidden.measurement = Measurement(id:"fixture",target:"new.example",canonicalTarget:"new.example",kind:.globalPing,regions:[],probeLimit:3,retryUntil:0); hidden.selectKind("global_http")
        let changedKind = hidden.measurement == nil
        let unicode = Self(home:FileManager.default.temporaryDirectory.appendingPathComponent("vcgo-network-idna-fixture"))
        unicode.selectKind("global_http"); unicode.target.stringValue = "例子.测试"; unicode.fixturePresentation = false
        var unicodePayloads:[[String:Any]] = []
        unicode.fixtureRun = { payload,_ in unicodePayloads.append(payload) }
        let unicodeValue:[String:Any] = ["schema_version":1,"kind":"global_http","status":"partial","measurement":["id":"unicode_measurement","target":"xn--fsqu00a.xn--0zwm56d","input_target":"例子.测试","regions":["JP","SG"],"requested_probes":3,"complete":false,"resume_allowed":true]]
        unicode.receive(.success(unicodeValue),generation:unicode.generation,kind:.globalHTTP)
        unicode.refreshMeasurement()
        let unicodeBound = unicodePayloads.first?["measurement_id"] as? String == "unicode_measurement" && unicodePayloads.first?["target"] as? String == "xn--fsqu00a.xn--0zwm56d" && unicodePayloads.first?["regions"] as? [String] == ["JP","SG"]
        unicode.stopEmbeddedWork()
        let receipt = Self(home:FileManager.default.temporaryDirectory.appendingPathComponent("vcgo-network-stop-receipt-fixture"))
        receipt.selectKind("global_ping"); receipt.target.stringValue = "example.com"; receipt.fixturePresentation = true
        var receiptPayloads:[[String:Any]] = [], receiptCallbacks:[(Result<[String:Any],Error>)->Void] = []
        receipt.fixtureRun = { payload,completion in receiptPayloads.append(payload); receiptCallbacks.append(completion) }
        receipt.runSelected(); receipt.stopSelected()
        var cancelled = partial; cancelled["status"] = "cancelled"; cancelled["summary"] = "must-not-restore-late-summary"; cancelled["rows"] = [["label":"late row","value":"must-not-restore-late-row"]]
        var lateSuccess = cancelled; lateSuccess["status"] = "ok"
        receiptCallbacks[0](.success(lateSuccess))
        let successIgnored = receipt.measurement == nil && receipt.acceptedStopReceipts == 0 && receipt.stateField.stringValue.contains("已停止") && !receipt.results.string.contains("must-not-restore")
        receiptCallbacks[0](.success(cancelled))
        let cancellationReceipt = receipt.measurement?.id == "fixture_measurement_123" && receipt.acceptedStopReceipts == 1 && receipt.stateField.stringValue.contains("已停止") && !receipt.refreshMeasurementButton.isHidden && !receipt.results.string.contains("must-not-restore")
        receipt.refreshMeasurement()
        let stoppedReadOnly = receiptPayloads.count == 2 && receiptPayloads.last?["measurement_id"] as? String == "fixture_measurement_123"
        receipt.stopSelected(); receipt.target.stringValue = "new.example"
        receipt.controlTextDidChange(Notification(name:NSControl.textDidChangeNotification,object:receipt.target))
        receiptCallbacks[1](.success(cancelled))
        let staleTargetReceipt = receipt.measurement == nil && receipt.results.string.contains("尚未对新目标")
        var newCancelled = cancelled; var newInformation = newCancelled["measurement"] as! [String:Any]
        newInformation["input_target"] = "new.example"; newInformation["target"] = "new.example"; newInformation["id"] = "new_measurement"
        newCancelled["measurement"] = newInformation
        receipt.runSelected(); receipt.stopSelected(); let stoppedCallback = receiptCallbacks.last
        receipt.runSelected(); stoppedCallback?(.success(newCancelled))
        let staleRunReceipt = receipt.busy && receipt.measurement == nil && receipt.results.string.contains("正在检测")
        receipt.stopSelected(); let departedCallback = receiptCallbacks.last; receipt.stopEmbeddedWork()
        departedCallback?(.success(newCancelled))
        let staleNavigationReceipt = receipt.measurement == nil && !receipt.active
        let limited = Self(home:FileManager.default.temporaryDirectory.appendingPathComponent("vcgo-network-retry-after-fixture"))
        limited.selectKind("global_ping"); limited.target.stringValue = "example.com"; limited.fixturePresentation = true
        var limitedPayloads:[[String:Any]] = []
        limited.fixtureRun = { payload,_ in limitedPayloads.append(payload) }
        var rate = partial; var rateMeasurement = rate["measurement"] as! [String:Any]
        rateMeasurement["retry_after_seconds"] = 600; rate["measurement"] = rateMeasurement; rate["checked_at"] = Date().timeIntervalSince1970
        limited.measurement = Self.measurementHandle(rate,kind:.globalPing,input:"example.com",previous:nil,probeLimit:3)
        limited.refreshMeasurement()
        let rateWaited = limitedPayloads.isEmpty && limited.stateField.stringValue.contains("来源限流")
        if let value = limited.measurement { limited.measurement = Measurement(id:value.id,target:value.target,canonicalTarget:value.canonicalTarget,kind:value.kind,regions:value.regions,probeLimit:value.probeLimit,retryUntil:0) }
        limited.refreshMeasurement()
        let manualRetry = limitedPayloads.count == 1 && limitedPayloads[0]["measurement_id"] as? String == "fixture_measurement_123"
        limited.stopEmbeddedWork()
        let lifecycle = Self(home:FileManager.default.temporaryDirectory.appendingPathComponent("vcgo-network-lifecycle-fixture"))
        lifecycle.selectKind("ip_trace"); lifecycle.fixturePresentation = true; lifecycle.stopEmbeddedWork()
        let lifecycleApplications = lifecycle.resultApplications, lifecycleRequests = lifecycle.requestsStarted
        lifecycle.reports[.ipTrace] = Self.prepare(["status":"partial","title":"出口痕迹","summary":"cache restored after panel hide","rows":[]])
        lifecycle.activateEmbedded()
        let restoredLifecycle = lifecycle.active && lifecycle.results.string.contains("cache restored after panel hide") && lifecycle.resultApplications == lifecycleApplications + 1 && lifecycle.requestsStarted == lifecycleRequests && lifecycle.client == nil
        lifecycle.stopEmbeddedWork()
        let huge = Self.prepare(["status":"rate_limited","title":"限流","summary":"未取得完整结果","rows":(0..<120).map { ["label":"source \($0)","value":String(repeating:"长文本🧪",count:1000),"detail":String(repeating:"detail",count:1000),"status":"unknown"] }])
        let capped = huge.document.utf8.count <= 98_304 && huge.document.contains("上限截断") && huge.state.contains("限流")
        let idle = Self(home:FileManager.default.temporaryDirectory.appendingPathComponent("vcgo-network-layout-fixture"))
        _ = idle.embeddedContentView(); idle.root.frame.size = NSSize(width:760,height:460); idle.layout()
        let layoutCount = idle.layoutPasses, applicationCount = idle.resultApplications
        for _ in 0..<100 { idle.layout(); idle.setBusy(false) }
        let memoized = idle.layoutPasses == layoutCount && idle.resultApplications == applicationCount
        idle.root.frame.size.width += 40; idle.layout()
        let resized = idle.layoutPasses == layoutCount + 1 && idle.copyButton.frame.maxX <= idle.root.bounds.width
        worker.stopEmbeddedWork(); hidden.stopEmbeddedWork(); idle.stopEmbeddedWork()
        return [
            "network_stop_is_explicit_and_invalidates_late_completions":stopAvailable && cancellation,
            "network_stop_receipt_keeps_only_same_operation_measurement_identity":successIgnored && cancellationReceipt && stoppedReadOnly,
            "network_stop_receipt_cannot_revive_after_new_target_run_or_navigation":staleTargetReceipt && staleRunReceipt && staleNavigationReceipt,
            "network_measurement_retry_after_uses_click_time_guard_without_polling":rateWaited && manualRetry,
            "network_wrong_protocol_is_cached_without_hidden_textkit_apply":malformed && malformedVisible,
            "network_occluded_completion_prepares_bounded_report_without_rendering":hiddenCached,
            "network_visibility_restores_same_cached_report_without_new_requests":visible && reused,
            "network_incomplete_measurement_refresh_reuses_identity_without_resubmission":refreshReadOnly,
            "network_measurement_refresh_preserves_canonical_idna_target_and_original_regions":unicodeBound,
            "network_measurement_handles_and_previous_result_clear_when_target_or_kind_changes":changedTarget && changedKind,
            "network_unicode_report_total_is_bounded_and_rate_limit_remains_unknown":capped,
            "network_idle_layout_has_no_repeated_geometry_or_text_replacement":memoized && resized,
            "network_has_no_additional_polling_jobs":worker.performanceSnapshot["scheduled_jobs"] as? Int == 0,
            "network_panel_reactivation_consumes_cached_report_without_network":restoredLifecycle,
            "network_window_initialization_and_category_changes_never_start_requests": initial && controller.client == nil,
            "network_public_measurements_require_an_explicit_target": emptyPublicTarget,
            "network_browser_checks_are_manual_and_scoped_to_browser": browserScope,
            "network_switch_preserves_only_in_memory_target_drafts": retainedInput,
            "network_results_preserve_partial_and_unknown_outcomes": rendered && unknown,
            "network_close_invalidates_pending_callbacks_and_clears_results": closeInvalidates,
            "network_source_links_require_exact_trusted_https_urls": trustedSource("https://globalping.io/docs/api.globalping.io") != nil && trustedSource("https://globalping.io.evil.example/docs/api.globalping.io") == nil && trustedSource("http://globalping.io/docs/api.globalping.io") == nil && trustedSource("https://globalping.io/docs/api.globalping.io?target=private") == nil,
            "network_request_limits_are_bounded_without_mutating_preferences": validRequestLimits(),
            "network_readonly_result_document_has_positive_viewport_height": controller.resultScroll.contentSize.height > 0 && controller.results.frame.height >= controller.resultScroll.contentSize.height,
            "network_embedded_view_uses_shell_tags_and_compact_layout": embeddedFit && controller.window?.contentView !== embeddedView,
            "network_leaving_embedded_module_stops_only_its_current_worker": controller.generation == stoppedGeneration + 1 && controller.client == nil && !controller.busy
        ]
    }
    private static func validRequestLimits() -> Bool {
        guard let value = request(kind: .speed, target: "ignored.example", probeLimit: 1000, downloadMiB: 1000) else { return false }
        return value["probe_limit"] as? Int == 3 && value["download_bytes"] as? Int == 1_048_576 && value["target"] as? String == ""
            && CueNetworkTestPreferences.validProbeLimit(6) == 6 && CueNetworkTestPreferences.validDownloadMiB(5) == 5
    }
}
