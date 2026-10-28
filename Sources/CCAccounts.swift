import AppKit
import Foundation

/// The current CC Switch build does not export account quota. Opening its
/// own UI keeps OAuth ownership with CC Switch instead of borrowing secrets.
final class CueCCAccountsController: NSWindowController {
    private let client: CueUsageClient
    private let message = NSTextField(wrappingLabelWithString: "正在检查 CC Switch 可用的额度共享接口…")
    private let status = NSTextField(labelWithString: "")
    private let openButton = NSButton(title: "打开 CC Switch", target: nil, action: nil)
    init(home: URL) {
        client = CueUsageClient(home: home)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 590, height: 300), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "CC Switch · Codex 账号额度"; window.isReleasedWhenClosed = false; window.center()
        super.init(window: window)
        let root = NSView(frame: window.contentLayoutRect); window.contentView = root
        let title = NSTextField(labelWithString: "复用 CC Switch 的账号管理")
        title.font = .systemFont(ofSize: 19, weight: .semibold); title.frame = NSRect(x: 26, y: 242, width: 540, height: 28)
        message.font = .systemFont(ofSize: 13); message.frame = NSRect(x: 28, y: 97, width: 534, height: 129)
        status.font = .systemFont(ofSize: 11); status.textColor = .secondaryLabelColor; status.frame = NSRect(x: 28, y: 62, width: 534, height: 25)
        openButton.bezelStyle = .rounded; openButton.frame = NSRect(x: 398, y: 21, width: 166, height: 31); openButton.target = self; openButton.action = #selector(openCCSwitch)
        [title, message, status, openButton].forEach { root.addSubview($0) }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func showWindow(_ sender: Any?) {
        super.showWindow(sender); window?.makeKeyAndOrderFront(nil)
        client.run(["status"], module: "vibecue.ccswitch") { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let data):
                self.message.stringValue = data["message"] as? String ?? Self.unavailable
                self.status.stringValue = "VibeCue 不获取登录授权，不读取或复制 CC Switch 的登录 Token。"
            case .failure:
                self.message.stringValue = Self.unavailable
                self.status.stringValue = "当前未接通账号额度共享接口。"
            }
        }
    }
    private static let unavailable = "此版本 CC Switch 的账号额度仅保存在它的运行进程内，尚未提供可复用的本地额度接口。\n\n本周已用比例和重置时间，请在 CC Switch 的授权中心查看。API 参考费用不能换算为每周订阅额度。"
    @objc private func openCCSwitch() {
        guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.ccswitch.desktop") else {
            status.stringValue = "未找到 CC Switch，请从“应用程序”中打开。"; return
        }
        NSWorkspace.shared.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration()) { _, _ in }
    }
}
