#include "native_map.h"

#include "common.h"

#include <nlohmann/json.hpp>

#include <cmath>
#include <cstdint>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <sys/stat.h>

namespace NativeMap {
namespace {
using Json = nlohmann::json;

constexpr int kMaxZoomLimit = 12;
constexpr std::streamsize kMaxTileBytes = 2 * 1024 * 1024;
constexpr long long kMaxTileIndex = 1LL << 20;

struct Info {
    bool enabled = false;
    double mapBlockSize = 128, mapSizeX = 0, mapSizeY = 0, mapSizeZ = 0;
    int maxZoom = 0;
};

bool IsDirectory(const std::string& path) {
    struct stat st{};
    return stat(path.c_str(), &st) == 0 && S_ISDIR(st.st_mode);
}

bool IsFile(const std::string& path) {
    struct stat st{};
    return stat(path.c_str(), &st) == 0 && S_ISREG(st.st_mode);
}

bool ReadAll(const std::string& path, std::string& out, std::streamsize limit) {
    std::ifstream in(path, std::ios::binary);
    if (!in) return false;
    in.seekg(0, std::ios::end);
    const std::streamsize size = in.tellg();
    if (size < 0 || size > limit) return false;
    in.seekg(0, std::ios::beg);
    out.assign(static_cast<size_t>(size), '\0');
    return size == 0 || static_cast<bool>(in.read(&out[0], size));
}

bool PositiveFinite(const Json& v) { return v.is_number() && std::isfinite(v.get<double>()) && v.get<double>() > 0; }
bool NonNegativeFinite(const Json& v) { return v.is_number() && std::isfinite(v.get<double>()) && v.get<double>() >= 0; }

// A map is enabled only when map.json is well formed and the deepest zoom level exists on disk.
Info Load(const std::string& dir) {
    Info info;
    if (dir.empty()) return info;
    std::string text;
    if (!ReadAll(dir + "/map.json", text, 64 * 1024)) return info;
    const Json j = Json::parse(text, nullptr, false);
    if (!j.is_object()) return info;
    const Json zoom = j.value("maxZoom", Json());
    if (!zoom.is_number_integer() || zoom.get<int>() < 0 || zoom.get<int>() > kMaxZoomLimit) return info;
    const Json block = j.value("mapBlockSize", Json());
    if (!PositiveFinite(block)) return info;
    for (const char* key : {"mapSizeX", "mapSizeY", "mapSizeZ"})
        if (!NonNegativeFinite(j.value(key, Json()))) return info;
    if (!IsDirectory(dir + "/" + std::to_string(zoom.get<int>()))) return info;
    info.enabled = true;
    info.maxZoom = zoom.get<int>();
    info.mapBlockSize = block.get<double>();
    info.mapSizeX = j["mapSizeX"].get<double>();
    info.mapSizeY = j["mapSizeY"].get<double>();
    info.mapSizeZ = j["mapSizeZ"].get<double>();
    return info;
}

// Takaro sends numbers; a module or a hand-made call may send numeric strings. Only integers count.
long long TileIndex(const Json& args, const char* key) {
    const Json v = args.is_object() ? args.value(key, Json()) : Json();
    double d = 0;
    if (v.is_number()) {
        d = v.get<double>();
    } else if (v.is_string()) {
        const std::string s = v.get<std::string>();
        size_t used = 0;
        try {
            d = std::stod(s, &used);
        } catch (const std::exception&) {
            used = 0;
        }
        if (used == 0 || used != s.size()) throw std::runtime_error(std::string("getMapTile '") + key + "' must be an integer");
    } else {
        throw std::runtime_error(std::string("getMapTile requires integer '") + key + "'");
    }
    if (!std::isfinite(d) || d != std::floor(d) || std::fabs(d) > static_cast<double>(kMaxTileIndex))
        throw std::runtime_error(std::string("getMapTile '") + key + "' must be an integer");
    return static_cast<long long>(d);
}

bool LooksLikePng(const std::string& bytes) {
    static const char kSig[] = "\x89PNG\r\n\x1a\n";
    return bytes.size() > 8 && bytes.compare(0, 8, std::string(kSig, 8)) == 0;
}
}  // namespace

std::string Directory() {
    std::string dir = ConfigValue("TAKARO_MAP_DIR", "mapDir", "");
    if (dir.empty()) dir = PluginDataDir() + "/map";
    return dir;
}

std::string InfoJson(const std::string& dir) {
    const Info info = Load(dir);
    return Json{{"enabled", info.enabled},
                {"mapBlockSize", info.mapBlockSize},
                {"maxZoom", info.maxZoom},
                {"mapSizeX", info.mapSizeX},
                {"mapSizeY", info.mapSizeY},
                {"mapSizeZ", info.mapSizeZ}}
        .dump();
}

std::string TileJson(const std::string& dir, const std::string& argsJson) {
    const Info info = Load(dir);
    if (!info.enabled)
        throw std::runtime_error("Map tiles are not installed: render them with scripts/build-map-tiles.py "
                                 "and copy them to " + (dir.empty() ? std::string("TAKARO_MAP_DIR") : dir));
    const Json args = Json::parse(argsJson.empty() ? std::string("{}") : argsJson, nullptr, false);
    const long long x = TileIndex(args, "x"), y = TileIndex(args, "y"), z = TileIndex(args, "z");
    if (z < 0 || z > info.maxZoom)
        throw std::runtime_error("Map tile zoom " + std::to_string(z) + " is outside 0.." + std::to_string(info.maxZoom));
    const std::string path = dir + "/" + std::to_string(z) + "/" + std::to_string(x) + "/" + std::to_string(y) + ".png";
    std::string bytes;
    if (!IsFile(path) || !ReadAll(path, bytes, kMaxTileBytes)) {
        // Inside the zoom range but beyond the rendered image: an empty tile, not an error, so a
        // viewer panning past the map edge does not retry a failure for every tile.
        if (!IsFile(dir + "/blank.png") || !ReadAll(dir + "/blank.png", bytes, kMaxTileBytes))
            throw std::runtime_error("Map tile " + std::to_string(z) + "/" + std::to_string(x) + "/" +
                                     std::to_string(y) + " has not been rendered");
    }
    if (!LooksLikePng(bytes)) throw std::runtime_error("Map tile file is not a PNG: " + path);
    return Json(Base64(bytes)).dump();
}

std::string Base64(const std::string& bytes) {
    static const char kAlphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string out;
    out.reserve(((bytes.size() + 2) / 3) * 4);
    size_t i = 0;
    for (; i + 2 < bytes.size(); i += 3) {
        const uint32_t n = (static_cast<uint8_t>(bytes[i]) << 16) | (static_cast<uint8_t>(bytes[i + 1]) << 8) |
                           static_cast<uint8_t>(bytes[i + 2]);
        out += kAlphabet[(n >> 18) & 63];
        out += kAlphabet[(n >> 12) & 63];
        out += kAlphabet[(n >> 6) & 63];
        out += kAlphabet[n & 63];
    }
    if (i + 1 == bytes.size()) {
        const uint32_t n = static_cast<uint8_t>(bytes[i]) << 16;
        out += kAlphabet[(n >> 18) & 63];
        out += kAlphabet[(n >> 12) & 63];
        out += "==";
    } else if (i + 2 == bytes.size()) {
        const uint32_t n = (static_cast<uint8_t>(bytes[i]) << 16) | (static_cast<uint8_t>(bytes[i + 1]) << 8);
        out += kAlphabet[(n >> 18) & 63];
        out += kAlphabet[(n >> 12) & 63];
        out += kAlphabet[(n >> 6) & 63];
        out += '=';
    }
    return out;
}

}  // namespace NativeMap
