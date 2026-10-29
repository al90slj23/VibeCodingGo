# 模型文字和速度的独立来源

核对安装版本：Codex 26.1021.31345 (21828)，2026-10-29。

`GPT-6.1 Sol Ultra` 由模型显示名和推理强度组成。`Ultra` 不是速度；
Fast 由单独的服务档位决定，因此同一模型、同一强度可以使用标准或 Fast。

VibeCue 继续订阅已核对的 owner 状态。`latestThreadSettings` 是 Codex
已应用的下一轮设置，不是仅限上一轮的参数。它优先于旧轮次的运行记录。
专用 `thread-follower-read-model-settings` 只返回模型、强度等字段，不返回速度。

原生输入框的有效速度还经过临时选择、模型支持和账号权限判断。共享状态中的
原始 `priority` 请求值可以保留，而原生图标不一定显示。模型标题本身也不能
证明 Fast 已关闭。当前共享接口没有读取 renderer 最终速度选择的方法。

VibeCue 显示规则：

- 模型／强度单独显示，例如 `当前 · GPT-6.1 Sol Ultra`。
- 速度独立显示，例如 `Fast · 已保存` 或 `标准速度 · 已保存`。
- Fast 的显示需要本机模型目录确认该档位；目录读取失败、缺失或明确不支持时
  显示待核实／未支持，并保留原始值供悬停核对。
- null、未提供和显式标准档位分开处理，沿用保守的默认继承语义。
- 断线后历史档位仅显示为请求记录，不用于判断当前输入框的选择。

本机目录读取仅使用 `slug`、`display_name`、`service_tiers` 的 ID／名称及默认
档位白名单，读取失败立即失效，不读取授权或对话正文，不发送模型请求。

静态证据位于已安装 `app.asar` 的 `app-initial-feb30b196d11.js`、
`app-primary-7d7313419f84.js` 与 `app-shared-2cae01273bae.js`：模型标题、
`selectedServiceTier` 和图标展示分别计算；临时选择不在 owner 共享快照内。
