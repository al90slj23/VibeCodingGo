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
        let protocolUnavailable = fresh && desktop["status"] as? String == "unsupported" && desktop["reason_code"] as? String == "protocol_unsupported"
        attentionConnected = manual["connected"] as? Bool == true || attention
        if reply || control || attention {
            state = "connected"
            var available: [String] = []
            if reply { available.append("回复") }
            if attentionConnected { available.append("批准提醒") }
            if control { available.append("模型设置") }
            summary = "Codex 已自动连接 · " + available.joined(separator: "、") + (!attentionConnected ? " · 批准监听恢复中" : "")
        } else if attentionConnected {
            state = "connected"
            summary = protocolUnavailable ? "Codex 批准监听已连接 · 回复接口暂不兼容，后台会自动检查恢复。" : "Codex 批准监听已连接 · 桌面连接自动恢复中"
        } else {
            // Build changes and stream revision resets are rediscovery events.
            // An explicit method mismatch disables that capability, but its
            // availability is still rechecked without asking for an app update.
            state = protocolUnavailable ? "unsupported" : "reconnecting"
            summary = protocolUnavailable ? "Codex 接口暂不兼容，后台会自动检查恢复；可先在 Codex 操作。" : "Codex 自动重连中，正在重新发现当前连接。"
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
        var partialIncompatibility = incompatibility; partialIncompatibility["manual_approval"] = ["connected": true]
        let partialState = Self(watcher: partial, now: now)
        let incompatibleState = Self(watcher: incompatibility, now: now)
        let incompatiblePartialState = Self(watcher: partialIncompatibility, now: now)
        return ["desktop_connection_health_enables_only_confirmed_live_capabilities": connected.state == "connected" && connected.attentionConnected && connected.summary.contains("回复、批准提醒、模型设置"),
                "stale_watcher_cannot_claim_connected": Self(watcher: stale, now: now).state == "reconnecting",
                "stale_desktop_cannot_claim_connected": Self(watcher: staleDesktop, now: now).state == "reconnecting",
                "socket_without_capabilities_cannot_claim_connected": Self(watcher: missing, now: now).state == "reconnecting",
                "reply_connection_never_impersonates_approval_listener": partialState.state == "connected" && !partialState.attentionConnected && partialState.summary.contains("批准监听恢复中"),
                "revision_reset_never_asks_user_to_repair_a_protocol": Self(watcher: revisionReset, now: now).state == "reconnecting" && Self(watcher: revisionReset, now: now).summary.contains("自动重连") && !Self(watcher: revisionReset, now: now).summary.contains("早于"),
                "only_explicit_protocol_incompatibility_exposes_diagnostics": incompatibleState.state == "unsupported",
                "protocol_mismatch_is_rechecked_without_prescribing_an_app_update": incompatibleState.summary.contains("自动检查恢复") && !incompatibleState.summary.contains("更新"),
                "working_approval_listener_survives_reply_protocol_mismatch": incompatiblePartialState.state == "connected" && incompatiblePartialState.attentionConnected && incompatiblePartialState.summary.contains("回复接口暂不兼容") && !incompatiblePartialState.summary.contains("已自动连接")]
    }
}
