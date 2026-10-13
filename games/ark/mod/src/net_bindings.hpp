#pragma once

// ShooterGameServer 21241282, SHA-256 7e7ded49...7c520 only.
// A connected player's remote IP and ping, read on the game thread inside the
// existing snapshot pass (PostLogin and the 250 ms position sample):
//   controller.NetConnection -> UIpConnection::LowLevelGetRemoteAddress(false)
//   controller+0x488 PlayerState -> ExactPing (FloatProperty, ms) or Ping (byte, ms/4)
// UNetConnection's vtable (0x446D6D0, no RTTI) holds the PURE_VIRTUAL stub that
// names "UNetConnection::LowLevelGetRemoteAddress" at slot 73. UIpConnection
// (vtables 0x43F41A0 and 0x46CDE50) overrides that slot with 0x24D2FD0, which
// returns RemoteAddr->ToString(bAppendPort) and dereferences RemoteAddr
// (this+0x34338) unconditionally. The call is made only when slot 73 is
// exactly that implementation and RemoteAddr is set. Anything unverified
// yields no value: an address or ping is never invented.
#include "weapon_bindings.hpp"
#include <algorithm>
#include <cctype>
#include <cmath>
#include <optional>
#include <string>

namespace ark_net {

struct Api {
  ark_entity::FName* (*make_name)(ark_entity::FName*, const wchar_t*, int, int) =
      reinterpret_cast<ark_entity::FName* (*)(ark_entity::FName*, const wchar_t*, int, int)>(0x1C892D0);
  void (*game_free)(const void*) = reinterpret_cast<void (*)(const void*)>(0x1AF8E30);
  uintptr_t remote_address_impl = 0x24D2FD0;
  size_t remote_address_slot = 73;
  size_t remote_addr_offset = 0x34338;
  size_t player_state_offset = 0x488;
  ark_death::ObjectArray objects{};
  long game_thread_tid = 0;
};

// Dotted IPv4 or textual IPv6, port stripped. Anything else is rejected.
inline std::optional<std::string> normalize_ip(std::string text) {
  if (text.empty() || text.size() > 64) return std::nullopt;
  if (text.front() == '[') {
    const auto close = text.find(']');
    if (close == std::string::npos) return std::nullopt;
    text = text.substr(1, close - 1);
  } else if (std::count(text.begin(), text.end(), ':') == 1) {
    text = text.substr(0, text.find(':'));
  }
  if (text.find(':') == std::string::npos) {
    int groups = 0;
    size_t i = 0;
    while (i <= text.size()) {
      size_t j = i;
      int value = 0;
      while (j < text.size() && j - i < 4 && std::isdigit(static_cast<unsigned char>(text[j])))
        value = value * 10 + (text[j++] - '0');
      if (j == i || j - i > 3 || value > 255) return std::nullopt;
      ++groups;
      if (j == text.size()) break;
      if (text[j] != '.' || groups == 4) return std::nullopt;
      i = j + 1;
    }
    if (groups != 4 || text == "0.0.0.0") return std::nullopt;
    return text;
  }
  for (unsigned char c : text)
    if (!std::isxdigit(c) && c != ':' && c != '.') return std::nullopt;
  if (text.size() < 2 || text == "::") return std::nullopt;
  return text;
}

inline bool property_type_is(void* property, const wchar_t* type, const Api& api) {
  if (!ark_entity::plausible(property)) return false;
  void* klass = *reinterpret_cast<void* const*>(reinterpret_cast<uintptr_t>(property) + 0x10);
  if (!ark_entity::plausible(klass)) return false;
  ark_entity::FName wanted{};
  api.make_name(&wanted, type, 1, 1);
  const auto found = *reinterpret_cast<const ark_entity::FName*>(reinterpret_cast<uintptr_t>(klass) + 0x18);
  return wanted.index > 0 && found.index == wanted.index && found.number == wanted.number;
}

// Offset of a reflected scalar field of `object`'s class, checked against the
// class size; -1 when the field or its type cannot be verified.
inline int32_t scalar_offset(void* object, const wchar_t* name, const wchar_t* type, int32_t width,
                             const Api& api) {
  void* klass = *reinterpret_cast<void* const*>(reinterpret_cast<uintptr_t>(object) + 0x10);
  ark_entity::FName field{};
  api.make_name(&field, name, 1, 1);
  if (field.index <= 0) return -1;
  void* property = ark_weapon::find_named_field(klass, field);
  if (!property || !property_type_is(property, type, api)) return -1;
  const int32_t size = *reinterpret_cast<const int32_t*>(reinterpret_cast<uintptr_t>(klass) + 0x40);
  const int32_t offset = *reinterpret_cast<const int32_t*>(reinterpret_cast<uintptr_t>(property) + 0x4C);
  if (size <= 0 || size > 65536 || offset < 0 || offset > size - width || offset % width != 0) return -1;
  return offset;
}

inline bool on_game_thread(const Api& api) {
  return api.make_name && api.game_free && api.game_thread_tid > 0 &&
         syscall(SYS_gettid) == api.game_thread_tid;
}

inline std::optional<std::string> remote_ip(void* controller, const Api& api,
                                            const char** failed_step = nullptr) {
  auto fail = [&](const char* step) -> std::optional<std::string> {
    if (failed_step) *failed_step = step;
    return std::nullopt;
  };
  if (!on_game_thread(api)) return fail("api");
  ark_weapon::Api objects{};
  objects.make_name = api.make_name;
  objects.objects = api.objects;
  void* connection = nullptr;
  if (!ark_weapon::object_field(controller, L"NetConnection", connection, objects) || !connection)
    return fail("NetConnection");
  auto* const* vtable = *reinterpret_cast<void* const* const*>(connection);
  if (!ark_entity::plausible(vtable) ||
      reinterpret_cast<uintptr_t>(vtable[api.remote_address_slot]) != api.remote_address_impl)
    return fail("IpConnection");
  void* klass = *reinterpret_cast<void* const*>(reinterpret_cast<uintptr_t>(connection) + 0x10);
  const int32_t size = *reinterpret_cast<const int32_t*>(reinterpret_cast<uintptr_t>(klass) + 0x40);
  if (size <= 0 || static_cast<size_t>(size) < api.remote_addr_offset + 8) return fail("IpConnection-size");
  void* remote = *reinterpret_cast<void* const*>(reinterpret_cast<uintptr_t>(connection) + api.remote_addr_offset);
  if (!ark_entity::plausible(remote) || !ark_entity::plausible(*reinterpret_cast<void* const*>(remote)))
    return fail("RemoteAddr");
  ark_inventory::FString text{};
  reinterpret_cast<ark_inventory::FString* (*)(ark_inventory::FString*, void*, bool)>(
      api.remote_address_impl)(&text, connection, false);
  std::string value;
  const bool copied = ark_entity::copy_borrowed_string(text, value, 64);
  if (text.data) api.game_free(text.data);
  if (!copied) return fail("RemoteAddress-value");
  auto ip = normalize_ip(value);
  if (!ip) return fail("RemoteAddress-format");
  return ip;
}

// Milliseconds, from ExactPing when reflected as a float, else Ping x 4.
inline std::optional<int32_t> ping_ms(void* controller, const Api& api,
                                      const char** failed_step = nullptr) {
  auto fail = [&](const char* step) -> std::optional<int32_t> {
    if (failed_step) *failed_step = step;
    return std::nullopt;
  };
  if (!on_game_thread(api)) return fail("api");
  if (!ark_weapon::live_object(controller, api.objects)) return fail("controller");
  void* state = *reinterpret_cast<void* const*>(reinterpret_cast<uintptr_t>(controller) + api.player_state_offset);
  if (!ark_weapon::live_object(state, api.objects)) return fail("PlayerState");
  const auto base = reinterpret_cast<uintptr_t>(state);
  if (const int32_t exact = scalar_offset(state, L"ExactPing", L"FloatProperty", 4, api); exact >= 0) {
    const float value = *reinterpret_cast<const float*>(base + exact);
    if (!std::isfinite(value) || value < 0.f || value > 60000.f) return fail("ExactPing-value");
    return static_cast<int32_t>(std::lround(value));
  }
  if (const int32_t coarse = scalar_offset(state, L"Ping", L"ByteProperty", 1, api); coarse >= 0)
    return static_cast<int32_t>(*reinterpret_cast<const uint8_t*>(base + coarse)) * 4;
  return fail("Ping-property");
}

} // namespace ark_net
