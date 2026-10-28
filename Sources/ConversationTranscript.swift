import AppKit
import WebKit

private final class CueTranscriptBridge: NSObject, WKScriptMessageHandler {
    weak var owner: CueConversationTranscriptView?
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame, let owner, owner.isRendererURL(message.frameInfo.request.url) else { return }
        owner.receive(message.body)
    }
}

/// The conversation document renders offline. User Markdown never becomes
/// script, and the WebView can read only its packaged renderer directory.
final class CueConversationTranscriptView: NSView, WKNavigationDelegate {
    let webView: WKWebView
    private let bridge = CueTranscriptBridge()
    private var documentURL: URL?
    private var payload: [String: Any] = ["messages": [], "state": "loading"]
    private(set) var isReady = false
    private(set) var renderError = ""
    var onAction: ((String, String) -> Void)?
    override var isFlipped: Bool { true }

    override init(frame: NSRect) {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.mediaTypesRequiringUserActionForPlayback = .all
        configuration.suppressesIncrementalRendering = false
        webView = WKWebView(frame: .zero, configuration: configuration)
        super.init(frame: frame)
        bridge.owner = self
        configuration.userContentController.add(bridge, name: "vibecue")
        webView.navigationDelegate = self
        webView.setValue(false, forKey: "drawsBackground")
        webView.setAccessibilityLabel("Codex 格式的当前对话")
        addSubview(webView)
        let resource = Bundle.main.resourceURL?.appendingPathComponent("Conversation/index.html")
        let development = URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent("assets/Conversation/index.html")
        documentURL = [resource, development].compactMap { $0 }.first { FileManager.default.fileExists(atPath: $0.path) }
        if let documentURL {
            // Defense in depth alongside the renderer's restrictive CSP.
            let rules = #"[{"trigger":{"url-filter":"^https?://"},"action":{"type":"block"}}]"#
            WKContentRuleListStore.default().compileContentRuleList(forIdentifier: "VibeCueConversationOffline", encodedContentRuleList: rules) { [weak self] list, _ in
                guard let self else { return }
                if let list { self.webView.configuration.userContentController.add(list) }
                self.webView.loadFileURL(documentURL, allowingReadAccessTo: documentURL.deletingLastPathComponent())
            }
        } else { renderError = "对话排版资源尚未安装，请更新 VibeCue。" }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    deinit { webView.configuration.userContentController.removeScriptMessageHandler(forName: "vibecue") }
    override func layout() { super.layout(); webView.frame = bounds }
    override func viewDidChangeEffectiveAppearance() { super.viewDidChangeEffectiveAppearance(); flush() }

    func update(messages: [[String: Any]], activities: [[String: Any]] = [], state: String = "ready", message: String = "") {
        payload = ["messages": messages, "activities": activities, "state": state, "message": message]
        flush()
    }
    private func flush() {
        guard isReady else { return }
        var values = payload
        values["theme"] = effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? "dark" : "light"
        webView.callAsyncJavaScript("window.VibeCueTranscript.update(payload)", arguments: ["payload": values], in: nil, in: .page) { [weak self] result in
            if case .failure = result { self?.renderError = "对话排版暂不可用，请刷新。" }
        }
    }
    fileprivate func receive(_ raw: Any) {
        guard let value = raw as? [String: Any], let action = value["action"] as? String else { return }
        if action == "ready" { isReady = true; flush(); return }
        switch action {
        case "copy":
            guard let text = value["text"] as? String, text.utf8.count <= 262_144 else { return }
            NSPasteboard.general.clearContents(); NSPasteboard.general.setString(text, forType: .string)
        case "openLink": if let url = value["url"] as? String, url.utf8.count <= 8192 { onAction?(action, url) }
        case "openAttachment": if let path = value["path"] as? String, path.utf8.count <= 4096 { onAction?(action, path) }
        default: break
        }
    }
    fileprivate func isRendererURL(_ url: URL?) -> Bool { url?.isFileURL == true && url?.standardizedFileURL == documentURL?.standardizedFileURL }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        webView.evaluateJavaScript("typeof window.VibeCueTranscript?.update === 'function'") { [weak self] ready, _ in
            guard let self, ready as? Bool == true else { return }; self.isReady = true; self.flush()
        }
    }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { renderError = "对话排版暂不可用，请刷新。" }
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { decisionHandler(.cancel); return }
        if url.isFileURL, url.standardizedFileURL == documentURL?.standardizedFileURL { decisionHandler(.allow); return }
        decisionHandler(.cancel)
        if action.navigationType == .linkActivated { onAction?("openLink", url.absoluteString) }
    }
    func reload() { isReady = false; renderError = ""; if let documentURL { webView.loadFileURL(documentURL, allowingReadAccessTo: documentURL.deletingLastPathComponent()) } }
    func evaluate(_ script: String, completion: @escaping (Any?, Error?) -> Void) { webView.evaluateJavaScript(script, completionHandler: completion) }
}
