import AppKit
import Foundation

struct CueBatchTarget {
    let threadID: String
    let title: String
    init?(threadID: String, title: String, provider: String = "codex") {
        guard provider == "codex", !threadID.isEmpty, threadID.count <= 160,
              CueIdentifier.isValid(threadID) else { return nil }
        self.threadID = threadID; self.title = String(title.prefix(180))
    }
    static func unique(_ values: [CueBatchTarget]) -> [CueBatchTarget] {
        var seen = Set<String>()
        return values.filter { seen.insert($0.threadID).inserted }
    }
}

/// Uses only VibeCoding Go's private control module, never sends a chat message.
struct CueControlStreamDecoder {
    private var pending = Data()
    private var received = 0
    private(set) var result: [String: Any]?
    mutating func append(_ data: Data) throws -> [[String: Any]] {
        received += data.count
        guard received <= 8_388_608, result == nil else { throw CueCodexControlClient.error("批量进度数据无效或过大。") }
        pending.append(data)
        var events: [[String: Any]] = []
        while let end = pending.firstIndex(of: 10) {
            let line = pending[..<end]; pending.removeSubrange(...end)
            guard line.count <= 2_097_152,
                  let value = try JSONSerialization.jsonObject(with: line) as? [String: Any],
                  let kind = value["event"] as? String else { throw CueCodexControlClient.error("批量进度格式无效。") }
            if kind == "result", let final = value["result"] as? [String: Any], result == nil { result = final }
            else if kind == "progress", result == nil { events.append(value) }
            else if kind != "finished" || result != nil { throw CueCodexControlClient.error("批量进度顺序无效。") }
        }
        guard pending.count <= 2_097_152 else { throw CueCodexControlClient.error("批量进度数据过大。") }
        return events
    }
    var isComplete: Bool { result != nil && pending.isEmpty }
}

final class CueCodexControlClient {
    let home: URL
    private let collectorOwner = UUID()
    init(home: URL) { self.home = home }
    func run(_ command: String, input: [String: Any]? = nil, progress: (([String: Any]) -> Void)? = nil, completion: @escaping (Result<[String: Any], Error>) -> Void) {
        let home = home
        CueCollectorScheduler.shared.submit(owner: collectorOwner, lane: CueCollectorScheduler.lane(module: "vibecue.codex_control", arguments: [command]), discard: { _ in
            DispatchQueue.main.async { completion(.failure(Self.error("控制请求仍未执行，请稍后再试。"))) }
        }) { finished in
            defer { finished() }
            do {
                let runtime = home.appendingPathComponent("runtime")
                guard FileManager.default.fileExists(atPath: runtime.appendingPathComponent("vibecue/codex_control.py").path) else {
                    throw Self.error("Codex 控制模块尚未安装，请安装最新版 VibeCoding Go。")
                }
                let process = Process(), output = Pipe(), stdin = Pipe()
                process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
                process.arguments = ["-m", "vibecue.codex_control", command] + (progress == nil ? [] : ["--stream"])
                process.qualityOfService = .utility
                process.currentDirectoryURL = runtime
                let inherited = ProcessInfo.processInfo.environment
                var environment: [String: String] = [:]
                for key in ["HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ", "TMPDIR", "CODEX_HOME"] {
                    environment[key] = inherited[key]
                }
                environment["VIBECUE_HOME"] = home.path; environment["PYTHONPATH"] = runtime.path
                environment["PYTHONDONTWRITEBYTECODE"] = "1"; environment["PYTHONUNBUFFERED"] = "1"
                environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
                process.environment = environment; process.standardOutput = output
                process.standardError = FileHandle.nullDevice; process.standardInput = stdin
                try process.run()
                if let input { try stdin.fileHandleForWriting.write(contentsOf: JSONSerialization.data(withJSONObject: input)) }
                try? stdin.fileHandleForWriting.close()
                let timeout = DispatchWorkItem { if process.isRunning { process.terminate() } }
                let count = (input?["thread_ids"] as? [String])?.count ?? 0
                let budget = progress == nil ? 120 : max(120, min(200, count) * 20 + 35)
                DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + .seconds(budget), execute: timeout)
                var data = Data(), exceeded = false, decoder = CueControlStreamDecoder()
                while true {
                    let chunk = output.fileHandleForReading.availableData
                    if chunk.isEmpty { break }
                    if let progress {
                        do {
                            for event in try decoder.append(chunk) { DispatchQueue.main.async { progress(event) } }
                        } catch {
                            if process.isRunning { process.terminate() }
                            process.waitUntilExit(); timeout.cancel(); throw error
                        }
                    } else if data.count + chunk.count <= 2_097_152 { data.append(chunk) }
                    else { exceeded = true; if process.isRunning { process.terminate() } }
                }
                process.waitUntilExit(); timeout.cancel()
                let parsed = progress == nil ? try JSONSerialization.jsonObject(with: data) as? [String: Any] : decoder.result
                guard !exceeded, progress == nil || decoder.isComplete, let result = parsed else {
                    throw Self.error("控制通道未返回有效结果；没有确认的项目不会标为成功。")
                }
                if process.terminationStatus != 0 && result["results"] == nil && result["models"] == nil {
                    throw Self.error(result["message"] as? String ?? result["error"] as? String ?? "Codex 控制通道暂不可用。")
                }
                DispatchQueue.main.async { completion(.success(result)) }
            } catch { DispatchQueue.main.async { completion(.failure(error)) } }
        }
    }
    fileprivate static func error(_ message: String) -> NSError { NSError(domain: "VibeCue.CodexControl", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
}

struct CueBatchModel {
    let id: String
    let name: String
    let efforts: [[String: String]]
    let defaultEffort: String
    let fastTier: String?
    let fastDescription: String
    init?(_ raw: [String: Any]) {
        guard let id = raw["id"] as? String, !id.isEmpty else { return nil }
        self.id = id; name = raw["name"] as? String ?? id
        efforts = raw["efforts"] as? [[String: String]] ?? []
        defaultEffort = raw["default_effort"] as? String ?? ""
        let fast = raw["fast_tier"] as? String
        fastTier = fast
        let tiers = raw["service_tiers"] as? [[String: Any]] ?? []
        fastDescription = tiers.first(where: { ($0["id"] as? String) == fast })?["description"] as? String ?? ""
    }
}

final class CueCodexBatchController: NSWindowController, NSWindowDelegate {
    private let targets: [CueBatchTarget]
    private let client: CueCodexControlClient
    private let connectAction: () -> Void
    private let root = CueFlippedView(frame: NSRect(x: 0, y: 0, width: 600, height: 740))
    private let model = NSPopUpButton(), effort = NSPopUpButton(), speed = NSPopUpButton()
    private let connection = NSTextField(wrappingLabelWithString: "正在读取 Codex 可用模型…")
    private let speedHint = NSTextField(wrappingLabelWithString: "")
    private let status = NSTextField(wrappingLabelWithString: "")
    private let resultText = NSTextView()
    private let progressBar = NSProgressIndicator()
    private let progressLabel = NSTextField(labelWithString: "")
    private var lastResults: [String: [String: Any]] = [:]
    private var applyingIDs: [String] = []
    private var appliedParameters: [String: Any]?
    private var queuedUpdates: [[String: Any]] = []
    private var retryButton: NSButton!
    private var closeButton: NSButton!
    private var openButton: NSButton!
    private var verifyButton: NSButton!
    var isApplying: Bool { !applyingIDs.isEmpty }
    private var models: [CueBatchModel] = []
    private var connected = false, busy = false
    private var catalogLoading = false
    private var reconnectTimer: Timer?
    private var applyButton: NSButton!
    private var connectButton: NSButton!
    private var refreshButton: NSButton!
    private let completion: () -> Void

    init(home: URL, targets: [CueBatchTarget], connect: @escaping () -> Void, completion: @escaping () -> Void = {}, loadAutomatically: Bool = true) {
        self.targets = CueBatchTarget.unique(targets); client = CueCodexControlClient(home: home)
        connectAction = connect; self.completion = completion
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 600, height: 740), styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "Codex 批量模型设置"; window.isReleasedWhenClosed = false
        window.level = .floating; window.hidesOnDeactivate = false
        super.init(window: window)
        window.contentView = root; window.delegate = self; window.center(); build()
        if loadAutomatically { reloadCatalog() }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func showWindow(_ sender: Any?) {
        super.showWindow(sender); startReconnectTimer()
    }
    func presentSheet(on parent: NSWindow) {
        guard let window else { return }
        // A sheet inherits its parent's ordering and can pull the entire large
        // workbench above other apps. Only this transient settings window floats.
        pinAboveWindows(parent: parent)
        let origin = NSPoint(x: parent.frame.midX - window.frame.width / 2,
                             y: parent.frame.midY - window.frame.height / 2)
        window.setFrameOrigin(origin); window.makeKeyAndOrderFront(nil); startReconnectTimer()
    }
    private func pinAboveWindows(parent: NSWindow) {
        window?.level = .floating; window?.hidesOnDeactivate = false
    }
    override func close() {
        if let window, let parent = window.sheetParent { parent.endSheet(window); window.orderOut(nil) }
        reconnectTimer?.invalidate(); reconnectTimer = nil
        super.close()
    }
    private func startReconnectTimer() {
        reconnectTimer?.invalidate()
        let timer = Timer(timeInterval: 5, repeats: true) { [weak self] _ in
            guard let self, self.window?.isVisible == true, self.window?.isMiniaturized != true else { return }
            self.reloadCatalog(automatic: true)
        }
        RunLoop.main.add(timer, forMode: .common); reconnectTimer = timer
    }
    func windowWillClose(_ notification: Notification) { reconnectTimer?.invalidate(); reconnectTimer = nil }
    deinit { reconnectTimer?.invalidate(); window?.orderOut(nil) }
    private func label(_ text: String, x: CGFloat = 24, y: CGFloat, width: CGFloat = 550, size: CGFloat = 12, weight: NSFont.Weight = .regular) {
        let value = NSTextField(wrappingLabelWithString: text)
        value.frame = NSRect(x: x, y: y, width: width, height: 36)
        value.font = .systemFont(ofSize: size, weight: weight); root.addSubview(value)
    }
    private func build() {
        root.backgroundColor = .windowBackgroundColor
        label("模型设置 · \(targets.count) 个 Codex 对话", y: 20, size: 19, weight: .semibold)
        label("逐项写入并回读 Codex。设置用于后续轮次，当前轮继续执行。", y: 56)
        connection.frame = NSRect(x: 24, y: 92, width: 400, height: 48)
        connection.font = .systemFont(ofSize: 11); connection.textColor = .secondaryLabelColor; root.addSubview(connection)
        refreshButton = NSButton(title: "刷新连接", target: self, action: #selector(refreshCatalog))
        refreshButton.bezelStyle = .rounded
        refreshButton.frame = NSRect(x: 459, y: 95, width: 116, height: 29); root.addSubview(refreshButton)
        label("选中的对话", y: 143, size: 11, weight: .semibold)
        label("后台设置 · 不切换 Codex 窗口", x: 315, y: 143, width: 265, size: 11)
        let targetScroll = NSScrollView(frame: NSRect(x: 24, y: 171, width: 552, height: 100))
        targetScroll.borderType = .bezelBorder; targetScroll.hasVerticalScroller = true
        let text = NSTextView(frame: targetScroll.bounds)
        text.isEditable = false; text.isSelectable = true; text.font = .systemFont(ofSize: 12)
        text.string = targets.enumerated().map { "\($0.offset + 1). \($0.element.title)" }.joined(separator: "\n")
        text.textContainerInset = NSSize(width: 7, height: 7); text.autoresizingMask = [.width]
        text.isVerticallyResizable = true; text.textContainer?.widthTracksTextView = true
        targetScroll.documentView = text; root.addSubview(targetScroll)
        for (title, y, popup, action) in [("模型", CGFloat(289), model, #selector(modelChanged)), ("推理强度", 332, effort, #selector(parametersChanged)), ("速度档位", 375, speed, #selector(parametersChanged))] {
            label(title, y: y + 4, width: 92)
            popup.frame = NSRect(x: 124, y: y, width: 452, height: 29)
            popup.bezelStyle = .rounded
            popup.target = self; popup.action = action; popup.setAccessibilityLabel("批量" + title); root.addSubview(popup)
        }
        fill(model, rows: [], keep: "保持各对话原模型")
        fill(effort, rows: [], keep: "保持各对话原强度")
        for (name, id) in [("保持各对话原闪电设置", "keep"), ("关闭闪电（标准）", "standard"), ("开启闪电（使用模型支持的档位）", "fast")] {
            speed.addItem(withTitle: name); speed.lastItem?.representedObject = id
        }
        speedHint.frame = NSRect(x: 124, y: 411, width: 452, height: 41)
        speedHint.font = .systemFont(ofSize: 11); speedHint.textColor = .secondaryLabelColor; root.addSubview(speedHint)
        status.frame = NSRect(x: 24, y: 451, width: 552, height: 36)
        status.font = .systemFont(ofSize: 11); root.addSubview(status)
        progressBar.frame = NSRect(x: 24, y: 490, width: 552, height: 8)
        progressBar.style = .bar; progressBar.isIndeterminate = false
        progressBar.minValue = 0; progressBar.maxValue = Double(targets.count)
        progressBar.setAccessibilityIdentifier("codex-batch.progress"); progressBar.setAccessibilityLabel("模型设置进度")
        root.addSubview(progressBar)
        progressLabel.frame = NSRect(x: 24, y: 505, width: 552, height: 20)
        progressLabel.font = .monospacedDigitSystemFont(ofSize: 11, weight: .regular)
        progressLabel.textColor = .secondaryLabelColor; root.addSubview(progressLabel)
        let results = NSScrollView(frame: NSRect(x: 24, y: 532, width: 552, height: 120))
        results.hasVerticalScroller = true; results.borderType = .bezelBorder
        resultText.frame = results.bounds; resultText.isEditable = false; resultText.isSelectable = true
        resultText.font = .systemFont(ofSize: 11); resultText.textContainerInset = NSSize(width: 7, height: 6)
        resultText.autoresizingMask = [.width]; resultText.isVerticallyResizable = true; resultText.textContainer?.widthTracksTextView = true
        results.documentView = resultText; root.addSubview(results)
        connectButton = NSButton(title: "重试连接", target: self, action: #selector(connect))
        connectButton.bezelStyle = .rounded
        connectButton.frame = NSRect(x: 24, y: 698, width: 100, height: 30); root.addSubview(connectButton)
        retryButton = NSButton(title: "仅重试未生效项", target: self, action: #selector(retryFailed))
        retryButton.frame = NSRect(x: 20, y: 660, width: 157, height: 30); retryButton.bezelStyle = .rounded; root.addSubview(retryButton)
        verifyButton = NSButton(title: "重新核验（不写入）", target: self, action: #selector(verifyResults))
        verifyButton.frame = NSRect(x: 181, y: 660, width: 180, height: 30); verifyButton.bezelStyle = .rounded; root.addSubview(verifyButton)
        openButton = NSButton(title: "取消后台待处理", target: self, action: #selector(cancelQueued))
        openButton.frame = NSRect(x: 365, y: 660, width: 215, height: 30); openButton.bezelStyle = .rounded; root.addSubview(openButton)
        closeButton = NSButton(title: "关闭", target: self, action: #selector(closeDialog))
        closeButton.bezelStyle = .rounded
        closeButton.frame = NSRect(x: 351, y: 698, width: 99, height: 30); root.addSubview(closeButton)
        applyButton = NSButton(title: "应用到 \(targets.count) 个对话", target: self, action: #selector(apply))
        applyButton.bezelStyle = .rounded
        applyButton.frame = NSRect(x: 457, y: 698, width: 119, height: 30); applyButton.keyEquivalent = "\r"
        root.addSubview(applyButton); updateControls()
    }
    private func fill(_ popup: NSPopUpButton, rows: [[String: String]], keep: String) {
        let selected = popup.selectedItem?.representedObject as? String ?? ""
        popup.removeAllItems(); popup.addItem(withTitle: keep); popup.lastItem?.representedObject = ""
        for row in rows {
            guard let id = row["id"] else { continue }
            popup.addItem(withTitle: row["name"] ?? id); popup.lastItem?.representedObject = id
        }
        if let item = popup.itemArray.first(where: { $0.representedObject as? String == selected }) { popup.select(item) }
        else if !selected.isEmpty {
            popup.addItem(withTitle: selected + "（当前不可用）")
            popup.lastItem?.representedObject = selected; popup.lastItem?.isEnabled = false
            popup.menu?.autoenablesItems = false; popup.selectItem(at: popup.numberOfItems - 1)
        }
    }
    private func selected(_ popup: NSPopUpButton) -> String { popup.selectedItem?.representedObject as? String ?? "" }
    private var selectedModel: CueBatchModel? { models.first { $0.id == selected(model) } }
    private func reloadCatalog(automatic: Bool = false) {
        guard !busy, !catalogLoading else { return }; catalogLoading = true
        if !automatic { busy = true; updateControls() }
        client.run("catalog") { [weak self] result in
            guard let self else { return }; self.catalogLoading = false
            if !automatic { self.busy = false }
            switch result {
            case .success(let data): self.applyCatalog(data, preservingWork: automatic)
            case .failure(let error):
                self.connected = false; self.connection.stringValue = "自动重连中… \(error.localizedDescription)"
                self.connection.textColor = .secondaryLabelColor
            }
            self.updateControls()
            let queued = self.queuedUpdates; self.queuedUpdates = []; self.adoptQueuedResults(queued)
        }
    }
    private func applyCatalog(_ data: [String: Any], preservingWork: Bool = false) {
        let keepResults = preservingWork || !lastResults.isEmpty
        connected = data["connected"] as? Bool == true
        models = (data["models"] as? [[String: Any]] ?? []).compactMap(CueBatchModel.init)
        fill(model, rows: models.map { ["id": $0.id, "name": $0.name] }, keep: "保持各对话原模型")
        connection.stringValue = connected ? "Codex 已自动连接；逐个检查对话就绪状态后写入并核验。" : "Codex 自动重连中… 可先选参数，连接恢复后才能应用。"
        connection.textColor = connected ? .systemGreen : .secondaryLabelColor
        connection.toolTip = data["message"] as? String
        if !keepResults { status.stringValue = data["source"] as? String == "cache" ? "当前为本机模型缓存；应用前会重新校验实时能力。" : (data["message"] as? String ?? "") }
        // A background health check never writes the result text or operation
        // status. Avoid clearing and restoring NSTextView's mutable storage.
        updateEfforts(preservingWork: keepResults)
    }
    private func updateEfforts(preservingWork: Bool = false) {
        if let current = selectedModel {
            fill(effort, rows: current.efforts, keep: "保持原强度（仍需与新模型兼容）")
        } else {
            var seen = Set<String>()
            let choices = models.flatMap(\.efforts).filter { seen.insert($0["id"] ?? "").inserted }
            fill(effort, rows: choices, keep: "保持各对话原强度")
        }
        refreshParameterDetails(clearResults: !preservingWork)
    }
    @objc private func modelChanged() {
        updateEfforts()
        parametersChanged()
    }
    @objc private func parametersChanged() {
        refreshParameterDetails(clearResults: true)
    }
    private func refreshParameterDetails(clearResults: Bool) {
        if clearResults && !resultText.string.isEmpty {
            resultText.string = ""; lastResults = [:]; appliedParameters = nil
            progressBar.doubleValue = 0; progressLabel.stringValue = ""
            status.stringValue = "参数已调整，尚未应用。"; status.textColor = .secondaryLabelColor
        }
        if let current = selectedModel {
            speedHint.stringValue = current.fastTier == nil ? "该模型未声明可用闪电档位；开启请求会被拒绝。" : (current.fastDescription.isEmpty ? "使用该模型声明的 Fast 档位；不保证固定倍速。" : current.fastDescription)
            speed.itemArray.first(where: { $0.representedObject as? String == "fast" })?.title = "开启闪电 · \(current.name)"
        } else { speedHint.stringValue = "倍率和用量规则取决于各对话当前模型；逐个校验，不兼容项会单独报告。" }
        updateControls()
    }
    private func updateControls() {
        guard applyButton != nil else { return }
        model.isEnabled = !busy; effort.isEnabled = !busy; speed.isEnabled = !busy
        refreshButton.isEnabled = !busy && !catalogLoading; connectButton.isEnabled = !busy && !catalogLoading && !connected
        connectButton.isHidden = connected
        closeButton.title = isApplying ? "后台继续" : "关闭"
        retryButton.isHidden = lastResults.isEmpty
        retryButton.isEnabled = !busy && connected && lastResults.values.contains { $0["status"] as? String == "failed" }
        verifyButton.isHidden = lastResults.isEmpty
        verifyButton.isEnabled = !busy && connected && lastResults.values.contains { ["verified", "unverified"].contains($0["status"] as? String ?? "") }
        openButton.isHidden = !lastResults.values.contains { $0["status"] as? String == "queued" }
        openButton.isEnabled = !busy
        let changes = !selected(model).isEmpty || !selected(effort).isEmpty || selected(speed) != "keep"
        let missingModel = !selected(model).isEmpty && selectedModel == nil
        let invalidEffort = selectedModel.map { current in !selected(effort).isEmpty && !current.efforts.contains { $0["id"] == selected(effort) } } ?? false
        applyButton.isEnabled = !busy && connected && changes && !targets.isEmpty && targets.count <= 200 && !missingModel && !invalidEffort
        if targets.count > 200 { status.stringValue = "单次最多操作 200 个对话，请返回列表缩小选择。"; status.textColor = .systemOrange }
        else if missingModel || invalidEffort { status.stringValue = "所选模型或强度不在当前可用能力中，请重新选择。"; status.textColor = .systemOrange }
        else if status.stringValue == "所选模型或强度不在当前可用能力中，请重新选择。" {
            status.stringValue = "参数已调整，尚未应用。"; status.textColor = .secondaryLabelColor
        }
        applyButton.title = busy ? "正在处理…" : "应用到 \(targets.count) 个对话"
    }
    @objc private func refreshCatalog() { reloadCatalog() }
    func refreshConnection() { reloadCatalog() }
    @objc private func connect() { reloadCatalog() }
    @objc private func closeDialog() { close() }
    @objc private func apply() {
        guard !busy, connected else { return }
        var input: [String: Any] = ["speed": selected(speed), "defer_unloaded": true]
        if !selected(model).isEmpty { input["model"] = selected(model) }
        if !selected(effort).isEmpty { input["effort"] = selected(effort) }
        appliedParameters = input; lastResults = [:]
        startApply(ids: targets.map(\.threadID), parameters: input)
    }
    @objc private func retryFailed() {
        guard !busy, connected, let parameters = appliedParameters else { return }
        // Uncertain submissions must only be read back, never blindly resent.
        let ids = targets.filter { lastResults[$0.threadID]?["status"] as? String == "failed" }.map(\.threadID)
        guard !ids.isEmpty else { return }
        startApply(ids: ids, parameters: parameters)
    }
    private func startApply(ids: [String], parameters: [String: Any]) {
        guard !busy, !ids.isEmpty else { return }
        applyingIDs = ids; busy = true
        for id in ids { lastResults.removeValue(forKey: id) }
        progressBar.maxValue = Double(ids.count); progressBar.doubleValue = 0
        progressLabel.stringValue = "已处理 0 / \(ids.count)"
        status.stringValue = "正在准备逐项写入并核验；不会启动或打断任务。"; status.textColor = .secondaryLabelColor
        renderResults(); updateControls()
        client.run("apply", input: parameters.merging(["thread_ids": ids]) { _, new in new }, progress: { [weak self] event in
            self?.receiveProgress(event)
        }) { [weak self] result in
            guard let self else { return }; self.busy = false
            let ids = self.applyingIDs; self.applyingIDs = []
            switch result {
            case .success(let data):
                for row in data["results"] as? [[String: Any]] ?? [] { self.acceptRow(row, ids: ids) }
                self.fillUnconfirmed(ids: ids, message: data["error"] as? String ?? "批次返回不完整；没有确认的项目不会当成成功，也不会自动重发。")
                self.progressBar.doubleValue = Double(ids.count)
                self.finishSummary()
                self.completion()
            case .failure(let error):
                self.fillUnconfirmed(ids: ids, message: "连接未返回完整结果；请先重新核验，不会自动重发。")
                self.status.stringValue = error.localizedDescription; self.status.textColor = .systemOrange
                self.progressLabel.stringValue = self.summaryText
            }
            self.renderResults()
            self.updateControls()
        }
    }
    private var summaryText: String {
        let counts = ["verified", "unverified", "failed"].map { state in lastResults.values.filter { $0["status"] as? String == state }.count }
        let queued = lastResults.values.filter { $0["status"] as? String == "queued" }.count
        return "已确认 \(counts[0]) / \(targets.count) · 后台待处理 \(queued) · 待核验 \(counts[1]) · 未生效 \(counts[2])"
    }
    private func finishSummary() {
        let allVerified = targets.allSatisfy { lastResults[$0.threadID]?["status"] as? String == "verified" }
        let queued = lastResults.values.contains { $0["status"] as? String == "queued" }
        status.stringValue = allVerified ? "全部已由 Codex 原生设置回读确认，后续轮次生效。" : queued ? "连接或运行者暂不可用，已后台排队并自动重试，无需打开对话。" : "批次已结束，部分项目未生效或待核验；请查看逐项结果。"
        status.textColor = allVerified ? .systemGreen : .systemOrange
        progressLabel.stringValue = summaryText
    }
    private func acceptRow(_ row: [String: Any], ids: [String]) {
        guard let id = row["thread_id"] as? String, ids.contains(id),
              ["verified", "unverified", "failed", "queued"].contains(row["status"] as? String ?? "") else { return }
        lastResults[id] = row
    }
    private func fillUnconfirmed(ids: [String], message: String) {
        for id in ids where lastResults[id] == nil {
            lastResults[id] = ["thread_id": id, "status": "unverified", "verified": false, "message": message]
        }
    }
    private func receiveProgress(_ event: [String: Any], collect: Bool = true) {
        guard !applyingIDs.isEmpty, event["total"] as? Int == applyingIDs.count,
              let completed = event["completed"] as? Int, completed >= 0, completed <= applyingIDs.count else { return }
        if let id = event["thread_id"] as? String, !applyingIDs.contains(id) { return }
        progressBar.doubleValue = max(progressBar.doubleValue, Double(completed))
        progressLabel.stringValue = "已处理 \(completed) / \(applyingIDs.count) · \(summaryText)"
        let phase = event["phase"] as? String ?? "starting"
        let name = ["starting": "准备中", "reading": "读取原生设置", "loading": "后台载入配置", "writing": "正在写入", "verifying": "回读核验", "completed": "已处理"] [phase] ?? "处理中"
        let title = targets.first { $0.threadID == event["thread_id"] as? String }?.title ?? ""
        status.stringValue = "\(name) · \(title)"; status.textColor = .secondaryLabelColor
        if collect, let row = event["result"] as? [String: Any] {
            acceptRow(row, ids: applyingIDs); renderResults()
        }
    }
    private func describe(_ settings: [String: Any]) -> String {
        var parts: [String] = []
        for (key, label) in [("model", "模型"), ("effort", "强度"), ("service_tier", "速度")] {
            if let value = settings[key] as? String {
                parts.append("\(label) \(key == "service_tier" && value == "default" ? "标准" : value)")
            }
        }
        return parts.joined(separator: " · ")
    }
    private func renderResults() {
        resultText.string = targets.compactMap { target -> String? in
            guard let row = lastResults[target.threadID] else { return nil }
            let state = row["status"] as? String ?? "unverified"
            let name = state == "verified" ? "✓ 已确认" : state == "queued" ? "◷ 后台待处理" : state == "failed" ? "× 未生效" : "! 待核验"
            var lines = ["\(name) · \(target.title)", row["message"] as? String ?? ""]
            if let expected = row["expected_settings"] as? [String: Any], !expected.isEmpty { lines.append("目标：" + describe(expected)) }
            if let current = row["settings"] as? [String: Any], state != "failed", row["expected_settings"] != nil { lines.append("回读：" + describe(current)) }
            return lines.joined(separator: "\n")
        }.joined(separator: "\n\n")
    }
    func adoptQueuedResults(_ rows: [[String: Any]]) {
        guard !busy else { queuedUpdates = rows; return }
        var changed = false
        for row in rows {
            guard let id = row["thread_id"] as? String, targets.contains(where: { $0.threadID == id }),
                  let queueID = row["queue_id"] as? String else { continue }
            let previous = lastResults[id]
            guard previous?["queue_id"] as? String == queueID || previous == nil && ["queued", "unverified"].contains(row["status"] as? String ?? "") else { continue }
            guard previous == nil || !NSDictionary(dictionary: previous!).isEqual(to: row) else { continue }
            acceptRow(row, ids: targets.map(\.threadID)); changed = true
        }
        if changed { renderResults(); finishSummary(); updateControls() }
    }
    @objc private func cancelQueued() {
        guard !busy else { return }
        let items = lastResults.values.filter { $0["status"] as? String == "queued" }.compactMap { row -> [String: String]? in
            guard let id = row["thread_id"] as? String, let queueID = row["queue_id"] as? String else { return nil }
            return ["thread_id": id, "queue_id": queueID]
        }
        guard !items.isEmpty else { return }
        busy = true; updateControls()
        client.run("queue-cancel", input: ["items": items]) { [weak self] result in
            guard let self else { return }; self.busy = false
            if case .success(let data) = result { self.adoptQueuedResults(data["results"] as? [[String: Any]] ?? []) }
            else if case .failure(let error) = result { self.status.stringValue = error.localizedDescription }
            self.updateControls()
        }
    }
    static func readbackMatches(_ row: [String: Any], expected: [String: String]) -> Bool {
        guard row["status"] as? String == "read", !expected.isEmpty,
              let actual = row["settings"] as? [String: Any], let saved = row["saved_settings"] as? [String: Any] else { return false }
        let persisted = row["verification_basis"] as? String == "persisted_settings" && row["loaded"] as? Bool == false
        if persisted && row["verified"] as? Bool != true { return false }
        guard let independent = row[persisted ? "indexed_settings" : "composer_settings"] as? [String: Any] else { return false }
        return expected.allSatisfy { key, value in
            func equal(_ candidate: Any?) -> Bool {
                guard let candidate = candidate as? String else { return false }
                return candidate == value || key == "service_tier" && ["priority", "fast"].contains(value) && ["priority", "fast"].contains(candidate)
            }
            return equal(actual[key]) && equal(saved[key]) && (key == "service_tier" || equal(independent[key]))
        }
    }
    @objc private func verifyResults() {
        guard !busy else { return }
        let ids = targets.filter { ["verified", "unverified"].contains(lastResults[$0.threadID]?["status"] as? String ?? "") }.map(\.threadID)
        guard !ids.isEmpty else { return }
        busy = true; applyingIDs = ids; progressBar.maxValue = Double(ids.count); progressBar.doubleValue = 0
        status.stringValue = "正在重新读取 Codex 原生设置；不修改模型配置，不启动任务。"; updateControls()
        client.run("inspect", input: ["thread_ids": ids], progress: { [weak self] event in self?.receiveProgress(event, collect: false) }) { [weak self] result in
            guard let self else { return }; self.busy = false; self.applyingIDs = []
            switch result {
            case .success(let data):
                let rows = data["results"] as? [[String: Any]] ?? []
                for id in ids {
                    let row = rows.first { $0["thread_id"] as? String == id } ?? [:]
                    let parameters = self.lastResults[id]?["requested_parameters"] as? [String: Any] ?? self.appliedParameters ?? [:]
                    var expected: [String: String] = [:]
                    for key in ["model", "effort"] { expected[key] = parameters[key] as? String }
                    if parameters["speed"] as? String == "standard" { expected["service_tier"] = "default" }
                    else if parameters["speed"] as? String == "fast" {
                        let model = parameters["model"] as? String ?? (row["settings"] as? [String: Any])?["model"] as? String
                        expected["service_tier"] = self.models.first { $0.id == model }?.fastTier ?? "priority"
                    }
                    let verified = Self.readbackMatches(row, expected: expected)
                    var updated: [String: Any] = ["thread_id": id, "status": verified ? "verified" : "unverified", "verified": verified,
                        "settings": row["settings"] ?? [:], "expected_settings": expected,
                        "message": verified ? "独立重新回读已匹配，未再次写入设置。" : "原生回读未匹配或暂不可读；未再次写入，请在 Codex 核对。"]
                    updated["queue_id"] = self.lastResults[id]?["queue_id"]; updated["requested_parameters"] = parameters
                    self.lastResults[id] = updated
                }
                self.progressBar.doubleValue = Double(ids.count); self.finishSummary()
            case .failure(let error): self.status.stringValue = error.localizedDescription; self.status.textColor = .systemOrange
            }
            self.renderResults(); self.updateControls(); self.completion()
        }
    }
    static func validationReport() -> [String: Bool] {
        let one = CueBatchTarget(threadID: "thread-one", title: "First")!
        let second = CueBatchTarget(threadID: "thread-one", title: "Same thread")!
        let two = CueBatchTarget(threadID: "thread-two", title: "Second")!
        let model = CueBatchModel(["id": "fixture-model", "name": "Fixture", "default_effort":"medium", "efforts": [["id":"high","name":"高"],["id":"medium","name":"中"]], "fast_tier":"priority", "service_tiers":[["id":"priority","description":"1.5x speed"]]])!
        var results = ["batch_targets_deduplicate_by_thread_not_alert": CueBatchTarget.unique([one, second, two]).count == 2,
                "batch_targets_reject_invalid_or_other_provider": CueBatchTarget(threadID: "bad/path", title: "bad") == nil && CueBatchTarget(threadID: "valid", title: "Other", provider: "other") == nil,
                "batch_speed_uses_model_capability_description": model.fastTier == "priority" && model.fastDescription == "1.5x speed",
                "batch_missing_fast_capability_is_not_invented": CueBatchModel(["id":"no-fast"])?.fastTier == nil]
        _ = NSApplication.shared
        let controller = CueCodexBatchController(home: FileManager.default.temporaryDirectory, targets: [one], connect: {}, loadAutomatically: false)
        controller.models = [model]
        controller.fill(controller.model, rows: [["id":model.id,"name":model.name]], keep: "保持")
        controller.model.selectItem(at: 1)
        controller.modelChanged()
        results["batch_model_change_preserves_keep_effort_choice"] = controller.selected(controller.effort).isEmpty
        controller.effort.selectItem(at: 1); controller.modelChanged()
        results["batch_model_change_preserves_explicit_effort_choice"] = controller.selected(controller.effort) == "high"
        controller.fill(controller.model, rows: [], keep: "保持")
        controller.updateControls()
        results["batch_missing_catalog_model_is_not_silently_replaced"] = controller.selected(controller.model) == model.id && !controller.applyButton.isEnabled
        let liveCatalog: [String: Any] = ["connected": true, "models": [["id": "fixture-model", "name": "Fixture", "default_effort": "medium", "efforts": [["id":"high","name":"高"],["id":"medium","name":"中"]], "fast_tier": "priority", "service_tiers": [["id":"priority","description":"Fast"]]]], "message": "已复用现有 Codex 对话连接"]
        controller.applyCatalog(liveCatalog)
        controller.updateControls()
        controller.effort.selectItem(at: 1); controller.speed.selectItem(at: 2)
        controller.resultText.string = "上一批次已经确认的结果"
        controller.status.stringValue = "已确认 1 项；未重新应用"
        controller.applyCatalog(liveCatalog, preservingWork: true); controller.updateControls()
        results["batch_automatic_catalog_refresh_preserves_parameters_and_results"] = controller.selected(controller.model) == model.id && controller.selected(controller.effort) == "high" && controller.selected(controller.speed) == "fast" && controller.resultText.string == "上一批次已经确认的结果" && controller.status.stringValue == "已确认 1 项；未重新应用"
        results["batch_live_connection_hides_manual_connect_prompt"] = controller.connectButton.isHidden && controller.connection.stringValue.hasPrefix("Codex 已自动连接")
        controller.applyCatalog(["connected": false, "models": liveCatalog["models"]!, "message": "等待 Codex 恢复"], preservingWork: true); controller.updateControls()
        results["batch_disconnect_keeps_choices_but_disables_apply_during_auto_recovery"] = controller.connection.stringValue.hasPrefix("Codex 自动重连中") && !controller.connectButton.isHidden && controller.connectButton.title == "重试连接" && !controller.applyButton.isEnabled && controller.resultText.string == "上一批次已经确认的结果"
        controller.startReconnectTimer()
        results["batch_visible_connection_retries_use_five_second_timer"] = controller.reconnectTimer?.isValid == true && controller.reconnectTimer?.timeInterval == 5
        controller.windowWillClose(Notification(name: NSWindow.willCloseNotification))
        results["batch_closing_window_stops_automatic_recovery_poll"] = controller.reconnectTimer == nil
        let progressController = CueCodexBatchController(home: FileManager.default.temporaryDirectory, targets: [one, two], connect: {}, loadAutomatically: false)
        progressController.connected = true; progressController.busy = true; progressController.applyingIDs = [one.threadID, two.threadID]
        let parent = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 700, height: 800), styleMask: [.titled], backing: .buffered, defer: true)
        parent.level = .normal; progressController.pinAboveWindows(parent: parent)
        results["batch_progress_remains_above_normal_windows_without_hiding_on_deactivation"] = progressController.window?.level == .floating && progressController.window?.hidesOnDeactivate == false
        results["batch_float_never_elevates_the_main_workbench"] = parent.level == .normal && progressController.window?.sheetParent == nil
        progressController.receiveProgress(["event": "progress", "total": 2, "completed": 1, "phase": "completed", "thread_id": one.threadID,
            "result": ["thread_id": one.threadID, "status": "verified", "message": "原生回读一致"]])
        results["batch_progress_and_result_visible_before_process_exit"] = progressController.progressBar.doubleValue == 1 && progressController.resultText.string.contains("原生回读一致") && progressController.isApplying
        progressController.receiveProgress(["total": 2, "completed": 0, "phase": "reading", "thread_id": two.threadID])
        results["batch_progress_never_moves_backwards"] = progressController.progressBar.doubleValue == 1
        progressController.receiveProgress(["total": 2, "completed": 2, "phase": "completed", "thread_id": "unselected", "result": ["thread_id": two.threadID, "status": "verified"]])
        results["batch_progress_rejects_wrong_target_without_changing_results"] = progressController.lastResults.count == 1 && progressController.progressBar.doubleValue == 1
        progressController.lastResults[two.threadID] = ["thread_id": two.threadID, "status": "failed", "error_kind": "not_loaded", "message": "对话未载入"]
        progressController.busy = false; progressController.applyingIDs = []; progressController.finishSummary(); progressController.updateControls()
        results["batch_partial_failure_is_not_presented_as_success"] = progressController.status.textColor == .systemOrange && progressController.status.stringValue.contains("部分") && progressController.progressLabel.stringValue.contains("已确认 1 / 2")
        results["batch_unready_items_never_offer_automatic_window_navigation"] = progressController.retryButton.isEnabled && progressController.openButton.isHidden && progressController.root.subviews.compactMap { ($0 as? NSButton)?.title }.allSatisfy { !$0.contains("打开未就绪") && !$0.contains("自动打开") }
        progressController.lastResults[two.threadID] = ["thread_id": two.threadID, "status": "unverified"]
        progressController.updateControls()
        results["batch_uncertain_writes_can_be_verified_but_not_retried"] = !progressController.retryButton.isEnabled && progressController.verifyButton.isEnabled
        let completedText = progressController.resultText.string, completedStatus = progressController.status.stringValue
        progressController.applyCatalog(liveCatalog)
        results["batch_manual_connection_refresh_keeps_completed_results_and_retry_state"] = progressController.lastResults.count == 2 && progressController.resultText.string == completedText && progressController.status.stringValue == completedStatus
        progressController.lastResults[two.threadID] = ["thread_id": two.threadID, "queue_id": "queue-one", "status": "queued", "message": "后台等待"]
        progressController.finishSummary(); progressController.updateControls()
        results["batch_queued_settings_are_distinct_from_applied_and_can_be_cancelled"] = progressController.progressLabel.stringValue.contains("后台待处理 1") && progressController.status.stringValue.contains("后台排队") && !progressController.openButton.isHidden
        progressController.adoptQueuedResults([["thread_id": two.threadID, "queue_id": "another-queue", "status": "verified"]])
        results["batch_old_queue_results_cannot_overwrite_a_newer_intent"] = progressController.lastResults[two.threadID]?["status"] as? String == "queued"
        progressController.adoptQueuedResults([["thread_id": two.threadID, "queue_id": "queue-one", "status": "verified", "message": "后台完成"]])
        results["batch_completed_background_queue_updates_existing_progress_window"] = progressController.lastResults[two.threadID]?["status"] as? String == "verified" && progressController.resultText.string.contains("后台完成")
        results["batch_settings_use_specific_feature_and_parameter_names"] = progressController.window?.title == "Codex 批量模型设置" && progressController.root.subviews.compactMap { ($0 as? NSTextField)?.stringValue }.contains("推理强度") && progressController.root.subviews.compactMap { ($0 as? NSTextField)?.stringValue }.contains("速度档位")
        let matching: [String: Any] = ["status": "read", "settings": ["model": "fixture", "effort": "max", "service_tier": "default"],
            "saved_settings": ["model": "fixture", "effort": "max", "service_tier": "default"], "composer_settings": ["model": "fixture", "effort": "max"]]
        let expected = ["model": "fixture", "effort": "max", "service_tier": "default"]
        results["batch_readonly_verification_requires_three_matching_sources"] = readbackMatches(matching, expected: expected)
        var stale = matching; stale["settings"] = ["model": "fixture", "effort": "ultra", "service_tier": "default"]
        var staleComposer = matching; staleComposer["composer_settings"] = ["model": "fixture", "effort": "ultra"]
        results["batch_saved_echo_cannot_hide_stale_native_settings_or_composer"] = !readbackMatches(stale, expected: expected) && !readbackMatches(staleComposer, expected: expected)
        var missing = matching; missing.removeValue(forKey: "composer_settings")
        results["batch_missing_native_evidence_never_becomes_verified"] = !readbackMatches(missing, expected: expected)
        var persisted = matching; persisted.removeValue(forKey: "composer_settings")
        persisted["loaded"] = false; persisted["verified"] = true; persisted["verification_basis"] = "persisted_settings"
        persisted["indexed_settings"] = ["model": "fixture", "effort": "max"]
        results["batch_unloaded_verification_uses_independent_persisted_index"] = readbackMatches(persisted, expected: expected)
        persisted["indexed_settings"] = ["model": "fixture", "effort": "ultra"]
        results["batch_unloaded_index_conflict_is_never_marked_verified"] = !readbackMatches(persisted, expected: expected)
        do {
            var decoder = CueControlStreamDecoder()
            let events: [[String: Any]] = [["event": "progress", "phase": "reading", "thread_id": "中文", "total": 2, "completed": 0],
                ["event": "progress", "phase": "completed", "total": 2, "completed": 1],
                ["event": "result", "result": ["results": [["status": "verified"]]]]]
            let data = try events.reduce(into: Data()) { data, event in data.append(try JSONSerialization.data(withJSONObject: event)); data.append(10) }
            var observed: [[String: Any]] = []
            for byte in data { observed.append(contentsOf: try decoder.append(Data([byte]))) }
            results["batch_stream_decodes_fragmented_utf8_and_frames_incrementally"] = observed.count == 2 && observed.first?["thread_id"] as? String == "中文" && decoder.isComplete
            var invalid = CueControlStreamDecoder()
            results["batch_stream_rejects_malformed_frames"] = (try? invalid.append(Data("not-json\n".utf8))) == nil
            results["batch_stream_rejects_data_after_final_result"] = (try? decoder.append(Data("{}\n".utf8))) == nil
            var truncated = CueControlStreamDecoder(); _ = try truncated.append(Data("{\"event\":\"progress\"}\n".utf8))
            results["batch_truncated_stream_cannot_be_mistaken_for_completed_batch"] = !truncated.isComplete
        } catch { results["batch_stream_decodes_fragmented_utf8_and_frames_incrementally"] = false }
        return results
    }
    static func captureFixture(to output: URL) -> Bool {
        _ = NSApplication.shared
        let target = CueBatchTarget(threadID: "fixture-pinned", title: "VibeCoding Go｜本轮详情与批量设置")!
        let other = CueBatchTarget(threadID: "fixture-running", title: "ALV｜构建与发布验收")!
        let controller = CueCodexBatchController(home: FileManager.default.temporaryDirectory, targets: [target, target, other], connect: {}, loadAutomatically: false)
        let fixture = CueBatchModel(["id":"fixture-model", "name":"模型示例", "default_effort":"high", "efforts":[["id":"high","name":"高"],["id":"medium","name":"中"]], "fast_tier":"priority", "service_tiers":[["id":"priority","description":"1.5x speed · 以当前模型声明为准"]]])!
        controller.models = [fixture]
        controller.fill(controller.model, rows: [["id":fixture.id,"name":fixture.name]], keep: "保持各对话原模型")
        controller.model.selectItem(at: 1); controller.modelChanged(); controller.effort.selectItem(at: 1); controller.speed.selectItem(at: 2); controller.parametersChanged()
        controller.connected = true; controller.busy = true
        controller.connection.stringValue = "Codex 已自动连接；逐个检查对话就绪状态后写入并核验。"
        controller.connection.textColor = .systemGreen
        controller.applyingIDs = [target.threadID, other.threadID]
        controller.receiveProgress(["total": 2, "completed": 1, "phase": "completed", "thread_id": target.threadID,
            "result": ["thread_id": target.threadID, "status": "verified", "expected_settings": ["model": "fixture-model", "effort": "high", "service_tier": "priority"], "settings": ["model": "fixture-model", "effort": "high", "service_tier": "priority"], "message": "Codex 原生设置与输入框模型均已回读确认。"]])
        controller.receiveProgress(["total": 2, "completed": 1, "phase": "verifying", "thread_id": other.threadID])
        controller.updateControls()
        controller.window?.appearance = NSAppearance(named: .darkAqua)
        controller.root.appearance = controller.window?.appearance
        controller.window?.displayIfNeeded()
        guard let rep = controller.root.bitmapImageRepForCachingDisplay(in: controller.root.bounds) else { return false }
        controller.root.effectiveAppearance.performAsCurrentDrawingAppearance { controller.root.cacheDisplay(in: controller.root.bounds, to: rep) }
        guard let png = rep.representation(using: .png, properties: [:]) else { return false }
        do { try png.write(to: output); return true } catch { return false }
    }
}
