import AppKit
import Foundation
import WebKit

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

private enum CueConversationColors {
    static let background = NSColor(name: nil) { $0.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? NSColor(srgbRed: 24/255, green: 24/255, blue: 24/255, alpha: 1) : .white }
    static let input = NSColor(name: nil) { $0.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? NSColor(white: 0.15, alpha: 1) : NSColor(white: 0.965, alpha: 1) }
    static let border = NSColor(name: nil) { $0.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? NSColor(white: 1, alpha: 0.13) : NSColor(white: 0, alpha: 0.12) }
}
private final class CueConversationSurface: NSView {
    var onLayout: (() -> Void)?
    var onAppearance: (() -> Void)?
    override var isFlipped: Bool { true }
    override func layout() { super.layout(); onLayout?() }
    override func draw(_ dirtyRect: NSRect) { CueConversationColors.background.setFill(); dirtyRect.fill() }
    override func viewDidChangeEffectiveAppearance() { super.viewDidChangeEffectiveAppearance(); needsDisplay = true; onAppearance?() }
}
private final class CueConversationComposer: NSView {
    override var isFlipped: Bool { true }
    override func draw(_ dirtyRect: NSRect) {
        let path = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: 22, yRadius: 22)
        CueConversationColors.input.setFill(); path.fill(); CueConversationColors.border.setStroke(); path.lineWidth = 1; path.stroke()
    }
}
private final class CueConversationPlaceholder: NSTextField {
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
}
private final class CueConversationSendButton: NSButton {
    override var isEnabled: Bool { didSet { needsDisplay = true } }
    override func draw(_ dirtyRect: NSRect) {
        let dark = effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        let fill: NSColor = isEnabled ? (dark ? .white : NSColor(white: 0.10, alpha: 1)) : NSColor.secondaryLabelColor.withAlphaComponent(0.18)
        fill.setFill(); NSBezierPath(ovalIn: bounds.insetBy(dx: 1, dy: 1)).fill()
        let ink: NSColor = isEnabled ? (dark ? NSColor(white: 0.1, alpha: 1) : .white) : .tertiaryLabelColor
        let symbol = NSImage(systemSymbolName: "arrow.up", accessibilityDescription: nil)?.withSymbolConfiguration(.init(pointSize: 15, weight: .semibold).applying(.init(paletteColors: [ink])))
        symbol?.draw(in: NSRect(x: (bounds.width - 16)/2, y: (bounds.height - 18)/2, width: 16, height: 18), from: .zero, operation: .sourceOver, fraction: isHighlighted ? 0.65 : 1, respectFlipped: true, hints: nil)
    }
}

final class CueConversationController: NSWindowController, NSWindowDelegate, NSTextViewDelegate {
    let event: CueEvent
    var onClosed: (() -> Void)?
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
    private let transcript = CueConversationTranscriptView()
    private let composer = NSTextView(), note = NSTextView()
    private let composerSurface = CueConversationComposer()
    private let composeScroll = NSScrollView(), noteScroll = NSScrollView()
    private let noteToggle = NSButton(title: "", target: nil, action: nil)
    private let moreButton = NSButton(title: "", target: nil, action: nil)
    private let inputPlaceholder = CueConversationPlaceholder(labelWithString: "继续这个对话…")
    private var notesVisible = false
    private var attachmentPaths = Set<String>()
    private var projectPath = ""
    private let codexButton = NSButton(title: "在 Codex 打开", target: nil, action: nil)
    private let doneButton = NSButton(title: "标记处理完成", target: nil, action: nil)
    private let laterButton = NSButton(title: "稍后继续", target: nil, action: nil)
    private let loadButton = NSButton(title: "载入对话", target: nil, action: nil)
    private let connectButton = NSButton(title: "接通对话输入…", target: nil, action: nil)
    private let sendButton = CueConversationSendButton(title: "发送到 Codex", target: nil, action: nil)
    private let pathButton = NSButton(title: "访达打开", target: nil, action: nil)
    private let refreshButton = NSButton(title: "刷新", target: nil, action: nil)
    private let unlockButton = NSButton(title: "已核对，解锁发送", target: nil, action: nil)
    private var timer: Timer?
    private var reading = false, checking = false, sending = false, loading = false
    var hasInFlightOperation: Bool { sending || loading }
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
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1000, height: 820), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "VibeCue · " + event.title; window.isReleasedWhenClosed = false
        window.contentMinSize = NSSize(width: 640, height: 580); window.titlebarAppearsTransparent = true; window.backgroundColor = CueConversationColors.background; window.center()
        super.init(window: window); window.delegate = self; window.contentView = root
        titleField.stringValue = event.title; titleField.font = .systemFont(ofSize: 14, weight: .semibold); titleField.lineBreakMode = .byTruncatingTail; titleField.toolTip = event.title
        summary.font = .monospacedDigitSystemFont(ofSize: 10.5, weight: .regular); summary.textColor = .secondaryLabelColor; summary.lineBreakMode = .byTruncatingTail
        project.font = .systemFont(ofSize: 11); project.textColor = .secondaryLabelColor; project.lineBreakMode = .byTruncatingMiddle
        source.font = .systemFont(ofSize: 10.5); source.textColor = .tertiaryLabelColor; source.lineBreakMode = .byTruncatingTail
        connection.font = .systemFont(ofSize: 10.5); connection.textColor = .secondaryLabelColor; connection.maximumNumberOfLines = 1; connection.lineBreakMode = .byTruncatingTail
        feedback.font = .systemFont(ofSize: 10.5); feedback.textColor = .secondaryLabelColor; feedback.maximumNumberOfLines = 2
        draftHint.font = .systemFont(ofSize: 10); draftHint.textColor = .secondaryLabelColor; draftHint.lineBreakMode = .byTruncatingTail
        count.font = .monospacedDigitSystemFont(ofSize: 10, weight: .regular); count.textColor = .secondaryLabelColor; count.alignment = .right
        noteTitle.font = .systemFont(ofSize: 13, weight: .semibold); noteHint.font = .systemFont(ofSize: 11); noteHint.textColor = .secondaryLabelColor
        for (view, scroll) in [(composer, composeScroll), (note, noteScroll)] {
            scroll.documentView = view; scroll.hasVerticalScroller = true; scroll.autohidesScrollers = true; scroll.borderType = .noBorder; scroll.drawsBackground = false
            view.isEditable = true; view.isSelectable = true; view.isRichText = false; view.drawsBackground = false; view.textColor = .labelColor
            view.font = .systemFont(ofSize: 14); view.textContainerInset = NSSize(width: 4, height: 10)
            view.isVerticallyResizable = true; view.isHorizontallyResizable = false
            view.autoresizingMask = [.width]; view.textContainer?.widthTracksTextView = true
            view.textContainer?.containerSize = NSSize(width: 600, height: CGFloat.greatestFiniteMagnitude)
        }
        composer.isAutomaticQuoteSubstitutionEnabled = false; composer.isAutomaticDashSubstitutionEnabled = false
        composer.delegate = self; note.delegate = self
        composer.setAccessibilityLabel("发送给当前 Codex 对话的消息"); note.setAccessibilityLabel("下次继续的本机备注")
        for button in [codexButton, doneButton, laterButton, loadButton, connectButton, sendButton, pathButton, refreshButton, unlockButton] { button.bezelStyle = .rounded; button.target = self }
        codexButton.action = #selector(openCodex); doneButton.action = #selector(markHandled); laterButton.action = #selector(closeForLater)
        sendButton.action = #selector(send); loadButton.action = #selector(loadThread); connectButton.action = #selector(connectCodex)
        pathButton.action = #selector(revealProject); refreshButton.action = #selector(refresh)
        unlockButton.action = #selector(unlockSend); unlockButton.toolTip = "请先核对消息是否已经送达。此按钮只允许再次发送，不会自动重发。"
        sendButton.identifier = NSUserInterfaceItemIdentifier("conversation.send"); doneButton.identifier = NSUserInterfaceItemIdentifier("conversation.handled"); laterButton.identifier = NSUserInterfaceItemIdentifier("conversation.later")
        noteToggle.identifier = NSUserInterfaceItemIdentifier("conversation.notes"); noteToggle.target = self; noteToggle.action = #selector(toggleNotes)
        noteToggle.image = NSImage(systemSymbolName: "sidebar.right", accessibilityDescription: "备注"); noteToggle.imagePosition = .imageOnly; noteToggle.isBordered = false; noteToggle.toolTip = "下次从这里继续 · 备注"; noteToggle.setAccessibilityLabel("展开或收起待续备注")
        codexButton.image = NSImage(systemSymbolName: "arrow.up.forward.square", accessibilityDescription: nil); codexButton.imagePosition = .imageOnly; codexButton.isBordered = false; codexButton.toolTip = "在 Codex 打开此对话"; codexButton.setAccessibilityLabel("在 Codex 打开")
        doneButton.title = "标记完成"; doneButton.font = .systemFont(ofSize: 11); doneButton.isBordered = false
        sendButton.isBordered = false; sendButton.setAccessibilityLabel("发送到当前对话")
        moreButton.image = NSImage(systemSymbolName: "ellipsis", accessibilityDescription: "更多对话操作"); moreButton.imagePosition = .imageOnly; moreButton.isBordered = false; moreButton.target = self; moreButton.action = #selector(showMore)
        moreButton.menu = NSMenu()
        for (title, selector) in [("刷新对话", #selector(refresh)), ("在访达中打开项目", #selector(revealProject)), ("稍后继续", #selector(closeForLater))] { let item = NSMenuItem(title: title, action: selector, keyEquivalent: ""); item.target = self; moreButton.menu?.addItem(item) }
        moreButton.setAccessibilityLabel("更多对话操作")
        inputPlaceholder.font = .systemFont(ofSize: 14); inputPlaceholder.textColor = .tertiaryLabelColor; inputPlaceholder.isSelectable = false
        sendButton.keyEquivalent = "\r"; sendButton.keyEquivalentModifierMask = [.command]
        sendButton.toolTip = "发送到当前对话（⌘↩）。不会自动发送草稿。"
        let views: [NSView] = [titleField, summary, project, source, connection, feedback, draftHint, count, noteTitle, noteHint, transcript, composerSurface, noteScroll, codexButton, doneButton, laterButton, loadButton, connectButton, sendButton, pathButton, refreshButton, unlockButton, noteToggle, moreButton, inputPlaceholder]
        views.forEach { root.addSubview($0) }; root.onLayout = { [weak self] in self?.layout() }
        composerSurface.addSubview(composeScroll)
        // The native editor stays above its background, while all prompt text
        // remains outside the WebView and its scripting environment.
        root.addSubview(composerSurface, positioned: .below, relativeTo: nil)
        root.onAppearance = { [weak self] in self?.composerSurface.needsDisplay = true; self?.sendButton.needsDisplay = true; self?.window?.backgroundColor = CueConversationColors.background }
        transcript.onAction = { [weak self] action, value in self?.handleTranscriptAction(action, value: value) }
        [pathButton, refreshButton].forEach { $0.isHidden = true }
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
    func windowWillClose(_ notification: Notification) { timer?.invalidate(); timer = nil; saveDraft(); if !hasInFlightOperation { onClosed?() }; /* Closing never handles a task. */ }
    func windowShouldClose(_ sender: NSWindow) -> Bool { saveDraft() }
    func stop() { timer?.invalidate(); timer = nil; saveDraft(); client.stop() }
    private func releaseClosedWindowIfIdle() {
        if window?.isVisible != true && !hasInFlightOperation { onClosed?() }
    }

    private func layout() {
        let w = root.bounds.width, h = root.bounds.height
        let sidebar: CGFloat = notesVisible ? min(248, max(210, w * 0.28)) : 0
        let mainWidth = w - sidebar
        let column = min(768, mainWidth - 40), left = (mainWidth - column) / 2
        titleField.frame = NSRect(x: 24, y: 12, width: max(160, w - 296), height: 22)
        summary.frame = NSRect(x: 24, y: 40, width: max(120, mainWidth - 210), height: 17)
        project.frame = NSRect(x: max(24, mainWidth - 179), y: 40, width: 155, height: 17)
        project.alignment = .right
        codexButton.frame = NSRect(x: w - 244, y: 8, width: 30, height: 28)
        noteToggle.frame = NSRect(x: w - 209, y: 8, width: 30, height: 28)
        doneButton.frame = NSRect(x: w - 174, y: 9, width: 112, height: 27)
        moreButton.frame = NSRect(x: w - 53, y: 8, width: 30, height: 28)
        source.frame = NSRect(x: left, y: 65, width: column, height: 15)
        composer.textContainer?.containerSize = NSSize(width: max(120, column - 32), height: CGFloat.greatestFiniteMagnitude)
        if let container = composer.textContainer { composer.layoutManager?.ensureLayout(for: container) }
        let textHeight = composer.textContainer.flatMap { composer.layoutManager?.usedRect(for: $0).height } ?? 20
        let inputHeight = min(154, max(44, ceil(textHeight) + 20))
        let composeHeight = inputHeight + 42
        let bottom: CGFloat = feedback.stringValue.isEmpty ? 18 : 49
        let composeY = h - bottom - composeHeight
        composerSurface.frame = NSRect(x: left, y: composeY, width: column, height: composeHeight)
        composeScroll.frame = NSRect(x: 12, y: 4, width: column - 24, height: inputHeight)
        composer.setFrameSize(NSSize(width: composeScroll.contentSize.width, height: max(textHeight + 20, inputHeight)))
        inputPlaceholder.frame = NSRect(x: left + 17, y: composeY + 15, width: column - 56, height: 21)
        inputPlaceholder.isHidden = !composer.string.isEmpty
        sendButton.frame = NSRect(x: left + column - 44, y: composeY + composeHeight - 39, width: 32, height: 32)
        draftHint.frame = NSRect(x: left + 16, y: composeY + composeHeight - 30, width: max(110, column - 196), height: 17)
        draftHint.stringValue = "Enter 发送 · ⇧Enter 换行"
        count.frame = NSRect(x: left + column - 186, y: composeY + composeHeight - 30, width: 129, height: 17)
        count.isHidden = composer.string.utf8.count < 1024
        let actionWidth: CGFloat = (!loadButton.isHidden ? 90 : 0) + (!connectButton.isHidden ? 112 : 0) + (!unlockButton.isHidden ? 165 : 0)
        connection.frame = NSRect(x: left + 2, y: composeY - 25, width: max(140, column - actionWidth - 12), height: 18)
        connection.toolTip = connection.stringValue
        var actionX = left + column
        for (button, width) in [(unlockButton, CGFloat(160)), (connectButton, 107), (loadButton, 85)] where !button.isHidden {
            actionX -= width; button.frame = NSRect(x: actionX, y: composeY - 31, width: width - 4, height: 26)
        }
        transcript.frame = NSRect(x: 0, y: 82, width: mainWidth, height: max(170, composeY - 119))
        feedback.frame = NSRect(x: left + 2, y: h - 39, width: column - 4, height: 32)
        feedback.isHidden = feedback.stringValue.isEmpty
        let notesShown = notesVisible
        [noteTitle, noteHint, noteScroll, laterButton].forEach { $0.isHidden = !notesShown }
        if notesShown {
            let sideX: CGFloat = sidebar > 0 ? mainWidth + 16 : max(20, w - 302)
            let sideWidth: CGFloat = sidebar > 0 ? sidebar - 32 : 282
            noteTitle.frame = NSRect(x: sideX, y: 91, width: sideWidth, height: 23)
            noteHint.frame = NSRect(x: sideX, y: 125, width: sideWidth, height: 48)
            noteScroll.frame = NSRect(x: sideX, y: 179, width: sideWidth, height: max(150, h - 250))
            note.drawsBackground = true; note.backgroundColor = CueConversationColors.input
            note.setFrameSize(NSSize(width: noteScroll.contentSize.width, height: max(note.frame.height, noteScroll.contentSize.height)))
            laterButton.frame = NSRect(x: sideX, y: h - 55, width: sideWidth, height: 30)
        }
    }
    private func refreshMetrics() {
        if let detail = detailProvider() {
            let state = ["running": "运行中", "waiting": "等待你处理", "complete": "本轮已结束", "aborted": "已中断", "interrupted": "已中断"][detail.status] ?? "待续"
            summary.stringValue = state + " · " + detail.summaryLine() + " · " + detail.secondaryLine
            summary.toolTip = summary.stringValue + "\n" + detail.settingsTooltip
        } else { summary.stringValue = "待续 · 关闭后继续保留" }
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
                if self.bodySignature.isEmpty { self.transcript.update(messages: [], state: "empty", message: "当前记录暂时无法读取，可在 Codex 中打开或稍后刷新。") }
            }
        }
    }
    private func applyTranscript(_ data: [String: Any]) {
        guard data["thread_id"] as? String == event.threadID else { return }
        if let cwd = data["cwd"] as? String, !cwd.isEmpty { projectPath = cwd; project.stringValue = URL(fileURLWithPath: cwd).lastPathComponent; project.toolTip = cwd; pathButton.isEnabled = true }
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
        guard readable else { if bodySignature.isEmpty { transcript.update(messages: [], state: "empty", message: data["message"] as? String ?? "当前对话记录暂不可用。") }; return }
        if let title = data["title"] as? String, !title.isEmpty { titleField.stringValue = title; titleField.toolTip = title; window?.title = "VibeCue · " + title }
        let messages = data["messages"] as? [[String: Any]] ?? []
        attachmentPaths = Set(messages.flatMap { $0["attachments"] as? [[String: Any]] ?? [] }.compactMap { row in
            guard let path = row["path"] as? String, path.hasPrefix("/") else { return nil }
            return URL(fileURLWithPath: path).standardizedFileURL.path
        })
        render(messages, activities: data["activities"] as? [[String: Any]] ?? [])
    }
    private func render(_ rows: [[String: Any]], activities: [[String: Any]] = []) {
        guard let data = try? JSONSerialization.data(withJSONObject: ["messages": rows, "activities": activities], options: [.sortedKeys]), String(data: data, encoding: .utf8) != bodySignature else { return }
        bodySignature = String(data: data, encoding: .utf8) ?? ""
        transcript.update(messages: Array(rows.suffix(60)), activities: activities, state: rows.isEmpty ? "empty" : "ready")
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
        doneButton.isEnabled = !sending && !loading; doneButton.title = handled ? "已标记完成" : "标记完成"
        doneButton.attributedTitle = NSAttributedString(string: doneButton.title, attributes: [.font: NSFont.systemFont(ofSize: 11), .foregroundColor: doneButton.isEnabled ? NSColor.secondaryLabelColor : NSColor.tertiaryLabelColor])
        connectButton.title = "重试连接"
        unlockButton.isHidden = pendingRequestID.isEmpty || sending
        unlockButton.isEnabled = !sending && !loading
        sendButton.needsDisplay = true; layout()
    }
    func textDidChange(_ notification: Notification) {
        updateControls(); saveWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.saveDraft() }; saveWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4, execute: work)
    }
    func textView(_ textView: NSTextView, doCommandBy commandSelector: Selector) -> Bool {
        guard textView === composer, NSStringFromSelector(commandSelector) == "insertNewline:",
              !textView.hasMarkedText(), !NSEvent.modifierFlags.contains(.shift), !NSEvent.modifierFlags.contains(.option) else { return false }
        if sendButton.isEnabled { send() }
        else if !composer.string.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            feedback.stringValue = !pendingRequestID.isEmpty ? "上次发送结果待核对，草稿已保留。" : connection.stringValue
            layout()
        }
        return true
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
            self.saveDraft(); self.updateControls()
            if self.window?.isVisible == true { self.lastRead = .distantPast; self.readConversation(); self.checkConnection(force: true) }
            else { self.releaseClosedWindowIfIdle() }
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
            self.updateControls()
            if self.window?.isVisible == true { self.checkConnection(force: true) }
            else { self.releaseClosedWindowIfIdle() }
        }
    }
    @objc private func markHandled() { guard !sending && !loading && saveDraft() else { return }; handled = true; onHandled(); summary.stringValue = "已标记处理完成 · 有新提醒时仍会再次通知"; updateControls(); close() }
    @objc private func closeForLater() { window?.performClose(nil) }
    @objc private func showMore() { moreButton.menu?.popUp(positioning: nil, at: NSPoint(x: 0, y: moreButton.bounds.maxY + 4), in: moreButton) }
    @objc private func toggleNotes() { notesVisible.toggle(); noteToggle.contentTintColor = notesVisible ? .controlAccentColor : .secondaryLabelColor; layout(); if notesVisible { window?.makeFirstResponder(note) } }
    @objc private func openCodex() { saveDraft(); onCodex() }
    @objc private func connectCodex() { onConnect() }
    @objc private func revealProject() { onPathAction("reveal_project") }
    @objc private func refresh() { fingerprint = ""; readDelay = 1.5; lastRead = .distantPast; if !transcript.renderError.isEmpty { transcript.reload() }; readConversation(); checkConnection(force: true) }
    private func handleTranscriptAction(_ action: String, value: String) {
        if action == "openAttachment" {
            let url = URL(fileURLWithPath: value).standardizedFileURL
            guard attachmentPaths.contains(url.path), FileManager.default.fileExists(atPath: url.path) else { return }
            if ["png", "jpg", "jpeg", "heic", "gif", "webp", "pdf"].contains(url.pathExtension.lowercased()) { NSWorkspace.shared.open(url) }
            else { NSWorkspace.shared.activateFileViewerSelecting([url]) }
            return
        }
        guard action == "openLink", let url = URL(string: value) else { return }
        if ["https", "http", "mailto"].contains(url.scheme?.lowercased() ?? ""), url.user == nil, url.password == nil {
            NSWorkspace.shared.open(url); return
        }
        if url.scheme == "codex", url.host == "threads" { NSWorkspace.shared.open(url); return }
        var path = value.removingPercentEncoding ?? value
        if url.isFileURL { path = url.path }
        else if url.scheme != nil { return }
        path = path.replacingOccurrences(of: #"(?:(?::\d+)(?::\d+)?|#L\d+(?:-L\d+)?)$"#, with: "", options: .regularExpression)
        let local: URL
        if path.hasPrefix("/") { local = URL(fileURLWithPath: path).standardizedFileURL }
        else {
            guard projectPath.hasPrefix("/") else { return }
            local = URL(fileURLWithPath: projectPath, isDirectory: true).appendingPathComponent(path).standardizedFileURL
            guard local.path.hasPrefix(URL(fileURLWithPath: projectPath, isDirectory: true).standardizedFileURL.path + "/") else { return }
        }
        guard FileManager.default.fileExists(atPath: local.path) else { feedback.stringValue = "此文件目前不在本机。"; layout(); return }
        // A document link reveals a known local item; it never executes a
        // script, command or application embedded in conversation Markdown.
        NSWorkspace.shared.activateFileViewerSelecting([local])
    }
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
        var released = 0
        controller.onClosed = { released += 1 }
        controller.sending = true
        controller.windowWillClose(Notification(name: NSWindow.willCloseNotification, object: controller.window))
        controller.releaseClosedWindowIfIdle()
        results["closing_during_send_keeps_helper_alive_until_receipt"] = released == 0 && controller.hasInFlightOperation
        controller.sending = false; controller.loading = true
        controller.releaseClosedWindowIfIdle()
        results["closing_during_load_keeps_helper_alive_until_result"] = released == 0
        controller.loading = false
        controller.releaseClosedWindowIfIdle()
        results["closed_window_releases_only_after_operation_finishes"] = released == 1
        controller.onClosed = nil
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

    private static func fixtureController(dark: Bool, width: CGFloat = 1000) -> (CueConversationController, URL) {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent("vibecue-conversation-capture-" + UUID().uuidString)
        let event = CueEvent(runningThreadID: "conversation-fixture", title: "VibeCue｜对话排版与后续任务", date: Date())
        let controller = CueConversationController(home: home, event: event, detailProvider: { nil }, onHandled: {}, onCodex: {}, onConnect: {}, onPathAction: { _ in })
        controller.fixtureMode = true
        controller.window?.setContentSize(NSSize(width: width, height: 820))
        controller.window?.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        controller.window?.setFrameOrigin(NSPoint(x: -10000, y: -10000)); controller.window?.orderFrontRegardless()
        controller.applyTranscript(["thread_id": event.threadID, "title": "VibeCue｜对话排版与后续任务", "cwd": "/Users/example/Projects/VibeCue", "source_readable": true, "coverage": ["partial": true], "messages": [
            ["id": "user-one", "turn_id": "turn-one", "role": "user", "phase": "user", "text": "把对话窗口按 Codex 的格式整理一下，代码和表格要能正常阅读。后续需求我会稍后继续。"],
            ["id": "comment-one", "turn_id": "turn-one", "role": "assistant", "phase": "commentary", "text": "我会对照当前排版，先检查内容和交互，再调整阅读区与输入区。"],
            ["id": "final-one", "turn_id": "turn-one", "role": "assistant", "phase": "final_answer", "text": "正文已按 Codex 的阅读方式整理。\n\n## 本次调整\n\n- **阅读区**：保留清晰的层级，减少常驻标签和边框。\n- **代码与表格**：正常排版，支持复制与横向滚动。\n- **待续记录**：关闭窗口后保留，备注可以按需展开。\n\n```swift\nlet status = ConversationState.connected\nawait conversation.send(message)\n```\n\n| 项目 | 状态 |\n| --- | --- |\n| Markdown 排版 | 已完成 |\n| 草稿与备注 | 自动保存 |\n\n> 本轮结束不等于所有需求都处理完。你可以在方便时继续。"]
        ], "activities": [["id": "tool-one", "turn_id": "turn-one", "name": "read_file", "label": "检查对话窗口与排版资源", "status": "complete"], ["id": "tool-two", "turn_id": "turn-one", "name": "exec_command", "label": "运行本机验证", "status": "complete"]]])
        controller.applyChatStatus(["connected": true, "supported": true, "transport": "desktop_ipc", "state": "idle", "can_send": true, "max_text_bytes": 8192, "message": "已连接 · 消息将发送到当前 Codex 对话"])
        controller.note.string = "下次：检查长对话和小窗口下的阅读体验。"
        controller.composer.string = "先保留这项，等我准备好再继续。"; controller.updateControls()
        if CommandLine.arguments.contains("--notes") { controller.toggleNotes() }
        controller.root.layoutSubtreeIfNeeded()
        return (controller, home)
    }
    private func waitForRenderer() -> Bool {
        let deadline = Date().addingTimeInterval(20)
        while !transcript.isReady && transcript.renderError.isEmpty && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.025)) }
        guard transcript.isReady else { return false }
        var ready = false, querying = false
        while !ready && Date() < deadline {
            if !querying {
                querying = true
                transcript.evaluate("document.querySelectorAll('.message').length >= 3") { value, _ in ready = value as? Bool == true; querying = false }
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.025))
        }
        return ready
    }
    static func rendererValidationReport() -> [String: Bool] {
        let (controller, home) = fixtureController(dark: true)
        defer { controller.window?.orderOut(nil); controller.stop(); try? FileManager.default.removeItem(at: home) }
        let ready = controller.waitForRenderer()
        var results = ["conversation_offline_webkit_document_loads": ready,
                       "conversation_notes_are_collapsed_by_default": !controller.notesVisible && controller.noteScroll.isHidden,
                       "conversation_composer_and_body_share_reading_axis": controller.composerSurface.frame.width == 768 && controller.transcript.frame.width == controller.root.bounds.width,
                       "conversation_input_is_native_editable_and_plain_text": controller.composer.isEditable && !controller.composer.isRichText]
        guard ready else { return results }
        var response: [String: Any]?
        controller.transcript.evaluate("({headings:document.querySelectorAll('.message-body h2').length, tables:document.querySelectorAll('.message-body table').length, code:document.querySelectorAll('.code-block').length, copies:document.querySelectorAll('[data-action=copy-code]').length, roles:document.querySelectorAll('.message-user').length, activities:document.querySelectorAll('details').length, invalidNavigation:document.querySelectorAll('a[href],iframe,img').length, theme:document.documentElement.dataset.theme})") { value, _ in response = value as? [String: Any] }
        let deadline = Date().addingTimeInterval(5)
        while response == nil && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.025)) }
        results["conversation_markdown_headings_tables_and_code_are_rendered"] = response?["headings"] as? Int == 1 && response?["tables"] as? Int == 1 && response?["code"] as? Int == 1
        results["conversation_code_has_copy_control"] = response?["copies"] as? Int == 1
        results["conversation_tools_are_collapsible_and_users_are_distinct"] = response?["activities"] as? Int == 1 && response?["roles"] as? Int == 1
        results["conversation_document_has_no_automatic_links_frames_or_images"] = response?["invalidNavigation"] as? Int == 0
        results["conversation_follows_native_dark_appearance"] = response?["theme"] as? String == "dark"
        controller.toggleNotes()
        results["conversation_notes_expand_without_consuming_default_body_width"] = controller.notesVisible && !controller.noteScroll.isHidden && controller.transcript.frame.width < controller.root.bounds.width
        return results
    }
    static func captureFixture(to url: URL, dark: Bool) throws {
        let (controller, home) = fixtureController(dark: dark, width: CommandLine.arguments.contains("--narrow") ? 680 : 1000)
        defer { controller.window?.orderOut(nil); controller.stop(); try? FileManager.default.removeItem(at: home) }
        guard controller.waitForRenderer() else { throw CocoaError(.fileReadUnknown) }
        if CommandLine.arguments.contains("--top") {
            var changed = false
            controller.transcript.evaluate("document.scrollingElement.scrollTop = 0") { _, _ in changed = true }
            let deadline = Date().addingTimeInterval(2)
            while !changed && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.025)) }
        }
        let root = controller.root
        root.layoutSubtreeIfNeeded()
        var webImage: NSImage?
        controller.transcript.webView.takeSnapshot(with: nil) { image, _ in webImage = image }
        let deadline = Date().addingTimeInterval(8)
        while webImage == nil && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.025)) }
        guard let webImage, let bitmap = root.bitmapImageRepForCachingDisplay(in: root.bounds) else { throw CocoaError(.fileWriteUnknown) }
        root.effectiveAppearance.performAsCurrentDrawingAppearance { root.cacheDisplay(in: root.bounds, to: bitmap) }
        let composite = NSImage(size: root.bounds.size)
        composite.lockFocus()
        bitmap.draw(in: root.bounds)
        let frame = controller.transcript.frame
        webImage.draw(in: NSRect(x: frame.minX, y: root.bounds.height - frame.maxY, width: frame.width, height: frame.height))
        composite.unlockFocus()
        guard let tiff = composite.tiffRepresentation, let final = NSBitmapImageRep(data: tiff), let png = final.representation(using: .png, properties: [:]) else { throw CocoaError(.fileWriteUnknown) }
        try png.write(to: url)
    }
}
