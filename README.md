# VibeCue — AI Coding Companion

原生 macOS AI 开发提醒助手。提醒的重点是“哪个对话、什么状态、是否需要我”，目前支持 Codex。

## 外观与分类

0.10 新增持久中断任务、API 参考费用、美元／人民币与自动汇率、16 段森林实录；保留 0.9 的 Codex 批量设置、本轮详情和悬停预览。

- **最近提醒**：左键菜单栏图标打开可滚动面板，每次最多显示 12 条（随屏幕高度调整）。未读提醒自动置顶，手动置顶在打开后仍保留；置顶项不占普通历史的 50 条额度。普通历史按最近打开时间保留，老任务刚打开也可再次标为未读。蓝色表示未读，灰色表示已发起对话跳转；状态按每条提醒独立记录并在重启后保留。关闭提示卡、提醒超时或批准请求解除都不会冒充“已打开”。右键提醒可选择“标为未读”“置顶／取消置顶”“删除”；删除只移除 VibeCue 提醒记录，不删除原对话。右键菜单栏图标仍提供快捷菜单。
- **正在运行**：列表顺序为置顶提醒 → 正在运行 → 中断待续 → 普通历史。绿色运行行点击打开对应对话，不生成提醒、不占历史额度；同一对话已在置顶时显示运行徽标，不重复列出。依本机明确的任务生命周期、工具调用和回复事件判断，完成、中止和阻塞式提问后移出；异步提问后继续工作的任务仍显示。监听启动只恢复有限尾部状态，不补发历史提醒。无法读取、监控断连或超过 24 小时没有活动证据的状态不冒充正在运行。
- **中断保留**：任务导航元数据持久保存在私有 `task-recovery.json`，不因 Codex 或 VibeCue 重启消失。已观察到本轮中断，或此前进程实例消失且未见完成时，显示橙色“中断待续”；无法核实运行连续性时显示“运行待确认”。只打开对话不会清除标记，实际续跑／完成才更新；右键可明确移除。置顶任务附中断徽标，其他中断任务独立分组，均不占普通历史 50 条额度。不自动发送继续消息或恢复任务。只保留标题、ID、数字和已知记录路径，不保存对话正文或凭据。
- **批量模式**：列表可勾选置顶、运行中或单个项目，以 Codex 对话 ID 去重；勾选时点行只切换选择，不跳转。批量弹窗独立选择模型、强度及闪电，均可保持原设置。模型与档位动态读取当前 Codex 能力；闪电描述随模型变化，不统一承诺 1.5 倍。新设置用于后续轮次，不发送消息或打断当前任务；未载入当前 Codex 的对话逐项提示先打开。结果区分已确认、待确认和失败，不把提交请求当成成功。
- **本轮详情与悬停**：显示本轮耗时、Tokens，浮窗补充输入／输出／缓存、模型、强度与速度。只有有限尾部证据时标明部分或下限，不把全会话累计当本轮；缺失保持未知。悬停约 0.45 秒显示最近用户／助手可见消息，支持文本选择、手动滚动、慢滚与跟随最新，移入浮窗暂停自动滚动。只读最近 2 MiB，正文最多每对话 8 条／12K 字符，另限制全局正文与状态文件大小；不展示系统、推理或工具参数，常见凭据做脱敏。预览保存在本机私有 `watcher-state.json` 短期快照，不写入用量账本。
- **待确认提醒接通**：浏览器 MCP 表单／URL／用户验证、真实提问与选项请求均纳入已观察请求的等待与解除流程；自动响应或已解除请求不回放。批量控制与批准监听共用显式接通的本机透明通道。当前 Codex 已运行但未接通时，列表和弹窗会明确提示；点“接通 Codex”后等待你主动退出再打开，不强制结束任务。仅增加模型／强度／速度控制，不提供批准决策、消息发送、权限变更或对话恢复接口。
- **历史用量**：菜单栏或主面板的“用量统计”打开独立原生窗口，提供时间、应用、模型、来源筛选，Token 分项、缓存命中率、53 周热力图，以及请求日志／模型／来源汇总。请求日志每页 50 条，可滚动查看。当前实现 Codex、Claude Code 与 OpenCode 本地历史，不声称已接入其他工具或 Claude Desktop 的完整历史。
- **用量口径**：从日志已有的 usage 数字读取，Codex 优先单次用量、缺失时对累计值做差；缓存与 reasoning 按协议去重。有父会话证据的分叉／子代理，沿各代分叉时间限定的祖先链，按完整用量数字签名与物理顺序剥除连续继承前缀；零值状态快照不会结束继承前缀。重写时间戳或同一毫秒的历史重放不算新请求，分叉后真实新用量保留。旧去重规则升级前先私有备份用量库，再只重建 Codex 缓存，保留其他来源与提醒记录。历史没有供应商信息时只标为会话来源，缺少模型保留 unknown；不推测供应商、账单、速度或 HTTP 状态。数据说明会展示不可读、暂缓或跳过的记录。
- **OpenCode**：只读 SQLite 的 V1 `message`、V2 `session_message` 用量字段和旧版 `storage/message` JSON；不读取正文、消息 part 或认证数据。支持 SQLite WAL、旧历史补入、同一消息流式更新，以及迁移、复制、重复目录防重。输入按 fresh input，独立 reasoning 归入输出一次；V2 已结束或失败的压缩请求也计入。来源筛选显示日志实际记录的 provider ID。旧版负 visible output 仅在与 reasoning 合并非负且精确符合明确 `tokens.total` 时兼容，不凭负值擅自抬高总量。
- **API 参考费用**：用量窗口提供“费用与汇率”，按四种互斥 Token 分类和精确模型 ID 读取 CC Switch `model_pricing` 单价，也支持按模型自定义美元／百万 Tokens 单价。新增输入、输出、缓存读取、缓存写入分别计算，不把缓存再计为普通输入。缺少单价显示未定价；混合汇总显示已定价小计及未定价条数。历史按当前单价重估，未记录的阶梯、长上下文或服务档位差价不推断，不等同订阅扣款。可选人民币、美元或双币；人民币支持手动汇率或 [Frankfurter](https://frankfurter.dev/) 公开 USD/CNY 日参考汇率，缓存 6 小时，失败保留标记过期的旧值，缺少汇率时不造默认值。只向固定公开端点请求币种，不发送账号或用量。
- **CC Switch 额度**：已核对的 4.0.21 版本仅在进程内缓存 OAuth 额度，尚无可复用的本地共享接口。VibeCue 提供打开 CC Switch 的入口，明确标示额度未接通；不读取、复制它的登录 Token，不发起新授权，也不把 API 参考费用冒充每周订阅额度。[同版本缓存实现](https://github.com/farion1231/cc-switch/blob/v4.0.21/src-tauri/src/services/usage_cache.rs)。
- **后台同步**：启动后与后台每 10 分钟扫描，打开用量窗口后可手动同步并查看进度，扫描和查询均在后台执行。用量缓存只保存数字、模型和会话标识，不保存正文或凭据，不修改源日志。明细保留在本机私有 `usage.sqlite3`，不进行 30 天删除。

默认读取 `CODEX_HOME`（未设置时为 `~/.codex`）下的 `sessions`、`archived_sessions`、`CLAUDE_CONFIG_DIR`（未设置时为 `~/.claude`）下的 `projects`，以及 `XDG_DATA_HOME`（未设置时为 `~/.local/share`）下的 `opencode`；识别软链接。`OPENCODE_DB` 可直接指定 OpenCode 数据库。Codex 的 `.jsonl.zst` 需要本机已有的 `zstd`，没有解码器时明确提示覆盖缺失。需要额外目录时可在 VibeCue 数据目录建立 `usage-sources.json`；列出的键覆盖对应默认来源，空数组关闭该工具的采集：

```json
{"codex_roots": ["/path/to/codex"], "claude_roots": ["/path/to/claude/projects"], "opencode_roots": ["/path/to/opencode"]}
```

0.5 在统一配方上加入光影材质：**路径 → 光体／暗部／高光／散射／反射 → 运动 → 出现／消退包络**。推荐 3 种环屏光影跑马灯：柔光流体、珍珠流光、棱镜流光。原 6 种极简方案和 50 个经典 ID 继续可选，并由同一渲染器绘制。材质截面沿屏幕法线着色，亮头和长尾连续流动；这是二维原生绘制的体积与光影表现，不声称是实时三维光线追踪。

- **提示卡**：3 套精修推荐使用真实 AppKit 标准材质和语义文字色，跟随系统明暗；旧 50 套按玻璃、纸张、经典等分类保留。装饰不再伪装为任务进度或音频仪表。
- **声音**：合成提示音 50 段；自然实录 20 段（含青蛙、鸟类、虫鸣、溪流与林冠风声）；城市与生活实录 4 段；系统音效从本机动态读取（数量随系统变化）。实录附 [音源与许可记录](assets/Sounds/CREDITS.md)，Apple 音频不打包分发。
- **分类与预览**：可多选标签（并集）、搜索名称，点选保存并立即预览。常用页提供精简选择，完整目录在分类库中。声音只试听、视觉只预览画面；整套预览才组合三者。
- **随机**：在当前所选标签范围抽取；三个分类独立。单次提醒和多显示器共用同一份抽取结果，窗口重建不会重新抽取。搜索词只筛选可见列表，不缩小随机池。
- **自定义**：主界面提供材质、柔和／清晰／醒目的存在感与 1–8 色配色；方向、速度、宽度、内缩、透明度和尾迹放在“更多调整”。尺寸单位为 AppKit 逻辑点（pt）。棱镜的色散取样沿用所选配色，单色不会被强行改成彩虹。

提示卡的标题、正文、图标和空白区域可点击打开对应对话；按钮保留各自操作。预览没有真实对话时不跳转。视觉预览持续 6 秒；结束、暂停或被真实提醒阻止时显示原因。真实提醒优先，连续预览只替换上一预览，不写入最近任务列表。全局暂停、静音、减少动态效果仍有效。

设置与图库跟随系统外观。升级到本代视觉时先将旧设置完整备份到私有 `backups/styles-before-lighting-*.json`，然后应用光影跑马灯及更清楚的默认光带宽度和亮度；保留声音选择、音量、开关、配色、方向、已选卡片和对话入口偏好。旧样式仍可从分类库选择。

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

人工批准接入采用透明 stdio 监听：官方 Codex app-server 与桌面客户端的原始字节双向直通；VibeCue 观察实际发给客户端的命令／文件／权限请求，以及 `mcpServer/elicitation/request`、真实提问和选项选择请求。MCP 表单、URL 与用户验证按需要回答或确认处理。匹配的客户端响应、`serverRequest/resolved`、任务结束或断线立即解除对应提醒和提示卡；自动响应或已经解除的请求不回放。

首次接通需要让 Codex 从监听入口启动一次：

1. 点击列表或批量弹窗的「接通 Codex」，也可在「集成与文件」选择「接通人工批准监听」。
2. 确认准备连接后，先完成正在进行的 Codex 工作，再正常退出 Codex。VibeCue 不会替你强制退出。
3. VibeCue 会重新打开 Codex；收到实际请求流后，面板才显示「已连接」。
4. 在「提醒规则」选择人工批准，设置其开关、特效、提示卡、音效与显示时间。

批量控制复用同一 owner 连接：私有 Unix socket 仅允许模型目录、只读线程元数据与模型／强度／服务档位更新。读取线程强制不带正文；不允许启动、恢复、发送消息或变更权限。更新只有收到真实设置通知匹配后才确认，缺少回读明确标为待确认。

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
build/VibeCue.app/Contents/MacOS/VibeCue --recent-self-test
build/VibeCue.app/Contents/MacOS/VibeCue --window-preview-self-test  # 短暂显示无声测试窗口
/usr/bin/python3 scripts/check-lighting.py --output /private/tmp/vibecue-lighting-review
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
