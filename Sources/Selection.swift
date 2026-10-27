import Foundation

/// Resolve once when an alert is shown, then reuse `fields` for that alert's
/// windows, redraws and sound. The persisted settings retain their random values.
struct CueResolvedSelection {
    static let randomToken = "random"
    let fields: [String: String]

    init(fields: [String: String], choose: (([String]) -> String)? = nil) {
        var resolved = fields
        for (key, allIdentifiers) in Self.catalogs where fields[key] == Self.randomToken {
            let identifiers = Self.pool(key: key, fields: fields, fallback: allIdentifiers)
            guard let fallback = identifiers.first else { continue }
            let selected = choose?(identifiers) ?? identifiers.randomElement() ?? fallback
            // Injected selectors are useful in tests, but cannot produce an
            // unsupported preset or another unresolved random value.
            resolved[key] = identifiers.contains(selected) ? selected : fallback
        }
        self.fields = resolved
    }

    private static func pool(key: String, fields: [String: String], fallback: [String]) -> [String] {
        let tags = Set((fields[key + "_random_tags"] ?? "").split(separator: ",").map { value in
            key == "effect" && value == "subtle" ? "recommended" : key == "effect" && value == "vivid" ? "classic" : String(value)
        })
        guard !tags.isEmpty, !tags.contains("all") else { return fallback }
        let ids: [String]
        switch key {
        case "effect": ids = CueEffectCatalog.presets.filter { !tags.isDisjoint(with: $0.tagIDs) }.map(\.id)
        case "card_style": ids = CueCardCatalog.presets.filter { !tags.isDisjoint(with: $0.tags) }.map(\.id)
        default: ids = CueSoundCatalog.presets.filter { !tags.isDisjoint(with: $0.tags) }.map(\.id)
        }
        return ids.isEmpty ? fallback : ids
    }

    private static var catalogs: [(String, [String])] {
        [("effect", CueEffectCatalog.presets.map { $0.id }),
         ("card_style", CueCardCatalog.presets.map { $0.id }),
         ("sound", CueSoundCatalog.presets.map { $0.id })]
    }

    static func validationReport() -> [String: Bool] {
        let catalogs = Self.catalogs
        let randomFields = Dictionary(uniqueKeysWithValues: catalogs.map { ($0.0, randomToken) })
        var calls: [[String]] = []
        let selection = CueResolvedSelection(fields: randomFields) { identifiers in
            calls.append(identifiers)
            return identifiers.last ?? ""
        }
        let independent = calls.count == catalogs.count && zip(calls, catalogs).allSatisfy { observed, catalog in
            observed == catalog.1 && selection.fields[catalog.0] == catalog.1.last
        }
        let snapshot = selection.fields
        let stable = selection.fields == snapshot && selection.fields == snapshot && calls.count == 3
        let valid = catalogs.allSatisfy { key, identifiers in
            selection.fields[key].map { identifiers.contains($0) } == true
        }

        var individual = true
        for (key, identifiers) in catalogs {
            var input = ["effect": "glow-breathe", "card_style": "glass-dark", "sound": "input"]
            input[key] = randomToken
            var individualCalls: [[String]] = []
            let resolved = CueResolvedSelection(fields: input) { candidates in
                individualCalls.append(candidates)
                return candidates.first ?? ""
            }
            var expected = input
            expected[key] = identifiers.first
            individual = individual && resolved.fields == expected && individualCalls == [identifiers]
        }

        let legacy = ["color": "#3BE8AB", "sound": "input"]
        var legacyChooseCalled = false
        let legacySelection = CueResolvedSelection(fields: legacy) { _ in
            legacyChooseCalled = true
            return "unexpected"
        }
        let passthrough = ["effect": "glow-breathe", "card_style": "glass-dark", "sound": "custom:owned.wav",
                           "color": "#FFC44D", "future_field": "preserve-me"]
        let silent = ["sound": "silent"]
        let legacyPreserved = legacySelection.fields == legacy && !legacyChooseCalled &&
            CueResolvedSelection(fields: passthrough).fields == passthrough &&
            CueResolvedSelection(fields: silent).fields == silent &&
            CueResolvedSelection(fields: [:]).fields.isEmpty

        let custom = ["palette": "#FF0000,#00FF00,#0000FF", "color": "#FF0000", "direction": "pingpong",
                      "speed": "2.4000", "width": "18", "inset": "8", "trail": "0.35",
                      "card_position": "bottom-right", "card_opacity": "0.68", "card_width": "620",
                      "text_scale": "1.15", "card_message": "false", "future_field": "preserve-me"]
        let customizedInput = randomFields.merging(custom) { _, custom in custom }
        let customized = CueResolvedSelection(fields: customizedInput) { $0.first ?? "" }
        let parametersPreserved = custom.allSatisfy { customized.fields[$0.key] == $0.value } &&
            customizedInput.filter { custom[$0.key] != nil } == custom &&
            randomFields.values.allSatisfy { $0 == randomToken }

        var covered: [String: Set<String>] = [:]
        var coverageValid = true
        for index in 0..<(catalogs.map { $0.1.count }.max() ?? 0) {
            let resolved = CueResolvedSelection(fields: randomFields) { identifiers in
                identifiers.isEmpty ? "" : identifiers[index % identifiers.count]
            }
            for (key, identifiers) in catalogs {
                guard let value = resolved.fields[key], identifiers.contains(value) else {
                    coverageValid = false
                    continue
                }
                covered[key, default: []].insert(value)
            }
        }
        let allFifty = coverageValid && catalogs.allSatisfy { key, identifiers in
            !identifiers.isEmpty && Set(identifiers).count == identifiers.count && covered[key] == Set(identifiers)
        }
        let invalidChoice = CueResolvedSelection(fields: randomFields) { _ in "not-a-preset" }
        let safeFallback = catalogs.allSatisfy { key, identifiers in invalidChoice.fields[key] == identifiers.first }
        let first = CueResolvedSelection(fields: randomFields) { $0.first ?? "" }
        let last = CueResolvedSelection(fields: randomFields) { $0.last ?? "" }
        let perAlert = catalogs.allSatisfy { key, _ in first.fields[key] != last.fields[key] } &&
            randomFields.values.allSatisfy { $0 == randomToken }

        let scopedFields = randomFields.merging(["effect_random_tags": "recommended", "card_style_random_tags": "native", "sound_random_tags": "nature"]) { _, new in new }
        var pools: [[String]] = []
        let scoped = CueResolvedSelection(fields: scopedFields) { ids in pools.append(ids); return ids.last ?? "" }
        let scopedCorrect = pools.count == 3 && pools[0] == CueEffectCatalog.presets.filter { $0.tagIDs.contains("recommended") }.map(\.id) && pools[1] == CueCardCatalog.presets.filter { $0.tags.contains("native") }.map(\.id) && pools[2] == CueSoundCatalog.presets.filter { $0.tags.contains("nature") }.map(\.id) && scoped.fields["sound"]?.hasPrefix("nature-") == true
        return ["random_selection_respects_tag_scope": scopedCorrect,
                "random_selections_independent": independent && individual,
                "random_selection_ids_valid": valid,
                "random_selection_snapshot_stable": stable,
                "random_selection_legacy_preserved": legacyPreserved,
                "random_selection_parameters_preserved": parametersPreserved,
                "random_selection_full_catalog_covered": allFifty,
                "random_selection_invalid_choice_safe": safeFallback,
                "random_selection_per_alert_resolution": perAlert]
    }
}
