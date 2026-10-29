import Foundation

/// Stable numeric routes are retained while the shell renders two levels of tags.
enum CueWorkspacePage: Int, CaseIterable {
    case workspace = 0, reminderRules = 1, reminderEffects = 2, codexSettings = 3, usage = 4
    case reminderOverview = 5, codingTasks = 6, networkTests = 7, general = 8, testSettings = 9
    case networkIP = 10, networkDomain = 11, networkTrace = 12, networkPing = 13, networkHTTP = 14, networkSpeed = 15
    case accountQuota = 16, usagePreferences = 17, conversation = 18, usageSchedule = 19, priceSources = 20
    case engineeringEnvironment = 21, engineeringAPI = 22, engineeringLogs = 23, engineeringSettings = 24
    static let moduleTitles = ["任务提醒", "编程助手", "网络检测", "工程工具"]
    var module: String {
        switch self {
        case .workspace: return "工作台"
        case .reminderOverview, .reminderRules, .reminderEffects: return "任务提醒"
        case .codingTasks, .usage, .codexSettings, .accountQuota, .usagePreferences, .conversation, .usageSchedule, .priceSources: return "编程助手"
        case .networkTests, .testSettings, .networkIP, .networkDomain, .networkTrace, .networkPing, .networkHTTP, .networkSpeed: return "网络检测"
        case .engineeringEnvironment, .engineeringAPI, .engineeringLogs, .engineeringSettings: return "工程工具"
        case .general: return "通用"
        }
    }
    var title: String {
        switch self {
        case .workspace: return "工作台"
        case .reminderOverview: return "提醒概览"
        case .reminderRules: return "规则样式"
        case .reminderEffects: return "全局效果"
        case .codingTasks: return "任务对话"
        case .usage: return "用量统计"
        case .codexSettings: return "连接设置"
        case .networkTests, .networkIP: return "IP 信誉"
        case .networkDomain: return "域名检测"
        case .networkTrace: return "出口痕迹"
        case .networkPing: return "全球 Ping"
        case .networkHTTP: return "全球 HTTP"
        case .networkSpeed: return "下载测速"
        case .testSettings: return "检测设置"
        case .general: return "通用设置"
        case .accountQuota: return "账号额度"
        case .usagePreferences: return "统计设置"
        case .conversation: return "当前对话"
        case .usageSchedule: return "同步频率"
        case .priceSources: return "价格来源"
        case .engineeringEnvironment: return "环境体检"
        case .engineeringAPI: return "接口调试"
        case .engineeringLogs: return "日志诊断"
        case .engineeringSettings: return "工具设置"
        }
    }
    var networkKind: String? {
        switch self {
        case .networkTests, .networkIP: return "ip_reputation"
        case .networkDomain: return "domain"
        case .networkTrace: return "ip_trace"
        case .networkPing: return "global_ping"
        case .networkHTTP: return "global_http"
        case .networkSpeed: return "speed"
        default: return nil
        }
    }
    var engineeringKind: String? {
        switch self {
        case .engineeringEnvironment: return "environment"
        case .engineeringAPI: return "api"
        case .engineeringLogs: return "logs"
        default: return nil
        }
    }
    static func networkPage(_ kind: String) -> Self {
        [.networkIP, .networkDomain, .networkTrace, .networkPing, .networkHTTP, .networkSpeed].first { $0.networkKind == kind } ?? .networkIP
    }
    static func canonical(_ raw: Int) -> Self {
        let value = Self(rawValue: raw) ?? .workspace
        switch value {
        case .networkTests: return .networkIP
        case .conversation: return .codingTasks // Retired embedded-chat route.
        default: return value
        }
    }
    static func children(of module: String) -> [Self] {
        switch module {
        case "任务提醒": return [.reminderOverview, .reminderRules, .reminderEffects]
        case "编程助手": return [.codingTasks, .usage, .accountQuota, .usagePreferences, .usageSchedule, .priceSources, .codexSettings]
        case "网络检测": return [.networkIP, .networkDomain, .networkTrace, .networkPing, .networkHTTP, .networkSpeed, .testSettings]
        case "工程工具": return [.engineeringEnvironment, .engineeringAPI, .engineeringLogs, .engineeringSettings]
        default: return []
        }
    }
    var isEmbedded: Bool { engineeringKind != nil || networkKind != nil || [.codingTasks, .usage, .accountQuota, .usagePreferences, .usageSchedule, .priceSources, .engineeringSettings].contains(self) }
    static func validationReport() -> [String: Bool] {
        ["workspace_legacy_page_routes_remain_stable": Self.reminderRules.rawValue == 1 && Self.reminderEffects.rawValue == 2 && Self.codexSettings.rawValue == 3 && Self.usage.rawValue == 4 && canonical(7) == .networkIP,
         "workspace_four_primary_names_are_intuitive_and_symmetric": moduleTitles.count == 4 && moduleTitles.allSatisfy { $0.count == 4 },
         "workspace_engineering_tools_and_settings_share_second_level": children(of: "工程工具") == [.engineeringEnvironment, .engineeringAPI, .engineeringLogs, .engineeringSettings] && children(of: "工程工具").allSatisfy { $0.isEmbedded && $0.module == "工程工具" } && Self.engineeringSettings.rawValue == 24,
         "workspace_reminder_settings_are_separate_from_coding": Set(children(of: "任务提醒")) == Set([.reminderOverview, .reminderRules, .reminderEffects]),
         "workspace_coding_includes_all_main_functions_in_second_level": Set([Self.codingTasks, .usage, .accountQuota, .usagePreferences, .usageSchedule, .priceSources, .codexSettings]).isSubset(of: Set(children(of: "编程助手"))),
         "workspace_network_kinds_each_have_one_secondary_tag": Set(children(of: "网络检测").compactMap(\.networkKind)).count == 6,
         "workspace_network_settings_remain_in_the_network_module": Self.testSettings.module == "网络检测" && children(of: "网络检测").contains(.testSettings),
         "workspace_individual_chats_are_not_module_tabs": !children(of: "编程助手").contains(.conversation) && !Self.conversation.isEmbedded && canonical(Self.conversation.rawValue) == .codingTasks]
    }
}
