```text
/make-plan Redesign VibeCue based on the 2026-10-27 audit (7/30).
Verdict: 应重构 VibeCue 的视觉系统、效果构建和目录组织，同时保留可靠的事件来源与对话导航。
Why: 活跃装饰、弱层级、目录数量导向与固定假仪表共同影响核心体验，应重构而非仅降低饱和度。
Preserve: 可靠人工批准来源、暂停/静音/Reduce Motion、旧样式ID、自定义参数、单次随机快照、逐项预览、整卡绑定对话跳转。
Discard: 单体按family绘图大switch；强制darkAqua；默认把全部50×3平铺；无数据支撑的进度/频谱。
Moves:
1. Useful / understandable：以事件和注意力强度为主线，标签区分来源与视觉语言，推荐先行，旧50保留为可选目录。证据E1/E3。
2. Aesthetic / unobtrusive：效果拆为路径、光层、运动和出现消退的声明式配方；统一克制默认，旧ID映射到同一引擎。证据E2。
3. Honest / thorough：采用真实AppKit材质与语义色，移除假进度和假音频仪表，支持系统明暗。证据E2/E3/E4。
4. Useful / long-lasting：声音分合成、自然、城市生活、系统标签，实录可核验许可与来源；随机限制在所选标签池，保留稳定单次快照。证据E3。
5. As little design as possible：图库以标签/搜索/选中预览分层，常用页只露推荐样式与高级入口，逐项试听/整卡跳转继续可用。证据E1。
Non-goals: 改审批策略、修改Codex安装、接Claude、云端服务。
Deliver: native system materials/appearance, recipe renderer, tagged catalogue/search/scoped random, verified sound assets/licenses, compatibility and native UI QA; retain old IDs.
Cutover: new UI follows system light/dark, tagged libraries and per-item preview work; recipe engine handles old/new IDs; no fake data graphics; installed binary equals build; remote revision verified.
```
