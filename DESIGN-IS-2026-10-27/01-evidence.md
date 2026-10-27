# 证据（基线 af9869f）

## E1 结构与任务
- main.swift:1243–1274：事件类型、三页签、整套预览与复位6个公共控件；另4个侧栏入口。边缘页面默认26个控件，调色板最多33；卡片19；声音16。PresetBrowser.swift:48–73：每图库51个选项+再预览，共52。
- main.swift:1263–1266：330pt滚动视口容纳930/780/465pt内容。作者视图树最多5层，图库4层；系统自动视图层未测。
- main.swift:1312–1370/PresetBrowser.swift:72–73：预设下拉与图库、单独试听存在重复入口。CardStyles.swift:246–268,589–596：整卡与打开按钮均可返回对话，这是应保留的便利交互。

## E2 视觉与动效
- main.swift:1091–1104、PresetBrowser.swift:40–45：窗口强制darkAqua和硬编码RGB；不跟随系统外观。
- CardStyles.swift:327–333,361–364,501–503：所谓玻璃/磨砂是实色底+透明渐变，未采用NSVisualEffectView。
- Effects.swift:106–110,292–297,448：宽度默认28pt，范围2–80；柔光最宽默认58.8pt，向内脉冲默认98pt；UI写px而实际为AppKit逻辑点。
- main.swift:514–538,562–568：默认各屏边缘+一张卡，高层级跨Space。不是系统通知中心组件。
- 类型尺度观察到9/9.5/10/10.5/11/11.5/12/13/15/16/17/18/19/22/24pt；未有统一spacing/type token。颜色静态扫描56个hex、29个常量sRGB构造，非实际屏幕色数。
- 联系表/private/tmp/vibecue-50-cards.png与/private/tmp/vibecue-effects-contact-sheet.png展示了旧装饰族；仅为静态证据。

## E3 文案与诚实性
- main.swift:1244,1274与README:3,9–16将50×3数量作为首要卖点；实际任务文案在main.swift:70–91,1229，是状态、对话及是否需要操作。
- CardStyles.swift:492–496：固定70%进度、固定sin高度音柱；394固定递增柱，未接真实数据。应移除数据含义，不能当进度/音频仪表。
- Effects.swift:102–104、PresetBrowser.swift:85–87：名称“彩虹”实际沿用当前配色，可能单色。声音50项是原创合成，README已明确，不是自然实录。
- 未见付费诱导、稀缺或隐藏退出证据。人工批准来源、未接通状态与测试边界有明确披露，保留。

## E4 资源与可访问性
- scripts/build.sh:5–13：原生Swift/AppKit，初始JS=0；无界面网络请求是源码推断，未抓包。TTI/CPU/GPU/耗电未测。
- main.swift:334–335：常驻0.5秒非动画轮询；闲置设置页0个动画时钟。PresetBrowser.swift:118–135：选中边缘预览1个24fps时钟；主提示1个30fps时钟，重极光可降到12/15/20/24。
- main.swift:541–575、Effects.swift:211–218：减少动态效果支持，隐藏后撤销时钟。未见降低透明度/提高对比度的专门适配。
- empty/loading/error/success/disabled有文案或原生控件；键盘焦点顺序、VoiceOver、实测文字对比度未知。原生ARIA/skip-link不适用。卡片非激活不抢焦点，已有命中路由测试。

## E5 官方参考
- https://developer.apple.com/design/human-interface-guidelines/materials ：材质用于明确层级，标准组件优先，效果应克制。
- https://developer.apple.com/design/human-interface-guidelines/motion ：动效应服务反馈及理解，并遵循辅助功能偏好。
- https://developer.apple.com/design/human-interface-guidelines/notifications ：简洁有价值的内容、减少重复打扰、声音短且有辨识度。

证据由三个并行只读审查汇总；没有把源码推断当作本轮用户或桌面验收。
