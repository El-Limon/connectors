#pragma once

// ShooterGameServer 21241282 only. ShooterGameMode::SaveWorld at vtable+D80
// waits for pending writes and reaches the completion log path before return.
// Its two early guards must be clear before a void return can prove completion.
#include "moderation_bindings.hpp"
#include <array>
#include <cstdint>
#include <cstring>
#include <sys/syscall.h>
#include <unistd.h>

namespace ark_save {

enum class Status { invalid, guarded, completed };
struct Api {
  ark_moderation::Api moderation{};
  long game_thread_tid = 0;
  uintptr_t expected_target = 0xE64830;
  uintptr_t text_min = 0x648400, text_max = 0x3F6170F;
  std::array<uint8_t, 20> prologue{
      0x55,0x48,0x89,0xE5,0x41,0x57,0x41,0x56,0x41,0x55,
      0x41,0x54,0x53,0x48,0x81,0xEC,0x48,0x02,0x00,0x00};
};

inline Status save_world(void* world, const Api& api) {
  if (api.game_thread_tid <= 0 || syscall(SYS_gettid) != api.game_thread_tid)
    return Status::invalid;
  void* mode = ark_moderation::game_mode(world, api.moderation);
  if (!mode) return Status::invalid;
  auto* vtable = *reinterpret_cast<const uintptr_t* const*>(mode);
  if (!ark_moderation::plausible(vtable)) return Status::invalid;
  const uintptr_t target = vtable[0xD80 / sizeof(void*)];
  if (target != api.expected_target || target < api.text_min || target >= api.text_max ||
      std::memcmp(reinterpret_cast<const void*>(target), api.prologue.data(),
                  api.prologue.size()) != 0) return Status::invalid;
  const auto base = reinterpret_cast<uintptr_t>(mode);
  if (*reinterpret_cast<const uint8_t*>(base + 0xCF8) != 0 ||
      *reinterpret_cast<const int32_t*>(base + 0x73C) != -1) return Status::guarded;
  reinterpret_cast<void (*)(void*, int)>(target)(mode, 0);
  return Status::completed;
}

} // namespace ark_save
