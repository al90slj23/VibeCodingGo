# VibeCue 提示音来源

现有 50 段乐句为 VibeCue 原创合成提示音，保留旧 ID。自然、城市与生活标签中的 8 项是实录剪辑，不是模拟自然声的合成器。macOS 系统音效由运行时读取 `/System/Library/Sounds`；仓库和应用包不包含 Apple 系统音频。

以下实录从原始发布站点 BigSoundBank 下载 WAV。每个音效页面均标注 CC0；[站点许可页](https://bigsoundbank.com/licenses.html)直接链接 [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/)。核验日期：2026-10-27。署名虽非 CC0 强制要求，仍保留作者和原始链接。

| 内置片段 | 原作者 / 原始作品 | 原录音片段 |
|---|---|---|
| 雨落片刻 | Joseph SARDIN · [Rain and Thunder #1](https://bigsoundbank.com/rain-and-storm-s0124.html) | 2.20–4.60 秒 |
| 海浪轻涌 | DenisChardonnet · [Sea: Waves](https://bigsoundbank.com/sea-waves-s0266.html) | 0.50–3.10 秒 |
| 林间鸟鸣 | Joseph SARDIN · [Forest](https://bigsoundbank.com/forest-s0100.html) | 2.10–4.50 秒 |
| 草间微风 | Joseph SARDIN · [Wind in Tall Grass](https://bigsoundbank.com/wind-in-tall-grass-s0908.html) | 7.50–9.90 秒 |
| 键盘轻敲 | Joseph SARDIN · [Computer Keyboard](https://bigsoundbank.com/computer-keyboard-s0229.html) | 0.80–2.40 秒 |
| 门铃来访 | Joseph SARDIN · [Doorbell #4](https://bigsoundbank.com/doorbell-4-s1604.html) | 0.15–2.55 秒 |
| 远处车流 | Joseph SARDIN · [Highway](https://bigsoundbank.com/highway-s0122.html) | 6.00–8.60 秒 |
| 路面脚步 | Joseph SARDIN · [Fast Steps on Concrete](https://bigsoundbank.com/fast-steps-on-concrete-s1318.html) | 7.00–8.80 秒 |

剪辑仅转单声道、重采样为 48 kHz / 16-bit PCM WAV、线性淡入淡出，并设置 RMS 目标和峰值上限；未变调、未变速、未循环。峰值不超过 -3.74 dBFS，目标 RMS 为 -17.72 dBFS，瞬态较强时优先限制峰值。森林原录音含微弱远处车流，海浪为 DenisChardonnet 的录音（页面结构化元数据写通用站点作者，按正文作者署名）。

`manifest.json` 记录原始下载 URL、原始 WAV SHA-256、精确起止片段、增益、淡入淡出时长和成品 SHA-256。原始长录音未收入仓库；音频完全本地播放，无运行时网络请求。

重制片段时，从 manifest 的 `download_url` 获取原 WAV，按 `clip_start_seconds` / `duration_seconds` 截取，使用 ffmpeg 转为 48 kHz 单声道浮点 PCM；按所记淡入淡出施加线性包络，再乘以 `10^(gain_db/20)`，四舍五入写入 int16 WAV。归一化增益为 min(0.13/RMS, 0.65/peak)，RMS 与 peak 在淡入淡出后测量；无需 ffmpeg 即可运行已打包应用。
