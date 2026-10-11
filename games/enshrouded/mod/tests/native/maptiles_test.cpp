// getMapInfo / getMapTile from an on-disk tile pack (src/native/maptiles.cpp), directly and through
// ExecuteAction so the action names and the payload shapes Takaro receives are pinned.
#include "fake_plugin.h"
#include "native/adapter.h"
#include "native/config.h"
#include "native/fileio.h"
#include "native/maptiles.h"
#include "testlib.h"

#include <map>
#include <sys/stat.h>

using namespace native;

namespace t {
std::string TempDir(const std::string& tag);
void Group(const char* name);
}  // namespace t

namespace {

const std::string kPng = std::string("\x89PNG\r\n\x1a\n", 8) + "tile-body";

void Write(const std::string& path, const std::string& data) {
    std::string err;
    EnsureDirectory(DirName(path), err);
    CHECK_MSG(AtomicWriteFile(path, data, err), path + ": " + err);
}

ActionOutcome Run(const std::string& mapDir, const std::string& action, const char* args) {
    t::FakePlugin fake;
    ActionView view;
    view.mapDir = mapDir;
    JsonValue a = t::J(args);
    return ExecuteAction(fake, action, a, view, 0);
}

const char* kManifest =
    R"({"format":"takaro-map-tiles/1","tileSize":128,"minZoom":0,"maxZoom":4,"mapSizeX":10240,"mapSizeY":4096,"mapSizeZ":10240})";

void Base64Tests() {
    t::Group("maptiles base64");
    CHECK_EQ(Base64Encode(""), std::string(""));
    CHECK_EQ(Base64Encode("f"), std::string("Zg=="));
    CHECK_EQ(Base64Encode("fo"), std::string("Zm8="));
    CHECK_EQ(Base64Encode("foo"), std::string("Zm9v"));
    CHECK_EQ(Base64Encode("foob"), std::string("Zm9vYg=="));
    CHECK_EQ(Base64Encode("fooba"), std::string("Zm9vYmE="));
    CHECK_EQ(Base64Encode("foobar"), std::string("Zm9vYmFy"));
    CHECK_EQ(Base64Encode(std::string("\xff\x00\x80", 3)), std::string("/wCA"));
}

void NoPackTests() {
    t::Group("maptiles no pack");
    std::string dir = t::TempDir("map-none");
    ActionOutcome info = Run(dir, "getMapInfo", "{}");
    CHECK(info.ok);
    CHECK(info.payload.get("enabled") && info.payload.get("enabled")->type == JsonValue::Bool &&
          !info.payload.get("enabled")->b);  // a real boolean: Takaro's MapInfoDTO rejects anything else
    CHECK(Num(info.payload.get("maxZoom")) && *Num(info.payload.get("maxZoom")) == 4);
    CHECK(Num(info.payload.get("mapSizeX")) && *Num(info.payload.get("mapSizeX")) == 0);
    ActionOutcome tile = Run(dir, "getMapTile", R"({"x":0,"y":0,"z":0})");
    CHECK(!tile.ok);
    CHECK_MSG(tile.error.find("no map tile pack installed") != std::string::npos, tile.error);

    Write(JoinPath(dir, "map.json"), R"({"format":"other","tileSize":128,"maxZoom":4,"mapSizeX":1,"mapSizeZ":1})");
    CHECK(!LoadMapTilePack(dir).ok);
    CHECK(LoadMapTilePack(dir).reason.find("format") != std::string::npos);
    Write(JoinPath(dir, "map.json"), R"({"format":"takaro-map-tiles/1","tileSize":128,"maxZoom":4,"mapSizeX":0,"mapSizeZ":1})");
    CHECK(!LoadMapTilePack(dir).ok);
    Write(JoinPath(dir, "map.json"), "not json");
    CHECK(!LoadMapTilePack(dir).ok);
    Write(JoinPath(dir, "map.json"),
          R"({"format":"takaro-map-tiles/1","tileSize":128,"maxZoom":4,"mapSizeX":1,"mapSizeZ":1,"outsideTile":"../x.png"})");
    CHECK(!LoadMapTilePack(dir).ok);  // the outside tile must stay inside the pack
    CHECK(!LoadMapTilePack("").ok);
}

void PackTests() {
    t::Group("maptiles pack");
    std::string dir = t::TempDir("map-pack");
    Write(JoinPath(dir, "map.json"), kManifest);
    Write(dir + "/4/40/41.png", kPng);
    Write(dir + "/0/0/0.png", kPng + "z0");
    Write(dir + "/4/1/1.png", "GIF89a not a png");

    ActionOutcome info = Run(dir, "getMapInfo", "[]");
    CHECK(info.ok);
    CHECK(info.payload.get("enabled") && info.payload.get("enabled")->b);
    CHECK(Num(info.payload.get("maxZoom")) && *Num(info.payload.get("maxZoom")) == 4);
    CHECK(Num(info.payload.get("mapBlockSize")) && *Num(info.payload.get("mapBlockSize")) == 128);
    CHECK(Num(info.payload.get("mapSizeX")) && *Num(info.payload.get("mapSizeX")) == 10240);
    CHECK(Num(info.payload.get("mapSizeY")) && *Num(info.payload.get("mapSizeY")) == 4096);
    CHECK(Num(info.payload.get("mapSizeZ")) && *Num(info.payload.get("mapSizeZ")) == 10240);

    ActionOutcome tile = Run(dir, "getMapTile", R"({"x":40,"y":41,"z":4})");
    CHECK(tile.ok);
    CHECK(tile.payload.type == JsonValue::String);
    CHECK_EQ(tile.payload.str, Base64Encode(kPng));
    // numeric strings and the "zoom" alias, as Takaro and modules may send them
    tile = Run(dir, "getMapTile", R"({"x":"0","y":"0","zoom":"0"})");
    CHECK(tile.ok && tile.payload.str == Base64Encode(kPng + "z0"));

    tile = Run(dir, "getMapTile", R"({"x":5,"y":5,"z":4})");
    CHECK(!tile.ok);
    CHECK_MSG(tile.error.find("outside the map") != std::string::npos, tile.error);
    tile = Run(dir, "getMapTile", R"({"x":0,"y":0,"z":5})");
    CHECK(!tile.ok && tile.error.find("zoom 5 is outside 0-4") != std::string::npos);
    tile = Run(dir, "getMapTile", R"({"x":0.5,"y":0,"z":4})");
    CHECK(!tile.ok && tile.error.find("integer x, y and z") != std::string::npos);
    tile = Run(dir, "getMapTile", R"({"x":0,"y":0})");
    CHECK(!tile.ok);
    tile = Run(dir, "getMapTile", R"({"x":1,"y":1,"z":4})");
    CHECK(!tile.ok && tile.error.find("not a PNG") != std::string::npos);

    // an outside tile answers in-range tiles that are not in the pack (beyond the world edge)
    Write(JoinPath(dir, "map.json"),
          R"({"format":"takaro-map-tiles/1","tileSize":128,"maxZoom":4,"mapSizeX":10240,"mapSizeZ":10240,"outsideTile":"outside.png"})");
    Write(JoinPath(dir, "outside.png"), kPng + "outside");
    tile = Run(dir, "getMapTile", R"({"x":-3,"y":-1,"z":2})");
    CHECK(tile.ok && tile.payload.str == Base64Encode(kPng + "outside"));
    tile = Run(dir, "getMapTile", R"({"x":40,"y":41,"z":4})");
    CHECK(tile.ok && tile.payload.str == Base64Encode(kPng));
    tile = Run(dir, "getMapTile", R"({"x":0,"y":0,"z":9})");
    CHECK(!tile.ok);  // zoom limits still apply

    std::string big(kMaxMapTileBytes + 1, 'x');
    big.replace(0, 8, kPng.substr(0, 8));
    Write(dir + "/3/0/0.png", big);
    tile = Run(dir, "getMapTile", R"({"x":0,"y":0,"z":3})");
    CHECK(!tile.ok && tile.error.find("larger than") != std::string::npos);
}

void ConfigTests() {
    t::Group("maptiles config");
    auto env = [](std::map<std::string, std::string> m) {
        return EnvFn([m](const char* k) {
            auto it = m.find(k);
            return it == m.end() ? std::string() : it->second;
        });
    };
    NativeConfig c = LoadNativeConfig("Z:\\srv", env({}), "");
    CHECK_EQ(c.mapDir, std::string("Z:\\srv\\takaro\\map"));
    c = LoadNativeConfig("Z:\\srv", env({}), R"({"mapDir":"maps\\embervale"})");
    CHECK_EQ(c.mapDir, std::string("Z:\\srv\\maps\\embervale"));
    c = LoadNativeConfig("Z:\\srv", env({{"TAKARO_MAP_DIR", "D:\\tiles"}}), R"({"mapDir":"x"})");
    CHECK_EQ(c.mapDir, std::string("D:\\tiles"));
    CHECK(ConfigSummaryJson(c).find("\"mapDir\":\"D:\\\\tiles\"") != std::string::npos);
}

}  // namespace

void RunMapTileTests() {
    Base64Tests();
    NoPackTests();
    PackTests();
    ConfigTests();
}
