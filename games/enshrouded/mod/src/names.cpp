#include "names.h"

#include <cctype>
#include <cstring>
#include <string>
#include <vector>

namespace {

std::string Lower(const std::string& s) {
    std::string out = s;
    for (char& c : out) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return out;
}

bool In(const char* const* list, size_t count, const std::string& lowered) {
    for (size_t i = 0; i < count; i++)
        if (lowered == list[i]) return true;
    return false;
}

// Tokens kept whole rather than camel-split: either a technical word that a camel split
// would shatter (8kMapLabel -> "8k Map Label") or one that is dropped anyway.
const char* const kAtomic[] = {"8kmaplabel", "maplabel",  "mapmarker", "placement", "helper",
                               "deprecated", "projectilespawner", "noui", "healthbar", "ag2",
                               "old",        "original",  "backup",    "variation",  "lore",
                               "townsfolk",  "notfinished", "donotuse"};

// Dropped while they lead, in order, never below one remaining token: what kind of thing
// the template is, which is what the type, family and faction fields already say.
const char* const kLeading[] = {"8kmaplabel", "maplabel", "mapmarker", "placement", "helper",
                                "base",       "prop",     "teleport",  "enemy",     "animal",
                                "npc",        "townsfolk", "player",   "trap",      "fish",
                                "shroud",     "zone",     "attack",    "projectilespawner",
                                "projectile", "collider", "deprecated", "test"};

// Dropped wherever they appear: build bookkeeping, not anything a player or an operator
// would recognise.
const char* const kNoise[] = {"ag2",       "old",      "deprecated", "noui",     "healthbar",
                              "lore",      "original", "backup",     "variation", "placement",
                              "helper",    "notfinished", "donotuse", "test"};

bool IsDigits(const std::string& s) {
    if (s.empty()) return false;
    for (char c : s)
        if (!std::isdigit(static_cast<unsigned char>(c))) return false;
    return true;
}

// `hint`, `hint02`: a quest-design marker.
bool IsHint(const std::string& lowered) {
    if (lowered.compare(0, 4, "hint") != 0) return false;
    for (size_t i = 4; i < lowered.size(); i++)
        if (!std::isdigit(static_cast<unsigned char>(lowered[i]))) return false;
    return true;
}

// `v03`: an asset revision.
bool IsVersion(const std::string& lowered) {
    if (lowered.size() < 2 || lowered[0] != 'v') return false;
    for (size_t i = 1; i < lowered.size(); i++)
        if (!std::isdigit(static_cast<unsigned char>(lowered[i]))) return false;
    return true;
}

// `T1` .. `T6`: the difficulty tier, which becomes a readable suffix rather than a token.
bool IsTier(const std::string& s) {
    return s.size() == 2 && (s[0] == 'T' || s[0] == 't') && s[1] >= '1' && s[1] <= '6';
}

bool IsNoise(const std::string& token) {
    const std::string lowered = Lower(token);
    return In(kNoise, sizeof kNoise / sizeof *kNoise, lowered) || IsHint(lowered) || IsVersion(lowered);
}

// Split one underscore-separated token on its camel boundaries, and strip a trailing
// variant number off a word (`cryptKeeper01` -> crypt, Keeper). A token that begins with a
// digit keeps its digits: `6x6` is a measurement, not an enumerator.
void CamelSplit(const std::string& token, std::vector<std::string>& out) {
    std::vector<std::string> parts;
    std::string current;
    char previous = 0;
    for (char c : token) {
        const bool splits = c >= 'A' && c <= 'Z' &&
                            ((previous >= 'a' && previous <= 'z') || (previous >= '0' && previous <= '9'));
        if (splits && !current.empty()) {
            parts.push_back(current);
            current.clear();
        }
        current += c;
        previous = c;
    }
    if (!current.empty()) parts.push_back(current);

    for (const std::string& part : parts) {
        if (IsTier(part) || part.empty() || !std::isalpha(static_cast<unsigned char>(part[0]))) {
            out.push_back(part);
            continue;
        }
        size_t end = part.size();
        while (end > 0 && std::isdigit(static_cast<unsigned char>(part[end - 1]))) end--;
        // Keep at least one letter; a word that is nothing but digits never reaches here.
        out.push_back(end > 0 && std::isalpha(static_cast<unsigned char>(part[end - 1])) ? part.substr(0, end) : part);
    }
}

std::string Capitalised(const std::string& token) {
    std::string out = token;
    if (!out.empty()) out[0] = static_cast<char>(std::toupper(static_cast<unsigned char>(out[0])));
    return out;
}

}  // namespace

std::string DisplayName(const char* code, NameKind kind) {
    const std::string raw = code ? code : "";

    std::vector<std::string> tokens;
    std::string piece;
    std::vector<std::string> pieces;
    for (char c : raw) {
        if (c == '_') {
            if (!piece.empty()) pieces.push_back(piece);
            piece.clear();
        } else {
            piece += c;
        }
    }
    if (!piece.empty()) pieces.push_back(piece);

    for (const std::string& part : pieces) {
        const std::string lowered = Lower(part);
        if (In(kAtomic, sizeof kAtomic / sizeof *kAtomic, lowered) || IsDigits(part) || IsHint(lowered) ||
            IsVersion(lowered))
            tokens.push_back(part);
        else
            CamelSplit(part, tokens);
    }

    // A map label spells out its region and its slot before the name that matters:
    // `8kMapLabel_steppes_town_06_Brightwich` is Brightwich. The last enumerator that has a
    // real name after it is where the name starts.
    bool trailing_number = false;
    if (kind == NameKind::Location) {
        size_t cut = tokens.size();
        for (size_t i = 0; i < tokens.size(); i++) {
            if (!IsDigits(tokens[i])) continue;
            bool named_after = false;
            for (size_t j = i + 1; j < tokens.size(); j++)
                if (!IsDigits(tokens[j]) && !IsNoise(tokens[j])) named_after = true;
            if (named_after) cut = i;
        }
        if (cut < tokens.size()) {
            tokens.erase(tokens.begin(), tokens.begin() + static_cast<long>(cut) + 1);
            trailing_number = !tokens.empty() && IsDigits(tokens.back());
        }
    }

    std::vector<std::string> filtered;
    for (size_t i = 0; i < tokens.size(); i++) {
        if (IsNoise(tokens[i])) continue;
        if (IsDigits(tokens[i])) {
            // `..._HuntressCamp_1`: the enumerator that survived the cut above is part of
            // the name, because it is what tells two Huntress Camps apart.
            if (trailing_number && i + 1 == tokens.size() && !filtered.empty()) filtered.push_back(tokens[i]);
            continue;
        }
        filtered.push_back(tokens[i]);
    }

    std::vector<std::string> named = filtered;
    while (named.size() > 1 && In(kLeading, sizeof kLeading / sizeof *kLeading, Lower(named.front())))
        named.erase(named.begin());

    std::string tier;
    std::vector<std::string> rest;
    for (const std::string& token : named) {
        if (tier.empty() && IsTier(token))
            tier = token.substr(1);
        else
            rest.push_back(token);
    }
    named = rest;
    // Everything was technical: say what is left rather than nothing, and never the code.
    if (named.empty()) named = filtered;
    if (named.empty()) CamelSplit(raw, named);

    std::string name;
    for (const std::string& token : named) {
        if (token.empty()) continue;
        if (!name.empty()) name += ' ';
        name += Capitalised(token);
    }
    if (!tier.empty()) {
        if (!name.empty()) name += ' ';
        name += "(Tier " + tier + ")";
    }
    if (name.empty()) {
        for (char c : raw) name += (c == '_') ? ' ' : c;
    }
    return name;
}
