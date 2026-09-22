// Every entity and location name the catalogue answers with, over the whole shipped table.
//
// The dedicated server ships no localisation, so those names are derived from the template
// codes. Before names.cpp the derivation opened underscores and nothing else, and what
// reached an operator was "1 Player AG2" and "8k Map Label deepforest camp 01 Huntress
// Camp 1". The vectors below pin the rules; the corpus pass asserts that no code in
// gamedata.cpp -- 979 entities and 1031 locations -- produces a name with a dev token, an
// underscore, a bare tier code or a leading digit still in it, and that none of them is
// just its own code with the underscores opened.
#include "gamedata.h"
#include "names.h"

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace {

int failures = 0;

void Expect(const char* code, NameKind kind, const char* want) {
    const std::string got = DisplayName(code, kind);
    if (got != want) {
        std::printf("  %-52s -> \"%s\" (wanted \"%s\")\n", code, got.c_str(), want);
        failures++;
    }
}

std::vector<std::string> Words(const std::string& name) {
    std::vector<std::string> words;
    std::string current;
    for (char c : name) {
        if (c == ' ') {
            if (!current.empty()) words.push_back(current);
            current.clear();
        } else {
            current += c;
        }
    }
    if (!current.empty()) words.push_back(current);
    return words;
}

std::string Lower(std::string s) {
    for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

// Everything one derived name has to satisfy, or the reason it does not.
std::string Wrong(const char* code, const std::string& name) {
    if (name.empty()) return "empty";
    if (name.find('_') != std::string::npos) return "still carries an underscore";
    if (name[0] == ' ') return "starts with a space";
    if (name[0] >= '0' && name[0] <= '9') return "starts with a digit";
    for (const std::string& word : Words(name)) {
        const std::string lowered = Lower(word);
        if (lowered == "ag2" || lowered == "deprecated" || lowered == "placement" || lowered == "noui" ||
            lowered == "healthbar")
            return "keeps the dev token \"" + word + "\"";
        if (word.size() == 2 && (word[0] == 'T' || word[0] == 't') && word[1] >= '0' && word[1] <= '9')
            return "keeps the bare tier token \"" + word + "\"";
    }
    std::string opened;
    for (const char* c = code; *c; c++) opened += (*c == '_') ? ' ' : *c;
    if (name == opened) return "is the code with its underscores opened";
    return "";
}

int Corpus(const char* what, NameKind kind, const char* const* codes, size_t count) {
    int wrong = 0;
    for (size_t i = 0; i < count; i++) {
        const std::string reason = Wrong(codes[i], DisplayName(codes[i], kind));
        if (reason.empty()) continue;
        if (wrong < 10) std::printf("  %-64s %s\n", codes[i], reason.c_str());
        wrong++;
    }
    std::printf("%s: %zu codes, %d offenders\n", what, count, wrong);
    return wrong;
}

}  // namespace

int main() {
    // The rules, in the order names.cpp applies them.
    Expect("1_Player_AG2", NameKind::Entity, "Player");
    Expect("Animal_Baby_T1_Goat", NameKind::Entity, "Baby Goat (Tier 1)");
    Expect("Enemy_Skeleton_Heavy", NameKind::Entity, "Skeleton Heavy");
    Expect("NPC_Workshop_cryptKeeper01", NameKind::Entity, "Workshop Crypt Keeper");
    Expect("Animal_Wildlife_Cat_01_black_AG2", NameKind::Entity, "Wildlife Cat Black");
    Expect("AutomatedPlayer", NameKind::Entity, "Automated Player");
    Expect("8KMapLabel_deepforest_camp_01_HuntressCamp_1", NameKind::Location, "Huntress Camp 1");
    Expect("8kMapLabel_AncientDungeon_NightTemple_general", NameKind::Location,
           "Ancient Dungeon Night Temple General");
    Expect("8kMapLabel_deepforest_Town_07_Whitewind", NameKind::Location, "Whitewind");
    Expect("8kMapLabel_steppes_town_06_Brightwich", NameKind::Location, "Brightwich");
    Expect("Prop_OpenWorld_SavePoint", NameKind::Location, "Open World Save Point");
    Expect("Teleport_Platform_Gameplay", NameKind::Location, "Platform Gameplay");
    if (failures) std::printf("%d vector(s) wrong\n", failures);

    std::vector<const char*> entity_codes;
    for (size_t i = 0; i < kEntityCount; i++) entity_codes.push_back(kEntities[i].code);
    std::vector<const char*> location_codes;
    for (size_t i = 0; i < kLocationCount; i++) location_codes.push_back(kLocations[i].code);

    const int bad = Corpus("entities", NameKind::Entity, entity_codes.data(), entity_codes.size()) +
                    Corpus("locations", NameKind::Location, location_codes.data(), location_codes.size());

    if (failures || bad) {
        std::printf("names_test FAILED\n");
        return 1;
    }
    std::printf("names_test ok\n");
    return 0;
}
