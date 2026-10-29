import AppKit

/// Read-only numeric metadata. No prompt, response, credential, or raw payload.
final class CueUsageLogDetailsController: NSWindowController {
    private let content = NSTextView()
    private var threadID = ""
    private let openButton = NSButton(title: "在 Codex 中打开对话", target: nil, action: nil)
    init(row: [String: Any], currency: String, title: String = "请求详情") {
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 620, height: 650), styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.title = title; window.minSize = NSSize(width: 520, height: 460); window.isReleasedWhenClosed = false; window.center()
        super.init(window: window)
        let root = CueUsageDetailSurface(frame: window.contentLayoutRect); window.contentView = root
        let scroll = NSScrollView(frame: NSRect(x: 20, y: 18, width: 580, height: 568)); scroll.autoresizingMask = [.width, .height]
        scroll.hasVerticalScroller = true; scroll.borderType = .noBorder; scroll.drawsBackground = false
        content.frame = NSRect(x: 0, y: 0, width: 566, height: 568); content.isEditable = false; content.isSelectable = true; content.isRichText = true; content.drawsBackground = false
        content.isVerticallyResizable = true; content.isHorizontallyResizable = false; content.textContainer?.widthTracksTextView = true
        content.textContainerInset = NSSize(width: 4, height: 7); scroll.documentView = content; root.addSubview(scroll)
        openButton.frame = NSRect(x: 390, y: 603, width: 210, height: 30); openButton.autoresizingMask = [.minXMargin, .minYMargin]
        openButton.bezelStyle = .rounded; openButton.target = self; openButton.action = #selector(openThread); root.addSubview(openButton)
        let string = NSMutableAttributedString()
        func text(_ key: String) -> String { (row[key] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "—" }
        func add(_ label: String, _ value: String) {
            let paragraph = NSMutableParagraphStyle(); paragraph.paragraphSpacing = 4
            string.append(NSAttributedString(string: label + "\n", attributes: [.font: NSFont.systemFont(ofSize: 11, weight: .medium), .foregroundColor: NSColor.secondaryLabelColor]))
            string.append(NSAttributedString(string: value + "\n\n", attributes: [.font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .regular), .foregroundColor: NSColor.labelColor, .paragraphStyle: paragraph]))
        }
        let date = DateFormatter(); date.dateFormat = "yyyy-MM-dd HH:mm:ss"
        add("时间", (row["timestamp"] as? NSNumber).map { date.string(from: Date(timeIntervalSince1970: $0.doubleValue)) } ?? "—")
        add("应用 / 模型", text("app") + " / " + text("model"))
        for (key, label) in [("requested_model", "请求模型"), ("pricing_model", "计价模型")] { if let model = row[key] as? String, !model.isEmpty, model != row["model"] as? String { add(label, model) } }
        add("供应商 / 来源", (row["provider_name"] as? String ?? "会话日志（供应商未记录）") + " / " + text("source"))
        if let provider = row["provider_id"] as? String, !provider.isEmpty { add("供应商标识", provider) }
        add("API 参考费用", CueUsageMoney.display(row, currency: currency) + ((row["unpriced_requests"] as? NSNumber)?.doubleValue ?? 0 > 0 ? " · 含未定价记录" : ""))
        for (key, label) in [("input_cost", "费用拆分 · 新增输入"), ("output_cost", "费用拆分 · 输出"), ("cache_read_cost", "费用拆分 · 缓存读取"), ("cache_creation_cost", "费用拆分 · 缓存写入")] {
            var amount: [String: Any] = [:]
            if let value = row[key + "_usd"] as? NSNumber { amount["cost_usd"] = value }
            if let value = row[key + "_cny"] as? NSNumber { amount["cost_cny"] = value }
            add(label, amount.isEmpty ? "—（未记录或未定价）" : CueUsageMoney.display(amount, currency: currency))
        }
        for (key, label) in [("input_tokens", "新增输入 Tokens"), ("output_tokens", "输出 Tokens"), ("cache_read_tokens", "缓存读取 Tokens"), ("cache_creation_tokens", "缓存写入 Tokens"), ("total_tokens", "总 Tokens")] {
            add(label, (row[key] as? NSNumber).map { CueUsageDashboardController.integer($0.doubleValue) } ?? "—")
        }
        add("生成速度", Self.performance(row, key: "speed_tps")); add("首字时间", Self.performance(row, key: "first_token_ms")); add("总耗时", Self.performance(row, key: "duration_ms"))
        add("HTTP 状态", (row["status_code"] as? NSNumber)?.stringValue ?? "未记录（不推断为成功）")
        add("性能数据口径", row["timing_estimated"] as? Bool == true ? "≈ 会话日志估算，包含日志时序间隔；并非供应商实测计费。" : "仅显示本地日志保留的性能字段；缺失项显示 —。")
        for (key, label) in [("price_source_name", "单价来源"), ("provenance", "记录来源"), ("origin_request_id", "原始请求标识"), ("thread_id", "会话标识"), ("id", "本地记录标识")] { if let value = row[key] as? String, !value.isEmpty { add(label, value) } }
        content.textStorage?.setAttributedString(string)
        content.minSize = NSSize(width: 0, height: scroll.contentSize.height); content.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        content.autoresizingMask = .width
        if let container = content.textContainer { content.layoutManager?.ensureLayout(for: container); let height = (content.layoutManager?.usedRect(for: container).height ?? 0) + 20; content.setFrameSize(NSSize(width: scroll.contentSize.width, height: max(scroll.contentSize.height, height))) }
        let candidate = row["thread_id"] as? String ?? ""
        if row["app"] as? String == "codex", candidate.range(of: #"^[A-Za-z0-9_-]{1,160}$"#, options: .regularExpression) != nil { threadID = candidate }
        openButton.isHidden = threadID.isEmpty
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func showWindow(_ sender: Any?) { super.showWindow(sender); window?.makeKeyAndOrderFront(nil) }
    @objc private func openThread() { guard !threadID.isEmpty, let url = URL(string: "codex://threads/" + threadID) else { return }; NSWorkspace.shared.open(url) }
    static func performance(_ row: [String: Any], key: String) -> String {
        guard let raw = row[key] as? NSNumber, raw.doubleValue.isFinite, raw.doubleValue >= 0 else { return "—" }
        let prefix = row["timing_estimated"] as? Bool == true ? "≈" : ""
        if key == "speed_tps" { return prefix + String(format: "%.1f tok/s", raw.doubleValue) }
        return prefix + String(format: "%.2f 秒", raw.doubleValue / 1000)
    }
    static func validationReport() -> [String: Bool] {
        ["usage_missing_performance_stays_unknown": performance([:], key: "speed_tps") == "—",
         "usage_estimated_session_performance_is_marked": performance(["duration_ms": 1234, "timing_estimated": true], key: "duration_ms") == "≈1.23 秒",
         "usage_http_status_never_inferred_from_duration": performance(["duration_ms": 1000], key: "status_code") == "—"]
    }
    static func captureFixture(to url: URL, dark: Bool) throws {
        let controller = CueUsageLogDetailsController(row: ["app": "codex", "model": "gpt-6.1-sol", "source": "codex_session", "provider_name": "会话日志", "timestamp": 1793239200.0, "input_tokens": 4702, "output_tokens": 502, "cache_read_tokens": 169728, "cache_creation_tokens": 0, "total_tokens": 174932, "cost_usd": 0.0454, "cost_cny": 0.3224, "first_token_ms": 1400, "duration_ms": 14200, "speed_tps": 35.3, "timing_estimated": true, "provenance": "Codex 会话时序（估算）", "thread_id": "offline-fixture"], currency: "CNY")
        controller.window?.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        guard let root = controller.window?.contentView, let bitmap = root.bitmapImageRepForCachingDisplay(in: root.bounds) else { throw CocoaError(.fileWriteUnknown) }
        root.effectiveAppearance.performAsCurrentDrawingAppearance { root.cacheDisplay(in: root.bounds, to: bitmap) }
        guard let png = bitmap.representation(using: .png, properties: [:]) else { throw CocoaError(.fileWriteUnknown) }; try png.write(to: url)
    }
}
private final class CueUsageDetailSurface: NSView {
    override var isFlipped: Bool { true }
    override func draw(_ dirtyRect: NSRect) { NSColor.windowBackgroundColor.setFill(); bounds.fill() }
}
