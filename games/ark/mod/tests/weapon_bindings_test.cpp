// Held-weapon display name for entity-killed: pawn.CurrentWeapon ->
// weapon.AssociatedPrimalItem -> item.DescriptiveNameBase, every pointer
// verified against the global object array; never an invented weapon.
#include "../src/weapon_bindings.hpp"
#include <array>
#include <cassert>
#include <cstring>
#include <cwchar>

namespace {
template <size_t N> struct Block { alignas(16) std::array<unsigned char, N> bytes{}; };
template <class T> void put(void* base, size_t at, T value) {
  std::memcpy(static_cast<unsigned char*>(base) + at, &value, sizeof(value));
}
void* item_base{};
void* string_class{};
void* get_item_base() { return item_base; }
void* get_string_class() { return string_class; }
ark_entity::FName* make_name(ark_entity::FName* out, const wchar_t* text, int, int) {
  out->number = 0;
  out->index = std::wcscmp(text, L"CurrentWeapon") == 0 ? 11
      : std::wcscmp(text, L"AssociatedPrimalItem") == 0 ? 12
      : std::wcscmp(text, L"DescriptiveNameBase") == 0 ? 13 : 0;
  return out;
}
}

int main() {
  Block<0x200> pawn_class, weapon_class, item_class, item_root, string_type, other_type;
  Block<0x80> current_weapon, associated_item, base_name;
  Block<0x200> pawn, weapon, item;
  // Global object array: one chunk of 16-byte items {object, flags, serial}.
  Block<0x100> chunk;
  void* chunks[1] = {chunk.bytes.data()};
  void* const* chunk_table = chunks;
  int32_t count = 8;
  ark_weapon::Api api{};
  api.primal_item_class = get_item_base;
  api.string_property_class = get_string_class;
  api.make_name = make_name;
  api.objects = {&chunk_table, &count};
  api.game_thread_tid = syscall(SYS_gettid);
  item_base = item_root.bytes.data();
  string_class = string_type.bytes.data();
  auto live = [&](void* object, int32_t index) {
    put<int32_t>(object, 0x0C, index);
    put<void*>(chunk.bytes.data(), static_cast<size_t>(index) * 16, object);
    put<int32_t>(chunk.bytes.data(), static_cast<size_t>(index) * 16 + 8, 1);
  };
  live(pawn.bytes.data(), 1);
  live(weapon.bytes.data(), 2);
  live(item.bytes.data(), 3);
  put<void*>(pawn.bytes.data(), 0x10, pawn_class.bytes.data());
  put<void*>(weapon.bytes.data(), 0x10, weapon_class.bytes.data());
  put<void*>(item.bytes.data(), 0x10, item_class.bytes.data());
  for (auto* klass : {&pawn_class, &weapon_class, &item_class}) put<int32_t>(klass->bytes.data(), 0x40, 0x200);
  put<void*>(item_class.bytes.data(), 0x30, item_root.bytes.data());
  // pawn.CurrentWeapon at +0x100
  put<void*>(pawn_class.bytes.data(), 0x38, current_weapon.bytes.data());
  put<ark_entity::FName>(current_weapon.bytes.data(), 0x18, {11, 0});
  put<int32_t>(current_weapon.bytes.data(), 0x4C, 0x100);
  put<void*>(current_weapon.bytes.data(), 0x10, other_type.bytes.data());
  // weapon.AssociatedPrimalItem at +0x108
  put<void*>(weapon_class.bytes.data(), 0x38, associated_item.bytes.data());
  put<ark_entity::FName>(associated_item.bytes.data(), 0x18, {12, 0});
  put<int32_t>(associated_item.bytes.data(), 0x4C, 0x108);
  put<void*>(associated_item.bytes.data(), 0x10, other_type.bytes.data());
  // item.DescriptiveNameBase (StrProperty) at +0x120
  put<void*>(item_class.bytes.data(), 0x38, base_name.bytes.data());
  put<ark_entity::FName>(base_name.bytes.data(), 0x18, {13, 0});
  put<int32_t>(base_name.bytes.data(), 0x4C, 0x120);
  put<void*>(base_name.bytes.data(), 0x10, string_type.bytes.data());
  static const wchar_t sword[] = L"Sword";
  put<ark_inventory::FString>(item.bytes.data(), 0x120, {const_cast<wchar_t*>(sword), 6, 6});
  put<void*>(pawn.bytes.data(), 0x100, weapon.bytes.data());
  put<void*>(weapon.bytes.data(), 0x108, item.bytes.data());

  std::string name;
  assert(ark_weapon::held_weapon_name(pawn.bytes.data(), name, api) == ark_weapon::Status::ok);
  assert(name == "Sword");

  // Unarmed: a verified null CurrentWeapon.
  put<void*>(pawn.bytes.data(), 0x100, nullptr);
  assert(ark_weapon::held_weapon_name(pawn.bytes.data(), name, api) == ark_weapon::Status::unarmed && name.empty());
  put<void*>(pawn.bytes.data(), 0x100, weapon.bytes.data());

  // A pointer that is not a live object-array entry is never followed.
  put<int32_t>(chunk.bytes.data(), 2 * 16 + 8, 0);
  assert(ark_weapon::held_weapon_name(pawn.bytes.data(), name, api) == ark_weapon::Status::unverified && name.empty());
  put<int32_t>(chunk.bytes.data(), 2 * 16 + 8, 1);

  // An item that does not derive from PrimalItem is not named.
  put<void*>(item_class.bytes.data(), 0x30, nullptr);
  assert(ark_weapon::held_weapon_name(pawn.bytes.data(), name, api) == ark_weapon::Status::unverified && name.empty());
  put<void*>(item_class.bytes.data(), 0x30, item_root.bytes.data());

  // A base-name field of another property type is not trusted.
  put<void*>(base_name.bytes.data(), 0x10, other_type.bytes.data());
  assert(ark_weapon::held_weapon_name(pawn.bytes.data(), name, api) == ark_weapon::Status::unverified);
  put<void*>(base_name.bytes.data(), 0x10, string_type.bytes.data());

  // Missing field and misaligned offset.
  put<ark_entity::FName>(current_weapon.bytes.data(), 0x18, {99, 0});
  assert(ark_weapon::held_weapon_name(pawn.bytes.data(), name, api) == ark_weapon::Status::unverified);
  put<ark_entity::FName>(current_weapon.bytes.data(), 0x18, {11, 0});
  put<int32_t>(current_weapon.bytes.data(), 0x4C, 0x101);
  assert(ark_weapon::held_weapon_name(pawn.bytes.data(), name, api) == ark_weapon::Status::unverified);
  put<int32_t>(current_weapon.bytes.data(), 0x4C, 0x100);

  // Wrong thread.
  api.game_thread_tid = -1;
  assert(ark_weapon::held_weapon_name(pawn.bytes.data(), name, api) == ark_weapon::Status::unverified);
  api.game_thread_tid = syscall(SYS_gettid);
  assert(ark_weapon::held_weapon_name(pawn.bytes.data(), name, api) == ark_weapon::Status::ok && name == "Sword");
  return 0;
}
