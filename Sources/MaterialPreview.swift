import AppKit

/// A close view of the same rendered light field, rather than a different
/// illustration. Full-screen preview remains the motion/scale reference.
final class CueMaterialPreviewView: NSView {
    let style: CueVisualStyle
    let opacity: Double
    var latest: NSImage?
    init(frame: NSRect, style: CueVisualStyle, opacity: Double) {
        self.style = style; self.opacity = opacity
        super.init(frame: frame); advance(to: 1.35)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override var isOpaque: Bool { false }
    func advance(to time: TimeInterval) {
        latest = Self.image(style: style, size: bounds.size, time: time, opacity: opacity)
        needsDisplay = true
    }
    override func draw(_ dirtyRect: NSRect) {
        latest?.draw(in: bounds, from: .zero, operation: .sourceOver, fraction: 1)
    }
    private static func bitmap(size: NSSize) -> NSBitmapImageRep? {
        guard size.width > 8, size.height > 8 else { return nil }
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(ceil(size.width)), pixelsHigh: Int(ceil(size.height)), bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 32)
        rep?.size = size
        return rep
    }
    private static func draw(in bitmap: NSBitmapImageRep, _ body: () -> Void) {
        guard let context = NSGraphicsContext(bitmapImageRep: bitmap) else { return }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = context
        defer { NSGraphicsContext.restoreGraphicsState() }
        body()
    }
    static func image(style: CueVisualStyle, size: NSSize, time: TimeInterval, opacity: Double = 1) -> NSImage {
        let image = NSImage(size: size)
        guard let output = bitmap(size: size) else { return image }
        let rep = CueEdgeEffectView.bitmap(style: style, size: NSSize(width: 720, height: 360), time: time, background: false)
        guard let pixels = rep.bitmapData else { return image }
        let alphaFirst = rep.bitmapFormat.contains(.alphaFirst)
        let unpremultiplied = rep.bitmapFormat.contains(.alphaNonpremultiplied)
        var strongest = -1.0, peakX = rep.pixelsWide / 2, peakY = 16
        for y in stride(from: 0, to: rep.pixelsHigh, by: 2) {
            for x in stride(from: 0, to: rep.pixelsWide, by: 2) {
                let offset = y * rep.bytesPerRow + x * 4
                let alpha = Double(pixels[offset + (alphaFirst ? 0 : 3)]) / 255
                guard alpha > 0 else { continue }
                let first = offset + (alphaFirst ? 1 : 0)
                let light = (Double(pixels[first]) * 0.2126 + Double(pixels[first + 1]) * 0.7152 + Double(pixels[first + 2]) * 0.0722) * (unpremultiplied ? alpha : 1)
                if light > strongest { strongest = light; peakX = x; peakY = y }
            }
        }
        // Keep the light centred even when its source pixels touch a screen
        // edge. Any area outside the real source is transparent padding.
        let cropWidth: CGFloat = 336, cropHeight: CGFloat = 160
        let requested = CGRect(x: CGFloat(peakX) - cropWidth / 2, y: CGFloat(peakY) - cropHeight / 2, width: cropWidth, height: cropHeight)
        let crop = requested.intersection(CGRect(x: 0, y: 0, width: rep.pixelsWide, height: rep.pixelsHigh))
        guard !crop.isEmpty, let cg = rep.cgImage?.cropping(to: crop) else { return image }
        let detail = NSImage(cgImage: cg, size: crop.size)
        let scale = min((size.width - 8) / cropWidth, (size.height - 8) / cropHeight)
        let target = NSSize(width: cropWidth * scale, height: cropHeight * scale)
        let origin = NSPoint(x: (size.width - target.width) / 2, y: (size.height - target.height) / 2)
        let destination = NSRect(x: origin.x + (crop.minX - requested.minX) * scale,
                                 y: origin.y + (cropHeight - (crop.minY - requested.minY) - crop.height) * scale,
                                 width: crop.width * scale, height: crop.height * scale)
        draw(in: output) {
            NSColor.clear.setFill(); NSRect(origin: .zero, size: size).fill(using: .copy)
            detail.draw(in: destination, from: .zero, operation: .sourceOver, fraction: opacity)
        }
        image.addRepresentation(output)
        return image
    }
    static func renderContactSheet(to url: URL) throws -> [String: Bool] {
        let size = NSSize(width: 960, height: 440)
        guard let output = bitmap(size: size) else { throw CocoaError(.fileWriteUnknown) }
        var results: [String: Bool] = [:]
        // Finish source renders before starting the sheet context. No nested
        // NSImage focus/cache representations can replace the sheet's pixels.
        let details = CueEffectCatalog.recommendedPresets.map { preset -> (CueEffectPreset, NSImage) in
            var fields = CueSettings.defaultStyles["complete"] ?? [:]
            fields["effect"] = preset.id; fields["palette"] = preset.defaultPalette.joined(separator: ",")
            return (preset, image(style: CueVisualStyle(fields: fields, fallback: .systemBlue), size: NSSize(width: 300, height: 180), time: 1.35, opacity: 0.85))
        }
        draw(in: output) {
            NSColor(srgbRed: 0.055, green: 0.065, blue: 0.075, alpha: 1).setFill(); NSRect(origin: .zero, size: size).fill()
            for (index, entry) in details.enumerated() {
                let x = CGFloat(index) * 320 + 10
                entry.1.draw(in: NSRect(x: x, y: 205, width: 300, height: 180))
                NSColor(srgbRed: 0.94, green: 0.95, blue: 0.96, alpha: 1).setFill(); NSRect(x: x, y: 10, width: 300, height: 180).fill()
                entry.1.draw(in: NSRect(x: x, y: 10, width: 300, height: 180))
                entry.0.name.draw(in: NSRect(x: x + 8, y: 403, width: 284, height: 24), withAttributes: [.font: NSFont.systemFont(ofSize: 15, weight: .medium), .foregroundColor: NSColor.white])
            }
        }
        for (preset, detail) in details {
            let rep = detail.representations.compactMap { $0 as? NSBitmapImageRep }.first
            var peak = CGFloat(0), centrePeak = CGFloat(0)
            if let rep {
                for y in stride(from: 0, to: rep.pixelsHigh, by: 2) {
                    for x in stride(from: 0, to: rep.pixelsWide, by: 2) {
                        let alpha = rep.colorAt(x: x, y: y)?.alphaComponent ?? 0
                        peak = max(peak, alpha)
                        if abs(x - rep.pixelsWide / 2) < rep.pixelsWide / 6 && abs(y - rep.pixelsHigh / 2) < rep.pixelsHigh / 6 { centrePeak = max(centrePeak, alpha) }
                    }
                }
            }
            results[preset.id + "_detail_contains_light"] = peak > 0.15
            results[preset.id + "_detail_centred"] = centrePeak > 0.15
        }
        var lightPixels = 0
        for y in stride(from: 0, to: output.pixelsHigh, by: 4) {
            for x in stride(from: 0, to: output.pixelsWide, by: 4) {
                if let color = output.colorAt(x: x, y: y)?.usingColorSpace(.sRGB), color.redComponent > 0.9 && color.greenComponent > 0.9 && color.blueComponent > 0.9 { lightPixels += 1 }
            }
        }
        results["contact_sheet_light_panels_drawn"] = lightPixels > 5000
        guard let png = output.representation(using: .png, properties: [:]) else { throw CocoaError(.fileWriteUnknown) }
        try png.write(to: url)
        return results
    }
}
