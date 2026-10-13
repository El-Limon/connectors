// Player IP and ping: controller.NetConnection -> verified UIpConnection
// LowLevelGetRemoteAddress, PlayerState ExactPing/Ping by reflection.
// Nothing unverified is ever reported.
#include "../src/net_bindings.hpp"
#include <array>
#include <cassert>
#include <cstring>
#include <cwchar>

namespace {
template <size_t N> struct Block { alignas(16) std::array<unsigned char, N> bytes{}; };
template <class T> void put(void* base, size_t at, T value) {
  std::memcpy(static_cast<unsigned char*>(base) + at, &value, sizeof(value));
}
ark_entity::FName* make_name(ark_entity::FName* out, const wchar_t* text, int, int) {
  out->number = 0;
  out->index = std::wcscmp(text, L"NetConnection") == 0 ? 21
      : std::wcscmp(text, L"ExactPing") == 0 ? 22
      : std::wcscmp(text, L"Ping") == 0 ? 23
      : std::wcscmp(text, L"FloatProperty") == 0 ? 31
      : std::wcscmp(text, L"ByteProperty") == 0 ? 32 : 0;
  return out;
}
int frees = 0;
void game_free(const void* p) { ++frees; delete[] static_cast<const wchar_t*>(p); }
const wchar_t* returned = L"203.0.113.9";
bool append_port_seen = true;
ark_inventory::FString* fake_remote(ark_inventory::FString* out, void*, bool append_port) {
  append_port_seen = append_port;
  const size_t n = std::wcslen(returned) + 1;
  auto* data = new wchar_t[n];
  std::wmemcpy(data, returned, n);
  *out = {data, static_cast<int32_t>(n), static_cast<int32_t>(n)};
  return out;
}
}

int main() {
  // normalize_ip
  assert(ark_net::normalize_ip("203.0.113.9") == "203.0.113.9");
  assert(ark_net::normalize_ip("203.0.113.9:7777") == "203.0.113.9");
  assert(ark_net::normalize_ip("[2001:db8::1]:7777") == "2001:db8::1");
  assert(ark_net::normalize_ip("2001:db8::1") == "2001:db8::1");
  assert(!ark_net::normalize_ip("0.0.0.0"));
  assert(!ark_net::normalize_ip("256.1.1.1"));
  assert(!ark_net::normalize_ip("1.2.3"));
  assert(!ark_net::normalize_ip("1.2.3.4.5"));
  assert(!ark_net::normalize_ip("steam.123"));
  assert(!ark_net::normalize_ip(""));

  Block<0x200> controller_class, state_class, connection_class, net_field, exact_field, ping_field,
      float_type, byte_type, object_type;
  Block<0x200> controller, state, connection;
  Block<0x40> remote_addr;
  Block<0x100> chunk;
  void* chunks[1] = {chunk.bytes.data()};
  void* const* chunk_table = chunks;
  int32_t count = 8;
  std::array<void*, 80> vtable{};
  std::array<void*, 4> remote_vtable{};

  ark_net::Api api{};
  api.make_name = make_name;
  api.game_free = game_free;
  api.remote_address_impl = reinterpret_cast<uintptr_t>(&fake_remote);
  api.remote_addr_offset = 0x100;
  api.player_state_offset = 0x88;
  api.objects = {&chunk_table, &count};
  api.game_thread_tid = syscall(SYS_gettid);

  auto live = [&](void* object, int32_t index) {
    put<int32_t>(object, 0x0C, index);
    put<void*>(chunk.bytes.data(), static_cast<size_t>(index) * 16, object);
  };
  live(controller.bytes.data(), 1);
  live(state.bytes.data(), 2);
  live(connection.bytes.data(), 3);
  put<void*>(controller.bytes.data(), 0x10, controller_class.bytes.data());
  put<void*>(state.bytes.data(), 0x10, state_class.bytes.data());
  put<void*>(connection.bytes.data(), 0x10, connection_class.bytes.data());
  for (auto* klass : {&controller_class, &state_class, &connection_class})
    put<int32_t>(klass->bytes.data(), 0x40, 0x200);
  put<ark_entity::FName>(float_type.bytes.data(), 0x18, {31, 0});
  put<ark_entity::FName>(byte_type.bytes.data(), 0x18, {32, 0});
  // controller.NetConnection at +0x100, PlayerState at +0x88
  put<void*>(controller_class.bytes.data(), 0x38, net_field.bytes.data());
  put<ark_entity::FName>(net_field.bytes.data(), 0x18, {21, 0});
  put<int32_t>(net_field.bytes.data(), 0x4C, 0x100);
  put<void*>(net_field.bytes.data(), 0x10, object_type.bytes.data());
  put<void*>(controller.bytes.data(), 0x100, connection.bytes.data());
  put<void*>(controller.bytes.data(), 0x88, state.bytes.data());
  // connection: UIpConnection vtable, RemoteAddr set
  vtable[73] = reinterpret_cast<void*>(&fake_remote);
  put<void*>(connection.bytes.data(), 0, vtable.data());
  put<void*>(remote_addr.bytes.data(), 0, remote_vtable.data());
  put<void*>(connection.bytes.data(), 0x100, remote_addr.bytes.data());
  // state: ExactPing (float) at +0x120 -> Ping (byte) at +0x124
  put<void*>(state_class.bytes.data(), 0x38, exact_field.bytes.data());
  put<ark_entity::FName>(exact_field.bytes.data(), 0x18, {22, 0});
  put<int32_t>(exact_field.bytes.data(), 0x4C, 0x120);
  put<void*>(exact_field.bytes.data(), 0x10, float_type.bytes.data());
  put<void*>(exact_field.bytes.data(), 0x28, ping_field.bytes.data());
  put<ark_entity::FName>(ping_field.bytes.data(), 0x18, {23, 0});
  put<int32_t>(ping_field.bytes.data(), 0x4C, 0x124);
  put<void*>(ping_field.bytes.data(), 0x10, byte_type.bytes.data());
  put<float>(state.bytes.data(), 0x120, 47.6f);
  put<uint8_t>(state.bytes.data(), 0x124, 12);

  void* pc = controller.bytes.data();
  auto ip = ark_net::remote_ip(pc, api);
  assert(ip && *ip == "203.0.113.9" && !append_port_seen && frees == 1);
  assert(ark_net::ping_ms(pc, api) == 48);

  // A port in the engine's text is stripped; garbage is rejected (and freed).
  returned = L"203.0.113.9:7777";
  assert(ark_net::remote_ip(pc, api) == "203.0.113.9" && frees == 2);
  returned = L"unknown";
  const char* step = "";
  assert(!ark_net::remote_ip(pc, api, &step) && std::string(step) == "RemoteAddress-format" && frees == 3);
  returned = L"203.0.113.9";

  // Another slot-73 implementation (demo/child connection) is never called.
  vtable[73] = nullptr;
  assert(!ark_net::remote_ip(pc, api, &step) && std::string(step) == "IpConnection" && frees == 3);
  vtable[73] = reinterpret_cast<void*>(&fake_remote);
  // Unset RemoteAddr would be dereferenced by the engine: refused.
  put<void*>(connection.bytes.data(), 0x100, nullptr);
  assert(!ark_net::remote_ip(pc, api, &step) && std::string(step) == "RemoteAddr");
  put<void*>(connection.bytes.data(), 0x100, remote_addr.bytes.data());
  // A connection that is not a live object, or a null one.
  put<int32_t>(connection.bytes.data(), 0x0C, 5);
  assert(!ark_net::remote_ip(pc, api, &step) && std::string(step) == "NetConnection");
  put<int32_t>(connection.bytes.data(), 0x0C, 3);
  put<void*>(controller.bytes.data(), 0x100, nullptr);
  assert(!ark_net::remote_ip(pc, api, &step) && std::string(step) == "NetConnection");
  put<void*>(controller.bytes.data(), 0x100, connection.bytes.data());

  // ExactPing of another type is not trusted: falls back to Ping x 4.
  put<void*>(exact_field.bytes.data(), 0x10, byte_type.bytes.data());
  assert(ark_net::ping_ms(pc, api) == 48);
  put<uint8_t>(state.bytes.data(), 0x124, 30);
  assert(ark_net::ping_ms(pc, api) == 120);
  put<void*>(exact_field.bytes.data(), 0x10, float_type.bytes.data());
  put<float>(state.bytes.data(), 0x120, -1.f);
  assert(!ark_net::ping_ms(pc, api, &step) && std::string(step) == "ExactPing-value");
  put<float>(state.bytes.data(), 0x120, 47.6f);
  // A dead PlayerState is not read.
  put<int32_t>(state.bytes.data(), 0x0C, 6);
  assert(!ark_net::ping_ms(pc, api, &step) && std::string(step) == "PlayerState");
  put<int32_t>(state.bytes.data(), 0x0C, 2);

  // Off the game thread nothing is read or called.
  api.game_thread_tid = -1;
  assert(!ark_net::remote_ip(pc, api) && !ark_net::ping_ms(pc, api));
  api.game_thread_tid = syscall(SYS_gettid);
  assert(ark_net::remote_ip(pc, api) == "203.0.113.9" && ark_net::ping_ms(pc, api) == 48);
  return 0;
}
