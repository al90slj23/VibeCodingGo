import AppKit
import Foundation

/// User-authored local drafts and next-step notes. Conversation bodies remain
/// in memory only; this file is separate from the reminder and usage ledgers.
final class CueConversationDrafts {
    private static let lock = NSLock()
    let file: URL
    private var rows: [String: [String: String]] = [:]
    private(set) var error = ""
    init(home: URL) {
        file = home.appendingPathComponent("conversation-drafts.json")
        if let data = try? Data(contentsOf: file), data.count <= 4_194_304,
           let raw = try? JSONSerialization.jsonObject(with: data) as? [String: [String: String]] { rows = raw }
    }
    func values(_ thread: String) -> [String: String] { rows[thread] ?? [:] }
    @discardableResult func save(_ thread: String, draft: String, note: String, pending: String = "") -> Bool {
        guard CueBatchTarget(threadID: thread, title: "") != nil, draft.utf8.count <= 65_536, note.count <= 4000 else { error = "草稿或备注过长，尚未保存。"; return false }
        Self.lock.lock(); defer { Self.lock.unlock() }
        if FileManager.default.fileExists(atPath: file.path) {
            guard let data = try? Data(contentsOf: file), data.count <= 4_194_304,
                  let latest = try? JSONSerialization.jsonObject(with: data) as? [String: [String: String]] else { error = "原有草稿暂不可读，未覆盖。请先复制重要内容。"; return false }
            rows = latest
        }
        if draft.isEmpty && note.isEmpty && pending.isEmpty { rows.removeValue(forKey: thread) }
        else { rows[thread] = ["draft": draft, "note": note, "pending_request_id": pending] }
        do {
            let data = try JSONSerialization.data(withJSONObject: rows, options: [.sortedKeys])
            guard data.count <= 4_194_304 else { error = "本机草稿已达到保存上限，请整理后重试。"; return false }
            try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            try data.write(to: file, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
            error = ""; return true
        } catch { self.error = "本机草稿未能保存，请先复制重要内容。"; return false }
    }
}

private final class CueConversationSurface: NSView {
    var onLayout: (() -> Void)?
    override var isFlipped: Bool { true }
    override func layout() { super.layout(); onLayout?() }
    override func draw(_ dirtyRect: NSRect) { NSColor.windowBackgroundColor.setFill(); dirtyRect.fill() }
}

final class CueConversationController: NSWindowController, NSWindowDelegate, NSTextViewDelegate {
    let event: CueEvent
    private let client: CueUsageClient
    private let drafts: CueConversationDrafts
    private let detailProvider: () -> CueTaskDetail?
    private let onHandled: () -> Void
    private let onCodex: () -> Void
    private let onConnect: () -> Void
    private let onPathAction: (String) -> Void
    private let root = CueConversationSurface()
    private let titleField = NSTextField(labelWithString: "")
    private let summary = NSTextField(labelWithString: "")
    private let project = NSTextField(labelWithString: "项目目录读取中…")
    private let source = NSTextField(labelWithString: "最近对话")
    private let connection = NSTextField(wrappingLabelWithString: "正在检查 Codex 对话连接…")
    private let feedback = NSTextField(wrappingLabelWithString: "")
    private let draftHint = NSTextField(labelWithString: "草稿仅保存在本机；点击发送后才会交给 Codex。")
    private let count = NSTextField(labelWithString: "")
    private let noteTitle = NSTextField(labelWithString: "下次从这里继续")
    private let noteHint = NSTextField(wrappingLabelWithString: "只看不处理，也会保留为待续。备注和草稿会自动保存。")
    private let text = NSTextView(), composer = NSTextView(), note = NSTextView()
    private let transcriptScroll = NSScrollView(), composeScroll = NSScrollView(), noteScroll = NSScrollView()
    private let codexButton = NSButton(title: "在 Codex 打开", target: nil, action: nil)
    private let doneButton = NSButton(title: "标记处理完成", target: nil, action: nil)
    private let laterButton = NSButton(title: "稍后继续", target: nil, action: nil)
    private let loadButton = NSButton(title: "载入对话", target: nil, action: nil)
    private let connectButton = NSButton(title: "接通对话输入…", target: nil, action: nil)
    private let sendButton = NSButton(title: "发送到 Codex", target: nil, action: nil)
    private let pathButton = NSButton(title: "访达打开", target: nil, action: nil)
    private let refreshButton = NSButton(title: "刷新", target: nil, action: nil)
    private let unlockButton = NSButton(title: "已核对，解锁发送", target: nil, action: nil)
    private var timer: Timer?
    private var reading = false, checking = false, sending = false, loading = false
    private var canSend = false, canLoad = false, messageLimit = 8192
    private var activeTurnID = "", chatState = "unknown", chatTransport = "", pendingRequestID = ""
    private var fingerprint = "", bodySignature = ""
    private var lastStatusCheck = Date.distantPast
    private var lastRead = Date.distantPast
    private var readDelay: TimeInterval = 1.5
    private var loadGeneration = 0
    private var saveWork: DispatchWorkItem?
    private var handled = false
    private var fixtureMode = false

    init(home: URL, event: CueEvent, detailProvider: @escaping () -> CueTaskDetail?, onHandled: @escaping () -> Void, onCodex: @escaping () -> Void, onConnect: @escaping () -> Void, onPathAction: @escaping (String) -> Void) {
        self.event = event; client = CueUsageClient(home: home); drafts = CueConversationDrafts(home: home)
        self.detailProvider = detailProvider; self.onHandled = onHandled; self.onCodex = onCodex; self.onConnect = onConnect; self.onPathAction = onPathAction
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1040, height: 800), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "VibeCue · " + event.title; window.isReleasedWhenClosed = false
        window.contentMinSize = NSSize(width: 900, height: 700); window.center()
        super.init(window: window); window.delegate = self; window.contentView = root
        titleField.stringValue = event.title; titleField.font = .systemFont(ofSize: 20, weight: .semibold); titleField.lineBreakMode = .byTruncatingTail; titleField.toolTip = event.title
        summary.font = .monospacedDigitSystemFont(ofSize: 11.5, weight: .regular); summary.textColor = .secondaryLabelColor
        project.font = .systemFont(ofSize: 11); project.textColor = .secondaryLabelColor; project.lineBreakMode = .byTruncatingMiddle
        source.font = .systemFont(ofSize: 12, weight: .semibold); source.textColor = .secondaryLabelColor
        connection.font = .systemFont(ofSize: 11); connection.textColor = .secondaryLabelColor; connection.maximumNumberOfLines = 3
        feedback.font = .systemFont(ofSize: 11); feedback.textColor = .secondaryLabelColor; feedback.maximumNumberOfLines = 3
        draftHint.font = .systemFont(ofSize: 10); draftHint.textColor = .secondaryLabelColor; draftHint.lineBreakMode = .byTruncatingTail
        count.font = .monospacedDigitSystemFont(ofSize: 10, weight: .regular); count.textColor = .secondaryLabelColor; count.alignment = .right
        noteTitle.font = .systemFont(ofSize: 13, weight: .semibold); noteHint.font = .systemFont(ofSize: 11); noteHint.textColor = .secondaryLabelColor
        for (view, scroll, editable) in [(text, transcriptScroll, false), (composer, composeScroll, true), (note, noteScroll, true)] {
            scroll.documentView = view; scroll.hasVerticalScroller = true; scroll.autohidesScrollers = true; scroll.borderType = .bezelBorder
            view.isEditable = editable; view.isSelectable = true; view.isRichText = false; view.drawsBackground = true; view.backgroundColor = .textBackgroundColor; view.textColor = .labelColor
            view.font = .systemFont(ofSize: editable ? 13 : 14); view.textContainerInset = NSSize(width: 13, height: 12)
            view.isVerticallyResizable = true; view.isHorizontallyResizable = false
            view.autoresizingMask = [.width]; view.textContainer?.widthTracksTextView = true
            view.textContainer?.containerSize = NSSize(width: 600, height: CGFloat.greatestFiniteMagnitude)
        }
        composer.delegate = self; note.delegate = self
        composer.setAccessibilityLabel("发送给当前 Codex 对话的消息"); note.setAccessibilityLabel("下次继续的本机备注"); text.setAccessibilityLabel("当前对话最近的用户与助手消息")
        for button in [codexButton, doneButton, laterButton, loadButton, connectButton, sendButton, pathButton, refreshButton, unlockButton] { button.bezelStyle = .rounded; button.target = self }
        codexButton.action = #selector(openCodex); doneButton.action = #selector(markHandled); laterButton.action = #selector(closeForLater)
        sendButton.action = #selector(send); loadButton.action = #selector(loadThread); connectButton.action = #selector(connectCodex)
        pathButton.action = #selector(revealProject); refreshButton.action = #selector(refresh)
        unlockButton.action = #selector(unlockSend); unlockButton.toolTip = "请先核对消息是否已经送达。此按钮只允许再次发送，不会自动重发。"
        sendButton.identifier = NSUserInterfaceItemIdentifier("conversation.send"); doneButton.identifier = NSUserInterfaceItemIdentifier("conversation.handled"); laterButton.identifier = NSUserInterfaceItemIdentifier("conversation.later")
        sendButton.keyEquivalent = "\r"; sendButton.keyEquivalentModifierMask = [.command]
        sendButton.toolTip = "发送到当前对话（⌘↩）。不会自动发送草稿。"
        let views: [NSView] = [titleField, summary, project, source, connection, feedback, draftHint, count, noteTitle, noteHint, transcriptScroll, composeScroll, noteScroll, codexButton, doneButton, laterButton, loadButton, connectButton, sendButton, pathButton, refreshButton, unlockButton]
        views.forEach { root.addSubview($0) }; root.onLayout = { [weak self] in self?.layout() }
        let saved = drafts.values(event.threadID); composer.string = saved["draft"] ?? ""; note.string = saved["note"] ?? ""; pendingRequestID = saved["pending_request_id"] ?? ""
        if !pendingRequestID.isEmpty { feedback.stringValue = "上次发送结果尚未确认。请先查看对话，核对后再决定是否重发。" }
        refreshMetrics(); layout(); updateControls()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    deinit { timer?.invalidate(); saveWork?.cancel() }

    override func showWindow(_ sender: Any?) {
        super.showWindow(sender); handled = false; window?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        guard !fixtureMode else { return }
        readConversation(); checkConnection(force: true)
        timer?.invalidate(); timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            guard let self, self.window?.isVisible == true, self.window?.isMiniaturized != true else { return }
            self.refreshMetrics(); self.readConversation(); self.checkConnection()
        }
        if let timer { RunLoop.main.add(timer, forMode: .common) }
    }
    func windowWillClose(_ notification: Notification) { timer?.invalidate(); timer = nil; saveDraft(); /* Closing never handles a task. */ }
    func windowShouldClose(_ sender: NSWindow) -> Bool { saveDraft() }
    func stop() { timer?.invalidate(); timer = nil; saveDraft(); client.stop() }

    private func layout() {
        let w = root.bounds.width, h = root.bounds.height, sidebar: CGFloat = 230, gap: CGFloat = 18, left: CGFloat = 24
        let mainWidth = w - left * 2 - gap - sidebar, sideX = left + mainWidth + gap
        titleField.frame = NSRect(x: left, y: 18, width: w - 410, height: 28)
        codexButton.frame = NSRect(x: w - 358, y: 17, width: 135, height: 30)
        doneButton.frame = NSRect(x: w - 215, y: 17, width: 191, height: 30)
        summary.frame = NSRect(x: left, y: 55, width: w - 48, height: 19)
        project.frame = NSRect(x: left, y: 81, width: w - 153, height: 18)
        pathButton.frame = NSRect(x: w - 125, y: 76, width: 103, height: 28)
        source.frame = NSRect(x: left, y: 119, width: mainWidth - 75, height: 21)
        refreshButton.frame = NSRect(x: left + mainWidth - 66, y: 114, width: 68, height: 28)
        transcriptScroll.frame = NSRect(x: left, y: 149, width: mainWidth, height: max(190, h - 415))
        connection.frame = NSRect(x: left, y: h - 257, width: mainWidth, height: 45)
        loadButton.frame = NSRect(x: left, y: h - 211, width: 117, height: 28)
        connectButton.frame = NSRect(x: left + 124, y: h - 211, width: 164, height: 28)
        unlockButton.frame = NSRect(x: left + mainWidth - 169, y: h - 211, width: 171, height: 28)
        composeScroll.frame = NSRect(x: left, y: h - 176, width: mainWidth, height: 92)
        count.frame = NSRect(x: left + mainWidth - 162, y: h - 76, width: 162, height: 17)
        draftHint.frame = NSRect(x: left, y: h - 75, width: mainWidth - 169, height: 17)
        sendButton.frame = NSRect(x: left + mainWidth - 158, y: h - 48, width: 161, height: 31)
        feedback.frame = NSRect(x: left, y: h - 49, width: mainWidth - 175, height: 35)
        noteTitle.frame = NSRect(x: sideX, y: 119, width: sidebar, height: 21)
        noteHint.frame = NSRect(x: sideX, y: 151, width: sidebar, height: 52)
        noteScroll.frame = NSRect(x: sideX, y: 215, width: sidebar, height: max(160, h - 310))
        laterButton.frame = NSRect(x: sideX, y: h - 75, width: sidebar, height: 31)
        text.setFrameSize(NSSize(width: transcriptScroll.contentSize.width, height: max(text.frame.height, transcriptScroll.contentSize.height)))
        composer.setFrameSize(NSSize(width: composeScroll.contentSize.width, height: max(composer.frame.height, composeScroll.contentSize.height)))
        note.setFrameSize(NSSize(width: noteScroll.contentSize.width, height: max(note.frame.height, noteScroll.contentSize.height)))
    }
    private func refreshMetrics() {
        if let detail = detailProvider() { summary.stringValue = "待续 · " + detail.summaryLine() + " · " + detail.secondaryLine }
        else { summary.stringValue = "待续 · 关闭窗口后保留，处理完成时请明确标记" }
        summary.toolTip = summary.stringValue
    }
    private func payload(_ values: [String: Any]) -> Data? { try? JSONSerialization.data(withJSONObject: values) }
    private func readConversation() {
        guard !reading, Date().timeIntervalSince(lastRead) > readDelay else { return }
        reading = true; lastRead = Date(); loadGeneration += 1; let generation = loadGeneration
        var request: [String: Any] = ["thread_id": event.threadID]
        if !fingerprint.isEmpty { request["if_fingerprint"] = fingerprint }
        client.run(["read"], module: "vibecue.thread_info", input: payload(request)) { [weak self] result in
            guard let self else { return }; self.reading = false
            guard generation == self.loadGeneration else { return }
            switch result {
            case .success(let data): self.applyTranscript(data)
            case .failure(let error):
                self.fingerprint = ""; self.readDelay = 30; self.lastRead = Date()
                self.source.stringValue = "最近对话 · 暂不可读"; self.source.toolTip = error.localizedDescription
            }
        }
    }
    private func applyTranscript(_ data: [String: Any]) {
        guard data["thread_id"] as? String == event.threadID else { return }
        if let cwd = data["cwd"] as? String, !cwd.isEmpty { project.stringValue = cwd; project.toolTip = cwd; pathButton.isEnabled = true }
        else { project.stringValue = "此对话未记录本机项目目录"; pathButton.isEnabled = false }
        let readable = data["source_readable"] as? Bool == true
        if readable {
            readDelay = 1.5
            if let value = data["fingerprint"] as? String { fingerprint = value }
        } else { fingerprint = ""; readDelay = 30; lastRead = Date() }
        let coverage = data["coverage"] as? [String: Any] ?? [:]
        source.stringValue = readable ? (coverage["partial"] as? Bool == true ? "最近对话 · 部分记录" : "最近对话") : "最近对话 · 暂不可读"
        source.toolTip = data["message"] as? String
        if data["unchanged"] as? Bool == true { return }
        guard readable else { return }
        render(data["messages"] as? [[String: Any]] ?? [])
    }
    private func render(_ rows: [[String: Any]]) {
        guard let data = try? JSONSerialization.data(withJSONObject: rows, options: [.sortedKeys]), String(data: data, encoding: .utf8) != bodySignature else { return }
        bodySignature = String(data: data, encoding: .utf8) ?? ""
        let viewport = transcriptScroll.contentView.bounds
        let atBottom = text.bounds.height - viewport.maxY < 50 || text.string.isEmpty
        let selection = text.selectedRange(), result = NSMutableAttributedString(string: "")
        let paragraph = NSMutableParagraphStyle(); paragraph.lineSpacing = 4; paragraph.paragraphSpacing = 7
        for row in rows.suffix(60) {
            guard let role = row["role"] as? String, ["user", "assistant"].contains(role), let body = row["text"] as? String, !body.isEmpty else { continue }
            let label = role == "user" ? "你" : "Codex"
            let heading = label + "  " + Self.dateLabel(row["created_at"]) + "\n"
            result.append(NSAttributedString(string: heading, attributes: [.font: NSFont.systemFont(ofSize: 11.5, weight: .semibold), .foregroundColor: role == "user" ? NSColor.controlAccentColor : NSColor.secondaryLabelColor, .paragraphStyle: paragraph]))
            result.append(NSAttributedString(string: body + "\n\n", attributes: [.font: NSFont.systemFont(ofSize: 13.5), .foregroundColor: NSColor.labelColor, .paragraphStyle: paragraph]))
        }
        if result.length == 0 { result.append(NSAttributedString(string: "没有找到可展示的用户／助手消息。", attributes: [.foregroundColor: NSColor.secondaryLabelColor])) }
        text.textStorage?.setAttributedString(result); text.layoutManager?.ensureLayout(for: text.textContainer!)
        if selection.length > 0, NSMaxRange(selection) <= result.length { text.setSelectedRange(selection) }
        if atBottom && selection.length == 0 { text.scrollToEndOfDocument(nil) }
        else { transcriptScroll.contentView.scroll(to: viewport.origin); transcriptScroll.reflectScrolledClipView(transcriptScroll.contentView) }
    }
    private static func dateLabel(_ raw: Any?) -> String {
        let date: Date?
        if let value = raw as? Double { date = Date(timeIntervalSince1970: value) }
        else if let text = raw as? String { let formatter = ISO8601DateFormatter(); formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; date = formatter.date(from: text) ?? ISO8601DateFormatter().date(from: text) }
        else { date = nil }
        guard let date else { return "" }; let formatter = DateFormatter(); formatter.dateFormat = "MM-dd HH:mm"; return formatter.string(from: date)
    }
    private func checkConnection(force: Bool = false) {
        guard !checking, force || Date().timeIntervalSince(lastStatusCheck) >= 8 else { return }
        checking = true; lastStatusCheck = Date()
        client.run(["status"], module: "vibecue.codex_chat", input: payload(["thread_id": event.threadID])) { [weak self] result in
            guard let self else { return }; self.checking = false
            switch result {
            case .success(let data): self.applyChatStatus(data)
            case .failure(let error): self.canSend = false; self.canLoad = false; self.connection.stringValue = error.localizedDescription; self.updateControls()
            }
        }
    }
    private func applyChatStatus(_ data: [String: Any]) {
        canSend = data["can_send"] as? Bool == true; chatState = data["state"] as? String ?? "unknown"
        chatTransport = data["transport"] as? String ?? ""
        canLoad = data["supported"] as? Bool == true && data["connected"] as? Bool == true && (data["needs_load"] as? Bool == true || chatState == "not_loaded")
        activeTurnID = data["active_turn_id"] as? String ?? ""
        if let value = data["max_text_bytes"] as? Int, value > 0 { messageLimit = value }
        connection.stringValue = data["message"] as? String ?? "尚未接通对话输入。"
        connectButton.isHidden = data["supported"] as? Bool == true
        updateControls()
    }
    private func updateControls() {
        let length = composer.string.utf8.count, hasText = !composer.string.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        count.stringValue = "\(length) / \(messageLimit) 字节"; count.textColor = length > messageLimit ? .systemRed : .secondaryLabelColor
        count.toolTip = "输入长度按 UTF-8 计算。中文通常占 3 字节；超限不会截断或发送。"
        sendButton.isEnabled = canSend && hasText && length <= messageLimit && !sending && !loading && pendingRequestID.isEmpty
        sendButton.title = sending ? "正在发送…" : chatTransport == "desktop_ipc" ? "发送到当前对话" : chatState == "active" ? "追加到当前任务" : "发送到 Codex"
        sendButton.toolTip = !canSend ? connection.stringValue : chatTransport == "desktop_ipc" ? "发送到当前 Codex 对话（⌘↩）。运行时作为补充，本轮已结束时开始新一轮。" : "发送到当前对话（⌘↩）。不会自动发送草稿。"
        loadButton.isEnabled = canLoad && !loading && !sending; loadButton.isHidden = !canLoad; loadButton.title = loading ? "正在载入…" : "载入对话"
        doneButton.isEnabled = !sending && !loading; doneButton.title = handled ? "已标记处理完成" : "标记处理完成"
        unlockButton.isHidden = pendingRequestID.isEmpty || sending
        unlockButton.isEnabled = !sending && !loading
    }
    func textDidChange(_ notification: Notification) {
        updateControls(); saveWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.saveDraft() }; saveWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4, execute: work)
    }
    @discardableResult private func saveDraft() -> Bool {
        let okay = drafts.save(event.threadID, draft: composer.string, note: note.string, pending: pendingRequestID)
        if !okay { feedback.stringValue = drafts.error; feedback.textColor = .systemRed }
        return okay
    }
    @objc private func send() {
        guard !fixtureMode, sendButton.isEnabled else { return }
        let message = composer.string, requestID = UUID().uuidString
        pendingRequestID = requestID
        guard saveDraft() else { pendingRequestID = ""; updateControls(); return }
        sending = true; feedback.stringValue = "正在提交，尚未确认…"; feedback.textColor = .secondaryLabelColor; updateControls()
        var values: [String: Any] = ["thread_id": event.threadID, "text": message, "request_id": requestID]
        if chatState == "active" { values["expected_turn_id"] = activeTurnID }
        client.run(["send"], module: "vibecue.codex_chat", input: payload(values)) { [weak self] result in
            guard let self else { return }; self.sending = false
            switch result {
            case .success(let data):
                self.applySendResult(data, sentText: message, requestID: requestID)
            case .failure:
                self.feedback.stringValue = "发送结果未确认。消息可能已提交，请查看对话后再决定是否重发。"; self.feedback.textColor = .systemOrange
            }
            self.saveDraft(); self.updateControls(); self.lastRead = .distantPast; self.readConversation(); self.checkConnection(force: true)
        }
    }
    private func applySendResult(_ data: [String: Any], sentText: String, requestID: String) {
        let state = data["status"] as? String ?? "unverified"
        let matches = data["thread_id"] as? String == event.threadID && data["request_id"] as? String == requestID
        feedback.stringValue = data["message"] as? String ?? "发送结果待确认，请查看对话。"
        if state == "accepted" && matches {
            pendingRequestID = ""; if composer.string == sentText { composer.string = "" }
            feedback.textColor = .systemGreen
        } else if state == "failed" && matches { pendingRequestID = ""; feedback.textColor = .systemOrange }
        else {
            feedback.stringValue = "发送结果尚未确认，请核对当前对话；不会自动重发。"; feedback.textColor = .systemOrange
        }
    }
    @objc private func loadThread() {
        guard !fixtureMode, canLoad, !loading, !sending else { return }; loading = true; updateControls()
        client.run(["load"], module: "vibecue.codex_chat", input: payload(["thread_id": event.threadID])) { [weak self] result in
            guard let self else { return }; self.loading = false
            switch result { case .success(let data): self.feedback.stringValue = data["message"] as? String ?? "载入结果待确认。"
            case .failure(let error): self.feedback.stringValue = error.localizedDescription }
            self.updateControls(); self.checkConnection(force: true)
        }
    }
    @objc private func markHandled() { guard !sending && !loading && saveDraft() else { return }; handled = true; onHandled(); summary.stringValue = "已标记处理完成 · 有新提醒时仍会再次通知"; updateControls(); close() }
    @objc private func closeForLater() { window?.performClose(nil) }
    @objc private func openCodex() { saveDraft(); onCodex() }
    @objc private func connectCodex() { onConnect() }
    @objc private func revealProject() { onPathAction("reveal_project") }
    @objc private func refresh() { fingerprint = ""; readDelay = 1.5; lastRead = .distantPast; readConversation(); checkConnection(force: true) }
    @objc private func unlockSend() {
        guard !sending, !loading, !pendingRequestID.isEmpty else { return }
        pendingRequestID = ""; saveDraft(); updateControls()
        feedback.stringValue = "已解锁。请确认上次消息未送达，再编辑并发送；不会自动重发。"; feedback.textColor = .systemOrange
    }

    static func validationReport() -> [String: Bool] {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent("vibecue-conversation-" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        let event = CueEvent(runningThreadID: "conversation-fixture", title: "对话查看与待续验收", date: Date())
        var handledCalls = 0
        let controller = CueConversationController(home: home, event: event, detailProvider: { nil }, onHandled: { handledCalls += 1 }, onCodex: {}, onConnect: {}, onPathAction: { _ in })
        controller.fixtureMode = true
        controller.composer.string = "下次要继续的消息"; controller.note.string = "先保留，方便时补验收。"
        let closed = controller.windowShouldClose(controller.window!)
        let saved = CueConversationDrafts(home: home).values(event.threadID)
        var results = ["passive_detail_close_keeps_draft_note_without_handling": closed && saved["draft"] == controller.composer.string && saved["note"] == controller.note.string && handledCalls == 0]
        let second = CueConversationDrafts(home: home)
        second.save("another-thread", draft: "另一窗口草稿", note: "")
        controller.saveDraft()
        results["saving_one_window_preserves_other_window_draft"] = CueConversationDrafts(home: home).values("another-thread")["draft"] == "另一窗口草稿"
        controller.applyTranscript(["thread_id": event.threadID, "source_readable": false, "fingerprint": "failed-source"])
        results["unreadable_source_never_poisoned_retry_fingerprint"] = controller.fingerprint.isEmpty
        results["failed_source_read_backs_off_without_losing_body"] = controller.readDelay == 30
        controller.pendingRequestID = "send-one"; controller.composer.string = "实际发送的内容"
        controller.applySendResult(["status": "unverified", "thread_id": event.threadID, "request_id": "send-one"], sentText: "实际发送的内容", requestID: "send-one")
        results["unconfirmed_send_keeps_draft_and_blocks_duplicate_retry"] = controller.pendingRequestID == "send-one" && controller.composer.string == "实际发送的内容"
        controller.applySendResult(["status": "accepted", "thread_id": "wrong-thread", "request_id": "send-one"], sentText: "实际发送的内容", requestID: "send-one")
        results["mismatched_send_receipt_cannot_clear_draft"] = !controller.pendingRequestID.isEmpty && !controller.composer.string.isEmpty
        controller.composer.string = "发送期间编辑的新内容"
        controller.applySendResult(["status": "accepted", "thread_id": event.threadID, "request_id": "send-one"], sentText: "实际发送的内容", requestID: "send-one")
        results["accepted_send_preserves_new_edits_and_never_marks_task_handled"] = controller.pendingRequestID.isEmpty && controller.composer.string == "发送期间编辑的新内容" && handledCalls == 0
        controller.applyChatStatus(["supported": false, "connected": true, "state": "legacy_bridge", "can_send": false, "message": "旧通道待重新连接"])
        results["legacy_connection_does_not_enable_send_or_load"] = !controller.sendButton.isEnabled && !controller.loadButton.isEnabled
        controller.applyChatStatus(["supported": true, "connected": true, "transport": "desktop_ipc", "state": "active", "can_send": true, "active_turn_id": "current-turn", "message": "已接通当前对话"])
        results["existing_desktop_owner_enables_send_without_restart_or_load"] = controller.sendButton.isEnabled && controller.sendButton.title == "发送到当前对话" && controller.connectButton.isHidden && controller.loadButton.isHidden
        controller.applyChatStatus(["supported": true, "connected": true, "transport": "desktop_ipc", "state": "waiting", "can_send": false, "message": "请先在 Codex 回答当前问题"])
        results["desktop_pending_input_blocks_send_with_actionable_reason"] = !controller.sendButton.isEnabled && controller.sendButton.toolTip == "请先在 Codex 回答当前问题"
        controller.markHandled()
        results["explicit_handled_button_is_the_only_completion_action"] = handledCalls == 1
        let blocked = home.appendingPathComponent("blocked-home"); try? Data("file".utf8).write(to: blocked)
        let failing = CueConversationController(home: blocked, event: event, detailProvider: { nil }, onHandled: { handledCalls += 1 }, onCodex: {}, onConnect: {}, onPathAction: { _ in })
        failing.fixtureMode = true; failing.composer.string = "不能丢失的编辑"
        let canClose = failing.windowShouldClose(failing.window!)
        failing.markHandled()
        results["failed_draft_save_prevents_close_and_handled_action"] = !canClose && handledCalls == 1
        return results
    }

    static func captureFixture(to url: URL, dark: Bool) throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent("vibecue-conversation-capture-" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        let event = CueEvent(runningThreadID: "conversation-fixture", title: "VibeCue｜对话查看与稍后继续", date: Date())
        let controller = CueConversationController(home: home, event: event, detailProvider: { nil }, onHandled: {}, onCodex: {}, onConnect: {}, onPathAction: { _ in })
        controller.fixtureMode = true; controller.window?.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        controller.applyTranscript(["thread_id": event.threadID, "cwd": "/Users/example/Projects/VibeCue", "source_readable": true, "coverage": ["partial": true], "messages": [
            ["role": "user", "text": "本轮工作已完成，但还有几个后续需求，等我有空再继续。", "created_at": Date().timeIntervalSince1970 - 120],
            ["role": "assistant", "text": "已完成本轮修改并通过检查。后续可以继续完善设置面板与交互细节。\n\n你可以直接关闭这个窗口，任务仍会保留在待续列表；等方便时再回来。", "created_at": Date().timeIntervalSince1970 - 80]
        ]])
        controller.applyChatStatus(["connected": true, "supported": true, "state": "idle", "can_send": true, "max_text_bytes": 8192, "message": "已接通 Codex，可以直接回复。发送后开始新一轮，沿用当前模型与权限设置。"])
        controller.note.string = "下次：检查移动端布局，补一轮实际体验。"; controller.composer.string = "我准备继续了，先从上次的后续需求开始。"; controller.updateControls()
        controller.root.layoutSubtreeIfNeeded()
        guard let bitmap = controller.root.bitmapImageRepForCachingDisplay(in: controller.root.bounds) else { throw CocoaError(.fileWriteUnknown) }
        controller.root.cacheDisplay(in: controller.root.bounds, to: bitmap)
        guard let data = bitmap.representation(using: .png, properties: [:]) else { throw CocoaError(.fileWriteUnknown) }
        try data.write(to: url)
    }
}
