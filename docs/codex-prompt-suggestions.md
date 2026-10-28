# Codex 下一步输入建议的共享边界

核对日期：2026-10-29。核对安装：`/Applications/ChatGPT.app`，bundle ID
`com.openai.codex`，版本 `26.1021.31345`，build `21828`。

## 实际来源

输入框内按 Tab 接受的下一条建议由 Codex 的 Cumberbatch 服务提供。
当前版本在 Electron 主进程按 `hostId / threadId / latestTurnId` 缓存结果；
普通助手消息、对话完成提醒和用户草稿均不是这条预测的来源。

只读核对的本机 `Contents/Resources/app.asar` 位置：

- `webview/assets/composer-content-75731baf379a.js`：`Cj` 请求
  `requestUserMessagePrediction`，`wj` 将返回结果交给编辑器预测插件。
- `.vite/build/main-0FVbxCQU.js`：`BRe.request` 管理内存查询缓存，
  `VRe.requestUserMessagePrediction` 是服务入口；没有只取缓存的公开方法。
  缓存缺失时 `BRe.runPrediction` / `FRe` 会调用 `resumeThread`，然后走
  `thread/sample` 或临时 fork 与 turn 路径进行模型推理。
- `webview/assets/app-initial-feb30b196d11.js`：`k3s` 管理当前编辑器的预测
  状态，`R3s` 的 ProseMirror 插件负责预览和接受；这些不是磁盘持久化记录。
- `.vite/build/src-Dl21kCYQ.js`：`Oe` 是当前本机 owner/follower IPC 的版本表。
  它提供已实现的线程状态订阅、模型读取和明确的用户操作；没有预测读取、
  通用 RPC、`thread/sample` 或 Cumberbatch 请求方法。

现有跟随订阅的 live 快照也只包含对话状态、历史、设置和用量等字段，
没有 prediction / suggestion / Cumberbatch 字段。仅检查字段名称与轮次状态；
没有输出对话正文。预测缓存没有落在现有会话记录或共享状态路径中。

因此 VibeCue 当前不能通过已核对的只读连接取得 Codex 输入框里的同一条建议。
不能把会触发推理的方法当作读取缓存，也不能根据助手结尾猜测建议后标成 Codex
原建议。本次不修改 Codex 安装包，不访问其进程私有内存、凭据或登录文件，
不增加授权、不自动恢复线程或发起模型请求。

## VibeCue 返回契约

被动对话状态包含：

```json
{
  "prompt_suggestion": null,
  "prompt_suggestion_status": "not_exposed",
  "latest_turn_id": "owner-reported-turn-id-or-null"
}
```

连接不可用时 `prompt_suggestion_status` 为 `unavailable`。
`null` 明确使客户端清除旧建议。`latest_turn_id` 仅来自同一 owner 的已验证快照，
旧透明代理拿不到时保持 `null`。

未来接通真实共享来源后，可将 `prompt_suggestion` 扩展为绑定同一对话、同一轮次的
`{text, id, thread_id, turn_id, source}`。客户端只在连接就绪、对话空闲、没有待确认
发送且草稿为空时显示；Tab 仅写入草稿，随后 Enter 才走现有明确发送流程。
断线、开始新轮次、等待人工操作或建议失效时应清除，不能自动发送。
