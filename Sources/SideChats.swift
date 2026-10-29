import AppKit
import Foundation

/// A read-only relation verified by the live Codex owner. This never creates
/// an alert, follow-up, transcript, or independent task-list entry.
struct CueSideChat {
    static let verifiedSource = "codex-desktop-owner-stream"
    let threadID: String
    let parentThreadID: String
    let title: String
    let source: String
    let observedAt: Date
    let verifiedAt: Date
    let expiresAt: Date
    private let rawStatus: String
    private let allowsOpen: Bool

    init?(raw: [String: Any]) {
        guard let id = raw["thread_id"] as? String, CueThreadFollowups.valid(id),
              let parent = raw["parent_thread_id"] as? String, CueThreadFollowups.valid(parent), parent != id,
              raw["ephemeral"] as? Bool == true,
              raw["source"] as? String == Self.verifiedSource,
              let observed = Self.date(raw["observed_at"]),
              let verified = Self.date(raw["verified_at"]),
              let expires = Self.date(raw["expires_at"]) else { return nil }
        threadID = id; parentThreadID = parent; source = Self.verifiedSource
        observedAt = observed; verifiedAt = verified; expiresAt = expires
        let name = (raw["title"] as? String ?? "").split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        title = name.isEmpty ? "侧边聊天 · " + String(id.prefix(8)) : String(name.prefix(180))
        rawStatus = raw["status"] as? String ?? "unavailable"
        allowsOpen = raw["can_open"] as? Bool == true
    }

    private static func date(_ raw: Any?) -> Date? {
        if let value = raw as? Double, value.isFinite, value > 0 { return Date(timeIntervalSince1970: value) }
        guard let value = raw as? String else { return nil }
        let formatter = ISO8601DateFormatter(); formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    }
    private func fresh(at now: Date) -> Bool {
        verifiedAt <= now.addingTimeInterval(300) && observedAt <= now.addingTimeInterval(300) && expiresAt > now
    }
    func status(at now: Date = Date()) -> String {
        guard fresh(at: now), ["running", "waiting", "completed", "idle", "interrupted"].contains(rawStatus) else { return "unavailable" }
        return rawStatus
    }
    var canOpen: Bool { canOpen(at: Date()) }
    func canOpen(at now: Date) -> Bool { allowsOpen && status(at: now) != "unavailable" }
    var statusLabel: String {
        ["running": "运行中", "waiting": "等待处理", "completed": "已完成", "idle": "待续", "interrupted": "已中断"][status()] ?? "暂不可用"
    }
    var statusColor: NSColor {
        switch status() {
        case "running": return .systemGreen
        case "waiting", "interrupted": return .systemOrange
        case "idle": return .systemPurple
        default: return .secondaryLabelColor
        }
    }

    /// Parent IDs come only from the verified relation. Shared projects,
    /// collections, and inherited titles never imply a parent-child relation.
    static func grouped(raw: [String: Any], now: Date = Date()) -> [String: [CueSideChat]] {
        guard let groups = raw["by_parent"] as? [String: Any] else { return [:] }
        var candidates: [CueSideChat] = []
        let disconnected = raw["state"] as? String != "connected"
        for (parent, value) in groups where CueThreadFollowups.valid(parent) {
            for row in value as? [[String: Any]] ?? [] {
                var observed = row
                if disconnected { observed["status"] = "disconnected"; observed["can_open"] = false }
                guard let child = CueSideChat(raw: observed), child.parentThreadID == parent,
                      child.verifiedAt <= now.addingTimeInterval(300), child.observedAt <= now.addingTimeInterval(300) else { continue }
                candidates.append(child)
            }
        }
        // An ambiguous child is withheld rather than attributed to two parents.
        let parents = Dictionary(grouping: candidates, by: \.threadID).mapValues { Set($0.map(\.parentThreadID)) }
        var unique: [String: CueSideChat] = [:]
        for child in candidates where parents[child.threadID]?.count == 1 {
            if let previous = unique[child.threadID], previous.verifiedAt > child.verifiedAt || (previous.verifiedAt == child.verifiedAt && previous.observedAt >= child.observedAt) { continue }
            unique[child.threadID] = child
        }
        return Dictionary(grouping: unique.values, by: \.parentThreadID).mapValues {
            $0.sorted { $0.observedAt == $1.observedAt ? $0.threadID < $1.threadID : $0.observedAt < $1.observedAt }
        }
    }

    static func fixtureRaw(parent: String, child: String, title: String, status: String, now: Date = Date()) -> [String: Any] {
        ["thread_id": child, "parent_thread_id": parent, "title": title, "status": status, "source": verifiedSource,
         "observed_at": now.timeIntervalSince1970, "verified_at": now.timeIntervalSince1970,
         "expires_at": now.addingTimeInterval(60).timeIntervalSince1970, "ephemeral": true, "can_open": true]
    }

    static func validationReport() -> [String: Bool] {
        let now = Date()
        let valid = fixtureRaw(parent: "parent", child: "child", title: "侧聊", status: "running", now: now)
        var wrongSource = valid; wrongSource["source"] = "collection-membership"
        var durable = valid; durable["ephemeral"] = false
        var unverified = valid; unverified.removeValue(forKey: "verified_at")
        var stale = valid; stale["expires_at"] = now.addingTimeInterval(-1).timeIntervalSince1970
        var unknown = valid; unknown["status"] = "new-provider-state"
        var conflict = valid; conflict["parent_thread_id"] = "another-parent"
        let grouped = Self.grouped(raw: ["state": "connected", "by_parent": ["parent": [valid, valid]]], now: now)
        return [
            "side_chat_requires_verified_owner_relation": Self(raw: wrongSource) == nil && Self(raw: durable) == nil && Self(raw: unverified) == nil,
            "side_chat_collection_membership_never_infers_parent": Self.grouped(raw: ["collection_id": "parent", "threads": [valid]], now: now).isEmpty,
            "side_chat_rejects_mismatched_parent_group": Self.grouped(raw: ["state": "connected", "by_parent": ["wrong-parent": [valid]]], now: now).isEmpty,
            "side_chat_deduplicates_verified_child": grouped["parent"]?.count == 1,
            "side_chat_withholds_conflicting_parent_relations": Self.grouped(raw: ["state": "connected", "by_parent": ["parent": [valid], "another-parent": [conflict]]], now: now).isEmpty,
            "side_chat_missing_live_evidence_never_means_completed": Self(raw: stale)?.status(at: now) == "unavailable" && Self(raw: stale)?.canOpen(at: now) == false,
            "side_chat_unknown_status_is_unavailable": Self(raw: unknown)?.status(at: now) == "unavailable" && Self(raw: unknown)?.canOpen(at: now) == false,
            "side_chat_disconnected_snapshot_preserves_relation_without_navigation": Self.grouped(raw: ["state": "disconnected", "by_parent": ["parent": [valid]]], now: now)["parent"]?.first.map { $0.status(at: now) == "unavailable" && !$0.canOpen(at: now) } == true,
            "side_chat_unknown_snapshot_state_disables_navigation": Self.grouped(raw: ["state": "future-owner-state", "by_parent": ["parent": [valid]]], now: now)["parent"]?.first?.canOpen(at: now) == false,
        ]
    }
}

/// Kept separate from a task row's navigation and batch-selection targets.
final class CueSideChatButton: NSButton {
    var invoke: (() -> Void)?
    init() {
        super.init(frame: .zero)
        identifier = NSUserInterfaceItemIdentifier("side-chats")
        isBordered = false; alignment = .right; font = .systemFont(ofSize: 10, weight: .medium)
        contentTintColor = .controlAccentColor; setButtonType(.momentaryPushIn)
        target = self; action = #selector(trigger)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    @objc private func trigger() { invoke?() }
}

private final class CueSideChatOpenButton: NSButton {
    var invoke: (() -> Void)?
    init() {
        super.init(frame: .zero)
        title = "在 Codex 打开"; bezelStyle = .rounded; controlSize = .small
        target = self; action = #selector(trigger)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    @objc private func trigger() { invoke?() }
}

private final class CueSideChatsContentController: NSViewController {
    var chatRows: [CueSideChat] = []
    var parentTitle = ""
    var open: ((CueSideChat) -> Void)?
    override func loadView() {
        let root = CueFlippedView(frame: NSRect(x: 0, y: 0, width: 470, height: 240)); root.backgroundColor = .windowBackgroundColor
        view = root; rebuild()
    }
    func update(title: String, children: [CueSideChat]) {
        parentTitle = title; self.chatRows = children
        if isViewLoaded { rebuild() }
    }
    private func rebuild() {
        view.subviews.forEach { $0.removeFromSuperview() }
        let viewport = min(CGFloat(max(1, chatRows.count)) * 72, 360)
        view.frame.size = NSSize(width: 470, height: viewport + 94)
        func label(_ text: String, frame: NSRect, size: CGFloat, color: NSColor, weight: NSFont.Weight = .regular) {
            let field = NSTextField(labelWithString: text); field.frame = frame; field.font = .systemFont(ofSize: size, weight: weight)
            field.textColor = color; field.lineBreakMode = .byTruncatingTail; view.addSubview(field)
        }
        label("侧边聊天 \(chatRows.count)", frame: NSRect(x: 18, y: 13, width: 434, height: 22), size: 14, color: .labelColor, weight: .semibold)
        label(parentTitle, frame: NSRect(x: 18, y: 38, width: 434, height: 17), size: 11, color: .secondaryLabelColor)
        let scroll = NSScrollView(frame: NSRect(x: 10, y: 61, width: 450, height: viewport))
        scroll.hasVerticalScroller = true; scroll.drawsBackground = false; scroll.autohidesScrollers = true
        let document = CueFlippedView(frame: NSRect(x: 0, y: 0, width: 450, height: CGFloat(chatRows.count) * 72))
        for (index, child) in chatRows.enumerated() {
            let y = CGFloat(index) * 72
            let title = NSTextField(labelWithString: child.title); title.frame = NSRect(x: 12, y: y + 8, width: 308, height: 20)
            title.font = .systemFont(ofSize: 12, weight: .medium); title.lineBreakMode = .byTruncatingTail; title.toolTip = child.title; document.addSubview(title)
            let status = NSTextField(labelWithString: child.statusLabel + " · " + String(child.threadID.prefix(8)))
            status.frame = NSRect(x: 12, y: y + 33, width: 308, height: 18); status.font = .systemFont(ofSize: 10.5)
            status.textColor = child.statusColor; status.identifier = NSUserInterfaceItemIdentifier("side-chat-status:" + child.threadID); document.addSubview(status)
            let button = CueSideChatOpenButton(); button.frame = NSRect(x: 326, y: y + 19, width: 110, height: 26)
            button.isEnabled = child.canOpen; button.identifier = NSUserInterfaceItemIdentifier("side-chat-open:" + child.threadID)
            button.toolTip = button.isEnabled ? "打开此侧边聊天" : "等待 Codex 重新验证此侧边聊天"
            button.setAccessibilityLabel("在 Codex 打开侧边聊天：" + child.title)
            button.invoke = { [weak self] in guard child.canOpen else { return }; self?.open?(child) }
            document.addSubview(button)
            let separator = NSBox(frame: NSRect(x: 12, y: y + 69, width: 424, height: 1)); separator.boxType = .separator; document.addSubview(separator)
        }
        scroll.documentView = document; view.addSubview(scroll)
        label("临时对话 · 状态来自 Codex", frame: NSRect(x: 18, y: viewport + 68, width: 434, height: 16), size: 10, color: .tertiaryLabelColor)
    }
}

final class CueSideChatsPopoverController {
    private let popover = NSPopover()
    private let content = CueSideChatsContentController()
    private(set) var parentThreadID: String?
    var isShown: Bool { popover.isShown }
    init() {
        popover.behavior = .semitransient; popover.animates = false; popover.contentViewController = content
    }
    func show(parentID: String, parentTitle: String, children: [CueSideChat], anchor: NSView, open: @escaping (CueSideChat) -> Void) {
        if isShown && parentThreadID == parentID { close(); return }
        close(); guard !children.isEmpty else { return }
        parentThreadID = parentID; content.update(title: parentTitle, children: children)
        content.open = { [weak self] child in self?.close(); open(child) }
        _ = content.view; popover.contentSize = content.view.frame.size
        popover.show(relativeTo: anchor.bounds, of: anchor, preferredEdge: .maxY)
    }
    func refresh(groups: [String: [CueSideChat]]) {
        guard isShown, let id = parentThreadID else { return }
        guard let children = groups[id], !children.isEmpty else { close(); return }
        content.update(title: content.parentTitle, children: children); popover.contentSize = content.view.frame.size
    }
    func close() { popover.performClose(nil); parentThreadID = nil; content.open = nil }

    static func validationReport() -> [String: Bool] {
        let now = Date()
        let active = CueSideChat(raw: CueSideChat.fixtureRaw(parent: "parent", child: "active-child", title: "运行中的侧边聊天", status: "running", now: now))!
        let complete = CueSideChat(raw: CueSideChat.fixtureRaw(parent: "parent", child: "complete-child", title: "已完成的侧边聊天", status: "completed", now: now))!
        let content = CueSideChatsContentController()
        content.update(title: "父对话", children: [active, complete]); _ = content.view
        func document() -> NSView? { content.view.subviews.compactMap { $0 as? NSScrollView }.first?.documentView }
        let labels = document()?.subviews.compactMap { $0 as? NSTextField }.filter { $0.identifier?.rawValue.hasPrefix("side-chat-status:") == true }.map(\.stringValue) ?? []
        var opened: [String] = []; content.open = { opened.append($0.threadID) }
        let buttons = document()?.subviews.compactMap { $0 as? CueSideChatOpenButton } ?? []
        buttons.last?.invoke?()
        var disconnected = CueSideChat.fixtureRaw(parent: "parent", child: "active-child", title: active.title, status: "disconnected", now: now)
        disconnected["can_open"] = false
        content.update(title: "父对话", children: [CueSideChat(raw: disconnected)!])
        let disabled = document()?.subviews.compactMap { $0 as? CueSideChatOpenButton }.first
        disabled?.invoke?()
        return [
            "side_chat_native_panel_shows_individual_live_and_completed_states": labels.contains("运行中 · active-c") && labels.contains("已完成 · complete"),
            "side_chat_native_panel_open_button_targets_only_selected_child": buttons.count == 2 && buttons.allSatisfy { $0.title == "在 Codex 打开" } && opened == ["complete-child"],
            "side_chat_native_panel_disconnected_button_is_disabled_and_inert": disabled?.isEnabled == false && opened == ["complete-child"],
        ]
    }

    /// Renders the same native content as the anchored panel, without opening
    /// Codex or touching the real task ledger.
    static func captureFixture(to output: URL, dark: Bool = true) -> Bool {
        _ = NSApplication.shared
        let now = Date()
        let content = CueSideChatsContentController()
        content.update(title: "VibeCoding Go｜任务运行状态与提醒列表", children: [
            CueSideChat(raw: CueSideChat.fixtureRaw(parent: "fixture-parent", child: "fixture-side-running", title: "核对侧边聊天入口与状态", status: "running", now: now))!,
            CueSideChat(raw: CueSideChat.fixtureRaw(parent: "fixture-parent", child: "fixture-side-completed", title: "确认原生按钮与列表布局", status: "completed", now: now))!,
        ])
        let view = content.view
        let window = NSWindow(contentRect: view.bounds, styleMask: .borderless, backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false; window.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        window.contentView = view; window.setFrameOrigin(NSPoint(x: -10000, y: -10000)); window.orderFrontRegardless(); window.displayIfNeeded()
        defer { window.orderOut(nil) }
        guard let representation = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return false }
        view.effectiveAppearance.performAsCurrentDrawingAppearance { view.cacheDisplay(in: view.bounds, to: representation) }
        guard let data = representation.representation(using: .png, properties: [:]) else { return false }
        do { try data.write(to: output); return true } catch { return false }
    }
}
