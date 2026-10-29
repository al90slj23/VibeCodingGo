import Foundation

/// A live capability summary. An old launch receipt or the existence of a
/// socket cannot by itself mean that replies, approvals and settings work.
struct CueCodexConnection {
    let state: String
    let summary: String
    let attentionConnected: Bool
    init(watcher: [String: Any], now: Date = Date()) {
        func recent(_ value: Any?, within seconds: Double) -> Bool {
            guard let value = value as? Double, value.isFinite else { return false }
            return (-2...seconds).contains(now.timeIntervalSince1970 - value)
        }
        guard watcher["running"] as? Bool == true, recent(watcher["heartbeat"], within: 15) else {
            state = "reconnecting"; summary = "Codex 连接恢复中，后台会自动重试。"; attentionConnected = false; return
        }
        let manual = watcher["manual_approval"] as? [String: Any] ?? [:]
        let desktop = watcher["desktop_connection"] as? [String: Any] ?? [:]
        let fresh = recent(desktop["last_seen"], within: 20)
        let capabilities = desktop["capabilities"] as? [String: Any] ?? [:]
        let attached = fresh && desktop["connected"] as? Bool == true && desktop["status"] as? String == "connected"
        let reply = attached && capabilities["reply"] as? Bool == true
        let control = attached && capabilities["control"] as? Bool == true
        let attention = attached && capabilities["attention"] as? Bool == true
        attentionConnected = manual["connected"] as? Bool == true || attention
        if reply || control || attention {
            state = "connected"
            var available: [String] = []
            if reply { available.append("回复") }
            if attentionConnected { available.append("批准提醒") }
            if control { available.append("批量设置") }
            summary = "Codex 已自动连接 · " + available.joined(separator: "、") + (!attentionConnected ? " · 批准监听恢复中" : "")
        } else if attentionConnected {
            state = "connected"; summary = "Codex 批准监听已连接 · 桌面连接自动恢复中"
        } else {
            // A stream revision reset is normal after owner/cache replacement.
            // Only the transport's explicit incompatibility code needs setup.
            state = fresh && desktop["status"] as? String == "unsupported" && desktop["reason_code"] as? String == "protocol_unsupported" ? "unsupported" : "reconnecting"
            summary = state == "unsupported" ? String((desktop["message"] as? String ?? "当前 Codex 版本需要适配，请更新 VibeCue。").prefix(180)) : "Codex 自动重连中，正在重新发现当前连接。"
        }
    }
    static func validationReport() -> [String: Bool] {
        let now = Date(), stamp = now.timeIntervalSince1970
        let desktop: [String: Any] = ["connected": true, "status": "connected", "last_seen": stamp, "capabilities": ["reply": true, "attention": true, "control": true]]
        let watcher: [String: Any] = ["running": true, "heartbeat": stamp, "desktop_connection": desktop]
        let connected = Self(watcher: watcher, now: now)
        var partial = watcher; var partialDesktop = desktop; partialDesktop["capabilities"] = ["reply": true, "attention": false, "control": false]; partial["desktop_connection"] = partialDesktop
        var stale = watcher; stale["heartbeat"] = stamp - 25
        var staleDesktop = watcher; var old = desktop; old["last_seen"] = stamp - 40; staleDesktop["desktop_connection"] = old
        var missing = watcher; var noCapabilities = desktop; noCapabilities["capabilities"] = [:]; missing["desktop_connection"] = noCapabilities
        var revisionReset = watcher; revisionReset["desktop_connection"] = ["connected":false, "status":"unsupported", "last_seen":stamp, "message":"对话订阅快照早于已观察版本"]
        var incompatibility = watcher; incompatibility["desktop_connection"] = ["connected":false, "status":"unsupported", "reason_code":"protocol_unsupported", "last_seen":stamp, "message":"当前 Codex 协议需要适配"]
        let partialState = Self(watcher: partial, now: now)
        return ["desktop_connection_health_enables_only_confirmed_live_capabilities": connected.state == "connected" && connected.attentionConnected && connected.summary.contains("回复、批准提醒、批量设置"),
                "stale_watcher_cannot_claim_connected": Self(watcher: stale, now: now).state == "reconnecting",
                "stale_desktop_cannot_claim_connected": Self(watcher: staleDesktop, now: now).state == "reconnecting",
                "socket_without_capabilities_cannot_claim_connected": Self(watcher: missing, now: now).state == "reconnecting",
                "reply_connection_never_impersonates_approval_listener": partialState.state == "connected" && !partialState.attentionConnected && partialState.summary.contains("批准监听恢复中"),
                "revision_reset_never_asks_user_to_repair_a_protocol": Self(watcher: revisionReset, now: now).state == "reconnecting" && Self(watcher: revisionReset, now: now).summary.contains("自动重连") && !Self(watcher: revisionReset, now: now).summary.contains("早于"),
                "only_explicit_protocol_incompatibility_exposes_diagnostics": Self(watcher: incompatibility, now: now).state == "unsupported"]
    }
}
