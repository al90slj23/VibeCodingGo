import AppKit
import Foundation
import CoreFoundation

/// One geometry for all conversation viewers. Coordinates are AppKit points,
/// so moving between Retina and non-Retina displays needs no pixel conversion.
/// This file contains no thread identifiers, drafts or conversation content.
final class CueConversationWindowState {
    private static let registryLock = NSLock()
    private static var registry: [String: CueConversationWindowState] = [:]
    static func shared(home: URL) -> CueConversationWindowState {
        let key = home.standardizedFileURL.path
        registryLock.lock(); defer { registryLock.unlock() }
        if let saved = registry[key] { return saved }
        let saved = CueConversationWindowState(home: home)
        registry[key] = saved
        return saved
    }

    let file: URL
    private let lock = NSLock()
    private var latest: NSRect?
    private var dirty = false
    private var saveWork: DispatchWorkItem?

    init(home: URL) {
        file = home.appendingPathComponent("conversation-window.json")
        guard let size = try? file.resourceValues(forKeys: [.fileSizeKey]).fileSize, size <= 4096,
              let bytes = try? Data(contentsOf: file), bytes.count <= 4096,
              let raw = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any],
              Self.number(raw["version"]) == 1,
              let x = Self.number(raw["x"]), let y = Self.number(raw["y"]),
              let width = Self.number(raw["width"]), let height = Self.number(raw["height"]) else { return }
        let frame = NSRect(x: x, y: y, width: width, height: height)
        if Self.valid(frame) { latest = frame }
    }
    deinit { saveWork?.cancel() }

    func restoredFrame(screens: [NSRect], minimum: NSSize) -> NSRect? {
        lock.lock(); let frame = latest; lock.unlock()
        return frame.flatMap { Self.clamped($0, screens: screens, minimum: minimum) }
    }

    /// Update the shared in-memory value immediately, so another viewer opened
    /// during the short disk debounce already sees the most recent adjustment.
    @discardableResult func record(_ frame: NSRect) -> Bool {
        guard Self.valid(frame) else { return false }
        lock.lock()
        guard latest != frame else { lock.unlock(); return true }
        latest = frame; dirty = true; saveWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.flush() }
        saveWork = work
        lock.unlock()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2, execute: work)
        return true
    }

    /// Any closing window may flush, but can never substitute its older frame.
    @discardableResult func flush() -> Bool {
        lock.lock(); defer { lock.unlock() }
        saveWork?.cancel(); saveWork = nil
        guard dirty, let frame = latest else { return true }
        do {
            let raw: [String: Any] = ["version": 1, "x": frame.minX, "y": frame.minY,
                                      "width": frame.width, "height": frame.height]
            let bytes = try JSONSerialization.data(withJSONObject: raw, options: [.sortedKeys])
            try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            try bytes.write(to: file, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
            dirty = false; return true
        } catch { return false }
    }

    static var visibleScreens: [NSRect] {
        let main = NSScreen.main?.visibleFrame
        return ([main].compactMap { $0 } + NSScreen.screens.map(\.visibleFrame)).reduce(into: []) { result, frame in
            if !result.contains(frame) { result.append(frame) }
        }
    }

    static func clamped(_ frame: NSRect, screens: [NSRect], minimum: NSSize) -> NSRect? {
        guard valid(frame) else { return nil }
        let available = screens.filter { valid($0) }
        guard var screen = available.first else { return nil }
        var overlap: CGFloat = -1
        for candidate in available {
            let intersection = frame.intersection(candidate)
            let area = intersection.isNull ? 0 : intersection.width * intersection.height
            if area > overlap { screen = candidate; overlap = area }
        }
        let width = min(screen.width, max(minimum.width, frame.width))
        let height = min(screen.height, max(minimum.height, frame.height))
        return NSRect(x: min(max(frame.minX, screen.minX), screen.maxX - width),
                      y: min(max(frame.minY, screen.minY), screen.maxY - height),
                      width: width, height: height)
    }

    private static func valid(_ frame: NSRect) -> Bool {
        [frame.minX, frame.minY, frame.width, frame.height].allSatisfy(\.isFinite) &&
        abs(frame.minX) <= 1_000_000 && abs(frame.minY) <= 1_000_000 &&
        frame.width > 0 && frame.height > 0 && frame.width <= 20_000 && frame.height <= 20_000
    }
    private static func number(_ raw: Any?) -> Double? {
        guard let value = raw as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID() else { return nil }
        return value.doubleValue.isFinite ? value.doubleValue : nil
    }

    static func validationReport() -> [String: Bool] {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent("vibecue-window-state-" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        let screen = NSRect(x: 0, y: 0, width: 1920, height: 1055), minimum = NSSize(width: 640, height: 608)
        let first = shared(home: home), second = shared(home: home)
        let desired = NSRect(x: 160, y: 120, width: 1100, height: 820)
        first.record(desired)
        var results = ["conversation_geometry_is_shared_before_disk_debounce": second.restoredFrame(screens: [screen], minimum: minimum) == desired]
        first.flush()
        results["conversation_geometry_survives_a_new_store_instance"] = CueConversationWindowState(home: home).restoredFrame(screens: [screen], minimum: minimum) == desired
        let newer = NSRect(x: 280, y: 160, width: 1200, height: 850)
        second.record(newer); first.flush()
        results["closing_an_older_viewer_flushes_the_latest_shared_geometry"] = CueConversationWindowState(home: home).restoredFrame(screens: [screen], minimum: minimum) == newer
        let external = NSRect(x: -1800, y: 80, width: 1000, height: 800)
        results["conversation_geometry_preserves_negative_coordinate_display"] = clamped(external, screens: [screen, NSRect(x: -1920, y: 0, width: 1920, height: 1055)], minimum: minimum) == external
        let moved = clamped(external, screens: [screen], minimum: minimum)
        results["conversation_geometry_removed_display_moves_back_onscreen"] = moved.map { screen.contains($0) && $0.size == external.size } == true
        let smallScreen = NSRect(x: 0, y: 0, width: 600, height: 500)
        let fitted = clamped(NSRect(x: 3000, y: 4000, width: 1800, height: 1300), screens: [smallScreen], minimum: minimum)
        results["conversation_geometry_large_window_fits_smaller_display"] = fitted == smallScreen
        let undersized = clamped(NSRect(x: 10, y: 20, width: 100, height: 100), screens: [screen], minimum: minimum)
        results["conversation_geometry_restores_the_minimum_readable_size"] = undersized?.size == minimum
        let permissions = (try? FileManager.default.attributesOfItem(atPath: first.file.path)[.posixPermissions]) as? NSNumber
        results["conversation_geometry_state_is_private_and_contains_only_frame"] = permissions?.intValue == 0o600 && ((try? Data(contentsOf: first.file)).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] })?.count == 5
        let badHome = home.appendingPathComponent("invalid")
        try? FileManager.default.createDirectory(at: badHome, withIntermediateDirectories: true)
        let file = badHome.appendingPathComponent("conversation-window.json"), bad = Data("{\"version\":1,\"x\":0,\"y\":false,\"width\":1000,\"height\":800}".utf8)
        try? bad.write(to: file)
        let invalid = CueConversationWindowState(home: badHome)
        invalid.flush()
        results["invalid_conversation_geometry_is_ignored_without_overwriting_it"] = invalid.restoredFrame(screens: [screen], minimum: minimum) == nil && (try? Data(contentsOf: file)) == bad
        return results
    }
}
