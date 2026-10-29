# VibeCoding Go 提示音来源

50 段乐句为 VibeCoding Go 原创合成提示音，旧 ID 保留。24 项自然及城市音效是真实录音的短剪辑，其中本次新增 16 项青蛙、鸟鸣、虫鸣与森林溪流。macOS 系统音效由运行时读取 `/System/Library/Sounds`；仓库和应用包不包含 Apple 系统音频。

所有实录均来自 BigSoundBank。每个原始作品页标注 CC0，[站点许可页](https://bigsoundbank.com/licenses.html)直接链接 [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/)，允许剪辑、再分发和商用。核验日期分别记录于 manifest，最新为 2026-10-28。以下保留正文作者署名（可能与站点通用结构化元数据不同）。

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
| 夜蛙独唱 | Joseph SARDIN · [One frog](https://bigsoundbank.com/one-frog-s0819.html) | 6.85–9.65 秒 |
| 池畔蛙群 | Joseph SARDIN · [Frogs #2](https://bigsoundbank.com/frogs-2-s0998.html) | 20.80–24.40 秒 |
| 田间蟋蟀 | Joseph SARDIN · [Field cricket](https://bigsoundbank.com/field-cricket-s1020.html) | 8.55–11.55 秒 |
| 草丛虫鸣 | Joseph SARDIN · [Insect song #4](https://bigsoundbank.com/insect-song-4-s3187.html) | 0.15–2.95 秒 |
| 夜虫轻鸣 | Joseph SARDIN · [Nocturnal insect #1](https://bigsoundbank.com/song-of-nocturnal-insect-1-s0424.html) | 0.20–3.40 秒 |
| 夏日蝉鸣 | Clarisse · [Cicadas](https://bigsoundbank.com/cicadas-s3002.html) | 3.50–6.50 秒 |
| 知更鸟小调 | Joseph SARDIN · [Robin #4](https://bigsoundbank.com/robin-4-s1670.html) | 0.35–3.05 秒 |
| 乌鸫清唱 | Le tiroir du fond · [Common Blackbird #30](https://bigsoundbank.com/common-blackbird-30-s3503.html) | 0.35–3.35 秒 |
| 林鸮夜语 | Joseph SARDIN · [Tawny Owl #2](https://bigsoundbank.com/tawny-owl-2-s1764.html) | 0.00–3.60 秒 |
| 夜莺啼鸣 | Joseph SARDIN · [Common Nightingale #2](https://bigsoundbank.com/common-nightingale-2-s3087.html) | 0.10–3.20 秒 |
| 林莺轻唱 | Le tiroir du fond · [Eurasian blackcap #1](https://bigsoundbank.com/eurasian-blackcap-1-s3466.html) | 2.55–6.15 秒 |
| 林间溪语 | Pierre SIBANARCO · [Forest and Stream #1](https://bigsoundbank.com/forest-and-stream-1-s2713.html) | 22.45–26.25 秒 |
| 山溪潺潺 | Joseph SARDIN · [Small Stream #4](https://bigsoundbank.com/small-stream-4-s1354.html) | 20.40–23.80 秒 |
| 林冠微响 | Joseph SARDIN · [Forest #4](https://bigsoundbank.com/foret-4-s2749.html) | 16.50–20.30 秒 |
| 晨鸟苏醒 | Joseph SARDIN · [Birds Waking #3](https://bigsoundbank.com/wake-birds-3-s0999.html) | 26.40–30.00 秒 |
| 暮鸟归林 | Joseph SARDIN · [Evening Birds](https://bigsoundbank.com/evening-birds-s1859.html) | 0.00–3.40 秒 |

剪辑转为单声道、48 kHz / 16-bit PCM WAV，施加线性淡入淡出和受峰值限制的增益；未变调、未变速、未循环、未合成动物叫声。目标 RMS 为 -17.72 dBFS，峰值不超过 -3.74 dBFS；瞬态较强时优先限制峰值，因此实际 RMS 可低于目标。旧 8 段来自原站 WAV，新增 16 段来自原站 192 或 320 kbps MP3，来源格式在 manifest 标注；转换为 WAV 不意味着恢复无损原始质量。

标签允许交叉筛选，如自然＋鸟鸣＋动物；真实场地按原作者说明标注：池畔蛙群原场地在市区，夜虫物种未确定，草丛虫鸣仅推测为绿螽斯；不会将所有自然声音都声称为森林录音。森林旧录音含微弱远处车流，海浪作者为 DenisChardonnet，蝉鸣作者为 Clarisse，林溪作者为 Pierre SIBANARCO，乌鸫和林莺作者为 Le tiroir du fond。

`manifest.json` 记录原始下载 URL、原文件和来源页面 SHA-256、作者、许可、精确起止点、处理参数和成品 SHA-256。原始长录音未收入仓库；音效随应用打包，完全本地播放，无运行时网络请求。

重制时，先将所需 `download_url` 下载至临时目录，保留 URL 中的文件名。使用 `python3 scripts/prepare-recorded-sounds.py --source-dir 原始文件目录 --output-dir 输出目录 --ids nature-frog-solo,nature-cricket`。脚本先核验原文件 SHA-256，以 ffmpeg 解码，在淡入淡出后按 min(0.13/RMS, 0.65/peak) 归一化，再写入 int16 PCM；成品必须匹配 manifest 的 SHA-256。ffmpeg 只用于重制，不是应用运行依赖。
