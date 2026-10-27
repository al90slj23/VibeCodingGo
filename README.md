# VibeCue — AI Coding Companion

界面和文档使用简体中文，对话输入识别同时兼容简体、繁体及英文。升级保留个人设置，并备份上一版 App。

AI 开发提醒助手：在 macOS 用屏幕四周泛光、不同提示音与对话卡，提醒你回到需要注意的 AI coding 工作。目前先支持 Codex，事件接入层与提醒接口分开，之后可扩充其他开发工具。

| 事件 | 默认画面 | 默认提示音 |
| --- | --- | --- |
| 本轮运行完成 | 绿色泛光 | 上行和弦 |
| 提问或要求确认／选择 | 琥珀色泛光 | 三段提问音 |
| 人工批准（目前仅样式预览） | 珊瑚色泛光 | 两短一长 |

提示卡显示 Codex 的对话名称。边缘窗口不拦截鼠标、不抢键盘焦点，涵盖所有屏幕；提示卡显示在鼠标所在屏幕。完成提醒约 8 秒，其他提醒约 14 秒。需要回答的提醒会优先显示。遵循 macOS 减少动态效果设置。

## 安装与启动

需要 macOS 13 以上、Xcode Command Line Tools 中的 Swift 与 `/usr/bin/python3`。无第三方套件、无 API key。

```sh
./scripts/build.sh
/usr/bin/python3 scripts/install.py
```

安装位置：

- `/Applications/VibeCue.app`：原生菜单列应用程序。
- `~/Library/Application Support/VibeCue/`：程序、事件队列与私有状态。
- `~/Library/LaunchAgents/local.vibecue.plist`：登录后启动应用程序，由 App 管理监控子进程。
- `~/.codex/hooks.json`：加入可选的结构化提问 hook，保留其他工具的 hooks；升级时移除 VibeCue 旧的权限请求回调。

**自动批准不提醒。** `PermissionRequest` 发生在批准流程之前，不能证明需要用户操作。因此不再用它发出提醒，即使旧的已信任回调仍被调用，也不会产生提示。现有 Codex 接入无法可靠确认请求是否真的交给人工处理，人工批准类型目前只提供自定义样式及明确的测试预览，实际提醒开关暂不可用。

完成与提问的本机纪录监控不依赖 hooks。可选的提问 hook 若需要启用，请在 Codex CLI 的 `/hooks` 中审阅并信任指向 `VibeCue/vibecuectl hook` 的 `PreToolUse` 项目。安装不会改动信任纪录或略过审阅。

官方依据：[Hooks](https://learn.chatgpt.com/docs/hooks)、[Notifications](https://learn.chatgpt.com/docs/notifications)。

若全局 `codex` 命令不可用，可使用官方 App 内置 CLI 打开审阅：

```sh
./scripts/review-hooks.sh
# 进入 CLI 后输入 /hooks
```

此脚本只启动官方 CLI，不会替你信任 hooks。

## 操作

点击 Mac 顶部菜单列的 VibeCue 图标，可以打开设置面板、预览三种提示样式、关闭声音、暂停 30 分钟，或查看最近提醒。双击 App 也会打开设置面板。在「提醒规则」选择类型后，可个别设置开关、泛光颜色、提示音与显示时间，并立即预览。人工批准类型目前仅可编辑及预览样式，实际提醒开关不可用。音量与泛光强度为全局设置。

声音使用 AVAudioPlayer 播放本机合成 WAV，也可选择静音或导入自己的音效档；自选文件会验证后复制到 VibeCue 私有文件夹。播放走一般媒体音频输出，不改动系统音量或静音设置。提醒卡中的「打开对话」使用 `codex://threads/<id>`，需要系统已注册 Codex URL handler。

```sh
# 项目内的控制命令
./scripts/vibecuectl status
./scripts/vibecuectl test complete
./scripts/vibecuectl test input
./scripts/vibecuectl test permission

# 完全停止或恢复 App 及其监控子进程
./scripts/vibecuectl stop
./scripts/vibecuectl start

# 移除登录启动与 VibeCue 自己的 hook，保留程序与数据
/usr/bin/python3 scripts/uninstall.py
```

也可使用安装后的 `~/Library/Application Support/VibeCue/vibecuectl`。菜单列结束功能会连同监控进程一起停止，再次打开 App 恢复。

若 Codex 记录在外置磁盘，macOS 可能要求 VibeCue 的读取授权。请使用面板「集成与文件」中的记录目录授权，或接受系统针对该磁盘的读取提示；不需要授予完整磁盘访问。读取被拒绝时，面板会显示异常，不能只以进程仍在运行判断监控健康。

## 事件来源与准确性

监控进程只读本机 `~/.codex/state_*.sqlite` 对话中继数据与 session JSONL 的添加事件，不修改 Codex 数据库或对话，不读取登录凭证，不连网。对话名称优先取本机正式名称；启动时从既有文件尾端开始，不重播历史提醒。跨进程以工具自己的 SQLite 去重，队列有数量与过期上限。

- `task_complete` 表示**本轮结束**，并非保证整个项目已完成。中断及子代理结束不提醒。
- 结构化 `request_user_input`／`request_user_input_async` 调用产生提问提醒。Code Mode 的嵌套调用依运行记录辨识，无法证明未运行分支，提醒文本会注明记录判断。
- 如果最后一段回复明确要求用户确认或选择，也会标为提问；这是保守文本规则，提示卡会标示「依回复文本判断」，可能有漏判或误判。
- `PermissionRequest` 只表示发生权限请求，没有可靠的人工等待或自动审查结果字段，因此一律不提醒。也不会以等待时间、命令运行速度或单一审批旗标猜测。未来实际人工批准事件必须提供已确认的 `requires_user_action: true`，且不能来自原始 `PermissionRequest hook`。VibeCue 不会替你批准、拒绝或回复。
- 本机纪录格式是内部接口，Codex 更新后可能需要调整。监控状态显示 `local-transcripts`，不能视为官方稳定事件 API。
- 没有落在本机 session 纪录中的云端或其他电脑对话，不在此监控范围。云端协调的对话也可能不支持本机 command hooks。

原有 `config.toml` 及 computer-use 的 `notify` 设置维持原样。安装器会备份被修改的既有 `hooks.json`；卸载只移除 VibeCue 自己的项目。

## 开发与验证

```sh
/usr/bin/python3 -m unittest discover -s tests -v
# 在隔离目录验证安装，不启动进程
/usr/bin/python3 scripts/install.py --no-start --user-home /private/tmp/vibecue-install-qa
# 单独显示测试，截取应用程序自己的卡片与泛光画面
VIBECUE_HOME=/private/tmp/vibecue-qa \
  'build/VibeCue.app/Contents/MacOS/VibeCue' \
  --demo input --capture /private/tmp/vibecue-banner.png \
  --capture-glow /private/tmp/vibecue-glow.png
```

测试涵盖启动不重播、部分写入、文件替换／截断、子代理与中断过滤、事件去重、暂时错误重试、提问规则、停用标记、队列上限、进程单例、hook 不产生批准决策，以及安装保留原设置。实际播放状态、播放进度与音频电平可在 `app-status.json` 查看；该状态不能代替人耳确认。

## 接入其他 AI 开发工具

`vibecue/providers/codex.py` 是目前实际运作的 Codex adapter。它输出统一的本机事件给 `vibecue/events.py`，原生 App 只负责显示、声音与设置。添加工具时应提供自己的事件与对话名称来源，不沿用 Codex 的内部纪录格式。

```json
{"provider":"codex","id":"stable-event-id","kind":"input","thread_id":"thread-id","title":"对话名称","message":"需要回答","created_at":"2026-10-27T12:00:00Z"}
```

此版本尚未接入 Claude Desktop 或其他工具。VibeCue 是独立项目；安装后运行不依赖原代码所在目录。
