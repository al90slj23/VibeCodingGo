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
        "complete": ["color": "#3BE8AB", "sound": "complete"],
        "input": ["color": "#FFC44D", "sound": "input"],
        "permission": ["color": "#FF7A61", "sound": "permission"]
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
    var eventStyles = CueSettings.defaultStyles

    init() {}
    init(json: [String: Any]) {
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
            }
        }
    }
    var json: [String: Any] {
        ["version": 1, "enabled": enabled, "complete_enabled": completeEnabled,
         "input_enabled": inputEnabled, "permission_enabled": permissionEnabled,
         "glow_enabled": glowEnabled, "all_displays": allDisplays,
         "glow_intensity": glowIntensity, "sound_enabled": soundEnabled,
         "sound_volume": soundVolume, "complete_duration": completeDuration,
         "input_duration": inputDuration, "permission_duration": permissionDuration,
         "event_styles": eventStyles]
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
        if ["complete", "input", "permission", "error", "silent"].contains(token) { return true }
        let name = String(token.dropFirst(7))
        return token.hasPrefix("custom:") && !name.isEmpty && name != "." && name != ".." && name == URL(fileURLWithPath: name).lastPathComponent && !name.contains("/") && !name.contains("\\")
    }
    func color(for kind: String) -> NSColor {
        NSColor.cueColor(hex: eventStyles[kind]?["color"] ?? "") ?? CueEvent(kind: kind, title: "", message: "").color
    }
    func soundToken(for kind: String) -> String { eventStyles[kind]?["sound"] ?? kind }
    func soundName(for kind: String) -> String {
        let token = soundToken(for: kind)
        if token == "silent" { return "静音" }
        if token.hasPrefix("custom:") { return "自选音效" }
        return CueEvent(kind: token, title: "", message: "").sound
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

// Generate short PCM chimes locally. AVAudioPlayer uses the normal media output
// rather than the separate macOS sound-effects route used by named NSSounds.
struct CueChime {
    static let sampleRate = 48_000

    static func waveData(kind: String) -> Data {
        let notes: [(Double, Double, Double)]
        switch kind {
        case "complete": notes = [(523.25, 0.16, 0.035), (659.25, 0.16, 0.035), (783.99, 0.34, 0.12)]
        case "input": notes = [(880, 0.22, 0.10), (659.25, 0.22, 0.10), (880, 0.30, 0.12)]
        case "permission": notes = [(440, 0.16, 0.09), (440, 0.16, 0.09), (554.37, 0.34, 0.12)]
        default: notes = [(349.23, 0.26, 0.10), (261.63, 0.42, 0.14)]
        }
        var pcm = Data()
        for (frequency, duration, gap) in notes {
            let count = Int(Double(sampleRate) * duration)
            for sample in 0..<count {
                let time = Double(sample) / Double(sampleRate)
                let attack = min(1, time / 0.015)
                let release = min(1, max(0, (duration - time) / 0.09))
                let envelope = attack * release * exp(-1.4 * time / duration)
                let fundamental = sin(2 * .pi * frequency * time)
                let harmonic = 0.16 * sin(2 * .pi * frequency * 2 * time)
                let value = Int16(max(-1, min(1, 0.65 * envelope * (fundamental + harmonic))) * Double(Int16.max))
                pcm.appendLittleEndian(value)
            }
            pcm.append(Data(count: Int(Double(sampleRate) * gap) * 2))
        }
        var wav = Data("RIFF".utf8)
        wav.appendLittleEndian(UInt32(36 + pcm.count))
        wav.append(Data("WAVEfmt ".utf8))
        wav.appendLittleEndian(UInt32(16))
        wav.appendLittleEndian(UInt16(1)) // PCM
        wav.appendLittleEndian(UInt16(1)) // mono
        wav.appendLittleEndian(UInt32(sampleRate))
        wav.appendLittleEndian(UInt32(sampleRate * 2))
        wav.appendLittleEndian(UInt16(2))
        wav.appendLittleEndian(UInt16(16))
        wav.append(Data("data".utf8))
        wav.appendLittleEndian(UInt32(pcm.count))
        wav.append(pcm)
        return wav
    }

    static func write(kind: String, directory: URL) throws -> URL {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let path = directory.appendingPathComponent("\(kind).wav")
        try waveData(kind: kind).write(to: path, options: .atomic)
        return path
    }

    // A silent pipeline check; changes only each player's volume, never system settings.
    static func selfTest(directory: URL) -> [[String: Any]] {
        ["complete", "input", "permission", "error"].map { kind in
            do {
                let path = try write(kind: kind, directory: directory)
                let player = try AVAudioPlayer(contentsOf: path)
                player.volume = 0
                player.isMeteringEnabled = true
                let prepared = player.prepareToPlay()
                let started = player.play()
                RunLoop.current.run(until: Date().addingTimeInterval(0.12))
                player.updateMeters()
                let result: [String: Any] = ["kind": kind, "audio_backend": "AVAudioPlayer", "volume": 0,
                    "path": path.path, "prepared": prepared, "play_started": started,
                    "is_playing_after_100ms": player.isPlaying, "playback_time": player.currentTime,
                    "duration": player.duration, "peak_dbfs": player.peakPower(forChannel: 0),
                    "output_device_uid": player.currentDevice ?? "system-default"]
                player.stop()
                return result
            } catch { return ["kind": kind, "error": error.localizedDescription] }
        }
    }
}

extension Data {
    mutating func appendLittleEndian<T: FixedWidthInteger>(_ value: T) {
        var little = value.littleEndian
        Swift.withUnsafeBytes(of: &little) { append(contentsOf: $0) }
    }
}

final class GlowView: NSView {
    var glow = NSColor.systemGreen
    override var isOpaque: Bool { false }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.clear.setFill(); bounds.fill(using: .copy)
        let width: CGFloat = 34
        let gradient = NSGradient(colors: [glow.withAlphaComponent(0.62), glow.withAlphaComponent(0.12), glow.withAlphaComponent(0)])!
        gradient.draw(in: NSRect(x: 0, y: 0, width: width, height: bounds.height), angle: 0)
        gradient.draw(in: NSRect(x: bounds.width - width, y: 0, width: width, height: bounds.height), angle: 180)
        gradient.draw(in: NSRect(x: 0, y: 0, width: bounds.width, height: width), angle: 90)
        gradient.draw(in: NSRect(x: 0, y: bounds.height - width, width: bounds.width, height: width), angle: 270)
        glow.withAlphaComponent(0.85).setStroke()
        let outline = NSBezierPath(roundedRect: bounds.insetBy(dx: 1.5, dy: 1.5), xRadius: 10, yRadius: 10)
        outline.lineWidth = 2; outline.stroke()
    }
}

final class CuePanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

final class BannerView: NSView {
    let statusLabel = NSTextField(labelWithString: "")
    let conversationLabel = NSTextField(labelWithString: "")
    let messageLabel = NSTextField(wrappingLabelWithString: "")
    let icon = NSImageView()
    let openButton = NSButton(title: "打开对话 ↗", target: nil, action: nil)
    let closeButton = NSButton(title: "×", target: nil, action: nil)

    init(event: CueEvent, target: AnyObject, color: NSColor) {
        super.init(frame: NSRect(x: 0, y: 0, width: 560, height: 164))
        wantsLayer = true
        layer?.backgroundColor = NSColor(srgbRed: 0.055, green: 0.07, blue: 0.09, alpha: 0.97).cgColor
        layer?.cornerRadius = 20
        layer?.borderColor = color.withAlphaComponent(0.55).cgColor
        layer?.borderWidth = 1

        icon.image = NSImage(systemSymbolName: event.symbol, accessibilityDescription: event.label)
        icon.contentTintColor = color
        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 27, weight: .medium)
        icon.frame = NSRect(x: 22, y: 105, width: 35, height: 35)
        addSubview(icon)

        statusLabel.stringValue = "\(event.providerLabel.uppercased())  ·  \(event.label)"
        statusLabel.font = .systemFont(ofSize: 12, weight: .semibold)
        statusLabel.textColor = color
        statusLabel.frame = NSRect(x: 72, y: 127, width: 420, height: 18)
        addSubview(statusLabel)

        conversationLabel.stringValue = event.title
        conversationLabel.font = .systemFont(ofSize: 19, weight: .semibold)
        conversationLabel.textColor = .white
        conversationLabel.lineBreakMode = .byTruncatingTail
        conversationLabel.frame = NSRect(x: 72, y: 96, width: 445, height: 27)
        conversationLabel.toolTip = event.title
        addSubview(conversationLabel)

        messageLabel.stringValue = event.message.isEmpty ? "回到这个对话查看最新进度。" : event.message
        messageLabel.font = .systemFont(ofSize: 12)
        messageLabel.textColor = NSColor.white.withAlphaComponent(0.70)
        messageLabel.maximumNumberOfLines = 2
        messageLabel.lineBreakMode = .byWordWrapping
        messageLabel.frame = NSRect(x: 72, y: 49, width: 445, height: 38)
        addSubview(messageLabel)

        openButton.bezelStyle = .inline
        openButton.contentTintColor = color
        openButton.font = .systemFont(ofSize: 12, weight: .semibold)
        openButton.target = target; openButton.action = #selector(CueController.openCurrent)
        openButton.frame = NSRect(x: 71, y: 14, width: 116, height: 24)
        openButton.isEnabled = event.provider == "codex" && !event.threadID.isEmpty
        addSubview(openButton)

        closeButton.bezelStyle = .inline
        closeButton.contentTintColor = NSColor.white.withAlphaComponent(0.70)
        closeButton.font = .systemFont(ofSize: 24, weight: .light)
        closeButton.target = target; closeButton.action = #selector(CueController.dismissCurrent)
        closeButton.frame = NSRect(x: 519, y: 119, width: 27, height: 29)
        closeButton.setAccessibilityLabel("关闭提醒")
        addSubview(closeButton)
        setAccessibilityElement(true)
        setAccessibilityLabel("\(event.providerLabel) 提醒：\(event.label)，\(event.title)")
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
}

final class CueController: NSObject, NSApplicationDelegate, AVAudioPlayerDelegate {
    let home: URL
    let eventsDirectory: URL
    let inflightDirectory: URL
    var statusItem: NSStatusItem!
    var edges: [NSPanel] = []
    var banner: NSPanel?
    var current: CueEvent?
    var pending: [CueEvent] = []
    var recent: [CueEvent] = []
    var seen: [String] = []
    var readTimer: Timer?
    var pulseTimer: Timer?
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
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] { settings = CueSettings(json: json) }
        saveSettings()
        restoreHistoryAccess()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = NSImage(systemSymbolName: "waveform.path", accessibilityDescription: "VibeCue")
        statusItem.button?.toolTip = "VibeCue — AI Coding Companion"
        startWatcher()
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
        for file in files.filter({ $0.pathExtension == "json" }).sorted(by: { $0.lastPathComponent < $1.lastPathComponent }).prefix(50) {
            guard let properties = try? file.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey]),
                  properties.isRegularFile == true, properties.isSymbolicLink != true,
                  let size = properties.fileSize, size <= 1_048_576 else { continue }
            let claim = inflightDirectory.appendingPathComponent(UUID().uuidString + ".json")
            do {
                try FileManager.default.moveItem(at: file, to: claim)
                defer { try? FileManager.default.removeItem(at: claim) }
                guard let event = CueEvent(try Data(contentsOf: claim)), abs(event.date.timeIntervalSinceNow) <= 120 else { continue }
                enqueue(event)
            } catch { /* A producer or another process may have moved the file. */ }
        }
    }

    func enqueue(_ event: CueEvent) {
        guard !seen.contains(event.id) else { return }
        seen.append(event.id)
        if seen.count > 512 { seen.removeFirst(seen.count - 512) }
        recent.removeAll { $0.threadID == event.threadID && $0.kind == event.kind && !event.threadID.isEmpty }
        recent.insert(event, at: 0)
        if recent.count > 8 { recent.removeLast(recent.count - 8) }
        refreshMenu()
        guard pausedUntil == nil, settings.allows(event) else { return }
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

    func showNext() {
        guard current == nil, pausedUntil == nil else { return }
        pending.removeAll { abs($0.date.timeIntervalSinceNow) > 120 || !settings.allows($0) }
        guard !pending.isEmpty else { writeStatus(); return }
        let event = pending.removeFirst()
        current = event; shownAt = Date()
        lastShownTitle = event.title
        makeWindows(for: event)
        playChime(kind: event.kind, name: event.sound)
        if !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            pulseTimer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in
                guard let self else { return }
                let opacity = self.settings.glowIntensity * (0.76 + 0.20 * sin(Date().timeIntervalSince(self.shownAt) * .pi * 0.75))
                self.edges.forEach { $0.alphaValue = opacity }
            }
            RunLoop.main.add(pulseTimer!, forMode: .common)
        }
        hideTimer = Timer.scheduledTimer(timeInterval: settings.duration(event.kind), target: self, selector: #selector(dismissCurrent), userInfo: nil, repeats: false)
        RunLoop.main.add(hideTimer!, forMode: .common)
        writeStatus()
    }

    func makeWindows(for event: CueEvent) {
        edges.forEach { $0.close() }; edges.removeAll()
        banner?.close(); banner = nil
        let targetScreen = NSScreen.screens.first(where: { NSMouseInRect(NSEvent.mouseLocation, $0.frame, false) }) ?? NSScreen.main
        let screens = settings.allDisplays ? NSScreen.screens : targetScreen.map { [$0] } ?? []
        for screen in settings.glowEnabled ? screens : [] {
            let panel = CuePanel(contentRect: screen.frame, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            configure(panel, clickThrough: true)
            let glow = GlowView(frame: NSRect(origin: .zero, size: screen.frame.size))
            glow.glow = settings.color(for: event.kind)
            panel.contentView = glow
            panel.alphaValue = settings.glowIntensity
            panel.orderFrontRegardless()
            edges.append(panel)
        }
        guard let screen = NSScreen.screens.first(where: { NSMouseInRect(NSEvent.mouseLocation, $0.frame, false) }) ?? NSScreen.main else { return }
        let width = min(CGFloat(560), screen.visibleFrame.width - 40)
        let rect = NSRect(x: screen.visibleFrame.midX - width / 2, y: screen.visibleFrame.maxY - 188, width: width, height: 164)
        let panel = CuePanel(contentRect: rect, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        configure(panel, clickThrough: false)
        panel.hasShadow = true
        panel.contentView = BannerView(event: event, target: self, color: settings.color(for: event.kind))
        panel.orderFrontRegardless()
        banner = panel
    }

    func configure(_ panel: NSPanel, clickThrough: Bool) {
        panel.isOpaque = false; panel.backgroundColor = .clear
        panel.hasShadow = false; panel.ignoresMouseEvents = clickThrough
        panel.level = NSWindow.Level(rawValue: NSWindow.Level.screenSaver.rawValue + (clickThrough ? 0 : 1))
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        panel.hidesOnDeactivate = false; panel.isReleasedWhenClosed = false
        panel.animationBehavior = .none
    }

    func hideWindows() {
        pulseTimer?.invalidate(); pulseTimer = nil
        hideTimer?.invalidate(); hideTimer = nil
        edges.forEach { $0.close() }; edges.removeAll()
        banner?.close(); banner = nil
    }

    @objc func dismissCurrent() {
        hideWindows(); current = nil; writeStatus()
        if !pending.isEmpty {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                guard self.current == nil else { return }
                self.showNext()
            }
        }
    }
    @objc func openCurrent() { if let current { open(event: current) }; dismissCurrent() }
    func open(event: CueEvent) {
        guard event.provider == "codex", !event.threadID.isEmpty,
              event.threadID.unicodeScalars.allSatisfy({ CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_" )).contains($0) }) else { return }
        var components = URLComponents()
        components.scheme = "codex"; components.host = "threads"; components.path = "/" + event.threadID
        if let url = components.url { NSWorkspace.shared.open(url) }
    }
    @objc func openRecent(_ sender: NSMenuItem) {
        guard sender.tag < recent.count else { return }
        open(event: recent[sender.tag])
    }
    @objc func toggleMute() {
        muted.toggle(); saveSettings()
        if muted { audioPlayer?.stop() }
        refreshMenu(); settingsPanel?.rebuild(); writeStatus()
    }

    func playChime(kind: String, name: String) {
        audioPlayer?.stop()
        let token = settings.soundToken(for: kind)
        lastSound = settings.soundName(for: kind); lastSoundPlayStarted = false; lastSoundPlayingAfter100ms = false
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
        playChime(kind: kind, name: CueEvent(kind: kind, title: "", message: "").sound)
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
        let path = home.appendingPathComponent("watcher-state.json")
        if let data = try? Data(contentsOf: path), data.count <= 2_097_152,
           let state = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           state["running"] as? Bool == true,
           let heartbeat = state["heartbeat"] as? Double,
           (0...15).contains(Date().timeIntervalSince1970 - heartbeat) {
            let count = state["watched_threads"] as? Int ?? 0
            let errors = state["read_error_count"] as? Int ?? 0
            if errors > 0 {
                message = "需要授权读取记录 · \(errors) 个文件无法读取"
            } else if count == 0 {
                message = "尚未找到可监控的本机对话"
            } else {
                message = "本机监控中 · \(count) 个对话"
            }
        }
        if watcherHealthText != message {
            watcherHealthText = message
            if statusItem != nil { refreshMenu() }
        }
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

    func applySettings() {
        saveSettings()
        if muted || !settings.enabled || pausedUntil != nil { audioPlayer?.stop() }
        audioPlayer?.volume = Float(settings.soundVolume)
        pending.removeAll { !settings.allows($0) }
        if let current {
            if settings.allows(current) {
                makeWindows(for: current)
                hideTimer?.invalidate()
                let remaining = max(0.1, settings.duration(current.kind) - Date().timeIntervalSince(shownAt))
                hideTimer = Timer.scheduledTimer(timeInterval: remaining, target: self, selector: #selector(dismissCurrent), userInfo: nil, repeats: false)
                RunLoop.main.add(hideTimer!, forMode: .common)
            } else { audioPlayer?.stop(); hideWindows(); self.current = nil }
        }
        refreshMenu(); writeStatus()
        if current == nil, pausedUntil == nil { showNext() }
    }

    func refreshMenu() {
        let menu = NSMenu()
        let panel = NSMenuItem(title: "打开 VibeCue 面板…", action: #selector(openSettings), keyEquivalent: ",")
        panel.target = self; menu.addItem(panel)
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
        if !recent.isEmpty {
            menu.addItem(.separator())
            let header = NSMenuItem(title: "最近提醒 · 点击打开对话", action: nil, keyEquivalent: "")
            header.isEnabled = false; menu.addItem(header)
            for (index, event) in recent.enumerated() {
                let item = NSMenuItem(title: "\(event.kind == "complete" ? "✓" : "●") \(event.title.prefix(36))", action: #selector(openRecent(_:)), keyEquivalent: "")
                item.target = self; item.tag = index; item.isEnabled = event.provider == "codex" && !event.threadID.isEmpty
                item.toolTip = "\(event.label) · \(event.title)"; menu.addItem(item)
            }
        }
        menu.addItem(.separator())
        let exit = NSMenuItem(title: "结束 VibeCue", action: #selector(quit), keyEquivalent: "q")
        exit.target = self; menu.addItem(exit)
        statusItem.menu = menu
    }

    func writeStatus() {
        let data: [String: Any] = ["pid": ProcessInfo.processInfo.processIdentifier, "muted": muted,
            "paused_until": pausedUntil?.timeIntervalSince1970 ?? 0,
            "current_id": current?.id ?? "", "current_kind": current?.kind ?? "",
            "current_title": current?.title ?? "", "last_shown_title": lastShownTitle,
            "current_color": current.map { settings.color(for: $0.kind).cueHex } ?? "",
            "selected_sound": current.map { settings.soundToken(for: $0.kind) } ?? "",
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
            "codex_permission_wait_supported": false, "raw_permission_requests_suppressed": true,
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
    override var isFlipped: Bool { true }
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

    init(cue: CueController) {
        self.cue = cue
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 590), styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "VibeCue — AI Coding Companion"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isReleasedWhenClosed = false
        window.appearance = NSAppearance(named: .darkAqua)
        window.center()
        super.init(window: window)
        window.delegate = self
        window.contentView = root
        root.wantsLayer = true
        root.layer?.backgroundColor = NSColor(srgbRed: 0.09, green: 0.10, blue: 0.12, alpha: 1).cgColor
        sidebar.wantsLayer = true
        sidebar.layer?.backgroundColor = NSColor(srgbRed: 0.065, green: 0.075, blue: 0.09, alpha: 1).cgColor
        root.addSubview(sidebar); root.addSubview(content)
        buildSidebar()
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
        mark.contentTintColor = NSColor(srgbRed: 0.44, green: 0.92, blue: 0.77, alpha: 1)
        mark.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 24, weight: .semibold)
        sidebar.addSubview(mark)
        label("VibeCue", in: sidebar, frame: NSRect(x: 60, y: 29, width: 116, height: 30), size: 22, weight: .semibold)
        label("AI Coding Companion", in: sidebar, frame: NSRect(x: 20, y: 73, width: 156, height: 17), size: 11, color: .secondaryLabelColor)
        label("AI 开发提醒助手", in: sidebar, frame: NSRect(x: 20, y: 94, width: 156, height: 18), size: 11, color: .secondaryLabelColor)
        for (index, pair) in [("总览", "square.grid.2x2"), ("提醒规则", "bell"), ("泛光与声音", "waveform"), ("集成与文件", "puzzlepiece.extension")].enumerated() {
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
        label("VibeCue 0.1.1", in: sidebar, frame: NSRect(x: 20, y: 555, width: 154, height: 17), size: 10.5, color: .tertiaryLabelColor)
    }

    @objc func navigate(_ sender: NSButton) { selectPage(sender.tag) }
    func selectPage(_ value: Int) { page = min(3, max(0, value)); rebuild() }

    func rebuild() {
        content.subviews.forEach { $0.removeFromSuperview() }
        healthLabel = nil; threadLabel = nil; latestLabel = nil; hookLabel = nil; sliderLabels = [:]
        for (index, button) in navigation.enumerated() {
            button.layer?.backgroundColor = (index == page ? NSColor(srgbRed: 0.16, green: 0.24, blue: 0.24, alpha: 1) : .clear).cgColor
            button.contentTintColor = index == page ? NSColor(srgbRed: 0.58, green: 0.96, blue: 0.83, alpha: 1) : .secondaryLabelColor
        }
        switch page {
        case 0: overview()
        case 1: notifications()
        case 2: glowAndSound()
        default: providers()
        }
        label("变更会立即保存到本机。", in: content, frame: NSRect(x: 26, y: 558, width: 525, height: 18), size: 10.5, color: .tertiaryLabelColor)
        updateHealth()
    }

    func heading(_ title: String, _ detail: String) {
        label(title, in: content, frame: NSRect(x: 26, y: 25, width: 525, height: 31), size: 24, weight: .semibold)
        label(detail, in: content, frame: NSRect(x: 27, y: 66, width: 525, height: 21), size: 12, color: .secondaryLabelColor)
    }

    func card(_ y: CGFloat, _ height: CGFloat) -> CueFlippedView {
        let view = CueFlippedView(frame: NSRect(x: 26, y: y, width: 526, height: height))
        view.wantsLayer = true
        view.layer?.backgroundColor = NSColor(srgbRed: 0.125, green: 0.14, blue: 0.165, alpha: 1).cgColor
        view.layer?.borderColor = NSColor.white.withAlphaComponent(0.06).cgColor
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
        label("测试会使用目前的提醒与音量设置；一般提醒不会抢走输入焦点。", in: content, frame: NSRect(x: 27, y: 505, width: 520, height: 34), size: 11, color: .secondaryLabelColor, lines: 2)
    }

    func notifications() {
        heading("提醒自定义", "分别设置每一种提醒的颜色、声音与显示时间。")
        let selector = NSPopUpButton(frame: NSRect(x: 26, y: 106, width: 526, height: 32), pullsDown: false)
        for (name, kind) in [("本轮已完成", "complete"), ("需要回答或判断", "input"), ("人工批准 · 仅供预览", "permission")] {
            selector.addItem(withTitle: name); selector.lastItem?.representedObject = kind
            if kind == selectedKind { selector.select(selector.lastItem) }
        }
        selector.target = self; selector.action = #selector(selectEventKind(_:))
        selector.identifier = NSUserInterfaceItemIdentifier("event.kind"); selector.setAccessibilityLabel("自定义提醒类型")
        content.addSubview(selector)
        let view = card(154, 356)
        let isPermission = selectedKind == "permission"
        let on = selectedKind == "complete" ? cue.settings.completeEnabled : cue.settings.inputEnabled
        let detail = isPermission ? "Codex 尚无可确认人工等待的信号；自动批准不提醒。可先自定义并预览。" : (selectedKind == "complete" ? "目前一轮运行结束，不一定代表整个需求已交付。" : "Codex 提出问题时提醒你回答或选择。")
        toggle(isPermission ? "人工批准（尚未接入）" : "启用这一类提醒", detail, in: view, y: 17, key: selectedKind + "_enabled", on: isPermission ? false : on, enabled: !isPermission)
        label("泛光颜色", in: view, frame: NSRect(x: 20, y: 91, width: 170, height: 23), size: 12, weight: .medium)
        let color = NSColorWell(frame: NSRect(x: 391, y: 84, width: 116, height: 32))
        color.color = cue.settings.color(for: selectedKind); color.target = self; color.action = #selector(changeEventColor(_:))
        color.identifier = NSUserInterfaceItemIdentifier("event.color"); color.setAccessibilityLabel("提醒泛光颜色")
        view.addSubview(color)
        label("提示音", in: view, frame: NSRect(x: 20, y: 136, width: 170, height: 23), size: 12, weight: .medium)
        let sounds = NSPopUpButton(frame: NSRect(x: 200, y: 129, width: 307, height: 31), pullsDown: false)
        let selectedSound = cue.settings.soundToken(for: selectedKind)
        var options = [("上行三音", "complete"), ("问答三音", "input"), ("双短一长", "permission"), ("低音警示", "error"), ("静音", "silent")]
        if selectedSound.hasPrefix("custom:") { options.append(("自选音效（已导入）", selectedSound)) }
        for (name, token) in options {
            sounds.addItem(withTitle: name); sounds.lastItem?.representedObject = token
            if token == selectedSound { sounds.select(sounds.lastItem) }
        }
        sounds.target = self; sounds.action = #selector(changeEventSound(_:))
        sounds.identifier = NSUserInterfaceItemIdentifier("event.sound"); sounds.setAccessibilityLabel("提醒提示音")
        view.addSubview(sounds)
        _ = button("选取音效文件…", in: view, frame: NSRect(x: 200, y: 172, width: 147, height: 30), action: #selector(importEventSound))
        _ = button("只试听", in: view, frame: NSRect(x: 356, y: 172, width: 151, height: 30), action: #selector(previewEventSound))
        label("显示时间", in: view, frame: NSRect(x: 20, y: 226, width: 170, height: 23), size: 12, weight: .medium)
        let duration = NSPopUpButton(frame: NSRect(x: 391, y: 218, width: 116, height: 30), pullsDown: false)
        var seconds: [Double] = [3, 5, 8, 14, 20, 30, 60]
        let selectedDuration = cue.settings.duration(selectedKind)
        if !seconds.contains(selectedDuration) { seconds.append(selectedDuration); seconds.sort() }
        for value in seconds {
            duration.addItem(withTitle: "\(Int(value)) 秒"); duration.lastItem?.representedObject = value
            if value == selectedDuration { duration.select(duration.lastItem) }
        }
        duration.identifier = NSUserInterfaceItemIdentifier(selectedKind + "_duration")
        duration.target = self; duration.action = #selector(changeDuration(_:)); duration.setAccessibilityLabel("提醒显示时间")
        view.addSubview(duration)
        _ = button("预览这种提醒", in: view, frame: NSRect(x: 17, y: 282, width: 240, height: 34), action: #selector(previewEvent))
        _ = button("重置此类默认", in: view, frame: NSRect(x: 267, y: 282, width: 240, height: 34), action: #selector(resetEventStyle))
        label("设置立即保存。音效文件会验证后复制到本机；全局音量与亮度仍然生效。", in: content, frame: NSRect(x: 27, y: 526, width: 520, height: 36), size: 10.5, color: .secondaryLabelColor, lines: 2)
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
        let provider = card(106, 270)
        label("Codex", in: provider, frame: NSRect(x: 20, y: 17, width: 486, height: 27), size: 18, weight: .semibold)
        healthLabel = label("", in: provider, frame: NSRect(x: 20, y: 53, width: 486, height: 21), size: 12, weight: .medium)
        label("完成与结构化问题由本机对话记录监控。自动批准后继续运行的权限请求不提醒。目前尚无可确认人工批准等待的信号，因此 Codex 的人工批准提醒暂不启用。", in: provider, frame: NSRect(x: 20, y: 88, width: 486, height: 73), size: 12, color: .secondaryLabelColor, lines: 4)
        hookLabel = label("", in: provider, frame: NSRect(x: 20, y: 169, width: 486, height: 20), size: 11, color: .secondaryLabelColor)
        _ = button("授权读取本机 Codex 记录…", in: provider, frame: NSRect(x: 16, y: 200, width: 283, height: 31), action: #selector(authorizeHistoryRead))
        label("没有本机记录的云端对话不在涵盖范围；尚未接入其他 AI 工具。", in: provider, frame: NSRect(x: 20, y: 242, width: 486, height: 23), size: 10.5, color: .tertiaryLabelColor)
        let files = card(392, 145)
        label("本机文件", in: files, frame: NSRect(x: 20, y: 17, width: 480, height: 24), size: 13, weight: .semibold)
        label(cue.home.path, in: files, frame: NSRect(x: 20, y: 52, width: 486, height: 35), size: 10.5, color: .secondaryLabelColor, lines: 2)
        _ = button("原代码文件夹", in: files, frame: NSRect(x: 16, y: 96, width: 160, height: 31), action: #selector(openSource))
        _ = button("设置文件夹", in: files, frame: NSRect(x: 181, y: 96, width: 160, height: 31), action: #selector(openConfig))
        _ = button("诊断日志", in: files, frame: NSRect(x: 346, y: 96, width: 160, height: 31), action: #selector(openLogs))
    }

    func updateHealth() {
        healthLabel?.stringValue = cue.watcherHealthText
        healthLabel?.textColor = cue.watcherHealthText.hasPrefix("本机监控中") ? NSColor(srgbRed: 0.5, green: 0.94, blue: 0.76, alpha: 1) : .systemOrange
        let pause = cue.pausedUntil == nil ? (cue.settings.enabled ? "提醒已启用" : "提醒已关闭") : "提醒已暂停"
        threadLabel?.stringValue = "\(pause)  ·  \(cue.settings.soundEnabled ? "声音已启用" : "声音已关闭")"
        latestLabel?.stringValue = cue.recent.first.map { "\($0.label) · \($0.title)" } ?? "尚无提醒。可以先测试一则提醒效果。"
        hookLabel?.stringValue = "普通权限请求已忽略；结构化提问 hook 为可选补充。"
    }

    @objc func changeToggle(_ sender: NSSwitch) {
        let value = sender.state == .on
        switch sender.identifier?.rawValue {
        case "enabled": cue.settings.enabled = value
        case "complete_enabled": cue.settings.completeEnabled = value
        case "input_enabled": cue.settings.inputEnabled = value
        case "permission_enabled": return
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
            cue.applySettings(); rebuild()
        } catch {
            let alert = NSAlert(); alert.messageText = "无法导入音效"; alert.informativeText = error.localizedDescription
            alert.addButton(withTitle: "知道了"); alert.runModal()
        }
    }
    @objc func previewEventSound() {
        cue.playChime(kind: selectedKind, name: cue.settings.soundName(for: selectedKind))
    }
    @objc func previewEvent() {
        cue.enqueue(CueEvent(kind: selectedKind, title: "VibeCue · 自定义提醒预览", message: "使用这一类的颜色、提示音与显示时间。"))
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
        cue.playChime(kind: kind, name: CueEvent(kind: kind, title: "", message: "").sound)
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
        ("confirmed-runtime-wait", "runtime waitingOnApproval", true, true),
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

let app = NSApplication.shared
let controller = CueController()
app.delegate = controller
app.run()
