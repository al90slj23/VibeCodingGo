#!/usr/bin/swift
// Rebuild VibeCue's original code-drawn macOS icon. No remote artwork or fonts.
// Run: CLANG_MODULE_CACHE_PATH=/tmp/vibecue-swift-cache swift scripts/generate-icon.swift
import AppKit
import Foundation

enum IconError: Error { case bitmap, png, iconutil(Int32) }

let fileManager = FileManager.default
let project = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
let output = project.appendingPathComponent("assets/AppIcon.icns")
let temporary = fileManager.temporaryDirectory.appendingPathComponent("VibeCueIcon-" + UUID().uuidString, isDirectory: true)
let iconset = temporary.appendingPathComponent("AppIcon.iconset", isDirectory: true)
try fileManager.createDirectory(at: iconset, withIntermediateDirectories: true)

func color(_ red: CGFloat, _ green: CGFloat, _ blue: CGFloat, _ alpha: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: red, green: green, blue: blue, alpha: alpha)
}

func stroke(_ path: NSBezierPath, width: CGFloat, color: NSColor) {
    path.lineWidth = width
    path.lineCapStyle = .round
    path.lineJoinStyle = .round
    color.setStroke()
    path.stroke()
}

func render(size: Int) throws -> Data {
    guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0),
        let context = NSGraphicsContext(bitmapImageRep: bitmap) else { throw IconError.bitmap }
    NSGraphicsContext.saveGraphicsState()
    defer { NSGraphicsContext.restoreGraphicsState() }
    NSGraphicsContext.current = context
    context.imageInterpolation = .high
    context.cgContext.scaleBy(x: CGFloat(size) / 1024, y: CGFloat(size) / 1024)
    context.cgContext.setAllowsAntialiasing(true)

    let tileRect = NSRect(x: 78, y: 78, width: 868, height: 868)
    let tile = NSBezierPath(roundedRect: tileRect, xRadius: 190, yRadius: 190)
    NSGraphicsContext.saveGraphicsState()
    let shadow = NSShadow()
    shadow.shadowColor = color(0.01, 0.025, 0.03, 0.28)
    shadow.shadowBlurRadius = 25
    shadow.shadowOffset = NSSize(width: 0, height: -12)
    shadow.set()
    color(0.075, 0.10, 0.12).setFill()
    tile.fill()
    NSGraphicsContext.restoreGraphicsState()

    NSGraphicsContext.saveGraphicsState()
    tile.addClip()
    NSGradient(starting: color(0.16, 0.205, 0.22), ending: color(0.045, 0.075, 0.09))!
        .draw(in: tileRect, angle: -75)
    // A restrained teal aura gives the cue a light source without losing contrast.
    NSGradient(colors: [color(0.16, 0.64, 0.57, 0.17), color(0.10, 0.26, 0.25, 0)])!
        .draw(fromCenter: NSPoint(x: 600, y: 625), radius: 0,
              toCenter: NSPoint(x: 570, y: 540), radius: 490, options: [.drawsAfterEndingLocation])
    NSGraphicsContext.restoreGraphicsState()
    stroke(NSBezierPath(roundedRect: tileRect.insetBy(dx: 1.5, dy: 1.5), xRadius: 189, yRadius: 189),
           width: 3, color: color(0.77, 0.94, 0.93, 0.13))

    // The V is also a completion cue. Two separate arcs communicate a signal.
    let vee = NSBezierPath()
    vee.move(to: NSPoint(x: 292, y: 651))
    vee.line(to: NSPoint(x: 470, y: 350))
    vee.line(to: NSPoint(x: 648, y: 651))
    NSGraphicsContext.saveGraphicsState()
    let glow = NSShadow()
    glow.shadowColor = color(0.12, 0.89, 0.73, 0.20)
    glow.shadowBlurRadius = 28
    glow.shadowOffset = .zero
    glow.set()
    stroke(vee, width: 75, color: color(0.21, 0.93, 0.77))
    NSGraphicsContext.restoreGraphicsState()
    stroke(vee, width: 67, color: color(0.33, 0.97, 0.82))

    let innerCue = NSBezierPath()
    innerCue.move(to: NSPoint(x: 658, y: 751))
    innerCue.curve(to: NSPoint(x: 751, y: 658), controlPoint1: NSPoint(x: 709, y: 751),
                   controlPoint2: NSPoint(x: 751, y: 709))
    stroke(innerCue, width: 36, color: color(0.28, 0.83, 0.74))
    let outerCue = NSBezierPath()
    outerCue.move(to: NSPoint(x: 658, y: 819))
    outerCue.curve(to: NSPoint(x: 819, y: 658), controlPoint1: NSPoint(x: 747, y: 819),
                   controlPoint2: NSPoint(x: 819, y: 747))
    stroke(outerCue, width: 30, color: color(0.30, 0.66, 0.63))

    guard let png = bitmap.representation(using: .png, properties: [:]) else { throw IconError.png }
    return png
}

for points in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let suffix = scale == 2 ? "@2x" : ""
        let filename = "icon_\(points)x\(points)\(suffix).png"
        try render(size: points * scale).write(to: iconset.appendingPathComponent(filename))
    }
}
let preview = temporary.appendingPathComponent("AppIcon-1024.png")
try render(size: 1024).write(to: preview)
try fileManager.createDirectory(at: output.deletingLastPathComponent(), withIntermediateDirectories: true)
let process = Process()
process.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
process.arguments = ["-c", "icns", iconset.path, "-o", output.path]
try process.run()
process.waitUntilExit()
guard process.terminationStatus == 0 else { throw IconError.iconutil(process.terminationStatus) }
print("Generated: \(output.path)")
print("Preview: \(preview.path)")
