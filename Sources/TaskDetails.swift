import AppKit
import Foundation
import CoreFoundation

struct CueTaskMessage {
    let role: String
    let text: String
    let date: Date?
}

/// A backend-provided usage scope. Current settings never participate in pricing.
struct CueTaskUsageSummary {
    let totalTokens: Int64?
    let inputTokens: Int64?
    let outputTokens: Int64?
    let cacheReadTokens: Int64?
    let cacheCreationTokens: Int64?
    let requestCount: Int64?
    let tokensStatus: String
    let costStatus: String
    let currency: String
    let provenance: String
    let updatedAt: Date?
    let stale: Bool
    private let money: [String: Any]
    private let pricedRequests: Int64?
    private let unpricedRequests: Int64?
    private let priceSources: [String]
    private let pricingStale: Bool
    private let fx: [String: Any]
    private let message: String
    private let legacy: Bool
    private let history: Bool

    init(raw: [String: Any], history: Bool = false, legacy: Bool = false) {
        totalTokens = CueTaskDetail.integer(raw["total_tokens"])
        inputTokens = CueTaskDetail.integer(raw["input_tokens"])
        outputTokens = CueTaskDetail.integer(raw["output_tokens"])
        cacheReadTokens = CueTaskDetail.integer(raw["cache_read_tokens"])
        cacheCreationTokens = CueTaskDetail.integer(raw["cache_creation_tokens"])
        requestCount = CueTaskDetail.integer(raw["request_count"])
        let declaredTokens = CueTaskDetail.label(raw["tokens_status"], limit: 40)
        tokensStatus = totalTokens == nil || declaredTokens == "unknown" ? "unknown" : (["complete", "partial"].contains(declaredTokens) ? declaredTokens : "partial")
        var amounts: [String: Any] = [:]
        for key in ["cost_usd", "cost_cny"] {
            if let value = raw[key] as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(), value.doubleValue.isFinite, value.doubleValue >= 0 { amounts[key] = value }
        }
        money = amounts
        let declaredCost = CueTaskDetail.label(raw["cost_status"], limit: 40)
        costStatus = ["unknown", "unpriced"].contains(declaredCost) ? declaredCost : (amounts.isEmpty ? "unknown" : (declaredCost == "complete" ? "complete" : "partial"))
        let declaredCurrency = CueTaskDetail.label(raw["currency"], limit: 10)
        currency = ["USD", "CNY", "both"].contains(declaredCurrency) ? declaredCurrency : "CNY"
        provenance = CueTaskDetail.label(raw["provenance"], limit: 80)
        updatedAt = CueTaskDetail.date(raw["updated_at"])
        stale = raw["stale"] as? Bool == true
        pricedRequests = CueTaskDetail.integer(raw["priced_requests"])
        unpricedRequests = CueTaskDetail.integer(raw["unpriced_requests"])
        priceSources = (raw["price_sources"] as? [String] ?? []).prefix(12).map { CueTaskDetail.label($0, limit: 100) }.filter { !$0.isEmpty }
        pricingStale = raw["pricing_stale"] as? Bool == true
        fx = raw["fx"] as? [String: Any] ?? [:]
        message = CueTaskDetail.label(raw["message"], limit: 360)
        self.legacy = legacy
        self.history = history
    }

    var tokenLine: String {
        guard let total = totalTokens, tokensStatus != "unknown" else { return "— Tokens" }
        return (tokensStatus == "partial" ? "≥" : "") + CueTaskDetail.compact(total) + " Tokens"
    }

    /// All money remains in full base units. A star denotes a known subtotal.
    var costLine: String {
        if costStatus == "unpriced" { return "未定价" }
        guard costStatus != "unknown", !money.isEmpty else { return "—" }
        let formatted = CueUsageMoney.display(money, currency: currency)
        let amount = formatted.replacingOccurrences(of: " / ", with: "\n") + (formatted.contains("$") ? " USD" : "")
        return amount + (costStatus == "partial" && !["未定价", "汇率未获取"].contains(formatted) ? " *" : "")
    }

    var tooltip: String {
        func exact(_ value: Int64?) -> String { value.map(CueTaskDetail.exact) ?? "—" }
        var lines = [history ? "历史累计 · 已同步账本" : "本轮用量",
                     "总 Tokens：" + (tokensStatus == "unknown" ? "—（尚未取得）" : (tokensStatus == "partial" ? "≥" : "") + exact(totalTokens)),
                     "新增输入 \(exact(inputTokens)) · 输出 \(exact(outputTokens))",
                     "缓存读取 \(exact(cacheReadTokens)) · 缓存写入 \(exact(cacheCreationTokens))"]
        if let count = requestCount { lines.append("请求记录：" + CueTaskDetail.exact(count)) }
        if costStatus == "unknown" { lines.append("API 参考费用：—（尚未取得）") }
        else if costStatus == "unpriced" { lines.append("API 参考费用：未定价") }
        else { lines.append("API 参考费用：" + CueUsageMoney.display(money, currency: "both")) }
        if tokensStatus == "partial" { lines.append("Tokens 为已确认部分，累计值可能继续增加。") }
        if costStatus == "partial" { lines.append("* 费用仅为已定价小计，仍有未计入或未定价用量。") }
        if let priced = pricedRequests, let unpriced = unpricedRequests {
            lines.append("已定价 / 未定价记录：\(CueTaskDetail.exact(priced)) / \(CueTaskDetail.exact(unpriced))")
        }
        if history { lines.append("仅含已成功同步到账本的对话记录；未同步的实时增量尚未计入。") }
        if let updated = updatedAt {
            let formatter = DateFormatter(); formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
            lines.append((history ? "最近账本同步：" : "最近用量记录：") + formatter.string(from: updated))
        } else if history { lines.append("最近账本同步：尚未取得") }
        if stale { lines.append("当前为缓存快照，可能尚未反映最新用量。") }
        if !money.isEmpty || costStatus == "unpriced" {
            lines.append("按记录中的模型及当前生效 API 单价估算，非订阅实际扣费。")
        }
        if !priceSources.isEmpty { lines.append("价格来源：" + priceSources.joined(separator: "、")) }
        if pricingStale { lines.append("价格来源为缓存，可能已过期。") }
        if let rate = fx["rate"] as? NSNumber, CFGetTypeID(rate) != CFBooleanGetTypeID(), rate.doubleValue.isFinite, rate.doubleValue > 0 {
            lines.append("1 USD = \(rate.stringValue) CNY · " + CueTaskDetail.label(fx["source"], limit: 100) + " " + CueTaskDetail.label(fx["date"], limit: 30))
            if fx["stale"] as? Bool == true { lines.append("换算汇率为缓存，可能已过期。") }
        } else if money["cost_usd"] != nil, money["cost_cny"] == nil, currency != "USD" { lines.append("人民币换算汇率尚未取得。") }
        if legacy { lines.append("旧版状态仅提供轮次 Tokens；本轮费用与历史累计尚未取得。") }
        if !message.isEmpty { lines.append(message) }
        return lines.joined(separator: "\n")
    }

    static func validationReport() -> [String: Bool] {
        let unknown = CueTaskUsageSummary(raw: [:])
        let zero = CueTaskUsageSummary(raw: ["total_tokens": 0, "tokens_status": "complete", "cost_usd": 0, "cost_cny": 0, "cost_status": "complete", "currency": "CNY"])
        let partial = CueTaskUsageSummary(raw: ["total_tokens": 12345, "tokens_status": "partial", "cost_usd": 0.003, "cost_status": "partial", "currency": "USD", "priced_requests": 1, "unpriced_requests": 2])
        let both = CueTaskUsageSummary(raw: ["cost_usd": 12345.67, "cost_cny": 1234567.89, "cost_status": "complete", "currency": "both"])
        let invalid = CueTaskUsageSummary(raw: ["total_tokens": true, "input_tokens": -1, "output_tokens": 0.5, "cost_usd": Double.nan, "cost_cny": -1, "cost_status": "complete"])
        let unpriced = CueTaskUsageSummary(raw: ["total_tokens": 50, "tokens_status": "complete", "cost_status": "unpriced"])
        let noFX = CueTaskUsageSummary(raw: ["cost_usd": 1, "cost_status": "complete", "currency": "CNY"])
        let history = CueTaskUsageSummary(raw: ["total_tokens": 2000000, "tokens_status": "partial", "updated_at": 1793170000, "stale": true], history: true)
        let legacy = CueTaskDetail(raw: ["total_tokens": 500, "tokens_status": "complete"])
        let explicitUnknown = CueTaskDetail(raw: ["total_tokens": 500, "tokens_status": "complete", "turn_usage": ["tokens_status": "unknown"]])
        let recorded = CueTaskDetail(raw: ["total_tokens": 500, "current_settings": ["status": "current", "source": "codex_desktop", "model": "new-model"], "turn_usage": ["total_tokens": 700, "tokens_status": "complete", "cost_usd": 1, "cost_status": "complete", "currency": "USD"], "history_usage": ["total_tokens": 900, "tokens_status": "complete", "cost_usd": 2, "cost_status": "complete", "currency": "USD"]])
        return ["task_usage_unknown_is_distinct_from_zero": unknown.tokenLine == "— Tokens" && unknown.costLine == "—" && zero.tokenLine == "0 Tokens" && zero.costLine == "0.0000 ¥",
                "task_usage_partial_exposes_lower_bound_and_priced_subtotal": partial.tokenLine == "≥12.3K Tokens" && partial.costLine == "$0.0030 USD *" && partial.tooltip.contains("已定价小计") && partial.tooltip.contains("1 / 2"),
                "task_usage_both_currencies_keep_full_base_unit_amounts": both.costLine == "1,234,567.89 ¥\n$12,345.67 USD" && !both.costLine.contains("M") && !both.costLine.contains("K"),
                "task_usage_invalid_numbers_remain_unknown": invalid.totalTokens == nil && invalid.inputTokens == nil && invalid.outputTokens == nil && invalid.costLine == "—",
                "task_usage_unpriced_is_distinct_from_unknown": unpriced.tokenLine == "50 Tokens" && unpriced.costLine == "未定价" && unknown.costLine != unpriced.costLine,
                "task_usage_missing_fx_is_explicit": noFX.costLine == "汇率未获取" && noFX.tooltip.contains("汇率尚未取得"),
                "task_usage_history_explains_sync_time_and_live_increment": history.tokenLine == "≥2.0M Tokens" && history.tooltip.contains("最近账本同步：") && history.tooltip.contains("未同步的实时增量") && history.tooltip.contains("缓存快照"),
                "task_usage_legacy_flat_fields_fallback_to_turn_only": legacy.turnUsage.tokenLine == "500 Tokens" && legacy.turnUsage.costLine == "—" && legacy.historyUsage.tokenLine == "— Tokens" && legacy.turnUsage.tooltip.contains("旧版状态"),
                "task_usage_explicit_unknown_never_reuses_legacy_total": explicitUnknown.turnUsage.tokenLine == "— Tokens",
                "task_usage_nested_scopes_remain_separate_from_settings": recorded.turnUsage.tokenLine == "700 Tokens" && recorded.historyUsage.tokenLine == "900 Tokens" && recorded.turnUsage.costLine == "$1.00 USD" && recorded.historyUsage.costLine == "$2.00 USD"]
    }
}

/// Bounded, in-memory telemetry. Missing fields remain unknown, never zero.
struct CueTaskDetail {
    let status: String
    let turnID: String
    let startedAt: Date?
    let finishedAt: Date?
    let lastActivity: Date?
    let elapsedSeconds: Double?
    let elapsedStatus: String
    private let recordedModel: String
    private let recordedReasoningEffort: String
    private let recordedServiceTier: String
    private let selectedModel: String
    private let selectedModelStatus: String
    private let selectedReasoningEffort: String
    private let selectedServiceTier: String
    private let selectedSpeedStatus: String
    private let selectedModelDisplayName: String
    private let selectedSpeedKind: String
    private let selectedSpeedMessage: String
    let currentSettingsStatus: String
    let currentSettingsSource: String
    let currentSettingsObservedAt: Date?
    private let currentSettingsVerifiedAt: Date?
    private let currentSettingsExpiresAt: Date?
    private let currentSettingsMessage: String
    var model: String { currentSettingsReadable ? (["default", "unknown"].contains(selectedModelStatus) ? "" : selectedModel) : recordedModel }
    var reasoningEffort: String { currentSettingsReadable ? selectedReasoningEffort : recordedReasoningEffort }
    var serviceTier: String { currentSettingsReadable ? selectedServiceTier : recordedServiceTier }
    let totalTokens: Int64?
    let inputTokens: Int64?
    let outputTokens: Int64?
    let cacheReadTokens: Int64?
    let cacheCreationTokens: Int64?
    let tokensStatus: String
    let turnUsage: CueTaskUsageSummary
    let historyUsage: CueTaskUsageSummary
    let messages: [CueTaskMessage]
    let lastMessage: String
    let lastMessageAt: Date?
    let lastRole: String
    let contentTruncated: Bool

    init(raw: [String: Any]) {
        status = Self.label(raw["status"], limit: 40, fallback: "unknown")
        turnID = Self.label(raw["turn_id"], limit: 160)
        startedAt = Self.date(raw["started_at"])
        finishedAt = Self.date(raw["finished_at"])
        lastActivity = Self.date(raw["last_activity"])
        elapsedSeconds = Self.number(raw["elapsed_seconds"])
        elapsedStatus = Self.label(raw["elapsed_status"], limit: 40, fallback: "unknown")
        recordedModel = Self.label(raw["model"], limit: 180)
        recordedReasoningEffort = Self.label(raw["reasoning_effort"], limit: 40)
        recordedServiceTier = Self.label(raw["service_tier"], limit: 40)
        let selected = raw["current_settings"] as? [String: Any] ?? [:]
        currentSettingsStatus = Self.label(selected["status"], limit: 30)
        currentSettingsSource = Self.label(selected["source"], limit: 80)
        currentSettingsObservedAt = Self.date(selected["observed_at"])
        currentSettingsVerifiedAt = Self.date(selected["verified_at"])
        currentSettingsExpiresAt = Self.date(selected["expires_at"])
        currentSettingsMessage = Self.label(selected["message"] ?? selected["reason"], limit: 240)
        selectedModel = Self.label(selected["model"], limit: 180)
        selectedModelStatus = Self.label(selected["model_status"], limit: 40)
        selectedReasoningEffort = Self.label(selected["reasoning_effort"], limit: 40)
        selectedServiceTier = Self.label(selected["service_tier"], limit: 40)
        selectedSpeedStatus = Self.label(selected["speed_status"], limit: 40)
        selectedModelDisplayName = Self.label(selected["model_display_name"], limit: 180)
        selectedSpeedKind = Self.label(selected["speed_kind"], limit: 40)
        selectedSpeedMessage = Self.label(selected["speed_message"], limit: 700)
        totalTokens = Self.integer(raw["total_tokens"])
        inputTokens = Self.integer(raw["input_tokens"])
        outputTokens = Self.integer(raw["output_tokens"])
        cacheReadTokens = Self.integer(raw["cache_read_tokens"])
        cacheCreationTokens = Self.integer(raw["cache_creation_tokens"])
        let declared = Self.label(raw["tokens_status"], limit: 40)
        tokensStatus = totalTokens == nil ? "unknown" : ["complete", "partial"].contains(declared) ? declared : "partial"
        let legacyUsage = raw.filter { ["total_tokens", "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "tokens_status", "request_count"].contains($0.key) }
        turnUsage = CueTaskUsageSummary(raw: raw["turn_usage"] as? [String: Any] ?? (raw["turn_usage"] == nil ? legacyUsage : [:]), legacy: raw["turn_usage"] == nil)
        historyUsage = CueTaskUsageSummary(raw: raw["history_usage"] as? [String: Any] ?? [:], history: true)
        lastMessage = Self.text(raw["last_message"], limit: 12_000)
        lastMessageAt = Self.date(raw["last_message_at"])
        lastRole = Self.label(raw["last_role"], limit: 30)
        var result: [CueTaskMessage] = []
        var truncated = raw["content_truncated"] as? Bool == true
        if (raw["last_message"] as? String ?? "").count > 12_000 { truncated = true }
        var budget = 12_000
        let rows = raw["messages"] as? [[String: Any]] ?? []
        if rows.count > 8 { truncated = true }
        for row in rows.suffix(8).reversed() {
            guard let role = row["role"] as? String, ["user", "assistant"].contains(role) else { continue }
            let value = Self.text(row["text"], limit: 12_000)
            if (row["text"] as? String ?? "").count > 12_000 { truncated = true }
            guard !value.isEmpty, budget > 0 else { continue }
            let kept = String(value.suffix(budget))
            if kept.count < value.count { truncated = true }
            budget -= kept.count
            result.append(CueTaskMessage(role: role, text: kept, date: Self.date(row["created_at"])))
        }
        result.reverse()
        if result.isEmpty, ["user", "assistant"].contains(lastRole), !lastMessage.isEmpty {
            result = [CueTaskMessage(role: lastRole, text: lastMessage, date: lastMessageAt)]
        }
        messages = result
        contentTruncated = truncated
    }

    var isActive: Bool { ["running", "waiting"].contains(status) }
    var startDate: Date? { startedAt }
    var finishedDate: Date? { finishedAt }

    func elapsed(now: Date, running: Bool? = nil, stoppedAt: Date? = nil) -> TimeInterval? {
        if let start = startedAt {
            if running ?? (status == "running") { return max(0, now.timeIntervalSince(start)) }
            if let end = stoppedAt, end >= start { return end.timeIntervalSince(start) }
            if let end = finishedAt, end >= start { return end.timeIntervalSince(start) }
            // Waiting and interrupted rows freeze at the last observed work;
            // a newly generated telemetry snapshot is not new execution time.
            if let end = lastActivity, end >= start { return end.timeIntervalSince(start) }
        }
        return elapsedSeconds
    }

    private func scope(eventDate: Date?) -> String {
        if let eventDate { return startedAt.map { eventDate < $0.addingTimeInterval(-1) } ?? true ? "最新轮" : "本轮" }
        return isActive ? "本轮" : "最新轮"
    }
    func turnUsageLabel(eventDate: Date? = nil) -> String { scope(eventDate: eventDate) }
    func durationLine(now: Date = Date(), eventDate: Date? = nil, running: Bool? = nil, stoppedAt: Date? = nil) -> String {
        let duration = elapsed(now: now, running: running, stoppedAt: stoppedAt).map { (elapsedStatus == "partial" ? "≥" : "") + Self.duration($0) } ?? "耗时待确认"
        return "\(scope(eventDate: eventDate)) \(duration)"
    }
    private var tokenText: String {
        if let total = totalTokens, tokensStatus != "unknown" {
            return (tokensStatus == "partial" ? "≥" : "") + Self.compact(total) + " Tokens" + (tokensStatus == "partial" ? "（部分）" : "")
        }
        return "Tokens 待同步"
    }
    func tokenLine(eventDate: Date? = nil) -> String {
        "\(scope(eventDate: eventDate)) · \(tokenText)"
    }
    func summaryLine(now: Date = Date(), eventDate: Date? = nil) -> String {
        let duration = elapsed(now: now).map { (elapsedStatus == "partial" ? "≥" : "") + Self.duration($0) } ?? "耗时待确认"
        return "\(scope(eventDate: eventDate)) · \(duration) · \(tokenText)"
    }

    private var currentSettingsReadable: Bool {
        guard ["current", "partial"].contains(currentSettingsStatus), !currentSettingsSource.isEmpty else { return false }
        if let expiry = currentSettingsExpiresAt { return expiry >= Date() }
        if let observed = currentSettingsObservedAt, !(-10...45).contains(Date().timeIntervalSince(observed)) { return false }
        return true
    }
    private static func friendlyModel(_ identifier: String, displayName: String = "") -> String {
        let name = displayName.isEmpty ? identifier : displayName
        let parts = name.split(separator: "-").map(String.init)
        if parts.count == 3, parts[0].lowercased() == "gpt", ["sol", "terra", "luna", "astra"].contains(parts[2].lowercased()), parts[1].range(of: #"^[0-9]+(?:\.[0-9]+)*$"#, options: .regularExpression) != nil {
            return "GPT-" + parts[1] + " " + parts[2].capitalized
        }
        return name
    }
    private static func effortLabel(_ effort: String) -> String {
        ["none":"None", "minimal":"Minimal", "low":"Low", "medium":"Medium", "high":"High", "xhigh":"Extra High", "max":"Max", "ultra":"Ultra", "default":"默认强度"][effort] ?? effort
    }
    var modelSelectionLine: String {
        if currentSettingsReadable {
            let name = selectedModelStatus == "default" ? "模型跟随默认" : selectedModel.isEmpty ? "模型未提供" : Self.friendlyModel(selectedModel, displayName: selectedModelDisplayName)
            return "当前 · " + name + " " + (selectedReasoningEffort.isEmpty ? "强度未提供" : Self.effortLabel(selectedReasoningEffort))
        }
        guard !recordedModel.isEmpty || !recordedReasoningEffort.isEmpty else { return "当前设置不可读" }
        return (currentSettingsStatus.isEmpty ? "" : "当前不可读 · ") + (isActive ? "本轮记录 · " : "上次记录 · ") + Self.friendlyModel(recordedModel) + (recordedReasoningEffort.isEmpty ? "" : " " + Self.effortLabel(recordedReasoningEffort))
    }
    var speedSelectionLine: String {
        if currentSettingsReadable {
            switch selectedSpeedKind {
            case "fast": return "Fast · 已保存"
            case "ultrafast": return "Ultra Fast · 已保存"
            case "standard": return "标准速度 · 已保存"
            case "unsupported": return "档位未支持"
            case "default": return "速度跟随默认"
            default:
                if selectedSpeedStatus == "default" { return "速度跟随默认" }
                if ["default", "standard"].contains(selectedServiceTier) { return "标准速度 · 已保存" }
                return selectedServiceTier.isEmpty ? "速度未提供" : "速度待核实"
            }
        }
        if recordedServiceTier.isEmpty { return "速度待同步" }
        if recordedServiceTier == "default" { return "速度跟随默认（记录）" }
        return "仅有历史档位"
    }
    var speedSelectionIsFast: Bool { currentSettingsReadable && ["fast", "ultrafast"].contains(selectedSpeedKind) }
    var secondaryLine: String {
        let modelLine = modelSelectionLine
        return modelLine == "当前设置不可读" ? modelLine : modelLine + " · " + speedSelectionLine
    }
    var settingsTooltip: String {
        var lines = [secondaryLine]
        if currentSettingsReadable {
            lines.append("来源：Codex 已保存的对话设置，只读获取。输入框内尚未提交的临时选择不在共享接口内。")
            if let observed = currentSettingsObservedAt {
                let formatter = DateFormatter(); formatter.dateFormat = "HH:mm:ss"
                lines.append("读取时间：" + formatter.string(from: observed))
            }
            if let verified = currentSettingsVerifiedAt {
                let formatter = DateFormatter(); formatter.dateFormat = "HH:mm:ss"
                lines.append("连接核对时间：" + formatter.string(from: verified))
            }
            if currentSettingsStatus == "partial" { lines.append("Codex 尚未提供的字段保持未知，不用历史值补齐。") }
            if selectedModelStatus == "default" { lines.append("没有显式模型覆盖，跟随 Codex 的默认选择；未推断实际默认模型。") }
            if selectedSpeedStatus == "default", selectedServiceTier.isEmpty { lines.append("没有显式速度覆盖，跟随 Codex 的默认选择；未推断为标准或闪电。") }
        } else {
            lines.append("当前选择暂不可核实；记录值仅表示已读到的本轮或上次执行设置。")
            if !currentSettingsMessage.isEmpty { lines.append(currentSettingsMessage) }
        }
        if currentSettingsReadable {
            if !selectedServiceTier.isEmpty { lines.append("已保存请求档位：" + selectedServiceTier) }
            if !selectedSpeedMessage.isEmpty { lines.append(selectedSpeedMessage) }
            lines.append("Ultra 表示推理强度；Fast 是独立速度档位。")
        } else if !recordedServiceTier.isEmpty { lines.append("历史请求档位：" + recordedServiceTier + "；不能据此判断输入框当前的速度选择。") }
        return lines.joined(separator: "\n")
    }

    var tokenBreakdown: String {
        func value(_ value: Int64?) -> String { value.map(Self.exact) ?? "—" }
        return "新增输入 \(value(inputTokens)) · 输出 \(value(outputTokens))\n缓存读取 \(value(cacheReadTokens)) · 缓存写入 \(value(cacheCreationTokens))"
    }

    static func exact(_ value: Int64) -> String {
        let formatter = NumberFormatter(); formatter.numberStyle = .decimal
        formatter.maximumFractionDigits = 0
        return formatter.string(from: NSNumber(value: value)) ?? String(value)
    }
    fileprivate static func compact(_ value: Int64) -> String {
        for (suffix, scale) in [("B", 1_000_000_000.0), ("M", 1_000_000.0), ("K", 1_000.0)] {
            if Double(value) >= scale { return String(format: "%.1f%@", Double(value) / scale, suffix) }
        }
        return exact(value)
    }
    static func duration(_ seconds: TimeInterval) -> String {
        let total = Int(min(315_360_000, max(0, seconds)))
        if total >= 3600 { return "\(total / 3600)时\((total % 3600) / 60)分\(total % 60)秒" }
        if total >= 60 { return "\(total / 60)分\(total % 60)秒" }
        return "\(total)秒"
    }
    private static func number(_ raw: Any?) -> Double? {
        guard let value = raw as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID() else { return nil }
        let result = value.doubleValue
        return result.isFinite && result >= 0 && result <= 9_000_000_000_000_000 ? result : nil
    }
    fileprivate static func integer(_ raw: Any?) -> Int64? {
        guard let value = number(raw), value.rounded(.towardZero) == value else { return nil }
        return Int64(value)
    }
    fileprivate static func date(_ raw: Any?) -> Date? {
        if let value = number(raw), value > 0 && value < 253_402_300_799 { return Date(timeIntervalSince1970: value) }
        guard let value = raw as? String else { return nil }
        let formatter = ISO8601DateFormatter(); formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    }
    fileprivate static func label(_ raw: Any?, limit: Int, fallback: String = "") -> String {
        guard let value = raw as? String else { return fallback }
        return String(value.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ").prefix(limit))
    }
    private static func text(_ raw: Any?, limit: Int) -> String {
        guard let value = raw as? String else { return "" }
        let cleaned = String(value.unicodeScalars.filter { $0 == "\n" || $0 == "\t" || !CharacterSet.controlCharacters.contains($0) })
        return String(cleaned.suffix(limit)).trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

private final class CueTaskHoverPanel: NSPanel {
    override var canBecomeMain: Bool { false }
    override var canBecomeKey: Bool { true }
}

private final class CueTaskHoverSurface: NSVisualEffectView {
    var entered: (() -> Void)?
    var exited: (() -> Void)?
    private var tracking: NSTrackingArea?
    override var isFlipped: Bool { true }
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let tracking { removeTrackingArea(tracking) }
        let area = NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self)
        addTrackingArea(area); tracking = area
    }
    override func mouseEntered(with event: NSEvent) { entered?() }
    override func mouseExited(with event: NSEvent) { exited?() }
}

/// A nonactivating hover panel. All preview text lives only in this controller.
final class CueTaskHoverController: NSObject {
    private var panel: CueTaskHoverPanel?
    private let titleField = NSTextField(labelWithString: "")
    private let summaryField = NSTextField(labelWithString: "")
    private let modelField = NSTextField(labelWithString: "")
    private let breakdownField = NSTextField(wrappingLabelWithString: "")
    private let previewHeading = NSTextField(labelWithString: "最近对话")
    private let footerField = NSTextField(labelWithString: "移入浮窗暂停滚动 · 可选择文字")
    private let scroll = NSScrollView()
    private let text = NSTextView()
    private var showWork: DispatchWorkItem?
    private var hideWork: DispatchWorkItem?
    private var timer: Timer?
    private var activeThreadID: String?
    private weak var anchor: NSView?
    private var provider: (() -> CueTaskDetail?)?
    private var currentTitle = ""
    private var currentBody = ""
    private var renderedMessages = ""
    private var currentDetail = CueTaskDetail(raw: [:])
    private var overRow = false
    private var overPanel = false
    private var previousTick = Date()
    private var lastRefresh = Date.distantPast
    private var latestMessageRange = NSRange(location: 0, length: 0)

    var isVisible: Bool { panel?.isVisible == true }
    var isEngaged: Bool { overRow || overPanel || isVisible }

    func hover(threadID: String, title: String, anchor: NSView, provider: @escaping () -> CueTaskDetail?) {
        hideWork?.cancel(); overRow = true
        self.anchor = anchor; self.provider = provider; currentTitle = title
        if activeThreadID == threadID, isVisible {
            refresh(); if !overPanel { positionPanel() }; return
        }
        showWork?.cancel(); activeThreadID = threadID
        if isVisible { panel?.orderOut(nil); timer?.invalidate(); timer = nil }
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.activeThreadID == threadID, self.overRow else { return }
            self.showNow()
        }
        showWork = work; DispatchQueue.main.asyncAfter(deadline: .now() + 0.45, execute: work)
    }

    func leaveRow() { overRow = false; showWork?.cancel(); scheduleHide() }

    func hide() {
        showWork?.cancel(); hideWork?.cancel(); timer?.invalidate(); timer = nil
        if let panel { panel.parent?.removeChildWindow(panel); panel.orderOut(nil) }
        provider = nil; anchor = nil; activeThreadID = nil; overRow = false; overPanel = false
        currentBody = ""; renderedMessages = ""; text.string = ""; currentDetail = CueTaskDetail(raw: [:])
    }

    func refresh() {
        guard activeThreadID != nil else { return }
        update(detail: provider?() ?? CueTaskDetail(raw: [:]), title: currentTitle)
    }

    func update(detail: CueTaskDetail, title: String) {
        makePanel()
        currentDetail = detail; currentTitle = title
        titleField.stringValue = title; titleField.toolTip = title
        summaryField.stringValue = detail.summaryLine(now: Date())
        var tips = detail.totalTokens.map { "Tokens：" + CueTaskDetail.exact($0) } ?? "Tokens 尚未记录"
        if detail.tokensStatus == "partial" || detail.elapsedStatus == "partial" { tips += "\n只统计已读取的本轮记录；≥ 表示当前可确认的下限。" }
        summaryField.toolTip = tips
        modelField.stringValue = detail.secondaryLine; modelField.toolTip = detail.settingsTooltip
        breakdownField.stringValue = detail.tokenBreakdown
        previewHeading.stringValue = detail.contentTruncated ? "最近对话 · 部分内容" : "最近对话"
        renderMessages(detail.messages)
    }

    private func scheduleHide() {
        hideWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self, !self.overRow, !self.overPanel else { return }
            self.hide()
        }
        hideWork = work; DispatchQueue.main.asyncAfter(deadline: .now() + 0.25, execute: work)
    }

    private func makePanel() {
        guard panel == nil else { return }
        let size = NSSize(width: 500, height: 474)
        let window = CueTaskHoverPanel(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        window.isOpaque = false; window.backgroundColor = .clear; window.hasShadow = true
        window.hidesOnDeactivate = false; window.becomesKeyOnlyIfNeeded = true
        window.isReleasedWhenClosed = false; window.animationBehavior = .utilityWindow
        window.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        let surface = CueTaskHoverSurface(frame: NSRect(origin: .zero, size: size))
        surface.material = .popover; surface.blendingMode = .withinWindow; surface.state = .active
        surface.wantsLayer = true; surface.layer?.cornerRadius = 13; surface.layer?.masksToBounds = true
        surface.layer?.borderWidth = 0.7; surface.layer?.borderColor = NSColor.separatorColor.withAlphaComponent(0.6).cgColor
        surface.entered = { [weak self] in self?.overPanel = true; self?.hideWork?.cancel() }
        surface.exited = { [weak self] in self?.overPanel = false; self?.scheduleHide() }
        window.contentView = surface
        titleField.frame = NSRect(x: 18, y: 14, width: 464, height: 24)
        titleField.font = .systemFont(ofSize: 14, weight: .semibold); titleField.lineBreakMode = .byTruncatingTail
        summaryField.frame = NSRect(x: 18, y: 43, width: 464, height: 20)
        summaryField.font = .monospacedDigitSystemFont(ofSize: 11.5, weight: .medium)
        modelField.frame = NSRect(x: 18, y: 67, width: 464, height: 18)
        modelField.font = .systemFont(ofSize: 11); modelField.textColor = .secondaryLabelColor
        modelField.lineBreakMode = .byTruncatingTail
        breakdownField.frame = NSRect(x: 18, y: 91, width: 464, height: 37)
        breakdownField.font = .monospacedDigitSystemFont(ofSize: 11, weight: .regular)
        breakdownField.textColor = .secondaryLabelColor
        let separator = NSBox(frame: NSRect(x: 18, y: 137, width: 464, height: 1)); separator.boxType = .separator
        previewHeading.frame = NSRect(x: 18, y: 150, width: 310, height: 18)
        previewHeading.font = .systemFont(ofSize: 11, weight: .semibold); previewHeading.textColor = .secondaryLabelColor
        let follow = NSButton(title: "跟随最新", target: self, action: #selector(followLatest))
        follow.bezelStyle = .rounded; follow.frame = NSRect(x: 390, y: 143, width: 95, height: 29)
        follow.setAccessibilityLabel("跟随最新对话内容")
        scroll.frame = NSRect(x: 18, y: 178, width: 464, height: 258)
        scroll.drawsBackground = false; scroll.borderType = .noBorder; scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        text.frame = NSRect(x: 0, y: 0, width: 448, height: 258)
        text.isEditable = false; text.isSelectable = true; text.drawsBackground = false
        text.isRichText = true; text.importsGraphics = false; text.isVerticallyResizable = true; text.isHorizontallyResizable = false
        text.minSize = NSSize(width: 0, height: 258); text.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        text.textContainerInset = NSSize(width: 0, height: 7)
        text.textContainer?.widthTracksTextView = true; text.textContainer?.lineFragmentPadding = 0
        text.setAccessibilityLabel("最近用户和助手对话，可滚动并选择文字")
        scroll.documentView = text
        footerField.frame = NSRect(x: 18, y: 447, width: 464, height: 17)
        footerField.font = .systemFont(ofSize: 10); footerField.textColor = .tertiaryLabelColor
        for view in [titleField, summaryField, modelField, breakdownField, separator, previewHeading, follow, scroll, footerField] { surface.addSubview(view) }
        panel = window
    }

    private func showNow() {
        makePanel(); currentBody = ""; renderedMessages = ""; refresh(); positionPanel()
        guard let panel else { return }
        if let parent = anchor?.window, panel.parent !== parent {
            panel.parent?.removeChildWindow(panel); parent.addChildWindow(panel, ordered: .above)
        }
        // Deliberately no activate/makeKey: showing a tip must not steal typing.
        panel.orderFrontRegardless(); previousTick = Date(); lastRefresh = Date()
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in self?.tick() }
        if let timer { RunLoop.main.add(timer, forMode: .common) }
        scrollToLatestMessage()
    }

    private func positionPanel() {
        guard let panel, let anchor, let window = anchor.window else { return }
        let target = window.convertToScreen(anchor.convert(anchor.bounds, to: nil))
        let screen = window.screen ?? NSScreen.main
        let visible = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let size = panel.frame.size
        var x = target.maxX + 10
        if x + size.width > visible.maxX - 8 { x = target.minX - size.width - 10 }
        x = max(visible.minX + 8, min(x, visible.maxX - size.width - 8))
        let y = max(visible.minY + 8, min(target.maxY - size.height, visible.maxY - size.height - 8))
        panel.level = NSWindow.Level(rawValue: max(NSWindow.Level.popUpMenu.rawValue, window.level.rawValue + 1))
        panel.setFrameOrigin(NSPoint(x: x, y: y))
    }

    private func renderMessages(_ messages: [CueTaskMessage]) {
        let result = NSMutableAttributedString()
        let date = DateFormatter(); date.locale = Locale(identifier: "zh_CN"); date.dateFormat = "HH:mm:ss"
        let headingStyle = NSMutableParagraphStyle(); headingStyle.paragraphSpacing = 5
        let signature = messages.map { $0.role + "\u{0}" + $0.text + "\u{0}" + ($0.date?.description ?? "") }.joined(separator: "\u{1}")
        var latest = NSRange(location: 0, length: 0)
        for message in messages {
            latest = NSRange(location: result.length, length: 0)
            let heading = (message.role == "user" ? "你" : "Codex") + (message.date.map { " · " + date.string(from: $0) } ?? "") + "\n"
            result.append(NSAttributedString(string: heading, attributes: [.font: NSFont.systemFont(ofSize: 10.5, weight: .semibold), .foregroundColor: NSColor.secondaryLabelColor, .paragraphStyle: headingStyle]))
            result.append(CueHoverMarkdown.render(message.text))
            result.append(NSAttributedString(string: "\n", attributes: [.font: NSFont.systemFont(ofSize: 13)]))
            latest.length = result.length - latest.location
        }
        if messages.isEmpty {
            result.append(NSAttributedString(string: "暂未读取到可显示的最近对话。", attributes: [.font: NSFont.systemFont(ofSize: 12), .foregroundColor: NSColor.secondaryLabelColor]))
        }
        guard signature != renderedMessages || result.string != currentBody else { return }
        let origin = scroll.contentView.bounds.origin
        let wasAtEnd = text.frame.height - scroll.contentView.bounds.maxY < 10
        let selection = text.selectedRange()
        let selected = selection.length > 0 && NSMaxRange(selection) <= (currentBody as NSString).length ? (currentBody as NSString).substring(with: selection) : ""
        let wasEmpty = currentBody.isEmpty
        currentBody = result.string; renderedMessages = signature; latestMessageRange = latest
        text.textStorage?.setAttributedString(result)
        text.setFrameSize(NSSize(width: scroll.contentSize.width, height: max(scroll.contentSize.height, text.frame.height)))
        text.layoutManager?.ensureLayout(for: text.textContainer!)
        let height = (text.layoutManager?.usedRect(for: text.textContainer!).height ?? 0) + 24
        text.setFrameSize(NSSize(width: scroll.contentSize.width, height: max(scroll.contentSize.height, height)))
        if !selected.isEmpty {
            let newSelection = (currentBody as NSString).range(of: selected)
            if newSelection.location != NSNotFound { text.setSelectedRange(newSelection) }
        }
        if wasEmpty { scrollToLatestMessage() }
        else if wasAtEnd && !overPanel { scrollToEnd() }
        else { scroll.contentView.scroll(to: scroll.contentView.constrainBoundsRect(NSRect(origin: origin, size: scroll.contentSize)).origin); scroll.reflectScrolledClipView(scroll.contentView) }
    }

    private func tick() {
        guard isVisible else { timer?.invalidate(); timer = nil; return }
        let now = Date(), elapsed = min(0.15, nowInterval(since: previousTick)); previousTick = now
        if now.timeIntervalSince(lastRefresh) >= 1 { lastRefresh = now; refresh() }
        advanceScroll(elapsed: elapsed)
    }
    private func advanceScroll(elapsed: TimeInterval) {
        guard !overPanel, text.selectedRange().length == 0 else { return }
        let maximum = max(0, text.frame.height - scroll.contentSize.height)
        let current = scroll.contentView.bounds.origin
        if current.y < maximum {
            scroll.contentView.scroll(to: NSPoint(x: current.x, y: min(maximum, current.y + elapsed * 10)))
            scroll.reflectScrolledClipView(scroll.contentView)
        }
    }
    private func nowInterval(since date: Date) -> TimeInterval { max(0, Date().timeIntervalSince(date)) }
    private func scrollToLatestMessage() {
        guard let layout = text.layoutManager, let container = text.textContainer, !currentBody.isEmpty else { return }
        layout.ensureLayout(for: container)
        let glyphs = layout.glyphRange(forCharacterRange: latestMessageRange, actualCharacterRange: nil)
        let rect = layout.boundingRect(forGlyphRange: glyphs, in: container)
        let maximum = max(0, text.frame.height - scroll.contentSize.height)
        scroll.contentView.scroll(to: NSPoint(x: 0, y: min(maximum, max(0, rect.minY))))
        scroll.reflectScrolledClipView(scroll.contentView)
    }
    private func scrollToEnd() {
        scroll.contentView.scroll(to: NSPoint(x: 0, y: max(0, text.frame.height - scroll.contentSize.height)))
        scroll.reflectScrolledClipView(scroll.contentView)
    }
    @objc private func followLatest() { text.setSelectedRange(NSRange(location: 0, length: 0)); refresh(); scrollToLatestMessage() }

    deinit {
        showWork?.cancel(); hideWork?.cancel(); timer?.invalidate()
        if let panel { panel.parent?.removeChildWindow(panel); panel.orderOut(nil) }
    }

    static func validationReport() -> [String: Bool] {
        let now = Date(timeIntervalSince1970: 1_793_170_000)
        let unknown = CueTaskDetail(raw: [:])
        let partial = CueTaskDetail(raw: ["status": "running", "started_at": now.addingTimeInterval(-125).timeIntervalSince1970, "elapsed_status": "partial", "total_tokens": 12345, "tokens_status": "partial", "model": "gpt-model", "reasoning_effort": "high", "service_tier": "fast"])
        let complete = CueTaskDetail(raw: ["status": "complete", "started_at": now.addingTimeInterval(-120).timeIntervalSince1970, "finished_at": now.addingTimeInterval(-60).timeIntervalSince1970, "total_tokens": 0, "tokens_status": "complete"])
        let waiting = CueTaskDetail(raw: ["status": "waiting", "started_at": now.addingTimeInterval(-120).timeIntervalSince1970, "last_activity": now.addingTimeInterval(-60).timeIntervalSince1970, "elapsed_seconds": 120])
        let invalid = CueTaskDetail(raw: ["total_tokens": true, "input_tokens": -1, "output_tokens": 2.5, "elapsed_seconds": Double.infinity])
        let messages = CueTaskDetail(raw: ["messages": [["role": "system", "text": "SYSTEM-NOT-VISIBLE"], ["role": "tool", "text": "TOOL-ARGUMENTS-NOT-VISIBLE"], ["role": "reasoning", "text": "REASONING-NOT-VISIBLE"], ["role": "user", "text": "可见问题"], ["role": "assistant", "text": "可见回复"]]])
        let long = CueTaskDetail(raw: ["messages": (0..<10).map { ["role": "assistant", "text": String(repeating: "内容", count: 3000) + "\($0)"] }])
        let selection: [String: Any] = ["status": "current", "source": "codex_desktop", "observed_at": Date().timeIntervalSince1970, "model": "current-model", "reasoning_effort": "ultra", "service_tier": "priority", "model_display_name": "current-model", "speed_kind": "fast", "speed_source": "codex_saved_thread_settings"]
        let selected = CueTaskDetail(raw: ["status": "running", "model": "old-model", "reasoning_effort": "low", "service_tier": "default", "current_settings": selection, "total_tokens": 12345, "tokens_status": "partial"])
        var inherited = selection; inherited["service_tier"] = NSNull(); inherited["speed_status"] = "default"; inherited["reasoning_effort"] = "default"; inherited["speed_kind"] = "default"
        let inheritedDetail = CueTaskDetail(raw: ["current_settings": inherited])
        var defaultModel = inherited; defaultModel["status"] = "partial"; defaultModel["model"] = NSNull(); defaultModel["model_status"] = "default"
        let defaultModelDetail = CueTaskDetail(raw: ["model": "old-model", "current_settings": defaultModel])
        var clearedSelection = selection; clearedSelection["status"] = "partial"; clearedSelection["model"] = NSNull(); clearedSelection["model_status"] = "unknown"; clearedSelection["reasoning_effort"] = NSNull(); clearedSelection["service_tier"] = NSNull(); clearedSelection["speed_status"] = "unknown"; clearedSelection.removeValue(forKey: "speed_kind")
        let clearedSelectionDetail = CueTaskDetail(raw: ["model": "old-model", "reasoning_effort": "ultra", "service_tier": "priority", "current_settings": clearedSelection])
        var incomplete = selection; incomplete["status"] = "partial"; incomplete.removeValue(forKey: "reasoning_effort"); incomplete.removeValue(forKey: "service_tier"); incomplete.removeValue(forKey: "speed_kind")
        let incompleteDetail = CueTaskDetail(raw: ["model": "old-model", "reasoning_effort": "low", "service_tier": "fast", "current_settings": incomplete])
        var expired = selection; expired["observed_at"] = Date().addingTimeInterval(-120).timeIntervalSince1970
        let expiredDetail = CueTaskDetail(raw: ["status": "complete", "model": "old-model", "reasoning_effort": "low", "service_tier": "fast", "current_settings": expired])
        var disconnected = selection; disconnected["status"] = "unavailable"
        let disconnectedDetail = CueTaskDetail(raw: ["current_settings": disconnected])
        var healthyUnchanged = expired; healthyUnchanged["verified_at"] = Date().timeIntervalSince1970; healthyUnchanged["expires_at"] = Date().addingTimeInterval(30).timeIntervalSince1970
        let healthyUnchangedDetail = CueTaskDetail(raw: ["current_settings": healthyUnchanged])
        let historicalDefault = CueTaskDetail(raw: ["model": "old-model", "service_tier": "default"])
        var explicitDefault = selection; explicitDefault["service_tier"] = "default"; explicitDefault["speed_kind"] = "standard"
        let explicitDefaultDetail = CueTaskDetail(raw: ["current_settings": explicitDefault])
        var standardSol = selection; standardSol["model"] = "gpt-6.1-sol"; standardSol["model_display_name"] = "GPT-6.1-Sol"; standardSol["service_tier"] = "default"; standardSol["speed_kind"] = "standard"
        var fastSol = standardSol; fastSol["service_tier"] = "priority"; fastSol["speed_kind"] = "fast"
        var unconfirmedSol = fastSol; unconfirmedSol.removeValue(forKey: "speed_kind")
        let standardSolDetail = CueTaskDetail(raw: ["current_settings": standardSol]), fastSolDetail = CueTaskDetail(raw: ["current_settings": fastSol]), unconfirmedSolDetail = CueTaskDetail(raw: ["current_settings": unconfirmedSol])
        var results = ["task_detail_unknown_never_fabricates_zero_tokens": unknown.totalTokens == nil && unknown.summaryLine(now: now).contains("待同步"),
                       "task_detail_partial_has_explicit_lower_bound": partial.summaryLine(now: now).contains("≥") && partial.summaryLine(now: now).contains("部分"),
                       "task_detail_live_duration_advances": partial.elapsed(now: now.addingTimeInterval(10)) == 135,
                       "task_detail_completed_duration_stays_fixed": complete.elapsed(now: now.addingTimeInterval(10)) == 60,
                       "task_detail_waiting_duration_freezes_at_last_work": waiting.elapsed(now: now) == 60 && waiting.elapsed(now: now.addingTimeInterval(10)) == 60,
                       "task_detail_hour_duration_keeps_seconds": CueTaskDetail.duration(3725) == "1时2分5秒",
                       "task_detail_row_tokens_exclude_separate_duration": partial.tokenLine().contains("Tokens") && !partial.tokenLine().contains("秒"),
                       "task_detail_explicit_zero_is_retained": complete.summaryLine(now: now).contains("0 Tokens"),
                       "task_detail_old_alert_labels_latest_turn": partial.summaryLine(now: now, eventDate: now.addingTimeInterval(-1000)).hasPrefix("最新轮"),
                       "task_detail_invalid_numbers_remain_unknown": invalid.totalTokens == nil && invalid.inputTokens == nil && invalid.outputTokens == nil && invalid.elapsedSeconds == nil,
                       "task_detail_reliable_model_and_speed_are_visible": partial.secondaryLine.contains("gpt-model") && partial.secondaryLine.contains("High") && partial.secondaryLine.contains("仅有历史档位"),
                       "task_detail_current_owner_settings_override_recorded_settings": selected.model == "current-model" && selected.reasoningEffort == "ultra" && selected.serviceTier == "priority" && selected.secondaryLine.hasPrefix("当前 · ") && !selected.secondaryLine.contains("old-model"),
                       "task_detail_current_settings_do_not_change_numeric_usage": selected.totalTokens == 12345 && selected.tokensStatus == "partial",
                       "task_detail_current_fast_state_is_explicit": selected.speedSelectionLine == "Fast · 已保存" && selected.modelSelectionLine.contains("Ultra") && !selected.modelSelectionLine.contains("Fast"),
                       "task_detail_null_speed_override_follows_default_without_guessing": inheritedDetail.secondaryLine.contains("速度跟随默认") && inheritedDetail.secondaryLine.contains("默认强度") && !inheritedDetail.secondaryLine.contains("标准速度") && !inheritedDetail.secondaryLine.contains("闪电"),
                       "task_detail_null_model_override_follows_default_without_guessing": defaultModelDetail.model.isEmpty && defaultModelDetail.secondaryLine.contains("模型跟随默认") && !defaultModelDetail.secondaryLine.contains("模型未提供") && !defaultModelDetail.secondaryLine.contains("old-model") && defaultModelDetail.settingsTooltip.contains("未推断实际默认模型"),
                       "task_detail_null_clear_removes_previous_model_effort_and_fast": clearedSelectionDetail.model.isEmpty && clearedSelectionDetail.reasoningEffort.isEmpty && clearedSelectionDetail.serviceTier.isEmpty && clearedSelectionDetail.secondaryLine.contains("模型未提供") && clearedSelectionDetail.secondaryLine.contains("强度未提供") && clearedSelectionDetail.secondaryLine.contains("速度未提供") && !clearedSelectionDetail.secondaryLine.contains("old-model") && !clearedSelectionDetail.secondaryLine.contains("current-model") && !clearedSelectionDetail.secondaryLine.contains("Ultra") && !clearedSelectionDetail.secondaryLine.contains("Fast"),
                       "task_detail_historical_null_normalization_never_proves_standard_speed": historicalDefault.secondaryLine.contains("速度跟随默认") && !historicalDefault.secondaryLine.contains("标准速度") && explicitDefaultDetail.secondaryLine.contains("标准速度"),
                       "task_detail_partial_current_settings_never_mix_historical_values": incompleteDetail.secondaryLine.contains("强度未提供") && incompleteDetail.secondaryLine.contains("速度未提供") && !incompleteDetail.secondaryLine.contains("低强度") && !incompleteDetail.secondaryLine.contains("Fast"),
                       "task_detail_expired_settings_are_not_presented_as_current": expiredDetail.secondaryLine.hasPrefix("当前不可读 · 上次记录") && !expiredDetail.secondaryLine.contains("current-model"),
                       "task_detail_healthy_subscription_keeps_unchanged_settings_current": healthyUnchangedDetail.secondaryLine.hasPrefix("当前 · ") && healthyUnchangedDetail.model == "current-model",
                       "task_detail_unavailable_snapshot_does_not_reuse_current_fields": disconnectedDetail.secondaryLine == "当前设置不可读" && !disconnectedDetail.secondaryLine.contains("current-model"),
                       "task_detail_record_only_settings_have_visible_provenance": partial.secondaryLine.hasPrefix("本轮记录 · ") && !partial.secondaryLine.contains("当前"),
                       "task_detail_setting_tooltip_explains_source_and_inheritance": selected.settingsTooltip.contains("只读获取") && selected.settingsTooltip.contains("读取时间") && inheritedDetail.settingsTooltip.contains("未推断"),
                       "task_detail_only_user_and_assistant_are_previewed": messages.messages.map(\.role) == ["user", "assistant"],
                       "task_detail_preview_text_stays_bounded": long.messages.count <= 8 && long.messages.reduce(0) { $0 + $1.text.count } <= 12000 && long.contentTruncated]
        results["native_model_title_is_independent_of_saved_fast_setting"] = standardSolDetail.modelSelectionLine == "当前 · GPT-6.1 Sol Ultra" && fastSolDetail.modelSelectionLine == standardSolDetail.modelSelectionLine && standardSolDetail.speedSelectionLine == "标准速度 · 已保存" && fastSolDetail.speedSelectionIsFast
        results["raw_priority_without_catalog_confirmation_does_not_assert_fast"] = unconfirmedSolDetail.speedSelectionLine == "速度待核实" && !unconfirmedSolDetail.speedSelectionIsFast && unconfirmedSolDetail.settingsTooltip.contains("priority")
        results["saved_speed_tooltip_explains_independent_strength_and_composer_boundary"] = fastSolDetail.settingsTooltip.contains("Ultra 表示推理强度") && fastSolDetail.settingsTooltip.contains("尚未提交")
        _ = NSApplication.shared
        results.merge(CueTaskUsageSummary.validationReport()) { _, new in new }
        results.merge(CueHoverMarkdown.validationReport()) { _, new in new }
        let hover = CueTaskHoverController(); hover.makePanel(); hover.update(detail: messages, title: "测试标题")
        results["task_hover_text_is_selectable_and_not_editable"] = hover.text.isSelectable && !hover.text.isEditable
        results["task_hover_initial_update_does_not_make_key"] = hover.panel?.isKeyWindow == false
        results["task_hover_contains_only_visible_conversation"] = hover.currentBody.contains("可见回复") && !hover.currentBody.contains("NOT-VISIBLE")
        let styled = CueTaskDetail(raw: ["messages": [["role": "assistant", "text": "**Same 同样文字**"]]])
        hover.update(detail: styled, title: "排版")
        let boldBody = hover.currentBody
        // AppKit substitutes PingFang for Chinese during layout; that family has
        // no italic face. Verify the italic trait on Latin and the removal of
        // bold on Chinese, rather than requiring a nonexistent fallback face.
        let styledRange = (hover.currentBody as NSString).range(of: "Same")
        let chineseRange = (hover.currentBody as NSString).range(of: "同样文字")
        let boldFont = hover.text.textStorage?.attribute(.font, at: styledRange.location, effectiveRange: nil) as? NSFont
        let boldChineseFont = hover.text.textStorage?.attribute(.font, at: chineseRange.location, effectiveRange: nil) as? NSFont
        hover.update(detail: CueTaskDetail(raw: ["messages": [["role": "assistant", "text": "*Same 同样文字*"]]]), title: "排版")
        let italicFont = hover.text.textStorage?.attribute(.font, at: styledRange.location, effectiveRange: nil) as? NSFont
        let emphasisChineseFont = hover.text.textStorage?.attribute(.font, at: chineseRange.location, effectiveRange: nil) as? NSFont
        results["task_hover_style_only_change_refreshes_native_attributes"] = hover.currentBody == boldBody && boldFont?.fontDescriptor.symbolicTraits.contains(.bold) == true && italicFont?.fontDescriptor.symbolicTraits.contains(.italic) == true && italicFont?.fontDescriptor.symbolicTraits.contains(.bold) == false && boldChineseFont?.fontDescriptor.symbolicTraits.contains(.bold) == true && emphasisChineseFont?.fontDescriptor.symbolicTraits.contains(.bold) == false
        let reading = CueTaskDetail(raw: ["messages": [["role": "assistant", "text": String(repeating: "这是用于滚动验收的可见回复。\n", count: 80)]]])
        hover.update(detail: reading, title: "长回复")
        hover.text.setSelectedRange(NSRange(location: 0, length: 0))
        hover.scroll.contentView.scroll(to: NSPoint(x: 0, y: 20))
        let beforeScroll = hover.scroll.contentView.bounds.origin.y
        hover.advanceScroll(elapsed: 0.1)
        results["task_hover_auto_scroll_moves_slowly"] = hover.scroll.contentView.bounds.origin.y > beforeScroll && hover.scroll.contentView.bounds.origin.y <= beforeScroll + 2
        hover.overPanel = true
        let paused = hover.scroll.contentView.bounds.origin.y
        hover.advanceScroll(elapsed: 0.1)
        results["task_hover_pointer_inside_pauses_auto_scroll"] = hover.scroll.contentView.bounds.origin.y == paused
        let appended = CueTaskDetail(raw: ["messages": [["role": "assistant", "text": reading.messages[0].text + "\n新增可见进展。"]]])
        hover.update(detail: appended, title: "长回复更新")
        results["task_hover_refresh_preserves_reading_position"] = abs(hover.scroll.contentView.bounds.origin.y - paused) < 1
        hover.overPanel = false; hover.text.setSelectedRange(NSRange(location: 0, length: 2))
        hover.advanceScroll(elapsed: 0.1)
        results["task_hover_text_selection_pauses_auto_scroll"] = abs(hover.scroll.contentView.bounds.origin.y - paused) < 1
        let anchor = NSView(frame: NSRect(x: 0, y: 0, width: 400, height: 50))
        hover.hover(threadID: "fixture", title: "短暂停留", anchor: anchor, provider: { reading })
        hover.leaveRow()
        results["task_hover_leaving_before_delay_cancels_open"] = hover.showWork?.isCancelled == true
        hover.hide()
        results["task_hover_hide_discards_preview_memory"] = hover.currentBody.isEmpty && hover.text.string.isEmpty
        hover.update(detail: styled, title: "重新打开")
        results["task_hover_reopening_same_markdown_is_not_blank"] = hover.currentBody.contains("同样文字") && !hover.renderedMessages.isEmpty
        hover.hide()
        return results
    }

    @discardableResult static func captureFixture(to output: URL, dark: Bool = true) -> Bool {
        _ = NSApplication.shared
        let now = Date()
        let reply = "## 本轮进展\n\n已保留 **当前对话** 和待续记录。\n\n- 问题回复显示为正常文字\n- 移入浮窗暂停滚动，可选择复制\n\n```swift\nlet status = \"继续处理\"\n```\n\n可先核对 `当前模型`，再决定下一步。"
        let detail = CueTaskDetail(raw: ["status": "running", "started_at": now.addingTimeInterval(-754).timeIntervalSince1970, "elapsed_status": "complete", "model": "gpt-6.1-sol", "reasoning_effort": "high", "service_tier": "fast", "total_tokens": 182476, "input_tokens": 8476, "output_tokens": 12000, "cache_read_tokens": 162000, "cache_creation_tokens": 0, "tokens_status": "complete", "messages": [["role": "user", "text": "每周额度已重置，请整理接下来要继续的任务。", "created_at": now.addingTimeInterval(-754).timeIntervalSince1970], ["role": "assistant", "text": reply, "created_at": now.addingTimeInterval(-20).timeIntervalSince1970]]])
        let hover = CueTaskHoverController(); hover.makePanel(); hover.update(detail: detail, title: "界面验收样例 · 当前任务详情")
        guard let window = hover.panel, let view = window.contentView else { return false }
        window.appearance = NSAppearance(named: dark ? .darkAqua : .aqua); window.displayIfNeeded()
        guard let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return false }
        view.cacheDisplay(in: view.bounds, to: bitmap)
        guard let data = bitmap.representation(using: .png, properties: [:]) else { return false }
        do { try data.write(to: output); hover.hide(); return true } catch { return false }
    }
}
