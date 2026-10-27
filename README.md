# VibeCue — AI Coding Companion

原生 macOS AI 开发提醒助手。目前支持 Codex，提供 50 种屏幕边缘特效、50 套提示卡、50 种原创提示音，以及按事件类型保存的颜色、声音和显示设置。界面和文档使用简体中文；对话输入识别兼容简体、繁体和英文。

## 提醒样式实验室

在设置的「提醒规则」中选择完成、等待输入或人工批准，再切换「屏幕特效」「提示框」「声音与规则」。

- **50 种边缘特效**：呼吸、霓虹、琉璃、彩虹跑马灯、顺逆追光、彗星、虚线、扫描、四角、波纹、粒子、极光与光环等。效果库提供全部预览。
- **多色调色板**：每类提醒可以单独配置最多 8 种颜色，或应用预设配色；保留旧版单色设置。
- **运动参数**：顺时针、逆时针、往返、速度、边框宽度、透明度、内缩距离和尾迹长度。
- **50 套提示卡**：玻璃、紧凑、极简、控制台、霓虹、纸笺、蓝图、像素、仪表等布局，支持位置、宽度、圆角、透明度、字号、正文与打开对话入口设置。
- **50 种声音**：玻璃、木琴、电子、铃铛、水滴、拨弦、空气、节奏、科幻等原创合成音效，另有静音和自选音频文件。自选文件验证后复制到私有目录；全局音量、暂停和静音始终有效。
- **独立规则**：每种事件有自己的开关、持续时间和样式，支持实际效果预览与恢复默认。

三个分类均提供包含全部 50 项的预览库，并各有一个额外的「随机」选项。三个随机开关独立；每次提醒各自抽取一种，单次提醒、多显示器显示和屏幕变化时保持同一组结果。配色、方向、速度、尺寸等自定义参数会继续保留；点击随机或再次预览会重新抽取。点击预设或下拉选项会立即保存并预览，图库保持打开以便连续比较。屏幕特效和提示框只预览画面，提示音只试听；「预览整套提醒」同时展示三者。连续选择替换上一个预览，不排队、不写入最近任务提醒。真实任务提醒优先显示，正在显示或排队时保留图库样例，避免被预览覆盖。

提示卡的标题、正文、图标和空白区域均可点击，打开该卡片对应的对话；关闭、打开等按钮保留各自操作。没有真实对话的样式预览不会跳转。提醒窗口不抢键盘焦点，屏幕边缘不会拦截鼠标。支持多显示器，并遵循系统减少动态效果设置。动画仅在显示提醒时运行，普通效果最高 30 fps，较重的多屏效果会降低帧率。

## 安装

需要 macOS 13 以上、Xcode Command Line Tools 的 Swift 与 `/usr/bin/python3`。没有第三方运行依赖，不需要给 VibeCue 配置 API key。

```sh
./scripts/build.sh
/usr/bin/python3 scripts/install.py
```

- App：`/Applications/VibeCue.app`
- 私有设置和事件：`~/Library/Application Support/VibeCue/`
- 登录启动：`~/Library/LaunchAgents/local.vibecue.plist`

升级会保留个人设置，停止旧进程，以新的签名包替换 App，并备份上一版。双击 App 或点击菜单栏入口即可打开设置。

## 真正的人工批准提醒

原始 `PermissionRequest` hook 发生在审批流程之前，**不能证明正在等人操作**。VibeCue 不用它触发提醒，也不根据等待时间、命令速度、自动审核状态或单一 `waitingOnApproval` 标志猜测。

人工批准接入采用透明 stdio 监听：官方 Codex app-server 与桌面客户端的原始字节双向直通；VibeCue 只观察实际发给客户端的 `item/commandExecution/requestApproval`、`item/fileChange/requestApproval`、`item/permissions/requestApproval` 及兼容的旧请求。匹配的客户端响应、`serverRequest/resolved`、任务结束或断线会立即解除对应提醒。自动审查和额外安全检查事件不触发人工提醒。

首次接通需要让 Codex 从监听入口启动一次：

1. 在 VibeCue 的「集成与文件」点击「接通人工批准监听」。
2. 确认准备连接后，先完成正在进行的 Codex 工作，再正常退出 Codex。VibeCue 不会替你强制退出。
3. VibeCue 会重新打开 Codex；收到实际请求流后，面板才显示「已连接」。
4. 在「提醒规则」选择人工批准，设置其开关、特效、提示卡、音效与显示时间。

也可以在正常退出 Codex 后执行：

```sh
./scripts/vibecuectl launch-codex
# 或先等待用户退出，再自动打开
./scripts/vibecuectl launch-codex --wait-for-exit
```

当前桌面版本通过启动环境中的 `CODEX_CLI_PATH` 支持这个覆盖入口，未公开承诺为稳定 API，未来 Codex 更新可能需要适配。覆盖只作用于这次启动，不改动 Codex 安装包、登录凭证、沙箱或审批模式。没有设置覆盖的普通启动会使用官方默认入口；需要监听时通过 VibeCue 启动。未连接时，人工批准类型仍可自定义和预览，面板不会假称已经接通。

监听器不发初始化请求、不启动任务、不批准或拒绝操作。原始命令、提示词和 RPC 内容不写入 VibeCue 日志；只保存必要的请求标识、连接健康状态和提醒元数据。队列丢失、无法解析或来源断开时停止不确定的提醒，而不是猜测用户需要操作。

协议依据：[Codex App Server](https://learn.chatgpt.com/docs/app-server)、[Auto-review](https://learn.chatgpt.com/docs/sandboxing/auto-review)。真正桌面接入与人工批准验收需要在通过此入口重新打开 Codex 后进行；模拟协议测试不等同于这项验收。

## 完成与提问来源

本地适配器只读 `~/.codex/state_*.sqlite` 的对话元数据与 session JSONL 的新增事件，不修改 Codex 数据库或对话，不读取登录凭证。启动时跳过历史，过滤子代理和用户中断，支持部分写入、文件替换与截断。

- `task_complete` 表示本轮结束，不保证整个项目已经交付。
- 结构化 `request_user_input`／`request_user_input_async` 触发等待输入提醒。
- Code Mode 的嵌套调用依运行记录识别，无法证明未执行分支，提示中会注明。
- 最后回复中明确要求用户确认或选择，也会以保守文本规则识别；提示会标注「依回复文本判断」，可能存在漏判或误判。
- 本地记录格式属于内部接口；没有落到这台电脑上的云端或其他电脑对话不在本地记录监控范围。

原有 `config.toml`、computer-use 的 `notify` 与其他工具的 hooks 保持原样。安装器仅加入可选的结构化提问 hook，并移除 VibeCue 自己的旧权限请求回调。完成和提问监控不依赖 hooks。可选提问 hook 若要启用，在 `/hooks` 中审阅 `PreToolUse` 项目；安装器不会替用户信任。

```sh
./scripts/review-hooks.sh
# 进入官方 CLI 后输入 /hooks
```

若记录放在外置磁盘，可在「集成与文件」为实际记录目录授予读取访问，不需要完整磁盘访问。读取被拒绝时面板会显示异常。

## 控制与验证

```sh
./scripts/vibecuectl status
./scripts/vibecuectl test complete
./scripts/vibecuectl test input
./scripts/vibecuectl test permission  # 明确的人工提醒测试，不是假装有真实待批请求
./scripts/vibecuectl stop
./scripts/vibecuectl start
/usr/bin/python3 scripts/uninstall.py
```

菜单栏提供暂停、静音、最近提醒、测试和设置入口。卸载会移除 VibeCue 的登录入口与 hook，保留程序及个人数据。

```sh
/usr/bin/python3 -m unittest discover -s tests -v
build/VibeCue.app/Contents/MacOS/VibeCue --appearance-self-test
build/VibeCue.app/Contents/MacOS/VibeCue --preview-self-test
build/VibeCue.app/Contents/MacOS/VibeCue --resolution-self-test
build/VibeCue.app/Contents/MacOS/VibeCue --event-validation-self-test
build/VibeCue.app/Contents/MacOS/VibeCue --customization-self-test
build/VibeCue.app/Contents/MacOS/VibeCue --effects-contact-sheet /private/tmp/vibecue-effects.png
build/VibeCue.app/Contents/MacOS/VibeCue --cards-contact-sheet /private/tmp/vibecue-cards.png
```

验证覆盖原始字节转发、自动审查静默、人工请求与解除配对、快速响应竞态、隐私文件、历史不重播、异常恢复，以及样式迁移和渲染。测试使用模拟后端，不会创建真实任务。声音播放状态不能代替人耳确认。

## 后续工具接入

`vibecue/providers/codex.py` 读取本地记录；`approval_bridge.py` 观察实际客户端审批流；`vibecue/events.py` 输出统一事件和解除控制；原生 Swift App 处理样式、声音与设置。新增工具应提供自己的可靠来源，不复用 Codex 内部格式。目前没有声称已经支持 Claude Desktop。
