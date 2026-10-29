import Foundation

/// Page identifiers 0...4 retain the previous command-line routes.
enum CueWorkspacePage: Int, CaseIterable {
    case workspace = 0, reminderRules = 1, reminderEffects = 2, codexSettings = 3, usage = 4
    case reminderOverview = 5, codingTasks = 6, networkTests = 7, general = 8, testSettings = 9
    var module: String {
        switch self {
        case .workspace: return "工作台"
        case .reminderOverview, .reminderRules, .reminderEffects: return "提醒"
        case .codingTasks, .usage, .codexSettings: return "辅助 Coding"
        case .networkTests, .testSettings: return "Test"
        case .general: return "通用"
        }
    }
    var title: String {
        switch self {
        case .workspace: return "工作台"
        case .reminderOverview: return "提醒概览"
        case .reminderRules: return "规则与样式"
        case .reminderEffects: return "全局效果与音量"
        case .codingTasks: return "Codex · 任务与对话"
        case .usage: return "用量与账号额度"
        case .codexSettings: return "Codex · 连接设置"
        case .networkTests: return "网络检测工具"
        case .testSettings: return "检测来源与范围"
        case .general: return "应用与本机数据"
        }
    }
    var symbol: String {
        switch self {
        case .workspace: return "square.grid.2x2"
        case .reminderOverview: return "bell.badge"
        case .reminderRules: return "slider.horizontal.3"
        case .reminderEffects: return "waveform"
        case .codingTasks: return "bubble.left.and.bubble.right"
        case .usage: return "chart.bar.xaxis"
        case .codexSettings: return "point.3.connected.trianglepath.dotted"
        case .networkTests: return "network"
        case .testSettings: return "gearshape.2"
        case .general: return "gearshape"
        }
    }
    static let ordered: [Self] = [.workspace, .reminderOverview, .reminderRules, .reminderEffects, .codingTasks, .usage, .codexSettings, .networkTests, .testSettings, .general]
    static func validationReport() -> [String: Bool] {
        ["workspace_legacy_page_routes_remain_stable": Self.reminderRules.rawValue == 1 && Self.reminderEffects.rawValue == 2 && Self.codexSettings.rawValue == 3 && Self.usage.rawValue == 4,
         "workspace_reminder_settings_are_separate_from_coding": Set(Self.allCases.filter { $0.module == "提醒" }) == Set([.reminderOverview, .reminderRules, .reminderEffects]),
         "workspace_coding_has_tasks_usage_and_connection_children": Set(Self.allCases.filter { $0.module == "辅助 Coding" }) == Set([.codingTasks, .usage, .codexSettings]),
         "workspace_test_settings_belong_to_test": Self.testSettings.module == "Test" && Self.networkTests.module == "Test" && Self.general.module == "通用",
         "workspace_navigation_contains_every_page_exactly_once": Self.ordered.count == Self.allCases.count && Set(Self.ordered) == Set(Self.allCases)]
    }
}
