#pragma once

// ShooterGameServer 21241282, SHA-256 7e7ded49...7c520 only.
// The display name of the weapon a killer pawn holds, read by reflection on
// the game thread while the death callback runs (before the original):
//   pawn.CurrentWeapon -> weapon.AssociatedPrimalItem -> item.DescriptiveNameBase
// Every object pointer must be a live entry of the global object array (weak
// serial not required), and
// the item's class must derive from PrimalItem. Anything unverified, and an
// unarmed killer, yields an empty name: a weapon is never invented.
#include "death_bindings.hpp"
#include "entity_bindings.hpp"
#include <string>

namespace ark_weapon {

enum class Status { ok, unarmed, unverified };

struct Api {
  void* (*primal_item_class)() = reinterpret_cast<void* (*)()>(0x13479B0);
  void* (*string_property_class)() = reinterpret_cast<void* (*)()>(0x1D2B470);
  ark_entity::FName* (*make_name)(ark_entity::FName*, const wchar_t*, int, int) =
      reinterpret_cast<ark_entity::FName* (*)(ark_entity::FName*, const wchar_t*, int, int)>(0x1C892D0);
  ark_death::ObjectArray objects{};
  long game_thread_tid = 0;
};

// Like ark_entity::find_property, but for object references, whose property
// class is not pinned here: the value read through it is verified instead.
inline void* find_named_field(void* klass, const ark_entity::FName& wanted) {
  for (int depth = 0; depth < 64 && ark_entity::plausible(klass); ++depth) {
    auto* child = *reinterpret_cast<void* const*>(reinterpret_cast<uintptr_t>(klass) + 0x38);
    for (int fields = 0; fields < 4096 && child; ++fields) {
      if (!ark_entity::plausible(child)) return nullptr;
      const auto found = *reinterpret_cast<const ark_entity::FName*>(reinterpret_cast<uintptr_t>(child) + 0x18);
      if (found.index == wanted.index && found.number == wanted.number) return child;
      child = *reinterpret_cast<void* const*>(reinterpret_cast<uintptr_t>(child) + 0x28);
    }
    if (child) return nullptr;
    klass = *reinterpret_cast<void* const*>(reinterpret_cast<uintptr_t>(klass) + 0x30);
  }
  return nullptr;
}

// A live entry of the global object array: the object's own index points back
// at it. Unlike ark_death::object_key this does not require a weak serial,
// which the engine assigns only once a weak pointer is taken (a held weapon
// and its item usually have none).
inline bool live_object(const void* object, const ark_death::ObjectArray& array) {
  if (!ark_death::plausible(object) || !array.count || !array.chunks) return false;
  const int32_t count = *array.count;
  const int32_t index = *reinterpret_cast<const int32_t*>(reinterpret_cast<uintptr_t>(object) + 0x0C);
  if (count <= 0 || count > 10000000 || index < 0 || index >= count) return false;
  auto* chunks = *array.chunks;
  if (!ark_death::plausible(chunks)) return false;
  auto* chunk = reinterpret_cast<const uint8_t*>(chunks[static_cast<uint32_t>(index) >> 16]);
  if (!ark_death::plausible(chunk)) return false;
  return *reinterpret_cast<void* const*>(chunk + (static_cast<uint32_t>(index) & 0xFFFFu) * 16u) == object;
}

// Reads the field `name` of `object` as an object pointer. found=false when
// the field or its offset cannot be verified; value=nullptr is a verified null.
inline bool object_field(void* object, const wchar_t* name, void*& value, const Api& api) {
  value = nullptr;
  if (!live_object(object, api.objects)) return false;
  void* klass = *reinterpret_cast<void* const*>(reinterpret_cast<uintptr_t>(object) + 0x10);
  ark_entity::FName field_name{};
  api.make_name(&field_name, name, 1, 1);
  if (field_name.index <= 0) return false;
  void* property = find_named_field(klass, field_name);
  if (!property) return false;
  const int32_t size = *reinterpret_cast<const int32_t*>(reinterpret_cast<uintptr_t>(klass) + 0x40);
  const int32_t offset = *reinterpret_cast<const int32_t*>(reinterpret_cast<uintptr_t>(property) + 0x4C);
  if (size <= 0 || size > 65536 || offset < 0 || offset > size - 8 || (offset & 7) != 0) return false;
  void* candidate = *reinterpret_cast<void* const*>(reinterpret_cast<uintptr_t>(object) + offset);
  if (candidate && !live_object(candidate, api.objects)) return false;
  value = candidate;
  return true;
}

inline Status held_weapon_name(void* pawn, std::string& name, const Api& api,
                               const char** failed_step = nullptr) {
  name.clear();
  auto fail = [&](const char* step) {
    if (failed_step) *failed_step = step;
    name.clear();
    return Status::unverified;
  };
  if (!api.primal_item_class || !api.string_property_class || !api.make_name ||
      api.game_thread_tid <= 0 || syscall(SYS_gettid) != api.game_thread_tid)
    return fail("api");
  void* weapon = nullptr;
  if (!object_field(pawn, L"CurrentWeapon", weapon, api)) return fail("CurrentWeapon");
  if (!weapon) return Status::unarmed;
  void* item = nullptr;
  if (!object_field(weapon, L"AssociatedPrimalItem", item, api) || !item) return fail("AssociatedPrimalItem");
  void* item_class = *reinterpret_cast<void* const*>(reinterpret_cast<uintptr_t>(item) + 0x10);
  if (!ark_entity::is_a(item_class, api.primal_item_class())) return fail("PrimalItem");
  void* string_class = api.string_property_class();
  ark_entity::FName base_name{};
  api.make_name(&base_name, L"DescriptiveNameBase", 1, 1);
  if (base_name.index <= 0 || !ark_entity::plausible(string_class)) return fail("DescriptiveNameBase-name");
  void* property = ark_entity::find_property(item_class, base_name, string_class);
  if (!property) return fail("DescriptiveNameBase-property");
  const int32_t size = *reinterpret_cast<const int32_t*>(reinterpret_cast<uintptr_t>(item_class) + 0x40);
  const int32_t offset = *reinterpret_cast<const int32_t*>(reinterpret_cast<uintptr_t>(property) + 0x4C);
  if (size <= 0 || size > 65536 || offset < 0 ||
      offset > size - static_cast<int32_t>(sizeof(ark_inventory::FString)))
    return fail("DescriptiveNameBase-offset");
  const auto* value = reinterpret_cast<const ark_inventory::FString*>(reinterpret_cast<uintptr_t>(item) + offset);
  if (!ark_entity::copy_borrowed_string(*value, name, 128)) return fail("DescriptiveNameBase-value");
  return Status::ok;
}

} // namespace ark_weapon
