# CC Switch Codex 额度共享

本目录针对官方 CC Switch **v4.0.21** 提供最小的本机构建补丁。

CC Switch 原版仅在进程内保存 Codex access token 和额度缓存；磁盘的多账号授权记录包含 refresh token，不能由第二个应用独立刷新，否则会与原应用竞争凭据轮换。本补丁让 **CC Switch 自己**继续通过原有 `query_codex_oauth_quota_for`、认证管理器和锁读取各账号额度，VibeCue 只读取不含凭据的结果。

## 开关与文件协议

- 默认关闭。CC Switch 配置目录存在普通文件 `vibecue-quota-sharing.enabled` 时启用；默认目录为 `~/.cc-switch`。
- 每 60 秒并行读取至多 20 个已有账号，不切换默认账号，不发起新登录。
- 每一轮最多等待 20 秒形成快照。超时查询不再标为成功；其 CC Switch 内部任务允许完成，避免在 OAuth 刷新轮换尚未保存时取消任务。
- 成功或失败均按账号记录。仅导出账号标识、邮箱、读取时间、用量百分比、重置时间以及固定错误码，不导出 token、原始上游响应、请求头或错误文本。
- 输出 `vibecue-codex-quota.json`，以 0600 临时文件原子替换，不增加 HTTP 端口或外部接口。
- 移除开关后不再查询或写入；VibeCue 会在 120 秒后把旧值标为过期。CC Switch 正式版升级若覆盖补丁，旧快照也会过期，不会继续显示为实时。

VibeCue 默认读取 `~/.cc-switch/vibecue-codex-quota.json`。如 CC Switch 使用自定义配置目录，可设置 `VIBECUE_CC_SWITCH_QUOTA_FILE` 指向其导出文件。读取端拒绝符号链接、其他用户所有、对其他用户开放或大于 1 MiB 的文件；只接受白名单字段。

## 构建

在官方 v4.0.21 源码目录应用。首次 Rust 构建会产生较大的缓存，请将 `CARGO_TARGET_DIR` 和 `TMPDIR` 指向空间足够的构建盘；本项目的本机构建放在 4TB 卷的 `build/` 下：

```sh
git apply --check /absolute/path/to/VibeCue/integrations/ccswitch/quota-sharing-v4.0.21.patch
git apply /absolute/path/to/VibeCue/integrations/ccswitch/quota-sharing-v4.0.21.patch
pnpm install --frozen-lockfile
pnpm run build:renderer
cargo test --release --manifest-path src-tauri/Cargo.toml --lib vibecue_quota
pnpm exec tauri build --bundles app --ci --no-sign --config '{"build":{"beforeBuildCommand":""},"bundle":{"createUpdaterArtifacts":false}}'
```

该本机构建不生成官方更新签名。macOS 安装前对生成的 `.app` 进行本机签名，并执行 `codesign --verify --deep --strict`；保留原版 `.app` 备份。官方更新会替换本机补丁，届时共享快照会自动过期，需要按新版本重新核对集成。

补丁只修改认证查询函数的 crate 可见性、注册导出模块和初始化后台循环；不修改账号管理、授权存储、供应商、切换或代理处理逻辑。`vibecue_quota.rs` 同时保存新增模块的可审阅源码；补丁是应用到官方源码的完整版本。

安装应保留原应用备份及整个 CC Switch 用户数据目录。启用标记后由 CC Switch 自身刷新已有授权，VibeCue 不接触其授权文件。
