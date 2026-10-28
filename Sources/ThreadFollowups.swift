import Foundation

/// Human workflow state is independent of Codex's turn lifecycle and of
/// whether a notification has been opened. No conversation body is stored.
struct CueThreadFollowup {
    let threadID: String
    var title: String
    let createdAt: Date
    var reviewedAt: Date
    var pinned: Bool
    var unread = false
    var isPinned: Bool { pinned || unread }
    var navigationID: String { "followup:" + threadID }
    var event: CueEvent { CueEvent(runningThreadID: threadID, title: title, date: reviewedAt) }
}

final class CueThreadFollowups {
    let file: URL
    private(set) var records: [CueThreadFollowup] = []
    private(set) var lastError = ""
    init(home: URL) { file = home.appendingPathComponent("thread-followups.json"); load() }

    static func valid(_ id: String) -> Bool {
        !id.isEmpty && id.count <= 160 && id.unicodeScalars.allSatisfy {
            CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_")).contains($0)
        }
    }
    func contains(_ id: String) -> Bool { records.contains { $0.threadID == id } }
    @discardableResult func retain(threadID: String, title: String, provider: String = "codex", now: Date = Date()) -> Bool {
        guard provider == "codex", Self.valid(threadID) else { return false }
        let name = String(title.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ").prefix(180))
        if let index = records.firstIndex(where: { $0.threadID == threadID }) {
            records[index].title = name.isEmpty ? "Codex 对话" : name; records[index].reviewedAt = now
        } else {
            records.append(CueThreadFollowup(threadID: threadID, title: name.isEmpty ? "Codex 对话" : name, createdAt: now, reviewedAt: now, pinned: false))
        }
        normalize(); save(); return true
    }
    @discardableResult func markHandled(threadID: String) -> Bool {
        guard contains(threadID) else { return false }
        records.removeAll { $0.threadID == threadID }; save(); return true
    }
    func setPinned(threadID: String, pinned: Bool) {
        guard let index = records.firstIndex(where: { $0.threadID == threadID }) else { return }
        records[index].pinned = pinned; normalize(); save()
    }
    @discardableResult func setUnread(threadID: String, unread: Bool) -> Bool {
        guard let index = records.firstIndex(where: { $0.threadID == threadID }) else { return false }
        records[index].unread = unread; normalize(); save(); return true
    }
    private func normalize() {
        records.sort { a, b in
            if a.unread != b.unread { return a.unread }
            if a.isPinned != b.isPinned { return a.isPinned }
            return a.reviewedAt != b.reviewedAt ? a.reviewedAt > b.reviewedAt : a.threadID < b.threadID
        }
    }
    private func save() {
        do {
            try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            let rows: [[String: Any]] = records.map { ["thread_id": $0.threadID, "title": $0.title, "created_at": $0.createdAt.timeIntervalSince1970, "reviewed_at": $0.reviewedAt.timeIntervalSince1970, "pinned": $0.pinned, "unread": $0.unread] }
            let data = try JSONSerialization.data(withJSONObject: ["version": 1, "records": rows], options: [.sortedKeys])
            try data.write(to: file, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
            lastError = ""
        } catch { lastError = "待续记录未能保存，重启后可能无法恢复。" }
    }
    private func load() {
        guard FileManager.default.fileExists(atPath: file.path) else { return }
        do {
            let data = try Data(contentsOf: file)
            guard let value = try JSONSerialization.jsonObject(with: data) as? [String: Any], value["version"] as? Int == 1,
                  let rows = value["records"] as? [[String: Any]] else { throw CocoaError(.fileReadCorruptFile) }
            var ids = Set<String>()
            records = rows.compactMap { raw in
                guard let id = raw["thread_id"] as? String, Self.valid(id), ids.insert(id).inserted,
                      let created = raw["created_at"] as? Double, created.isFinite, created > 0,
                      let reviewed = raw["reviewed_at"] as? Double, reviewed.isFinite, reviewed > 0 else { return nil }
                return CueThreadFollowup(threadID: id, title: String((raw["title"] as? String ?? "Codex 对话").prefix(180)), createdAt: Date(timeIntervalSince1970: created), reviewedAt: Date(timeIntervalSince1970: reviewed), pinned: raw["pinned"] as? Bool == true, unread: raw["unread"] as? Bool == true)
            }
            normalize()
        } catch { lastError = "待续记录暂时无法读取，原文件已保留。" }
    }
}
