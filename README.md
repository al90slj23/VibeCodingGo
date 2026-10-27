# VibeCue — AI Coding Companion

原生 macOS AI 开发提醒助手。提醒的重点是“哪个对话、什么状态、是否需要我”，目前支持 Codex。

## 外观与分类

0.4 将光效构建改为统一的声明式配方：**路径 → 光层 → 运动 → 出现／消退包络**。旧 50 个光效 ID 继续可用，并由同一个渲染器绘制；不再保留按每种样式写绘图分支的旧引擎。推荐页先呈现 6 种克制光效，跑马灯、角落、流动、静态、经典等作为可交叉的标签。

- **提示卡**：3 套精修推荐使用真实 AppKit 标准材质和语义文字色，跟随系统明暗；旧 50 套按玻璃、纸张、经典等分类保留。装饰不再伪装为任务进度或音频仪表。
- **声音**：合成提示音 50 段；自然实录 4 段；城市与生活实录 4 段；系统音效从本机动态读取（数量随系统变化）。实录附 [音源与许可记录](assets/Sounds/CREDITS.md)，Apple 音频不打包分发。
- **分类与预览**：可多选标签（并集）、搜索名称，点选保存并立即预览。常用页提供精简选择，完整目录在分类库中。声音只试听、视觉只预览画面；整套预览才组合三者。
- **随机**：在当前所选标签范围抽取；三个分类独立。单次提醒和多显示器共用同一份抽取结果，窗口重建不会重新抽取。搜索词只筛选可见列表，不缩小随机池。
- **自定义**：保留 1–8 色、方向、速度、宽度、内缩、透明度、尾迹、卡片位置和音量；尺寸单位为 AppKit 逻辑点（pt）。

提示卡的标题、正文、图标和空白区域可点击打开对应对话；按钮保留各自操作。预览没有真实对话时不跳转。真实提醒优先，连续预览只替换上一预览，不写入最近任务列表。全局暂停、静音、减少动态效果仍有效。

设置与图库跟随系统外观。升级到本代视觉时先将旧设置完整备份到私有 `backups/styles-before-native-*.json`，然后应用推荐光效/卡片；保留声音选择、音量、开关、配色和对话入口偏好。旧样式仍可从分类库选择。

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
build/VibeCue.app/Contents/MacOS/VibeCue --sound-source-self-test
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

## 设计与构建说明

[设计审查](DESIGN-IS-2026-10-27/03-verdict.md)记录旧版基线、问题与重构依据；不是对新版已完成主观验收的宣称。新版由 Effects.swift 的统一配方引擎、CardStyles.swift 的材质与布局、Sounds.swift 的音源提供器及 PresetBrowser.swift 的标签搜索界面组成。实时材质观感、动效舒适度与音源听感仍需要实际桌面体验；像素/音频唯一性测试只证明区分度，不证明审美质量。
