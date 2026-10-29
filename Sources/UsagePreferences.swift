import AppKit
import Foundation

/// Display preferences and exact per-model API reference prices. OAuth never
/// enters this window or the local worker used by it.
final class CueUsagePreferencesController: NSWindowController {
    var onNavigateSchedule: (() -> Void)?
    var onNavigatePrices: (() -> Void)?
    private let root = CuePreferencesSurface(frame: NSRect(x: 0, y: 0, width: 650, height: 660))
    private var isEmbedded = false
    private let client: CueUsageClient
    private let home: URL
    private var scheduleController: CueUsageScheduleController?
    private var sourcesController: CuePriceSourcesController?
    private let currency = NSPopUpButton(), exchangeMode = NSPopUpButton(), model = NSPopUpButton()
    private let rate = NSTextField(string: "")
    private let rateInfo = NSTextField(wrappingLabelWithString: "正在读取…")
    private let sourceInfo = NSTextField(wrappingLabelWithString: "")
    private let feedback = NSTextField(wrappingLabelWithString: "")
    private let fields = (0..<4).map { _ in NSTextField(string: "") }
    private let saveButton = NSButton(title: "保存显示设置", target: nil, action: nil)
    private let priceButton = NSButton(title: "保存此模型单价", target: nil, action: nil)
    private let resetButton = NSButton(title: "恢复来源队列价格", target: nil, action: nil)
    private let refreshButton = NSButton(title: "更新公开汇率", target: nil, action: nil)
    private let scheduleButton = NSButton(title: "同步频率…", target: nil, action: nil)
    private let sourcesButton = NSButton(title: "模型价格来源…", target: nil, action: nil)
    private var pricing: [[String: Any]] = []
    private var preferences: [String: Any] = [:]
    private let onChange: () -> Void
    private var busy = false
    private var loaded = false
    private var pendingPricingRefresh = false
    private var modelPriceBaseline: [String] = []
    private let knownModels: [String]
    private let priceKeys = ["input", "output", "cache_read", "cache_creation"]

    init(home: URL, knownModels: [String] = [], onChange: @escaping () -> Void) {
        self.home = home; self.client = CueUsageClient(home: home); self.knownModels = knownModels; self.onChange = onChange
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 650, height: 660), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "统计设置"; window.isReleasedWhenClosed = false; window.center()
        super.init(window: window)
        window.contentView = root
        func label(_ text: String, _ y: CGFloat, size: CGFloat = 12, weight: NSFont.Weight = .regular) {
            let field = NSTextField(labelWithString: text); field.font = .systemFont(ofSize: size, weight: weight)
            field.frame = NSRect(x: 26, y: y, width: 598, height: 22); root.addSubview(field)
        }
        label("统计设置", 22, size: 19, weight: .semibold)
        scheduleButton.frame = NSRect(x: 300, y: 20, width: 133, height: 30)
        sourcesButton.frame = NSRect(x: 441, y: 20, width: 179, height: 30)
        scheduleButton.bezelStyle = .rounded; scheduleButton.target = self; scheduleButton.action = #selector(showSchedule)
        sourcesButton.bezelStyle = .rounded; sourcesButton.target = self; sourcesButton.action = #selector(showSources)
        scheduleButton.identifier = NSUserInterfaceItemIdentifier("usage-settings.schedule")
        sourcesButton.identifier = NSUserInterfaceItemIdentifier("usage-settings.price-sources")
        root.addSubview(scheduleButton); root.addSubview(sourcesButton)
        label("按模型 API 单价估算；与订阅额度是不同口径。", 53)
        label("显示货币", 98); currency.frame = NSRect(x: 167, y: 94, width: 240, height: 28)
        for (id, text) in [("CNY", "人民币 CNY"), ("USD", "美元 USD"), ("both", "人民币 + 美元")] { currency.addItem(withTitle: text); currency.lastItem?.representedObject = id }
        label("美元兑人民币", 139); exchangeMode.frame = NSRect(x: 167, y: 135, width: 240, height: 28)
        for (id, text) in [("auto", "自动 · 公开日参考汇率"), ("manual", "手动设置")] { exchangeMode.addItem(withTitle: text); exchangeMode.lastItem?.representedObject = id }
        exchangeMode.target = self; exchangeMode.action = #selector(exchangeChanged)
        label("1 USD = 多少 CNY", 180); rate.frame = NSRect(x: 167, y: 177, width: 150, height: 25)
        rate.placeholderString = "如 7.20"; rate.setAccessibilityLabel("自定义美元兑人民币汇率")
        refreshButton.frame = NSRect(x: 424, y: 134, width: 196, height: 30)
        rateInfo.frame = NSRect(x: 28, y: 216, width: 590, height: 48); rateInfo.font = .systemFont(ofSize: 11); rateInfo.textColor = .secondaryLabelColor
        saveButton.frame = NSRect(x: 424, y: 268, width: 196, height: 31)
        label("模型单价 · USD / 百万 Tokens", 323, size: 16, weight: .semibold)
        model.frame = NSRect(x: 26, y: 357, width: 594, height: 28); model.target = self; model.action = #selector(modelChanged)
        sourceInfo.frame = NSRect(x: 29, y: 393, width: 588, height: 36); sourceInfo.font = .systemFont(ofSize: 11); sourceInfo.textColor = .secondaryLabelColor
        for (index, title) in ["新增输入", "输出", "缓存读取", "缓存写入"].enumerated() {
            let x = CGFloat(28 + index * 148)
            let caption = NSTextField(labelWithString: title); caption.frame = NSRect(x: x, y: 437, width: 139, height: 20); caption.font = .systemFont(ofSize: 11); root.addSubview(caption)
            fields[index].frame = NSRect(x: x, y: 464, width: 132, height: 26); fields[index].placeholderString = "USD / 1M"; fields[index].setAccessibilityLabel(title + " API 单价")
            root.addSubview(fields[index])
        }
        priceButton.frame = NSRect(x: 424, y: 509, width: 196, height: 31)
        resetButton.frame = NSRect(x: 217, y: 509, width: 200, height: 31)
        feedback.frame = NSRect(x: 28, y: 556, width: 590, height: 76); feedback.font = .systemFont(ofSize: 11); feedback.textColor = .secondaryLabelColor
        feedback.stringValue = "历史用量按当前单价重新估算。日志未记录的长上下文、服务档位和协议优惠差价不自动推断。"
        for button in [saveButton, priceButton, resetButton, refreshButton] { button.bezelStyle = .rounded; button.target = self }
        saveButton.action = #selector(savePreferences); priceButton.action = #selector(savePrice); resetButton.action = #selector(resetPrice); refreshButton.action = #selector(refreshRate)
        [currency, exchangeMode, model, rate, rateInfo, sourceInfo, feedback, saveButton, priceButton, resetButton, refreshButton].forEach { root.addSubview($0) }
        setBusy(false)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    /// The retained canvas can move between the main panel's tag containers.
    func embeddedContentView() -> NSView {
        if !isEmbedded { isEmbedded = true; window?.orderOut(nil); window?.contentView = nil }
        return root
    }
    func activateEmbedded() { if !loaded && !busy { load() } }
    func deactivateEmbedded() { /* Keep unsubmitted fields while changing tags. */ }
    /// Source changes must not reset currency, exchange-rate or model-price drafts.
    func refreshPricingPreservingDrafts() {
        guard !busy else { pendingPricingRefresh = true; return }
        guard loaded else { load(); return }
        setBusy(true)
        client.run(["settings"], module: "vibecue.usage_money") { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let data):
                if let prefs = data["prefs"] as? [String: Any], let overrides = prefs["model_overrides"] {
                    self.preferences["model_overrides"] = overrides
                }
                self.applyPricing(data, preservingDrafts: true)
            case .failure(let error):
                self.feedback.stringValue = error.localizedDescription; self.feedback.textColor = .systemRed
            }
            self.setBusy(false)
        }
    }
    override func showWindow(_ sender: Any?) {
        if isEmbedded { activateEmbedded(); root.window?.makeKeyAndOrderFront(nil); return }
        super.showWindow(sender); window?.makeKeyAndOrderFront(nil); load()
    }
    private func setBusy(_ value: Bool) {
        busy = value
        for control in [currency, exchangeMode, model, rate] as [NSControl] { control.isEnabled = !value && loaded }
        for button in [saveButton, priceButton, resetButton, refreshButton] { button.isEnabled = !value && loaded }
        refreshButton.isEnabled = !value && loaded && preferences["fx_mode"] as? String != "manual"
        refreshButton.toolTip = refreshButton.isEnabled ? "从 Frankfurter 更新公开的 USD/CNY 日参考汇率" : "切换并保存自动汇率模式后可更新"
        rate.isEnabled = !value && loaded && selected(exchangeMode) == "manual"
        priceButton.isEnabled = !value && loaded && !pricing.isEmpty; resetButton.isEnabled = priceButton.isEnabled
        fields.forEach { $0.isEnabled = !value && loaded && !pricing.isEmpty }
        if !value && pendingPricingRefresh {
            pendingPricingRefresh = false
            DispatchQueue.main.async { [weak self] in self?.refreshPricingPreservingDrafts() }
        }
    }
    private func selected(_ popup: NSPopUpButton) -> String { popup.selectedItem?.representedObject as? String ?? "" }
    private func select(_ popup: NSPopUpButton, _ id: String) { if let item = popup.itemArray.first(where: { $0.representedObject as? String == id }) { popup.select(item) } }
    private func load() {
        setBusy(true)
        client.run(["settings"], module: "vibecue.usage_money") { [weak self] result in
            guard let self else { return }; self.setBusy(false)
            switch result { case .success(let data): self.apply(data)
            case .failure(let error): self.feedback.stringValue = error.localizedDescription; self.feedback.textColor = .systemRed }
        }
    }
    private func apply(_ data: [String: Any]) {
        preferences = data["prefs"] as? [String: Any] ?? [:]; loaded = true
        select(currency, preferences["currency"] as? String ?? "CNY"); select(exchangeMode, preferences["fx_mode"] as? String ?? "auto")
        rate.stringValue = (preferences["manual_rate"] as? NSNumber)?.stringValue ?? ""
        applyPricing(data, preservingDrafts: false)
        updateRate(data["fx"] as? [String: Any] ?? [:]); setBusy(false)
    }
    private func applyPricing(_ data: [String: Any], preservingDrafts: Bool) {
        let old = selected(model), oldValues = fields.map(\.stringValue), oldBaseline = modelPriceBaseline
        let hasDraft = preservingDrafts && !old.isEmpty && oldBaseline.count == fields.count && oldValues != oldBaseline
        pricing = data["pricing"] as? [[String: Any]] ?? []
        let pricedIDs = Set(pricing.compactMap { $0["model"] as? String ?? $0["id"] as? String ?? $0["model_id"] as? String })
        var extras = knownModels
        if preservingDrafts && !old.isEmpty { extras.append(old) }
        pricing += Set(extras).filter { !$0.isEmpty && !pricedIDs.contains($0) }.map { ["model": $0, "source": "unpriced"] }
        pricing.sort { ($0["model"] as? String ?? "") < ($1["model"] as? String ?? "") }; model.removeAllItems()
        for row in pricing { guard let id = row["model"] as? String ?? row["id"] as? String ?? row["model_id"] as? String else { continue }; model.addItem(withTitle: id); model.lastItem?.representedObject = id }
        select(model, old); modelChanged()
        if hasDraft {
            for (field, value) in zip(fields, oldValues) { field.stringValue = value }
            modelPriceBaseline = oldBaseline
            let previousInfo = sourceInfo.stringValue, previousTip = sourceInfo.toolTip ?? previousInfo
            let notice = "来源价格已更新；未保存单价已保留，切换模型后采用新价格。"
            sourceInfo.stringValue = notice + "\n" + previousInfo
            sourceInfo.toolTip = notice + "\n" + previousTip
        }
    }
    private func updateRate(_ fx: [String: Any]) {
        if let value = fx["rate"] as? NSNumber {
            let date = fx["date"] as? String ?? ""; let source = fx["source"] as? String ?? ""
            rateInfo.stringValue = "1 USD = \(value.stringValue) CNY · \(source) \(date)" + (fx["stale"] as? Bool == true ? " · 缓存，可能已过期" : "")
        } else { rateInfo.stringValue = "尚无可用汇率。可更新公开汇率，或选择手动设置。" }
        rateInfo.toolTip = fx["error"] as? String
    }
    @objc private func exchangeChanged() { setBusy(busy) }
    @objc private func modelChanged() {
        guard let row = pricing.first(where: { ($0["model"] as? String ?? $0["id"] as? String ?? $0["model_id"] as? String) == selected(model) }) else { fields.forEach { $0.stringValue = "" }; modelPriceBaseline = fields.map(\.stringValue); return }
        for (index, key) in priceKeys.enumerated() { fields[index].stringValue = (row[key] as? NSNumber)?.stringValue ?? (row[key] as? String ?? "") }
        modelPriceBaseline = fields.map(\.stringValue)
        let source = row["source"] as? String ?? "unpriced"
        let name = row["source_name"] as? String ?? (source == "custom" ? "手动模型单价" : source == "ccswitch" ? "CC Switch 本机价格" : source)
        let missing = priceKeys.filter { row[$0] == nil || row[$0] is NSNull }
        sourceInfo.stringValue = source == "unpriced" ? "此模型未定价，可填写四项单价。按精确模型标识匹配。" : "当前来源：\(name)" + (row["source_stale"] as? Bool == true ? " · 缓存，可能已过期。" : "。") + (missing.isEmpty ? "按来源队列与精确模型标识匹配。" : "部分单价未提供，实际使用该项时保持未定价，可手动补充。")
        sourceInfo.toolTip = sourceInfo.stringValue
        if let updated = row["source_updated_at"] as? NSNumber {
            let formatter = DateFormatter(); formatter.locale = Locale(identifier: "zh_CN"); formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
            sourceInfo.toolTip = sourceInfo.stringValue + "\n价格读取于 " + formatter.string(from: Date(timeIntervalSince1970: updated.doubleValue))
        }
    }
    @objc private func showSchedule() {
        if let onNavigateSchedule { onNavigateSchedule(); return }
        if scheduleController == nil { scheduleController = CueUsageScheduleController(home: home, onChange: { [weak self] in self?.onChange() }) }
        scheduleController?.showWindow(nil); scheduleController?.window?.makeKeyAndOrderFront(nil)
    }
    @objc private func showSources() {
        if let onNavigatePrices { onNavigatePrices(); return }
        if sourcesController == nil { sourcesController = CuePriceSourcesController(home: home, onChange: { [weak self] in self?.load(); self?.onChange() }) }
        sourcesController?.showWindow(nil); sourcesController?.window?.makeKeyAndOrderFront(nil)
    }
    @objc private func refreshRate() {
        guard !busy else { return }; setBusy(true); feedback.stringValue = "正在更新公开日参考汇率…"
        client.run(["refresh", "--force"], module: "vibecue.usage_money") { [weak self] result in
            guard let self else { return }; self.setBusy(false)
            switch result { case .success(let data):
                let fx = data["fx"] as? [String: Any] ?? data
                self.updateRate(fx); self.feedback.stringValue = fx["error"] as? String ?? "汇率信息已更新。"; self.onChange()
            case .failure(let error): self.feedback.stringValue = error.localizedDescription }
        }
    }
    @objc private func savePreferences() {
        var patch: [String: Any] = ["currency": selected(currency), "fx_mode": selected(exchangeMode)]
        if selected(exchangeMode) == "manual" {
            guard let number = Double(rate.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)), number.isFinite, number > 0 else { feedback.stringValue = "请输入大于 0 的美元兑人民币汇率。"; return }
            patch["manual_rate"] = number
        }
        save(patch, message: "货币与汇率设置已保存。")
    }
    @objc private func savePrice() {
        guard !selected(model).isEmpty else { return }; var values: [String: Double] = [:]
        for (index, key) in priceKeys.enumerated() {
            guard let value = Double(fields[index].stringValue.trimmingCharacters(in: .whitespacesAndNewlines)), value.isFinite, value >= 0 else { feedback.stringValue = "四项单价都需要填写，可填 0；不能为负数。"; return }; values[key] = value
        }
        var overrides = preferences["model_overrides"] as? [String: Any] ?? [:]; overrides[selected(model)] = values
        save(["model_overrides": overrides], message: "单价已保存到手动来源；实际采用由该来源的启用状态和优先级决定。")
    }
    @objc private func resetPrice() {
        var overrides = preferences["model_overrides"] as? [String: Any] ?? [:]; overrides.removeValue(forKey: selected(model))
        save(["model_overrides": overrides], message: "已移除此模型手动单价，恢复按来源队列查找。")
    }
    private func save(_ patch: [String: Any], message: String) {
        guard !busy, let input = try? JSONSerialization.data(withJSONObject: patch) else { return }; setBusy(true)
        client.run(["save"], module: "vibecue.usage_money", input: input) { [weak self] result in
            guard let self else { return }; self.setBusy(false)
            switch result { case .success(let data): self.apply(data); self.feedback.stringValue = message; self.feedback.textColor = .secondaryLabelColor; self.onChange()
            case .failure(let error): self.feedback.stringValue = error.localizedDescription; self.feedback.textColor = .systemRed }
        }
    }
    static func captureFixture(to url: URL, dark: Bool) throws {
        let controller = CueUsagePreferencesController(home: URL(fileURLWithPath: "/private/tmp/vibecue-money-offline-fixture"), knownModels: ["model-without-price"], onChange: {})
        controller.window?.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        controller.apply(["prefs": ["currency": "both", "fx_mode": "auto"], "fx": ["rate": 7.20, "date": "2026-10-28", "source": "Frankfurter · 每日参考汇率"], "pricing": [["model": "example-model", "input": 2, "output": 10, "cache_read": 0.1, "cache_creation": 2.5, "source": "ccswitch"]]])
        guard let root = controller.window?.contentView else { throw CocoaError(.fileReadUnknown) }
        root.layoutSubtreeIfNeeded()
        guard let bitmap = root.bitmapImageRepForCachingDisplay(in: root.bounds) else { throw CocoaError(.fileWriteUnknown) }
        root.cacheDisplay(in: root.bounds, to: bitmap)
        guard let png = bitmap.representation(using: .png, properties: [:]) else { throw CocoaError(.fileWriteUnknown) }
        try png.write(to: url)
    }
    static func validationReport() -> [String: Bool] {
        let controller = CueUsagePreferencesController(home: URL(fileURLWithPath: "/private/tmp/vibecue-money-offline-fixture"), knownModels: ["unknown-model"], onChange: {})
        controller.apply(["prefs": ["currency": "USD", "fx_mode": "manual", "manual_rate": 7.1], "pricing": [["model": "exact-model", "input": 2, "output": 10, "cache_read": 0.1, "cache_creation": 0, "source": "ccswitch"]]])
        let manual = controller.rate.isEnabled
        controller.select(controller.exchangeMode, "auto"); controller.exchangeChanged()
        var results = CuePriceSourcesController.validationReport()
        results.merge(["unknown_cost_never_displays_zero": CueUsageMoney.display([:], currency: "USD") == "未定价",
                "zero_price_displays_zero": CueUsageMoney.display(["cost_usd": 0], currency: "USD") == "$0.0000",
                "missing_fx_is_explicit": CueUsageMoney.display(["cost_usd": 1], currency: "CNY") == "汇率未获取",
                "dual_currency_display": CueUsageMoney.display(["cost_usd": 1, "cost_cny": 7.1], currency: "both") == "7.10 ¥ / $1.00",
                "tiny_positive_cost_is_not_zero": CueUsageMoney.display(["cost_usd": 0.0000002], currency: "USD") == "$<0.0001",
                "small_summary_cost_is_not_rounded_to_zero": CueUsageMoney.display(["cost_usd": 0.003], currency: "USD", compact: true) == "$0.0030",
                "large_cny_summary_keeps_full_yuan_and_trailing_symbol": CueUsageMoney.display(["cost_usd": 55_000, "cost_cny": 390_700], currency: "CNY", compact: true) == "390,700.00 ¥",
                "million_cny_never_uses_scientific_or_compact_notation": CueUsageMoney.display(["cost_cny": 1_234_567.89], currency: "CNY", compact: true) == "1,234,567.89 ¥",
                "tiny_positive_cny_preserves_nonzero_lower_bound": CueUsageMoney.display(["cost_cny": 0.0000002], currency: "CNY") == "<0.0001 ¥",
                "small_cny_request_keeps_four_decimal_places": CueUsageMoney.display(["cost_cny": 0.3224], currency: "CNY") == "0.3224 ¥",
                "zero_cny_is_an_explicit_yuan_value": CueUsageMoney.display(["cost_cny": 0], currency: "CNY") == "0.0000 ¥",
                "large_usd_summary_also_keeps_full_currency_amount": CueUsageMoney.display(["cost_usd": 12_345.67], currency: "USD", compact: true) == "$12,345.67",
                "invalid_money_numbers_never_become_a_cost": CueUsageMoney.display(["cost_cny": true], currency: "CNY") == "未定价" && CueUsageMoney.display(["cost_cny": Double.nan], currency: "CNY") == "未定价",
                "unpriced_logged_models_can_be_customized": controller.pricing.contains { $0["model"] as? String == "unknown-model" },
                "manual_fx_field_enabled_only_for_manual_mode": manual && !controller.rate.isEnabled,
                "model_zero_rate_preserved_in_editor": controller.fields.last?.stringValue == "0"]) { _, new in new }
        let partial = CueUsagePreferencesController(home: URL(fileURLWithPath: "/private/tmp/vibecue-money-offline-fixture"), onChange: {})
        partial.apply(["prefs": ["currency": "USD"], "pricing": [["model": "partial-model", "input": 2, "output": 10, "cache_read": NSNull(), "cache_creation": NSNull(), "source": "public-source", "source_name": "公开样例"]]])
        results["missing_source_cache_prices_stay_blank_in_editor"] = partial.fields[2].stringValue.isEmpty && partial.fields[3].stringValue.isEmpty && partial.sourceInfo.stringValue.contains("未提供")
        results["effective_price_source_name_is_visible"] = partial.sourceInfo.stringValue.contains("公开样例")
        let host = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 650, height: 660), styleMask: [.titled], backing: .buffered, defer: false)
        let view = controller.embeddedContentView(); host.contentView = view
        controller.rate.stringValue = "8.88"; controller.deactivateEmbedded(); controller.activateEmbedded()
        results["usage_preferences_embedded_canvas_is_retained_and_reusable"] = view === controller.embeddedContentView() && view.window === host && controller.window?.contentView !== view
        results["usage_preferences_tag_switch_preserves_unsubmitted_fields"] = controller.rate.stringValue == "8.88" && !controller.busy
        controller.select(controller.currency, "both"); controller.select(controller.exchangeMode, "manual")
        controller.fields[0].stringValue = "123.45"
        let pendingPrice = controller.fields.map(\.stringValue)
        let newPricing: [String: Any] = ["pricing": [["model": "exact-model", "input": 3, "output": 11, "cache_read": 0.2, "cache_creation": 0, "source": "new-public-source", "source_name": "新来源"]]]
        controller.applyPricing(newPricing, preservingDrafts: true)
        results["pricing_source_refresh_preserves_currency_and_exchange_drafts"] = controller.selected(controller.currency) == "both" && controller.selected(controller.exchangeMode) == "manual" && controller.rate.stringValue == "8.88"
        results["pricing_source_refresh_preserves_unsubmitted_model_price"] = controller.fields.map(\.stringValue) == pendingPrice && controller.sourceInfo.stringValue.contains("未保存单价已保留") && controller.sourceInfo.stringValue.contains("新来源")
        controller.modelChanged(); controller.applyPricing(["pricing": [["model": "exact-model", "input": 4, "output": 12, "cache_read": 0.3, "cache_creation": 0, "source": "latest"]]], preservingDrafts: true)
        results["pricing_source_refresh_updates_clean_model_fields"] = controller.fields[0].stringValue == "4" && !controller.sourceInfo.stringValue.contains("未保存")
        var visits: [String] = []
        controller.onNavigateSchedule = { visits.append("schedule") }; controller.onNavigatePrices = { visits.append("prices") }
        controller.showSchedule(); controller.showSources()
        results["usage_preferences_buttons_navigate_main_panel_tags"] = visits == ["schedule", "prices"] && controller.scheduleController == nil && controller.sourcesController == nil
        host.contentView = nil
        return results
    }
}

private final class CuePreferencesSurface: NSView {
    override var isFlipped: Bool { true }
    override func draw(_ dirtyRect: NSRect) { NSColor.windowBackgroundColor.setFill(); dirtyRect.fill() }
}

enum CueUsageMoney {
    /// Formatting is presentation-only. Keep the exact NSNumber supplied by the
    /// backend, while reusing two fixed-format instances per calling thread.
    private static func amountFormatter(precision: Int) -> NumberFormatter {
        let key = "local.vibecue.money.amount." + String(precision)
        if let cached = Thread.current.threadDictionary[key] as? NumberFormatter { return cached }
        let formatter = NumberFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX"); formatter.numberStyle = .decimal
        formatter.usesGroupingSeparator = true; formatter.groupingSeparator = ","; formatter.decimalSeparator = "."
        formatter.usesSignificantDigits = false; formatter.maximumIntegerDigits = 309
        formatter.minimumFractionDigits = precision; formatter.maximumFractionDigits = precision
        formatter.roundingMode = .halfUp
        Thread.current.threadDictionary[key] = formatter
        return formatter
    }
    /// Money always stays in base currency units, including narrow summaries.
    /// `compact` remains in the shared API for callers that compact token counts.
    static func display(_ row: [String: Any], currency: String, compact: Bool = false) -> String {
        func amount(_ key: String, prefix: String = "", suffix: String = "") -> String? {
            guard let value = row[key] as? NSNumber,
                  CFGetTypeID(value) != CFBooleanGetTypeID(), value.doubleValue.isFinite else { return nil }
            let number = value.doubleValue
            if number > 0 && number < 0.0001 { return prefix + "<0.0001" + suffix }
            let precision = number >= 1 ? 2 : 4
            let formatter = amountFormatter(precision: precision)
            guard let formatted = formatter.string(from: value) else { return nil }
            return prefix + formatted + suffix
        }
        let usd = amount("cost_usd", prefix: "$"), cny = amount("cost_cny", suffix: " ¥")
        if currency == "USD" { return usd ?? "未定价" }
        if currency == "both" { return [cny, usd].compactMap { $0 }.joined(separator: " / ").nilIfEmpty ?? "未定价" }
        return cny ?? (usd == nil ? "未定价" : "汇率未获取")
    }
}
private extension String { var nilIfEmpty: String? { isEmpty ? nil : self } }
