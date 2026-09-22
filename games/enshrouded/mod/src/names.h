// Display names for item, entity and location templates, derived from their codes.
//
// The dedicated server ships no localisation: gamedata.h is baked from the code by
// tools/gen_gamedata.py and carries no display-name source for any of the three tables. A
// catalogue answer therefore has to derive its names, and it has to derive them well
// enough that an operator reading listItems, listEntities or listLocations sees words
// rather than the asset pipeline's bookkeeping.
//
// DisplayName is the derivation for one code. DistinctNames is what the catalogue answers
// with: the same derivation over a whole table, re-derived at a more detailed level for
// any codes that would otherwise share a name, so two different codes never collide.
//
// Nothing here touches Windows, the hooks or the game: it is a pure string function, so
// tests/names_test.cpp links it on the host and asserts the rules over the whole shipped
// table.
#pragma once
#include <cstddef>
#include <string>
#include <vector>

enum class NameKind { Item, Entity, Location };

// A human-readable name for a template code. Never empty, never the raw code with its
// underscores opened, never carrying a technical token an operator has no use for.
std::string DisplayName(const char* code, NameKind kind);

// One name per code, in order, such that two different codes never get the same name.
// Two rows carrying the same code legitimately share one name.
void DistinctNames(NameKind kind, const char* const* codes, size_t count, std::vector<std::string>& out);
