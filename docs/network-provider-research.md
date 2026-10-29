# Test 网络检测：来源与实现边界

初始核对日期：2026-10-29；补充核对与安装版实测：2026-10-30。初始研究阶段没有进行真实探测；0.32.0 已补齐六项安装版实测，详见[验收记录](network-solid-2026-10-30.md)。服务限额可能变化，应以运行时响应、限额接口与最新文档为准。

## 无新增授权的默认组合

| 用途 | 来源 | 无 Key 能力 | 需要说明的边界 |
| --- | --- | --- | --- |
| IP 归属与地区 | [ipapi.is 匿名层](https://ipapi.is/free-tier.html) | 每来源 IP 每 UTC 日 30 次；归属、ASN、地区 | 自 2026-09-01 起不再返回 VPN／代理／Tor／滥用判定；缺失字段不能当作否定 |
| IP 风险信号 | [proxycheck.io v3](https://proxycheck.io/api/) | 未注册来源 IP 每日 100 次；检测标记与来源风险分 | 匿名来源自身被判代理时可返回拒绝；额度不足或拒绝均显示未知，不自动改路由 |
| Tor 出口信号 | [Tor 官方出口列表](https://blog.torproject.org/changes-tor-exit-list-service/) | 下载出口 IP 列表后在本机匹配 | 命中表示曾观察为 Tor 出口；不等于恶意，未命中也不等于纯净 |
| 多地连通性 | [Globalping API](https://github.com/jsdelivr/globalping/blob/master/public/v1/spec.yaml) | 匿名每小时 250 个探针测试，每次最多 50 个探针 | 服务与选中探针会收到目标；HTTP 时延不是全球带宽测速 |
| 公共 DNS 对照 | [Cloudflare DoH JSON](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/) | 无 Key，查询 A／AAAA 等记录 | 域名被交给公共解析器；CDN、地理调度会导致合法差异 |
| 本机下载速率 | [Cloudflare Speedtest 开源引擎](https://github.com/cloudflare/speedtest) | 公共下载端点，按选定字节数请求 | 只代表本机到 Cloudflare 的这一条路径；消耗实际流量 |

上述外部查询只应在用户点击对应检测后发起；打开页面不执行探测。结果同时记录来源、时间、检测路径与覆盖不足项。不要生成没有可解释依据的“IP 纯净度 100 分”。

## proxycheck.io v3 契约

[官方 PHP 客户端 README](https://github.com/proxycheck/proxycheck-php#extra-information-included-in-the-query-result)提供完整 v3 返回示例。成功状态是 `ok` 或 `warning`，不是 `success`；IP 结果以所查 IP 为键，`detections` 下包含 `proxy`、`vpn`、`tor`、`hosting`、`anonymous` 等布尔值和 `risk`。`risk` 是该服务的风险分，应原样标注来源；不是账号封禁概率。新字段 `confidence` 允许缺失或为空，未知布尔值也必须保持未知。

默认只检查一个公开 IP，可使用 `https://proxycheck.io/v3/{IP}?tag=0`。`tag=0` 关闭该次正向检测日志，不是承诺服务不接收或不处理 IP。匿名 CDN 缓存约 10 分钟；`warning` 应保留提示，HTTP 401／403／429 或 `denied` 不能被当成“未检出”。仅发送 IP，不附带项目名称、路径或对话内容。v2 与 v3 格式不同，不能混用解析器。

## Globalping 请求、轮询与含义

最小示例（仅示意，不会由文档执行）：

```json
{"type":"ping","target":"example.com","locations":[{"country":"US"},{"country":"DE"},{"country":"SG"}]}
```

创建使用 `POST https://api.globalping.io/v1/measurements`，接受后返回测量 ID；结果从 `/v1/measurements/{id}` 读取。一个探针执行一次算一个 test，上例通常消耗 3 个，而不是 1 个。位置内 `limit` 与顶层 `limit` 互斥，实际探针数量可能少于请求数量。

[官方客户端规则](https://github.com/jsdelivr/globalping/blob/master/public/v1/spec.yaml)要求每次收到轮询响应后至少等 500ms，单个测量最多每秒两次查询，使用 ETag／If-None-Match；完成后停止轮询。限额从 `/v1/limits` 和响应头读取，429 后退避。请求、探针数量、响应体与总等待时间均应有上限。

[官方 Schema](https://github.com/jsdelivr/globalping/blob/master/public/v1/components/schemas.yaml)支持 ping、DNS、HTTP、traceroute、MTR。DNS、TCP、TLS、HTTP、各地区结果应分别展示。单次失败、NXDOMAIN、HTTP 403 或不同 DNS 答案只能形成观察结果，不能单独证明“域名已被全网封禁”。

## IP 痕迹、DNS 与浏览器泄露

原生应用可以报告自己的公网 IPv4／IPv6、接口与代理配置概况；它不能代替 Chrome、Safari、扩展、浏览器 DoH 与 WebRTC 的实际网络路径。[RFC 8828](https://www.rfc-editor.org/rfc/rfc8828.html)说明，WebRTC 的 ICE／STUN 在分流 VPN 或允许直连的代理环境下可能暴露额外公网地址。私网 IP 或 mDNS 名字本身不证明家庭公网 IP 或精确住址泄露。

浏览器检测应由用户点击后在其实际浏览器打开 [BrowserLeaks WebRTC](https://browserleaks.com/webrtc)、[DNSLeakTest](https://www.dnsleaktest.com/) 等页面；结果属于该浏览器。不要将应用内 WebKit 的结果冒充 Chrome 的检测结果。

[DNSLeakTest 的方法说明](https://test.dnsleaktest.com/what-is-the-difference.html)使用随机域名让权威服务器观察实际递归解析器。只读取本机配置的 DNS 服务器不构成 DNS 泄露测试。DNS 服务商、国家或 IP 不同也不自动等于泄露；需要与用户预期路径比较。[Google DoH](https://developers.google.com/speed/public-dns/docs/doh/json)提供 `edns_client_subnet=0.0.0.0/0`，避免向权威服务器额外提供客户端子网，但解析器本身仍会看到查询来源。

## 下载速率与流量

[Cloudflare 引擎](https://github.com/cloudflare/speedtest)默认下载 API 为 `https://speed.cloudflare.com/__down`，上传 API 为 `https://speed.cloudflare.com/__up`，文件大小按 bytes 指定。原生轻量实现应显式限制文件大小和总时间，显示实际字节数、耗时及 Mbps；极短下载应标为小样本，不推断宽带峰值，也不冒充完整 Cloudflare 测试。

[Cloudflare 官方测速页面](https://speed.cloudflare.com/)说明完整测试可能消耗最多 200MB 流量，并会处理连接 IP、估计地区与 ASN。VCGo 的轻量检测应使用自己的明确小流量上限；只在点击后运行，支持取消，避免自动后台反复测速。

## 暂不默认采用的来源

[Spamhaus](https://www.spamhaus.org/faqs/dnsbl-usage/)的公共 DNSBL 受非商业、低量及解析器条件限制，不宜作为通用免费应用的默认后端。StopForumSpam 官方 `/usage` 文档在本次访问中返回 403，未核实当前条款与限额，本轮不据此宣称已支持。

[OONI](https://ooni.org/about/)可作为未来“区域封锁历史证据”的独立来源；其 [API](https://api.ooni.io/)用于检索已有公开测量，不宜把缺少记录当成未封锁。启动 OONI Probe 会发布测量数据，不能静默替用户启动。后续集成应保留地区、网络、时间与异常证据。

## 后续模块建议

- HTTP／API 工作台：响应状态、TLS、重定向、请求时序与经过脱敏的可复制诊断。
- 域名与证书：DNS 记录、证书到期、证书链与安全响应头。
- 本机开发环境：端口占用、代理配置、运行时版本、项目服务健康。
- 发布检查：域名解析、HTTPS、构建版本和发布前后可达性对照。

这些可以逐项接入，先保留明确模块边界；本文件的建议不表示相关功能已经实现。

## 0.28.1 代理兼容验收

传输复用环境或 macOS CFNetwork 系统规则，PAC 通过原生接口有界解析，再交给系统 curl 执行 HTTP/HTTPS/SOCKS。代理配置和请求体仅经 stdin，日志不输出代理凭证；无自动直连降级。响应、stderr、gzip 展开、DNS/PAC/传输等待均设上限，保留 TLS 验证，不自动重定向。

代理端解析域名时只提交原公开 FQDN，本机私网/fake-IP DNS 结果作为单独观察，不用作连接目标；拒绝私网字面 IP、localhost 和内部后缀。透明 TUN 的 benchmark fake-IP 仅在确认 utun/tun 路由时兼容。本机 DNS 与代理端 HTTPS 不一致时显示部分结果，不把它判为全网封禁。HEAD 仅限制实际头部输出，不以网页声明的正文大小误判下载超限。

42 项隔离回归涵盖本机 HTTP CONNECT/SOCKS5h 握手、原生 PAC 脚本、NO_PROXY、凭证边界、超限终止、代理 DNS 与 TUN。2026-10-29 在当前系统 PAC/HTTP 路径对 example.com 发起一次最小 HTTPS HEAD，返回 200、约 1.2 秒，证书验证保留。没有查询用户出口 IP、创建全球探针或修改代理设置；此项只证明该传输路径，不代表所有公共检测来源都不会限流。


## 0.32.0 契约补充与实现

Globalping 探针地区从实际返回的平面字段 country/city/asn/network 读取，逐地区请求 limit: 1，并核对实际 probesCount、唯一地区、终态和丢包。记录已知测量 ID 后，停止、限流或部分失败仍可续查；续查只 GET 同一 ID，不重新 POST。原始输入、规范目标、类型与地区同时绑定，不能拿旧结果覆盖新的目标。

全球 HTTP 展示来源返回的单次 HTTPS HEAD 状态与分段时延；3xx 不当作已跟随后目标可达。官方探针会返回 TLS 授权状态，因此 HTTP 200 本身不证明证书可信：明确 authorized=false 时保留 HTTP 和时延，但整体与节点标为部分结果；缺失字段保持未知。本机 curl 的系统证书验证与远端探针的观察要分开理解。全球 firstByte 是建连后的分段时延，本机 curl 首字节则是从请求开始的累计时间，不直接横向比较。[官方探针实现](https://github.com/jsdelivr/globalping-probe/blob/master/src/command/handlers/http/undici.ts)。

域名检测对照系统 DNS、Cloudflare 与 Google DoH 的 A/AAAA，校验目标、记录类型和 CNAME 链；NXDOMAIN、NODATA、SERVFAIL 与目标 HTTP 错误分别报告。Google ECS 设为零。公共解析差异可能来自 CDN；没有记录、目标拒绝与单点超时都不能证明全网封禁。

IP 归属与风险响应必须对应所查目标，严格区分布尔值与未知值；风险来源不可用时仅以 Tor 官方列表补充 Tor 信号。ipify 不可用时可按同一路由从 Cloudflare trace 观察出口，来源会随结果显示。IPv4 与 IPv6 分组比较，不能因协议不同就判定泄露。

每次操作共享 45 秒默认预算，支持停止；测速最多 5 MiB、12 秒，同时报告实际字节数、整次请求有效速率与正文传输速率。当前结果仅驻留有界内存，不写入用量账本；手动复制报告与公开测量由用户明确触发。实现与测试边界见[0.32.0 验收](network-solid-2026-10-30.md)。
