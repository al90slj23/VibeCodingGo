# Web Coding 扩展板块调研（2026-10-30）

本轮只完成调研和集成建议，没有安装候选工具，也没有把下列方向宣称为已实现功能。现有一级板块为「任务提醒 / 编程助手 / 网络检测」；新增功能继续放在一个主面板内，按两级标签组织。以下名字统一四字，不建议把所有候选同时加为一级标签。

## 先做哪些

建议先补「开发前准备 → API 验证 → 问题定位」：**环境体检、接口调试、日志诊断**。它们能直接复用当前项目路径、Codex 对话入口与任务调度，而且能用按需 CLI 实现，未使用时没有常驻采集工作。**开发工具、依赖审计**是下一批；网页体检和流水预演涉及浏览器/容器，放到资源隔离完善后。

建议增加「工程工具」一级标签，二级按需加入环境体检、服务管理、接口调试、日志诊断、依赖审计、开发工具；「网页体检」「交付助手」成熟后再决定是否成为一级板块。以上是产品组织建议，需确定范围后实施。

| 候选板块 | 面向使用者的结果 | 可复用开源项目与许可证 | 推荐接法 / 相对成本 |
|---|---|---|---|
| 环境体检 | 这个项目需要什么 Node/Python/Go，实际用了什么，哪里缺失或冲突 | [mise](https://github.com/jdx/mise)，[MIT](https://github.com/jdx/mise/blob/main/LICENSE) | 优先调用已有 `mise ls --json`，先只读诊断，再做明确的安装/修复操作；低—中 |
| 接口调试 | 请求集合、环境变量、认证链、JSON 断言、接口回归报告 | [Hurl](https://github.com/Orange-OpenSource/hurl)，[Apache-2.0](https://github.com/Orange-OpenSource/hurl/blob/master/LICENSE)；界面与 Git 化集合参考 [Bruno](https://github.com/usebruno/bruno)，[MIT](https://github.com/usebruno/bruno/blob/main/license.md) | 首版用原生 Hurl CLI + JSON 报告，不嵌入另一套 Electron 应用；请求文件留在项目里，可交给 CI；低—中 |
| 日志诊断 | 合并前端/API/worker 日志，按时间串联，筛选错误，定位项目和任务 | [lnav](https://github.com/tstack/lnav)，[BSD-2-Clause](https://github.com/tstack/lnav/blob/master/LICENSE) | 按需 headless 查询、复用格式识别和 SQL 能力，原生展示结果；中 |
| 服务管理 | 每个项目的 dev server、API、worker、端口、健康状态和日志入口 | [mise daemons](https://mise.jdx.dev/daemons.html)，MIT；复杂编排可用 [process-compose](https://github.com/F1bonacc1/process-compose)，[Apache-2.0](https://github.com/F1bonacc1/process-compose/blob/main/LICENSE) | 与环境体检共用适配器；复杂依赖才使用 process-compose 的 REST/OpenAPI。已有服务和本工具启动的服务明确区分；中 |
| 开发工具 | JSON/YAML、编码、时间戳、正则、文本差异、JWT 内容查看 | [DevToys](https://github.com/DevToys-app/DevToys)，MIT；[CyberChef](https://github.com/gchq/CyberChef)，Apache-2.0 | 借鉴算法与工具组合，按需加载原生小工具或精选离线 Web 组件，不搬完整运行时；低 |
| 依赖审计 | 锁文件依赖漏洞、受影响版本、修复版本、文件位置与扫描报告 | [OSV-Scanner](https://github.com/google/osv-scanner)，[Apache-2.0](https://github.com/google/osv-scanner/blob/main/LICENSE) | CLI 的 JSON/SARIF 适配；按 lockfile 内容、引擎版本和漏洞库版本失效。首版只扫描；低—中 |
| 网页体检 | 性能、SEO、最佳实践、无障碍、前后报告差异 | [Lighthouse](https://github.com/GoogleChrome/lighthouse)，Apache-2.0；[axe-core](https://github.com/dequelabs/axe-core)，MPL-2.0 | Node/Chrome 按需单任务运行，浏览器内复用 axe 引擎；报告映射到 DOM 节点，保留待人工确认项；中—高 |
| 回归测试 | 登录/表单/关键业务路径、跨浏览器验证、截图差异与失败 trace | [Playwright](https://github.com/microsoft/playwright)，Apache-2.0 | 运行项目已有测试并导入结构化报告，复用浏览器和 trace 引擎；浏览器按需启动；中—高 |
| 模型评测 | 对同一套测试用例比较模型/提示词的结果、延迟和费用 | [promptfoo](https://github.com/promptfoo/promptfoo)，MIT 核心 | 复用声明式配置与 CLI 结果，不搬完整 Web 控制台。只对用户明确选择的 API/本地模型运行；中 |
| 流水预演 | 在推送前选择 GitHub Actions job、本机执行、查看日志及产物 | [act](https://github.com/nektos/act)，[MIT](https://github.com/nektos/act/blob/main/LICENSE) | 按需 CLI，使用现有 Docker 或明确选择的原生 job；不常驻新容器；中—高 |
| 发布助手 | 版本差异、变更分类、发布说明草稿与发布前检查 | [git-cliff](https://github.com/orhun/git-cliff)，MIT 或 Apache-2.0 | CLI `--context` JSON + 模板，先提供可审阅草稿，再接现有发布流程；低 |

## 可做出自己特色的交互

1. **报告关联对话**：接口失败、漏洞、日志异常或网页问题能定位到项目；用户选择后把结构化报告带入对应 Codex 对话，任务完成后可再次运行相同检查比较结果。
2. **项目工作台**：同一项目里集中显示环境、服务、最近检查、待续对话和费用。无需来回寻找不同工具的窗口。
3. **可重复的检查集合**：把“本地 API 健康检查 + 关键接口 + 网页体检”保存为项目配方，显示每一步状态、输入版本和报告，而不只显示一个不透明的总分。
4. **变化后才检查**：环境文件、lockfile、接口集合发生内容变化时标记旧报告需要刷新；扫描本身按需进行，避免每一个功能都新增后台轮询。
5. **开发到交付的证据链**：同一条记录关联 Git revision、工具版本、检查范围、失败原因、报告和后续修复对话，区分“本地通过”与“已经线上验证”。

## 复用的具体层次

首选顺序为：已有 CLI 的结构化输出 → 可独立调用的算法库 → 精选离线组件 → 重写适合原生 UI 的交互。优先迁移引擎、协议和文件格式，不复制整套桌面应用。上表成本是结合当前 Swift/Python 架构的工程判断，并非候选项目承诺。

每个工具适配器应声明：功能 ID、支持版本、可执行文件来源、项目输入、是否联网、资源类型、超时、取消方式、输出上限、结果版本和失效条件。重扫描、浏览器和容器使用独立有限队列；消息发送和状态探测的保留槽不能被占用。模块没有打开或没有运行任务时，不创建自己的定时器、守护进程和文件全量扫描。

结果分三层：原始报告（限量保存、可追溯）→ 标准化索引（项目/文件/严重度/时间）→ 当前窗口展示投影。UI 只接收当前页与变化记录，原始长日志不经过主线程整包解析。所有缓存都有输入身份、版本与失效事件，不能仅靠“几分钟内不刷新”代替正确性。

## 选型边界

- [Hurl 官方手册](https://hurl.dev/docs/manual.html)提供代理、断言、报告和 CLI 参数；可以复用 macOS 二进制，并保留文本请求文件。请求可能产生真实业务副作用，运行前应清楚显示请求目标及所选环境。
- [mise 的工具列表](https://mise.jdx.dev/cli/ls.html)与[项目服务](https://mise.jdx.dev/daemons.html)可共享一个适配器。首版不自动信任并执行陌生项目脚本；诊断中不展示 `.env` 明文。
- [lnav 的 headless/SQL 能力](https://lnav.org/features)适合按需日志分析。连续追踪必须处理文件轮换与读取游标，日志体量不能变成 UI 更新频率。
- [OSV-Scanner 官方说明](https://github.com/google/osv-scanner#data-sources-and-privacy)区分在线包元数据查询和离线漏洞库；离线模式需要先取得数据库，结果应显示数据库版本与时间，不能把过期库的“没发现”说成没有漏洞。
- [Lighthouse](https://github.com/GoogleChrome/lighthouse)需要合适的 Node 与 Chrome；其报告反映特定设备/网络条件。axe 自动结果不等于完整人工无障碍验收。浏览器工作必须和轻量统计任务隔离。
- [Playwright](https://github.com/microsoft/playwright)适合业务流程回归，与 Lighthouse 的性能/质量体检互补；保存的登录态和 trace 可能包含敏感信息，应局部保留并受控分享。
- [promptfoo](https://github.com/promptfoo/promptfoo)能复用声明式评测和 CLI/CI 流程，但模型测试会实际发起请求并产生费用。它不能把 Codex 订阅额度等同为任意第三方 API 调用权；首版不自动读取或转用账号凭据。核心 MIT 与其商业功能应分开核对。
- [act runner 文档](https://nektosact.com/usage/runners.html)说明容器与 GitHub runner 的差别；Mac 上能运行不代表完整复现 GitHub 托管的 macOS/Linux runner。
- **密钥巡检**值得后续做可替换引擎：[Gitleaks](https://github.com/gitleaks/gitleaks)官方当前声明以安全补丁为主，[Betterleaks](https://github.com/betterleaks/betterleaks)是后续参考，v2 仍有 RC。集成前固定稳定版本和报告协议，输出必须脱敏。暂不把它列为首批主依赖。
- [DevToysMac](https://github.com/DevToys-app/DevToysMac)已归档，可参考 Swift 实现，不适合作为持续更新的核心依赖。
- Globalping 已属于现有网络检测能力，扩展其多地区 DNS/TLS/HTTP 结果即可，不重复建立“全球测速”新板块。[官方 CLI](https://github.com/jsdelivr/globalping-cli)是 MPL-2.0；不能把所有 jsDelivr 项目都按同一种许可证处理。

## 许可证与兼容维护

许可证按本轮官方主分支/文档核对，正式迁入时还须固定具体版本或 commit，并复查所选子目录、依赖、资源和 NOTICE。MIT/BSD/Apache 仍需保留适用版权与许可声明；MPL 覆盖文件的修改按其条件处理。采用子进程不自动免除许可证义务。Bruno 的付费功能、各项目名称与图标也不能仅凭核心代码许可证就视为任意可复制。

适配器以能力探测和版本化结果协议为入口；协议变化只影响对应工具，并保留原始报告/诊断，不阻断任务列表和对话。更新工具前先用固定 fixture 跑契约测试，再发布经过验证的适配器版本。
