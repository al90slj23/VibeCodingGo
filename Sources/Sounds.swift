import AppKit
import AVFoundation
import Foundation

fileprivate enum CueSoundVoice {
    case soft, glass, wood, synth, bell, water, pluck, air, rhythm, orbital
}

fileprivate struct CueSoundTone {
    let frequency: Double
    let duration: Double
    let gap: Double
    let endFrequency: Double?
    let chord: [Double]

    init(_ frequency: Double, _ duration: Double, _ gap: Double = 0.08, end: Double? = nil, chord: [Double] = []) {
        self.frequency = frequency; self.duration = duration; self.gap = gap
        endFrequency = end; self.chord = chord
    }
}

struct CueSoundTag {
    let id: String
    let name: String
    let description: String
}

struct CueSoundPreset {
    let id: String
    let name: String
    let family: String
    let description: String
    let duration: Double
    let tags: [String]
    /// Explicit provenance: a timbre metaphor never turns synthesis into a recording.
    let sourceType: String
    let sourceDescription: String
    let sourceURL: String?
    let license: String
    fileprivate let assetName: String?
    fileprivate let systemURL: URL?
    fileprivate let voice: CueSoundVoice
    fileprivate let score: [CueSoundTone]
    fileprivate let legacy: Bool

    fileprivate init(_ id: String, _ name: String, _ family: String, _ description: String,
                     _ voice: CueSoundVoice, _ score: [CueSoundTone], legacy: Bool = false) {
        self.id = id; self.name = name; self.family = family; self.description = description
        self.voice = voice; self.score = score; self.legacy = legacy
        tags = ["synthesized"]; sourceType = "synthesized"
        sourceDescription = "VibeCoding Go 原创合成提示音"; sourceURL = nil; license = "VibeCoding Go 项目许可"
        assetName = nil; systemURL = nil
        duration = Double(score.reduce(0) { $0 + Int($1.duration * Double(CueChime.sampleRate)) + Int($1.gap * Double(CueChime.sampleRate)) }) / Double(CueChime.sampleRate)
    }

    fileprivate init(recording id: String, _ name: String, tag: String, additionalTags: [String] = [], description: String,
                     duration: Double, file: String, author: String, sourceURL: String) {
        self.id = id; self.name = name; family = tag == "nature" ? "自然实录" : "城市与生活实录"
        self.description = description; self.duration = duration; tags = [tag] + additionalTags.filter { $0 != tag }
        sourceType = "recording"; sourceDescription = "实录 · \(author) · CC0"
        self.sourceURL = sourceURL; license = "CC0 1.0"; assetName = file; systemURL = nil
        voice = .soft; score = []; legacy = false
    }

    fileprivate init(system url: URL, duration: Double) {
        let filename = url.deletingPathExtension().lastPathComponent
        id = "system:" + filename; name = filename; family = "macOS 系统音效"
        description = "使用本机 macOS 的 \(filename) 音效；音频文件不随 VibeCoding Go 分发。"
        self.duration = duration; tags = ["system"]; sourceType = "system"
        sourceDescription = "本机 macOS 系统音效"; sourceURL = nil; license = "Apple 系统资源，仅本机读取"
        assetName = nil; systemURL = url; voice = .soft; score = []; legacy = false
    }
}

enum CueSoundCatalog {
    static let tags: [CueSoundTag] = [
        .init(id: "all", name: "全部", description: "所有可用音源，来源类型分别标注。"),
        .init(id: "synthesized", name: "合成提示音", description: "50 段 VibeCoding Go 原创合成乐句，玻璃、木琴等为音色描述。"),
        .init(id: "nature", name: "自然", description: "青蛙、鸟鸣、虫鸣、森林、溪流、雨和风的 CC0 实录短片段。"),
        .init(id: "forest", name: "森林氛围", description: "林冠风声和林间溪流，保留真实环境声层次。"),
        .init(id: "animals", name: "动物", description: "青蛙、鸟类和昆虫的真实声音，可与其他标签组合筛选。"),
        .init(id: "birds", name: "鸟鸣", description: "知更鸟、乌鸫、林鸮、夜莺、林莺和晨暮鸟群。"),
        .init(id: "insects", name: "虫鸣", description: "蟋蟀、蝉和夜间昆虫的户外录音；未确定的物种会明确标注。"),
        .init(id: "water", name: "水声", description: "山溪、林溪、海浪和雨声。"),
        .init(id: "urban", name: "城市与生活", description: "键盘、门铃、车流和脚步的 CC0 实录短片段。"),
        .init(id: "system", name: "系统音效", description: "动态读取本机 macOS 系统声音，不复制或分发 Apple 音频。")
    ]
    private static let waveformCache: NSCache<NSString, NSImage> = {
        let cache = NSCache<NSString, NSImage>(); cache.totalCostLimit = 8 * 1024 * 1024
        return cache
    }()
    // The original bank stays intact and is explicitly tagged as synthesis.
    // The first four also preserve the exact PCM used by earlier releases.
    static let synthesizedPresets: [CueSoundPreset] = [
        .init("complete", "上行三音", "经典", "熟悉的三音上行完成提示", .soft,
              [.init(523.25, 0.16, 0.035), .init(659.25, 0.16, 0.035), .init(783.99, 0.34, 0.12)], legacy: true),
        .init("input", "问答三音", "经典", "高低高三段，提示需要回答", .soft,
              [.init(880, 0.22, 0.10), .init(659.25, 0.22, 0.10), .init(880, 0.30, 0.12)], legacy: true),
        .init("permission", "双短一长", "经典", "两下短音接一段长音", .soft,
              [.init(440, 0.16, 0.09), .init(440, 0.16, 0.09), .init(554.37, 0.34, 0.12)], legacy: true),
        .init("error", "低音警示", "经典", "两段温和的低音下行", .soft,
              [.init(349.23, 0.26, 0.10), .init(261.63, 0.42, 0.14)], legacy: true),
        .init("soft-ready", "轻柔就绪", "经典", "柔和单音与淡淡和声", .soft,
              [.init(587.33, 0.48, 0.12, chord: [1.5])]),

        .init("glass-drop", "玻璃轻点", "玻璃", "一颗清脆的玻璃音", .glass,
              [.init(1046.5, 0.42, 0.10)]),
        .init("glass-pair", "玻璃双响", "玻璃", "明亮的低高两声", .glass,
              [.init(783.99, 0.16, 0.07), .init(1174.66, 0.32, 0.12)]),
        .init("glass-arpeggio", "水晶阶梯", "玻璃", "三颗玻璃音逐级上升", .glass,
              [.init(659.25, 0.13, 0.04), .init(880, 0.13, 0.04), .init(1318.51, 0.28, 0.10)]),
        .init("glass-steps", "晶片四拍", "玻璃", "四拍轻快而清晰的节奏", .glass,
              [.init(1046.5, 0.10, 0.035), .init(987.77, 0.10, 0.035), .init(1174.66, 0.10, 0.035), .init(1567.98, 0.25, 0.13)]),
        .init("glass-bloom", "水晶绽放", "玻璃", "缓缓散开的水晶和弦", .glass,
              [.init(698.46, 0.65, 0.14, chord: [1.25, 1.5])]),

        .init("wood-tap", "木音轻敲", "木琴", "低调的单下木质敲击", .wood,
              [.init(523.25, 0.28, 0.15)]),
        .init("wood-pair", "木音双击", "木琴", "两声温暖的小木琴", .wood,
              [.init(392, 0.16, 0.07), .init(587.33, 0.20, 0.12)]),
        .init("wood-triplet", "木琴三连", "木琴", "均匀的三连音，尾音稍长", .wood,
              [.init(659.25, 0.12, 0.04), .init(523.25, 0.12, 0.04), .init(783.99, 0.22, 0.12)]),
        .init("wood-rise", "暖木上行", "木琴", "温和向上的三个木音", .wood,
              [.init(349.23, 0.12, 0.04), .init(440, 0.12, 0.04), .init(698.46, 0.30, 0.12)]),
        .init("wood-answer", "木琴回应", "木琴", "高低回转的小段回应", .wood,
              [.init(783.99, 0.16, 0.06), .init(587.33, 0.14, 0.08), .init(698.46, 0.24, 0.14)]),

        .init("synth-ready", "电子就绪", "电子", "柔化的两段电子确认音", .synth,
              [.init(440, 0.18, 0.06), .init(880, 0.40, 0.12)]),
        .init("synth-pulse", "电子脉冲", "电子", "三段有间隔的低电子音", .synth,
              [.init(329.63, 0.11, 0.09), .init(329.63, 0.11, 0.09), .init(493.88, 0.24, 0.14)]),
        .init("synth-confirm", "电子确认", "电子", "短促引子与和声收尾", .synth,
              [.init(587.33, 0.10, 0.045), .init(739.99, 0.38, 0.14, chord: [1.5])]),
        .init("synth-question", "电子疑问", "电子", "高低交替，最后轻轻上扬", .synth,
              [.init(698.46, 0.14, 0.075), .init(523.25, 0.14, 0.10), .init(659.25, 0.30, 0.14, end: 783.99)]),
        .init("synth-arc", "电子弧线", "电子", "流畅上滑后落回中心音", .synth,
              [.init(392, 0.34, 0.04, end: 783.99), .init(659.25, 0.30, 0.14)]),

        .init("bell-clear", "清铃一响", "铃铛", "清亮而不尖锐的单声铃", .bell,
              [.init(783.99, 0.80, 0.18)]),
        .init("bell-double", "双铃到访", "铃铛", "两声间隔分明的门铃式提示", .bell,
              [.init(659.25, 0.30, 0.10), .init(523.25, 0.65, 0.15)]),
        .init("bell-triangle", "三角轻铃", "铃铛", "三音回转的明亮铃声", .bell,
              [.init(987.77, 0.16, 0.06), .init(783.99, 0.16, 0.06), .init(1174.66, 0.55, 0.15)]),
        .init("bell-dawn", "晨光小铃", "铃铛", "低音起步，柔和和弦展开", .bell,
              [.init(392, 0.23, 0.07), .init(587.33, 0.60, 0.16, chord: [1.25])]),
        .init("bell-halo", "铃声光环", "铃铛", "一层明亮的铃声和声", .bell,
              [.init(523.25, 0.90, 0.16, chord: [1.5, 2])]),

        .init("water-drop", "水滴轻落", "水滴", "一声圆润的下滑水滴", .water,
              [.init(1174.66, 0.35, 0.12, end: 587.33)]),
        .init("water-pair", "双滴回声", "水滴", "两滴水音一高一低", .water,
              [.init(1318.51, 0.19, 0.09, end: 880), .init(987.77, 0.30, 0.13, end: 659.25)]),
        .init("water-ripple", "水面涟漪", "水滴", "三滴由近及远的小回响", .water,
              [.init(880, 0.18, 0.045, end: 587.33), .init(1174.66, 0.17, 0.07, end: 783.99), .init(1567.98, 0.27, 0.14, end: 1046.5)]),
        .init("water-sparkle", "水珠闪光", "水滴", "五颗轻快小水珠", .water,
              [.init(1046.5, 0.09, 0.025, end: 783.99), .init(1318.51, 0.09, 0.025, end: 987.77), .init(1567.98, 0.09, 0.025, end: 1174.66), .init(1318.51, 0.09, 0.025, end: 987.77), .init(1760, 0.23, 0.13, end: 1318.51)]),
        .init("water-answer", "水滴问答", "水滴", "低高两段、留出回答的节奏", .water,
              [.init(783.99, 0.22, 0.12, end: 523.25), .init(1174.66, 0.40, 0.14, end: 880)]),

        .init("pluck-light", "拨弦轻触", "拨弦", "轻弹一根温暖的短弦", .pluck,
              [.init(440, 0.44, 0.12)]),
        .init("pluck-up", "拨弦上行", "拨弦", "三个干净的上行拨弦音", .pluck,
              [.init(392, 0.14, 0.045), .init(493.88, 0.14, 0.045), .init(783.99, 0.34, 0.13)]),
        .init("pluck-down", "拨弦落定", "拨弦", "由高到低安稳落定", .pluck,
              [.init(880, 0.13, 0.05), .init(659.25, 0.17, 0.05), .init(523.25, 0.38, 0.13)]),
        .init("pluck-gentle", "柔弦和声", "拨弦", "轻盈的小和弦与单音", .pluck,
              [.init(349.23, 0.32, 0.08, chord: [1.25, 1.5]), .init(698.46, 0.29, 0.13)]),
        .init("pluck-return", "拨弦回环", "拨弦", "四音环绕后回到起点", .pluck,
              [.init(587.33, 0.12, 0.035), .init(739.99, 0.12, 0.035), .init(880, 0.12, 0.06), .init(587.33, 0.36, 0.13)]),

        .init("air-soft", "轻风低语", "空气", "慢起音的柔软和声", .air,
              [.init(523.25, 0.72, 0.15, chord: [1.5])]),
        .init("air-rise", "微风升起", "空气", "轻轻上升的一段气垫音", .air,
              [.init(392, 0.75, 0.14, end: 659.25)]),
        .init("air-pair", "风铃问候", "空气", "两段轻柔的空气音", .air,
              [.init(659.25, 0.30, 0.08), .init(880, 0.45, 0.14)]),
        .init("air-breathe", "呼吸一刻", "空气", "柔和缓起、缓落的低和声", .air,
              [.init(293.66, 0.90, 0.15, chord: [1.25, 1.5])]),
        .init("air-glow", "微光浮起", "空气", "细腻的三段悬浮和声", .air,
              [.init(440, 0.22, 0.035), .init(554.37, 0.22, 0.035), .init(830.61, 0.48, 0.15, chord: [1.5])]),

        .init("rhythm-two", "两拍轻击", "节奏", "短长两拍，清楚而低调", .rhythm,
              [.init(493.88, 0.12, 0.12), .init(659.25, 0.28, 0.15)]),
        .init("rhythm-three", "三拍提醒", "节奏", "两短一轻长，明快的三拍", .rhythm,
              [.init(783.99, 0.10, 0.08), .init(783.99, 0.10, 0.08), .init(987.77, 0.25, 0.14)]),
        .init("rhythm-sync", "错拍律动", "节奏", "有留白的切分小节奏", .rhythm,
              [.init(440, 0.10, 0.055), .init(659.25, 0.10, 0.16), .init(554.37, 0.18, 0.045), .init(880, 0.25, 0.14)]),
        .init("rhythm-tiny", "微型五拍", "节奏", "五下逐渐升高的小节拍", .rhythm,
              [.init(587.33, 0.075, 0.035), .init(659.25, 0.075, 0.035), .init(739.99, 0.075, 0.035), .init(880, 0.075, 0.07), .init(1174.66, 0.22, 0.14)]),
        .init("rhythm-check", "完成节拍", "节奏", "轻快四拍和明确的结束音", .rhythm,
              [.init(523.25, 0.09, 0.08), .init(523.25, 0.09, 0.04), .init(783.99, 0.13, 0.10), .init(1046.5, 0.28, 0.12)]),

        .init("orbital-ping", "轨道回响", "科幻", "清晰的柔化调频单音", .orbital,
              [.init(698.46, 0.48, 0.14)]),
        .init("orbital-scan", "星际扫描", "科幻", "平滑上扫再轻轻收束", .orbital,
              [.init(329.63, 0.65, 0.15, end: 987.77)]),
        .init("orbital-twin", "双星连线", "科幻", "两声分明的星际问候", .orbital,
              [.init(587.33, 0.23, 0.13), .init(1174.66, 0.39, 0.14)]),
        .init("orbital-lift", "升空信号", "科幻", "三段小幅上滑，轻盈升空", .orbital,
              [.init(392, 0.17, 0.045, end: 493.88), .init(587.33, 0.17, 0.045, end: 739.99), .init(880, 0.35, 0.14, end: 1046.5)]),
        .init("orbital-lock", "轨道锁定", "科幻", "短引导音接稳定和声", .orbital,
              [.init(880, 0.11, 0.10), .init(440, 0.58, 0.14, chord: [1.5])])
    ]

    static let recordedPresets: [CueSoundPreset] = [
        .init(recording: "nature-rain", "雨落片刻", tag: "nature", additionalTags: ["water"], description: "户外雨声实录，截取原录音前段；淡入淡出，无循环。", duration: 2.4,
              file: "nature-rain.wav", author: "Joseph SARDIN", sourceURL: "https://bigsoundbank.com/rain-and-storm-s0124.html"),
        .init(recording: "nature-waves", "海浪轻涌", tag: "nature", additionalTags: ["water"], description: "海边浪花实录短片段；保留原环境声。", duration: 2.6,
              file: "nature-waves.wav", author: "DenisChardonnet", sourceURL: "https://bigsoundbank.com/sea-waves-s0266.html"),
        .init(recording: "nature-birds", "林间鸟鸣", tag: "nature", additionalTags: ["forest", "animals", "birds"], description: "森林鸟鸣实录短片段，背景含自然环境声及微弱远处车流。", duration: 2.4,
              file: "nature-birds.wav", author: "Joseph SARDIN", sourceURL: "https://bigsoundbank.com/forest-s0100.html"),
        .init(recording: "nature-wind", "草间微风", tag: "nature", description: "风吹草丛实录短片段；淡入淡出，无循环。", duration: 2.4,
              file: "nature-wind.wav", author: "Joseph SARDIN", sourceURL: "https://bigsoundbank.com/wind-in-tall-grass-s0908.html"),
        .init(recording: "urban-keyboard", "键盘轻敲", tag: "urban", description: "实体电脑键盘实录，截取一小段敲键声。", duration: 1.6,
              file: "urban-keyboard.wav", author: "Joseph SARDIN", sourceURL: "https://bigsoundbank.com/computer-keyboard-s0229.html"),
        .init(recording: "urban-doorbell", "门铃来访", tag: "urban", description: "电磁机械门铃实录短片段，包含金属簧片余音。", duration: 2.4,
              file: "urban-doorbell.wav", author: "Joseph SARDIN", sourceURL: "https://bigsoundbank.com/doorbell-4-s1604.html"),
        .init(recording: "urban-traffic", "远处车流", tag: "urban", description: "道路旁约 50 米处的车辆实录短片段。", duration: 2.6,
              file: "urban-traffic.wav", author: "Joseph SARDIN", sourceURL: "https://bigsoundbank.com/highway-s0122.html"),
        .init(recording: "urban-footsteps", "路面脚步", tag: "urban", description: "鞋底踏过混凝土地面的实录短片段。", duration: 1.8,
              file: "urban-footsteps.wav", author: "Joseph SARDIN", sourceURL: "https://bigsoundbank.com/fast-steps-on-concrete-s1318.html"),
        .init(recording: "nature-frog-solo", "夜蛙独唱", tag: "nature", additionalTags: ["animals"], description: "夜间池边单只青蛙实录，保留一小段自然鸣叫。", duration: 2.8,
              file: "nature-frog-solo.wav", author: "Joseph SARDIN", sourceURL: "https://bigsoundbank.com/one-frog-s0819.html"),
        .init(recording: "nature-frog-chorus", "池畔蛙群", tag: "nature", additionalTags: ["animals", "water"], description: "池塘蛙群实录，背景含流水声；原场地在市区。", duration: 3.6,
              file: "nature-frog-chorus.wav", author: "Joseph SARDIN", sourceURL: "https://bigsoundbank.com/frogs-2-s0998.html"),
        .init(recording: "nature-cricket", "田间蟋蟀", tag: "nature", additionalTags: ["animals", "insects"], description: "田野蟋蟀的真实鸣声，截取连续短片段。", duration: 3.0,
              file: "nature-cricket.wav", author: "Joseph SARDIN", sourceURL: "https://bigsoundbank.com/field-cricket-s1020.html"),
        .init(recording: "nature-grass-insect", "草丛虫鸣", tag: "nature", additionalTags: ["animals", "insects"], description: "夜间植被中的昆虫实录；原作者推测为绿螽斯，物种未完全确定。", duration: 2.8,
              file: "nature-grass-insect.wav", author: "Joseph SARDIN", sourceURL: "https://bigsoundbank.com/insect-song-4-s3187.html"),
        .init(recording: "nature-night-insect", "夜虫轻鸣", tag: "nature", additionalTags: ["animals", "insects"], description: "未确定物种的夜间昆虫实录，不将其标为蟋蟀或蝉。", duration: 3.2,
              file: "nature-night-insect.wav", author: "Joseph SARDIN", sourceURL: "https://bigsoundbank.com/song-of-nocturnal-insect-1-s0424.html"),
        .init(recording: "nature-cicadas", "夏日蝉鸣", tag: "nature", additionalTags: ["animals", "insects"], description: "自然环境中多只蝉的真实鸣声，保留原有环境底声。", duration: 3.0,
              file: "nature-cicadas.wav", author: "Clarisse", sourceURL: "https://bigsoundbank.com/cicadas-s3002.html"),
        .init(recording: "nature-robin", "知更鸟小调", tag: "nature", additionalTags: ["animals", "birds"], description: "知更鸟站在树上鸣唱的实录短句。", duration: 2.7,
              file: "nature-robin.wav", author: "Joseph SARDIN", sourceURL: "https://bigsoundbank.com/robin-4-s1670.html"),
        .init(recording: "nature-blackbird", "乌鸫清唱", tag: "nature", additionalTags: ["animals", "birds"], description: "欧乌鸫的真实歌声，一段清晰短句。", duration: 3.0,
              file: "nature-blackbird.wav", author: "Le tiroir du fond", sourceURL: "https://bigsoundbank.com/common-blackbird-30-s3503.html"),
        .init(recording: "nature-tawny-owl", "林鸮夜语", tag: "nature", additionalTags: ["animals", "birds"], description: "灰林鸮的真实鸣叫；截取短段并柔和收尾。", duration: 3.6,
              file: "nature-tawny-owl.wav", author: "Joseph SARDIN", sourceURL: "https://bigsoundbank.com/tawny-owl-2-s1764.html"),
        .init(recording: "nature-nightingale", "夜莺啼鸣", tag: "nature", additionalTags: ["animals", "birds"], description: "夜莺的真实歌声，保留鸣声起伏。", duration: 3.1,
              file: "nature-nightingale.wav", author: "Joseph SARDIN", sourceURL: "https://bigsoundbank.com/common-nightingale-2-s3087.html"),
        .init(recording: "nature-blackcap", "林莺轻唱", tag: "nature", additionalTags: ["animals", "birds"], description: "黑顶林莺的真实歌声，保留原录音的停顿。", duration: 3.6,
              file: "nature-blackcap.wav", author: "Le tiroir du fond", sourceURL: "https://bigsoundbank.com/eurasian-blackcap-1-s3466.html"),
        .init(recording: "nature-forest-brook", "林间溪语", tag: "nature", additionalTags: ["forest", "water", "birds"], description: "林间小溪与自然环境实录，原环境含乌鸫、其他鸟类和飞虫。", duration: 3.8,
              file: "nature-forest-brook.wav", author: "Pierre SIBANARCO", sourceURL: "https://bigsoundbank.com/forest-and-stream-1-s2713.html"),
        .init(recording: "nature-mountain-stream", "山溪潺潺", tag: "nature", additionalTags: ["water"], description: "山间小溪实录；连续流水以淡入淡出收尾，不循环。", duration: 3.4,
              file: "nature-mountain-stream.wav", author: "Joseph SARDIN", sourceURL: "https://bigsoundbank.com/small-stream-4-s1354.html"),
        .init(recording: "nature-forest-canopy", "林冠微响", tag: "nature", additionalTags: ["forest", "birds"], description: "森林中的鸟声与风声实录，保留环境层次。", duration: 3.8,
              file: "nature-forest-canopy.wav", author: "Joseph SARDIN", sourceURL: "https://bigsoundbank.com/foret-4-s2749.html"),
        .init(recording: "nature-dawn-birds", "晨鸟苏醒", tag: "nature", additionalTags: ["animals", "birds"], description: "法国乡间黎明鸟群实录；原录于早晨六点半。", duration: 3.6,
              file: "nature-dawn-birds.wav", author: "Joseph SARDIN", sourceURL: "https://bigsoundbank.com/wake-birds-3-s0999.html"),
        .init(recording: "nature-evening-birds", "暮鸟归林", tag: "nature", additionalTags: ["animals", "birds"], description: "乡间入夜前最后一阵鸟鸣实录，原录于傍晚九点。", duration: 3.4,
              file: "nature-evening-birds.wav", author: "Joseph SARDIN", sourceURL: "https://bigsoundbank.com/evening-birds-s1859.html")
    ]

    /// Inventory is built from the local OS, so unavailable system sounds never
    /// appear as selectable presets. No Apple files are bundled with the app.
    static let systemPresets: [CueSoundPreset] = {
        let root = URL(fileURLWithPath: "/System/Library/Sounds", isDirectory: true)
        let urls = (try? FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: [.isRegularFileKey], options: [.skipsHiddenFiles])) ?? []
        return urls.sorted { $0.lastPathComponent.localizedStandardCompare($1.lastPathComponent) == .orderedAscending }.compactMap { url in
            guard ["aiff", "aif", "wav", "caf"].contains(url.pathExtension.lowercased()),
                  (try? url.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile) == true,
                  let file = try? AVAudioFile(forReading: url) else { return nil }
            let duration = Double(file.length) / file.fileFormat.sampleRate
            guard duration > 0, duration <= 30 else { return nil }
            return CueSoundPreset(system: url, duration: duration)
        }
    }()

    static let presets: [CueSoundPreset] = synthesizedPresets + recordedPresets + systemPresets
    static func presets(tagID: String) -> [CueSoundPreset] {
        tagID == "all" ? presets : presets.filter { $0.tags.contains(tagID) }
    }
    static func tag(id: String) -> CueSoundTag? { tags.first { $0.id == id } }
    static func preset(id: String) -> CueSoundPreset? { presets.first { $0.id == id } }
    static func contains(id: String) -> Bool { preset(id: id) != nil }
    static func displayName(id: String) -> String {
        if id == "silent" { return "静音" }
        if id.hasPrefix("custom:") { return "自选音效" }
        return preset(id: id)?.name ?? "提示音"
    }

    fileprivate static func recordingURL(for preset: CueSoundPreset) -> URL? {
        guard let filename = preset.assetName, filename == URL(fileURLWithPath: filename).lastPathComponent else { return nil }
        var roots: [URL] = []
        if let resources = Bundle.main.resourceURL { roots.append(resources.appendingPathComponent("Sounds", isDirectory: true)) }
        // The source fallback lets a developer run the command-line self-test
        // before packaging. Installed applications use their bundled resource.
        if Bundle.main.bundleURL.pathExtension != "app" {
            roots.append(URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("assets/Sounds", isDirectory: true))
        }
        return roots.map { $0.appendingPathComponent(filename) }.first { FileManager.default.isReadableFile(atPath: $0.path) }
    }

    static func waveformImage(for preset: CueSoundPreset, size: NSSize = NSSize(width: 180, height: 44)) -> NSImage {
        let key = (preset.id + ":" + String(Int(size.width)) + "x" + String(Int(size.height))) as NSString
        if let image = waveformCache.object(forKey: key) { return image }
        guard let wav = try? CueChime.resolvedWaveData(for: preset) else {
            return NSImage(systemSymbolName: "speaker.slash", accessibilityDescription: "音源不可用") ?? NSImage(size: size)
        }
        let samples = CueChime.samples(from: wav)
        guard !samples.isEmpty else { return NSImage(size: size) }
        let width = max(16, Int(size.width)), height = max(16, Int(size.height))
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: width * 4, bitsPerPixel: 32)!
        NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        NSColor.clear.setFill(); NSRect(x: 0, y: 0, width: width, height: height).fill(using: .copy)
        let path = NSBezierPath(); path.lineCapStyle = .round; path.lineWidth = 1.5
        let columns = max(8, width / 3)
        for column in 0..<columns {
            let start = column * samples.count / columns
            let end = max(start + 1, (column + 1) * samples.count / columns)
            let peak = samples[start..<min(end, samples.count)].map { abs(Double($0) / Double(Int16.max)) }.max() ?? 0
            let amplitude = max(1, CGFloat(peak) * CGFloat(height) * 0.6)
            let x = CGFloat(column) * CGFloat(width) / CGFloat(columns) + 1
            path.move(to: NSPoint(x: x, y: CGFloat(height) / 2 - amplitude / 2))
            path.line(to: NSPoint(x: x, y: CGFloat(height) / 2 + amplitude / 2))
        }
        NSColor(srgbRed: 0.35, green: 0.88, blue: 0.77, alpha: 0.95).setStroke(); path.stroke()
        NSGraphicsContext.restoreGraphicsState()
        let image = NSImage(size: size); image.addRepresentation(rep)
        waveformCache.setObject(image, forKey: key, cost: width * height * 4)
        return image
    }

    static func validationReport() -> [String: Bool] {
        var hashes = Set<UInt64>()
        var nonSilent = true, notClipped = true, validDuration = true, stableData = true
        for preset in synthesizedPresets {
            let wav = CueChime.synthesizedWaveData(kind: preset.id)
            let samples = CueChime.samples(from: wav)
            let peak = samples.map { abs(Int($0)) }.max() ?? 0
            let energy = samples.reduce(0.0) { $0 + pow(Double($1) / Double(Int16.max), 2) }
            nonSilent = nonSilent && peak > 1000 && sqrt(energy / Double(max(1, samples.count))) > 0.005
            notClipped = notClipped && peak < 32000
            let duration = Double(samples.count) / Double(CueChime.sampleRate)
            validDuration = validDuration && duration >= 0.4 && duration <= 2.0 && abs(duration - preset.duration) < 0.0001
            var hash: UInt64 = 1469598103934665603
            for value in wav { hash = (hash ^ UInt64(value)) &* 1099511628211 }
            hashes.insert(hash)
            stableData = stableData && wav == CueChime.synthesizedWaveData(kind: preset.id)
        }
        let recordedData = recordedPresets.compactMap { try? CueChime.resolvedWaveData(for: $0) }
        let recordingsValid = recordedData.count == recordedPresets.count && zip(recordedPresets, recordedData).allSatisfy { preset, data in
            let values = CueChime.samples(from: data)
            let peak = values.map { abs(Int($0)) }.max() ?? 0
            let rms = sqrt(values.reduce(0.0) { $0 + pow(Double($1) / Double(Int16.max), 2) } / Double(max(1, values.count)))
            return abs(Double(values.count) / Double(CueChime.sampleRate) - preset.duration) < 0.0001 && peak < 32000 && peak > 1000 && rms > 0.005
        }
        let systemReadable = systemPresets.allSatisfy { preset in
            guard let url = preset.systemURL else { return false }
            return url.path.hasPrefix("/System/Library/Sounds/") && FileManager.default.isReadableFile(atPath: url.path)
        }
        var checks = ["exactly_50_synthesized_presets": synthesizedPresets.count == 50,
                "unique_sound_ids": Set(presets.map(\.id)).count == presets.count,
                "fifty_distinct_audio_signals": hashes.count == 50,
                "all_non_silent": nonSilent, "no_pcm_clipping": notClipped,
                "short_notification_durations": validDuration,
                "deterministic_local_synthesis": stableData,
                "legacy_ids_preserved": ["complete", "input", "permission", "error"].allSatisfy { preset(id: $0)?.legacy == true },
                "at_least_24_cc0_recordings": recordedPresets.count >= 24 && recordedPresets.allSatisfy { $0.sourceType == "recording" && $0.license == "CC0 1.0" && $0.sourceURL != nil },
                "recordings_decodable_non_silent_and_unclipped": recordingsValid,
                "expanded_nature_and_preserved_urban_recordings": self.presets(tagID: "nature").count >= 20 && self.presets(tagID: "urban").count == 4,
                "forest_animal_bird_insect_water_filters_have_recordings": ["forest", "animals", "birds", "insects", "water"].allSatisfy { !self.presets(tagID: $0).isEmpty && self.presets(tagID: $0).allSatisfy { $0.sourceType == "recording" } },
                "source_tags_are_explicit": presets.allSatisfy { !$0.tags.isEmpty && $0.tags.allSatisfy { tag(id: $0) != nil } && !$0.sourceDescription.isEmpty },
                "system_files_read_from_os_only": systemReadable,
                "silent_is_separate": !contains(id: "silent")]
        checks.merge(CueSoundPreparation.validationReport()) { _, new in new }
        return checks
    }
}

/// One finite, latest-only audio preparation owner. Playback and its UI state
/// remain with the caller; file reads, synthesis, decoding and device preparation
/// never run on the UI thread. A cancelled/older request cannot start a sound.
struct CuePreparedSound {
    let player: AVAudioPlayer
    let sourcePath: String
    let preparationMilliseconds: Double
    let wasPreparedOnMainThread: Bool
}

final class CueSoundPreparation {
    private let queue = DispatchQueue(label: "local.vibecue.sound-preparation", qos: .utility)
    private let lock = NSLock()
    private var revision: UInt64 = 0

    private func nextRevision() -> UInt64 {
        lock.lock(); defer { lock.unlock() }
        revision &+= 1
        return revision
    }
    private func isCurrent(_ expected: UInt64) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return revision == expected
    }
    func cancel() { _ = nextRevision() }

    func prepare(kind: String, customDirectory: URL, volume: Float,
                 completion: @escaping (Result<CuePreparedSound, Error>) -> Void) {
        let expected = nextRevision()
        queue.async { [weak self] in
            guard let self, self.isCurrent(expected) else { return }
            let result = Result { try Self.prepareNow(kind: kind, customDirectory: customDirectory, volume: volume) }
            guard self.isCurrent(expected) else {
                if case .success(let prepared) = result { prepared.player.stop() }
                return
            }
            DispatchQueue.main.async { [weak self] in
                guard let self, self.isCurrent(expected) else {
                    if case .success(let prepared) = result { prepared.player.stop() }
                    return
                }
                completion(result)
            }
        }
    }

    private static func customSource(kind: String, directory: URL) throws -> URL {
        let name = String(kind.dropFirst(7))
        guard !name.isEmpty, name != ".", name != "..", !name.contains("/"), !name.contains("\\"),
              !name.contains("\0"), name == URL(fileURLWithPath: name).lastPathComponent else {
            throw NSError(domain: "VibeCueAudio", code: 3, userInfo: [NSLocalizedDescriptionKey: "Invalid custom sound selection"])
        }
        let path = directory.appendingPathComponent(name)
        let properties = try path.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey])
        guard properties.isRegularFile == true, properties.isSymbolicLink != true,
              let bytes = properties.fileSize, bytes > 0, bytes <= 20 * 1024 * 1024 else {
            throw NSError(domain: "VibeCueAudio", code: 4, userInfo: [NSLocalizedDescriptionKey: "Custom sound is unavailable"])
        }
        return path
    }

    private static func prepareNow(kind: String, customDirectory: URL, volume: Float) throws -> CuePreparedSound {
        let started = ProcessInfo.processInfo.systemUptime
        let player: AVAudioPlayer
        let source: String
        if kind.hasPrefix("custom:") {
            let path = try customSource(kind: kind, directory: customDirectory)
            player = try AVAudioPlayer(contentsOf: path); source = path.path
        } else {
            guard let preset = CueSoundCatalog.preset(id: kind) else { throw CocoaError(.fileReadNoSuchFile) }
            if let url = preset.systemURL {
                player = try AVAudioPlayer(contentsOf: url); source = url.path
            } else {
                player = try AVAudioPlayer(data: CueChime.resolvedWaveData(for: preset))
                source = "memory:" + preset.id
            }
        }
        guard player.duration.isFinite, player.duration > 0, player.duration <= 30 else {
            throw NSError(domain: "VibeCueAudio", code: 4, userInfo: [NSLocalizedDescriptionKey: "Sound duration is invalid"])
        }
        player.volume = volume.isFinite ? max(0, min(1, volume)) : 0
        player.isMeteringEnabled = true
        guard player.prepareToPlay() else {
            throw NSError(domain: "VibeCueAudio", code: 1, userInfo: [NSLocalizedDescriptionKey: "Audio output preparation failed"])
        }
        return CuePreparedSound(player: player, sourcePath: source,
                                preparationMilliseconds: max(0, ProcessInfo.processInfo.systemUptime - started) * 1000,
                                wasPreparedOnMainThread: Thread.isMainThread)
    }

    /// Silent isolation checks. No sound is played and no user files are read.
    static func validationReport() -> [String: Bool] {
        _ = NSApplication.shared
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("vcgo-audio-preparation-" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        var checks: [String: Bool] = [:]
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let preparation = CueSoundPreparation()
            var received: Result<CuePreparedSound, Error>?, callbackOnMain = false, oldDelivered = false
            preparation.prepare(kind: "complete", customDirectory: directory, volume: 0) { value in
                callbackOnMain = Thread.isMainThread; received = value
            }
            let deadline = Date().addingTimeInterval(10)
            while received == nil, deadline.timeIntervalSinceNow > 0 { RunLoop.current.run(until: Date().addingTimeInterval(0.01)) }
            checks["audio_preparation_main_callback_is_bounded"] = received != nil && callbackOnMain
            if case .success(let prepared) = received {
                checks["audio_preparation_work_runs_off_main_thread"] = !prepared.wasPreparedOnMainThread
                checks["audio_preparation_is_silent_and_uses_memory_without_files"] = !prepared.player.isPlaying && prepared.player.volume == 0 && prepared.sourcePath == "memory:complete" && ((try? FileManager.default.contentsOfDirectory(atPath: directory.path).isEmpty) == true)
            } else { checks["audio_preparation_is_silent_and_uses_memory_without_files"] = false }
            var newestDelivered = false
            preparation.prepare(kind: "input", customDirectory: directory, volume: 0) { _ in oldDelivered = true }
            preparation.prepare(kind: "complete", customDirectory: directory, volume: 0) { value in
                if case .success(let prepared) = value { prepared.player.stop(); newestDelivered = true }
            }
            let latestDeadline = Date().addingTimeInterval(5)
            while !newestDelivered, latestDeadline.timeIntervalSinceNow > 0 { RunLoop.current.run(until: Date().addingTimeInterval(0.01)) }
            checks["audio_preparation_latest_only_replaces_pending_delivery"] = newestDelivered && !oldDelivered
            var cancelledDelivered = false
            preparation.prepare(kind: "input", customDirectory: directory, volume: 0) { _ in cancelledDelivered = true }
            preparation.cancel()
            RunLoop.current.run(until: Date().addingTimeInterval(0.03))
            checks["audio_preparation_cancel_invalidates_queued_and_main_delivery"] = !cancelledDelivered
            do { _ = try customSource(kind: "custom:../escape.wav", directory: directory); checks["audio_preparation_rejects_custom_traversal"] = false }
            catch { checks["audio_preparation_rejects_custom_traversal"] = true }
            let custom = directory.appendingPathComponent("fixture.wav")
            try CueChime.waveData(kind: "complete").write(to: custom)
            let regular = try customSource(kind: "custom:fixture.wav", directory: directory)
            checks["audio_preparation_accepts_valid_custom_path"] = regular == custom
            let linked = directory.appendingPathComponent("linked.wav")
            try FileManager.default.createSymbolicLink(at: linked, withDestinationURL: custom)
            do { _ = try customSource(kind: "custom:linked.wav", directory: directory); checks["audio_preparation_rejects_custom_symlinks"] = false }
            catch { checks["audio_preparation_rejects_custom_symlinks"] = true }
            if case .success(let prepared) = received { prepared.player.stop() }
        } catch { checks["audio_preparation_fixture_completed"] = false }
        return checks
    }
}

struct CueChime {
    static let sampleRate = 48_000
    private static let waveCache: NSCache<NSString, NSData> = {
        let cache = NSCache<NSString, NSData>(); cache.totalCostLimit = 16 * 1024 * 1024
        return cache
    }()

    static func waveData(kind: String) -> Data {
        let identifier = CueSoundCatalog.contains(id: kind) ? kind : "error"
        if let data = waveCache.object(forKey: identifier as NSString) { return data as Data }
        // Kept for older callers. The throwing write path below reports a
        // missing recording instead of silently substituting another sound.
        return (try? resolvedWaveData(for: CueSoundCatalog.preset(id: identifier)!)) ?? Data()
    }

    fileprivate static func resolvedWaveData(for preset: CueSoundPreset) throws -> Data {
        if let data = waveCache.object(forKey: preset.id as NSString) { return data as Data }
        let data: Data
        if preset.sourceType == "recording" {
            guard let url = CueSoundCatalog.recordingURL(for: preset) else {
                throw NSError(domain: "VibeCue.Sound", code: 1, userInfo: [NSLocalizedDescriptionKey: "找不到实录音源：\(preset.name)。请重新安装完整的 VibeCoding Go。"])
            }
            data = try Data(contentsOf: url)
            guard !samples(from: data).isEmpty else {
                throw NSError(domain: "VibeCue.Sound", code: 2, userInfo: [NSLocalizedDescriptionKey: "实录音源格式无效：\(preset.name)。"])
            }
        } else if let url = preset.systemURL {
            data = try convertedSystemWaveData(from: url)
        } else {
            data = synthesizedWaveData(kind: preset.id)
        }
        waveCache.setObject(data as NSData, forKey: preset.id as NSString, cost: data.count)
        return data
    }

    private static func convertedSystemWaveData(from url: URL) throws -> Data {
        let file = try AVAudioFile(forReading: url)
        let capacity = AVAudioFrameCount(file.length)
        guard capacity > 0, Double(file.length) / file.fileFormat.sampleRate <= 30,
              let input = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: capacity),
              let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: Double(sampleRate), channels: 1, interleaved: true),
              let converter = AVAudioConverter(from: file.processingFormat, to: target),
              let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: AVAudioFrameCount(ceil(Double(capacity) * Double(sampleRate) / file.processingFormat.sampleRate)) + 256) else {
            throw NSError(domain: "VibeCue.Sound", code: 3, userInfo: [NSLocalizedDescriptionKey: "无法读取本机系统音效。"])
        }
        try file.read(into: input)
        var supplied = false, conversionError: NSError?
        converter.convert(to: output, error: &conversionError) { _, status in
            if supplied { status.pointee = .endOfStream; return nil }
            supplied = true; status.pointee = .haveData; return input
        }
        if let conversionError { throw conversionError }
        guard output.frameLength > 0, let values = output.int16ChannelData?[0] else {
            throw NSError(domain: "VibeCue.Sound", code: 4, userInfo: [NSLocalizedDescriptionKey: "本机系统音效没有可播放内容。"])
        }
        var pcm = Data()
        for index in 0..<Int(output.frameLength) { pcm.cueAppendLE(values[index]) }
        return wavData(pcm: pcm)
    }

    fileprivate static func synthesizedWaveData(kind: String) -> Data {
        let preset = CueSoundCatalog.preset(id: kind) ?? CueSoundCatalog.preset(id: "error")!
        var pcm = Data()
        var noiseState = preset.id.utf8.reduce(UInt64(1469598103934665603)) { ($0 ^ UInt64($1)) &* 1099511628211 }
        var noiseLowpass = 0.0
        for tone in preset.score {
            let count = Int(Double(sampleRate) * tone.duration)
            var phase = 0.0
            for sample in 0..<count {
                let time = Double(sample) / Double(sampleRate)
                let value: Double
                if preset.legacy {
                    let attack = min(1, time / 0.015)
                    let release = min(1, max(0, (tone.duration - time) / 0.09))
                    let envelope = attack * release * exp(-1.4 * time / tone.duration)
                    value = 0.65 * envelope * (sin(2 * .pi * tone.frequency * time) + 0.16 * sin(2 * .pi * tone.frequency * 2 * time))
                } else {
                    let progress = time / tone.duration
                    let frequency = tone.endFrequency.map { tone.frequency * pow($0 / tone.frequency, progress) } ?? tone.frequency
                    phase += 2 * .pi * frequency / Double(sampleRate)
                    let attackTime = preset.voice == .air ? 0.09 : preset.voice == .soft ? 0.025 : 0.008
                    let attack = min(1, time / attackTime)
                    let release = min(1, max(0, (tone.duration - time) / min(0.13, tone.duration * 0.35)))
                    let decay: Double
                    switch preset.voice {
                    case .wood: decay = 5.8
                    case .glass, .pluck: decay = 3.6
                    case .bell: decay = 2.6
                    case .water: decay = 2.8
                    case .rhythm: decay = 4.2
                    case .air: decay = 0.45
                    default: decay = 1.5
                    }
                    let envelope = attack * release * exp(-decay * progress)
                    let voice: Double
                    switch preset.voice {
                    case .soft: voice = sin(phase) + 0.13 * sin(phase * 2)
                    case .glass: voice = 0.82 * sin(phase) + 0.14 * sin(phase * 2.76) * exp(-progress * 5) + 0.07 * sin(phase * 4.07) * exp(-progress * 9)
                    case .wood: voice = 0.85 * sin(phase) + 0.19 * sin(phase * 3.05) * exp(-progress * 12)
                    case .synth: voice = 0.83 * sin(phase) + 0.12 * sin(phase * 2) + 0.07 * sin(phase * 3)
                    case .bell: voice = 0.77 * sin(phase) + 0.19 * sin(phase * 2.01) * exp(-progress * 2) + 0.08 * sin(phase * 3.98) * exp(-progress * 6)
                    case .water: voice = 0.88 * sin(phase + 0.5 * exp(-progress * 4) * sin(phase * 1.5))
                    case .pluck: voice = 0.75 * sin(phase) + 0.18 * sin(phase * 2) * exp(-progress * 3) + 0.09 * sin(phase * 3) * exp(-progress * 7)
                    case .air:
                        noiseState ^= noiseState << 13; noiseState ^= noiseState >> 7; noiseState ^= noiseState << 17
                        let noise = Double(noiseState & 65535) / 32767.5 - 1
                        noiseLowpass += 0.08 * (noise - noiseLowpass)
                        voice = 0.8 * sin(phase + 0.016 * sin(2 * .pi * 4 * time)) + 0.12 * sin(phase * 2) + noiseLowpass * 0.045
                    case .rhythm: voice = 0.82 * sin(phase) + 0.12 * (2 / .pi) * asin(sin(phase * 2))
                    case .orbital: voice = 0.86 * sin(phase + (0.45 * exp(-progress * 2)) * sin(phase * 2.01))
                    }
                    let harmony = tone.chord.enumerated().reduce(0.0) { result, chord in result + sin(phase * chord.element) * (chord.offset == 0 ? 0.23 : 0.14) }
                    value = 0.60 * envelope * (voice + harmony) / (tone.chord.isEmpty ? 1 : 1.18)
                }
                pcm.cueAppendLE(Int16(max(-0.98, min(0.98, value)) * Double(Int16.max)))
            }
            pcm.append(Data(count: Int(Double(sampleRate) * tone.gap) * 2))
        }
        return wavData(pcm: pcm)
    }

    private static func wavData(pcm: Data) -> Data {
        var wav = Data("RIFF".utf8)
        wav.cueAppendLE(UInt32(36 + pcm.count)); wav.append(Data("WAVEfmt ".utf8))
        wav.cueAppendLE(UInt32(16)); wav.cueAppendLE(UInt16(1)); wav.cueAppendLE(UInt16(1))
        wav.cueAppendLE(UInt32(sampleRate)); wav.cueAppendLE(UInt32(sampleRate * 2))
        wav.cueAppendLE(UInt16(2)); wav.cueAppendLE(UInt16(16)); wav.append(Data("data".utf8))
        wav.cueAppendLE(UInt32(pcm.count)); wav.append(pcm)
        return wav
    }

    fileprivate static func samples(from data: Data) -> [Int16] {
        guard data.count >= 44, String(data: data.prefix(4), encoding: .ascii) == "RIFF",
              String(data: data[8..<12], encoding: .ascii) == "WAVE" else { return [] }
        var offset = 12, validFormat = false
        while offset + 8 <= data.count {
            let id = String(data: data[offset..<(offset + 4)], encoding: .ascii)
            let length = Int(UInt32(data[offset + 4]) | UInt32(data[offset + 5]) << 8 | UInt32(data[offset + 6]) << 16 | UInt32(data[offset + 7]) << 24)
            let start = offset + 8
            guard length >= 0, length <= data.count - start else { return [] }
            if id == "fmt ", length >= 16 {
                validFormat = data[start] == 1 && data[start + 1] == 0 && data[start + 2] == 1 && data[start + 3] == 0 && data[start + 14] == 16 && data[start + 15] == 0
                let rate = UInt32(data[start + 4]) | UInt32(data[start + 5]) << 8 | UInt32(data[start + 6]) << 16 | UInt32(data[start + 7]) << 24
                validFormat = validFormat && rate == UInt32(sampleRate)
            }
            if id == "data", validFormat {
                return stride(from: start, to: start + length - 1, by: 2).map { index in
                    Int16(bitPattern: UInt16(data[index]) | UInt16(data[index + 1]) << 8)
                }
            }
            offset = start + length + length % 2
        }
        return []
    }

    static func write(kind: String, directory: URL) throws -> URL {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let identifier = CueSoundCatalog.contains(id: kind) ? kind : "error"
        guard let preset = CueSoundCatalog.preset(id: identifier) else { throw CocoaError(.fileReadNoSuchFile) }
        if let systemURL = preset.systemURL { return systemURL }
        let path = directory.appendingPathComponent(identifier + ".wav")
        try resolvedWaveData(for: preset).write(to: path, options: .atomic)
        return path
    }

    // Preserve the existing four-sound backend check. It remains SILENT and
    // never changes system output/volume or automatically plays the whole bank.
    static func selfTest(directory: URL) -> [[String: Any]] {
        ["complete", "input", "permission", "error"].map { kind in
            do {
                let path = try write(kind: kind, directory: directory)
                let player = try AVAudioPlayer(contentsOf: path)
                player.volume = 0; player.isMeteringEnabled = true
                let prepared = player.prepareToPlay(), started = player.play()
                RunLoop.current.run(until: Date().addingTimeInterval(0.12)); player.updateMeters()
                let result: [String: Any] = ["kind": kind, "audio_backend": "AVAudioPlayer", "volume": 0,
                    "path": path.path, "prepared": prepared, "play_started": started,
                    "is_playing_after_100ms": player.isPlaying, "playback_time": player.currentTime,
                    "duration": player.duration, "peak_dbfs": player.peakPower(forChannel: 0),
                    "output_device_uid": player.currentDevice ?? "system-default"]
                player.stop(); return result
            } catch { return ["kind": kind, "error": error.localizedDescription] }
        }
    }
}

private extension Data {
    mutating func cueAppendLE<T: FixedWidthInteger>(_ value: T) {
        var little = value.littleEndian
        Swift.withUnsafeBytes(of: &little) { append(contentsOf: $0) }
    }
}
