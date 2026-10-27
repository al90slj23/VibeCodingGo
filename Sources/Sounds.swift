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

struct CueSoundPreset {
    let id: String
    let name: String
    let family: String
    let description: String
    let duration: Double
    fileprivate let voice: CueSoundVoice
    fileprivate let score: [CueSoundTone]
    fileprivate let legacy: Bool

    fileprivate init(_ id: String, _ name: String, _ family: String, _ description: String,
                     _ voice: CueSoundVoice, _ score: [CueSoundTone], legacy: Bool = false) {
        self.id = id; self.name = name; self.family = family; self.description = description
        self.voice = voice; self.score = score; self.legacy = legacy
        duration = Double(score.reduce(0) { $0 + Int($1.duration * Double(CueChime.sampleRate)) + Int($1.gap * Double(CueChime.sampleRate)) }) / Double(CueChime.sampleRate)
    }
}

enum CueSoundCatalog {
    private static let waveformCache: NSCache<NSString, NSImage> = {
        let cache = NSCache<NSString, NSImage>(); cache.totalCostLimit = 8 * 1024 * 1024
        return cache
    }()
    // Each preset is an original score and synthesis recipe. No network audio
    // or third-party recordings are used. The first four preserve old releases.
    static let presets: [CueSoundPreset] = [
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

    static func preset(id: String) -> CueSoundPreset? { presets.first { $0.id == id } }
    static func contains(id: String) -> Bool { preset(id: id) != nil }
    static func displayName(id: String) -> String {
        if id == "silent" { return "静音" }
        if id.hasPrefix("custom:") { return "自选音效" }
        return preset(id: id)?.name ?? "提示音"
    }

    static func waveformImage(for preset: CueSoundPreset, size: NSSize = NSSize(width: 180, height: 44)) -> NSImage {
        let key = (preset.id + ":" + String(Int(size.width)) + "x" + String(Int(size.height))) as NSString
        if let image = waveformCache.object(forKey: key) { return image }
        let wav = CueChime.waveData(kind: preset.id)
        let samples = CueChime.samples(from: wav)
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
        for preset in presets {
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
        return ["exactly_50_sound_presets": presets.count == 50,
                "unique_sound_ids": Set(presets.map(\.id)).count == 50,
                "fifty_distinct_audio_signals": hashes.count == 50,
                "all_non_silent": nonSilent, "no_pcm_clipping": notClipped,
                "short_notification_durations": validDuration,
                "deterministic_local_synthesis": stableData,
                "legacy_ids_preserved": ["complete", "input", "permission", "error"].allSatisfy { preset(id: $0)?.legacy == true },
                "silent_is_separate": !contains(id: "silent")]
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
        let data = synthesizedWaveData(kind: identifier)
        waveCache.setObject(data as NSData, forKey: identifier as NSString, cost: data.count)
        return data
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
        var wav = Data("RIFF".utf8)
        wav.cueAppendLE(UInt32(36 + pcm.count)); wav.append(Data("WAVEfmt ".utf8))
        wav.cueAppendLE(UInt32(16)); wav.cueAppendLE(UInt16(1)); wav.cueAppendLE(UInt16(1))
        wav.cueAppendLE(UInt32(sampleRate)); wav.cueAppendLE(UInt32(sampleRate * 2))
        wav.cueAppendLE(UInt16(2)); wav.cueAppendLE(UInt16(16)); wav.append(Data("data".utf8))
        wav.cueAppendLE(UInt32(pcm.count)); wav.append(pcm)
        return wav
    }

    fileprivate static func samples(from data: Data) -> [Int16] {
        guard data.count >= 44 else { return [] }
        return stride(from: 44, to: data.count - 1, by: 2).map { offset in
            Int16(bitPattern: UInt16(data[offset]) | UInt16(data[offset + 1]) << 8)
        }
    }

    static func write(kind: String, directory: URL) throws -> URL {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let identifier = CueSoundCatalog.contains(id: kind) ? kind : "error"
        let path = directory.appendingPathComponent(identifier + ".wav")
        try waveData(kind: identifier).write(to: path, options: .atomic)
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
