import AppKit
import AVFoundation
import Foundation
import Darwin

struct CueEvent {
    let id: String
    let kind: String
    let title: String
    let threadID: String
    let message: String
    let date: Date
    let provider: String

    init?(_ data: Data) {
        guard let raw = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let id = raw["id"] as? String, !id.isEmpty,
              let kind = raw["kind"] as? String,
              ["complete", "input", "permission", "error"].contains(kind) else { return nil }
        self.id = String(id.prefix(200))
        self.kind = kind
        self.provider = String((raw["provider"] as? String ?? "codex").prefix(40))
        self.title = String((raw["title"] as? String ?? "Codex 對話").prefix(180))
        self.threadID = String((raw["thread_id"] as? String ?? "").prefix(160))
        self.message = String((raw["message"] as? String ?? "").prefix(220))
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
    var label: String {
        switch kind {
        case "complete": return "本輪已完成"
        case "input": return "需要你回答或判斷"
        case "permission": return "有權限請求，請查看 Codex"
        default: return "執行遇到問題"
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
        case "input": return "問答三音"
        case "permission": return "雙短一長"
        default: return "低音警示"
        }
    }
}

struct CueSettings {
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
    }
    var json: [String: Any] {
        ["version": 1, "enabled": enabled, "complete_enabled": completeEnabled,
         "input_enabled": inputEnabled, "permission_enabled": permissionEnabled,
         "glow_enabled": glowEnabled, "all_displays": allDisplays,
         "glow_intensity": glowIntensity, "sound_enabled": soundEnabled,
         "sound_volume": soundVolume, "complete_duration": completeDuration,
         "input_duration": inputDuration, "permission_duration": permissionDuration]
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
    func duration(_ kind: String) -> Double {
        switch kind {
        case "complete": return completeDuration
        case "input": return inputDuration
        default: return permissionDuration
        }
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
    let openButton = NSButton(title: "開啟對話 ↗", target: nil, action: nil)
    let closeButton = NSButton(title: "×", target: nil, action: nil)

    init(event: CueEvent, target: AnyObject) {
        super.init(frame: NSRect(x: 0, y: 0, width: 560, height: 164))
        wantsLayer = true
        layer?.backgroundColor = NSColor(srgbRed: 0.055, green: 0.07, blue: 0.09, alpha: 0.97).cgColor
        layer?.cornerRadius = 20
        layer?.borderColor = event.color.withAlphaComponent(0.55).cgColor
        layer?.borderWidth = 1

        icon.image = NSImage(systemSymbolName: event.symbol, accessibilityDescription: event.label)
        icon.contentTintColor = event.color
        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 27, weight: .medium)
        icon.frame = NSRect(x: 22, y: 105, width: 35, height: 35)
        addSubview(icon)

        statusLabel.stringValue = "\(event.providerLabel.uppercased())  ·  \(event.label)"
        statusLabel.font = .systemFont(ofSize: 12, weight: .semibold)
        statusLabel.textColor = event.color
        statusLabel.frame = NSRect(x: 72, y: 127, width: 420, height: 18)
        addSubview(statusLabel)

        conversationLabel.stringValue = event.title
        conversationLabel.font = .systemFont(ofSize: 19, weight: .semibold)
        conversationLabel.textColor = .white
        conversationLabel.lineBreakMode = .byTruncatingTail
        conversationLabel.frame = NSRect(x: 72, y: 96, width: 445, height: 27)
        conversationLabel.toolTip = event.title
        addSubview(conversationLabel)

        messageLabel.stringValue = event.message.isEmpty ? "回到這個對話查看最新進度。" : event.message
        messageLabel.font = .systemFont(ofSize: 12)
        messageLabel.textColor = NSColor.white.withAlphaComponent(0.70)
        messageLabel.maximumNumberOfLines = 2
        messageLabel.lineBreakMode = .byWordWrapping
        messageLabel.frame = NSRect(x: 72, y: 49, width: 445, height: 38)
        addSubview(messageLabel)

        openButton.bezelStyle = .inline
        openButton.contentTintColor = event.color
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
        closeButton.setAccessibilityLabel("關閉提醒")
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
    var watcherHealthText = "監看程序未連線"
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
                    self.enqueue(CueEvent(kind: kind, title: "VibeCue · 提醒效果測試", message: "這是本機示範。正式提醒會顯示對話名稱。"))
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
        guard pausedUntil == nil, settings.allows(event.kind) else { return }
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
        pending.removeAll { abs($0.date.timeIntervalSinceNow) > 120 || !settings.allows($0.kind) }
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
            glow.glow = event.color
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
        panel.contentView = BannerView(event: event, target: self)
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
        lastSound = name; lastSoundPlayStarted = false; lastSoundPlayingAfter100ms = false
        lastSoundPath = ""; lastSoundPeakDBFS = -160; lastSoundPlaybackTime = 0
        lastSoundFinished = false; lastSoundFinishSucceeded = false; lastSoundError = ""
        guard !muted else { writeStatus(); return }
        do {
            let path = try CueChime.write(kind: kind, directory: home.appendingPathComponent("sounds", isDirectory: true))
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
        enqueue(CueEvent(kind: kind, title: "VibeCue · 提醒效果測試", message: "這是本機示範。正式提醒會顯示對話名稱。"))
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
        var message = "監看程序未連線"
        let path = home.appendingPathComponent("watcher-state.json")
        if let data = try? Data(contentsOf: path), data.count <= 2_097_152,
           let state = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           state["running"] as? Bool == true,
           let heartbeat = state["heartbeat"] as? Double,
           (0...15).contains(Date().timeIntervalSince1970 - heartbeat) {
            let count = state["watched_threads"] as? Int ?? 0
            let errors = state["read_error_count"] as? Int ?? 0
            if errors > 0 {
                message = "需要授權讀取記錄 · \(errors) 個檔案無法讀取"
            } else if count == 0 {
                message = "尚未找到可監看的本機對話"
            } else {
                message = "本機監看中 · \(count) 個對話"
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
            watcherLaunchError = "本機監看模組尚未安裝"
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
                try? handle.seekToEnd()
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
            watcherLaunchError = "監看程序啟動失敗：\(error.localizedDescription)"
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
        panel.title = "授權讀取本機 Codex 記錄"
        panel.message = "請選擇 Codex 的 sessions 記錄資料夾。VibeCue 只讀取本機記錄以判斷提醒，不修改或上傳對話。"
        panel.prompt = "允許讀取此資料夾"
        panel.canChooseDirectories = true; panel.canChooseFiles = false
        panel.canCreateDirectories = false; panel.allowsMultipleSelection = false
        panel.showsHiddenFiles = true; panel.directoryURL = expected
        guard panel.runModal() == .OK, let selected = panel.url else { return }
        guard selected.resolvingSymlinksInPath().standardizedFileURL.path == expected.path else {
            let alert = NSAlert()
            alert.messageText = "請選擇 Codex 的 sessions 記錄資料夾"
            alert.informativeText = "為了限定讀取範圍，請選擇：\(expected.path)"
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
        pending.removeAll { !settings.allows($0.kind) }
        if let current {
            if settings.allows(current.kind) {
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
        let panel = NSMenuItem(title: "開啟 VibeCue 面板…", action: #selector(openSettings), keyEquivalent: ",")
        panel.target = self; menu.addItem(panel)
        let subtitle = NSMenuItem(title: "AI Coding Companion · AI 開發提醒助手", action: nil, keyEquivalent: "")
        subtitle.isEnabled = false; menu.addItem(subtitle)
        menu.addItem(.separator())
        let status = NSMenuItem(title: pausedUntil == nil ? "VibeCue · \(watcherHealthText)" : "已暫停提醒 30 分鐘", action: nil, keyEquivalent: "")
        status.isEnabled = false; menu.addItem(status)
        menu.addItem(.separator())
        let enabled = NSMenuItem(title: "啟用提醒", action: #selector(toggleEnabled), keyEquivalent: "")
        enabled.target = self; enabled.state = settings.enabled ? .on : .off; menu.addItem(enabled)
        let mute = NSMenuItem(title: "播放提示音", action: #selector(toggleMute), keyEquivalent: "")
        mute.target = self; mute.state = muted ? .off : .on; menu.addItem(mute)
        let pause = NSMenuItem(title: pausedUntil == nil ? "暫停提醒 30 分鐘" : "恢復提醒", action: #selector(togglePause), keyEquivalent: "")
        pause.target = self; menu.addItem(pause)
        let demoMenu = NSMenu()
        for (name, kind) in [("完成 · 綠色 / 上行三音", "complete"), ("需要回答 · 琥珀色 / 問答三音", "input"), ("權限請求 · 珊瑚色 / 雙短一長", "permission")] {
            let item = NSMenuItem(title: name, action: #selector(demo(_:)), keyEquivalent: "")
            item.target = self; item.representedObject = kind; demoMenu.addItem(item)
        }
        let tests = NSMenuItem(title: "測試提醒效果", action: nil, keyEquivalent: ""); tests.submenu = demoMenu; menu.addItem(tests)
        let audioMenu = NSMenu()
        for (name, kind) in [("完成 · 上行三音", "complete"), ("需要回答 · 問答三音", "input"), ("權限請求 · 雙短一長", "permission")] {
            let item = NSMenuItem(title: name, action: #selector(testSound(_:)), keyEquivalent: "")
            item.target = self; item.representedObject = kind; item.isEnabled = !muted; audioMenu.addItem(item)
        }
        let audioTests = NSMenuItem(title: "只測試提示音", action: nil, keyEquivalent: ""); audioTests.submenu = audioMenu; menu.addItem(audioTests)
        if !recent.isEmpty {
            menu.addItem(.separator())
            let header = NSMenuItem(title: "最近提醒 · 點擊開啟對話", action: nil, keyEquivalent: "")
            header.isEnabled = false; menu.addItem(header)
            for (index, event) in recent.enumerated() {
                let item = NSMenuItem(title: "\(event.kind == "complete" ? "✓" : "●") \(event.title.prefix(36))", action: #selector(openRecent(_:)), keyEquivalent: "")
                item.target = self; item.tag = index; item.isEnabled = event.provider == "codex" && !event.threadID.isEmpty
                item.toolTip = "\(event.label) · \(event.title)"; menu.addItem(item)
            }
        }
        menu.addItem(.separator())
        let exit = NSMenuItem(title: "結束 VibeCue", action: #selector(quit), keyEquivalent: "q")
        exit.target = self; menu.addItem(exit)
        statusItem.menu = menu
    }

    func writeStatus() {
        let data: [String: Any] = ["pid": ProcessInfo.processInfo.processIdentifier, "muted": muted,
            "paused_until": pausedUntil?.timeIntervalSince1970 ?? 0,
            "current_id": current?.id ?? "", "current_kind": current?.kind ?? "",
            "current_title": current?.title ?? "", "last_shown_title": lastShownTitle,
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
        label("AI 開發提醒助手", in: sidebar, frame: NSRect(x: 20, y: 94, width: 156, height: 18), size: 11, color: .secondaryLabelColor)
        for (index, pair) in [("總覽", "square.grid.2x2"), ("提醒規則", "bell"), ("泛光與聲音", "waveform"), ("集成與檔案", "puzzlepiece.extension")].enumerated() {
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
        label("本機執行 · Codex", in: sidebar, frame: NSRect(x: 20, y: 533, width: 154, height: 19), size: 10.5, color: .secondaryLabelColor)
        label("VibeCue 0.1.0", in: sidebar, frame: NSRect(x: 20, y: 555, width: 154, height: 17), size: 10.5, color: .tertiaryLabelColor)
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
        label("變更會立即儲存到本機。", in: content, frame: NSRect(x: 26, y: 558, width: 525, height: 18), size: 10.5, color: .tertiaryLabelColor)
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

    func toggle(_ title: String, _ detail: String, in parent: NSView, y: CGFloat, key: String, on: Bool) {
        label(title, in: parent, frame: NSRect(x: 20, y: y, width: 425, height: 22), size: 13, weight: .medium)
        if !detail.isEmpty { label(detail, in: parent, frame: NSRect(x: 20, y: y + 25, width: 425, height: 32), size: 11, color: .secondaryLabelColor, lines: 2) }
        let control = NSSwitch(frame: NSRect(x: 467, y: y + 1, width: 40, height: 24))
        control.state = on ? .on : .off; control.target = self; control.action = #selector(changeToggle(_:))
        control.identifier = NSUserInterfaceItemIdentifier(key); control.setAccessibilityLabel(title)
        parent.addSubview(control)
    }

    func rule(_ title: String, _ detail: String, y: CGFloat, key: String, on: Bool, duration: Double, durationKey: String) {
        let view = card(y, 122)
        toggle(title, detail, in: view, y: 17, key: key, on: on)
        label("顯示時間", in: view, frame: NSRect(x: 20, y: 84, width: 120, height: 23), size: 11.5, color: .secondaryLabelColor)
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
        popup.setAccessibilityLabel(title + "顯示時間")
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
        heading("AI 開發提醒助手", "離開對話視窗，也能掌握需要你注意的時刻。")
        let status = card(106, 199)
        label("CODEX · 本機監看", in: status, frame: NSRect(x: 20, y: 17, width: 480, height: 21), size: 11, weight: .semibold, color: .secondaryLabelColor)
        healthLabel = label("", in: status, frame: NSRect(x: 20, y: 50, width: 480, height: 26), size: 18, weight: .semibold)
        threadLabel = label("", in: status, frame: NSRect(x: 20, y: 87, width: 480, height: 24), size: 12, color: .secondaryLabelColor)
        toggle("啟用提醒", "暫停全部泛光、提示卡與聲音。", in: status, y: 130, key: "enabled", on: cue.settings.enabled)
        let latest = card(322, 101)
        label("最近一則提醒", in: latest, frame: NSRect(x: 20, y: 15, width: 480, height: 20), size: 11, color: .secondaryLabelColor)
        latestLabel = label("", in: latest, frame: NSRect(x: 20, y: 45, width: 480, height: 37), size: 13, weight: .medium, lines: 2)
        _ = button("測試完成提醒", in: content, frame: NSRect(x: 25, y: 450, width: 168, height: 32), action: #selector(testAlert(_:)), represented: "complete")
        _ = button("測試等待輸入", in: content, frame: NSRect(x: 205, y: 450, width: 168, height: 32), action: #selector(testAlert(_:)), represented: "input")
        label("測試會使用目前的提醒與音量設定；一般提醒不會搶走輸入焦點。", in: content, frame: NSRect(x: 27, y: 505, width: 520, height: 34), size: 11, color: .secondaryLabelColor, lines: 2)
    }

    func notifications() {
        heading("提醒規則", "按事件決定提醒時機與停留時間。")
        rule("本輪已完成", "目前一輪執行結束；不一定代表整個需求已交付。", y: 106, key: "complete_enabled", on: cue.settings.completeEnabled, duration: cue.settings.completeDuration, durationKey: "complete_duration")
        rule("需要回答或判斷", "Codex 發出結構化問題時，顯示對話名稱。", y: 243, key: "input_enabled", on: cue.settings.inputEnabled, duration: cue.settings.inputDuration, durationKey: "input_duration")
        rule("權限請求", "需要已信任的 hook；自動審核可能會直接處理請求。", y: 380, key: "permission_enabled", on: cue.settings.permissionEnabled, duration: cue.settings.permissionDuration, durationKey: "permission_duration")
        label("普通文字提問不一定產生結構化事件。集成範圍見「集成與檔案」。", in: content, frame: NSRect(x: 27, y: 520, width: 520, height: 25), size: 10.5, color: .secondaryLabelColor)
    }

    func glowAndSound() {
        heading("泛光與聲音", "調整提醒的存在感，保留你的工作節奏。")
        let glow = card(106, 204)
        toggle("螢幕邊緣泛光", "提示卡仍會顯示對話名稱。", in: glow, y: 16, key: "glow_enabled", on: cue.settings.glowEnabled)
        toggle("在所有螢幕顯示泛光", "關閉後只使用滑鼠所在的螢幕。", in: glow, y: 78, key: "all_displays", on: cue.settings.allDisplays)
        slider("泛光亮度", in: glow, y: 140, key: "glow_intensity", value: cue.settings.glowIntensity, min: 0.15)
        let audio = card(325, 212)
        toggle("播放提示音", "使用一般媒體輸出，不修改系統音量或輸出裝置。", in: audio, y: 16, key: "sound_enabled", on: cue.settings.soundEnabled)
        slider("提示音音量", in: audio, y: 82, key: "sound_volume", value: cue.settings.soundVolume)
        for (index, pair) in [("完成音", "complete"), ("等待輸入音", "input"), ("權限請求音", "permission")].enumerated() {
            let test = button("▶ \(pair.0)", in: audio, frame: NSRect(x: 17 + index * 164, y: 157, width: 158, height: 30), action: #selector(testAudio(_:)), represented: pair.1)
            test.isEnabled = cue.settings.soundEnabled
        }
    }

    func providers() {
        heading("集成與檔案", "AI Coding Companion · 目前已接入 Codex。")
        let provider = card(106, 270)
        label("Codex", in: provider, frame: NSRect(x: 20, y: 17, width: 486, height: 27), size: 18, weight: .semibold)
        healthLabel = label("", in: provider, frame: NSRect(x: 20, y: 53, width: 486, height: 21), size: 12, weight: .medium)
        label("完成與結構化問題由本機對話記錄監看。權限請求需要在 Codex 中審閱並信任 PermissionRequest hook；自動審核可能直接處理請求，提醒不代表仍在等待批准。", in: provider, frame: NSRect(x: 20, y: 88, width: 486, height: 73), size: 12, color: .secondaryLabelColor, lines: 4)
        hookLabel = label("", in: provider, frame: NSRect(x: 20, y: 169, width: 486, height: 20), size: 11, color: .secondaryLabelColor)
        _ = button("授權讀取本機 Codex 記錄…", in: provider, frame: NSRect(x: 16, y: 200, width: 283, height: 31), action: #selector(authorizeHistoryRead))
        label("沒有本機記錄的雲端對話不在涵蓋範圍；尚未接入其他 AI 工具。", in: provider, frame: NSRect(x: 20, y: 242, width: 486, height: 23), size: 10.5, color: .tertiaryLabelColor)
        let files = card(392, 145)
        label("本機檔案", in: files, frame: NSRect(x: 20, y: 17, width: 480, height: 24), size: 13, weight: .semibold)
        label(cue.home.path, in: files, frame: NSRect(x: 20, y: 52, width: 486, height: 35), size: 10.5, color: .secondaryLabelColor, lines: 2)
        _ = button("原始碼資料夾", in: files, frame: NSRect(x: 16, y: 96, width: 160, height: 31), action: #selector(openSource))
        _ = button("設定資料夾", in: files, frame: NSRect(x: 181, y: 96, width: 160, height: 31), action: #selector(openConfig))
        _ = button("診斷日誌", in: files, frame: NSRect(x: 346, y: 96, width: 160, height: 31), action: #selector(openLogs))
    }

    func updateHealth() {
        healthLabel?.stringValue = cue.watcherHealthText
        healthLabel?.textColor = cue.watcherHealthText.hasPrefix("本機監看中") ? NSColor(srgbRed: 0.5, green: 0.94, blue: 0.76, alpha: 1) : .systemOrange
        let pause = cue.pausedUntil == nil ? (cue.settings.enabled ? "提醒已啟用" : "提醒已關閉") : "提醒已暫停"
        threadLabel?.stringValue = "\(pause)  ·  \(cue.settings.soundEnabled ? "聲音已啟用" : "聲音已關閉")"
        latestLabel?.stringValue = cue.recent.first.map { "\($0.label) · \($0.title)" } ?? "尚無提醒。可以先測試一則提醒效果。"
        let healthURL = cue.home.appendingPathComponent("hook-health.json")
        let hookData = (try? Data(contentsOf: healthURL)).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        hookLabel?.stringValue = hookData?["PermissionRequest"] == nil ? "權限 hook：尚未觀察到事件；請在 Codex 中確認信任。" : "權限 hook：已觀察到 PermissionRequest 事件。"
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
    @objc func testAlert(_ sender: NSButton) {
        let kind = sender.identifier?.rawValue ?? "complete"
        cue.enqueue(CueEvent(kind: kind, title: "VibeCue · 提醒效果測試", message: "使用你目前的泛光、聲音與顯示時間設定。"))
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

if let index = CommandLine.arguments.firstIndex(of: "--audio-self-test"), CommandLine.arguments.indices.contains(index + 1) {
    let result = CueChime.selfTest(directory: URL(fileURLWithPath: CommandLine.arguments[index + 1], isDirectory: true))
    if let json = try? JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]), let output = String(data: json, encoding: .utf8) { print(output) }
    exit(result.allSatisfy { $0["play_started"] as? Bool == true && $0["is_playing_after_100ms"] as? Bool == true } ? 0 : 1)
}

let app = NSApplication.shared
let controller = CueController()
app.delegate = controller
app.run()
