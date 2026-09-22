// Display names for entity and location templates, derived from their codes.
//
// The dedicated server ships no localisation: gamedata.h is baked from the code by
// tools/gen_gamedata.py, ItemDef carries the only name field there is, and EntityDef and
// LocationDef carry none at all. So a catalogue answer for entities and locations has to
// derive its name, and until this file it derived it by opening underscores -- which put
// "1 Player AG2" and "8k Map Label deepforest camp 01 Huntress Camp 1" in front of an
// operator as if they were names.
//
// Nothing here touches Windows, the hooks or the game: it is a pure string function, so
// tests/names_test.cpp links it on the host and asserts the rules over the whole shipped
// table. A curated or localised source is a separate ticket; this is the readable floor.
#pragma once
#include <string>

enum class NameKind { Entity, Location };

// A human-readable name for a template code. Never empty, never the raw code with its
// underscores opened, never carrying a technical token an operator has no use for.
std::string DisplayName(const char* code, NameKind kind);
