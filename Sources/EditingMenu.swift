import AppKit
import WebKit

/// LSUIElement apps still need a real Edit menu for the AppKit responder chain.
enum CueEditingMenu {
    static func install() {
        let main = NSApp.mainMenu ?? NSMenu()
        guard !main.items.contains(where: { $0.identifier?.rawValue == "vibecue.edit-menu" }) else { return }
        if main.items.isEmpty {
            let application = NSMenuItem(title: "VibeCoding Go", action: nil, keyEquivalent: "")
            let menu = NSMenu(title: "VibeCoding Go")
            menu.addItem(withTitle: "隐藏 VibeCoding Go", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
            menu.addItem(.separator())
            menu.addItem(withTitle: "退出 VibeCoding Go", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
            application.submenu = menu; main.addItem(application)
        }
        let item = NSMenuItem(title: "编辑", action: nil, keyEquivalent: "")
        item.identifier = NSUserInterfaceItemIdentifier("vibecue.edit-menu")
        let edit = NSMenu(title: "编辑")
        func add(_ title: String, _ action: Selector, _ key: String, _ modifiers: NSEvent.ModifierFlags = .command) {
            let row = NSMenuItem(title: title, action: action, keyEquivalent: key)
            row.keyEquivalentModifierMask = modifiers; row.target = nil
            edit.addItem(row)
        }
        add("撤销", Selector(("undo:")), "z")
        add("重做", Selector(("redo:")), "z", [.command, .shift])
        edit.addItem(.separator())
        add("剪切", #selector(NSText.cut(_:)), "x")
        add("复制", #selector(NSText.copy(_:)), "c")
        add("粘贴", #selector(NSText.paste(_:)), "v")
        add("粘贴并匹配样式", #selector(NSTextView.pasteAsPlainText(_:)), "v", [.command, .option, .shift])
        add("全选", #selector(NSText.selectAll(_:)), "a")
        item.submenu = edit; main.addItem(item); NSApp.mainMenu = main
    }

    static func editingAction(_ event: NSEvent) -> Selector? {
        guard event.type == .keyDown, let key = event.charactersIgnoringModifiers?.lowercased() else { return nil }
        let modifiers = event.modifierFlags.intersection([.command, .shift, .option, .control])
        if modifiers == [.command, .shift], key == "z" { return Selector(("redo:")) }
        if modifiers == [.command, .option, .shift], key == "v" { return #selector(NSTextView.pasteAsPlainText(_:)) }
        guard modifiers == .command else { return nil }
        return ["x": #selector(NSText.cut(_:)), "c": #selector(NSText.copy(_:)), "v": #selector(NSText.paste(_:)),
                "a": #selector(NSText.selectAll(_:)), "z": Selector(("undo:"))][key]
    }

    static func validationReport() -> [String: Bool] {
        let oldMenu = NSApp.mainMenu
        install()
        let pasteboard = NSPasteboard.general
        let saved = pasteboard.pasteboardItems?.map { item -> NSPasteboardItem in
            let copy = NSPasteboardItem()
            for type in item.types { if let data = item.data(forType: type) { copy.setData(data, forType: type) } }
            return copy
        } ?? []
        var lastChange = pasteboard.changeCount
        defer {
            // Do not overwrite a newer copy made by the person during a test.
            if pasteboard.changeCount == lastChange { pasteboard.clearContents(); if !saved.isEmpty { pasteboard.writeObjects(saved) } }
            NSApp.mainMenu = oldMenu
        }
        let window = CueConversationWindow(contentRect: NSRect(x: -10000, y: -10000, width: 400, height: 160), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        let editor = NSTextView(frame: NSRect(x: 0, y: 0, width: 400, height: 160))
        editor.isRichText = false; editor.isEditable = true; editor.allowsUndo = true
        editor.string = "VibeCoding Go 剪贴板回归"
        window.contentView = editor; window.orderFrontRegardless(); window.makeKey(); window.makeFirstResponder(editor)
        defer { window.orderOut(nil) }
        func key(_ value: String, modifiers: NSEvent.ModifierFlags = .command) -> NSEvent {
            NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: modifiers, timestamp: 0, windowNumber: window.windowNumber, context: nil, characters: value, charactersIgnoringModifiers: value, isARepeat: false, keyCode: 0)!
        }
        var results: [String: Bool] = [:]
        editor.setSelectedRange(NSRange(location: 0, length: (editor.string as NSString).length))
        let copied = window.performKeyEquivalent(with: key("c")); lastChange = pasteboard.changeCount
        results["editing_command_copy_uses_native_responder_selection"] = copied && pasteboard.string(forType: .string) == "VibeCoding Go 剪贴板回归"
        let cut = window.performKeyEquivalent(with: key("x")); lastChange = pasteboard.changeCount
        results["editing_command_cut_removes_only_selected_input"] = cut && editor.string.isEmpty && pasteboard.string(forType: .string) == "VibeCoding Go 剪贴板回归"
        let pasted = window.performKeyEquivalent(with: key("v")); lastChange = pasteboard.changeCount
        results["editing_command_paste_inserts_clipboard_as_plain_text"] = pasted && editor.string == "VibeCoding Go 剪贴板回归" && !editor.isRichText
        editor.setSelectedRange(NSRange(location: 0, length: 0))
        results["editing_command_select_all_targets_focused_editor"] = window.performKeyEquivalent(with: key("a")) && editor.selectedRange().length == (editor.string as NSString).length
        results["editing_menu_keeps_undo_redo_and_paste_matching_style"] = NSApp.mainMenu?.items.first(where: { $0.identifier?.rawValue == "vibecue.edit-menu" })?.submenu?.items.filter { $0.action != nil }.count == 7
        results["editing_shortcuts_leave_send_and_ime_keys_alone"] = editingAction(key("\r")) == nil && editingAction(key("c", modifiers: [.control])) == nil && editingAction(key("c", modifiers: [])) == nil
        let configuration = WKWebViewConfiguration(); configuration.websiteDataStore = .nonPersistent()
        let web = WKWebView(frame: editor.frame, configuration: configuration)
        window.contentView = web; web.loadHTMLString("<p>VibeCoding Go WebKit copy test</p>", baseURL: nil)
        var ready = false, checking = false
        let loadDeadline = Date().addingTimeInterval(8)
        while !ready && Date() < loadDeadline {
            if !checking {
                checking = true
                web.evaluateJavaScript("document.readyState === 'complete' && document.body.textContent.includes('VibeCoding Go WebKit copy test')") { value, _ in ready = value as? Bool == true; checking = false }
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.02))
        }
        window.makeFirstResponder(web)
        var selected = false
        if ready {
            web.evaluateJavaScript("const r=document.createRange();r.selectNodeContents(document.querySelector('p'));const s=window.getSelection();s.removeAllRanges();s.addRange(r);s.toString()") { value, _ in selected = value as? String == "VibeCoding Go WebKit copy test" }
            while !selected && Date() < loadDeadline { RunLoop.current.run(until: Date().addingTimeInterval(0.02)) }
        }
        let webCopy = selected && window.performKeyEquivalent(with: key("c"))
        let copyDeadline = Date().addingTimeInterval(2)
        while webCopy && pasteboard.string(forType: .string)?.trimmingCharacters(in: .whitespacesAndNewlines) != "VibeCoding Go WebKit copy test" && Date() < copyDeadline { RunLoop.current.run(until: Date().addingTimeInterval(0.02)) }
        results["editing_command_copy_works_for_readonly_webkit_transcript"] = webCopy && pasteboard.string(forType: .string)?.trimmingCharacters(in: .whitespacesAndNewlines) == "VibeCoding Go WebKit copy test"
        if results["editing_command_copy_works_for_readonly_webkit_transcript"] == true { lastChange = pasteboard.changeCount }
        return results
    }
}

final class CueConversationWindow: NSWindow {
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        if let action = CueEditingMenu.editingAction(event), firstResponder?.tryToPerform(action, with: self) == true { return true }
        return super.performKeyEquivalent(with: event)
    }
}
