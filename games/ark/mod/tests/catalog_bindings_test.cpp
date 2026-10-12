#include "../src/catalog_bindings.hpp"
#include <cassert>
#include <cstring>
#include <cwchar>
#include <unordered_map>

using namespace ark_catalog;
namespace {
int freed = 0;
alignas(8) unsigned char base_class[0x48]{};
alignas(8) unsigned char item_class[0x100]{};
alignas(8) unsigned char wrong_class[0x38]{};
alignas(8) unsigned char item_cdo[0x40]{};
alignas(8) unsigned char string_type[0x40]{};
alignas(8) unsigned char base_name_prop[0x60]{};
std::unordered_map<void*, std::wstring> paths;
void* primal_item_class() { return base_class; }
void* string_property_class() { return string_type; }
ark_entity::FName* make_name(ark_entity::FName* out, const wchar_t* text, int, int) {
  assert(std::wcscmp(text, L"DescriptiveNameBase") == 0);
  *out = {7, 0};
  return out;
}
ark_inventory::FString* class_path(ark_inventory::FString* out, void* klass) {
  auto it = paths.find(klass);
  assert(it != paths.end());
  const auto& value = it->second;
  auto* data = new wchar_t[value.size() + 1];
  std::memcpy(data, value.c_str(), (value.size() + 1) * sizeof(wchar_t));
  *out = {data, static_cast<int32_t>(value.size() + 1), static_cast<int32_t>(value.size() + 1)};
  return out;
}
void game_free(const void* p) { ++freed; delete[] static_cast<const wchar_t*>(p); }
ark_inventory::FString* short_name(ark_inventory::FString* out, void* cdo) {
  assert(cdo == item_cdo);
  constexpr wchar_t value[] = L"Stone";
  auto* data = new wchar_t[sizeof(value) / sizeof(wchar_t)];
  std::memcpy(data, value, sizeof(value));
  *out = {data, 6, 6};
  return out;
}
template<class T> void put(unsigned char* p, size_t offset, T value) {
  std::memcpy(p + offset, &value, sizeof(value));
}
}

int main() {
  alignas(8) unsigned char engine[0x1D8]{};
  alignas(8) unsigned char holder[0x38]{};
  alignas(8) unsigned char data[0x930]{};
  void* classes[3]{item_class, nullptr, base_class};
  put(engine, 0x1D0, static_cast<void*>(holder));
  put(holder, 0x30, static_cast<void*>(data));
  put(data, 0x910, static_cast<void*>(classes));
  put(data, 0x918, int32_t{3});
  put(data, 0x91C, int32_t{3});
  put(item_class, 0x30, static_cast<void*>(base_class));
  put(item_class, 0xF8, static_cast<void*>(item_cdo));
  put(item_cdo, 0x10, static_cast<void*>(item_class));
  paths[item_class] = L"/Game/Items/Stone.PrimalItemResource_Stone_C";
  void* engine_ptr = engine;
  Api api{&engine_ptr, primal_item_class, class_path, game_free, syscall(SYS_gettid)};
  api.string_property_class = string_property_class;
  api.make_name = make_name;
  // UPrimalItem::DescriptiveNameBase: a StrProperty at CDO+0x18.
  put(item_class, 0x38, static_cast<void*>(base_name_prop));
  put(item_class, 0x40, int32_t{0x40});
  put(base_name_prop, 0x10, static_cast<void*>(string_type));
  put(base_name_prop, 0x18, ark_entity::FName{7, 0});
  put(base_name_prop, 0x4C, int32_t{0x18});
  static const wchar_t shotgun[] = L"Shotgun";
  put(item_cdo, 0x18, ark_inventory::FString{const_cast<wchar_t*>(shotgun), 8, 8});
  Page out;
  assert(page(0, 2, out, api) == Status::ok);
  assert(out.identity.count == 3 && out.items.size() == 1);
  assert(out.items[0].code == "/Game/Items/Stone.PrimalItemResource_Stone_C");
  assert(out.items[0].class_name == "Stone.PrimalItemResource_Stone_C");
  NamedPage named;
  assert(named_page(0, 2, named, api, short_name) == Status::ok);
  assert(named.items.size() == 1 && named.items[0].name == "Shotgun" &&
         named.items[0].code == "/Game/Items/Stone.PrimalItemResource_Stone_C");
  // Empty base name: the short name is the fallback, never an empty catalog name.
  put(item_cdo, 0x18, ark_inventory::FString{nullptr, 0, 0});
  assert(named_page(0, 2, named, api, short_name) == Status::ok && named.items[0].name == "Stone");
  // A property of another type is not trusted as the base name.
  put(base_name_prop, 0x10, static_cast<void*>(wrong_class));
  put(item_cdo, 0x18, ark_inventory::FString{const_cast<wchar_t*>(shotgun), 8, 8});
  assert(named_page(0, 2, named, api, short_name) == Status::ok && named.items[0].name == "Stone");
  // An offset outside the class default object is not read.
  put(base_name_prop, 0x10, static_cast<void*>(string_type));
  put(base_name_prop, 0x4C, int32_t{0x38});
  assert(named_page(0, 2, named, api, short_name) == Status::ok && named.items[0].name == "Stone");
  put(base_name_prop, 0x4C, int32_t{0x18});
  put(item_class, 0xF8, static_cast<void*>(nullptr));
  assert(named_page(0, 2, named, api, short_name) == Status::not_ready && named.items.empty());
  put(item_class, 0xF8, static_cast<void*>(item_cdo));
  const Identity id = out.identity;
  assert(page(2, 2, out, api, &id) == Status::ok && out.items.empty());
  assert(freed == 9);
  Identity stale = id; ++stale.count;
  assert(page(0, 2, out, api, &stale) == Status::changed);
  classes[0] = wrong_class;
  assert(page(0, 2, out, api) == Status::unverified_class);
  classes[0] = item_class;
  api.game_thread_tid = -1;
  assert(page(0, 2, out, api) == Status::invalid_layout);
  api.game_thread_tid = syscall(SYS_gettid);
  put(data, 0x918, int32_t{65537});
  assert(page(0, 2, out, api) == Status::invalid_layout);
  put(data, 0x918, int32_t{0});
  assert(page(0, 2, out, api) == Status::not_ready);
}
