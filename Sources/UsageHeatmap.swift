import AppKit

/// Calendar blocks share one scale, including expanded historic 53-week blocks.
final class CueUsageHeatmap: NSView {
    struct MonthLabel { let value: String; let frame: NSRect }
    struct Geometry {
        let grid: NSRect
        let cell: CGFloat
        let gap: CGFloat
        let monthLabels: [MonthLabel]
        var stride: CGFloat { cell + gap }
    }
    var days: [[String: Any]] = [] { didSet { needsDisplay = true; if let key = hoverKey, let row = days.first(where: { $0["date"] as? String == key }) { updatePreview(day: key, row: row) } } }
    var currency = "CNY" { didSet { needsDisplay = true } }
    var referenceDate: Date?
    var weeks = 53 { didSet { needsDisplay = true; hidePreview() } }
    var selectedDate: String? { didSet { needsDisplay = true } }
    var metric = "tokens" { didSet { needsDisplay = true } }
    var chartStyle = "heatmap" { didSet { needsDisplay = true; hidePreview() } }
    var periodStart: String?, periodEnd: String?
    var trendPoints: [[String: Any]]? { didSet { needsDisplay = true; if let key = hoverKey, let stamp = key.split(separator: "#").last.flatMap({ Double($0) }), key.contains("#"), let point = trendPoints?.first(where: { ($0["timestamp"] as? NSNumber)?.doubleValue == stamp }) { updatePreview(day: key, row: point) } } }
    var trendGranularity = "day"
    var onSelectDate: ((String) -> Void)?
    private var tracking: NSTrackingArea?
    private var cells: [(NSRect, String, [String: Any])] = []
    private var hoverKey: String?
    private var hoverPanel: NSPanel?
    private var hoverLabels: [NSTextField] = []
    private var hoverValues: [NSTextField] = []
    deinit { hoverPanel?.orderOut(nil) }
    override var isFlipped: Bool { true }
    static func cellSize(width: CGFloat) -> CGFloat { min(22, max(9, (width - 56 - 52 * 3) / 53)) }
    static func preferredHeight(width: CGFloat) -> CGFloat { ceil(22 + cellSize(width: width) * 7 + 6 * 3 + 30) }
    var sections: Int { max(1, min(10, (weeks + 52) / 53)) }
    func contentHeight(width: CGFloat) -> CGFloat { chartStyle == "heatmap" ? Self.preferredHeight(width: width) * CGFloat(sections) + CGFloat(max(0, sections - 1)) * 34 : Self.preferredHeight(width: width) }
    private func range(section: Int = 0) -> (Calendar, Date, Date) {
        let calendar = Calendar.current
        let today = calendar.startOfDay(for: referenceDate ?? Date())
        let weekday = (calendar.component(.weekday, from: today) + 5) % 7
        let monday = calendar.date(byAdding: .day, value: -weekday, to: today)!
        let start = calendar.date(byAdding: .day, value: -(52 + section * 53) * 7, to: monday)!
        return (calendar, start, today)
    }
    var rangeDescription: String {
        let (calendar, _, today) = range(), (_, start, _) = range(section: sections - 1)
        let formatter = DateFormatter(); formatter.calendar = calendar; formatter.locale = Locale(identifier: "en_US_POSIX"); formatter.dateFormat = "yyyy.MM.dd"
        return formatter.string(from: start) + " — " + formatter.string(from: today)
    }
    func geometry(width: CGFloat, section: Int = 0) -> Geometry {
        let (calendar, start, _) = range(section: section), gap: CGFloat = 3
        let cell = Self.cellSize(width: width), gridWidth = 53 * cell + 52 * gap
        let offset = CGFloat(section) * (Self.preferredHeight(width: width) + 34)
        let grid = NSRect(x: 40 + max(0, (width - 56 - gridWidth) / 2), y: offset + 22, width: gridWidth, height: 7 * cell + 6 * gap)
        let attributes: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 9.5)]
        var candidates: [MonthLabel] = [], priorMonth = -1
        for week in 0..<53 {
            let date = calendar.date(byAdding: .day, value: week * 7, to: start)!, month = calendar.component(.month, from: date)
            guard month != priorMonth else { continue }; priorMonth = month
            let label = month == 1 ? "\(calendar.component(.year, from: date))年1月" : "\(month)月"
            let labelWidth = ceil((label as NSString).size(withAttributes: attributes).width)
            candidates.append(MonthLabel(value: label, frame: NSRect(x: min(grid.maxX - labelWidth, grid.minX + CGFloat(week) * (cell + gap)), y: offset + 3, width: labelWidth, height: 15)))
        }
        var accepted: [MonthLabel] = []
        for (index, label) in candidates.enumerated() {
            if index == 0, candidates.count > 1, label.frame.maxX + 8 > candidates[1].frame.minX { continue }
            if let previous = accepted.last, previous.frame.maxX + 8 > label.frame.minX { continue }
            accepted.append(label)
        }
        return Geometry(grid: grid, cell: cell, gap: gap, monthLabels: accepted)
    }
    override func updateTrackingAreas() {
        super.updateTrackingAreas(); if let tracking { removeTrackingArea(tracking) }
        let area = NSTrackingArea(rect: .zero, options: [.mouseMoved, .mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self)
        addTrackingArea(area); tracking = area
    }
    override func viewWillMove(toWindow newWindow: NSWindow?) { if newWindow == nil { hidePreview() }; super.viewWillMove(toWindow: newWindow) }
    override func mouseExited(with event: NSEvent) { hidePreview() }
    override func mouseMoved(with event: NSEvent) {
        let point = convert(event.locationInWindow, from: nil)
        guard let cell = cells.first(where: { $0.0.contains(point) }) else { hidePreview(); return }
        guard hoverKey != cell.1 else { return }; hoverKey = cell.1; presentPreview(day: cell.1, row: cell.2, rect: cell.0)
    }
    override func mouseDown(with event: NSEvent) {
        let point = convert(event.locationInWindow, from: nil)
        guard let cell = cells.first(where: { $0.0.contains(point) }) else { return }
        hidePreview(); onSelectDate?(String(cell.1.prefix(10)))
    }
    override func draw(_ dirtyRect: NSRect) {
        cells = []
        NSColor.controlBackgroundColor.withAlphaComponent(0.45).setFill(); NSBezierPath(roundedRect: bounds, xRadius: 10, yRadius: 10).fill()
        if chartStyle != "heatmap" { drawTrend(); return }
        let (_, _, today) = range()
        let formatter = DateFormatter(); formatter.locale = Locale(identifier: "en_US_POSIX"); formatter.dateFormat = "yyyy-MM-dd"
        let values = Dictionary(days.compactMap { row -> (String, [String: Any])? in guard let date = row["date"] as? String else { return nil }; return (date, row) }, uniquingKeysWith: { _, new in new })
        let metricKey = metric == "cost" ? (currency == "USD" ? "cost_usd" : "cost_cny") : metric
        let maximum = max(metric == "cache_hit_rate" ? 0.01 : 1, days.map { Self.number($0, metricKey) }.max() ?? 1)
        let attributes: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 9.5), .foregroundColor: NSColor.secondaryLabelColor]
        for section in 0..<sections {
            let (calendar, start, _) = range(section: section), layout = geometry(width: bounds.width, section: section)
            for label in layout.monthLabels { (label.value as NSString).draw(in: label.frame, withAttributes: attributes) }
            for (row, title) in [(0, "周一"), (2, "周三"), (4, "周五"), (6, "周日")] {
                let paragraph = NSMutableParagraphStyle(); paragraph.alignment = .right
                (title as NSString).draw(in: NSRect(x: layout.grid.minX - 31, y: layout.grid.minY + CGFloat(row) * layout.stride + (layout.cell - 13) / 2, width: 24, height: 15), withAttributes: [.font: NSFont.systemFont(ofSize: 9), .foregroundColor: NSColor.secondaryLabelColor, .paragraphStyle: paragraph])
            }
            for week in 0..<53 { for day in 0..<7 {
                let date = calendar.date(byAdding: .day, value: week * 7 + day, to: start)!, key = formatter.string(from: date), row = values[key] ?? [:]
                let rect = NSRect(x: layout.grid.minX + CGFloat(week) * layout.stride, y: layout.grid.minY + CGFloat(day) * layout.stride, width: layout.cell, height: layout.cell)
                let future = date > today, count = Self.number(row, metricKey), level = count <= 0 ? 0 : min(1, max(0.15, sqrt(count / maximum)))
                let inRange = row["in_selected_range"] as? Bool != false
                let color = future ? NSColor.quaternaryLabelColor.withAlphaComponent(0.035) : count <= 0 ? NSColor.quaternaryLabelColor.withAlphaComponent(0.16) : NSColor.controlAccentColor.withAlphaComponent(CGFloat((0.22 + level * 0.74) * (inRange ? 1 : 0.24)))
                color.setFill(); NSBezierPath(roundedRect: rect, xRadius: 2.5, yRadius: 2.5).fill()
                if metric == "cost", Self.number(row, "requests") > 0, !(row[metricKey] is NSNumber) {
                    NSColor.systemOrange.withAlphaComponent(inRange ? 0.8 : 0.3).setStroke(); let unknown = NSBezierPath(roundedRect: rect.insetBy(dx: 1, dy: 1), xRadius: 2, yRadius: 2); unknown.lineWidth = 1; unknown.setLineDash([2, 2], count: 2, phase: 0); unknown.stroke()
                }
                if key == selectedDate { NSColor.labelColor.setStroke(); let border = NSBezierPath(roundedRect: rect.insetBy(dx: -1, dy: -1), xRadius: 3, yRadius: 3); border.lineWidth = 1.5; border.stroke() }
                if !future { cells.append((rect, key, row)) }
            } }
            let y = layout.grid.maxY + 12
            ((section == 0 ? "点击日期查看当天明细 · 所有年份使用同一色标" : "较早 53 周 · 同一色标") as NSString).draw(at: NSPoint(x: layout.grid.minX, y: y), withAttributes: attributes)
            let x = layout.grid.maxX - 113
            ("少" as NSString).draw(at: NSPoint(x: x, y: y), withAttributes: attributes)
            for index in 0..<5 {
                (index == 0 ? NSColor.quaternaryLabelColor.withAlphaComponent(0.16) : NSColor.controlAccentColor.withAlphaComponent(CGFloat(0.22 + Double(index) / 4 * 0.74))).setFill()
                NSBezierPath(roundedRect: NSRect(x: x + 19 + CGFloat(index) * 14, y: y + 1, width: 10, height: 10), xRadius: 2, yRadius: 2).fill()
            }
            ("多" as NSString).draw(at: NSPoint(x: x + 97, y: y), withAttributes: attributes)
        }
    }
    private func drawTrend() {
        let formatter = DateFormatter(); formatter.locale = Locale(identifier: "en_US_POSIX"); formatter.dateFormat = "yyyy-MM-dd"
        var series: [(String, [String: Any])] = []
        var clippedToLoadedRange = false
        if let trendPoints {
            for point in trendPoints.sorted(by: { Self.number($0, "timestamp") < Self.number($1, "timestamp") }) {
                let timestamp = (point["timestamp"] as? NSNumber)?.doubleValue
                let day = timestamp.map { formatter.string(from: Date(timeIntervalSince1970: $0)) } ?? point["date"] as? String ?? ""
                guard !day.isEmpty else { continue }
                let identity = trendGranularity == "hour" ? day + "#" + String(Int(timestamp ?? 0)) : day
                series.append((identity, point))
            }
        } else {
            let values = Dictionary(days.compactMap { row -> (String, [String: Any])? in guard let date = row["date"] as? String else { return nil }; return (date, row) }, uniquingKeysWith: { _, new in new })
            let eligible = days.filter { $0["in_selected_range"] as? Bool != false }.compactMap { $0["date"] as? String }.sorted()
            if let first = periodStart ?? eligible.first, let last = periodEnd ?? eligible.last, let requestedStart = formatter.date(from: first), let requestedEnd = formatter.date(from: last), requestedStart <= requestedEnd {
                let (_, loadedStart, today) = range(section: sections - 1)
                if requestedEnd >= loadedStart, requestedStart <= today {
                    var cursor = max(requestedStart, loadedStart); let end = min(requestedEnd, today)
                    clippedToLoadedRange = requestedStart < loadedStart
                    while cursor <= end && series.count < 3710 {
                        let date = formatter.string(from: cursor); series.append((date, values[date] ?? ["date": date])); cursor = Calendar.current.date(byAdding: .day, value: 1, to: cursor)!
                    }
                }
            }
        }
        guard !series.isEmpty else {
            ("当前筛选暂无趋势数据；较早历史可展开日历查看。" as NSString).draw(at: NSPoint(x: 30, y: 50), withAttributes: [.font: NSFont.systemFont(ofSize: 12), .foregroundColor: NSColor.secondaryLabelColor]); return
        }
        let key = metric == "cost" ? (currency == "USD" ? "cost_usd" : "cost_cny") : metric
        let unknownCosts = metric == "cost" && series.contains { Self.number($0.1, "requests") > 0 && !($0.1[key] is NSNumber) }
        if metric == "cost", !series.contains(where: { $0.1[key] is NSNumber }), series.contains(where: { Self.number($0.1, "requests") > 0 }) {
            let message = key == "cost_cny" && series.contains(where: { $0.1["cost_usd"] is NSNumber }) ? "人民币汇率暂不可用，可在统计设置中切换美元查看。" : "所选区间仅有未定价记录，费用趋势暂不可用。"
            (message as NSString).draw(at: NSPoint(x: 30, y: 50), withAttributes: [.font: NSFont.systemFont(ofSize: 12), .foregroundColor: NSColor.secondaryLabelColor]); return
        }
        let maximum = max(metric == "cache_hit_rate" ? 0.01 : 1, series.map { Self.number($0.1, key) }.max() ?? 1)
        let plot = NSRect(x: 93, y: 18, width: max(50, bounds.width - 115), height: max(50, bounds.height - 51))
        let attributes: [NSAttributedString.Key: Any] = [.font: NSFont.monospacedDigitSystemFont(ofSize: 9, weight: .regular), .foregroundColor: NSColor.secondaryLabelColor]
        for index in 0...3 {
            let fraction = Double(index) / 3, y = plot.maxY - CGFloat(fraction) * plot.height
            NSColor.separatorColor.withAlphaComponent(0.5).setStroke(); let grid = NSBezierPath(); grid.move(to: NSPoint(x: plot.minX, y: y)); grid.line(to: NSPoint(x: plot.maxX, y: y)); grid.lineWidth = 0.5; grid.stroke()
            let amount = maximum * fraction
            let label = metric == "cost" ? CueUsageMoney.display([key: amount], currency: currency) : metric == "cache_hit_rate" ? String(format: "%.0f%%", amount * 100) : CueUsageDashboardController.compact(amount)
            let paragraph = NSMutableParagraphStyle(); paragraph.alignment = .right
            (label as NSString).draw(in: NSRect(x: 5, y: y - 7, width: 79, height: 16), withAttributes: [.font: NSFont.monospacedDigitSystemFont(ofSize: 9, weight: .regular), .foregroundColor: NSColor.secondaryLabelColor, .paragraphStyle: paragraph])
        }
        let stride = plot.width / CGFloat(series.count), path = NSBezierPath()
        var connected = false
        for (index, entry) in series.enumerated() {
            let x = plot.minX + (CGFloat(index) + 0.5) * stride, y = plot.maxY - CGFloat(Self.number(entry.1, key) / maximum) * plot.height
            cells.append((NSRect(x: plot.minX + CGFloat(index) * stride, y: plot.minY, width: max(1, stride), height: plot.height), entry.0, entry.1))
            if metric == "cost", Self.number(entry.1, "requests") > 0, !(entry.1[key] is NSNumber) { NSColor.systemOrange.setFill(); NSBezierPath(ovalIn: NSRect(x: x - 2, y: plot.maxY - 5, width: 4, height: 4)).fill(); connected = false; continue }
            if connected { path.line(to: NSPoint(x: x, y: y)) } else { path.move(to: NSPoint(x: x, y: y)) }; connected = true
            if chartStyle == "bar" { let barWidth = max(0.5, min(38, stride * 0.74)); NSColor.controlAccentColor.withAlphaComponent(0.78).setFill(); NSBezierPath(roundedRect: NSRect(x: x - barWidth / 2, y: y, width: barWidth, height: max(0.5, plot.maxY - y)), xRadius: min(2, stride * 0.15), yRadius: min(2, stride * 0.15)).fill() }
            else if series.count <= 2 { NSColor.controlAccentColor.setFill(); NSBezierPath(ovalIn: NSRect(x: x - 3, y: y - 3, width: 6, height: 6)).fill() }
        }
        if chartStyle == "area", let area = path.copy() as? NSBezierPath {
            if !unknownCosts { area.line(to: NSPoint(x: plot.maxX - stride / 2, y: plot.maxY)); area.line(to: NSPoint(x: plot.minX + stride / 2, y: plot.maxY)); area.close(); NSColor.controlAccentColor.withAlphaComponent(0.15).setFill(); area.fill() }
            NSColor.controlAccentColor.setStroke(); path.lineWidth = 1.5; path.stroke()
        }
        if unknownCosts || clippedToLoadedRange {
            (((unknownCosts ? "橙色点：费用暂不可用" : "") + (clippedToLoadedRange ? "  ·  趋势限于已加载日历范围" : "")) as NSString).draw(at: NSPoint(x: plot.minX, y: 2), withAttributes: attributes)
        }
        let ticks: [(Int, NSTextAlignment)] = series.count == 1 ? [(0, .center)] : series.count == 2 ? [(0, .left), (1, .right)] : [(0, .left), (series.count / 2, .center), (series.count - 1, .right)]
        for (index, alignment) in ticks {
            let paragraph = NSMutableParagraphStyle(); paragraph.alignment = alignment
            let x = plot.minX + (CGFloat(index) + 0.5) * stride
            let rect = NSRect(x: min(plot.maxX - 110, max(plot.minX, x - (alignment == .center ? 55 : alignment == .right ? 110 : 0))), y: plot.maxY + 9, width: 110, height: 17)
            ((series[index].1["label"] as? String ?? series[index].0) as NSString).draw(in: rect, withAttributes: [.font: attributes[.font]!, .foregroundColor: NSColor.secondaryLabelColor, .paragraphStyle: paragraph])
        }
    }
    static func number(_ row: [String: Any], _ key: String) -> Double { guard let number = row[key] as? NSNumber, number.doubleValue.isFinite else { return 0 }; return max(0, number.doubleValue) }
    private func makePreview() {
        guard hoverPanel == nil else { return }
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 310, height: 228), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.isOpaque = false; panel.backgroundColor = .clear; panel.hasShadow = true; panel.level = .popUpMenu; panel.ignoresMouseEvents = true
        let root = CueUsageHoverCard(frame: NSRect(x: 0, y: 0, width: 310, height: 228)); panel.contentView = root
        for index in 0..<8 {
            let label = NSTextField(labelWithString: ""); label.font = .monospacedDigitSystemFont(ofSize: index == 0 ? 12 : 11, weight: index == 0 ? .semibold : .regular)
            label.textColor = index == 7 ? .secondaryLabelColor : .labelColor
            label.frame = NSRect(x: 16, y: 15 + CGFloat(index) * 25, width: index == 0 || index == 7 ? 278 : 105, height: 20); root.addSubview(label); hoverLabels.append(label)
            if (1...6).contains(index) { let value = NSTextField(labelWithString: ""); value.font = .monospacedDigitSystemFont(ofSize: 11, weight: .regular); value.alignment = .right; value.frame = NSRect(x: 125, y: 15 + CGFloat(index) * 25, width: 169, height: 20); root.addSubview(value); hoverValues.append(value) }
        }
        hoverPanel = panel
    }
    private func presentPreview(day: String, row: [String: Any], rect: NSRect) {
        makePreview(); guard let panel = hoverPanel, let window else { return }
        updatePreview(day: day, row: row); panel.appearance = window.effectiveAppearance
        let screenRect = window.convertToScreen(convert(rect, to: nil)), screen = window.screen?.visibleFrame ?? NSScreen.main?.visibleFrame ?? .zero
        var origin = NSPoint(x: screenRect.maxX + 10, y: screenRect.maxY - panel.frame.height)
        if origin.x + panel.frame.width > screen.maxX - 10 { origin.x = screenRect.minX - panel.frame.width - 10 }
        origin.x = max(screen.minX + 10, origin.x); origin.y = min(screen.maxY - panel.frame.height - 10, max(screen.minY + 10, origin.y))
        panel.setFrameOrigin(origin); panel.orderFrontRegardless()
    }
    private func updatePreview(day: String, row: [String: Any]) {
        let date = DateFormatter(); date.locale = Locale(identifier: "en_US_POSIX"); date.dateFormat = "yyyy-MM-dd"
        let title = DateFormatter(); title.locale = Locale(identifier: "zh_CN"); title.dateFormat = "yyyy年M月d日 EEEE"
        let requests = Self.number(row, "requests"), unpriced = Self.number(row, "unpriced_requests")
        var heading = date.date(from: String(day.prefix(10))).map { title.string(from: $0) } ?? day
        if day.contains("#"), let timestamp = row["timestamp"] as? NSNumber { let hour = DateFormatter(); hour.locale = Locale(identifier: "zh_CN"); hour.dateFormat = "yyyy-MM-dd EEE HH:mm"; heading = hour.string(from: Date(timeIntervalSince1970: timestamp.doubleValue)) }
        let strings = [heading,
                       "Tokens", "请求", "API 参考费用", "新增输入 / 输出", "缓存读取 / 写入", "缓存命中率",
                       row["in_selected_range"] as? Bool == false ? "当前日期在筛选外，点击可查看当天" : requests == 0 ? "当前本地账本无记录" : unpriced > 0 ? "\(Int(unpriced)) 条未定价 · 费用为已定价小计" : "点击日期查看当天请求日志"]
        for (index, text) in strings.enumerated() { hoverLabels[index].stringValue = text; hoverLabels[index].toolTip = text }
        let input = Self.number(row, "input_tokens") + Self.number(row, "cache_read_tokens") + Self.number(row, "cache_creation_tokens")
        let hit = row["cache_hit_rate"] as? NSNumber
        let values = [CueUsageDashboardController.integer(Self.number(row, "tokens")), CueUsageDashboardController.integer(requests), requests == 0 ? CueUsageMoney.display(["cost_usd": 0, "cost_cny": 0], currency: currency) : CueUsageMoney.display(row, currency: currency), "\(CueUsageDashboardController.compact(Self.number(row, "input_tokens"))) / \(CueUsageDashboardController.compact(Self.number(row, "output_tokens")))", "\(CueUsageDashboardController.compact(Self.number(row, "cache_read_tokens"))) / \(CueUsageDashboardController.compact(Self.number(row, "cache_creation_tokens")))", hit.map { String(format: "%.1f%%", $0.doubleValue * 100) } ?? (input > 0 ? String(format: "%.1f%%", Self.number(row, "cache_read_tokens") / input * 100) : "—")]
        for (index, value) in values.enumerated() { hoverValues[index].stringValue = value; hoverValues[index].toolTip = value }
    }
    func hidePreview() { hoverPanel?.orderOut(nil); hoverKey = nil }
    static func captureHoverFixture(to url: URL, dark: Bool) throws {
        let chart = CueUsageHeatmap(); chart.makePreview(); chart.currency = "CNY"
        chart.hoverPanel?.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        chart.updatePreview(day: "2026-10-29", row: ["tokens": 4514000000.0, "requests": 20000, "cost_usd": 608.55, "cost_cny": 4320.72, "input_tokens": 35000000, "output_tokens": 12000000, "cache_read_tokens": 4467000000.0, "cache_creation_tokens": 0, "cache_hit_rate": 0.967, "unpriced_requests": 2])
        guard let root = chart.hoverPanel?.contentView, let bitmap = root.bitmapImageRepForCachingDisplay(in: root.bounds) else { throw CocoaError(.fileWriteUnknown) }
        root.effectiveAppearance.performAsCurrentDrawingAppearance { root.cacheDisplay(in: root.bounds, to: bitmap) }
        guard let data = bitmap.representation(using: .png, properties: [:]) else { throw CocoaError(.fileWriteUnknown) }; try data.write(to: url)
    }
    static func validationReport() -> [String: Bool] {
        let chart = CueUsageHeatmap(); chart.weeks = 106; chart.makePreview(); chart.updatePreview(day: "2026-10-29", row: ["tokens": 123, "requests": 2, "cost_cny": 1.25, "unpriced_requests": 1])
        let text = (chart.hoverLabels + chart.hoverValues).map(\.stringValue).joined(separator: "\n")
        var report = ["heatmap_history_expands_in_53_week_blocks": chart.sections == 2 && chart.geometry(width: 1000, section: 1).grid.minY > chart.geometry(width: 1000).grid.maxY,
                "heatmap_hover_has_date_tokens_requests_and_money": text.contains("2026年10月29日") && text.contains("123") && text.contains("1.25 ¥"),
                "heatmap_hover_preserves_unpriced_coverage": text.contains("1 条未定价"),
                "heatmap_hover_never_activates_or_accepts_input": chart.hoverPanel?.styleMask.contains(.nonactivatingPanel) == true && chart.hoverPanel?.ignoresMouseEvents == true]
        chart.updatePreview(day: "2026-10-29", row: ["tokens": 123, "requests": 2, "cost_usd": NSNull(), "cost_cny": NSNull(), "unpriced_requests": 2])
        report["heatmap_unknown_cost_never_becomes_free"] = chart.hoverValues[2].stringValue == "未定价" && chart.hoverLabels[7].stringValue.contains("2 条未定价")
        chart.updatePreview(day: "2026-10-29", row: ["requests": 1, "cost_usd": 1, "in_selected_range": false])
        report["heatmap_hover_distinguishes_outside_date_selection"] = chart.hoverLabels[7].stringValue.contains("筛选外")
        return report
    }
}

private final class CueUsageHoverCard: NSView {
    override var isFlipped: Bool { true }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.controlBackgroundColor.setFill(); NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: 10, yRadius: 10).fill()
        NSColor.separatorColor.setStroke(); let line = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: 10, yRadius: 10); line.lineWidth = 1; line.stroke()
    }
}
