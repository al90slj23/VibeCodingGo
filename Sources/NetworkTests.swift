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
        case .ipReputation: return "留空时由 ipify 识别当前出口；待查 IP 会发送给 ipapi.is 和 proxycheck.io。风险字段来自来源数据库，不能保证所有平台都接受此 IP。"
        case .domain: return "运行后会查询输入域名的 DNS，并访问其 HTTPS 地址。单次失败或地区差异只能作为信号，不能单独证明域名被封禁。"
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
    override func draw(_ dirtyRect: NSRect) { fillColor.setFill(); dirtyRect.fill() }
}

/// Explicit, one-shot diagnostics. Constructing or navigating the window does no I/O.
final class CueNetworkTestsController: NSWindowController, NSWindowDelegate, NSTextViewDelegate {
    private let home: URL
    private var client: CueUsageClient?
    private let root = CueNetworkTestSurface(), sidebar = CueNetworkTestSurface()
    private let titleField = NSTextField(labelWithString: "")
    private let detailField = NSTextField(wrappingLabelWithString: "")
    private let targetLabel = NSTextField(labelWithString: "检测目标")
    private let target = NSTextField(string: "")
    private let runButton = NSButton(title: "运行检测", target: nil, action: nil)
    private let spinner = NSProgressIndicator()
    private let stateField = NSTextField(labelWithString: "等待运行")
    private let disclosure = NSTextField(wrappingLabelWithString: "")
    private let resultScroll = NSScrollView(), results = NSTextView()
    private let browserWebRTC = NSButton(title: "浏览器 WebRTC 检测 ↗", target: nil, action: nil)
    private let browserDNS = NSButton(title: "浏览器 DNS 检测 ↗", target: nil, action: nil)
    private let footer = NSTextField(labelWithString: "本机不保存检测结果 · 可选择并复制文字")
    private var categoryButtons: [NSButton] = []
    private var selected: CueNetworkTestKind = .ipReputation
    private var drafts: [String: String] = [:]
    private var generation = 0
    private var busy = false
    private var embedded = false

    // Documentation and manually opened browser tests only. Never trust a URL
    // supplied by a diagnostic response unless it exactly matches this list.
    private static let sourceURLs: Set<String> = [
        "https://www.ipify.org/", "https://ipapi.is/free-tier.html", "https://proxycheck.io/api/",
        "https://globalping.io/docs/api.globalping.io", "https://github.com/cloudflare/speedtest",
        "https://browserleaks.com/webrtc", "https://www.dnsleaktest.com/",
        "https://www.rfc-editor.org/rfc/rfc8828"
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
        target.identifier = NSUserInterfaceItemIdentifier("network.target"); target.setAccessibilityLabel("网络检测目标")
        runButton.target = self; runButton.action = #selector(runSelected); runButton.bezelStyle = .rounded
        runButton.identifier = NSUserInterfaceItemIdentifier("network.run")
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
        [titleField, detailField, targetLabel, target, runButton, spinner, stateField, disclosure, resultScroll, browserWebRTC, browserDNS, footer].forEach { root.addSubview($0) }
        root.onLayout = { [weak self] in self?.layout() }
        applySelection(); layout()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    deinit { client?.stop() }

    func selectKind(_ kind: String) {
        let canonical = kind == "egress" ? "ip_trace" : kind == "download" ? "speed" : kind
        guard let next = CueNetworkTestKind(rawValue: canonical), next != selected else { return }
        drafts[selected.rawValue] = target.stringValue
        generation += 1; client?.stop(); client = nil; busy = false
        selected = next; applySelection(); layout()
    }
    /// Main-shell navigation supplies the module and category tags. Detach
    /// this native view from the hidden compatibility window without showing it.
    func embeddedContentView() -> NSView {
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
        return root
    }

    /// Call on leaving this module or closing the main panel, not on redraw.
    func stopEmbeddedWork() {
        drafts[selected.rawValue] = target.stringValue
        let wasBusy = busy
        generation += 1; client?.stop(); client = nil; setBusy(false)
        if wasBusy {
            stateField.stringValue = "已停止本机等待 · 再次运行可获取新结果"
            stateField.textColor = .secondaryLabelColor
            showPlain("本次检测尚未完成。已提交的公共测量可能继续；这里不会自动重新发送。")
        }
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
        target.frame = NSRect(x: x, y: targetY, width: max(120, available - 140), height: 29)
        runButton.frame = NSRect(x: x + available - 125, y: noInput ? 57 : targetY - 2, width: 125, height: 33)
        spinner.frame = NSRect(x: x, y: stateY + 2, width: 16, height: 16)
        stateField.frame = NSRect(x: x + (busy ? 23 : 1), y: stateY, width: available - 24, height: 20)
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
        target.isEnabled = !value && selected.acceptsTarget
        if value { spinner.startAnimation(nil) } else { spinner.stopAnimation(nil) }
        layout()
    }
    private static func request(kind: CueNetworkTestKind, target: String, probeLimit: Int, downloadMiB: Int) -> [String: Any]? {
        let value = target.trimmingCharacters(in: .whitespacesAndNewlines)
        guard value.utf8.count <= 2048, !value.contains(where: { $0.isNewline || $0 == "\0" }), !kind.requiresTarget || !value.isEmpty else { return nil }
        return ["kind": kind.rawValue, "target": kind.acceptsTarget ? value : "", "probe_limit": CueNetworkTestPreferences.validProbeLimit(probeLimit), "download_bytes": CueNetworkTestPreferences.validDownloadMiB(downloadMiB) * 1_048_576]
    }
    @objc private func runSelected() {
        guard !busy else { return }
        guard let payload = Self.request(kind: selected, target: target.stringValue, probeLimit: CueNetworkTestPreferences.probeLimit, downloadMiB: CueNetworkTestPreferences.downloadMiB),
              let input = try? JSONSerialization.data(withJSONObject: payload) else {
            stateField.stringValue = selected.requiresTarget ? "请先填写检测目标；目标格式还会由检测模块核对。" : "目标输入过长或含有无效字符。"
            stateField.textColor = .systemOrange; return
        }
        drafts[selected.rawValue] = target.stringValue
        generation += 1; let expectedGeneration = generation, expectedKind = selected
        client?.stop(); let worker = CueUsageClient(home: home); client = worker
        setBusy(true); stateField.stringValue = "正在运行 · 等待所选服务返回结果"; stateField.textColor = .secondaryLabelColor
        showPlain("正在检测…\n\n本次只执行当前选择的项目。关闭窗口或切换项目会停止本机等待。")
        worker.run(["run"], module: "vibecue.network_tests", input: input) { [weak self] result in
            guard let self, self.generation == expectedGeneration, self.selected == expectedKind else { return }
            self.setBusy(false); self.client = nil
            switch result {
            case .success(let data):
                guard data["kind"] as? String == expectedKind.rawValue else {
                    self.stateField.stringValue = "结果与所选检测不一致，未显示旧结果。"; self.stateField.textColor = .systemOrange
                    self.showPlain("本次结果无法核对，请重新运行。"); return
                }
                self.render(data)
            case .failure:
                self.stateField.stringValue = "本次检测未完成"; self.stateField.textColor = .systemOrange
                self.showPlain("检测服务暂不可用或本机检测模块未完成。\n\n可以稍后重试；未将历史结果显示为当前检测。")
            }
        }
    }
    private func showPlain(_ text: String) {
        results.textStorage?.setAttributedString(NSAttributedString(string: text, attributes: [.font: NSFont.systemFont(ofSize: 12.5), .foregroundColor: NSColor.labelColor]))
        results.scrollRangeToVisible(NSRange(location: 0, length: 0))
    }
    private static func text(_ value: Any?, limit: Int = 1600) -> String {
        if let value = value as? String { return String(value.prefix(limit)) }
        if let value = value as? NSNumber { return String(value.stringValue.prefix(limit)) }
        return "未提供"
    }
    private func render(_ data: [String: Any]) {
        let state = data["status"] as? String ?? "unknown"
        let label: String
        switch state {
        case "ok": label = "检测已完成"
        case "partial": label = "部分结果 · 尚有未确认项"
        case "error": label = "本次检测未完成"
        default: label = "返回状态未确认"
        }
        stateField.stringValue = label; stateField.textColor = state == "ok" ? .secondaryLabelColor : .systemOrange
        let document = NSMutableAttributedString(string: "")
        let paragraph = NSMutableParagraphStyle(); paragraph.lineSpacing = 4; paragraph.paragraphSpacing = 7
        func append(_ text: String, size: CGFloat = 12, weight: NSFont.Weight = .regular, color: NSColor = .labelColor, link: URL? = nil) {
            var attributes: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: size, weight: weight), .foregroundColor: color, .paragraphStyle: paragraph]
            if let link { attributes[.link] = link }
            document.append(NSAttributedString(string: text, attributes: attributes))
        }
        append(Self.text(data["title"], limit: 160) + "\n", size: 16, weight: .semibold)
        append(Self.text(data["summary"], limit: 2400) + "\n\n", size: 12.5)
        if let checked = data["checked_at"] {
            let timestamp: String
            if let number = checked as? NSNumber, number.doubleValue.isFinite, (0...4_000_000_000).contains(number.doubleValue) {
                let formatter = DateFormatter(); formatter.dateStyle = .medium; formatter.timeStyle = .medium
                timestamp = formatter.string(from: Date(timeIntervalSince1970: number.doubleValue))
            } else { timestamp = Self.text(checked, limit: 80) }
            append("检测时间：" + timestamp + "\n\n", size: 10.5, color: .secondaryLabelColor)
        }
        let rows = data["rows"] as? [[String: Any]] ?? []
        if rows.isEmpty { append("本次没有可核对的检测字段。\n\n", color: .secondaryLabelColor) }
        for row in rows.prefix(64) {
            append(Self.text(row["label"], limit: 120) + "\n", weight: .semibold)
            append(Self.text(row["value"]) + "\n", size: 12.5)
            if let detail = row["detail"] as? String, !detail.isEmpty { append(String(detail.prefix(2400)) + "\n", size: 11, color: .secondaryLabelColor) }
            append("\n")
        }
        if let sources = data["sources"] as? [[String: Any]], !sources.isEmpty {
            append("检测来源\n", weight: .semibold)
            for source in sources.prefix(12) {
                let name = Self.text(source["name"], limit: 160)
                if let raw = source["url"] as? String, let url = Self.trustedSource(raw) { append(name + " ↗\n", color: .linkColor, link: url) }
                else { append(name + "（来源链接未核对）\n", size: 11, color: .secondaryLabelColor) }
            }
            append("\n")
        }
        if let notes = data["notes"] as? [String], !notes.isEmpty {
            append("范围与限制\n", weight: .semibold)
            for note in notes.prefix(16) { append("• " + String(note.prefix(2400)) + "\n", size: 11, color: .secondaryLabelColor) }
        }
        results.textStorage?.setAttributedString(document); results.scrollRangeToVisible(NSRange(location: 0, length: 0))
    }
    func textView(_ textView: NSTextView, clickedOnLink link: Any, at charIndex: Int) -> Bool {
        let raw = (link as? URL)?.absoluteString ?? (link as? String ?? "")
        if let trusted = Self.trustedSource(raw) { NSWorkspace.shared.open(trusted) }
        return true
    }
    @objc private func openWebRTC() { if let url = Self.trustedSource("https://browserleaks.com/webrtc") { NSWorkspace.shared.open(url) } }
    @objc private func openDNS() { if let url = Self.trustedSource("https://www.dnsleaktest.com/") { NSWorkspace.shared.open(url) } }
    func windowWillClose(_ notification: Notification) {
        stopEmbeddedWork()
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
        return [
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
