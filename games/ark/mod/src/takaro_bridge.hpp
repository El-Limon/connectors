#pragma once
// The Takaro Generic connection inside the game process. Threads:
//   takaro-io      owns the WebSocket (connect, TLS, identify, heartbeat,
//                  reconnect) and the event pump (reads the native event ring,
//                  advances the cursor only on pong-confirmed delivery);
//   takaro-req x4  run Generic requests against the gate (may wait for the
//                  game thread, so never on the I/O thread);
//   takaro-maint   ban reconciliation, timed-ban expiry, state persistence.
// None of them is the game thread; the game thread only touches gate::Server.
#include "gate_state.hpp"
#include "json.hpp"
#include "takaro_adapter.hpp"
#include "takaro_store.hpp"
#include "ws_client.hpp"

#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <sys/eventfd.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

namespace takaro {

struct Config {
  std::string url = "wss://connect.takaro.io/";
  std::string registration_token;
  std::string identity_token;
  std::string server_name;
  std::string state_dir;
  std::string ban_file;   // default <state_dir>/ban-metadata.json
  std::string ca_file;
  bool wire_debug = false;
  std::chrono::milliseconds ping_interval{5000};
  std::chrono::milliseconds idle_timeout{20000};
  unsigned max_missed_pongs = 2;
  std::chrono::milliseconds reconnect_base{2000};
  std::chrono::milliseconds reconnect_max{60000};
  std::chrono::milliseconds pump_interval{250};
  std::chrono::milliseconds reconcile_interval{1000};
  size_t workers = 4;
};

using LogSink = std::function<void(const std::string&)>;

// Port of the sidecar's mapEvent. nullopt means the event cannot be sent
// safely and is skipped (logged), so one bad record never blocks the ring.
inline std::optional<json::Value> map_event(const gate::Event& e, KnownPlayers& known) {
  auto name_of = [&](const std::string& id, const std::string& name) {
    return !name.empty() && name != id ? name : known.name(id);
  };
  json::Value data = json::Value::object();
  if (e.type == "log") {
    const std::string& msg = e.message;
    if (js_trim(msg).empty() || msg.size() > 512) return std::nullopt;
    for (unsigned char c : msg) if (c < 0x20 || c == 0x7f) return std::nullopt;
    std::string lower = msg;
    for (auto& c : lower) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    for (const char* marker : {"commandline", "password", "token", "authorization", "auth=", "rcon", "steam_"})
      if (lower.find(marker) != std::string::npos) return std::nullopt;
    // Dotted quads (IPv4 addresses).
    for (size_t i = 0; i < msg.size(); ++i) {
      size_t j = i;
      int groups = 0;
      while (groups < 4) {
        size_t d = 0;
        while (j < msg.size() && std::isdigit(static_cast<unsigned char>(msg[j])) && d < 3) { ++j; ++d; }
        if (!d) break;
        ++groups;
        if (groups == 4) break;
        if (j >= msg.size() || msg[j] != '.') break;
        ++j;
      }
      if (groups == 4) return std::nullopt;
    }
    data.set("msg", json::str(msg));
    if (!e.timestamp.empty()) data.set("timestamp", json::str(e.timestamp));
    return data;
  }
  if (e.type != "chat-message" && e.type != "player-connected" && e.type != "player-disconnected" &&
      e.type != "player-death" && e.type != "entity-killed")
    return std::nullopt;
  if (!valid_steam64(e.id)) return std::nullopt;
  data.set("player", player_json(e.id, name_of(e.id, e.name)));
  if (e.type == "chat-message") {
    data.set("msg", json::str(e.message));
    data.set("channel", json::str("global"));
  }
  if (e.type == "player-death" && valid_steam64(e.attacker_id) && e.attacker_id != e.id)
    data.set("attacker", player_json(e.attacker_id, known.name(e.attacker_id)));
  if (e.type == "entity-killed") {
    if (js_trim(e.entity_code).empty() || js_trim(e.entity_name).empty()) return std::nullopt;
    data.set("entity", json::str(js_trim(e.entity_code)));
    // Takaro EventEntityKilled requires a string. Empty means the kill is
    // verified but the weapon is not identified; never invent one.
    data.set("weapon", json::str(""));
  }
  if (!e.timestamp.empty()) data.set("timestamp", json::str(e.timestamp));
  return data;
}

inline std::string redact_frame(const std::string& frame) {
  std::string out = frame;
  for (const char* key : {"\"registrationToken\":\"", "\"identityToken\":\"", "\"token\":\"", "\"password\":\""}) {
    size_t at = 0;
    while ((at = out.find(key, at)) != std::string::npos) {
      const size_t start = at + std::strlen(key);
      const size_t end = out.find('"', start);
      if (end == std::string::npos) break;
      out.replace(start, end - start, "<redacted>");
      at = start;
    }
  }
  if (out.size() > 2000) out = out.substr(0, 2000) + "...";
  return out;
}

class Bridge {
 public:
  Bridge(Config config, gate::Server& gate, LogSink log)
      : config_(std::move(config)), gate_(gate), log_(std::move(log)),
        known_(config_.state_dir.empty() ? std::string() : config_.state_dir + "/known-players.json"),
        bans_(gate_, BanFile(config_.ban_file.empty() && !config_.state_dir.empty()
                                 ? config_.state_dir + "/ban-metadata.json" : config_.ban_file),
              known_),
        adapter_(gate_, bans_, known_) {
    if (!config_.state_dir.empty()) cursor_path_ = config_.state_dir + "/event-cursor.json";
    const Cursor saved = cursor_path_.empty() ? Cursor{} : load_cursor(cursor_path_);
    if (saved.boot_id == gate_.boot_id()) { scan_ = persisted_ = saved.seq; }
  }

  ~Bridge() { stop(); }

  bool start() {
    if (running_.exchange(true)) return false;
    wake_fd_ = eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK);
    if (wake_fd_ < 0) { running_ = false; return false; }
    io_ = std::thread([this] { io_loop(); });
    for (size_t i = 0; i < config_.workers; ++i) workers_.emplace_back([this] { worker_loop(); });
    maint_ = std::thread([this] { maintenance_loop(); });
    return true;
  }

  void stop() {
    if (!running_.exchange(false)) return;
    wake();
    { std::lock_guard<std::mutex> lock(requests_mutex_); }
    requests_cv_.notify_all();
    { std::lock_guard<std::mutex> lock(maint_mutex_); }
    maint_cv_.notify_all();
    if (io_.joinable()) io_.join();
    for (auto& w : workers_) if (w.joinable()) w.join();
    workers_.clear();
    if (maint_.joinable()) maint_.join();
    if (wake_fd_ >= 0) { close(wake_fd_); wake_fd_ = -1; }
  }

  // Observable state (status file, tests).
  bool identified() const { return identified_.load(); }
  uint64_t persisted_cursor() const { return persisted_.load(); }
  std::string game_server_id() const { std::lock_guard<std::mutex> l(status_mutex_); return game_server_id_; }
  uint64_t connections() const { return connections_.load(); }

 private:
  struct Request {
    uint64_t epoch;
    std::string request_id;
    std::string action;
    json::Value args;
  };
  struct Outbound {
    uint64_t epoch;
    std::string text;
    std::function<void()> on_written;
  };
  struct InFlight { uint64_t send_id; uint64_t seq; };
  struct Ping { uint64_t id; uint64_t up_to; };

  Config config_;
  gate::Server& gate_;
  LogSink log_;
  KnownPlayers known_;
  BanManager bans_;
  Adapter adapter_;
  std::string cursor_path_;

  std::atomic<bool> running_{false};
  std::thread io_;
  std::vector<std::thread> workers_;
  std::thread maint_;
  int wake_fd_ = -1;

  // I/O thread state.
  ws::Connection conn_;
  std::atomic<uint64_t> epoch_{0};
  std::atomic<bool> identified_{false};
  std::atomic<uint64_t> connections_{0};
  unsigned attempts_ = 0;
  uint64_t send_id_ = 0;
  uint64_t confirmed_id_ = 0;
  uint64_t ping_id_ = 0;
  std::deque<Ping> pings_;
  std::deque<InFlight> in_flight_;
  std::atomic<uint64_t> scan_{0};
  std::atomic<uint64_t> persisted_{0};
  std::chrono::steady_clock::time_point last_activity_{};
  std::chrono::steady_clock::time_point next_ping_{};
  std::chrono::steady_clock::time_point last_eager_ping_{};
  std::chrono::steady_clock::time_point next_pump_{};
  bool truncation_logged_ = false;

  std::mutex out_mutex_;
  std::deque<Outbound> outbound_;

  std::mutex requests_mutex_;
  std::condition_variable requests_cv_;
  std::deque<Request> requests_;

  std::mutex maint_mutex_;
  std::condition_variable maint_cv_;

  mutable std::mutex status_mutex_;
  std::string game_server_id_;
  std::string last_error_;

  void log(const std::string& line) { if (log_) log_(line); }

  void wake() {
    if (wake_fd_ < 0) return;
    const uint64_t one = 1;
    (void)!write(wake_fd_, &one, sizeof(one));
  }

  // Any thread: queue a frame for the current connection. Dropped when the
  // connection it belongs to is gone (a late result must not cross a reconnect).
  bool post(uint64_t epoch, std::string text, std::function<void()> on_written = {}) {
    if (!identified_.load() || epoch != epoch_.load()) return false;
    {
      std::lock_guard<std::mutex> lock(out_mutex_);
      if (outbound_.size() >= 1024) return false;
      outbound_.push_back({epoch, std::move(text), std::move(on_written)});
    }
    wake();
    return true;
  }

  std::string identify_frame() const {
    json::Value payload = json::Value::object();
    payload.set("identityToken", json::str(config_.identity_token));
    if (!config_.registration_token.empty()) payload.set("registrationToken", json::str(config_.registration_token));
    if (!config_.server_name.empty()) payload.set("name", json::str(config_.server_name));
    return json::dump(json::obj({{"type", json::str("identify")}, {"payload", payload}}));
  }

  void set_error(const std::string& error) {
    std::lock_guard<std::mutex> lock(status_mutex_);
    last_error_ = error;
  }

  void disconnect(const std::string& why) {
    if (conn_.is_open()) log("takaro-ws-closed " + why);
    conn_.close_now();
    identified_ = false;
    ++epoch_;
    pings_.clear();
    in_flight_.clear();
    scan_ = persisted_.load();  // unconfirmed events are replayed (at-least-once)
    {
      std::lock_guard<std::mutex> lock(out_mutex_);
      outbound_.clear();
    }
    set_error(why);
  }

  std::chrono::milliseconds backoff() const {
    auto delay = config_.reconnect_base * (1LL << std::min(attempts_, 10u));
    return std::min<std::chrono::milliseconds>(delay, config_.reconnect_max);
  }

  void io_loop() {
    // OpenSSL writes may raise SIGPIPE on a reset socket; keep it on this thread.
    sigset_t block;
    sigemptyset(&block);
    sigaddset(&block, SIGPIPE);
    pthread_sigmask(SIG_BLOCK, &block, nullptr);
    auto next_attempt = std::chrono::steady_clock::now();
    const auto url = ws::parse_url(config_.url);
    if (!url) { log("takaro-ws-invalid-url"); set_error("invalid TAKARO_WS_URL"); }
    while (running_.load()) {
      const auto now = std::chrono::steady_clock::now();
      if (!conn_.is_open()) {
        if (!url || now < next_attempt) { idle_wait(next_attempt); continue; }
        std::string error;
        ws::Connection::Options options;
        options.ca_file = config_.ca_file;
        log("takaro-ws-connecting " + url->host);
        if (!conn_.open(*url, options, error)) {
          const auto delay = backoff();
          ++attempts_;
          log("takaro-ws-connect-failed " + error + " retry-ms=" + std::to_string(delay.count()));
          set_error(error);
          next_attempt = std::chrono::steady_clock::now() + delay;
          continue;
        }
        ++connections_;
        ++epoch_;
        pings_.clear();
        in_flight_.clear();
        scan_ = persisted_.load();
        last_activity_ = std::chrono::steady_clock::now();
        next_ping_ = last_activity_ + config_.ping_interval;
        log("takaro-ws-open sending identify");
        conn_.send_text(identify_frame());
      }
      pollfd fds[2] = {{conn_.fd(), conn_.poll_events(), 0}, {wake_fd_, POLLIN, 0}};
      const int r = ::poll(fds, 2, static_cast<int>(config_.pump_interval.count()));
      if (r < 0 && errno != EINTR) { disconnect("poll failed"); continue; }
      if (fds[1].revents & POLLIN) { uint64_t v; (void)!read(wake_fd_, &v, sizeof(v)); }
      if (!step()) {
        const auto delay = backoff();
        ++attempts_;
        next_attempt = std::chrono::steady_clock::now() + delay;
        log("takaro-ws-reconnect-in-ms " + std::to_string(delay.count()));
      }
    }
    if (conn_.is_open()) {
      conn_.send_close(1001);
      std::string ignored;
      conn_.flush(ignored);
    }
    conn_.close_now();
    identified_ = false;
  }

  void idle_wait(std::chrono::steady_clock::time_point until) {
    const auto now = std::chrono::steady_clock::now();
    const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(until - now).count();
    pollfd p{wake_fd_, POLLIN, 0};
    if (::poll(&p, 1, static_cast<int>(std::clamp<long long>(ms, 1, 500))) > 0) {
      uint64_t v; (void)!read(wake_fd_, &v, sizeof(v));
    }
  }

  // One I/O iteration. False = the connection was dropped.
  bool step() {
    std::string error;
    {
      std::deque<Outbound> batch;
      { std::lock_guard<std::mutex> lock(out_mutex_); batch.swap(outbound_); }
      for (auto& out : batch) {
        if (out.epoch != epoch_.load()) continue;
        if (config_.wire_debug) log("takaro-wire SEND " + redact_frame(out.text));
        if (!conn_.send_text(out.text, std::move(out.on_written))) log("takaro-ws-send-buffer-full dropped");
      }
    }
    std::vector<ws::Frame> frames;
    const auto status = conn_.read(frames, error);
    for (auto& frame : frames) {
      last_activity_ = std::chrono::steady_clock::now();
      if (frame.kind == ws::Frame::Kind::text) handle_text(frame.payload);
      else if (frame.kind == ws::Frame::Kind::pong) note_pong(frame.payload);
    }
    if (status != ws::Connection::ReadStatus::ok) {
      disconnect(status == ws::Connection::ReadStatus::closed ? "closed by peer" : error);
      return false;
    }
    if (!conn_.is_open()) { disconnect("closed"); return false; }
    const auto now = std::chrono::steady_clock::now();
    if (identified_.load() && now >= next_pump_) {
      next_pump_ = now + config_.pump_interval;
      pump();
    }
    if (now >= next_ping_) {
      next_ping_ = now + config_.ping_interval;
      if (pings_.size() >= config_.max_missed_pongs) {
        log("takaro-ws-heartbeat-missed " + std::to_string(pings_.size()) + " pings unanswered");
        disconnect("heartbeat timeout");
        return false;
      }
      if (now - last_activity_ > config_.idle_timeout) {
        log("takaro-ws-idle-timeout");
        disconnect("idle timeout");
        return false;
      }
      send_ping();
    }
    if (!conn_.flush(error)) { disconnect(error); return false; }
    return true;
  }

  void send_ping() {
    ++ping_id_;
    pings_.push_back({ping_id_, send_id_});
    conn_.send_ping(std::to_string(ping_id_));
  }

  void note_pong(const std::string& payload) {
    for (size_t i = 0; i < pings_.size(); ++i) {
      if (std::to_string(pings_[i].id) != payload) continue;
      // Ordered transport: our ping was processed after every frame written before it.
      const uint64_t up_to = pings_[i].up_to;
      pings_.erase(pings_.begin(), pings_.begin() + static_cast<long>(i) + 1);
      if (up_to > confirmed_id_) confirm(up_to);
      return;
    }
  }

  void confirm(uint64_t send_id) {
    confirmed_id_ = send_id;
    uint64_t seq = persisted_.load();
    while (!in_flight_.empty() && in_flight_.front().send_id <= send_id) {
      seq = in_flight_.front().seq;
      in_flight_.pop_front();
    }
    if (seq > persisted_.load()) {
      if (!cursor_path_.empty()) (void)save_cursor(cursor_path_, Cursor{gate_.boot_id(), seq});
      persisted_ = seq;
    }
  }

  void pump() {
    const auto page = gate_.events_since(scan_.load(), 64);
    if (page.truncated && !truncation_logged_) {
      log("takaro-event-ring-truncated before seq " + std::to_string(scan_.load()) + "; some events were lost");
      truncation_logged_ = true;
    }
    bool sent = false;
    for (const auto& event : page.events) {
      const auto data = map_event(event, known_);
      if (!data) {
        log("takaro-event-skipped seq=" + std::to_string(event.seq) + " type=" + event.type);
        scan_ = event.seq;
        in_flight_.push_back({send_id_, event.seq});  // confirmed with the next real send
        continue;
      }
      if (!event.id.empty() && !event.name.empty()) known_.remember(event.id, event.name);
      const std::string frame = json::dump(json::obj({{"type", json::str("gameEvent")},
          {"payload", json::obj({{"type", json::str(event.type)}, {"data", *data}})}}));
      if (config_.wire_debug) log("takaro-wire SEND " + redact_frame(frame));
      if (!conn_.send_text(frame)) break;  // buffer full: retry on the next pump
      ++send_id_;
      scan_ = event.seq;
      in_flight_.push_back({send_id_, event.seq});
      sent = true;
    }
    // Confirm promptly instead of waiting for the next heartbeat.
    const auto now = std::chrono::steady_clock::now();
    if ((sent || (!in_flight_.empty() && pings_.empty())) && now - last_eager_ping_ >= std::chrono::milliseconds(1000)) {
      last_eager_ping_ = now;
      send_ping();
    }
  }

  void handle_text(const std::string& raw) {
    if (config_.wire_debug) log("takaro-wire RECV " + redact_frame(raw));
    const auto message = json::parse(raw);
    if (!message || !message->is_object()) { log("takaro-ws-invalid-message"); return; }
    const json::Value* type = message->get("type");
    const std::string kind = type && type->is_string() ? type->as_string() : std::string();
    if (kind == "identifyResponse") {
      const json::Value& payload = as_record(message->get("payload"));
      if (payload.get("error")) {
        log("takaro-identify-failed " + redact_frame(json::dump(*payload.get("error"))));
        set_error("identify rejected");
        conn_.send_close(1000);
        return;
      }
      std::string id;
      if (const auto* gsid = payload.get("gameServerId"); gsid && gsid->is_string()) id = gsid->as_string();
      else if (const auto* server = as_record(payload.get("server")).get("id"); server && server->is_string()) id = server->as_string();
      {
        std::lock_guard<std::mutex> lock(status_mutex_);
        game_server_id_ = id;
        last_error_.clear();
      }
      attempts_ = 0;
      identified_ = true;
      next_pump_ = std::chrono::steady_clock::now();
      log("takaro-identified gameServerId=" + id);
      return;
    }
    if (kind == "request") {
      const json::Value* rid = message->get("requestId");
      if (!rid || !rid->is_string() || rid->as_string().empty()) return;
      Request request;
      request.epoch = epoch_.load();
      request.request_id = rid->as_string();
      const json::Value& payload = as_record(message->get("payload"));
      if (const auto* action = payload.get("action"); action && action->is_string()) request.action = action->as_string();
      if (payload.has_key("args")) {
        for (const auto& [k, v] : payload.as_object()) if (k == "args") request.args = v;
      }
      {
        std::lock_guard<std::mutex> lock(requests_mutex_);
        if (requests_.size() >= 64) { log("takaro-request-queue-full withheld"); return; }
        requests_.push_back(std::move(request));
      }
      requests_cv_.notify_one();
      return;
    }
    if (kind == "ping") { conn_.send_text("{\"type\":\"pong\"}"); return; }
    if (kind == "error") { log("takaro-error " + redact_frame(raw)); return; }
  }

  void worker_loop() {
    while (running_.load()) {
      Request request;
      {
        std::unique_lock<std::mutex> lock(requests_mutex_);
        requests_cv_.wait_for(lock, std::chrono::milliseconds(500), [&] { return !requests_.empty() || !running_.load(); });
        if (!running_.load()) return;
        if (requests_.empty()) continue;
        request = std::move(requests_.front());
        requests_.pop_front();
      }
      const auto started = std::chrono::steady_clock::now();
      std::string outcome = "ok";
      try {
        if (request.action.empty()) withhold("request has no action");
        const json::Value args = normalize_args(&request.args);
        Adapter::AfterWrite after_write;
        json::Value payload = adapter_.handle(request.action, args, &after_write);
        const std::string frame = json::dump(json::obj({{"type", json::str("response")},
            {"requestId", json::str(request.request_id)}, {"payload", std::move(payload)}}));
        if (!post(request.epoch, frame, after_write)) outcome = "connection-gone";
      } catch (const Withhold& w) {
        outcome = std::string("withheld: ") + w.what();
      } catch (const std::exception& e) {
        outcome = std::string("withheld: internal ") + e.what();
      }
      const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - started).count();
      const std::string rid = request.request_id.size() <= 128 ? request.request_id : request.request_id.substr(0, 128);
      if (outcome != "ok" || config_.wire_debug)
        log("takaro-request action=" + request.action.substr(0, 64) + " id=" + rid + " ms=" + std::to_string(ms) +
            " result=" + outcome);
    }
  }

  void write_status() {
    if (config_.state_dir.empty()) return;
    json::Value status = json::Value::object();
    status.set("bootId", json::str(gate_.boot_id()));
    status.set("utc", json::str(gate::utc_now()));
    status.set("identified", json::Value::boolean(identified_.load()));
    status.set("connections", json::num(static_cast<double>(connections_.load())));
    {
      std::lock_guard<std::mutex> lock(status_mutex_);
      status.set("gameServerId", game_server_id_.empty() ? json::Value::null() : json::str(game_server_id_));
      status.set("lastError", last_error_.empty() ? json::Value::null() : json::str(last_error_));
    }
    status.set("eventCursor", json::num(static_cast<double>(persisted_.load())));
    status.set("eventScanCursor", json::num(static_cast<double>(scan_.load())));
    status.set("latestEventSeq", json::num(static_cast<double>(gate_.latest_sequence())));
    status.set("banReconciliationReady", json::Value::boolean(bans_.ready()));
    const auto h = gate_.health();
    status.set("gameReady", json::Value::boolean(h.ready));
    json::Value players = json::Value::array();
    for (const auto& p : gate_.players()) players.push(json::str(p.id));
    status.set("onlinePlayers", std::move(players));
    (void)write_atomic(config_.state_dir + "/status.json", json::dump(status) + "\n", 0644);
  }

  void maintenance_loop() {
    auto next_status = std::chrono::steady_clock::now();
    bool last_ok = true;
    while (running_.load()) {
      if (gate_.health().ready) {
        const bool ok = bans_.reconcile();
        if (ok != last_ok) log(ok ? "takaro-ban-reconciliation-ready" : "takaro-ban-reconciliation-failed");
        last_ok = ok;
      }
      for (const auto& p : gate_.players()) known_.remember(p.id, p.name);
      known_.flush();
      const auto now = std::chrono::steady_clock::now();
      if (now >= next_status) { next_status = now + std::chrono::seconds(5); write_status(); }
      std::unique_lock<std::mutex> lock(maint_mutex_);
      maint_cv_.wait_for(lock, config_.reconcile_interval, [&] { return !running_.load(); });
    }
    write_status();
  }
};

}  // namespace takaro
