import AppKit

/// Small, offline Markdown presentation for a selectable NSTextView. Blocks are
/// laid out explicitly, so paragraph/list/code newlines never depend on SwiftUI.
enum CueHoverMarkdown {
    private static let tokens = try! NSRegularExpression(pattern: #"(?<!\\)(?:(`+)([^`\n]+)\1|\[([^\]\n]+)\]\(([^)\s]+)\)|\*\*([^\n]+?)\*\*|__([^\n]+?)__|(?<!\*)\*([^*\n]+)\*(?!\*)|(?<!_)_([^_\n]+)_(?!_))"#)
    private static func style(_ font: NSFont, color: NSColor = .labelColor, indent: CGFloat = 0) -> [NSAttributedString.Key: Any] {
        let paragraph = NSMutableParagraphStyle(); paragraph.lineSpacing = 3; paragraph.paragraphSpacing = 4; paragraph.headIndent = indent
        return [.font: font, .foregroundColor: color, .paragraphStyle: paragraph]
    }
    private static func plain(_ value: String) -> String {
        value.replacingOccurrences(of: #"\\([\\`*_{}\[\]()#+.!>\-])"#, with: "$1", options: .regularExpression)
    }
    private static func inline(_ value: String, font: NSFont, color: NSColor = .labelColor, indent: CGFloat = 0, depth: Int = 0) -> NSAttributedString {
        let output = NSMutableAttributedString(); let source = value as NSString
        let attributes = style(font, color: color, indent: indent)
        guard depth < 6 else { return NSAttributedString(string: value, attributes: attributes) }
        var cursor = 0
        for match in tokens.matches(in: value, range: NSRange(location: 0, length: source.length)) {
            if match.range.location > cursor { output.append(NSAttributedString(string: plain(source.substring(with: NSRange(location: cursor, length: match.range.location - cursor))), attributes: attributes)) }
            func group(_ index: Int) -> String? { let range = match.range(at: index); return range.location == NSNotFound ? nil : source.substring(with: range) }
            if let code = group(2) {
                var codeStyle = attributes; codeStyle[.font] = NSFont.monospacedSystemFont(ofSize: max(11, font.pointSize - 1), weight: .regular); codeStyle[.backgroundColor] = NSColor.quaternaryLabelColor.withAlphaComponent(0.10)
                output.append(NSAttributedString(string: code, attributes: codeStyle))
            } else if let label = group(3), let destination = group(4) {
                let labelText = NSMutableAttributedString(attributedString: inline(label, font: font, color: color, indent: indent, depth: depth + 1))
                if let url = URL(string: destination), ["https", "http"].contains(url.scheme?.lowercased() ?? "") {
                    labelText.addAttributes([.link: url, .foregroundColor: NSColor.linkColor], range: NSRange(location: 0, length: labelText.length))
                }
                output.append(labelText)
            } else if let strong = group(5) ?? group(6) {
                output.append(inline(strong, font: NSFontManager.shared.convert(font, toHaveTrait: .boldFontMask), color: color, indent: indent, depth: depth + 1))
            } else if let emphasis = group(7) ?? group(8) {
                output.append(inline(emphasis, font: NSFontManager.shared.convert(font, toHaveTrait: .italicFontMask), color: color, indent: indent, depth: depth + 1))
            }
            cursor = NSMaxRange(match.range)
        }
        if cursor < source.length { output.append(NSAttributedString(string: plain(source.substring(from: cursor)), attributes: attributes)) }
        return output
    }
    static func render(_ markdown: String) -> NSAttributedString {
        let output = NSMutableAttributedString(); var fence: Character?; var fenceLength = 0
        let base = NSFont.systemFont(ofSize: 13)
        for line in markdown.components(separatedBy: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            let opening = trimmed.prefix(while: { $0 == "`" || $0 == "~" })
            if let first = opening.first, opening.count >= 3, opening.allSatisfy({ $0 == first }) {
                if fence == nil { fence = first; fenceLength = opening.count; continue }
                if fence == first, opening.count >= fenceLength, trimmed.dropFirst(opening.count).trimmingCharacters(in: .whitespaces).isEmpty { fence = nil; continue }
            }
            if fence != nil {
                var attributes = style(NSFont.monospacedSystemFont(ofSize: 11.5, weight: .regular))
                attributes[.backgroundColor] = NSColor.quaternaryLabelColor.withAlphaComponent(0.09)
                output.append(NSAttributedString(string: line + "\n", attributes: attributes)); continue
            }
            if let range = line.range(of: #"^ {0,3}#{1,6}\s+"#, options: .regularExpression) {
                let level = line[range].filter { $0 == "#" }.count
                output.append(inline(String(line[range.upperBound...]), font: .systemFont(ofSize: level <= 2 ? 15 : 13.5, weight: .semibold)))
            } else if let range = line.range(of: #"^\s{0,8}[-+*]\s+"#, options: .regularExpression) {
                output.append(NSAttributedString(string: "• ", attributes: style(base, indent: 12)))
                output.append(inline(String(line[range.upperBound...]), font: base, indent: 12))
            } else if let range = line.range(of: #"^ {0,3}>\s?"#, options: .regularExpression) {
                output.append(NSAttributedString(string: "│ ", attributes: style(base, color: .secondaryLabelColor, indent: 12)))
                output.append(inline(String(line[range.upperBound...]), font: base, color: .secondaryLabelColor, indent: 12))
            } else { output.append(inline(line, font: base)) }
            output.append(NSAttributedString(string: "\n", attributes: style(base)))
        }
        return output
    }
    static func validationReport() -> [String: Bool] {
        let rendered = render("## 标题\n\n- **加粗** 与 *强调*，`let x = 1`\n\n```json\n{\"questionItemId\":\"literal example\"}\n```\n[文档](https://example.com)\n[危险](javascript:alert(1))")
        func font(_ word: String) -> NSFont? { let range = (rendered.string as NSString).range(of: word); return range.location == NSNotFound ? nil : rendered.attribute(.font, at: range.location, effectiveRange: nil) as? NSFont }
        let link = (rendered.string as NSString).range(of: "文档")
        let unsafe = (rendered.string as NSString).range(of: "危险")
        return ["hover_markdown_keeps_heading_list_and_code_lines": rendered.string.hasPrefix("标题\n\n• ") && rendered.string.contains("\n{\"questionItemId\":\"literal example\"}\n") && !rendered.string.contains("```"),
                "hover_markdown_maps_emphasis_and_code_to_native_fonts": font("加粗")?.fontDescriptor.symbolicTraits.contains(.bold) == true && font("强调")?.fontDescriptor.symbolicTraits.contains(.italic) == true && font("let x")?.fontDescriptor.symbolicTraits.contains(.monoSpace) == true,
                "hover_markdown_safe_links_only": rendered.attribute(.link, at: link.location, effectiveRange: nil) != nil && rendered.attribute(.link, at: unsafe.location, effectiveRange: nil) == nil,
                "hover_markdown_literal_html_never_executes": render("<script>literal</script>").string == "<script>literal</script>\n"]
    }
}
