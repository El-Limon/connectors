#pragma once
// Durable connector state in the state directory: ban metadata (same version-1
// format the 0.1 sidecar wrote), known player names, the event cursor and a
// status snapshot. Every write is temp file + fsync + rename + directory fsync.
#include "json.hpp"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <ctime>
#include <map>
#include <mutex>
#include <optional>
#include <random>
#include <stdexcept>
#include <string>

namespace takaro {

inline bool make_dirs(const std::string& path) {
  if (path.empty()) return false;
  std::string partial;
  for (size_t i = 0; i < path.size(); ++i) {
    partial.push_back(path[i]);
    if ((path[i] == '/' && i > 0) || i + 1 == path.size()) {
      if (mkdir(partial.c_str(), 0700) != 0 && errno != EEXIST) return false;
    }
  }
  struct stat st{};
  return stat(path.c_str(), &st) == 0 && S_ISDIR(st.st_mode);
}

inline std::string dirname_of(const std::string& path) {
  const size_t slash = path.rfind('/');
  if (slash == std::string::npos) return ".";
  return slash == 0 ? "/" : path.substr(0, slash);
}

inline bool write_atomic(const std::string& path, const std::string& text, mode_t mode = 0600) {
  const std::string dir = dirname_of(path);
  if (!make_dirs(dir)) return false;
  static std::atomic<uint64_t> counter{0};
  const std::string tmp = path + "." + std::to_string(getpid()) + "." + std::to_string(++counter) + ".tmp";
  const int fd = open(tmp.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, mode);
  if (fd < 0) return false;
  size_t written = 0;
  while (written < text.size()) {
    const ssize_t n = write(fd, text.data() + written, text.size() - written);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) { close(fd); unlink(tmp.c_str()); return false; }
    written += static_cast<size_t>(n);
  }
  if (fsync(fd) != 0) { close(fd); unlink(tmp.c_str()); return false; }
  close(fd);
  if (rename(tmp.c_str(), path.c_str()) != 0) { unlink(tmp.c_str()); return false; }
  const int dfd = open(dir.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
  if (dfd >= 0) { (void)fsync(dfd); close(dfd); }
  return true;
}

// nullopt when absent; throws when present but unreadable.
inline std::optional<std::string> read_file(const std::string& path, size_t limit = 8 * 1024 * 1024) {
  const int fd = open(path.c_str(), O_RDONLY | O_CLOEXEC);
  if (fd < 0) {
    if (errno == ENOENT) return std::nullopt;
    throw std::runtime_error("cannot open " + path);
  }
  std::string text;
  char buf[16384];
  for (;;) {
    const ssize_t n = read(fd, buf, sizeof(buf));
    if (n < 0 && errno == EINTR) continue;
    if (n < 0) { close(fd); throw std::runtime_error("cannot read " + path); }
    if (n == 0) break;
    text.append(buf, static_cast<size_t>(n));
    if (text.size() > limit) { close(fd); throw std::runtime_error("file too large " + path); }
  }
  close(fd);
  return text;
}

inline int64_t now_ms() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
      std::chrono::system_clock::now().time_since_epoch()).count();
}

// ISO 8601 with a mandatory timezone, like the sidecar's validation.
inline std::optional<int64_t> parse_iso_ms(const std::string& s) {
  int year, month, day, hour, minute, second;
  if (s.size() < 20) return std::nullopt;
  if (std::sscanf(s.c_str(), "%4d-%2d-%2dT%2d:%2d:%2d", &year, &month, &day, &hour, &minute, &second) != 6)
    return std::nullopt;
  for (size_t i : {4u, 7u}) if (s[i] != '-') return std::nullopt;
  if (s[10] != 'T' || s[13] != ':' || s[16] != ':') return std::nullopt;
  for (size_t i : {0u,1u,2u,3u,5u,6u,8u,9u,11u,12u,14u,15u,17u,18u})
    if (s[i] < '0' || s[i] > '9') return std::nullopt;
  size_t i = 19;
  int64_t millis = 0;
  if (i < s.size() && s[i] == '.') {
    ++i;
    int digits = 0;
    while (i < s.size() && s[i] >= '0' && s[i] <= '9') {
      if (digits < 3) millis = millis * 10 + (s[i] - '0');
      ++digits; ++i;
    }
    if (digits < 1 || digits > 3) return std::nullopt;
    for (int k = digits; k < 3; ++k) millis *= 10;
  }
  int64_t offset_min = 0;
  if (i < s.size() && s[i] == 'Z') { ++i; }
  else if (i + 6 == s.size() && (s[i] == '+' || s[i] == '-') && s[i + 3] == ':') {
    int oh, om;
    if (std::sscanf(s.c_str() + i + 1, "%2d:%2d", &oh, &om) != 2 || oh > 23 || om > 59) return std::nullopt;
    offset_min = (oh * 60 + om) * (s[i] == '+' ? 1 : -1);
    i += 6;
  } else return std::nullopt;
  if (i != s.size()) return std::nullopt;
  if (month < 1 || month > 12 || day < 1 || day > 31 || hour > 23 || minute > 59 || second > 59) return std::nullopt;
  struct tm tm{};
  tm.tm_year = year - 1900; tm.tm_mon = month - 1; tm.tm_mday = day;
  tm.tm_hour = hour; tm.tm_min = minute; tm.tm_sec = second;
  const time_t t = timegm(&tm);
  struct tm back{};
  gmtime_r(&t, &back);
  if (back.tm_mday != day || back.tm_mon != month - 1) return std::nullopt;  // e.g. 31 Feb
  return (static_cast<int64_t>(t) - offset_min * 60) * 1000 + millis;
}

inline std::string iso_ms(int64_t ms) {
  const time_t t = static_cast<time_t>(ms / 1000);
  struct tm tm{};
  gmtime_r(&t, &tm);
  char buf[64];
  std::snprintf(buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ", tm.tm_year + 1900, tm.tm_mon + 1,
                tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec, static_cast<int>(ms % 1000));
  return buf;
}

inline bool valid_steam64(const std::string& id) {
  return id.size() == 17 && id.find_first_not_of("0123456789") == std::string::npos;
}

inline size_t codepoints(const std::string& s) {
  size_t n = 0;
  for (unsigned char c : s) if ((c & 0xc0) != 0x80) ++n;
  return n;
}

struct BanMeta {
  std::string reason;
  std::optional<std::string> expires_at;  // ISO, nullopt = permanent
  bool pending = false;
  int64_t pending_since = 0;
};

class BanFile {
 public:
  explicit BanFile(std::string path = {}) : path_(std::move(path)) {}
  const std::string& path() const { return path_; }

  // Throws on a corrupt file: the bridge refuses to manage bans blindly.
  std::map<std::string, BanMeta> load() const {
    std::map<std::string, BanMeta> out;
    if (path_.empty()) return out;
    const auto text = read_file(path_);
    if (!text) return out;
    const auto doc = json::parse(*text);
    if (!doc || !doc->is_object()) throw std::runtime_error("ban metadata is invalid");
    const auto* version = doc->get("version");
    const auto* bans = doc->get("bans");
    if (!version || !version->is_number() || version->as_number() != 1 || !bans || !bans->is_object())
      throw std::runtime_error("ban metadata version or entries are invalid");
    if (bans->as_object().size() > 4096) throw std::runtime_error("ban metadata has too many entries");
    for (const auto& [id, row] : bans->as_object()) {
      if (!valid_steam64(id) || !row.is_object()) throw std::runtime_error("ban metadata has an invalid entry");
      const auto* reason = row.get("reason");
      const auto* state = row.get("state");
      if (!reason || !reason->is_string() || codepoints(reason->as_string()) > 10000 ||
          reason->as_string().size() > 40000 || !state || !state->is_string() ||
          (state->as_string() != "pending" && state->as_string() != "active"))
        throw std::runtime_error("ban metadata has an invalid reason or state");
      BanMeta meta;
      meta.reason = reason->as_string();
      meta.pending = state->as_string() == "pending";
      if (const auto* expires = row.get("expiresAt")) {
        if (!expires->is_string() || !parse_iso_ms(expires->as_string()))
          throw std::runtime_error("ban metadata has an invalid expiry");
        meta.expires_at = expires->as_string();
      }
      if (meta.pending) {
        const auto* since = row.get("pendingSince");
        if (!since || !since->is_number() || since->as_number() <= 0)
          throw std::runtime_error("ban metadata has an invalid pending time");
        meta.pending_since = static_cast<int64_t>(since->as_number());
      }
      out[id] = std::move(meta);
    }
    return out;
  }

  bool save(const std::map<std::string, BanMeta>& state) const {
    if (path_.empty()) return true;
    json::Value bans = json::Value::object();
    for (const auto& [id, meta] : state) {
      json::Value row = json::Value::object();
      row.set("reason", json::str(meta.reason));
      row.set("expiresAt", meta.expires_at ? json::str(*meta.expires_at) : json::Value::null());
      row.set("state", json::str(meta.pending ? "pending" : "active"));
      if (meta.pending) row.set("pendingSince", json::num(static_cast<double>(meta.pending_since)));
      bans.set(id, std::move(row));
    }
    json::Value doc = json::Value::object();
    doc.set("version", json::num(1));
    doc.set("bans", std::move(bans));
    return write_atomic(path_, json::dump(doc));
  }

 private:
  std::string path_;
};

// Last seen Steam persona per Steam64. Takaro renames a player to a listBans
// entry's name, so a ban row must carry the real name, never the raw id.
class KnownPlayers {
 public:
  explicit KnownPlayers(std::string path = {}) : path_(std::move(path)) {
    if (path_.empty()) return;
    try {
      const auto text = read_file(path_);
      if (!text) return;
      const auto doc = json::parse(*text);
      const auto* players = doc ? doc->get("players") : nullptr;
      if (!players || !players->is_object()) return;
      for (const auto& [id, row] : players->as_object()) {
        const auto* name = row.get("name");
        if (valid_steam64(id) && name && name->is_string() && !name->as_string().empty() &&
            names_.size() < 100000)
          names_[id] = name->as_string();
      }
    } catch (...) {
      names_.clear();
    }
  }

  void remember(const std::string& id, const std::string& name) {
    if (!valid_steam64(id) || name.empty() || name == id) return;
    std::lock_guard<std::mutex> lock(mutex_);
    auto& slot = names_[id];
    if (slot != name) { slot = name; dirty_ = true; }
  }

  std::string name(const std::string& id) const {
    std::lock_guard<std::mutex> lock(mutex_);
    auto it = names_.find(id);
    return it == names_.end() ? std::string() : it->second;
  }

  void flush() {
    std::map<std::string, std::string> copy;
    {
      std::lock_guard<std::mutex> lock(mutex_);
      if (!dirty_ || path_.empty()) return;
      copy = names_;
      dirty_ = false;
    }
    json::Value players = json::Value::object();
    for (const auto& [id, name] : copy) players.set(id, json::obj({{"name", json::str(name)}}));
    json::Value doc = json::Value::object();
    doc.set("version", json::num(1));
    doc.set("players", std::move(players));
    if (!write_atomic(path_, json::dump(doc))) {
      std::lock_guard<std::mutex> lock(mutex_);
      dirty_ = true;
    }
  }

 private:
  std::string path_;
  mutable std::mutex mutex_;
  std::map<std::string, std::string> names_;
  bool dirty_ = false;
};

struct Cursor {
  std::string boot_id;
  uint64_t seq = 0;
};

inline Cursor load_cursor(const std::string& path) {
  Cursor cursor;
  try {
    const auto text = read_file(path, 65536);
    if (!text) return cursor;
    const auto doc = json::parse(*text);
    if (!doc) return cursor;
    const auto* boot = doc->get("bootId");
    const auto* seq = doc->get("seq");
    if (boot && boot->is_string() && seq && seq->is_number() && seq->as_number() >= 0) {
      cursor.boot_id = boot->as_string();
      cursor.seq = static_cast<uint64_t>(seq->as_number());
    }
  } catch (...) {
  }
  return cursor;
}

inline bool save_cursor(const std::string& path, const Cursor& cursor) {
  return write_atomic(path, json::dump(json::obj({{"bootId", json::str(cursor.boot_id)},
                                                  {"seq", json::num(static_cast<double>(cursor.seq))}})));
}

inline std::string random_uuid() {
  unsigned char b[16];
  std::random_device device;
  for (auto& x : b) x = static_cast<unsigned char>(device());
  b[6] = static_cast<unsigned char>((b[6] & 0x0f) | 0x40);
  b[8] = static_cast<unsigned char>((b[8] & 0x3f) | 0x80);
  char out[37];
  std::snprintf(out, sizeof(out), "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
                b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7], b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15]);
  return out;
}

}  // namespace takaro
