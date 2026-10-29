# 2026-10-29 有界性能交付

## 已保留的修复

隔离的临时 VibeCue 账本不会隐式读取真实 `~/.cc-switch/cc-switch.db`。只有默认应用账本或明确配置的来源使用真实 CC Switch 数值库；临时账本使用自己的 `cc-switch-usage.sqlite3`。本次增加路径守卫回归，不重新实现此项。

历史证据来自 `/Users/al90slj23/Diagnostics/2026-10-29/cpu-vibecue-test-isolation-findings.md`：旧的五模块测试在 957 秒后仍未完成，原任务修复后相同命令在普通 HOME 下 78 项测试耗时 3.755 秒。这是既有历史证据，本轮没有重跑旧故障路径或真实大库压力测试。

## 本轮修改

1. **未变化的 CC 数值桥接复用已有快照。** `vibecue/usage_ccswitch.py` 检查来源 DB 与 WAL 的路径、设备、inode、大小和纳秒修改时间。只有来源读取完整且前后指纹一致、调用方确认原生数值未变时，才跳过数值读取和全量匹配。来源变更、WAL 更新、原生插入/更新/删除、分叉合并纠正、重建、读取失败或达到行数上限均不能复用旧快照。直接调用桥接默认仍完整核对。
2. **原生变更信号覆盖增删改。** `usage.py` 的连接局部 TEMP 表和 TEMP triggers 跟踪三种原生工具的 request 变更，事务回滚也回滚该信号，不依赖 `imported/updated` 报告计数。两个 enrichment 表的现有写入仅在桥接内部执行，缓存只在这些步骤完成后保存。
3. **同步中的轮询只读进度。** 新增 `python -m vibecue.usage sync-status`，只读取 `usage_meta` 的同步进度并检查已有锁，不做请求/价格/筛选/热图聚合。已有同步锁忙时也返回小进度快照。缺失账本不创建文件。
4. **UI 合并进度轮询。** `Sources/UsageDashboard.swift` 使用独立 `statusClient` 和一个 in-flight 标志；同步期间定时刷新走小进度接口，结束后只在统计页可见时完整查询。原有 `queryInFlight/queryPending` 与隐藏页面保护保留。读取失败按显示间隔退避 5–60 秒，避免 0.2 秒同步重试循环；恢复后清理进度读取暂态错误，真实同步错误仍独立保留。

同一任务的整包构建、完整类型检查和大型验证由根任务串行执行，子任务只做局部语法及有界 fixture 验证。本次没有停止其他服务、修改系统设置、改动账号/代理授权或复现系统重启。

## 隔离测量

输入固定为 **3000 条原生数值 + 3000 条 CC 数值 + 独立 50 个短会话文件**。每条正常路径重复 5 次，取墙钟与进程 CPU 时间中位数；来源、价格及账本均指向临时 fixture，未读取真实用户历史。

| 正常路径 | 墙钟中位数 | CPU 时间中位数 | 测量进程峰值 RSS |
| --- | ---: | ---: | ---: |
| 未变化桥接，原完整匹配路径 | 48.176 ms | 44.192 ms | 40.266 MiB |
| 未变化桥接，快照复用路径 | 0.067 ms | 0.066 ms | 40.266 MiB |
| 进度轮询，原完整 query 路径 | 13.540 ms | 11.000 ms | 40.266 MiB |
| 进度轮询，metadata-only 路径 | 0.273 ms | 0.270 ms | 40.266 MiB |

RSS 是同一进程包含 fixture 创建和预热的高水位，不能据此宣称内存占用下降；CPU 时间为该操作的进程 user+system 增量，不代表整机 CPU 占用率。

无变化同步证据：50 个文件仍做目录发现，但 **读入会话字节 0、重开 header 0、parser 调用 0、导入 0**，本轮墙钟 21.079 ms。桥接缓存命中时来源数值扫描 0，SQL trace 未出现 `FROM usage_requests` 或 `FROM usage_cc_metrics` 全量匹配语句。保留目录枚举可发现新增文件，本轮没有扩展为全局 watcher。

## 验证与位置

- 定向命令：`PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest tests.test_usage_sync_status tests.test_usage_ccswitch tests.test_usage`
- 本轮结果：**70 项通过，1.577 秒**。
- 覆盖：默认真实库隔离、源 WAL 变化、原生纯删除与纠正、歧义/旧匹配撤销、行数上限不缓存、无变化不扫描、metadata-only SQL authorizer、锁忙不聚合、缺失账本无副作用、CLI 路由。
- Swift 局部检查：`swiftc -frontend -parse Sources/UsageDashboard.swift` 通过。新增原生离线检查覆盖进度轮询合并、隐藏页面、失败退避和暂态错误恢复；整包原生检查由根任务执行。
- 测量数据：`build/vcgo-0.28.1-performance.json`。
- 定向测试日志：`build/vcgo-0.28.1-performance-tests.log`。
- 本轮临时测量脚本：`/private/tmp/vcgo-bounded-benchmark.py`（输入量固定且只使用临时 fixture）。

## 证据限制

这些数字证明固定小输入和无变化路径消除了重复工作，不是用户全部历史的性能承诺。没有重扫真实 CC 大库、真实全历史或复跑旧失控测试。已有诊断显示本机多任务构建叠加及严重内存/swap 压力；缺失 stackshot，不能将 2026-10-29 系统 panic 单独归因于 VibeCoding Go。完整发布、安装及 UI 验收结果由本轮根任务报告。

根任务完整检查时发现一个依赖本机真实 Codex 进程变化的旧监控用例。已将该 transcript-only 用例固定为隔离进程快照，保留进程丢失/替换的专门恢复测试；同时排除实际存在的短生命周期 `codex-code-mode-host`，防止工具辅助进程出现/退出被误判为任务所有者变化。进程探测仍仅读 PID、启动时间与可执行文件，不读取 argv。

## 根任务安装复核

0.28.1 / build 37 已安装到 `/Applications/VibeCoding Go.app`。正式构建、657 项原生检查和 745 项 Python 全套检查串行完成且通过；安装二进制与构建 SHA-256 一致，36 个已安装 Python runtime 模块逐一匹配本轮源码。通过已安装 runtime 执行只读 `sync-status` 得到可用进度，当前未同步；本机 watcher 正常且 Codex 桌面连接为 connected。

安装后校验原提醒 ID 全部保留，设置、草稿、对话窗口几何、货币显示、同步频率与账号额度文件哈希一致。真实 UI 确认任务列表进入独立对话窗口，不带工作台的功能标签。完整日志位于 `build/vcgo-0.28.1-build.log`、`build/vcgo-0.28.1-python.log` 和 `build/vcgo-0.28.1-*-self-test.json`。
