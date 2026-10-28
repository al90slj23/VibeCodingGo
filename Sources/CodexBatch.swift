import AppKit
import Foundation

struct CueBatchTarget {
    let threadID: String
    let title: String
    init?(threadID: String, title: String, provider: String = "codex") {
        guard provider == "codex", !threadID.isEmpty, threadID.count <= 160,
              threadID.unicodeScalars.allSatisfy({ CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_")).contains($0) }) else { return nil }
        self.threadID = threadID; self.title = String(title.prefix(180))
    }
    static func unique(_ values: [CueBatchTarget]) -> [CueBatchTarget] {
        var seen = Set<String>()
        return values.filter { seen.insert($0.threadID).inserted }
    }
}

/// Uses only VibeCue's private control module, never sends a chat message.
final class CueCodexControlClient {
    let home: URL
    init(home: URL) { self.home = home }
    func run(_ command: String, input: [String: Any]? = nil, completion: @escaping (Result<[String: Any], Error>) -> Void) {
        let home = home
        DispatchQueue.global(qos: .utility).async {
            do {
                let runtime = home.appendingPathComponent("runtime")
                guard FileManager.default.fileExists(atPath: runtime.appendingPathComponent("vibecue/codex_control.py").path) else {
                    throw Self.error("Codex 控制模块尚未安装，请安装最新版 VibeCue。")
                }
                let process = Process(), output = Pipe(), stdin = Pipe()
                process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
                process.arguments = ["-m", "vibecue.codex_control", command]
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
                DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 120, execute: timeout)
                let data = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit(); timeout.cancel()
                guard data.count <= 2_097_152,
                      let result = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    throw Self.error("控制通道未返回有效结果；没有确认的项目不会标为成功。")
                }
                if process.terminationStatus != 0 && result["results"] == nil && result["models"] == nil {
                    throw Self.error(result["message"] as? String ?? result["error"] as? String ?? "Codex 控制通道暂不可用。")
                }
                DispatchQueue.main.async { completion(.success(result)) }
            } catch { DispatchQueue.main.async { completion(.failure(error)) } }
        }
    }
    private static func error(_ message: String) -> NSError { NSError(domain: "VibeCue.CodexControl", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
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

final class CueCodexBatchController: NSWindowController {
    private let targets: [CueBatchTarget]
    private let client: CueCodexControlClient
    private let connectAction: () -> Void
    private let root = CueFlippedView(frame: NSRect(x: 0, y: 0, width: 600, height: 650))
    private let model = NSPopUpButton(), effort = NSPopUpButton(), speed = NSPopUpButton()
    private let connection = NSTextField(wrappingLabelWithString: "正在读取 Codex 可用模型…")
    private let speedHint = NSTextField(wrappingLabelWithString: "")
    private let status = NSTextField(wrappingLabelWithString: "")
    private let resultText = NSTextView()
    private var models: [CueBatchModel] = []
    private var connected = false, busy = false
    private var applyButton: NSButton!
    private var connectButton: NSButton!
    private var refreshButton: NSButton!
    private let completion: () -> Void

    init(home: URL, targets: [CueBatchTarget], connect: @escaping () -> Void, completion: @escaping () -> Void = {}, loadAutomatically: Bool = true) {
        self.targets = CueBatchTarget.unique(targets); client = CueCodexControlClient(home: home)
        connectAction = connect; self.completion = completion
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 600, height: 650), styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "Codex 批量设置"; window.isReleasedWhenClosed = false
        super.init(window: window)
        window.contentView = root; window.center(); build()
        if loadAutomatically { reloadCatalog() }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    private func label(_ text: String, x: CGFloat = 24, y: CGFloat, width: CGFloat = 550, size: CGFloat = 12, weight: NSFont.Weight = .regular) {
        let value = NSTextField(wrappingLabelWithString: text)
        value.frame = NSRect(x: x, y: y, width: width, height: 36)
        value.font = .systemFont(ofSize: size, weight: weight); root.addSubview(value)
    }
    private func build() {
        root.backgroundColor = .windowBackgroundColor
        label("批量调整 \(targets.count) 个 Codex 对话", y: 20, size: 19, weight: .semibold)
        label("按对话去重。新设置用于后续轮次，当前轮继续执行。", y: 56)
        connection.frame = NSRect(x: 24, y: 92, width: 400, height: 48)
        connection.font = .systemFont(ofSize: 11); connection.textColor = .secondaryLabelColor; root.addSubview(connection)
        refreshButton = NSButton(title: "刷新连接", target: self, action: #selector(refreshCatalog))
        refreshButton.bezelStyle = .rounded
        refreshButton.frame = NSRect(x: 459, y: 95, width: 116, height: 29); root.addSubview(refreshButton)
        label("选中的对话", y: 143, size: 11, weight: .semibold)
        let targetScroll = NSScrollView(frame: NSRect(x: 24, y: 171, width: 552, height: 100))
        targetScroll.borderType = .bezelBorder; targetScroll.hasVerticalScroller = true
        let text = NSTextView(frame: targetScroll.bounds)
        text.isEditable = false; text.isSelectable = true; text.font = .systemFont(ofSize: 12)
        text.string = targets.enumerated().map { "\($0.offset + 1). \($0.element.title)" }.joined(separator: "\n")
        text.textContainerInset = NSSize(width: 7, height: 7); text.autoresizingMask = [.width]
        text.isVerticallyResizable = true; text.textContainer?.widthTracksTextView = true
        targetScroll.documentView = text; root.addSubview(targetScroll)
        for (title, y, popup, action) in [("模型", CGFloat(289), model, #selector(modelChanged)), ("强度", 332, effort, #selector(parametersChanged)), ("闪电模式", 375, speed, #selector(parametersChanged))] {
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
        status.frame = NSRect(x: 24, y: 457, width: 552, height: 42)
        status.font = .systemFont(ofSize: 11); root.addSubview(status)
        let results = NSScrollView(frame: NSRect(x: 24, y: 501, width: 552, height: 83))
        results.hasVerticalScroller = true; results.borderType = .bezelBorder
        resultText.frame = results.bounds; resultText.isEditable = false; resultText.isSelectable = true
        resultText.font = .systemFont(ofSize: 11); resultText.textContainerInset = NSSize(width: 7, height: 6)
        resultText.autoresizingMask = [.width]; resultText.isVerticallyResizable = true; resultText.textContainer?.widthTracksTextView = true
        results.documentView = resultText; root.addSubview(results)
        connectButton = NSButton(title: "接通 Codex…", target: self, action: #selector(connect))
        connectButton.bezelStyle = .rounded
        connectButton.frame = NSRect(x: 24, y: 603, width: 145, height: 30); root.addSubview(connectButton)
        let close = NSButton(title: "关闭", target: self, action: #selector(closeDialog))
        close.bezelStyle = .rounded
        close.frame = NSRect(x: 351, y: 603, width: 99, height: 30); root.addSubview(close)
        applyButton = NSButton(title: "应用到 \(targets.count) 个对话", target: self, action: #selector(apply))
        applyButton.bezelStyle = .rounded
        applyButton.frame = NSRect(x: 457, y: 603, width: 119, height: 30); applyButton.keyEquivalent = "\r"
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
    private func reloadCatalog() {
        guard !busy else { return }; busy = true; updateControls()
        client.run("catalog") { [weak self] result in
            guard let self else { return }; self.busy = false
            switch result {
            case .success(let data):
                self.connected = data["connected"] as? Bool == true
                self.models = (data["models"] as? [[String: Any]] ?? []).compactMap(CueBatchModel.init)
                self.fill(self.model, rows: self.models.map { ["id": $0.id, "name": $0.name] }, keep: "保持各对话原模型")
                self.connection.stringValue = self.connected ? "已接通当前 Codex，可修改并确认所选对话的设置。" : "尚未接通当前 Codex。可先选参数；接通后才能应用。\n接通会等待你主动退出 Codex，再通过 VibeCue 打开。"
                self.connection.textColor = self.connected ? .systemGreen : .secondaryLabelColor
                self.status.stringValue = data["source"] as? String == "cache" ? "当前为本机模型缓存；应用前会重新校验实时能力。" : (data["message"] as? String ?? "")
                self.updateEfforts()
            case .failure(let error):
                self.connected = false; self.connection.stringValue = error.localizedDescription
            }
            self.updateControls()
        }
    }
    private func updateEfforts() {
        if let current = selectedModel {
            fill(effort, rows: current.efforts, keep: "保持原强度（仍需与新模型兼容）")
        } else {
            var seen = Set<String>()
            let choices = models.flatMap(\.efforts).filter { seen.insert($0["id"] ?? "").inserted }
            fill(effort, rows: choices, keep: "保持各对话原强度")
        }
        parametersChanged()
    }
    @objc private func modelChanged() {
        updateEfforts()
        parametersChanged()
    }
    @objc private func parametersChanged() {
        if !resultText.string.isEmpty { resultText.string = ""; status.stringValue = "参数已调整，尚未应用。"; status.textColor = .secondaryLabelColor }
        if let current = selectedModel {
            speedHint.stringValue = current.fastTier == nil ? "该模型未声明可用闪电档位；开启请求会被拒绝。" : (current.fastDescription.isEmpty ? "使用该模型声明的 Fast 档位；不保证固定倍速。" : current.fastDescription)
            speed.itemArray.first(where: { $0.representedObject as? String == "fast" })?.title = "开启闪电 · \(current.name)"
        } else { speedHint.stringValue = "倍率和用量规则取决于各对话当前模型；逐个校验，不兼容项会单独报告。" }
        updateControls()
    }
    private func updateControls() {
        guard applyButton != nil else { return }
        model.isEnabled = !busy; effort.isEnabled = !busy; speed.isEnabled = !busy
        refreshButton.isEnabled = !busy; connectButton.isEnabled = !busy && !connected
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
    @objc private func connect() { connectAction() }
    @objc private func closeDialog() { close() }
    @objc private func apply() {
        guard !busy, connected else { return }
        var input: [String: Any] = ["thread_ids": targets.map(\.threadID), "speed": selected(speed)]
        if !selected(model).isEmpty { input["model"] = selected(model) }
        if !selected(effort).isEmpty { input["effort"] = selected(effort) }
        busy = true; status.stringValue = "正在逐个应用并核对设置；当前任务会继续执行。"; updateControls()
        client.run("apply", input: input) { [weak self] result in
            guard let self else { return }; self.busy = false
            switch result {
            case .success(let data):
                let rows = data["results"] as? [[String: Any]] ?? []
                let summary = data["summary"] as? [String: Any] ?? [:]
                self.status.stringValue = "已确认 \(summary["verified"] as? Int ?? 0) · 待确认 \(summary["unverified"] as? Int ?? 0) · 失败 \(summary["failed"] as? Int ?? 0)"
                self.resultText.string = rows.map { row in
                    let id = row["thread_id"] as? String ?? ""
                    let title = self.targets.first { $0.threadID == id }?.title ?? id
                    let state = row["status"] as? String ?? "unverified"
                    let name = state == "verified" ? "已确认" : state == "failed" ? "失败" : "待确认"
                    return "\(name) · \(title)\n\(row["message"] as? String ?? "")"
                }.joined(separator: "\n\n")
                self.completion()
            case .failure(let error): self.status.stringValue = error.localizedDescription
            }
            self.updateControls()
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
        return results
    }
    static func captureFixture(to output: URL) -> Bool {
        _ = NSApplication.shared
        let target = CueBatchTarget(threadID: "fixture-pinned", title: "VibeCue｜本轮详情与批量设置")!
        let other = CueBatchTarget(threadID: "fixture-running", title: "ALV｜构建与发布验收")!
        let controller = CueCodexBatchController(home: FileManager.default.temporaryDirectory, targets: [target, target, other], connect: {}, loadAutomatically: false)
        let fixture = CueBatchModel(["id":"fixture-model", "name":"模型示例", "default_effort":"high", "efforts":[["id":"high","name":"高"],["id":"medium","name":"中"]], "fast_tier":"priority", "service_tiers":[["id":"priority","description":"1.5x speed · 以当前模型声明为准"]]])!
        controller.models = [fixture]
        controller.fill(controller.model, rows: [["id":fixture.id,"name":fixture.name]], keep: "保持各对话原模型")
        controller.model.selectItem(at: 1); controller.modelChanged(); controller.effort.selectItem(at: 1); controller.speed.selectItem(at: 2); controller.parametersChanged()
        controller.connection.stringValue = "尚未接通当前 Codex。可先选参数；接通后才能应用。\n接通会等待你主动退出 Codex，再通过 VibeCue 打开。"
        controller.status.stringValue = "展示样例 · 已按对话去重，参数不会写入真实对话。"
        controller.window?.appearance = NSAppearance(named: .darkAqua)
        controller.root.appearance = controller.window?.appearance
        controller.window?.displayIfNeeded()
        guard let rep = controller.root.bitmapImageRepForCachingDisplay(in: controller.root.bounds) else { return false }
        controller.root.effectiveAppearance.performAsCurrentDrawingAppearance { controller.root.cacheDisplay(in: controller.root.bounds, to: rep) }
        guard let png = rep.representation(using: .png, properties: [:]) else { return false }
        do { try png.write(to: output); return true } catch { return false }
    }
}
