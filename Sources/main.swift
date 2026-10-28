import AppKit
import AVFoundation
import Foundation
import Darwin
import UniformTypeIdentifiers

struct CueEvent {
    let id: String
    let kind: String
    let title: String
    let threadID: String
    let message: String
    let date: Date
    let provider: String
    let source: String
    let requiresUserAction: Bool

    init?(_ data: Data) {
        guard let raw = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let id = raw["id"] as? String, !id.isEmpty,
              let kind = raw["kind"] as? String,
              ["complete", "input", "permission", "error"].contains(kind) else { return nil }
        let source = raw["source"] as? String ?? ""
        let requiresUserAction = raw["requires_user_action"] as? Bool == true
        // A permission request can be approved automatically. Never turn that
        // raw lifecycle event into a human-action alert, including old queued
        // events produced by earlier versions of the helper.
        if kind == "permission" {
            guard source != "PermissionRequest hook",
                  requiresUserAction || source == "manual test" else { return nil }
        }
        self.id = String(id.prefix(200))
        self.kind = kind
        self.provider = String((raw["provider"] as? String ?? "codex").prefix(40))
        self.title = String((raw["title"] as? String ?? "Codex 对话").prefix(180))
        self.threadID = String((raw["thread_id"] as? String ?? "").prefix(160))
        self.message = String((raw["message"] as? String ?? "").prefix(220))
        self.source = source
        self.requiresUserAction = requiresUserAction
        if let timestamp = raw["created_at"] as? Double {
            self.date = Date(timeIntervalSince1970: timestamp)
        } else if let text = raw["created_at"] as? String,
                  let date = CueEvent.parseDate(text) {
            self.date = date
        } else { return nil }
    }

    init(kind: String, title: String, message: String) {
        id = UUID().uuidString; self.kind = kind; self.title = title
        self.message = message; threadID = ""; date = Date(); provider = "codex"
        source = "native demo"; requiresUserAction = kind == "permission"
    }

    // Navigation-only state. External alert JSON never accepts this kind.
    init(runningThreadID: String, title: String, date: Date) {
        id = "running:" + runningThreadID; kind = "running"; self.title = title
        threadID = runningThreadID; message = "正在运行"; self.date = date
        provider = "codex"; source = "running task navigation"; requiresUserAction = false
    }

    private static func parseDate(_ text: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: text) ?? ISO8601DateFormatter().date(from: text)
    }

    var color: NSColor {
        switch kind {
        case "complete": return NSColor(srgbRed: 0.23, green: 0.91, blue: 0.67, alpha: 1)
        case "input": return NSColor(srgbRed: 1, green: 0.77, blue: 0.30, alpha: 1)
        case "permission": return NSColor(srgbRed: 1, green: 0.48, blue: 0.38, alpha: 1)
        default: return NSColor(srgbRed: 1, green: 0.32, blue: 0.40, alpha: 1)
        }
    }
    var providerLabel: String { provider == "codex" ? "Codex" : provider.capitalized }
    var isPreview: Bool { source == "native demo" || source == "manual test" }
    var label: String {
        switch kind {
        case "complete": return "本轮已完成"
        case "input": return "需要你回答或判断"
        case "permission": return source == "native demo" || source == "manual test" ? "人工批准提醒测试" : "正在等待你人工批准"
        default: return "运行遇到问题"
        }
    }
    var symbol: String {
        switch kind {
        case "complete": return "checkmark.circle.fill"
        case "input": return "questionmark.bubble.fill"
        case "permission": return "hand.raised.fill"
        default: return "exclamationmark.triangle.fill"
        }
    }
    var sound: String {
        switch kind {
        case "complete": return "上行三音"
        case "input": return "问答三音"
        case "permission": return "双短一长"
        default: return "低音警示"
        }
    }
}

struct CueSettings {
    static let defaultStyles: [String: [String: String]] = [
        "complete": ["color": "#698DA8", "sound": "complete", "effect": "luminous-flow", "card_style": "native-compact", "width": "8", "inset": "3", "opacity": "0.85", "speed": "0.7", "trail": "0.28", "presence": "clear"],
        "input": ["color": "#B58B49", "sound": "input", "effect": "prism-flow", "card_style": "native-focus", "width": "8", "inset": "3", "opacity": "0.85", "speed": "0.7", "trail": "0.28", "presence": "clear"],
        "permission": ["color": "#B87461", "sound": "permission", "effect": "pearl-flow", "card_style": "native-focus", "width": "8", "inset": "3", "opacity": "0.85", "speed": "0.7", "trail": "0.28", "presence": "clear"]
    ]
    var enabled = true
    var completeEnabled = true
    var inputEnabled = true
    var permissionEnabled = true
    var glowEnabled = true
    var allDisplays = true
    var glowIntensity = 0.85
    var soundEnabled = true
    var soundVolume = 0.85
    var completeDuration = 8.0
    var inputDuration = 14.0
    var permissionDuration = 14.0
    var designGeneration = 3
    var eventStyles = CueSettings.defaultStyles

    init() {}
    init(json: [String: Any]) {
        designGeneration = json["design_generation"] as? Int ?? 1
        enabled = json["enabled"] as? Bool ?? true
        completeEnabled = json["complete_enabled"] as? Bool ?? true
        inputEnabled = json["input_enabled"] as? Bool ?? true
        permissionEnabled = json["permission_enabled"] as? Bool ?? true
        glowEnabled = json["glow_enabled"] as? Bool ?? true
        allDisplays = json["all_displays"] as? Bool ?? true
        glowIntensity = min(1, max(0.15, json["glow_intensity"] as? Double ?? 0.85))
        soundEnabled = json["sound_enabled"] as? Bool ?? true
        soundVolume = min(1, max(0, json["sound_volume"] as? Double ?? 0.85))
        completeDuration = min(60, max(3, json["complete_duration"] as? Double ?? 8))
        inputDuration = min(60, max(3, json["input_duration"] as? Double ?? 14))
        permissionDuration = min(60, max(3, json["permission_duration"] as? Double ?? 14))
        if let styles = json["event_styles"] as? [String: [String: String]] {
            for kind in ["complete", "input", "permission"] {
                if let color = styles[kind]?["color"], NSColor.cueColor(hex: color) != nil { eventStyles[kind]?["color"] = color }
                if let sound = styles[kind]?["sound"], CueSettings.validSoundToken(sound) { eventStyles[kind]?["sound"] = sound }
                let keys = Set(["effect", "palette", "direction", "speed", "width", "opacity", "inset", "trail", "card_style", "card_position", "card_opacity", "card_radius", "text_scale", "card_width", "card_message", "card_open", "effect_random_tags", "card_style_random_tags", "sound_random_tags", "presence"])
                let visual = (styles[kind] ?? [:]).filter { keys.contains($0.key) }
                if !visual.isEmpty {
                    var fields = eventStyles[kind] ?? [:]
                    fields.merge(visual) { _, new in new }
                    eventStyles[kind] = CueVisualStyle.normalizedFields(fields, fallback: color(for: kind))
                }
            }
        }
    }
    var json: [String: Any] {
        ["version": 1, "design_generation": designGeneration, "enabled": enabled, "complete_enabled": completeEnabled,
         "input_enabled": inputEnabled, "permission_enabled": permissionEnabled,
         "glow_enabled": glowEnabled, "all_displays": allDisplays,
         "glow_intensity": glowIntensity, "sound_enabled": soundEnabled,
         "sound_volume": soundVolume, "complete_duration": completeDuration,
         "input_duration": inputDuration, "permission_duration": permissionDuration,
         "event_styles": eventStyles]
    }
    mutating func useNativeRecommendedLook() {
        let previousGeneration = designGeneration
        designGeneration = 3
        for kind in ["complete", "input", "permission"] {
            let old = eventStyles[kind] ?? [:]
            var next = CueSettings.defaultStyles[kind] ?? [:]
            for key in ["color", "palette", "sound", "sound_random_tags", "direction", "card_position", "card_message", "card_open"] {
                if let value = old[key] { next[key] = value }
            }
            if previousGeneration >= 2 {
                for (key, value) in old where key.hasPrefix("card_") || key == "text_scale" { next[key] = value }
            }
            if old["effect"] == "random" { next["effect"] = "random"; next["effect_random_tags"] = "lighting" }
            if old["sound"] == "random", next["sound_random_tags"] == nil { next["sound_random_tags"] = "synthesized" }
            if old["card_style"] == "random", next["card_style_random_tags"] == nil { next["card_style"] = "random"; next["card_style_random_tags"] = "native" }
            eventStyles[kind] = next
        }
    }
    func allows(_ kind: String) -> Bool {
        guard enabled else { return false }
        switch kind {
        case "complete": return completeEnabled
        case "input": return inputEnabled
        case "permission": return permissionEnabled
        default: return true
        }
    }
    func allows(_ event: CueEvent) -> Bool { enabled && (event.isPreview || allows(event.kind)) }
    func duration(_ kind: String) -> Double {
        switch kind {
        case "complete": return completeDuration
        case "input": return inputDuration
        default: return permissionDuration
        }
    }
    static func validSoundToken(_ token: String) -> Bool {
        if token == "random" || token == "silent" || CueSoundCatalog.contains(id: token) { return true }
        let name = String(token.dropFirst(7))
        return token.hasPrefix("custom:") && !name.isEmpty && name != "." && name != ".." && name == URL(fileURLWithPath: name).lastPathComponent && !name.contains("/") && !name.contains("\\")
    }
    func color(for kind: String) -> NSColor {
        NSColor.cueColor(hex: eventStyles[kind]?["color"] ?? "") ?? CueEvent(kind: kind, title: "", message: "").color
    }
    func visualStyle(for kind: String) -> CueVisualStyle { CueVisualStyle(fields: eventStyles[kind] ?? [:], fallback: color(for: kind)) }
    func cardStyle(for kind: String) -> CueCardStyle { CueCardStyle(fields: eventStyles[kind] ?? [:]) }
    func soundToken(for kind: String) -> String { eventStyles[kind]?["sound"] ?? kind }
    func soundName(for kind: String) -> String {
        let token = soundToken(for: kind)
        if token == "silent" { return "静音" }
        if token == "random" { return "随机" }
        if token.hasPrefix("custom:") { return "自选音效" }
        return CueSoundCatalog.displayName(id: token)
    }
}

extension NSColor {
    static func cueColor(hex: String) -> NSColor? {
        let value = hex.hasPrefix("#") ? String(hex.dropFirst()) : hex
        guard value.count == 6, let rgb = UInt32(value, radix: 16) else { return nil }
        return NSColor(srgbRed: CGFloat((rgb >> 16) & 255) / 255, green: CGFloat((rgb >> 8) & 255) / 255, blue: CGFloat(rgb & 255) / 255, alpha: 1)
    }
    var cueHex: String {
        let color = usingColorSpace(.sRGB) ?? .white
        return String(format: "#%02X%02X%02X", Int((color.redComponent * 255).rounded()), Int((color.greenComponent * 255).rounded()), Int((color.blueComponent * 255).rounded()))
    }
}

extension Data {
    mutating func appendLittleEndian<T: FixedWidthInteger>(_ value: T) {
        var little = value.littleEndian
        Swift.withUnsafeBytes(of: &little) { append(contentsOf: $0) }
    }
}

final class CuePanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

struct CueResolution {
    let targetID: String
    init?(_ data: Data) {
        guard let raw = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              raw["action"] as? String == "resolve",
              raw["source"] as? String == "codex client approval stream",
              let target = raw["target_id"] as? String, !target.isEmpty, target.count <= 200 else { return nil }
        let date: Date?
        if let timestamp = raw["created_at"] as? Double { date = Date(timeIntervalSince1970: timestamp) }
        else if let text = raw["created_at"] as? String {
            let formatter = ISO8601DateFormatter(); formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            date = formatter.date(from: text) ?? ISO8601DateFormatter().date(from: text)
        } else { date = nil }
        guard let date, abs(date.timeIntervalSinceNow) <= 120 else { return nil }
        targetID = target
    }
}

struct CueResolutionTracker {
    private var ids: [String] = []
    mutating func insert(_ id: String) {
        if !ids.contains(id) { ids.append(id) }
        if ids.count > 1024 { ids.removeFirst(ids.count - 1024) }
    }
    func contains(_ id: String) -> Bool { ids.contains(id) }
}

final class CueController: NSObject, NSApplicationDelegate, AVAudioPlayerDelegate {
    static let previewDuration: TimeInterval = 6
    let home: URL
    let eventsDirectory: URL
    let inflightDirectory: URL
    var statusItem: NSStatusItem!
    var edges: [NSPanel] = []
    var edgeViews: [CueEdgeEffectView] = []
    var banner: NSPanel?
    var current: CueEvent?
    var previewComponent = "all"
    var previewFeedback = ""
    var appearanceMigrationError = ""
    var presentsWindows = true
    var resolvedFields: [String: String] = [:]
    var resolvedEventID = ""
    var lastPlayedSoundToken = ""
    var pending: [CueEvent] = []
    var recent: [CueEvent] = []
    var runningTasks: [CueRunningTask] = []
    var taskDetails: [String: CueTaskDetail] = [:]
    var batchController: CueCodexBatchController?
    private var runningTaskSnapshot = Data()
    lazy var recentHistory = CueRecentHistory(home: home)
    var shortcutMenu: NSMenu?
    var statusPopover: NSPopover?
    var statusPopoverController: CueStatusPopoverController?
    var usageDashboard: CueUsageDashboardController?
    var openConversationURL: (URL) -> Bool = { NSWorkspace.shared.open($0) }
    var seen: [String] = []
    var resolutions = CueResolutionTracker()
    var manualApprovalConnected = false
    var manualApprovalStatus = "人工批准监听未连接"
    var approvalLauncher: Process?
    var approvalLaunchLog: FileHandle?
    var readTimer: Timer?
    var pulseTimer: Timer?
    var effectFrameRate = 0.0
    var nextEffectTelemetry = Date.distantPast
    var hideTimer: Timer?
    var pausedUntil: Date?
    var settings = CueSettings()
    var muted: Bool { get { !settings.soundEnabled } set { settings.soundEnabled = !newValue } }
    var settingsPanel: CueSettingsController?
    var audioPlayer: AVAudioPlayer?
    var lastSound = ""
    var lastSoundPlayStarted = false
    var lastSoundPlayingAfter100ms = false
    var lastSoundPath = ""
    var lastSoundOutputDevice = "system-default"
    var lastSoundPeakDBFS: Float = -160
    var lastSoundPlaybackTime: Double = 0
    var lastSoundFinished = false
    var lastSoundFinishSucceeded = false
    var lastSoundError = ""
    var lastShownTitle = ""
    var lockFD: Int32 = -1
    var ownsLock = false
    var shownAt = Date()
    var monitorObserver: NSObjectProtocol?
    var watcherHealthText = "监控进程未连接"
    var nextHealthCheck = Date.distantPast
    var watcherProcess: Process?
    var watcherRestartTimer: Timer?
    var watcherRestartDelay = 2.0
    var watcherStartedAt = Date.distantPast
    var watcherRestartRequested = false
    var watcherLaunchError = ""
    var watcherLogHandles: [FileHandle] = []
    var shuttingDown = false
    var historyURL: URL?
    var historyScopeActive = false

    override init() {
        home = ProcessInfo.processInfo.environment["VIBECUE_HOME"].map { URL(fileURLWithPath: $0, isDirectory: true) }
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/VibeCue", isDirectory: true)
        eventsDirectory = home.appendingPathComponent("events", isDirectory: true)
        inflightDirectory = home.appendingPathComponent("inflight", isDirectory: true)
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        for directory in [home, eventsDirectory, inflightDirectory] {
            try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        }
        lockFD = Darwin.open(home.appendingPathComponent("app.lock").path, O_CREAT | O_RDWR, 0o600)
        guard lockFD >= 0, flock(lockFD, LOCK_EX | LOCK_NB) == 0 else {
            if !CommandLine.arguments.contains("--background") {
                try? Data("open".utf8).write(to: home.appendingPathComponent("settings.request"), options: .atomic)
            }
            NSApp.terminate(nil); return
        }
        ownsLock = true
        try? FileManager.default.removeItem(at: home.appendingPathComponent("disabled"))
        if let data = try? Data(contentsOf: home.appendingPathComponent("settings.json")),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            settings = CueSettings(json: json)
            if (json["design_generation"] as? Int ?? 1) < 3 {
                let backups = home.appendingPathComponent("backups", isDirectory: true)
                do {
                    try FileManager.default.createDirectory(at: backups, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
                    let backup = backups.appendingPathComponent("styles-before-lighting-" + UUID().uuidString + ".json")
                    try data.write(to: backup, options: .atomic)
                    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: backup.path)
                    settings.useNativeRecommendedLook()
                } catch {
                    appearanceMigrationError = "旧样式备份未完成，已保留当前外观。"
                    fputs("VibeCue: settings backup unavailable; existing styles retained\n", stderr)
                }
            }
        }
        saveSettings()
        restoreHistoryAccess()
        recent = recentHistory.events
        seen = recent.map(\.id)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = NSImage(systemSymbolName: "waveform.path", accessibilityDescription: "VibeCue")
        statusItem.button?.toolTip = "VibeCue — AI Coding Companion"
        statusItem.button?.target = self
        statusItem.button?.action = #selector(toggleStatusPopover(_:))
        statusItem.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        startWatcher()
        usageDashboard = CueUsageDashboardController(home: home)
        usageDashboard?.startBackgroundSync()
        updateWatcherHealth()
        refreshMenu()
        recoverInflight()
        readTimer = Timer.scheduledTimer(timeInterval: 0.5, target: self, selector: #selector(scanEvents), userInfo: nil, repeats: true)
        RunLoop.main.add(readTimer!, forMode: .common)
        monitorObserver = NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak self] _ in
            guard let self, let current = self.current else { return }
            self.makeWindows(for: current)
        }
        writeStatus()
        let args = CommandLine.arguments
        if args.contains("--settings") || (!args.contains("--background") && !args.contains("--demo")) {
            DispatchQueue.main.async { self.openSettings() }
        }
        if let captureIndex = args.firstIndex(of: "--capture-settings"), args.indices.contains(captureIndex + 1) {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                self.openSettings()
                if let kindIndex = args.firstIndex(of: "--settings-kind"), args.indices.contains(kindIndex + 1), ["complete", "input", "permission"].contains(args[kindIndex + 1]) {
                    self.settingsPanel?.selectedKind = args[kindIndex + 1]
                }
                if let tabIndex = args.firstIndex(of: "--settings-editor-tab"), args.indices.contains(tabIndex + 1), let tab = Int(args[tabIndex + 1]) { self.settingsPanel?.appearanceTab = min(2, max(0, tab)) }
                if let pageIndex = args.firstIndex(of: "--settings-page"), args.indices.contains(pageIndex + 1), let page = Int(args[pageIndex + 1]) {
                    self.settingsPanel?.selectPage(page)
                }
                self.settingsPanel?.capture(to: args[captureIndex + 1])
            }
        }
        if let demoIndex = args.firstIndex(of: "--demo"), args.indices.contains(demoIndex + 1) {
            let kind = args[demoIndex + 1]
            if ["complete", "input", "permission", "error"].contains(kind) {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
                    self.enqueue(CueEvent(kind: kind, title: "VibeCue · 提醒效果测试", message: "这是本机示范。正式提醒会显示对话名称。"))
                    if let captureIndex = args.firstIndex(of: "--capture"), args.indices.contains(captureIndex + 1) {
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { self.captureBanner(to: args[captureIndex + 1]) }
                    }
                    if let captureIndex = args.firstIndex(of: "--capture-glow"), args.indices.contains(captureIndex + 1) {
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { self.captureGlow(to: args[captureIndex + 1]) }
                    }
                }
            }
        }
        scanEvents()
    }

    func recoverInflight() {
        guard let files = try? FileManager.default.contentsOfDirectory(at: inflightDirectory, includingPropertiesForKeys: nil) else { return }
        for file in files where file.pathExtension == "json" {
            let target = eventsDirectory.appendingPathComponent(UUID().uuidString + ".json")
            try? FileManager.default.moveItem(at: file, to: target)
        }
    }

    @objc func scanEvents() {
        let settingsRequest = home.appendingPathComponent("settings.request")
        if FileManager.default.fileExists(atPath: settingsRequest.path) {
            try? FileManager.default.removeItem(at: settingsRequest)
            openSettings()
        }
        if nextHealthCheck <= Date() { updateWatcherHealth() }
        if let until = pausedUntil, until <= Date() { pausedUntil = nil; refreshMenu(); writeStatus() }
        guard let files = try? FileManager.default.contentsOfDirectory(at: eventsDirectory, includingPropertiesForKeys: [.isRegularFileKey, .fileSizeKey]) else { return }
        var batch: [Data] = []
        for file in files.filter({ $0.pathExtension == "json" }).sorted(by: { $0.lastPathComponent < $1.lastPathComponent }).prefix(100) {
            guard let properties = try? file.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey]),
                  properties.isRegularFile == true, properties.isSymbolicLink != true,
                  let size = properties.fileSize, size <= 1_048_576 else { continue }
            let claim = inflightDirectory.appendingPathComponent(UUID().uuidString + ".json")
            do {
                try FileManager.default.moveItem(at: file, to: claim)
                defer { try? FileManager.default.removeItem(at: claim) }
                batch.append(try Data(contentsOf: claim))
            } catch { /* Another process may have moved the file. */ }
        }
        // Resolve controls first: a request approved before this poll must never
        // briefly play a stale alert simply because its show file sorted first.
        for data in batch { if let resolution = CueResolution(data) { applyResolution(resolution, resume: false) } }
        for data in batch {
            guard let event = CueEvent(data), abs(event.date.timeIntervalSinceNow) <= 120,
                  !resolutions.contains(event.id) else { continue }
            enqueue(event)
        }
        if current == nil, pausedUntil == nil { showNext() }
    }

    func applyResolution(_ resolution: CueResolution, resume: Bool = true) {
        resolutions.insert(resolution.targetID)
        pending.removeAll { ["permission", "input"].contains($0.kind) && $0.id == resolution.targetID }
        _ = recentHistory.resolve(id: resolution.targetID)
        recent = recentHistory.events
        if let current, ["permission", "input"].contains(current.kind), current.id == resolution.targetID {
            audioPlayer?.stop(); hideWindows(); self.current = nil
        }
        refreshMenu(); writeStatus()
        if resume, current == nil, pausedUntil == nil { showNext() }
    }

    func enqueue(_ event: CueEvent) {
        if event.isPreview {
            _ = preview(kind: event.kind, component: "all")
            return
        }
        guard !seen.contains(event.id), !resolutions.contains(event.id) else { return }
        guard recentHistory.append(event) else { return }
        seen.append(event.id)
        if seen.count > 512 { seen.removeFirst(seen.count - 512) }
        recent = recentHistory.events
        refreshMenu()
        guard pausedUntil == nil, settings.allows(event) else { return }
        // A real event immediately replaces a preview, including completion events.
        if current?.isPreview == true {
            previewFeedback = "任务提醒优先显示，预览已停止。"
            settingsPanel?.updatePreviewFeedback(previewFeedback)
            audioPlayer?.stop(); hideWindows(); current = nil
        }
        // Keep the latest state per conversation while preserving other conversations.
        if !event.threadID.isEmpty { pending.removeAll { $0.threadID == event.threadID } }
        if let current, !event.threadID.isEmpty, current.threadID == event.threadID {
            hideWindows()
            self.current = nil
        }
        // Human attention takes priority over completion notices from other chats.
        if event.kind != "complete", current?.kind == "complete" {
            hideWindows()
            current = nil
        }
        if event.kind == "complete" {
            pending.append(event)
        } else {
            let firstCompletion = pending.firstIndex(where: { $0.kind == "complete" }) ?? pending.endIndex
            pending.insert(event, at: firstCompletion)
        }
        while pending.count > 20 {
            if let completion = pending.lastIndex(where: { $0.kind == "complete" }) { pending.remove(at: completion) }
            else { pending.removeFirst() }
        }
        if current == nil { showNext() }
    }

    @discardableResult
    func preview(kind: String, component: String) -> String {
        func finish(_ text: String) -> String {
            previewFeedback = text; settingsPanel?.updatePreviewFeedback(text); writeStatus(); return text
        }
        guard settings.enabled else { return finish("已保存选择。全局提醒已关闭，预览未播放。") }
        guard pausedUntil == nil else { return finish("已保存选择。全局提醒已暂停，预览未播放。") }
        guard component != "effect" || settings.glowEnabled else {
            return finish("屏幕光效已关闭。请在“泛光与声音”中开启后预览。")
        }
        guard current == nil || current?.isPreview == true,
              !pending.contains(where: { !$0.isPreview }) else {
            return finish("已保存选择，右侧可查看样例。真实任务提醒正在显示或排队，稍后可再次预览。")
        }
        if current?.isPreview == true { hideWindows(); current = nil }
        audioPlayer?.stop()
        previewComponent = component
        if component == "sound" {
            playChime(kind: kind, name: settings.soundName(for: kind))
            return finish(muted || settings.soundToken(for: kind) == "silent" ? "已保存音效。当前处于静音状态。" : "正在试听：\(lastSound)")
        }
        let event = CueEvent(kind: kind, title: "VibeCue · 样式预览", message: "这是样式预览。正式提醒会显示具体对话名称。")
        previewComponent = component; current = event; shownAt = Date()
        makeWindows(for: event)
        if component == "all" { playChime(kind: kind, name: settings.soundName(for: kind)) }
        let id = event.id
        hideTimer = Timer.scheduledTimer(withTimeInterval: Self.previewDuration, repeats: false) { [weak self] _ in
            guard let self, self.current?.id == id, self.current?.isPreview == true else { return }
            self.dismissCurrent()
        }
        RunLoop.main.add(hideTimer!, forMode: .common)
        return finish(component == "effect" ? "正在预览屏幕特效，可继续点选比较。" : component == "card" ? "正在预览提示框，可继续点选比较。" : "正在预览整套提醒。")
    }

    func showNext() {
        guard current == nil, pausedUntil == nil else { return }
        pending.removeAll { abs($0.date.timeIntervalSinceNow) > 120 || !settings.allows($0) }
        guard !pending.isEmpty else { writeStatus(); return }
        let event = pending.removeFirst()
        current = event; shownAt = Date()
        lastShownTitle = event.title
        makeWindows(for: event)
        playChime(kind: event.kind, name: event.sound)
        hideTimer = Timer.scheduledTimer(timeInterval: settings.duration(event.kind), target: self, selector: #selector(dismissCurrent), userInfo: nil, repeats: false)
        RunLoop.main.add(hideTimer!, forMode: .common)
        writeStatus()
    }

    func makeWindows(for event: CueEvent) {
        if resolvedEventID != event.id { resolveStyles(for: event, reroll: true) }
        guard presentsWindows else { return }
        edges.forEach { $0.close() }; edges.removeAll(); edgeViews.removeAll()
        banner?.close(); banner = nil
        let targetScreen = NSScreen.screens.first(where: { NSMouseInRect(NSEvent.mouseLocation, $0.frame, false) }) ?? NSScreen.main
        let screens = settings.allDisplays ? NSScreen.screens : targetScreen.map { [$0] } ?? []
        let showEdges = settings.glowEnabled && (!event.isPreview || previewComponent != "card")
        for screen in showEdges ? screens : [] {
            let panel = CuePanel(contentRect: screen.frame, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            configure(panel, clickThrough: true)
            let glow = CueEdgeEffectView(frame: NSRect(origin: .zero, size: screen.frame.size), style: resolvedVisualStyle(for: event))
            glow.displayDuration = event.isPreview ? Self.previewDuration : settings.duration(event.kind)
            glow.advance(to: NSWorkspace.shared.accessibilityDisplayShouldReduceMotion ? 0 : Date().timeIntervalSince(shownAt))
            panel.contentView = glow
            edgeViews.append(glow)
            panel.alphaValue = settings.glowIntensity
            panel.orderFrontRegardless()
            edges.append(panel)
        }
        if event.isPreview && previewComponent == "effect" { startEffectClock(); return }
        guard let screen = NSScreen.screens.first(where: { NSMouseInRect(NSEvent.mouseLocation, $0.frame, false) }) ?? NSScreen.main else { return }
        let cardStyle = CueCardStyle(fields: resolvedFields)
        let rect = cardStyle.frame(in: screen.visibleFrame, mouse: NSEvent.mouseLocation)
        let panel = CuePanel(contentRect: rect, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        configure(panel, clickThrough: false)
        panel.hasShadow = true
        panel.contentView = CueStyledBannerView(event: event, target: self, color: settings.color(for: event.kind), palette: resolvedVisualStyle(for: event).palette, style: cardStyle, availableWidth: rect.width)
        panel.alphaValue = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion ? 1 : 0
        panel.orderFrontRegardless()
        if panel.alphaValue == 0 {
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.18
                panel.animator().alphaValue = 1
            }
        }
        banner = panel
        startEffectClock()
    }

    func startEffectClock() {
        pulseTimer?.invalidate(); pulseTimer = nil
        effectFrameRate = 0
        guard !edgeViews.isEmpty, !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion else { return }
        var fps = 30.0
        if let event = current {
            let visual = resolvedVisualStyle(for: event)
            let pixels = edgeViews.reduce(CGFloat(0)) { total, view in
                let scale = view.window?.backingScaleFactor ?? 2
                return total + view.bounds.width * view.bounds.height * scale * scale
            }
            if CueEffectCatalog.preset(id: visual.effectID).recipe.material != nil {
                fps = pixels > 33_000_000 ? 12 : pixels > 18_000_000 ? 15 : pixels > 8_000_000 ? 18 : 24
            } else if visual.effectID == "spectrum-aurora" {
                fps = pixels > 18_000_000 ? 12 : pixels > 9_000_000 ? 15 : pixels > 3_500_000 || edgeViews.count > 1 ? 20 : 24
            }
        }
        effectFrameRate = fps
        nextEffectTelemetry = Date().addingTimeInterval(0.5)
        pulseTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / fps, repeats: true) { [weak self] _ in
            guard let self else { return }
            let elapsed = Date().timeIntervalSince(self.shownAt)
            self.edgeViews.forEach { $0.advance(to: elapsed) }
            if self.nextEffectTelemetry <= Date() {
                self.nextEffectTelemetry = Date().addingTimeInterval(1)
                self.writeStatus()
            }
        }
        RunLoop.main.add(pulseTimer!, forMode: .common)
    }

    func configure(_ panel: NSPanel, clickThrough: Bool) {
        panel.isOpaque = false; panel.backgroundColor = .clear
        panel.hasShadow = false; panel.ignoresMouseEvents = clickThrough
        // Stay above menu/Dock surfaces, below system/security and popup layers.
        panel.level = NSWindow.Level(rawValue: NSWindow.Level.statusBar.rawValue + (clickThrough ? 1 : 2))
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        panel.hidesOnDeactivate = false; panel.isReleasedWhenClosed = false
        panel.animationBehavior = .none
    }

    func hideWindows() {
        pulseTimer?.invalidate(); pulseTimer = nil; effectFrameRate = 0
        hideTimer?.invalidate(); hideTimer = nil
        edges.forEach { $0.close() }; edges.removeAll(); edgeViews.removeAll()
        if let panel = banner {
            panel.ignoresMouseEvents = true
            if shuttingDown || NSWorkspace.shared.accessibilityDisplayShouldReduceMotion { panel.close() }
            else {
                NSAnimationContext.runAnimationGroup({ context in
                    context.duration = 0.12
                    panel.animator().alphaValue = 0
                }, completionHandler: { panel.close() })
            }
        }
        banner = nil
    }

    func resolveStyles(for event: CueEvent, reroll: Bool) {
        var fields = settings.eventStyles[event.kind] ?? [:]
        if !reroll, resolvedEventID == event.id {
            for key in ["effect", "card_style", "sound"] where fields[key] == "random" {
                if let old = resolvedFields[key] { fields[key] = old }
            }
        }
        resolvedFields = CueResolvedSelection(fields: fields).fields
        resolvedEventID = event.id
    }
    func resolvedVisualStyle(for event: CueEvent) -> CueVisualStyle {
        CueVisualStyle(fields: resolvedEventID == event.id ? resolvedFields : settings.eventStyles[event.kind] ?? [:], fallback: settings.color(for: event.kind))
    }

    @objc func dismissCurrent() {
        if current?.isPreview == true {
            previewFeedback = "预览已结束。点击样式或“预览”可再次查看。"
            settingsPanel?.updatePreviewFeedback(previewFeedback)
        }
        hideWindows(); current = nil; writeStatus()
        if !pending.isEmpty {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                guard self.current == nil else { return }
                self.showNext()
            }
        }
    }
    @objc func openCurrent() { if let current { open(event: current) }; dismissCurrent() }
    @discardableResult
    func open(event: CueEvent) -> Bool {
        guard event.provider == "codex", !event.threadID.isEmpty,
              event.threadID.unicodeScalars.allSatisfy({ CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_" )).contains($0) }) else { return false }
        var components = URLComponents()
        components.scheme = "codex"; components.host = "threads"; components.path = "/" + event.threadID
        guard let url = components.url, openConversationURL(url) else { return false }
        _ = recentHistory.markOpened(id: event.id, didOpen: true)
        refreshMenu(); writeStatus()
        return true
    }
    @objc func openRecent(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String, let event = recent.first(where: { $0.id == id }) else { return }
        open(event: event)
    }
    @objc func openRunningRecent(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String,
              let task = runningTasks.first(where: { $0.threadID == id }) else { return }
        open(event: task.ephemeralCueEvent)
    }
    @objc func toggleMute() {
        muted.toggle(); saveSettings()
        if muted { audioPlayer?.stop() }
        refreshMenu(); settingsPanel?.rebuild(); writeStatus()
    }

    func playChime(kind: String, name: String) {
        audioPlayer?.stop()
        let selectedSound = CueResolvedSelection(fields: settings.eventStyles[kind] ?? [:]).fields["sound"] ?? kind
        let token = current?.kind == kind && resolvedEventID == current?.id ? (resolvedFields["sound"] ?? kind) : selectedSound
        lastPlayedSoundToken = token
        lastSound = token.hasPrefix("custom:") ? "自选音效" : token == "silent" ? "静音" : CueSoundCatalog.displayName(id: token)
        lastSoundPlayStarted = false; lastSoundPlayingAfter100ms = false
        lastSoundPath = ""; lastSoundPeakDBFS = -160; lastSoundPlaybackTime = 0
        lastSoundFinished = false; lastSoundFinishSucceeded = false; lastSoundError = ""
        guard settings.enabled, pausedUntil == nil, !muted, token != "silent" else { writeStatus(); return }
        do {
            let path: URL
            if token.hasPrefix("custom:") {
                guard CueSettings.validSoundToken(token) else { throw NSError(domain: "VibeCueAudio", code: 3, userInfo: [NSLocalizedDescriptionKey: "Invalid custom sound selection"]) }
                path = home.appendingPathComponent("sounds/custom", isDirectory: true).appendingPathComponent(String(token.dropFirst(7)))
                let properties = try path.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
                guard properties.isRegularFile == true, properties.isSymbolicLink != true else { throw NSError(domain: "VibeCueAudio", code: 4, userInfo: [NSLocalizedDescriptionKey: "Custom sound is unavailable"]) }
            } else {
                path = try CueChime.write(kind: token, directory: home.appendingPathComponent("sounds", isDirectory: true))
            }
            lastSoundPath = path.path
            let player = try AVAudioPlayer(contentsOf: path)
            player.volume = Float(settings.soundVolume)
            player.isMeteringEnabled = true
            player.delegate = self
            audioPlayer = player // Retain until playback completes or a new chime replaces it.
            guard player.prepareToPlay() else { throw NSError(domain: "VibeCueAudio", code: 1, userInfo: [NSLocalizedDescriptionKey: "Audio output preparation failed"]) }
            lastSoundPlayStarted = player.play()
            lastSoundOutputDevice = player.currentDevice ?? "system-default"
            if !lastSoundPlayStarted { lastSoundError = "AVAudioPlayer.play returned false" }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) { [weak self, weak player] in
                guard let self, let player, self.audioPlayer === player else { return }
                player.updateMeters()
                self.lastSoundPlayingAfter100ms = player.isPlaying
                self.lastSoundPlaybackTime = player.currentTime
                self.lastSoundPeakDBFS = player.peakPower(forChannel: 0)
                self.lastSoundOutputDevice = player.currentDevice ?? "system-default"
                self.writeStatus()
            }
        } catch {
            lastSoundError = error.localizedDescription
            fputs("VibeCue: audio playback failed: \(error.localizedDescription)\n", stderr)
        }
        writeStatus()
    }

    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        guard audioPlayer === player else { return }
        lastSoundFinished = true; lastSoundFinishSucceeded = flag
        lastSoundPlaybackTime = player.duration
        if current == nil, previewComponent == "sound" {
            previewFeedback = flag ? "试听已结束。点击音效可再次试听。" : "试听未完成，请查看音频状态。"
            settingsPanel?.updatePreviewFeedback(previewFeedback)
        }
        writeStatus()
    }

    func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        guard audioPlayer === player else { return }
        lastSoundError = error?.localizedDescription ?? "Audio decode failed"
        lastSoundFinished = true; lastSoundFinishSucceeded = false
        writeStatus()
    }

    @objc func testSound(_ sender: NSMenuItem) {
        let kind = sender.representedObject as? String ?? "input"
        _ = preview(kind: kind, component: "sound")
    }
    @objc func togglePause() {
        pausedUntil = pausedUntil == nil ? Date().addingTimeInterval(1800) : nil
        audioPlayer?.stop()
        pending.removeAll(); hideWindows(); current = nil
        refreshMenu(); settingsPanel?.updateHealth(); writeStatus()
    }
    @objc func demo(_ sender: NSMenuItem) {
        let kind = sender.representedObject as? String ?? "complete"
        enqueue(CueEvent(kind: kind, title: "VibeCue · 提醒效果测试", message: "这是本机示范。正式提醒会显示对话名称。"))
    }
    @objc func quit() {
        do {
            try Data("Disabled from menu at \(ISO8601DateFormatter().string(from: Date()))\n".utf8)
                .write(to: home.appendingPathComponent("disabled"), options: .atomic)
        } catch {
            fputs("VibeCue: unable to save disabled marker\n", stderr)
        }
        NSApp.terminate(nil)
    }

    func updateWatcherHealth() {
        nextHealthCheck = Date().addingTimeInterval(2)
        var message = "监控进程未连接"
        let previousManual = manualApprovalConnected
        var nextRunningTasks: [CueRunningTask] = []
        var nextTaskDetails: [String: CueTaskDetail] = [:]
        var nextRunningSnapshot = Data()
        manualApprovalConnected = false
        manualApprovalStatus = approvalLauncher?.isRunning == true ? "等待 Codex 退出后接通" : "人工批准监听未连接"
        if let launchData = try? Data(contentsOf: home.appendingPathComponent("bridge-launch-state.json")), launchData.count < 65_536,
           let launch = try? JSONSerialization.jsonObject(with: launchData) as? [String: Any] {
            switch launch["status"] as? String {
            case "waiting_for_codex_exit" where approvalLauncher?.isRunning == true: manualApprovalStatus = "等待你退出 Codex 后接通"
            case "launching" where approvalLauncher?.isRunning == true: manualApprovalStatus = "正在重新打开 Codex"
            case "launched": manualApprovalStatus = "Codex 已启动，等待人工批准监听连接"
            case "error": manualApprovalStatus = "人工批准监听启动失败，请查看诊断日志"
            default: break
            }
        }
        let path = home.appendingPathComponent("watcher-state.json")
        if let data = try? Data(contentsOf: path), data.count <= 2_097_152,
           let state = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           state["running"] as? Bool == true,
           let heartbeat = state["heartbeat"] as? Double,
           (0...15).contains(Date().timeIntervalSince1970 - heartbeat) {
            if let manual = state["manual_approval"] as? [String: Any] {
                manualApprovalConnected = manual["connected"] as? Bool == true
                if manualApprovalConnected { manualApprovalStatus = "人工批准监听已连接" }
            }
            let count = state["watched_threads"] as? Int ?? 0
            let taskRows = state["running_tasks"] as? [[String: Any]] ?? []
            let detailRows = state["task_details"] as? [String: [String: Any]] ?? [:]
            nextTaskDetails = detailRows.mapValues { CueTaskDetail(raw: $0) }
            nextRunningTasks = taskRows.compactMap { CueRunningTask(raw: $0) }
            nextRunningSnapshot = (try? JSONSerialization.data(withJSONObject: ["tasks": taskRows, "details": detailRows], options: [.sortedKeys])) ?? Data()
            let errors = state["read_error_count"] as? Int ?? 0
            if errors > 0 {
                let cursors = state["cursors"] as? [String: [String: Any]] ?? [:]
                let denied = cursors.values.contains { ($0["read_error"] as? String ?? "").contains("PermissionError") }
                message = denied ? "需要授权读取记录 · \(errors) 个文件无法读取" : "部分记录暂时无法读取 · \(errors) 个文件"
            } else if count == 0 {
                message = "尚未找到可监控的本机对话"
            } else {
                message = "本机监控中 · \(count) 个对话"
            }
        }
        let runningChanged = runningTaskSnapshot != nextRunningSnapshot
        runningTasks = nextRunningTasks; taskDetails = nextTaskDetails; runningTaskSnapshot = nextRunningSnapshot
        if watcherHealthText != message || runningChanged {
            watcherHealthText = message
            if statusItem != nil { refreshMenu() }
        }
        if previousManual != manualApprovalConnected { settingsPanel?.rebuild(); batchController?.refreshConnection() }
        settingsPanel?.updateHealth()
    }

    func startWatcher() {
        guard !shuttingDown, watcherProcess?.isRunning != true else { return }
        watcherRestartTimer?.invalidate(); watcherRestartTimer = nil
        let runtime = home.appendingPathComponent("runtime", isDirectory: true)
        guard FileManager.default.fileExists(atPath: runtime.appendingPathComponent("vibecue/cli.py").path) else {
            watcherLaunchError = "本机监控模块尚未安装"
            writeStatus(); return
        }
        let logs = home.appendingPathComponent("logs", isDirectory: true)
        try? FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-m", "vibecue.cli", "watch"]
        process.currentDirectoryURL = runtime
        var environment = ProcessInfo.processInfo.environment
        environment["VIBECUE_HOME"] = home.path
        environment["PYTHONPATH"] = runtime.path
        environment["PYTHONUNBUFFERED"] = "1"
        environment["VIBECUE_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        process.environment = environment
        watcherLogHandles.forEach { try? $0.close() }; watcherLogHandles = []
        for (name, isError) in [("watcher.out.log", false), ("watcher.err.log", true)] {
            let path = logs.appendingPathComponent(name)
            if !FileManager.default.fileExists(atPath: path.path) { FileManager.default.createFile(atPath: path.path, contents: nil, attributes: [.posixPermissions: 0o600]) }
            if let handle = try? FileHandle(forWritingTo: path) {
                _ = try? handle.seekToEnd()
                watcherLogHandles.append(handle)
                if isError { process.standardError = handle } else { process.standardOutput = handle }
            } else {
                if isError { process.standardError = FileHandle.nullDevice } else { process.standardOutput = FileHandle.nullDevice }
            }
        }
        process.terminationHandler = { [weak self] child in
            DispatchQueue.main.async {
                guard let self, self.watcherProcess === child else { return }
                self.watcherProcess = nil
                self.watcherLogHandles.forEach { try? $0.close() }; self.watcherLogHandles = []
                guard !self.shuttingDown else { return }
                let requested = self.watcherRestartRequested
                self.watcherRestartRequested = false
                if Date().timeIntervalSince(self.watcherStartedAt) > 30 { self.watcherRestartDelay = 2 }
                self.scheduleWatcherRestart(after: requested ? 0.25 : self.watcherRestartDelay)
                self.watcherRestartDelay = min(60, self.watcherRestartDelay * 2)
                self.updateWatcherHealth(); self.writeStatus()
            }
        }
        do {
            watcherProcess = process
            try process.run()
            watcherStartedAt = Date(); watcherLaunchError = ""
        } catch {
            watcherProcess = nil
            watcherLaunchError = "监控进程启动失败：\(error.localizedDescription)"
            scheduleWatcherRestart(after: watcherRestartDelay)
            watcherRestartDelay = min(60, watcherRestartDelay * 2)
        }
        writeStatus()
    }

    func scheduleWatcherRestart(after delay: Double) {
        guard !shuttingDown else { return }
        watcherRestartTimer?.invalidate()
        watcherRestartTimer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { [weak self] _ in self?.startWatcher() }
        if let watcherRestartTimer { RunLoop.main.add(watcherRestartTimer, forMode: .common) }
    }

    func restoreHistoryAccess() {
        let bookmark = home.appendingPathComponent("history-readonly.bookmark")
        guard let data = try? Data(contentsOf: bookmark) else { return }
        var stale = false
        guard let url = try? URL(resolvingBookmarkData: data, options: [.withSecurityScope, .withoutUI], relativeTo: nil, bookmarkDataIsStale: &stale) else { return }
        historyURL = url
        historyScopeActive = url.startAccessingSecurityScopedResource()
        if stale, let updated = try? url.bookmarkData(options: [.withSecurityScope, .securityScopeAllowOnlyReadAccess], includingResourceValuesForKeys: nil, relativeTo: nil) {
            try? updated.write(to: bookmark, options: .atomic)
        }
    }

    func expectedHistoryDirectory() -> URL {
        if let data = try? Data(contentsOf: home.appendingPathComponent("watcher-state.json")),
           let state = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let cursors = state["cursors"] as? [String: [String: Any]] {
            for cursor in cursors.values {
                guard let path = cursor["path"] as? String else { continue }
                var ancestor = URL(fileURLWithPath: path).resolvingSymlinksInPath().deletingLastPathComponent()
                while ancestor.path != "/" {
                    if ancestor.lastPathComponent.lowercased() == "sessions" { return ancestor.standardizedFileURL }
                    ancestor.deleteLastPathComponent()
                }
            }
        }
        let codexHome = ProcessInfo.processInfo.environment["CODEX_HOME"].map { URL(fileURLWithPath: $0, isDirectory: true) }
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".codex", isDirectory: true)
        return codexHome.appendingPathComponent("sessions", isDirectory: true).resolvingSymlinksInPath().standardizedFileURL
    }

    @objc func authorizeHistoryRead() {
        let expected = expectedHistoryDirectory()
        let panel = NSOpenPanel()
        panel.title = "授权读取本机 Codex 记录"
        panel.message = "请选择 Codex 的 sessions 记录文件夹。VibeCue 只读取本机记录以判断提醒，不修改或上传对话。"
        panel.prompt = "允许读取此文件夹"
        panel.canChooseDirectories = true; panel.canChooseFiles = false
        panel.canCreateDirectories = false; panel.allowsMultipleSelection = false
        panel.showsHiddenFiles = true; panel.directoryURL = expected
        guard panel.runModal() == .OK, let selected = panel.url else { return }
        guard selected.resolvingSymlinksInPath().standardizedFileURL.path == expected.path else {
            let alert = NSAlert()
            alert.messageText = "请选择 Codex 的 sessions 记录文件夹"
            alert.informativeText = "为了限定读取范围，请选择：\(expected.path)"
            alert.runModal(); return
        }
        if historyScopeActive { historyURL?.stopAccessingSecurityScopedResource() }
        historyURL = selected
        historyScopeActive = selected.startAccessingSecurityScopedResource()
        if let bookmark = try? selected.bookmarkData(options: [.withSecurityScope, .securityScopeAllowOnlyReadAccess], includingResourceValuesForKeys: nil, relativeTo: nil) {
            try? bookmark.write(to: home.appendingPathComponent("history-readonly.bookmark"), options: .atomic)
        }
        watcherRestartDelay = 2
        if watcherProcess?.isRunning == true {
            watcherRestartRequested = true
            watcherProcess?.terminate()
        } else { startWatcher() }
        updateWatcherHealth(); writeStatus()
    }

    @objc func connectManualApproval() {
        guard approvalLauncher?.isRunning != true else { return }
        let alert = NSAlert()
        alert.messageText = "接通 Codex 提醒与批量设置"
        alert.informativeText = "首次连接需要你主动退出并重新打开 Codex。请先完成或保存当前工作；VibeCue 会等你退出后重新打开，并连接待确认提醒与模型控制。模型设置只在你点击“应用”后修改。"
        alert.addButton(withTitle: "准备连接，等待我退出")
        alert.addButton(withTitle: "稍后")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let helper = home.appendingPathComponent("vibecuectl")
        guard FileManager.default.isExecutableFile(atPath: helper.path) else {
            let failure = NSAlert(); failure.messageText = "本机接入模块尚未安装"; failure.informativeText = "请先安装最新版 VibeCue。"; failure.runModal(); return
        }
        let process = Process(); process.executableURL = helper
        process.arguments = ["launch-codex", "--wait-for-exit"]
        var environment = ProcessInfo.processInfo.environment
        environment["VIBECUE_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        process.environment = environment
        process.currentDirectoryURL = home
        let logs = home.appendingPathComponent("logs", isDirectory: true)
        try? FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let path = logs.appendingPathComponent("manual-approval-launch.err.log")
        if !FileManager.default.fileExists(atPath: path.path) { FileManager.default.createFile(atPath: path.path, contents: nil, attributes: [.posixPermissions: 0o600]) }
        if let handle = try? FileHandle(forWritingTo: path) {
            handle.seekToEndOfFile(); approvalLaunchLog = handle; process.standardError = handle; process.standardOutput = handle
        }
        process.terminationHandler = { [weak self] ended in
            DispatchQueue.main.async {
                guard let self else { return }
                self.approvalLauncher = nil
                try? self.approvalLaunchLog?.close(); self.approvalLaunchLog = nil
                self.updateWatcherHealth(); self.settingsPanel?.rebuild(); self.writeStatus()
                if ended.terminationStatus != 0 { self.manualApprovalStatus = "连接启动失败，请查看诊断日志"; self.settingsPanel?.updateHealth() }
            }
        }
        do {
            approvalLauncher = process; try process.run()
            manualApprovalStatus = "等待 Codex 退出后接通"; settingsPanel?.rebuild(); writeStatus()
        } catch {
            approvalLauncher = nil
            let failure = NSAlert(); failure.messageText = "无法准备监听"; failure.informativeText = error.localizedDescription; failure.runModal()
        }
    }

    @objc func openSettings() {
        if settingsPanel == nil { settingsPanel = CueSettingsController(cue: self) }
        settingsPanel?.showWindow(nil)
        settingsPanel?.window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        settingsPanel?.updateHealth()
        writeStatus()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openSettings(); return true
    }

    @objc func toggleEnabled() {
        settings.enabled.toggle()
        applySettings()
        settingsPanel?.rebuild()
    }

    func saveSettings() {
        guard let data = try? JSONSerialization.data(withJSONObject: settings.json, options: [.prettyPrinted, .sortedKeys]) else { return }
        try? data.write(to: home.appendingPathComponent("settings.json"), options: .atomic)
    }

    func applySettings(updateCurrent: Bool = true) {
        saveSettings()
        if muted || !settings.enabled || pausedUntil != nil { audioPlayer?.stop() }
        audioPlayer?.volume = Float(settings.soundVolume)
        pending.removeAll { !settings.allows($0) }
        if let current, updateCurrent || current.isPreview {
            if settings.allows(current) {
                resolveStyles(for: current, reroll: false)
                makeWindows(for: current)
                hideTimer?.invalidate()
                let remaining = max(0.1, (current.isPreview ? Self.previewDuration : settings.duration(current.kind)) - Date().timeIntervalSince(shownAt))
                let id = current.id
                hideTimer = Timer.scheduledTimer(withTimeInterval: remaining, repeats: false) { [weak self] _ in
                    guard let self, self.current?.id == id else { return }
                    self.dismissCurrent()
                }
                RunLoop.main.add(hideTimer!, forMode: .common)
            } else { audioPlayer?.stop(); hideWindows(); self.current = nil }
        }
        refreshMenu(); writeStatus()
        if current == nil, pausedUntil == nil { showNext() }
    }

    func refreshMenu() {
        guard statusItem != nil else { return }
        let menu = NSMenu()
        let panel = NSMenuItem(title: "打开 VibeCue 面板…", action: #selector(openSettings), keyEquivalent: ",")
        panel.target = self; menu.addItem(panel)
        let usage = NSMenuItem(title: "用量统计与请求日志…", action: #selector(openUsage), keyEquivalent: "u")
        usage.target = self; menu.addItem(usage)
        let subtitle = NSMenuItem(title: "AI Coding Companion · AI 开发提醒助手", action: nil, keyEquivalent: "")
        subtitle.isEnabled = false; menu.addItem(subtitle)
        menu.addItem(.separator())
        let status = NSMenuItem(title: pausedUntil == nil ? "VibeCue · \(watcherHealthText)" : "已暂停提醒 30 分钟", action: nil, keyEquivalent: "")
        status.isEnabled = false; menu.addItem(status)
        menu.addItem(.separator())
        let enabled = NSMenuItem(title: "启用提醒", action: #selector(toggleEnabled), keyEquivalent: "")
        enabled.target = self; enabled.state = settings.enabled ? .on : .off; menu.addItem(enabled)
        let mute = NSMenuItem(title: "播放提示音", action: #selector(toggleMute), keyEquivalent: "")
        mute.target = self; mute.state = muted ? .off : .on; menu.addItem(mute)
        let pause = NSMenuItem(title: pausedUntil == nil ? "暂停提醒 30 分钟" : "恢复提醒", action: #selector(togglePause), keyEquivalent: "")
        pause.target = self; menu.addItem(pause)
        let demoMenu = NSMenu()
        for (name, kind) in [("完成", "complete"), ("需要回答", "input"), ("人工批准测试", "permission")] {
            let item = NSMenuItem(title: "\(name) · \(settings.soundName(for: kind))", action: #selector(demo(_:)), keyEquivalent: "")
            item.target = self; item.representedObject = kind; demoMenu.addItem(item)
        }
        let tests = NSMenuItem(title: "测试提醒效果", action: nil, keyEquivalent: ""); tests.submenu = demoMenu; menu.addItem(tests)
        let audioMenu = NSMenu()
        for (name, kind) in [("完成", "complete"), ("需要回答", "input"), ("人工批准测试音", "permission")] {
            let item = NSMenuItem(title: "\(name) · \(settings.soundName(for: kind))", action: #selector(testSound(_:)), keyEquivalent: "")
            item.target = self; item.representedObject = kind; item.isEnabled = !muted; audioMenu.addItem(item)
        }
        let audioTests = NSMenuItem(title: "只测试提示音", action: nil, keyEquivalent: ""); audioTests.submenu = audioMenu; menu.addItem(audioTests)
        if !recent.isEmpty || !runningTasks.isEmpty {
            menu.addItem(.separator())
            func heading(_ title: String) {
                let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
                item.isEnabled = false; menu.addItem(item)
            }
            func reminder(_ record: CueRecentRecord) {
                let event = record.event
                let isRunning = runningTasks.contains { $0.threadID == event.threadID && event.provider == "codex" }
                let title = "\(record.isOpened ? "↗" : "●") \(event.title.prefix(52))\(isRunning ? " · 运行中" : "")"
                let item = NSMenuItem(title: title, action: #selector(openRecent(_:)), keyEquivalent: "")
                item.attributedTitle = NSAttributedString(string: title, attributes: [.foregroundColor: record.isOpened ? NSColor.secondaryLabelColor : NSColor.systemBlue, .font: NSFont.systemFont(ofSize: 13, weight: record.isOpened ? .regular : .semibold)])
                item.target = self; item.representedObject = event.id; item.isEnabled = record.canOpen
                item.toolTip = "\(event.label) · \(event.title)"; menu.addItem(item)
            }
            let pinned = recentHistory.records.filter(\.isPinned)
            if !pinned.isEmpty { heading("置顶提醒 · \(pinned.count) 条"); pinned.forEach(reminder) }
            let pinnedThreads = Set(pinned.filter { $0.event.provider == "codex" }.map { $0.event.threadID })
            let active = runningTasks.filter { !pinnedThreads.contains($0.threadID) }
            if !active.isEmpty {
                heading("正在运行 · \(active.count) 项")
                for task in active {
                    let item = NSMenuItem(title: "◉ \(task.title.prefix(52))", action: #selector(openRunningRecent(_:)), keyEquivalent: "")
                    item.target = self; item.representedObject = task.threadID; menu.addItem(item)
                }
            }
            let activeIDs = Set(runningTasks.map(\.threadID))
            let ordinary = recentHistory.records.filter { !$0.isPinned && !($0.event.provider == "codex" && activeIDs.contains($0.event.threadID)) }
            if !ordinary.isEmpty { heading("历史提醒 · \(ordinary.count) 条"); ordinary.forEach(reminder) }
        }
        menu.addItem(.separator())
        let exit = NSMenuItem(title: "结束 VibeCue", action: #selector(quit), keyEquivalent: "q")
        exit.target = self; menu.addItem(exit)
        shortcutMenu = menu
        statusItem.button?.title = recentHistory.unopenedCount > 0 ? " \(recentHistory.unopenedCount)" : ""
        statusItem.button?.toolTip = "VibeCue · \(recentHistory.unopenedCount) 条未读提醒 · \(runningTasks.count) 个任务正在运行"
        if statusPopover?.isShown == true { updateStatusPopover() }
    }

    @objc func openUsage() {
        statusPopover?.performClose(nil)
        if usageDashboard == nil { usageDashboard = CueUsageDashboardController(home: home) }
        usageDashboard?.showWindow(nil)
        usageDashboard?.window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func toggleStatusPopover(_ sender: NSButton? = nil) {
        guard let button = sender ?? statusItem.button else { return }
        if NSApp.currentEvent?.type == .rightMouseUp, let menu = shortcutMenu {
            statusPopover?.performClose(nil)
            menu.popUp(positioning: nil, at: NSPoint(x: 0, y: button.bounds.minY), in: button)
            return
        }
        if statusPopover?.isShown == true { statusPopover?.performClose(nil); writeStatus(); return }
        if statusPopoverController == nil {
            let actions = CueStatusPopoverActions(
                openSettings: { [weak self] in self?.statusPopover?.performClose(nil); self?.openSettings() },
                openUsage: { [weak self] in self?.openUsage() },
                toggleEnabled: { [weak self] in self?.toggleEnabled() },
                toggleSound: { [weak self] in self?.toggleMute() },
                pause: { [weak self] in self?.togglePause() },
                demo: { [weak self] in
                    guard let self else { return }
                    self.statusPopover?.performClose(nil)
                    _ = self.preview(kind: "complete", component: "all")
                },
                quit: { [weak self] in self?.quit() },
                openEvent: { [weak self] event in
                    guard let self else { return }
                    if self.open(event: event) { self.statusPopover?.performClose(nil) }
                },
                historyChanged: { [weak self] in
                    guard let self else { return }
                    self.recent = self.recentHistory.events
                    self.refreshMenu(); self.settingsPanel?.updateHealth(); self.writeStatus()
                },
                batchConfigure: { [weak self] targets in
                    guard let self else { return }
                    self.statusPopover?.performClose(nil)
                    self.batchController = CueCodexBatchController(home: self.home, targets: targets, connect: { [weak self] in self?.connectManualApproval() }, completion: { [weak self] in self?.updateWatcherHealth() })
                    self.batchController?.showWindow(nil); self.batchController?.window?.makeKeyAndOrderFront(nil)
                    NSApp.activate(ignoringOtherApps: true)
                },
                connectCodex: { [weak self] in
                    self?.statusPopover?.performClose(nil); self?.connectManualApproval()
                })
            statusPopoverController = CueStatusPopoverController(history: recentHistory, actions: actions)
            let popover = NSPopover(); popover.behavior = .transient
            popover.contentViewController = statusPopoverController
            statusPopover = popover
        }
        updateStatusPopover()
        statusPopover?.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        writeStatus()
    }

    func updateStatusPopover() {
        let screen = statusItem.button?.window?.screen ?? NSScreen.main
        let height = min(900, (screen?.visibleFrame.height ?? 900) - 52)
        statusPopoverController?.update(status: watcherHealthText, enabled: settings.enabled,
                                       soundEnabled: settings.soundEnabled, paused: pausedUntil != nil, maximumHeight: height, runningTasks: runningTasks, taskDetails: taskDetails, attentionConnected: manualApprovalConnected)
        if let controller = statusPopoverController, controller.isViewLoaded { statusPopover?.contentSize = controller.preferredContentSize }
    }

    func writeStatus() {
        let data: [String: Any] = ["pid": ProcessInfo.processInfo.processIdentifier, "muted": muted,
            "paused_until": pausedUntil?.timeIntervalSince1970 ?? 0,
            "current_id": current?.id ?? "", "current_kind": current?.kind ?? "",
            "current_title": current?.title ?? "", "last_shown_title": lastShownTitle,
            "current_color": current.map { settings.color(for: $0.kind).cueHex } ?? "",
            "effect_frame_rate": effectFrameRate,
            "effect_render_ms": edgeViews.map { $0.lastRenderDuration * 1000 },
            "effect_rendered_frames": edgeViews.map(\.renderedFrameCount),
            "preview_duration": Self.previewDuration,
            "edge_windows": edges.map { ["visible": $0.isVisible, "level": $0.level.rawValue, "alpha": $0.alphaValue, "frame": NSStringFromRect($0.frame)] as [String: Any] },
            "banner_visible": banner?.isVisible ?? false,
            "effect_preset_count": CueEffectCatalog.presets.count, "card_style_count": CueCardCatalog.presets.count,
            "sound_preset_count": CueSoundCatalog.presets.count,
            "current_is_preview": current?.isPreview ?? false, "preview_component": previewComponent,
            "appearance_migration_error": appearanceMigrationError, "preview_feedback": previewFeedback, "recent_count": recent.count, "recent_capacity": CueRecentHistory.capacity, "recent_pinned_count": recentHistory.pinnedCount, "recent_ordinary_count": recentHistory.ordinaryCount, "recent_unopened_count": recentHistory.unopenedCount, "running_task_count": runningTasks.count, "recent_history_error": recentHistory.lastError, "recent_panel_visible": statusPopover?.isShown ?? false, "recent_panel_size": NSStringFromSize(statusPopover?.contentSize ?? .zero), "seen_count": seen.count,
            "current_effect": current.map { resolvedVisualStyle(for: $0).effectID } ?? "",
            "current_card_style": current.map { _ in CueCardStyle(fields: resolvedFields).id } ?? "",
            "selected_sound": lastPlayedSoundToken,
            "edge_count": edges.count, "last_sound": lastSound, "sound_play_started": lastSoundPlayStarted,
            "audio_backend": "AVAudioPlayer", "sound_is_playing": audioPlayer?.isPlaying ?? false,
            "sound_is_playing_after_100ms": lastSoundPlayingAfter100ms, "sound_path": lastSoundPath,
            "sound_peak_dbfs": lastSoundPeakDBFS, "sound_playback_time": lastSoundPlaybackTime,
            "sound_output_device_uid": lastSoundOutputDevice, "sound_finished": lastSoundFinished,
            "sound_finish_succeeded": lastSoundFinishSucceeded, "sound_error": lastSoundError,
            "settings": settings.json, "panel_visible": settingsPanel?.window?.isVisible ?? false,
            "watcher_child_pid": watcherProcess?.isRunning == true ? watcherProcess!.processIdentifier : 0,
            "watcher_launch_error": watcherLaunchError, "watcher_restart_delay": watcherRestartDelay,
            "history_scope_active": historyScopeActive, "history_selected_path": historyURL?.path ?? "",
            "provider": current?.provider ?? "codex", "supported_providers": ["codex"],
            "codex_permission_wait_supported": true, "manual_approval_connected": manualApprovalConnected,
            "manual_approval_status": manualApprovalStatus, "raw_permission_requests_suppressed": true,
            "queued": pending.count, "updated_at": Date().timeIntervalSince1970]
        if let json = try? JSONSerialization.data(withJSONObject: data, options: [.sortedKeys]) {
            try? json.write(to: home.appendingPathComponent("app-status.json"), options: .atomic)
        }
    }

    func captureBanner(to path: String) {
        guard let view = banner?.contentView, let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
        view.cacheDisplay(in: view.bounds, to: rep)
        if let data = rep.representation(using: .png, properties: [:]) { try? data.write(to: URL(fileURLWithPath: path)) }
    }

    func captureGlow(to path: String) {
        guard let view = edges.first?.contentView, let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
        view.cacheDisplay(in: view.bounds, to: rep)
        if let data = rep.representation(using: .png, properties: [:]) { try? data.write(to: URL(fileURLWithPath: path)) }
    }

    func applicationWillTerminate(_ notification: Notification) {
        guard ownsLock else { if lockFD >= 0 { Darwin.close(lockFD) }; return }
        shuttingDown = true
        usageDashboard?.stopBackgroundSync()
        approvalLauncher?.terminate()
        try? approvalLaunchLog?.close(); approvalLaunchLog = nil
        watcherRestartTimer?.invalidate()
        if let process = watcherProcess, process.isRunning { process.terminate() }
        if historyScopeActive { historyURL?.stopAccessingSecurityScopedResource() }
        readTimer?.invalidate(); hideWindows()
        if let monitorObserver { NotificationCenter.default.removeObserver(monitorObserver) }
        try? FileManager.default.removeItem(at: home.appendingPathComponent("app-status.json"))
        if lockFD >= 0 { flock(lockFD, LOCK_UN); Darwin.close(lockFD) }
    }
}

final class CueFlippedView: NSView {
    var backgroundColor: NSColor? { didSet { updateColors() } }
    var outlineColor: NSColor? { didSet { updateColors() } }
    var appearanceDidChange: (() -> Void)?
    override var isFlipped: Bool { true }
    private func updateColors() {
        guard backgroundColor != nil || outlineColor != nil else { return }
        wantsLayer = true
        effectiveAppearance.performAsCurrentDrawingAppearance {
            if let backgroundColor { layer?.backgroundColor = backgroundColor.cgColor }
            if let outlineColor { layer?.borderColor = outlineColor.cgColor }
        }
    }
    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance(); updateColors(); needsDisplay = true
        appearanceDidChange?()
    }
}

final class CueSettingsController: NSWindowController, NSWindowDelegate {
    unowned let cue: CueController
    let root = CueFlippedView(frame: NSRect(x: 0, y: 0, width: 760, height: 590))
    let sidebar = CueFlippedView(frame: NSRect(x: 0, y: 0, width: 182, height: 590))
    let content = CueFlippedView(frame: NSRect(x: 182, y: 0, width: 578, height: 590))
    var page = 0
    var navigation: [NSButton] = []
    var healthLabel: NSTextField?
    var threadLabel: NSTextField?
    var latestLabel: NSTextField?
    var hookLabel: NSTextField?
    var sliderLabels: [String: NSTextField] = [:]
    var selectedKind = "complete"
    var appearanceTab = 0
    var presetBrowser: CuePresetBrowserController?
    var appearanceValues: [String: NSTextField] = [:]
    var advancedEffectsExpanded = false
    var appearanceScroll: NSScrollView?
    var previewStatusLabel: NSTextField?

    init(cue: CueController) {
        self.cue = cue
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 590), styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "VibeCue"
        window.titleVisibility = .visible
        window.titlebarAppearsTransparent = true
        window.isReleasedWhenClosed = false
        window.center()
        super.init(window: window)
        window.delegate = self
        window.contentView = root
        root.wantsLayer = true
        root.backgroundColor = .windowBackgroundColor
        sidebar.wantsLayer = true
        sidebar.backgroundColor = .underPageBackgroundColor
        root.addSubview(sidebar); root.addSubview(content)
        buildSidebar()
        root.appearanceDidChange = { [weak self] in self?.rebuild() }
        rebuild()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    @discardableResult
    func label(_ text: String, in parent: NSView, frame: NSRect, size: CGFloat = 13, weight: NSFont.Weight = .regular, color: NSColor = .labelColor, lines: Int = 1) -> NSTextField {
        let field = lines > 1 ? NSTextField(wrappingLabelWithString: text) : NSTextField(labelWithString: text)
        field.font = .systemFont(ofSize: size, weight: weight)
        field.textColor = color; field.frame = frame
        field.maximumNumberOfLines = lines
        field.lineBreakMode = lines > 1 ? .byWordWrapping : .byTruncatingTail
        parent.addSubview(field)
        return field
    }

    func buildSidebar() {
        let mark = NSImageView(frame: NSRect(x: 20, y: 29, width: 30, height: 30))
        mark.image = NSImage(systemSymbolName: "waveform.path", accessibilityDescription: "VibeCue")
        mark.contentTintColor = .secondaryLabelColor
        mark.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 24, weight: .semibold)
        sidebar.addSubview(mark)
        label("VibeCue", in: sidebar, frame: NSRect(x: 60, y: 29, width: 116, height: 30), size: 19, weight: .semibold)
        label("AI 开发提醒助手", in: sidebar, frame: NSRect(x: 20, y: 73, width: 156, height: 18), size: 11, color: .secondaryLabelColor)
        for (index, pair) in [("总览", "square.grid.2x2"), ("提醒规则", "bell"), ("泛光与声音", "waveform"), ("集成与文件", "puzzlepiece.extension"), ("用量统计", "chart.bar.xaxis")].enumerated() {
            let button = NSButton(title: pair.0, target: self, action: #selector(navigate(_:)))
            button.image = NSImage(systemSymbolName: pair.1, accessibilityDescription: nil)
            button.imagePosition = .imageLeading; button.imageScaling = .scaleProportionallyDown
            button.alignment = .left; button.isBordered = false
            button.font = .systemFont(ofSize: 13, weight: .medium)
            button.frame = NSRect(x: 13, y: 147 + index * 46, width: 156, height: 37)
            button.tag = index; button.identifier = NSUserInterfaceItemIdentifier("settings.page.\(index)")
            button.wantsLayer = true; button.layer?.cornerRadius = 8
            sidebar.addSubview(button); navigation.append(button)
        }
        let line = NSView(frame: NSRect(x: 20, y: 517, width: 142, height: 1))
        line.wantsLayer = true; line.layer?.backgroundColor = NSColor.separatorColor.cgColor; sidebar.addSubview(line)
        label("本机运行 · Codex", in: sidebar, frame: NSRect(x: 20, y: 533, width: 154, height: 19), size: 10.5, color: .secondaryLabelColor)
        label("VibeCue \(Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.5.0")", in: sidebar, frame: NSRect(x: 20, y: 555, width: 154, height: 17), size: 10.5, color: .tertiaryLabelColor)
    }

    @objc func navigate(_ sender: NSButton) {
        if sender.tag == 4 { cue.openUsage(); return }
        selectPage(sender.tag)
    }
    func selectPage(_ value: Int) { page = min(3, max(0, value)); rebuild() }

    func rebuild() {
        content.subviews.forEach { $0.removeFromSuperview() }
        healthLabel = nil; threadLabel = nil; latestLabel = nil; hookLabel = nil; sliderLabels = [:]; previewStatusLabel = nil
        for (index, button) in navigation.enumerated() {
            button.layer?.backgroundColor = (index == page ? NSColor.controlAccentColor.withAlphaComponent(0.1) : .clear).cgColor
            button.contentTintColor = index == page ? .labelColor : .secondaryLabelColor
        }
        switch page {
        case 0: overview()
        case 1: notifications()
        case 2: glowAndSound()
        default: providers()
        }
        if page != 1 { label(cue.appearanceMigrationError.isEmpty ? "变更会立即保存到本机。" : cue.appearanceMigrationError, in: content, frame: NSRect(x: 26, y: 558, width: 525, height: 18), size: 11, color: cue.appearanceMigrationError.isEmpty ? .tertiaryLabelColor : .systemOrange) }
        updateHealth()
    }

    func heading(_ title: String, _ detail: String) {
        label(title, in: content, frame: NSRect(x: 26, y: 25, width: 525, height: 31), size: 20, weight: .semibold)
        label(detail, in: content, frame: NSRect(x: 27, y: 66, width: 525, height: 21), size: 12, color: .secondaryLabelColor)
    }

    func card(_ y: CGFloat, _ height: CGFloat) -> CueFlippedView {
        let view = CueFlippedView(frame: NSRect(x: 26, y: y, width: 526, height: height))
        view.wantsLayer = true
        view.backgroundColor = .controlBackgroundColor
        view.outlineColor = .separatorColor
        view.layer?.borderWidth = 1; view.layer?.cornerRadius = 13
        content.addSubview(view); return view
    }

    func toggle(_ title: String, _ detail: String, in parent: NSView, y: CGFloat, key: String, on: Bool, enabled: Bool = true) {
        label(title, in: parent, frame: NSRect(x: 20, y: y, width: 425, height: 22), size: 13, weight: .medium)
        if !detail.isEmpty { label(detail, in: parent, frame: NSRect(x: 20, y: y + 25, width: 425, height: 32), size: 11, color: .secondaryLabelColor, lines: 2) }
        let control = NSSwitch(frame: NSRect(x: 467, y: y + 1, width: 40, height: 24))
        control.state = on ? .on : .off; control.target = self; control.action = #selector(changeToggle(_:))
        control.identifier = NSUserInterfaceItemIdentifier(key); control.setAccessibilityLabel(title)
        control.isEnabled = enabled
        parent.addSubview(control)
    }

    func rule(_ title: String, _ detail: String, y: CGFloat, key: String, on: Bool, duration: Double, durationKey: String, enabled: Bool = true) {
        let view = card(y, 122)
        toggle(title, detail, in: view, y: 17, key: key, on: on, enabled: enabled)
        label("显示时间", in: view, frame: NSRect(x: 20, y: 84, width: 120, height: 23), size: 11.5, color: .secondaryLabelColor)
        let popup = NSPopUpButton(frame: NSRect(x: 391, y: 80, width: 116, height: 26), pullsDown: false)
        var values: [Double] = [3, 5, 8, 14, 20, 30, 60]
        if !values.contains(duration) { values.append(duration); values.sort() }
        for seconds in values {
            popup.addItem(withTitle: "\(Int(seconds)) 秒")
            popup.lastItem?.representedObject = seconds
            if seconds == duration { popup.select(popup.lastItem) }
        }
        popup.identifier = NSUserInterfaceItemIdentifier(durationKey)
        popup.target = self; popup.action = #selector(changeDuration(_:))
        popup.setAccessibilityLabel(title + "显示时间")
        popup.isEnabled = enabled
        view.addSubview(popup)
    }

    func slider(_ title: String, in parent: NSView, y: CGFloat, key: String, value: Double, min: Double = 0, max: Double = 1) {
        label(title, in: parent, frame: NSRect(x: 20, y: y, width: 220, height: 20), size: 12, weight: .medium)
        let valueLabel = label("\(Int((value * 100).rounded()))%", in: parent, frame: NSRect(x: 447, y: y, width: 58, height: 20), size: 11.5, color: .secondaryLabelColor)
        valueLabel.alignment = .right; sliderLabels[key] = valueLabel
        let control = NSSlider(value: value, minValue: min, maxValue: max, target: self, action: #selector(changeSlider(_:)))
        control.frame = NSRect(x: 20, y: y + 30, width: 486, height: 22)
        control.isContinuous = true; control.identifier = NSUserInterfaceItemIdentifier(key)
        control.setAccessibilityLabel(title); parent.addSubview(control)
    }

    func button(_ title: String, in parent: NSView, frame: NSRect, action: Selector, represented: String = "") -> NSButton {
        let control = NSButton(title: title, target: self, action: action)
        control.bezelStyle = .rounded; control.font = .systemFont(ofSize: 12)
        control.frame = frame; control.identifier = NSUserInterfaceItemIdentifier(represented)
        parent.addSubview(control); return control
    }

    func overview() {
        heading("AI 开发提醒助手", "离开对话窗口，也能掌握需要你注意的时刻。")
        let status = card(106, 199)
        label("CODEX · 本机监控", in: status, frame: NSRect(x: 20, y: 17, width: 480, height: 21), size: 11, weight: .semibold, color: .secondaryLabelColor)
        healthLabel = label("", in: status, frame: NSRect(x: 20, y: 50, width: 480, height: 26), size: 18, weight: .semibold)
        threadLabel = label("", in: status, frame: NSRect(x: 20, y: 87, width: 480, height: 24), size: 12, color: .secondaryLabelColor)
        toggle("启用提醒", "暂停全部泛光、提示卡与声音。", in: status, y: 130, key: "enabled", on: cue.settings.enabled)
        let latest = card(322, 101)
        label("最近一则提醒", in: latest, frame: NSRect(x: 20, y: 15, width: 480, height: 20), size: 11, color: .secondaryLabelColor)
        latestLabel = label("", in: latest, frame: NSRect(x: 20, y: 45, width: 480, height: 37), size: 13, weight: .medium, lines: 2)
        _ = button("测试完成提醒", in: content, frame: NSRect(x: 25, y: 450, width: 168, height: 32), action: #selector(testAlert(_:)), represented: "complete")
        _ = button("测试等待输入", in: content, frame: NSRect(x: 205, y: 450, width: 168, height: 32), action: #selector(testAlert(_:)), represented: "input")
        _ = button("最近提醒…", in: content, frame: NSRect(x: 385, y: 450, width: 168, height: 32), action: #selector(openRecentInbox(_:)))
        label("测试会使用目前的提醒与音量设置；一般提醒不会抢走输入焦点。", in: content, frame: NSRect(x: 27, y: 505, width: 520, height: 34), size: 11, color: .secondaryLabelColor, lines: 2)
    }

    @objc func openRecentInbox(_ sender: NSButton) { cue.toggleStatusPopover(sender) }

    func notifications() {
        heading("提醒外观", "克制的光线，清楚的对话。按分类选择，点选即预览。")
        let selector = NSPopUpButton(frame: NSRect(x: 26, y: 106, width: 526, height: 31), pullsDown: false)
        for (name, kind) in [("本轮已完成", "complete"), ("需要回答或判断", "input"), ("人工批准", "permission")] {
            selector.addItem(withTitle: name); selector.lastItem?.representedObject = kind
            if kind == selectedKind { selector.select(selector.lastItem) }
        }
        selector.target = self; selector.action = #selector(selectEventKind(_:)); selector.identifier = NSUserInterfaceItemIdentifier("event.kind")
        selector.setAccessibilityLabel("自定义提醒类型"); content.addSubview(selector)
        for (index, title) in ["屏幕光影", "提示卡", "声音与规则"].enumerated() {
            let control = NSButton(title: title, target: self, action: #selector(selectAppearanceTab(_:)))
            control.frame = NSRect(x: 26 + index * 178, y: 151, width: 170, height: 30)
            control.bezelStyle = .rounded; control.tag = index; control.state = appearanceTab == index ? .on : .off
            control.isBordered = false; control.wantsLayer = true; control.layer?.cornerRadius = 8
            control.layer?.backgroundColor = (appearanceTab == index ? NSColor.controlAccentColor.withAlphaComponent(0.1) : NSColor.controlBackgroundColor).cgColor
            control.contentTintColor = appearanceTab == index ? .labelColor : .secondaryLabelColor
            control.identifier = NSUserInterfaceItemIdentifier("appearance.tab.\(index)")
            control.font = .systemFont(ofSize: 12, weight: appearanceTab == index ? .semibold : .regular)
            content.addSubview(control)
        }
        let scroll = NSScrollView(frame: NSRect(x: 26, y: 196, width: 526, height: 330))
        scroll.hasVerticalScroller = true; scroll.scrollerStyle = .legacy; scroll.drawsBackground = false; scroll.borderType = .noBorder
        let document = CueFlippedView(frame: NSRect(x: 0, y: 0, width: 510, height: appearanceTab == 0 ? (advancedEffectsExpanded ? 1040 : 520) : appearanceTab == 1 ? 780 : 465))
        document.wantsLayer = true; document.backgroundColor = .controlBackgroundColor; document.layer?.cornerRadius = 14
        scroll.documentView = document; content.addSubview(scroll); appearanceScroll = scroll
        appearanceValues = [:]
        if appearanceTab == 0 { edgeEditor(in: document) }
        else if appearanceTab == 1 { cardEditor(in: document) }
        else { soundRuleEditor(in: document) }
        _ = button("预览整套提醒", in: content, frame: NSRect(x: 23, y: 535, width: 260, height: 34), action: #selector(previewEvent))
        _ = button("恢复此类默认", in: content, frame: NSRect(x: 293, y: 535, width: 260, height: 34), action: #selector(resetEventStyle))
        previewStatusLabel = label(cue.previewFeedback.isEmpty ? "分类可多选，随机遵循分类。设置即时保存，预览遵循静音与暂停。" : cue.previewFeedback, in: content, frame: NSRect(x: 28, y: 573, width: 520, height: 16), size: 11, color: .secondaryLabelColor)
    }

    func updatePreviewFeedback(_ text: String) {
        previewStatusLabel?.stringValue = text
        previewStatusLabel?.toolTip = text
        presetBrowser?.updateFeedback(text)
    }

    func editorLabel(_ title: String, _ subtitle: String, in parent: NSView, y: CGFloat) {
        label(title, in: parent, frame: NSRect(x: 18, y: y, width: 472, height: 22), size: 13, weight: .semibold)
        if !subtitle.isEmpty { label(subtitle, in: parent, frame: NSRect(x: 18, y: y + 27, width: 472, height: 34), size: 11, color: .secondaryLabelColor, lines: 2) }
    }

    @discardableResult
    func appearancePopup(_ title: String, values: [(String, String)], selected: String, in parent: NSView, y: CGFloat, key: String) -> NSPopUpButton {
        label(title, in: parent, frame: NSRect(x: 18, y: y + 4, width: 150, height: 22), size: 12, weight: .medium)
        let popup = NSPopUpButton(frame: NSRect(x: 176, y: y, width: 314, height: 30), pullsDown: false)
        for (id, name) in values {
            popup.addItem(withTitle: name); popup.lastItem?.representedObject = id
            if id == selected { popup.select(popup.lastItem) }
        }
        popup.identifier = NSUserInterfaceItemIdentifier(key); popup.setAccessibilityLabel(title)
        popup.target = self; popup.action = #selector(changeAppearancePopup(_:)); parent.addSubview(popup)
        return popup
    }

    func appearanceSlider(_ title: String, value: Double, minimum: Double, maximum: Double, unit: String, in parent: NSView, y: CGFloat, key: String) {
        label(title, in: parent, frame: NSRect(x: 18, y: y, width: 325, height: 21), size: 12, weight: .medium)
        let amount = label(formatAppearance(value, unit: unit), in: parent, frame: NSRect(x: 362, y: y, width: 127, height: 21), size: 11.5, color: .secondaryLabelColor)
        amount.alignment = .right; appearanceValues[key] = amount; amount.identifier = NSUserInterfaceItemIdentifier(unit)
        let control = NSSlider(value: value, minValue: minimum, maxValue: maximum, target: self, action: #selector(changeAppearanceSlider(_:)))
        control.frame = NSRect(x: 18, y: y + 29, width: 472, height: 23); control.isContinuous = true
        control.identifier = NSUserInterfaceItemIdentifier(key); control.setAccessibilityLabel(title); parent.addSubview(control)
    }
    func formatAppearance(_ value: Double, unit: String) -> String {
        if unit == "%" { return "\(Int((value * 100).rounded()))%" }
        if unit == "×" { return String(format: "%.2f ×", value) }
        return String(format: "%.0f %@", value, unit)
    }

    func quickChoices(_ category: CuePresetCategory, selected: String) -> [(String, String)] {
        var values = Array(category.filtered(tags: [category.defaultTag]).prefix(6))
        if selected != "random", !values.contains(where: { $0.id == selected }), let item = category.entries.first(where: { $0.id == selected }) { values.append(item) }
        return [("random", "随机（按分类）")] + values.map { ($0.id, $0.name) }
    }
    func edgeEditor(in view: NSView) {
        let style = cue.settings.visualStyle(for: selectedKind)
        editorLabel("光影跑马灯", "有层次的环屏流光；支持顺时针、逆时针和往返。", in: view, y: 18)
        _ = appearancePopup("光影材质", values: quickChoices(.effect, selected: cue.settings.eventStyles[selectedKind]?["effect"] ?? style.effectID), selected: cue.settings.eventStyles[selectedKind]?["effect"] == "random" ? "random" : style.effectID, in: view, y: 89, key: "effect")
        _ = button("浏览光效分类…", in: view, frame: NSRect(x: 174, y: 131, width: 314, height: 30), action: #selector(openEffectGallery))
        _ = appearancePopup("存在感", values: [("gentle", "柔和"), ("clear", "清晰"), ("strong", "醒目")], selected: style.presence.rawValue, in: view, y: 184, key: "presence")
        editorLabel("多色调色板", "从单色到八色自由搭配，颜色会参与流动与渐变。", in: view, y: 243)
        let colors = style.paletteHex
        for (index, hex) in colors.enumerated() {
            let well = NSColorWell(frame: NSRect(x: 18 + index * 48, y: 307, width: 41, height: 31))
            well.color = NSColor.cueColor(hex: hex) ?? .systemTeal; well.tag = index
            well.identifier = NSUserInterfaceItemIdentifier("palette.\(index)"); well.setAccessibilityLabel("调色板颜色 \(index + 1)")
            well.target = self; well.action = #selector(changePaletteColor(_:)); view.addSubview(well)
        }
        let add = button("＋", in: view, frame: NSRect(x: 411, y: 307, width: 36, height: 30), action: #selector(addPaletteColor)); add.isEnabled = colors.count < 8
        let remove = button("−", in: view, frame: NSRect(x: 453, y: 307, width: 36, height: 30), action: #selector(removePaletteColor)); remove.isEnabled = colors.count > 1
        for (index, item) in [("彩虹", "rainbow"), ("霓虹", "neon"), ("极光", "aurora"), ("火焰", "fire"), ("预设配色", "preset")].enumerated() {
            _ = button(item.0, in: view, frame: NSRect(x: 15 + index * 96, y: 361, width: 91, height: 29), action: #selector(applyPalettePreset(_:)), represented: item.1)
        }
        let more = NSButton(title: advancedEffectsExpanded ? "收起更多调整" : "更多调整…", target: self, action: #selector(toggleAdvancedEffects))
        more.bezelStyle = .rounded; more.frame = NSRect(x: 18, y: 412, width: 472, height: 30); more.font = .systemFont(ofSize: 12)
        view.addSubview(more)
        guard advancedEffectsExpanded else { return }
        _ = appearancePopup("运动方向", values: [("clockwise", "顺时针"), ("counterclockwise", "逆时针"), ("pingpong", "往返流动")], selected: style.direction, in: view, y: 456, key: "direction")
        appearanceSlider("运动速度", value: style.speed, minimum: 0.15, maximum: 4, unit: "×", in: view, y: 513, key: "speed")
        appearanceSlider("边缘宽度", value: Double(style.width), minimum: 2, maximum: 80, unit: "pt", in: view, y: 589, key: "width")
        appearanceSlider("特效不透明度", value: style.opacity, minimum: 0.1, maximum: 1, unit: "%", in: view, y: 665, key: "opacity")
        appearanceSlider("距离屏幕边缘", value: Double(style.inset), minimum: 0, maximum: 80, unit: "pt", in: view, y: 741, key: "inset")
        appearanceSlider("光迹长度", value: style.trail, minimum: 0.03, maximum: 0.65, unit: "%", in: view, y: 817, key: "trail")
        label("动态效果仅在提醒显示时运行，最高 30 帧/秒；遵循系统“减少动态效果”。", in: view, frame: NSRect(x: 18, y: 913, width: 472, height: 40), size: 11, color: .secondaryLabelColor, lines: 2)
    }

    @objc func toggleAdvancedEffects() {
        let origin = appearanceScroll?.contentView.bounds.origin ?? .zero
        advancedEffectsExpanded.toggle(); rebuild()
        if let scroll = appearanceScroll {
            let proposed = NSRect(origin: origin, size: scroll.contentView.bounds.size)
            scroll.contentView.scroll(to: scroll.contentView.constrainBoundsRect(proposed).origin)
            scroll.reflectScrolledClipView(scroll.contentView)
        }
    }

    func cardEditor(in view: NSView) {
        let style = cue.settings.cardStyle(for: selectedKind)
        editorLabel("提示框外观", "从紧凑胶囊到终端面板，每种都有独立排版。", in: view, y: 18)
        _ = appearancePopup("卡片样式", values: quickChoices(.card, selected: cue.settings.eventStyles[selectedKind]?["card_style"] ?? style.id), selected: cue.settings.eventStyles[selectedKind]?["card_style"] == "random" ? "random" : style.id, in: view, y: 89, key: "card_style")
        _ = button("浏览提示卡分类…", in: view, frame: NSRect(x: 174, y: 131, width: 314, height: 30), action: #selector(openCardGallery))
        _ = appearancePopup("显示位置", values: CueCardCatalog.positions, selected: style.position, in: view, y: 199, key: "card_position")
        appearanceSlider("提示框不透明度", value: style.opacity, minimum: 0.35, maximum: 1, unit: "%", in: view, y: 260, key: "card_opacity")
        appearanceSlider("圆角大小", value: Double(style.radius ?? 20), minimum: 0, maximum: 48, unit: "pt", in: view, y: 336, key: "card_radius")
        appearanceSlider("文字大小", value: Double(style.textScale), minimum: 0.8, maximum: 1.25, unit: "×", in: view, y: 412, key: "text_scale")
        appearanceSlider("提示框宽度", value: Double(style.width ?? CueCardCatalog.preset(id: style.id).size.width), minimum: 380, maximum: 800, unit: "pt", in: view, y: 488, key: "card_width")
        appearanceCheck("显示说明文字", on: style.showMessage, in: view, y: 573, key: "card_message")
        appearanceCheck("显示“打开对话”入口", on: style.showOpenButton, in: view, y: 615, key: "card_open")
        label("点击提示框的标题、正文或空白处打开对应对话；按钮保留各自操作。", in: view, frame: NSRect(x: 18, y: 675, width: 472, height: 40), size: 11, color: .secondaryLabelColor, lines: 2)
    }
    func appearanceCheck(_ title: String, on: Bool, in parent: NSView, y: CGFloat, key: String) {
        let control = NSButton(checkboxWithTitle: title, target: self, action: #selector(changeAppearanceCheck(_:)))
        control.state = on ? .on : .off; control.font = .systemFont(ofSize: 12)
        control.frame = NSRect(x: 18, y: y, width: 472, height: 29); control.identifier = NSUserInterfaceItemIdentifier(key)
        parent.addSubview(control)
    }

    func soundRuleEditor(in view: NSView) {
        let isPermission = selectedKind == "permission"
        let on = selectedKind == "complete" ? cue.settings.completeEnabled : selectedKind == "input" ? cue.settings.inputEnabled : cue.settings.permissionEnabled
        let detail = isPermission ? (cue.manualApprovalConnected ? "只在确实等待你人工批准时提醒，自动审查不提醒。" : "需要先接通人工批准监听；可先保存开关和自定义样式。") : (selectedKind == "complete" ? "本轮执行结束时提醒，保留你的每类样式设置。" : "需要回答或选择时提醒。")
        toggle(isPermission ? "启用人工批准提醒" : "启用这一类提醒", detail, in: view, y: 17, key: selectedKind + "_enabled", on: on)
        _ = appearancePopup("提示音", values: soundOptions(), selected: cue.settings.soundToken(for: selectedKind), in: view, y: 105, key: "sound")
        _ = button("浏览声音分类…", in: view, frame: NSRect(x: 174, y: 148, width: 314, height: 30), action: #selector(openSoundGallery))
        _ = button("选取音效文件…", in: view, frame: NSRect(x: 174, y: 193, width: 154, height: 30), action: #selector(importEventSound))
        _ = button("只试听", in: view, frame: NSRect(x: 337, y: 193, width: 152, height: 30), action: #selector(previewEventSound))
        var seconds: [Double] = [3, 5, 8, 14, 20, 30, 60]
        let duration = cue.settings.duration(selectedKind)
        if !seconds.contains(duration) { seconds.append(duration); seconds.sort() }
        _ = appearancePopup("显示时间", values: seconds.map { (String($0), "\(Int($0)) 秒") }, selected: String(duration), in: view, y: 257, key: "duration")
        label("自选音效限制 20 MB、30 秒以内。静音、暂停和全局开关始终生效。", in: view, frame: NSRect(x: 18, y: 324, width: 472, height: 40), size: 11, color: .secondaryLabelColor, lines: 2)
    }
    func soundOptions() -> [(String, String)] {
        var options = quickChoices(.sound, selected: cue.settings.soundToken(for: selectedKind)) + [("silent", "静音")]
        let selected = cue.settings.soundToken(for: selectedKind)
        if selected.hasPrefix("custom:") { options.append((selected, "自选音效（已导入）")) }
        return options
    }

    @objc func selectAppearanceTab(_ sender: NSButton) { appearanceTab = sender.tag; rebuild() }
    @objc func changeAppearancePopup(_ sender: NSPopUpButton) {
        guard let key = sender.identifier?.rawValue, let value = sender.selectedItem?.representedObject as? String else { return }
        if key == "duration", let seconds = Double(value) {
            if selectedKind == "complete" { cue.settings.completeDuration = seconds }
            else if selectedKind == "input" { cue.settings.inputDuration = seconds }
            else { cue.settings.permissionDuration = seconds }
        } else {
            cue.settings.eventStyles[selectedKind]?[key] = value
            if value == "random", ["effect", "card_style", "sound"].contains(key), cue.settings.eventStyles[selectedKind]?[key + "_random_tags"] == nil {
                cue.settings.eventStyles[selectedKind]?[key + "_random_tags"] = key == "effect" ? "recommended" : key == "card_style" ? "native" : "synthesized"
            }
        }
        cue.applySettings(updateCurrent: false)
        if key == "card_style" || key == "effect" || key == "sound" || key == "presence" {
            rebuild()
            _ = cue.preview(kind: selectedKind, component: key == "card_style" ? "card" : key == "presence" ? "effect" : key)
        }
    }
    @objc func changeAppearanceSlider(_ sender: NSSlider) {
        guard let key = sender.identifier?.rawValue else { return }
        cue.settings.eventStyles[selectedKind]?[key] = String(format: "%.4f", sender.doubleValue)
        let unit = appearanceValues[key]?.identifier?.rawValue ?? ""
        appearanceValues[key]?.stringValue = formatAppearance(sender.doubleValue, unit: unit)
        cue.applySettings()
    }
    @objc func changeAppearanceCheck(_ sender: NSButton) {
        guard let key = sender.identifier?.rawValue else { return }
        cue.settings.eventStyles[selectedKind]?[key] = sender.state == .on ? "true" : "false"; cue.applySettings()
    }
    func setPalette(_ colors: [String]) {
        let valid = colors.filter { NSColor.cueColor(hex: $0) != nil }.prefix(8)
        guard let first = valid.first else { return }
        cue.settings.eventStyles[selectedKind]?["palette"] = valid.joined(separator: ",")
        cue.settings.eventStyles[selectedKind]?["color"] = first
        cue.applySettings()
    }
    @objc func changePaletteColor(_ sender: NSColorWell) {
        var colors = cue.settings.visualStyle(for: selectedKind).paletteHex
        guard colors.indices.contains(sender.tag) else { return }
        colors[sender.tag] = sender.color.cueHex; setPalette(colors)
    }
    @objc func addPaletteColor() {
        var colors = cue.settings.visualStyle(for: selectedKind).paletteHex
        guard colors.count < 8 else { return }
        let accents = ["#5CE1E6", "#997BFF", "#FF6FB1", "#FFD166", "#3BE8AB", "#167DCE", "#FF7A61", "#FFFFFF"]
        colors.append(accents[colors.count % accents.count]); setPalette(colors); rebuild()
    }
    @objc func removePaletteColor() {
        var colors = cue.settings.visualStyle(for: selectedKind).paletteHex
        guard colors.count > 1 else { return }; colors.removeLast(); setPalette(colors); rebuild()
    }
    @objc func applyPalettePreset(_ sender: NSButton) {
        let colors: [String]
        switch sender.identifier?.rawValue {
        case "rainbow": colors = ["#FF5E7E", "#FFBE0B", "#7CE38B", "#38CFFF", "#9A7DFF", "#F46DDA"]
        case "neon": colors = ["#3CEFFF", "#EC55FF", "#FFF657"]
        case "aurora": colors = ["#3BE8AB", "#4EC5FF", "#9A7DFF"]
        case "fire": colors = ["#FF4D4D", "#FFAD33", "#FFF4B0"]
        default: colors = CueEffectCatalog.preset(id: cue.settings.visualStyle(for: selectedKind).effectID).defaultPalette
        }
        setPalette(colors); rebuild()
    }
    @objc func openEffectGallery() { openPresetBrowser(.effect) }
    @objc func openCardGallery() { openPresetBrowser(.card) }
    @objc func openSoundGallery() { openPresetBrowser(.sound) }
    func openPresetBrowser(_ category: CuePresetCategory) {
        presetBrowser?.close()
        let kind = selectedKind
        let fields = cue.settings.eventStyles[kind] ?? [:]
        let id = fields[category.settingKey] == "random" ? "random" : category == .effect ? cue.settings.visualStyle(for: kind).effectID : category == .card ? cue.settings.cardStyle(for: kind).id : cue.settings.soundToken(for: kind)
        let eventName = kind == "complete" ? "本轮完成" : kind == "input" ? "等待输入" : "人工批准"
        presetBrowser = CuePresetBrowserController(category: category, fields: fields, color: cue.settings.color(for: kind), selectedID: id, eventName: eventName, windowOpacity: cue.settings.glowIntensity, shouldAnimate: { [weak self] in
            self?.cue.settings.enabled == true && self?.cue.pausedUntil == nil && self?.cue.current == nil
        }) { [weak self] id, tags in
            guard let self else { return ("", nil) }
            self.selectedKind = kind
            self.cue.settings.eventStyles[kind]?[category.settingKey] = id
            if id == "random" { self.cue.settings.eventStyles[kind]?[category.settingKey + "_random_tags"] = tags.filter { $0 != "all" }.joined(separator: ",") }
            self.cue.applySettings(updateCurrent: false); self.rebuild()
            self.presetBrowser?.fields = self.cue.settings.eventStyles[kind] ?? [:]
            self.presetBrowser?.accent = self.cue.settings.color(for: kind)
            self.presetBrowser?.windowOpacity = self.cue.settings.glowIntensity
            let allowed = self.cue.settings.enabled && self.cue.pausedUntil == nil && (self.cue.current == nil || self.cue.current?.isPreview == true) && !self.cue.pending.contains { !$0.isPreview }
            let feedback = self.cue.preview(kind: kind, component: category.rawValue)
            let resolved = category == .sound ? self.cue.lastPlayedSoundToken : self.cue.resolvedFields[category.settingKey]
            return (feedback, allowed ? resolved : nil)
        }
        presetBrowser?.showWindow(nil)
    }

    func glowAndSound() {
        heading("泛光与声音", "调整提醒的存在感，保留你的工作节奏。")
        let glow = card(106, 204)
        toggle("屏幕边缘泛光", "提示卡仍会显示对话名称。", in: glow, y: 16, key: "glow_enabled", on: cue.settings.glowEnabled)
        toggle("在所有屏幕显示泛光", "关闭后只使用鼠标所在的屏幕。", in: glow, y: 78, key: "all_displays", on: cue.settings.allDisplays)
        slider("泛光亮度", in: glow, y: 140, key: "glow_intensity", value: cue.settings.glowIntensity, min: 0.15)
        let audio = card(325, 212)
        toggle("播放提示音", "使用一般媒体输出，不修改系统音量或输出设备。", in: audio, y: 16, key: "sound_enabled", on: cue.settings.soundEnabled)
        slider("提示音音量", in: audio, y: 82, key: "sound_volume", value: cue.settings.soundVolume)
        for (index, pair) in [("完成音", "complete"), ("等待输入音", "input"), ("人工批准测试音", "permission")].enumerated() {
            let test = button("▶ \(pair.0)", in: audio, frame: NSRect(x: 17 + index * 164, y: 157, width: 158, height: 30), action: #selector(testAudio(_:)), represented: pair.1)
            test.isEnabled = cue.settings.soundEnabled
        }
    }

    func providers() {
        heading("集成与文件", "AI Coding Companion · 目前已接入 Codex。")
        let provider = card(106, 294)
        label("Codex", in: provider, frame: NSRect(x: 20, y: 17, width: 486, height: 27), size: 18, weight: .semibold)
        healthLabel = label("", in: provider, frame: NSRect(x: 20, y: 53, width: 486, height: 21), size: 12, weight: .semibold)
        label("完成和提问由本机记录监控。人工批准通过独立的只读监听接入；只监听交给你的请求，自动审查不会触发提醒，批准后会撤下提示。", in: provider, frame: NSRect(x: 20, y: 87, width: 486, height: 67), size: 11.5, color: .secondaryLabelColor, lines: 4)
        hookLabel = label("", in: provider, frame: NSRect(x: 20, y: 158, width: 486, height: 21), size: 11, color: .secondaryLabelColor)
        let connect = button(cue.manualApprovalConnected ? "重新连接人工批准监听…" : "接通人工批准监听…", in: provider, frame: NSRect(x: 16, y: 194, width: 244, height: 31), action: #selector(connectManualApproval))
        connect.isEnabled = cue.approvalLauncher?.isRunning != true
        _ = button("授权读取本机记录…", in: provider, frame: NSRect(x: 269, y: 194, width: 238, height: 31), action: #selector(authorizeHistoryRead))
        label("首次接通需要在当前任务结束后自行退出并重新打开 Codex。不会强制退出或更改批准策略。", in: provider, frame: NSRect(x: 20, y: 239, width: 486, height: 42), size: 10.5, color: .tertiaryLabelColor, lines: 3)
        let files = card(418, 126)
        label("本机文件", in: files, frame: NSRect(x: 20, y: 15, width: 480, height: 22), size: 13, weight: .semibold)
        label(cue.home.path, in: files, frame: NSRect(x: 20, y: 43, width: 486, height: 27), size: 10.5, color: .secondaryLabelColor, lines: 2)
        _ = button("源码文件夹", in: files, frame: NSRect(x: 16, y: 81, width: 160, height: 31), action: #selector(openSource))
        _ = button("设置文件夹", in: files, frame: NSRect(x: 181, y: 81, width: 160, height: 31), action: #selector(openConfig))
        _ = button("诊断日志", in: files, frame: NSRect(x: 346, y: 81, width: 160, height: 31), action: #selector(openLogs))
    }

    func updateHealth() {
        healthLabel?.stringValue = cue.watcherHealthText
        healthLabel?.textColor = cue.watcherHealthText.hasPrefix("本机监控中") ? NSColor(srgbRed: 0.5, green: 0.94, blue: 0.76, alpha: 1) : .systemOrange
        let pause = cue.pausedUntil == nil ? (cue.settings.enabled ? "提醒已启用" : "提醒已关闭") : "提醒已暂停"
        threadLabel?.stringValue = "\(pause)  ·  \(cue.settings.soundEnabled ? "声音已启用" : "声音已关闭")"
        latestLabel?.stringValue = cue.recent.first.map { "\($0.label) · \($0.title)" } ?? "尚无提醒。可以先测试一则提醒效果。"
        hookLabel?.stringValue = cue.manualApprovalStatus
    }

    @objc func changeToggle(_ sender: NSSwitch) {
        let value = sender.state == .on
        switch sender.identifier?.rawValue {
        case "enabled": cue.settings.enabled = value
        case "complete_enabled": cue.settings.completeEnabled = value
        case "input_enabled": cue.settings.inputEnabled = value
        case "permission_enabled": cue.settings.permissionEnabled = value
        case "glow_enabled": cue.settings.glowEnabled = value
        case "all_displays": cue.settings.allDisplays = value
        case "sound_enabled": cue.settings.soundEnabled = value
        default: return
        }
        cue.applySettings()
        if sender.identifier?.rawValue == "sound_enabled" { rebuild() }
        updateHealth()
    }
    @objc func changeSlider(_ sender: NSSlider) {
        let key = sender.identifier?.rawValue ?? ""
        if key == "glow_intensity" { cue.settings.glowIntensity = sender.doubleValue }
        if key == "sound_volume" { cue.settings.soundVolume = sender.doubleValue }
        sliderLabels[key]?.stringValue = "\(Int((sender.doubleValue * 100).rounded()))%"
        cue.applySettings()
    }
    @objc func changeDuration(_ sender: NSPopUpButton) {
        guard let duration = sender.selectedItem?.representedObject as? Double else { return }
        switch sender.identifier?.rawValue {
        case "complete_duration": cue.settings.completeDuration = duration
        case "input_duration": cue.settings.inputDuration = duration
        case "permission_duration": cue.settings.permissionDuration = duration
        default: return
        }
        cue.applySettings()
    }
    @objc func selectEventKind(_ sender: NSPopUpButton) {
        guard let kind = sender.selectedItem?.representedObject as? String else { return }
        selectedKind = kind; rebuild()
    }
    @objc func changeEventColor(_ sender: NSColorWell) {
        cue.settings.eventStyles[selectedKind]?["color"] = sender.color.cueHex
        cue.applySettings()
    }
    @objc func changeEventSound(_ sender: NSPopUpButton) {
        guard let token = sender.selectedItem?.representedObject as? String, CueSettings.validSoundToken(token) else { return }
        cue.settings.eventStyles[selectedKind]?["sound"] = token
        cue.applySettings()
    }
    @objc func importEventSound() {
        let panel = NSOpenPanel()
        panel.title = "选取这种提醒的音效"; panel.prompt = "导入音效"
        panel.canChooseDirectories = false; panel.canChooseFiles = true; panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.audio]
        guard panel.runModal() == .OK, let source = panel.url else { return }
        let access = source.startAccessingSecurityScopedResource()
        defer { if access { source.stopAccessingSecurityScopedResource() } }
        do {
            let properties = try source.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey])
            guard properties.isRegularFile == true, let size = properties.fileSize, size <= 20 * 1024 * 1024 else {
                throw NSError(domain: "VibeCueSoundImport", code: 1, userInfo: [NSLocalizedDescriptionKey: "请选择 20 MB 以内的音效文件。"])
            }
            let player = try AVAudioPlayer(contentsOf: source)
            guard player.duration > 0, player.duration <= 30 else {
                throw NSError(domain: "VibeCueSoundImport", code: 2, userInfo: [NSLocalizedDescriptionKey: "提醒音效需在 30 秒以内。"])
            }
            let directory = cue.home.appendingPathComponent("sounds/custom", isDirectory: true)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            let filename = UUID().uuidString + "." + source.pathExtension.lowercased()
            let destination = directory.appendingPathComponent(filename)
            try FileManager.default.copyItem(at: source, to: destination)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: destination.path)
            cue.settings.eventStyles[selectedKind]?["sound"] = "custom:" + filename
            cue.applySettings(updateCurrent: false); rebuild()
            _ = cue.preview(kind: selectedKind, component: "sound")
        } catch {
            let alert = NSAlert(); alert.messageText = "无法导入音效"; alert.informativeText = error.localizedDescription
            alert.addButton(withTitle: "知道了"); alert.runModal()
        }
    }
    @objc func previewEventSound() {
        _ = cue.preview(kind: selectedKind, component: "sound")
    }
    @objc func previewEvent() {
        _ = cue.preview(kind: selectedKind, component: "all")
    }
    @objc func resetEventStyle() {
        cue.settings.eventStyles[selectedKind] = CueSettings.defaultStyles[selectedKind]
        switch selectedKind {
        case "complete": cue.settings.completeDuration = 8
        case "input": cue.settings.inputDuration = 14
        default: cue.settings.permissionDuration = 14
        }
        cue.applySettings(); rebuild()
    }
    @objc func testAlert(_ sender: NSButton) {
        let kind = sender.identifier?.rawValue ?? "complete"
        cue.enqueue(CueEvent(kind: kind, title: "VibeCue · 提醒效果测试", message: "使用你目前的泛光、声音与显示时间设置。"))
        updateHealth()
    }
    @objc func testAudio(_ sender: NSButton) {
        let kind = sender.identifier?.rawValue ?? "input"
        _ = cue.preview(kind: kind, component: "sound")
    }
    @objc func openSource() {
        let data = try? Data(contentsOf: cue.home.appendingPathComponent("install.json"))
        let record = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        let path = record?["source_dir"] as? String
        let url = path.map { URL(fileURLWithPath: $0, isDirectory: true) } ?? Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent()
        NSWorkspace.shared.open(url)
    }
    @objc func openConfig() { NSWorkspace.shared.open(cue.home) }
    @objc func authorizeHistoryRead() { cue.authorizeHistoryRead() }
    @objc func connectManualApproval() { cue.connectManualApproval() }
    @objc func openLogs() {
        let logs = cue.home.appendingPathComponent("logs", isDirectory: true)
        try? FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        NSWorkspace.shared.open(logs)
    }
    func windowWillClose(_ notification: Notification) { DispatchQueue.main.async { self.cue.writeStatus() } }
    func capture(to path: String) {
        root.layoutSubtreeIfNeeded()
        guard let rep = root.bitmapImageRepForCachingDisplay(in: root.bounds) else { return }
        root.cacheDisplay(in: root.bounds, to: rep)
        if let data = rep.representation(using: .png, properties: [:]) { try? data.write(to: URL(fileURLWithPath: path)) }
    }
}

if CommandLine.arguments.contains("--recent-self-test") {
    _ = NSApplication.shared
    let home = FileManager.default.temporaryDirectory.appendingPathComponent("vibecue-recent-integration-" + UUID().uuidString)
    setenv("VIBECUE_HOME", home.path, 1)
    defer { try? FileManager.default.removeItem(at: home) }
    let cue = CueController(); cue.presentsWindows = false; cue.settings.enabled = false
    var results = CueRecentHistory.validationReport()
    results.merge(CueStatusPopoverController.validationReport()) { _, new in new }
    results.merge(CueCodexBatchController.validationReport()) { _, new in new }
    results.merge(CueTaskHoverController.validationReport()) { _, new in new }
    func event(_ id: String) -> CueEvent {
        CueEvent(try! JSONSerialization.data(withJSONObject: ["id": id, "kind": "complete", "thread_id": "fixture-chat", "title": "验收样例 · " + id, "created_at": Date().timeIntervalSince1970]))!
    }
    cue.enqueue(event("first")); cue.enqueue(event("second"))
    cue.openConversationURL = { _ in false }
    results["rejected_url_handoff_stays_unopened"] = !cue.open(event: event("first")) && cue.recentHistory.unopenedCount == 2
    cue.openConversationURL = { _ in true }
    results["successful_handoff_marks_only_its_event"] = cue.open(event: event("first")) && cue.recentHistory.unopenedCount == 1 && cue.recentHistory.records.first(where: { $0.event.id == "second" })?.isOpened == false
    let inputRaw: [String: Any] = ["id":"mcp-input-fixture", "kind":"input", "thread_id":"fixture-chat", "title":"浏览器确认样例", "source":"codex client approval stream", "created_at":Date().timeIntervalSince1970]
    let inputEvent = CueEvent(try! JSONSerialization.data(withJSONObject: inputRaw))!
    cue.current = inputEvent; cue.pending = [inputEvent]; cue.recentHistory.append(inputEvent)
    let inputResolution = CueResolution(try! JSONSerialization.data(withJSONObject: ["action":"resolve", "target_id":inputEvent.id, "source":"codex client approval stream", "created_at":Date().timeIntervalSince1970]))!
    cue.applyResolution(inputResolution, resume: false)
    results["resolved_mcp_input_dismisses_visible_and_queued_card"] = cue.current == nil && cue.pending.isEmpty && cue.recentHistory.records.first(where: { $0.event.id == inputEvent.id })?.resolved == true && cue.recentHistory.records.first(where: { $0.event.id == inputEvent.id })?.isOpened == false
    if let index = CommandLine.arguments.firstIndex(of: "--capture-status-panel"), CommandLine.arguments.indices.contains(index + 1) {
        results["status_panel_three_layer_fixture"] = CueStatusPopoverController.captureFixture(to: URL(fileURLWithPath: CommandLine.arguments[index + 1]))
    }
    if let index = CommandLine.arguments.firstIndex(of: "--capture-batch-panel"), CommandLine.arguments.indices.contains(index + 1) {
        results["batch_panel_fixture"] = CueStatusPopoverController.captureFixture(to: URL(fileURLWithPath: CommandLine.arguments[index + 1]), batchMode: true)
    }
    if let index = CommandLine.arguments.firstIndex(of: "--capture-batch-dialog"), CommandLine.arguments.indices.contains(index + 1) {
        results["batch_dialog_fixture"] = CueCodexBatchController.captureFixture(to: URL(fileURLWithPath: CommandLine.arguments[index + 1]))
    }
    print(String(data: try! JSONSerialization.data(withJSONObject: results, options: [.sortedKeys]), encoding: .utf8)!)
    exit(results.values.allSatisfy { $0 } ? 0 : 1)
}

if let index = CommandLine.arguments.firstIndex(of: "--material-preview-contact-sheet"), CommandLine.arguments.indices.contains(index + 1) {
    _ = NSApplication.shared
    do {
        let results = try CueMaterialPreviewView.renderContactSheet(to: URL(fileURLWithPath: CommandLine.arguments[index + 1]))
        print(String(data: try! JSONSerialization.data(withJSONObject: results, options: [.sortedKeys]), encoding: .utf8)!)
        exit(results.values.allSatisfy { $0 } ? 0 : 1)
    } catch { fputs("VibeCue: material detail capture failed\n", stderr); exit(1) }
}

if CommandLine.arguments.contains("--window-preview-self-test") {
    let home = FileManager.default.temporaryDirectory.appendingPathComponent("vibecue-window-preview-\(UUID().uuidString)")
    try! FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
    setenv("VIBECUE_HOME", home.path, 1)
    let application = NSApplication.shared
    application.setActivationPolicy(.accessory)
    let cue = CueController()
    cue.settings.soundEnabled = false
    cue.settings.eventStyles["complete"]?["effect"] = "luminous-flow"
    var results: [String: Bool] = [:]
    DispatchQueue.main.async {
        _ = cue.preview(kind: "complete", component: "effect")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) {
            results["edge_panels_visible_on_all_screens"] = cue.edges.count == NSScreen.screens.count && !cue.edges.isEmpty && cue.edges.allSatisfy(\.isVisible)
            results["edge_panels_above_menu_and_dock"] = cue.edges.allSatisfy { $0.level.rawValue > NSWindow.Level.statusBar.rawValue && $0.level.rawValue < NSWindow.Level.screenSaver.rawValue }
            results["edge_panels_cover_screen_frames"] = zip(cue.edges, NSScreen.screens).allSatisfy { $0.frame == $1.frame }
            results["edge_clock_advances_after_fade_in"] = cue.effectFrameRate > 0 && cue.edgeViews.allSatisfy { $0.elapsed > 0.4 }
            results["lighting_material_actually_renders"] = cue.edgeViews.allSatisfy { $0.renderedFrameCount > 1 && $0.lastRenderDuration > 0 }
            results["recommended_line_has_readable_width"] = CueEffectCatalog.preset(id: "luminous-flow").recipe.layers.contains { $0.minimumWidth >= 1 }
            results["six_second_preview_is_active"] = CueController.previewDuration == 6 && cue.current?.isPreview == true && cue.hideTimer?.isValid == true
            _ = cue.preview(kind: "complete", component: "card")
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
                results["card_preview_visible_after_fade_in"] = cue.banner?.isVisible == true && (cue.banner?.alphaValue ?? 0) > 0.95
                results["card_preview_has_no_edge_windows"] = cue.edges.isEmpty
                cue.shuttingDown = true; cue.hideWindows(); cue.current = nil
                try? FileManager.default.removeItem(at: home)
                print(String(data: try! JSONSerialization.data(withJSONObject: results, options: [.sortedKeys]), encoding: .utf8)!)
                exit(results.values.allSatisfy { $0 } ? 0 : 1)
            }
        }
    }
    application.run()
    exit(1)
}

if CommandLine.arguments.contains("--appearance-self-test") {
    var settings = CueSettings()
    settings.eventStyles["input"] = ["color": "#123456", "sound": "silent", "effect": CueEffectCatalog.presets[12].id, "palette": "#F05A90,#44DDFF,#FCDD55", "direction": "counterclockwise", "speed": "2.4", "width": "35", "opacity": "0.65", "inset": "18", "trail": "0.3", "card_style": "terminal", "card_position": "bottom-right", "card_opacity": "0.82", "text_scale": "1.1", "card_message": "false"]
    let data = try! JSONSerialization.data(withJSONObject: settings.json)
    let restored = CueSettings(json: try! JSONSerialization.jsonObject(with: data) as! [String: Any])
    let visual = restored.visualStyle(for: "input"), card = restored.cardStyle(for: "input")
    var results = CueEffectCatalog.validationReport()
    results.merge(CueCardCatalog.validationReport()) { _, new in new }
    results.merge(CueSoundCatalog.validationReport()) { _, new in new }
    results.merge(CueResolvedSelection.validationReport()) { _, new in new }
    results["palette_direction_roundtrip"] = visual.effectID == CueEffectCatalog.presets[12].id && visual.paletteHex == ["#F05A90", "#44DDFF", "#FCDD55"] && visual.direction == "counterclockwise" && visual.speed == 2.4
    results["card_layout_roundtrip"] = card.id == "terminal" && card.position == "bottom-right" && card.opacity == 0.82 && card.textScale == 1.1 && !card.showMessage
    results["legacy_single_color_and_sound"] = CueSettings(json: ["event_styles": ["input": ["color": "#167DCE", "sound": "silent"]]]).visualStyle(for: "input").paletteHex == ["#167DCE"]
    if let json = try? JSONSerialization.data(withJSONObject: results, options: [.sortedKeys]), let output = String(data: json, encoding: .utf8) { print(output) }
    exit(results.values.allSatisfy { $0 } ? 0 : 1)
}

if CommandLine.arguments.contains("--preview-self-test") {
    _ = NSApplication.shared
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("vibecue-preview-\(UUID().uuidString)")
    try! FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    setenv("VIBECUE_HOME", directory.path, 1)
    let cue = CueController(); cue.presentsWindows = false; cue.settings.soundEnabled = false
    cue.statusItem = NSStatusBar.system.statusItem(withLength: 0)
    var results: [String: Bool] = [:]
    for index in 0..<100 {
        cue.settings.eventStyles["input"]?["card_style"] = CueCardCatalog.presets[index % CueCardCatalog.presets.count].id
        _ = cue.preview(kind: "input", component: "card")
    }
    let oldTimer = cue.hideTimer
    results["rapid_selection_replaces_without_queue_or_history"] = cue.current?.isPreview == true && cue.pending.isEmpty && cue.recent.isEmpty && cue.seen.isEmpty
    let raw: [String: Any] = ["id": "real-approval", "kind": "permission", "thread_id": "test-chat", "source": "codex client approval stream", "requires_user_action": true, "created_at": Date().timeIntervalSince1970]
    let real = CueEvent(try! JSONSerialization.data(withJSONObject: raw))!
    cue.enqueue(real)
    results["real_event_preempts_preview"] = cue.current?.id == real.id && cue.recent.count == 1 && cue.seen.count == 1
    let shownAt = cue.shownAt
    _ = cue.preview(kind: "complete", component: "sound")
    cue.applySettings(updateCurrent: false)
    results["real_event_blocks_preview_and_keeps_deadline"] = cue.current?.id == real.id && cue.shownAt == shownAt && !cue.lastSoundPlayStarted
    oldTimer?.fire()
    results["old_preview_timer_cannot_hide_real_event"] = cue.current?.id == real.id
    cue.hideWindows(); cue.current = nil; cue.pending = [real]
    _ = cue.preview(kind: "complete", component: "card")
    results["queued_real_event_blocks_preview"] = cue.current == nil && cue.pending.map(\.id) == [real.id]
    cue.pending = []; cue.settings.enabled = false
    _ = cue.preview(kind: "complete", component: "card")
    results["global_disable_blocks_preview"] = cue.current == nil
    cue.settings.enabled = true; cue.pausedUntil = Date().addingTimeInterval(60)
    _ = cue.preview(kind: "complete", component: "card")
    results["pause_blocks_preview"] = cue.current == nil
    cue.pausedUntil = nil; _ = cue.preview(kind: "complete", component: "sound")
    results["mute_blocks_sound_preview"] = !cue.lastSoundPlayStarted && cue.audioPlayer == nil
    results["all_three_browsers_expose_unique_tagged_catalogs"] = [CuePresetCategory.effect, .card, .sound].allSatisfy { $0.entries.count >= 50 && Set($0.entries.map(\.id)).count == $0.entries.count && $0.tags.count >= 4 }
    results["all_three_browsers_offer_extra_random_choice"] = [CuePresetCategory.effect, .card, .sound].allSatisfy { $0.choices.count == $0.entries.count + 1 && $0.choices.first?.id == "random" }
    for key in ["effect", "card_style", "sound"] { cue.settings.eventStyles["input"]?[key] = "random" }
    let restored = CueSettings(json: cue.settings.json)
    results["random_settings_survive_restart"] = ["effect", "card_style", "sound"].allSatisfy { restored.eventStyles["input"]?[$0] == "random" }
    _ = cue.preview(kind: "input", component: "all")
    let selected = cue.resolvedFields
    if let event = cue.current { cue.makeWindows(for: event); cue.makeWindows(for: event) }
    cue.applySettings(updateCurrent: false)
    results["random_selection_stays_stable_during_one_preview"] = cue.resolvedFields == selected && cue.lastPlayedSoundToken == selected["sound"]
    results["random_snapshot_uses_real_catalog_ids"] = CueEffectCatalog.presets.contains { $0.id == selected["effect"] } && CueCardCatalog.presets.contains { $0.id == selected["card_style"] } && CueSoundCatalog.contains(id: selected["sound"] ?? "")
    results["multi_tag_filter_unions_sources"] = CuePresetCategory.sound.filtered(tags: ["nature", "urban"]).count == 8
    results["tag_search_filters_visible_names"] = CuePresetCategory.sound.filtered(tags: ["nature"], search: "雨").map(\.id) == ["nature-rain"]
    results["recommended_filter_excludes_classic"] = CuePresetCategory.effect.filtered(tags: ["recommended"]).count == CueEffectCatalog.recommendedPresets.count && CuePresetCategory.card.filtered(tags: ["native"]).count == 3
    var oldLook = CueSettings(json: ["sound_enabled": false, "sound_volume": 0.3, "event_styles": ["input": ["effect": "random", "sound": "nature-rain", "color": "#123456"]]])
    oldLook.useNativeRecommendedLook()
    results["look_migration_preserves_sound_colors_and_controls"] = !oldLook.soundEnabled && oldLook.soundVolume == 0.3 && oldLook.eventStyles["input"]?["sound"] == "nature-rain" && oldLook.eventStyles["input"]?["color"] == "#123456" && oldLook.eventStyles["input"]?["effect"] == "random" && oldLook.eventStyles["input"]?["effect_random_tags"] == "lighting"
    var secondGeneration = CueSettings(json: ["design_generation": 2, "sound_enabled": false, "event_styles": ["complete": ["effect": "dash-circuit", "card_style": "frosted-glass", "card_width": "620", "sound": "random", "sound_random_tags": "nature,urban", "palette": "#123456,#ABCDEF", "direction": "counterclockwise"]]])
    secondGeneration.useNativeRecommendedLook()
    let upgraded = secondGeneration.eventStyles["complete"] ?? [:]
    results["lighting_upgrade_preserves_cards_audio_palette_direction"] = secondGeneration.designGeneration == 3 && !secondGeneration.soundEnabled && upgraded["effect"] == "luminous-flow" && upgraded["card_style"] == "frosted-glass" && upgraded["card_width"] == "620" && upgraded["sound"] == "random" && upgraded["sound_random_tags"] == "nature,urban" && upgraded["palette"] == "#123456,#ABCDEF" && upgraded["direction"] == "counterclockwise"
    let strongPresence = CueSettings(json: ["event_styles": ["input": ["presence": "strong"]]])
    results["presence_setting_survives_restart"] = CueSettings(json: strongPresence.json).eventStyles["input"]?["presence"] == "strong"

    let scoped = CueSettings(json: ["event_styles": ["input": ["effect": "random", "effect_random_tags": "recommended", "card_style": "random", "card_style_random_tags": "native", "sound": "random", "sound_random_tags": "nature,urban"]]])
    let scopedRestored = CueSettings(json: scoped.json)
    results["random_tag_scopes_survive_restart"] = ["effect_random_tags", "card_style_random_tags", "sound_random_tags"].allSatisfy { scopedRestored.eventStyles["input"]?[$0] == scoped.eventStyles["input"]?[$0] }
    cue.hideWindows(); cue.current = nil
    cue.settings.eventStyles["input"]?["sound"] = "random"; cue.settings.eventStyles["input"]?["sound_random_tags"] = "nature"
    var soundScopeValid = true
    for _ in 0..<12 {
        _ = cue.preview(kind: "input", component: "sound")
        soundScopeValid = soundScopeValid && cue.lastPlayedSoundToken.hasPrefix("nature-")
    }
    results["sound_only_random_preview_respects_saved_scope"] = soundScopeValid
    let browser = CuePresetBrowserController(category: .sound, fields: ["sound": "random", "sound_random_tags": "nature,urban"], color: .systemBlue, selectedID: "random", eventName: "自检", shouldAnimate: { false }, onSelect: { _, _ in ("", nil) })
    results["browser_reopens_saved_multi_tag_scope"] = browser.selectedTags == Set(["nature", "urban"])
    browser.close()
    cue.settings.glowEnabled = false
    _ = cue.preview(kind: "input", component: "effect")
    results["disabled_edge_preview_reports_reason"] = cue.current == nil && cue.previewFeedback.contains("屏幕光效已关闭")
    cue.hideWindows(); NSStatusBar.system.removeStatusItem(cue.statusItem)
    try? FileManager.default.removeItem(at: directory)
    print(String(data: try! JSONSerialization.data(withJSONObject: results, options: [.sortedKeys]), encoding: .utf8)!)
    exit(results.values.allSatisfy { $0 } ? 0 : 1)
}

if CommandLine.arguments.contains("--resolution-self-test") {
    let raw: [String: Any] = ["action": "resolve", "target_id": "same-batch-request", "source": "codex client approval stream", "created_at": Date().timeIntervalSince1970]
    let resolution = CueResolution(try! JSONSerialization.data(withJSONObject: raw))
    var tracker = CueResolutionTracker()
    if let resolution { tracker.insert(resolution.targetID) }
    var invalid = raw; invalid["source"] = "PermissionRequest hook"
    let results: [String: Bool] = ["confirmed_control_accepted": resolution != nil, "same_batch_show_filtered": tracker.contains("same-batch-request"), "other_request_preserved": !tracker.contains("other-request"), "raw_hook_control_ignored": CueResolution(try! JSONSerialization.data(withJSONObject: invalid)) == nil]
    if let json = try? JSONSerialization.data(withJSONObject: results, options: [.sortedKeys]), let output = String(data: json, encoding: .utf8) { print(output) }
    exit(results.values.allSatisfy { $0 } ? 0 : 1)
}

if let index = CommandLine.arguments.firstIndex(of: "--effects-contact-sheet"), CommandLine.arguments.indices.contains(index + 1) {
    _ = NSApplication.shared
    do { try CueEffectCatalog.renderContactSheet(to: URL(fileURLWithPath: CommandLine.arguments[index + 1])); exit(0) }
    catch { fputs("VibeCue: effect capture failed\n", stderr); exit(1) }
}
if let index = CommandLine.arguments.firstIndex(of: "--cards-contact-sheet"), CommandLine.arguments.indices.contains(index + 1) {
    _ = NSApplication.shared
    do { try CueCardCatalog.renderContactSheet(to: URL(fileURLWithPath: CommandLine.arguments[index + 1])); exit(0) }
    catch { fputs("VibeCue: card capture failed\n", stderr); exit(1) }
}

if CommandLine.arguments.contains("--customization-self-test") {
    let legacy = CueSettings(json: ["input_duration": 20.0, "sound_volume": 0.4])
    var customized = legacy
    customized.eventStyles["input"] = ["color": "#167DCE", "sound": "silent"]
    customized.eventStyles["complete"] = ["color": "#E04099", "sound": "permission"]
    let data = try! JSONSerialization.data(withJSONObject: customized.json)
    let restored = CueSettings(json: try! JSONSerialization.jsonObject(with: data) as! [String: Any])
    let invalid = CueSettings(json: ["event_styles": ["input": ["color": "not-a-color", "sound": "custom:../escape.wav"]]])
    let results: [String: Bool] = [
        "legacy_defaults_preserved": legacy.eventStyles == CueSettings.defaultStyles && legacy.inputDuration == 20 && legacy.soundVolume == 0.4,
        "color_roundtrip_and_render_mapping": restored.color(for: "input").cueHex == "#167DCE" && restored.color(for: "complete").cueHex == "#E04099",
        "sound_roundtrip_and_playback_mapping": restored.soundToken(for: "input") == "silent" && restored.soundName(for: "input") == "静音" && restored.soundToken(for: "complete") == "permission" && restored.soundName(for: "complete") == "双短一长",
        "invalid_style_defaults": invalid.eventStyles == CueSettings.defaultStyles,
        "custom_sound_basename_only": CueSettings.validSoundToken("custom:sample.wav") && !CueSettings.validSoundToken("custom:../sample.wav") && !CueSettings.validSoundToken("custom:/tmp/sample.wav") && !CueSettings.validSoundToken("custom:..") && !CueSettings.validSoundToken("custom:folder\\sample.wav")
    ]
    if let json = try? JSONSerialization.data(withJSONObject: results, options: [.sortedKeys]), let output = String(data: json, encoding: .utf8) { print(output) }
    exit(results.values.allSatisfy { $0 } ? 0 : 1)
}

if CommandLine.arguments.contains("--event-validation-self-test") {
    let cases: [(String, String, Bool, Bool)] = [
        ("raw-request", "PermissionRequest hook", false, false),
        ("raw-request-cannot-self-confirm", "PermissionRequest hook", true, false),
        ("unconfirmed-unknown-source", "", false, false),
        ("confirmed-client-wait", "codex client approval stream", true, true),
        ("explicit-manual-test", "manual test", false, true)
    ]
    var failed = false
    var results: [[String: Any]] = []
    for (name, source, confirmed, expected) in cases {
        let raw: [String: Any] = ["id": name, "kind": "permission", "source": source,
                                  "requires_user_action": confirmed, "created_at": Date().timeIntervalSince1970]
        let accepted = (try? JSONSerialization.data(withJSONObject: raw)).flatMap { CueEvent($0) } != nil
        failed = failed || accepted != expected
        results.append(["name": name, "accepted": accepted, "expected": expected])
    }
    if let json = try? JSONSerialization.data(withJSONObject: results, options: [.sortedKeys]), let output = String(data: json, encoding: .utf8) { print(output) }
    exit(failed ? 1 : 0)
}

if let index = CommandLine.arguments.firstIndex(of: "--audio-self-test"), CommandLine.arguments.indices.contains(index + 1) {
    let result = CueChime.selfTest(directory: URL(fileURLWithPath: CommandLine.arguments[index + 1], isDirectory: true))
    if let json = try? JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]), let output = String(data: json, encoding: .utf8) { print(output) }
    exit(result.allSatisfy { $0["play_started"] as? Bool == true && $0["is_playing_after_100ms"] as? Bool == true } ? 0 : 1)
}

if CommandLine.arguments.contains("--sound-source-self-test") {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("vibecue-sources-\(UUID().uuidString)")
    let identifiers = ["complete", "nature-rain", "urban-keyboard"] + CueSoundCatalog.presets(tagID: "system").prefix(1).map(\.id)
    let results: [[String: Any]] = identifiers.map { id in
        do {
            let url = try CueChime.write(kind: id, directory: directory)
            let player = try AVAudioPlayer(contentsOf: url); player.volume = 0
            let prepared = player.prepareToPlay(), started = player.play()
            RunLoop.current.run(until: Date().addingTimeInterval(0.12))
            let result: [String: Any] = ["id": id, "prepared": prepared, "started": started, "playing_after_100ms": player.isPlaying, "duration": player.duration, "volume": 0]
            player.stop(); return result
        } catch { return ["id": id, "error": error.localizedDescription] }
    }
    try? FileManager.default.removeItem(at: directory)
    print(String(data: try! JSONSerialization.data(withJSONObject: results, options: [.sortedKeys]), encoding: .utf8)!)
    exit(results.allSatisfy { $0["started"] as? Bool == true && $0["playing_after_100ms"] as? Bool == true } ? 0 : 1)
}

let app = NSApplication.shared
let controller = CueController()
app.delegate = controller
app.run()
