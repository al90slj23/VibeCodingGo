import AppKit
import Foundation
import CoreFoundation

struct CueTaskMessage {
    let role: String
    let text: String
    let date: Date?
}

/// Bounded, in-memory telemetry. Missing fields remain unknown, never zero.
struct CueTaskDetail {
    let status: String
    let turnID: String
    let startedAt: Date?
    let finishedAt: Date?
    let lastActivity: Date?
    let elapsedSeconds: Double?
    let elapsedStatus: String
    let model: String
    let reasoningEffort: String
    let serviceTier: String
    let totalTokens: Int64?
    let inputTokens: Int64?
    let outputTokens: Int64?
    let cacheReadTokens: Int64?
    let cacheCreationTokens: Int64?
    let tokensStatus: String
    let messages: [CueTaskMessage]
    let lastMessage: String
    let lastMessageAt: Date?
    let lastRole: String
    let contentTruncated: Bool

    init(raw: [String: Any]) {
        status = Self.label(raw["status"], limit: 40, fallback: "unknown")
        turnID = Self.label(raw["turn_id"], limit: 160)
        startedAt = Self.date(raw["started_at"])
        finishedAt = Self.date(raw["finished_at"])
        lastActivity = Self.date(raw["last_activity"])
        elapsedSeconds = Self.number(raw["elapsed_seconds"])
        elapsedStatus = Self.label(raw["elapsed_status"], limit: 40, fallback: "unknown")
        model = Self.label(raw["model"], limit: 180)
        reasoningEffort = Self.label(raw["reasoning_effort"], limit: 40)
        serviceTier = Self.label(raw["service_tier"], limit: 40)
        totalTokens = Self.integer(raw["total_tokens"])
        inputTokens = Self.integer(raw["input_tokens"])
        outputTokens = Self.integer(raw["output_tokens"])
        cacheReadTokens = Self.integer(raw["cache_read_tokens"])
        cacheCreationTokens = Self.integer(raw["cache_creation_tokens"])
        let declared = Self.label(raw["tokens_status"], limit: 40)
        tokensStatus = totalTokens == nil ? "unknown" : ["complete", "partial"].contains(declared) ? declared : "partial"
        lastMessage = Self.text(raw["last_message"], limit: 12_000)
        lastMessageAt = Self.date(raw["last_message_at"])
        lastRole = Self.label(raw["last_role"], limit: 30)
        var result: [CueTaskMessage] = []
        var truncated = raw["content_truncated"] as? Bool == true
        if (raw["last_message"] as? String ?? "").count > 12_000 { truncated = true }
        var budget = 12_000
        let rows = raw["messages"] as? [[String: Any]] ?? []
        if rows.count > 8 { truncated = true }
        for row in rows.suffix(8).reversed() {
            guard let role = row["role"] as? String, ["user", "assistant"].contains(role) else { continue }
            let value = Self.text(row["text"], limit: 12_000)
            if (row["text"] as? String ?? "").count > 12_000 { truncated = true }
            guard !value.isEmpty, budget > 0 else { continue }
            let kept = String(value.suffix(budget))
            if kept.count < value.count { truncated = true }
            budget -= kept.count
            result.append(CueTaskMessage(role: role, text: kept, date: Self.date(row["created_at"])))
        }
        result.reverse()
        if result.isEmpty, ["user", "assistant"].contains(lastRole), !lastMessage.isEmpty {
            result = [CueTaskMessage(role: lastRole, text: lastMessage, date: lastMessageAt)]
        }
        messages = result
        contentTruncated = truncated
    }

    var isActive: Bool { ["running", "waiting"].contains(status) }
    var startDate: Date? { startedAt }
    var finishedDate: Date? { finishedAt }

    func elapsed(now: Date) -> TimeInterval? {
        if let start = startedAt {
            if isActive { return max(0, now.timeIntervalSince(start)) }
            if let end = finishedAt, end >= start { return end.timeIntervalSince(start) }
        }
        return elapsedSeconds
    }

    func summaryLine(now: Date = Date(), eventDate: Date? = nil) -> String {
        let scope: String
        if let eventDate {
            scope = startedAt.map { eventDate < $0.addingTimeInterval(-1) } ?? true ? "最新轮" : "本轮"
        } else { scope = isActive ? "本轮" : "最新轮" }
        let duration = elapsed(now: now).map { (elapsedStatus == "partial" ? "≥" : "") + Self.duration($0) } ?? "耗时待确认"
        let tokens: String
        if let total = totalTokens, tokensStatus != "unknown" {
            tokens = (tokensStatus == "partial" ? "≥" : "") + Self.compact(total) + " Tokens" + (tokensStatus == "partial" ? "（部分）" : "")
        } else { tokens = "Tokens 待同步" }
        return "\(scope) · \(duration) · \(tokens)"
    }

    var secondaryLine: String {
        var fields: [String] = []
        if !model.isEmpty, model != "unknown" { fields.append(model) }
        let effortNames = ["none": "无推理", "minimal": "极低强度", "low": "低强度", "medium": "中等强度", "high": "高强度", "xhigh": "特高强度", "max": "最高强度", "ultra": "Ultra 强度", "default": "默认强度"]
        if !reasoningEffort.isEmpty, reasoningEffort != "unknown" { fields.append(effortNames[reasoningEffort] ?? reasoningEffort) }
        if ["fast", "priority"].contains(serviceTier) { fields.append("闪电 Fast") }
        else if serviceTier == "ultrafast" { fields.append("极速 Ultra Fast") }
        else if ["default", "standard"].contains(serviceTier) { fields.append("标准速度") }
        else if !serviceTier.isEmpty, serviceTier != "unknown" { fields.append(serviceTier) }
        return fields.isEmpty ? "模型设置待同步" : fields.joined(separator: " · ")
    }

    var tokenBreakdown: String {
        func value(_ value: Int64?) -> String { value.map(Self.exact) ?? "—" }
        return "新增输入 \(value(inputTokens)) · 输出 \(value(outputTokens))\n缓存读取 \(value(cacheReadTokens)) · 缓存写入 \(value(cacheCreationTokens))"
    }

    static func exact(_ value: Int64) -> String {
        let formatter = NumberFormatter(); formatter.numberStyle = .decimal
        formatter.maximumFractionDigits = 0
        return formatter.string(from: NSNumber(value: value)) ?? String(value)
    }
    private static func compact(_ value: Int64) -> String {
        for (suffix, scale) in [("B", 1_000_000_000.0), ("M", 1_000_000.0), ("K", 1_000.0)] {
            if Double(value) >= scale { return String(format: "%.1f%@", Double(value) / scale, suffix) }
        }
        return exact(value)
    }
    private static func duration(_ seconds: TimeInterval) -> String {
        let total = Int(min(315_360_000, max(0, seconds)))
        if total >= 3600 { return "\(total / 3600)时\((total % 3600) / 60)分" }
        if total >= 60 { return "\(total / 60)分\(total % 60)秒" }
        return "\(total)秒"
    }
    private static func number(_ raw: Any?) -> Double? {
        guard let value = raw as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID() else { return nil }
        let result = value.doubleValue
        return result.isFinite && result >= 0 && result <= 9_000_000_000_000_000 ? result : nil
    }
    private static func integer(_ raw: Any?) -> Int64? {
        guard let value = number(raw), value.rounded(.towardZero) == value else { return nil }
        return Int64(value)
    }
    private static func date(_ raw: Any?) -> Date? {
        if let value = number(raw), value > 0 && value < 253_402_300_799 { return Date(timeIntervalSince1970: value) }
        guard let value = raw as? String else { return nil }
        let formatter = ISO8601DateFormatter(); formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    }
    private static func label(_ raw: Any?, limit: Int, fallback: String = "") -> String {
        guard let value = raw as? String else { return fallback }
        return String(value.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ").prefix(limit))
    }
    private static func text(_ raw: Any?, limit: Int) -> String {
        guard let value = raw as? String else { return "" }
        let cleaned = String(value.unicodeScalars.filter { $0 == "\n" || $0 == "\t" || !CharacterSet.controlCharacters.contains($0) })
        return String(cleaned.suffix(limit)).trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

private final class CueTaskHoverPanel: NSPanel {
    override var canBecomeMain: Bool { false }
    override var canBecomeKey: Bool { true }
}

private final class CueTaskHoverSurface: NSVisualEffectView {
    var entered: (() -> Void)?
    var exited: (() -> Void)?
    private var tracking: NSTrackingArea?
    override var isFlipped: Bool { true }
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let tracking { removeTrackingArea(tracking) }
        let area = NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self)
        addTrackingArea(area); tracking = area
    }
    override func mouseEntered(with event: NSEvent) { entered?() }
    override func mouseExited(with event: NSEvent) { exited?() }
}

/// A nonactivating hover panel. All preview text lives only in this controller.
final class CueTaskHoverController: NSObject {
    private var panel: CueTaskHoverPanel?
    private let titleField = NSTextField(labelWithString: "")
    private let summaryField = NSTextField(labelWithString: "")
    private let modelField = NSTextField(labelWithString: "")
    private let breakdownField = NSTextField(wrappingLabelWithString: "")
    private let previewHeading = NSTextField(labelWithString: "最近对话")
    private let footerField = NSTextField(labelWithString: "移入浮窗暂停滚动 · 可选择文字")
    private let scroll = NSScrollView()
    private let text = NSTextView()
    private var showWork: DispatchWorkItem?
    private var hideWork: DispatchWorkItem?
    private var timer: Timer?
    private var activeThreadID: String?
    private weak var anchor: NSView?
    private var provider: (() -> CueTaskDetail?)?
    private var currentTitle = ""
    private var currentBody = ""
    private var currentDetail = CueTaskDetail(raw: [:])
    private var overRow = false
    private var overPanel = false
    private var previousTick = Date()
    private var lastRefresh = Date.distantPast
    private var latestMessageRange = NSRange(location: 0, length: 0)

    var isVisible: Bool { panel?.isVisible == true }
    var isEngaged: Bool { overRow || overPanel || isVisible }

    func hover(threadID: String, title: String, anchor: NSView, provider: @escaping () -> CueTaskDetail?) {
        hideWork?.cancel(); overRow = true
        self.anchor = anchor; self.provider = provider; currentTitle = title
        if activeThreadID == threadID, isVisible {
            refresh(); if !overPanel { positionPanel() }; return
        }
        showWork?.cancel(); activeThreadID = threadID
        if isVisible { panel?.orderOut(nil); timer?.invalidate(); timer = nil }
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.activeThreadID == threadID, self.overRow else { return }
            self.showNow()
        }
        showWork = work; DispatchQueue.main.asyncAfter(deadline: .now() + 0.45, execute: work)
    }

    func leaveRow() { overRow = false; showWork?.cancel(); scheduleHide() }

    func hide() {
        showWork?.cancel(); hideWork?.cancel(); timer?.invalidate(); timer = nil
        if let panel { panel.parent?.removeChildWindow(panel); panel.orderOut(nil) }
        provider = nil; anchor = nil; activeThreadID = nil; overRow = false; overPanel = false
        currentBody = ""; text.string = ""; currentDetail = CueTaskDetail(raw: [:])
    }

    func refresh() {
        guard activeThreadID != nil else { return }
        update(detail: provider?() ?? CueTaskDetail(raw: [:]), title: currentTitle)
    }

    func update(detail: CueTaskDetail, title: String) {
        makePanel()
        currentDetail = detail; currentTitle = title
        titleField.stringValue = title; titleField.toolTip = title
        summaryField.stringValue = detail.summaryLine(now: Date())
        var tips = detail.totalTokens.map { "Tokens：" + CueTaskDetail.exact($0) } ?? "Tokens 尚未记录"
        if detail.tokensStatus == "partial" || detail.elapsedStatus == "partial" { tips += "\n只统计已读取的本轮记录；≥ 表示当前可确认的下限。" }
        summaryField.toolTip = tips
        modelField.stringValue = detail.secondaryLine; modelField.toolTip = detail.secondaryLine
        breakdownField.stringValue = detail.tokenBreakdown
        previewHeading.stringValue = detail.contentTruncated ? "最近对话 · 部分内容" : "最近对话"
        renderMessages(detail.messages)
    }

    private func scheduleHide() {
        hideWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self, !self.overRow, !self.overPanel else { return }
            self.hide()
        }
        hideWork = work; DispatchQueue.main.asyncAfter(deadline: .now() + 0.25, execute: work)
    }

    private func makePanel() {
        guard panel == nil else { return }
        let size = NSSize(width: 500, height: 474)
        let window = CueTaskHoverPanel(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        window.isOpaque = false; window.backgroundColor = .clear; window.hasShadow = true
        window.hidesOnDeactivate = false; window.becomesKeyOnlyIfNeeded = true
        window.isReleasedWhenClosed = false; window.animationBehavior = .utilityWindow
        window.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        let surface = CueTaskHoverSurface(frame: NSRect(origin: .zero, size: size))
        surface.material = .popover; surface.blendingMode = .withinWindow; surface.state = .active
        surface.wantsLayer = true; surface.layer?.cornerRadius = 13; surface.layer?.masksToBounds = true
        surface.layer?.borderWidth = 0.7; surface.layer?.borderColor = NSColor.separatorColor.withAlphaComponent(0.6).cgColor
        surface.entered = { [weak self] in self?.overPanel = true; self?.hideWork?.cancel() }
        surface.exited = { [weak self] in self?.overPanel = false; self?.scheduleHide() }
        window.contentView = surface
        titleField.frame = NSRect(x: 18, y: 14, width: 464, height: 24)
        titleField.font = .systemFont(ofSize: 14, weight: .semibold); titleField.lineBreakMode = .byTruncatingTail
        summaryField.frame = NSRect(x: 18, y: 43, width: 464, height: 20)
        summaryField.font = .monospacedDigitSystemFont(ofSize: 11.5, weight: .medium)
        modelField.frame = NSRect(x: 18, y: 67, width: 464, height: 18)
        modelField.font = .systemFont(ofSize: 11); modelField.textColor = .secondaryLabelColor
        modelField.lineBreakMode = .byTruncatingTail
        breakdownField.frame = NSRect(x: 18, y: 91, width: 464, height: 37)
        breakdownField.font = .monospacedDigitSystemFont(ofSize: 11, weight: .regular)
        breakdownField.textColor = .secondaryLabelColor
        let separator = NSBox(frame: NSRect(x: 18, y: 137, width: 464, height: 1)); separator.boxType = .separator
        previewHeading.frame = NSRect(x: 18, y: 150, width: 310, height: 18)
        previewHeading.font = .systemFont(ofSize: 11, weight: .semibold); previewHeading.textColor = .secondaryLabelColor
        let follow = NSButton(title: "跟随最新", target: self, action: #selector(followLatest))
        follow.bezelStyle = .rounded; follow.frame = NSRect(x: 390, y: 143, width: 95, height: 29)
        follow.setAccessibilityLabel("跟随最新对话内容")
        scroll.frame = NSRect(x: 18, y: 178, width: 464, height: 258)
        scroll.drawsBackground = false; scroll.borderType = .noBorder; scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        text.frame = NSRect(x: 0, y: 0, width: 448, height: 258)
        text.isEditable = false; text.isSelectable = true; text.drawsBackground = false
        text.isRichText = false; text.isVerticallyResizable = true; text.isHorizontallyResizable = false
        text.minSize = NSSize(width: 0, height: 258); text.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        text.textContainerInset = NSSize(width: 0, height: 7)
        text.textContainer?.widthTracksTextView = true; text.textContainer?.lineFragmentPadding = 0
        text.setAccessibilityLabel("最近用户和助手对话，可滚动并选择文字")
        scroll.documentView = text
        footerField.frame = NSRect(x: 18, y: 447, width: 464, height: 17)
        footerField.font = .systemFont(ofSize: 10); footerField.textColor = .tertiaryLabelColor
        for view in [titleField, summaryField, modelField, breakdownField, separator, previewHeading, follow, scroll, footerField] { surface.addSubview(view) }
        panel = window
    }

    private func showNow() {
        makePanel(); currentBody = ""; refresh(); positionPanel()
        guard let panel else { return }
        if let parent = anchor?.window, panel.parent !== parent {
            panel.parent?.removeChildWindow(panel); parent.addChildWindow(panel, ordered: .above)
        }
        // Deliberately no activate/makeKey: showing a tip must not steal typing.
        panel.orderFrontRegardless(); previousTick = Date(); lastRefresh = Date()
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in self?.tick() }
        if let timer { RunLoop.main.add(timer, forMode: .common) }
        scrollToLatestMessage()
    }

    private func positionPanel() {
        guard let panel, let anchor, let window = anchor.window else { return }
        let target = window.convertToScreen(anchor.convert(anchor.bounds, to: nil))
        let screen = window.screen ?? NSScreen.main
        let visible = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let size = panel.frame.size
        var x = target.maxX + 10
        if x + size.width > visible.maxX - 8 { x = target.minX - size.width - 10 }
        x = max(visible.minX + 8, min(x, visible.maxX - size.width - 8))
        let y = max(visible.minY + 8, min(target.maxY - size.height, visible.maxY - size.height - 8))
        panel.level = NSWindow.Level(rawValue: max(NSWindow.Level.popUpMenu.rawValue, window.level.rawValue + 1))
        panel.setFrameOrigin(NSPoint(x: x, y: y))
    }

    private func renderMessages(_ messages: [CueTaskMessage]) {
        let result = NSMutableAttributedString()
        let date = DateFormatter(); date.locale = Locale(identifier: "zh_CN"); date.dateFormat = "HH:mm:ss"
        let headingStyle = NSMutableParagraphStyle(); headingStyle.paragraphSpacing = 5
        let bodyStyle = NSMutableParagraphStyle(); bodyStyle.lineSpacing = 3; bodyStyle.paragraphSpacing = 4
        var latest = NSRange(location: 0, length: 0)
        for message in messages {
            latest = NSRange(location: result.length, length: 0)
            let heading = (message.role == "user" ? "你" : "Codex") + (message.date.map { " · " + date.string(from: $0) } ?? "") + "\n"
            result.append(NSAttributedString(string: heading, attributes: [.font: NSFont.systemFont(ofSize: 10.5, weight: .semibold), .foregroundColor: NSColor.secondaryLabelColor, .paragraphStyle: headingStyle]))
            result.append(NSAttributedString(string: message.text + "\n\n", attributes: [.font: NSFont.systemFont(ofSize: 13), .foregroundColor: NSColor.labelColor, .paragraphStyle: bodyStyle]))
            latest.length = result.length - latest.location
        }
        if messages.isEmpty {
            result.append(NSAttributedString(string: "暂未读取到可显示的最近对话。", attributes: [.font: NSFont.systemFont(ofSize: 12), .foregroundColor: NSColor.secondaryLabelColor]))
        }
        guard result.string != currentBody else { return }
        let origin = scroll.contentView.bounds.origin
        let wasAtEnd = text.frame.height - scroll.contentView.bounds.maxY < 10
        let selection = text.selectedRange()
        let selected = selection.length > 0 && NSMaxRange(selection) <= (currentBody as NSString).length ? (currentBody as NSString).substring(with: selection) : ""
        let wasEmpty = currentBody.isEmpty
        currentBody = result.string; latestMessageRange = latest
        text.textStorage?.setAttributedString(result)
        text.setFrameSize(NSSize(width: scroll.contentSize.width, height: max(scroll.contentSize.height, text.frame.height)))
        text.layoutManager?.ensureLayout(for: text.textContainer!)
        let height = (text.layoutManager?.usedRect(for: text.textContainer!).height ?? 0) + 24
        text.setFrameSize(NSSize(width: scroll.contentSize.width, height: max(scroll.contentSize.height, height)))
        if !selected.isEmpty {
            let newSelection = (currentBody as NSString).range(of: selected)
            if newSelection.location != NSNotFound { text.setSelectedRange(newSelection) }
        }
        if wasEmpty { scrollToLatestMessage() }
        else if wasAtEnd && !overPanel { scrollToEnd() }
        else { scroll.contentView.scroll(to: scroll.contentView.constrainBoundsRect(NSRect(origin: origin, size: scroll.contentSize)).origin); scroll.reflectScrolledClipView(scroll.contentView) }
    }

    private func tick() {
        guard isVisible else { timer?.invalidate(); timer = nil; return }
        let now = Date(), elapsed = min(0.15, nowInterval(since: previousTick)); previousTick = now
        if now.timeIntervalSince(lastRefresh) >= 1 { lastRefresh = now; refresh() }
        advanceScroll(elapsed: elapsed)
    }
    private func advanceScroll(elapsed: TimeInterval) {
        guard !overPanel, text.selectedRange().length == 0 else { return }
        let maximum = max(0, text.frame.height - scroll.contentSize.height)
        let current = scroll.contentView.bounds.origin
        if current.y < maximum {
            scroll.contentView.scroll(to: NSPoint(x: current.x, y: min(maximum, current.y + elapsed * 10)))
            scroll.reflectScrolledClipView(scroll.contentView)
        }
    }
    private func nowInterval(since date: Date) -> TimeInterval { max(0, Date().timeIntervalSince(date)) }
    private func scrollToLatestMessage() {
        guard let layout = text.layoutManager, let container = text.textContainer, !currentBody.isEmpty else { return }
        layout.ensureLayout(for: container)
        let glyphs = layout.glyphRange(forCharacterRange: latestMessageRange, actualCharacterRange: nil)
        let rect = layout.boundingRect(forGlyphRange: glyphs, in: container)
        let maximum = max(0, text.frame.height - scroll.contentSize.height)
        scroll.contentView.scroll(to: NSPoint(x: 0, y: min(maximum, max(0, rect.minY))))
        scroll.reflectScrolledClipView(scroll.contentView)
    }
    private func scrollToEnd() {
        scroll.contentView.scroll(to: NSPoint(x: 0, y: max(0, text.frame.height - scroll.contentSize.height)))
        scroll.reflectScrolledClipView(scroll.contentView)
    }
    @objc private func followLatest() { text.setSelectedRange(NSRange(location: 0, length: 0)); refresh(); scrollToLatestMessage() }

    deinit {
        showWork?.cancel(); hideWork?.cancel(); timer?.invalidate()
        if let panel { panel.parent?.removeChildWindow(panel); panel.orderOut(nil) }
    }

    static func validationReport() -> [String: Bool] {
        let now = Date(timeIntervalSince1970: 1_793_170_000)
        let unknown = CueTaskDetail(raw: [:])
        let partial = CueTaskDetail(raw: ["status": "running", "started_at": now.addingTimeInterval(-125).timeIntervalSince1970, "elapsed_status": "partial", "total_tokens": 12345, "tokens_status": "partial", "model": "gpt-model", "reasoning_effort": "high", "service_tier": "fast"])
        let complete = CueTaskDetail(raw: ["status": "complete", "started_at": now.addingTimeInterval(-120).timeIntervalSince1970, "finished_at": now.addingTimeInterval(-60).timeIntervalSince1970, "total_tokens": 0, "tokens_status": "complete"])
        let invalid = CueTaskDetail(raw: ["total_tokens": true, "input_tokens": -1, "output_tokens": 2.5, "elapsed_seconds": Double.infinity])
        let messages = CueTaskDetail(raw: ["messages": [["role": "system", "text": "SYSTEM-NOT-VISIBLE"], ["role": "tool", "text": "TOOL-ARGUMENTS-NOT-VISIBLE"], ["role": "reasoning", "text": "REASONING-NOT-VISIBLE"], ["role": "user", "text": "可见问题"], ["role": "assistant", "text": "可见回复"]]])
        let long = CueTaskDetail(raw: ["messages": (0..<10).map { ["role": "assistant", "text": String(repeating: "内容", count: 3000) + "\($0)"] }])
        var results = ["task_detail_unknown_never_fabricates_zero_tokens": unknown.totalTokens == nil && unknown.summaryLine(now: now).contains("待同步"),
                       "task_detail_partial_has_explicit_lower_bound": partial.summaryLine(now: now).contains("≥") && partial.summaryLine(now: now).contains("部分"),
                       "task_detail_live_duration_advances": partial.elapsed(now: now.addingTimeInterval(10)) == 135,
                       "task_detail_completed_duration_stays_fixed": complete.elapsed(now: now.addingTimeInterval(10)) == 60,
                       "task_detail_explicit_zero_is_retained": complete.summaryLine(now: now).contains("0 Tokens"),
                       "task_detail_old_alert_labels_latest_turn": partial.summaryLine(now: now, eventDate: now.addingTimeInterval(-1000)).hasPrefix("最新轮"),
                       "task_detail_invalid_numbers_remain_unknown": invalid.totalTokens == nil && invalid.inputTokens == nil && invalid.outputTokens == nil && invalid.elapsedSeconds == nil,
                       "task_detail_reliable_model_and_speed_are_visible": partial.secondaryLine.contains("gpt-model") && partial.secondaryLine.contains("高强度") && partial.secondaryLine.contains("Fast"),
                       "task_detail_only_user_and_assistant_are_previewed": messages.messages.map(\.role) == ["user", "assistant"],
                       "task_detail_preview_text_stays_bounded": long.messages.count <= 8 && long.messages.reduce(0) { $0 + $1.text.count } <= 12000 && long.contentTruncated]
        _ = NSApplication.shared
        let hover = CueTaskHoverController(); hover.makePanel(); hover.update(detail: messages, title: "测试标题")
        results["task_hover_text_is_selectable_and_not_editable"] = hover.text.isSelectable && !hover.text.isEditable
        results["task_hover_initial_update_does_not_make_key"] = hover.panel?.isKeyWindow == false
        results["task_hover_contains_only_visible_conversation"] = hover.currentBody.contains("可见回复") && !hover.currentBody.contains("NOT-VISIBLE")
        let reading = CueTaskDetail(raw: ["messages": [["role": "assistant", "text": String(repeating: "这是用于滚动验收的可见回复。\n", count: 80)]]])
        hover.update(detail: reading, title: "长回复")
        hover.text.setSelectedRange(NSRange(location: 0, length: 0))
        hover.scroll.contentView.scroll(to: NSPoint(x: 0, y: 20))
        let beforeScroll = hover.scroll.contentView.bounds.origin.y
        hover.advanceScroll(elapsed: 0.1)
        results["task_hover_auto_scroll_moves_slowly"] = hover.scroll.contentView.bounds.origin.y > beforeScroll && hover.scroll.contentView.bounds.origin.y <= beforeScroll + 2
        hover.overPanel = true
        let paused = hover.scroll.contentView.bounds.origin.y
        hover.advanceScroll(elapsed: 0.1)
        results["task_hover_pointer_inside_pauses_auto_scroll"] = hover.scroll.contentView.bounds.origin.y == paused
        let appended = CueTaskDetail(raw: ["messages": [["role": "assistant", "text": reading.messages[0].text + "\n新增可见进展。"]]])
        hover.update(detail: appended, title: "长回复更新")
        results["task_hover_refresh_preserves_reading_position"] = abs(hover.scroll.contentView.bounds.origin.y - paused) < 1
        hover.overPanel = false; hover.text.setSelectedRange(NSRange(location: 0, length: 2))
        hover.advanceScroll(elapsed: 0.1)
        results["task_hover_text_selection_pauses_auto_scroll"] = abs(hover.scroll.contentView.bounds.origin.y - paused) < 1
        let anchor = NSView(frame: NSRect(x: 0, y: 0, width: 400, height: 50))
        hover.hover(threadID: "fixture", title: "短暂停留", anchor: anchor, provider: { reading })
        hover.leaveRow()
        results["task_hover_leaving_before_delay_cancels_open"] = hover.showWork?.isCancelled == true
        hover.hide()
        results["task_hover_hide_discards_preview_memory"] = hover.currentBody.isEmpty && hover.text.string.isEmpty
        return results
    }

    @discardableResult static func captureFixture(to output: URL) -> Bool {
        _ = NSApplication.shared
        let now = Date()
        let reply = "已按对话去重整理任务。当前这轮已经运行 12 分钟，模型设置会用于后续请求。\n\n正在执行的工具会继续完成；你可以先查看任务的实时用量，再决定是否调整模型和强度。\n\n" + String(repeating: "任务的最新回复会继续在这里更新。移入浮窗后暂停自动滚动，可以选择文字、手动滚动并阅读上下文。\n\n", count: 4)
        let detail = CueTaskDetail(raw: ["status": "running", "started_at": now.addingTimeInterval(-754).timeIntervalSince1970, "elapsed_status": "complete", "model": "gpt-6.1-sol", "reasoning_effort": "high", "service_tier": "fast", "total_tokens": 182476, "input_tokens": 8476, "output_tokens": 12000, "cache_read_tokens": 162000, "cache_creation_tokens": 0, "tokens_status": "complete", "messages": [["role": "user", "text": "每周额度已重置，请整理接下来要继续的任务。", "created_at": now.addingTimeInterval(-754).timeIntervalSince1970], ["role": "assistant", "text": reply, "created_at": now.addingTimeInterval(-20).timeIntervalSince1970]]])
        let hover = CueTaskHoverController(); hover.makePanel(); hover.update(detail: detail, title: "界面验收样例 · 当前任务详情")
        guard let window = hover.panel, let view = window.contentView else { return false }
        window.appearance = NSAppearance(named: .darkAqua); window.displayIfNeeded()
        guard let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return false }
        view.cacheDisplay(in: view.bounds, to: bitmap)
        guard let data = bitmap.representation(using: .png, properties: [:]) else { return false }
        do { try data.write(to: output); hover.hide(); return true } catch { return false }
    }
}
