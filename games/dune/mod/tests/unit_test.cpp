// Host-side unit tests. No game process involved:
//   * the ELF parser and the .symtab/.dynsym walker, run against this very test binary
//   * the byte-signature matcher, including the "exactly once" rule the resolver enforces
//   * the Dune-specific resolver primitives: vtable-name demangling, UTF-16LE literal search,
//     RIP-relative displacement decoding, and the ProcessEvent code fingerprint
//   * JSON, the event ring buffer, log-line parsing and password/token redaction
//
// What is deliberately NOT here: anything that needs the game. The vtable readers, the ProcessEvent
// slot derivation and the hook installers all require a live, relocated process image, and a test
// that faked one would prove nothing about the real binary. Those are M0's job on the live server.
#include "common.h"
#include "events_parse.h"
#include "presence_diff.h"
#include "resolve.h"
#include "state.h"

#include <sys/stat.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

static int g_failed = 0, g_ran = 0;

#define CHECK(cond, ...)                                                     \
    do {                                                                     \
        g_ran++;                                                             \
        if (!(cond)) {                                                       \
            g_failed++;                                                      \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);           \
            printf("     ");                                                 \
            printf(__VA_ARGS__);                                             \
            printf("\n");                                                    \
        }                                                                    \
    } while (0)
#define EQ(a, b) CHECK((a) == (b), "got '%s' want '%s'", std::string(a).c_str(), std::string(b).c_str())

// A C symbol and a C++ method the .symtab walker and the demangler are checked against.
extern "C" void takaro_test_marker_function() {}
namespace takaro_test {
struct Marker {
    // noinline + volatile keeps the symbol in .symtab; a trivial inlinable method leaves none, and
    // then this test would be checking nothing.
    __attribute__((noinline)) void Method(int x) const { volatile int sink = x; (void)sink; }
};
}  // namespace takaro_test

static void TouchMarker() {
    takaro_test::Marker m;
    m.Method(1);
    // Calling it through a volatile pointer-to-member stops the linker from garbage-collecting the
    // symbol without the -Wpmf-conversions cast a plain `(void*)&Marker::Method` would need.
    void (takaro_test::Marker::*volatile pm)(int) const = &takaro_test::Marker::Method;
    (m.*pm)(2);
}

static void TestElfParser() {
    std::string self;
    CHECK(ReadFile("/proc/self/exe", self), "cannot read /proc/self/exe");
    if (self.empty()) return;
    const uint8_t* p = (const uint8_t*)self.data();

    ElfInfo info;
    CHECK(ResolveCore::ParseElfBuffer(p, self.size(), info), "ELF parse failed: %s", info.error.c_str());
    CHECK(info.textAddr != 0 && info.textSize != 0, "no .text");
    CHECK(!info.execRanges.empty(), "no executable PT_LOAD");
    CHECK(info.hasSymtab, "the test binary must be built unstripped");
    CHECK(!info.rodataRanges.empty(), "no read-only PT_LOAD recorded (needed for the UTF-16LE literal scan)");
    CHECK(info.symtabCount > 0, "empty .symtab");

    // The walker finds the C symbol by its plain name...
    bool foundC = false;
    uint64_t marker = 0;
    size_t visited = ResolveCore::ForEachSymbol(p, self.size(), false, [&](const ResolveCore::SymbolRec& s2) {
        if (!strcmp(s2.name, "takaro_test_marker_function")) { foundC = true; marker = s2.value; }
    });
    CHECK(visited > 0, "walked no symbols");
    CHECK(foundC && marker != 0, "takaro_test_marker_function not found in .symtab");

    // ...and the mangled C++ one demangles to the signature the resolver matches against.
    bool foundCpp = false;
    ResolveCore::ForEachSymbol(p, self.size(), false, [&](const ResolveCore::SymbolRec& s2) {
        if (strstr(s2.name, "Marker") == nullptr) return;
        std::string d = ResolveCore::Demangle(s2.name);
        if (d == "takaro_test::Marker::Method(int) const") foundCpp = true;
    });
    CHECK(foundCpp, "the demangler did not produce 'takaro_test::Marker::Method(int) const'");

    // A garbage ELF must be rejected, not crash.
    ElfInfo bad;
    CHECK(!ResolveCore::ParseElfBuffer((const uint8_t*)"not an elf", 10, bad), "garbage accepted");
    CHECK(ResolveCore::ForEachSymbol((const uint8_t*)"not an elf", 10, false,
                                     [](const ResolveCore::SymbolRec&) {}) == 0,
          "garbage walked");

    // Name matching: a pinned signature is exact, an unpinned key matches on the base name only.
    CHECK(ResolveCore::NameMatches("UObject::ProcessEvent(UFunction*, void*)", "UObject::ProcessEvent",
                                   "UObject::ProcessEvent(UFunction*, void*)"),
          "pinned signature");
    CHECK(!ResolveCore::NameMatches("UObject::ProcessEvent(UFunction*, void*, int)", "UObject::ProcessEvent",
                                    "UObject::ProcessEvent(UFunction*, void*)"),
          "a different overload must not match a pinned signature");
    CHECK(ResolveCore::NameMatches("FName::ToString() const", "FName::ToString", nullptr), "base name");
    CHECK(!ResolveCore::NameMatches("FNameXX::ToString() const", "FName::ToString", nullptr), "wrong class");
    EQ(ResolveCore::BaseName("UObject::ProcessEvent"), "ProcessEvent");
    EQ(ResolveCore::BaseName("RequestEngineExit"), "RequestEngineExit");
    EQ(ResolveCore::Demangle("plain_c_name"), "");
}

static void TestSignatureMatcher() {
    ResolveCore::Signature sig;
    std::string err;

    CHECK(ResolveCore::ParseSignature("48 8B ?? E8", sig, err), "parse failed: %s", err.c_str());
    CHECK(sig.bytes.size() == 4, "length %zu", sig.bytes.size());
    CHECK(sig.bytes[0] == 0x48 && sig.bytes[1] == 0x8B && sig.bytes[2] == -1 && sig.bytes[3] == 0xE8, "bytes");
    // `?` and `??` mean the same thing, whitespace is free-form.
    ResolveCore::Signature sig2;
    CHECK(ResolveCore::ParseSignature("48\t8b ? e8", sig2, err), "lenient parse: %s", err.c_str());
    CHECK(sig2.bytes == sig.bytes, "`?` and `??` must be equivalent");
    // Malformed input is rejected with a reason, never silently accepted.
    CHECK(!ResolveCore::ParseSignature("", sig2, err), "empty pattern accepted");
    CHECK(!ResolveCore::ParseSignature("4", sig2, err), "half a byte accepted");
    CHECK(!ResolveCore::ParseSignature("ZZ", sig2, err), "non-hex accepted");
    CHECK(!ResolveCore::ParseSignature("?? 48", sig2, err), "leading wildcard accepted");

    const uint8_t hay[] = {0x00, 0x48, 0x8B, 0x01, 0xE8, 0x90, 0x48, 0x8B, 0x02, 0xE8, 0x90};
    size_t off = 0;
    // Two matches -> the resolver must refuse the signature.
    CHECK(ResolveCore::ScanSignature(hay, sizeof hay, sig, off, 2) == 2, "expected two matches");
    // A pattern that pins the wildcard byte is unique.
    ResolveCore::Signature uniq;
    CHECK(ResolveCore::ParseSignature("48 8B 02 E8", uniq, err), "parse");
    CHECK(ResolveCore::ScanSignature(hay, sizeof hay, uniq, off, 2) == 1, "expected one match");
    CHECK(off == 6, "offset %zu", off);
    // No match, and a pattern longer than the haystack.
    ResolveCore::Signature none;
    CHECK(ResolveCore::ParseSignature("DE AD BE EF", none, err), "parse");
    CHECK(ResolveCore::ScanSignature(hay, sizeof hay, none, off, 2) == 0, "false positive");
    ResolveCore::Signature big;
    CHECK(ResolveCore::ParseSignature("48 8B 01 E8 90 48 8B 02 E8 90 00 11 22 33", big, err), "parse");
    CHECK(ResolveCore::ScanSignature(hay, sizeof hay, big, off, 2) == 0, "overlong pattern matched");
}

static void TestJson() {
    JsonValue v;
    CHECK(JsonParse(R"({"a":1,"b":"x\ny","c":[1,2,{"d":null}],"e":true,"f":-1.5})", v), "parse failed");
    CHECK(v.type == JsonValue::Object, "not an object");
    EQ(v.get("b")->str, "x\ny");
    EQ(v.get("a")->str, "1");
    CHECK(v.get("c")->arr.size() == 3, "array size");
    CHECK(v.get("e")->b, "bool");
    CHECK(v.get("f")->num == -1.5, "number");
    // 64-bit integers survive as text
    CHECK(JsonParse(R"({"id":9223372036854775807})", v), "bigint parse");
    EQ(v.get("id")->str, "9223372036854775807");
    // malformed input is rejected, never crashes
    CHECK(!JsonParse("{", v), "unterminated object accepted");
    CHECK(!JsonParse("{\"a\":}", v), "missing value accepted");
    CHECK(!JsonParse("[1,2", v), "unterminated array accepted");
    CHECK(!JsonParse("{} trailing", v), "trailing junk accepted");
    CHECK(!JsonParse("", v), "empty accepted");
    EQ(JsonEscape("a\"b\\c\n"), "a\\\"b\\\\c\\n");
    EQ(JsonNum(3), "3");
    EQ(JsonNum(1.5), "1.5");
}

static void TestRedaction() {
    EQ(Redact("WorldPassword=s3cr3t-fixture"), "WorldPassword=***");
    EQ(Redact("AdminPassword=hunter2 rest"), "AdminPassword=*** rest");
    EQ(Redact("[Settings] WorldPassword=abc\nServerName=Takaro"), "[Settings] WorldPassword=***\nServerName=Takaro");
    EQ(Redact("{\"WorldPassword\":\"abc\",\"x\":1}"), "{\"WorldPassword\":\"***\",\"x\":1}");
    EQ(Redact("-Password=abc"), "-Password=***");
    EQ(Redact("no secrets here"), "no secrets here");
    // Plugin/bearer tokens are redacted the same way as passwords.
    EQ(Redact("TAKARO_PLUGIN_TOKEN=abc123"), "TAKARO_PLUGIN_TOKEN=***");
    EQ(Redact("{\"token\":\"abc123\"}"), "{\"token\":\"***\"}");
    EQ(Redact("AuthToken: abc123"), "AuthToken: ***");
    EQ(Redact("ServerPassword=fixture-pw-42"), "ServerPassword=***");
    // A key that is only a prefix of a longer word is left alone.
    EQ(Redact("tokenConfigured true"), "tokenConfigured true");
    // key without a value must not eat the rest of the line
    EQ(Redact("WorldPassword"), "WorldPassword");
    EQ(Redact("WorldPassword="), "WorldPassword=");
}

static void TestRingBuffer() {
    auto& st = PluginState::Get();
    CHECK(st.LatestSeq() == 0, "fresh state should start at 0");
    for (int i = 0; i < 10; i++) st.EmitEvent("log", "{\"n\":" + std::to_string(i) + "}");
    CHECK(st.LatestSeq() == 10, "latestSeq=%llu", (unsigned long long)st.LatestSeq());

    JsonValue v;
    CHECK(JsonParse(st.EventsJson(0, 5), v), "events json invalid");
    CHECK(v.get("events")->arr.size() == 5, "limit not honoured");
    EQ(v.get("seq")->str, "5");
    EQ(v.get("latestSeq")->str, "10");
    CHECK(v.get("truncated")->b == false, "nothing dropped yet");
    EQ(v.get("bootId")->str, BootId());

    // cursor round-trip: the returned seq fetches exactly the remainder
    CHECK(JsonParse(st.EventsJson(5, 100), v), "events json invalid");
    CHECK(v.get("events")->arr.size() == 5, "remainder");
    EQ(v.get("events")->arr[0].get("seq")->str, "6");

    // nothing new -> cursor stays put, empty list
    CHECK(JsonParse(st.EventsJson(10, 100), v), "events json invalid");
    CHECK(v.get("events")->arr.empty(), "should be empty");
    EQ(v.get("seq")->str, "10");

    // overflow drops the oldest and reports truncation
    for (size_t i = 0; i < PluginState::kMaxEvents + 50; i++) st.EmitEvent("log", "{}");
    CHECK(st.Buffered() == PluginState::kMaxEvents, "buffered=%zu", st.Buffered());
    CHECK(JsonParse(st.EventsJson(1, 10), v), "events json invalid");
    CHECK(v.get("truncated")->b == true, "truncated flag not set after overflow");

    // a cursor from a previous boot (ahead of latestSeq) yields nothing and a sane cursor
    CHECK(JsonParse(st.EventsJson(st.LatestSeq() + 1000, 10), v), "events json invalid");
    CHECK(v.get("events")->arr.empty(), "future cursor");
}

static void TestCapabilities() {
    auto& st = PluginState::Get();
    st.SetCapability("players", "degraded", "waiting for game world");
    EQ(st.Capability("players"), "degraded");
    EQ(st.Capability("nonexistent"), "unimplemented");
    JsonValue v;
    CHECK(JsonParse(st.CapabilitiesJson(), v), "capabilities json invalid");
    EQ(v.get("players")->str, "degraded");
    CHECK(JsonParse(st.CapabilityDetailsJson(), v), "details json invalid");
    EQ(v.get("players")->str, "waiting for game world");
    st.SetCapability("players", "ok", "");
    CHECK(JsonParse(st.CapabilityDetailsJson(), v), "details json invalid");
    CHECK(v.get("players") == nullptr, "empty detail should be omitted");
}

static void TestLogLineSplit() {
    auto l = EventsParse::SplitLogLine(
        "[2026.09.17-05.58.10:857][ 27]LogChat: [76561198765432109] Tester (aka Takaro Tester): hi");
    CHECK(l.hasPrefix, "the standard UE prefix must be recognised");
    EQ(l.timestamp, "2026.09.17-05.58.10:857");
    EQ(l.frame, "27");
    EQ(l.category, "LogChat");
    EQ(l.message, "[76561198765432109] Tester (aka Takaro Tester): hi");

    // The first lines of a log file are written before LogTimes initialises and carry no prefix.
    auto n = EventsParse::SplitLogLine("LogInit: Display: Running engine for game: DuneSandbox");
    CHECK(!n.hasPrefix, "a prefix-less line must still parse");
    EQ(n.category, "LogInit");
    EQ(n.message, "Display: Running engine for game: DuneSandbox");

    // A line that is not "Category: message" at all keeps its whole text.
    auto raw = EventsParse::SplitLogLine("just some text without a category");
    EQ(raw.category, "");
    EQ(raw.message, "just some text without a category");

    // A trailing CR from a Windows-written line must not leak into the message.
    auto cr = EventsParse::SplitLogLine("LogDune: hello\r");
    EQ(cr.message, "hello");
}

static void TestLogRedaction() {
    std::string t = EventsParse::RedactLogLine(
        "LogNet: Browse: /Game/DuneSandbox/Maps/Survival_1??ID=76561198765432109?Ticket=AAAABBBBCCCCDDDD== next");
    CHECK(t.find("AAAABBBBCCCCDDDD") == std::string::npos, "the Steam auth ticket must be gone: %s", t.c_str());
    CHECK(t.find("76561198765432109") != std::string::npos, "the SteamID64 is not a secret and must stay");
    CHECK(t.find("<redacted>") != std::string::npos, "the removal must be visible");
    CHECK(t.find(" next") != std::string::npos, "only the ticket value is removed: %s", t.c_str());

    std::string p = EventsParse::RedactLogLine("LogNet: Login request: ?p=c2VjcmV0 userId=x");
    CHECK(p.find("c2VjcmV0") == std::string::npos, "the base64 world password must be gone: %s", p.c_str());

    std::string ini = EventsParse::RedactLogLine("Password=fixture-pw-42");
    CHECK(ini.find("fixture-pw-42") == std::string::npos, "common.cpp's Password= redaction still applies: %s", ini.c_str());

    // The real join line: the password and the ticket must go, the SteamID64 and the display name
    // must survive - Takaro needs both, and common.cpp's Redact() alone would swallow them because
    // it does not treat '?' as a value terminator.
    std::string join = EventsParse::RedactLogLine(
        "LogNet: Login request: ?Password=fixture-pw-42?Name=Tester??ID=76561198765432109?Ticket=AAAABBBB userId: NULL");
    CHECK(join.find("fixture-pw-42") == std::string::npos, "the join password must be gone: %s", join.c_str());
    CHECK(join.find("AAAABBBB") == std::string::npos, "the auth ticket must be gone: %s", join.c_str());
    CHECK(join.find("Tester") != std::string::npos, "the display name must survive: %s", join.c_str());
    CHECK(join.find("76561198765432109") != std::string::npos, "the SteamID64 must survive: %s", join.c_str());

    std::string clean = EventsParse::RedactLogLine("LogInit: nothing secret here");
    EQ(clean, "LogInit: nothing secret here");
}

static void TestLogNoiseFilter() {
    CHECK(EventsParse::IsNoise(""), "an empty line is noise");
    // Dune ships DefaultEngine.ini [Core.Log] with LogWorldPersistenceSubsystem=Verbose and
    // LogAuto=Verbose already turned up, so these two are per-object spam on every boot.
    CHECK(EventsParse::IsNoise("LogWorldPersistenceSubsystem: Verbose: saved actor 1234"),
          "the world-persistence spam Dune enables by default is noise");
    CHECK(EventsParse::IsNoise("LogAuto: Verbose: tick"), "LogAuto at Verbose is noise");
    CHECK(EventsParse::IsNoise("LogNetTraffic: Verbose: sending 42 bytes"), "net traffic is noise");
    CHECK(!EventsParse::IsNoise("LogChat: [76561198765432109] Tester: hi"), "chat is never noise");
    CHECK(!EventsParse::IsNoise("LogInit: Display: Unreal Engine version: 5.3.2"),
          "the engine-version line is never noise - it is the only place the UE version exists");
    CHECK(!EventsParse::IsNoise("LogWorldPersistenceSubsystem: world saved"),
          "the same category at a normal verbosity is not noise");
}

// ---------------------------------------------------------------------------------------------
// The Dune-specific resolver primitives. These are the pieces that replace "look up a demangled
// function name", so they are the ones worth testing off-process.

static void TestVTableClassName() {
    EQ(ResolveCore::VTableClassName("_ZTV7UObject"), "UObject");
    EQ(ResolveCore::VTableClassName("_ZTV14ADuneCharacter"), "ADuneCharacter");
    EQ(ResolveCore::VTableClassName("_ZTV24ADuneSandboxGameModeBase"), "ADuneSandboxGameModeBase");
    EQ(ResolveCore::VTableClassName("_ZTV27UDuneServerCommandSubsystem"), "UDuneServerCommandSubsystem");
    // The length prefix is part of the symbol: a wrong count is a different (usually invalid) name.
    // This is exactly the mistake that made the first draft of the wanted table miss half the classes.
    CHECK(ResolveCore::VTableClassName("_ZTV18ADuneGameModeBase") != "ADuneGameModeBase",
          "a wrong length prefix must not demangle to the intended class");
    // Not a vtable symbol, and not a C++ name at all.
    EQ(ResolveCore::VTableClassName("_ZTI7UObject"), "");
    EQ(ResolveCore::VTableClassName("plain_c_symbol"), "");
    EQ(ResolveCore::VTableClassName(nullptr), "");
}

static void TestUtf16LiteralSearch() {
    // Every UE string literal in this binary is UTF-16LE; a narrow search finds nothing, which would
    // silently make every xref-anchored signature look "unanchored but unique".
    auto bytes = ResolveCore::Utf16Bytes("Hi");
    CHECK(bytes.size() == 4, "size %zu", bytes.size());
    CHECK(bytes[0] == 'H' && bytes[1] == 0 && bytes[2] == 'i' && bytes[3] == 0, "encoding");
    EQ(std::string(), std::string());

    const uint8_t hay[] = {0xAA, 'H', 0, 'i', 0, 0xBB, 'H', 0, 'i', 0, 0xCC};
    auto hits = ResolveCore::FindAll(hay, sizeof hay, bytes.data(), bytes.size(), 8);
    CHECK(hits.size() == 2, "hits %zu", hits.size());
    CHECK(hits[0] == 1 && hits[1] == 6, "offsets %zu,%zu", hits[0], hits[1]);
    // maxHits caps the scan
    CHECK(ResolveCore::FindAll(hay, sizeof hay, bytes.data(), bytes.size(), 1).size() == 1, "maxHits");
    // absent, and degenerate inputs
    auto miss = ResolveCore::Utf16Bytes("Zz");
    CHECK(ResolveCore::FindAll(hay, sizeof hay, miss.data(), miss.size(), 8).empty(), "false positive");
    CHECK(ResolveCore::FindAll(nullptr, 10, bytes.data(), bytes.size(), 8).empty(), "null haystack");
    CHECK(ResolveCore::FindAll(hay, 2, bytes.data(), bytes.size(), 8).empty(), "haystack shorter than needle");
    CHECK(ResolveCore::Utf16Bytes("").empty(), "empty literal");
}

static void TestRipTarget() {
    // `lea rdi, [rip + 0x10]` at va 0x1000: 48 8D 3D 10 00 00 00, disp32 at offset 3, length 7.
    // RIP is the address of the NEXT instruction, so the target is 0x1000 + 7 + 0x10 = 0x1017.
    const uint8_t insn[] = {0x48, 0x8D, 0x3D, 0x10, 0x00, 0x00, 0x00};
    CHECK(ResolveCore::RipTarget(insn, 0x1000, 3) == 0x1017, "got 0x%llx",
          (unsigned long long)ResolveCore::RipTarget(insn, 0x1000, 3));
    // A negative displacement (the common case for a global that sits below the code).
    const uint8_t back[] = {0x48, 0x8B, 0x05, 0xF0, 0xFF, 0xFF, 0xFF};
    CHECK(ResolveCore::RipTarget(back, 0x1000, 3) == 0xFF7, "got 0x%llx",
          (unsigned long long)ResolveCore::RipTarget(back, 0x1000, 3));
    CHECK(ResolveCore::RipTarget(nullptr, 0x1000, 3) == 0, "null");
}

static void TestProcessEventFingerprint() {
    // A body carrying all four markers: a large `sub rsp, imm32`, an immediate 0x400 tested against a
    // dword, an indirect `call [reg+disp8]`, and >= 512 bytes.
    std::vector<uint8_t> body(600, 0x90);
    body[0] = 0x48; body[1] = 0x81; body[2] = 0xEC; body[3] = 0x80; body[4] = 0x00; body[5] = 0x00; body[6] = 0x00;
    body[16] = 0xF7; body[17] = 0x47; body[18] = 0x00; body[19] = 0x04; body[20] = 0x00; body[21] = 0x00;
    body[32] = 0xFF; body[33] = 0x50; body[34] = 0x20;
    auto fp = ResolveCore::FingerprintProcessEvent(body.data(), body.size());
    CHECK(fp.bigFrame, "bigFrame");
    CHECK(fp.funcNativeTest, "the FUNC_Native (0x400) immediate must be recognised");
    CHECK(fp.indirectCall, "indirectCall");
    CHECK(fp.longEnough, "longEnough");
    CHECK(fp.score() == 4, "score %d", fp.score());

    // A short, trivial body - the shape of the `push rbp; mov rbp,rsp; pop rbp; ret` base stubs that
    // fill most of UObject's vtable on this build. It must score low, or the derivation would accept
    // dozens of candidates.
    const uint8_t stub[] = {0x55, 0x48, 0x89, 0xE5, 0x5D, 0xC3, 0x90, 0x90, 0x90, 0x90, 0x90, 0x90,
                            0x90, 0x90, 0x90, 0x90, 0x90, 0x90, 0x90, 0x90};
    auto sfp = ResolveCore::FingerprintProcessEvent(stub, sizeof stub);
    CHECK(sfp.score() < 3, "a trivial stub scored %d", sfp.score());

    // Degenerate input must be safe, not a crash.
    CHECK(ResolveCore::FingerprintProcessEvent(nullptr, 1000).score() == 0, "null body");
    CHECK(ResolveCore::FingerprintProcessEvent(stub, 4).score() == 0, "too short");
}

static void TestEngineVersionLine() {
    // The version exists ONLY in the boot log on this build, so this parse is the only way we ever
    // learn it. Both the prefixed and the pre-LogTimes forms must work.
    EQ(EventsParse::ParseEngineVersionLine(
           "[2026.09.21-10.00.00:000][  0]LogInit: Display: Unreal Engine version: 5.3.2-1234+++UE5+Release-5.3"),
       "5.3.2-1234+++UE5+Release-5.3");
    EQ(EventsParse::ParseEngineVersionLine("LogInit: Unreal Engine version: 5.2.1-99"), "5.2.1-99");
    EQ(EventsParse::ParseEngineVersionLine("LogInit: Display: Engine Version: 5.3"), "");
    EQ(EventsParse::ParseEngineVersionLine("something else entirely"), "");
}

static void TestIdShapes() {
    CHECK(EventsParse::LooksLikeSteamId64("76561198765432109"), "the real test account id");
    CHECK(!EventsParse::LooksLikeSteamId64("7656119800073587"), "16 digits is not a SteamID64");
    CHECK(!EventsParse::LooksLikeSteamId64("12345678901234567"), "wrong prefix");
    CHECK(!EventsParse::LooksLikeSteamId64(""), "empty");
    // The FLS id is Takaro's gameId for this connector (plan Decision 3): bare 16 hex.
    CHECK(EventsParse::LooksLikeFlsId("00a1b2c3d4e5f607"), "16 hex");
    CHECK(EventsParse::LooksLikeFlsId("ABCDEF0123456789"), "upper case hex");
    CHECK(!EventsParse::LooksLikeFlsId("0000000000000000"), "an all-zero id is an uninitialised PlayerState");
    CHECK(!EventsParse::LooksLikeFlsId("00a1b2c3d4e5f60"), "15 hex");
    CHECK(!EventsParse::LooksLikeFlsId("00a1b2c3d4e5f60g"), "non-hex");
}

static void TestDuneSecretRedaction() {
    // The GM-command token is the crown jewel: leaking it is a full admin bypass on the server.
    CHECK(EventsParse::RedactLogLine("-ini:engine:[FuncomLiveServices]:ServerCommandsAuthToken=hunter2-fixture")
              .find("hunter2-fixture") == std::string::npos,
          "ServerCommandsAuthToken survived redaction");
    CHECK(EventsParse::RedactLogLine("{\"ServerCommandsAuthToken\":\"hunter2-fixture\"}").find("hunter2-fixture") ==
              std::string::npos,
          "the JSON form survived redaction");
    CHECK(EventsParse::RedactLogLine("DatabasePassword=pg-fixture-pw").find("pg-fixture-pw") == std::string::npos,
          "DatabasePassword survived redaction");
    CHECK(EventsParse::RedactLogLine("FuncomLiveServices__ServiceAuthToken=jwt.fixture.value")
              .find("jwt.fixture.value") == std::string::npos,
          "the FLS service token survived redaction");
    CHECK(EventsParse::RedactLogLine("Bgd.ServerLoginPassword=redacted-fixture").find("redacted-fixture") ==
              std::string::npos,
          "the join password survived redaction");
    // A login URL keeps the non-secret fields, because the connect fallback needs them.
    std::string url = EventsParse::RedactLogLine(
        "LogNet: Login request: ?Password=pw-fixture?Name=TakaroTester?Ticket=tkt-fixture userId: x");
    CHECK(url.find("pw-fixture") == std::string::npos, "url password survived");
    CHECK(url.find("tkt-fixture") == std::string::npos, "url ticket survived");
    CHECK(url.find("TakaroTester") != std::string::npos, "the display name must survive: %s", url.c_str());
    // Nothing to redact must be left completely alone.
    EQ(EventsParse::RedactLogLine("LogInit: Display: nothing secret here"),
       "LogInit: Display: nothing secret here");
}

// ---------------------------------------------------------------------------------------------
// LANE L2: the victim / killer / weapon role matcher.
//
// This is the F20 guard (memory: hard-test-no-overclaim): a victim and a killer the wrong way round
// is the one bug on this lane that a player-less server can never reveal, so the decision is made by
// a pure function and pinned here.
using EventsParse::ParamProp;
using EventsParse::ParamRole;

static void TestParamRoleMatcher() {
    // An empty parameter list yields NO role rather than a positional guess.
    std::vector<ParamProp> none;
    CHECK(EventsParse::MatchParamRole(none, ParamRole::Victim) == -1, "empty -> victim");
    CHECK(EventsParse::MatchParamRole(none, ParamRole::Killer) == -1, "empty -> killer");
    CHECK(EventsParse::MatchParamRole(none, ParamRole::Causer) == -1, "empty -> causer");
    CHECK(EventsParse::MatchParamRole(none, ParamRole::Weapon) == -1, "empty -> weapon");

    // The obvious case, and the two must not swap.
    std::vector<ParamProp> vk = {{"Victim", "ObjectProperty", 0}, {"Killer", "ObjectProperty", 8}};
    CHECK(EventsParse::MatchParamRole(vk, ParamRole::Victim) == 0, "Victim is param 0");
    CHECK(EventsParse::MatchParamRole(vk, ParamRole::Killer) == 1, "Killer is param 1");
    // Reversed declaration order must reverse the answer too - the match is by NAME, never by slot.
    std::vector<ParamProp> kv = {{"Killer", "ObjectProperty", 0}, {"Victim", "ObjectProperty", 8}};
    CHECK(EventsParse::MatchParamRole(kv, ParamRole::Victim) == 1, "Victim found at slot 1");
    CHECK(EventsParse::MatchParamRole(kv, ParamRole::Killer) == 0, "Killer found at slot 0");

    // A non-object parameter is never a victim, a killer or a causer: a float called `Target` is a
    // distance, not an actor.
    std::vector<ParamProp> floaty = {{"Target", "FloatProperty", 0}};
    CHECK(EventsParse::MatchParamRole(floaty, ParamRole::Victim) == -1, "a float Target is not a victim");
    std::vector<ParamProp> structy = {{"TargetActor", "StructProperty", 0}};
    CHECK(EventsParse::MatchParamRole(structy, ParamRole::Victim) == -1, "a struct TargetActor is not a victim");

    // Specificity: `DamageCauser` belongs to the causer role, and an `InstigatorController` beats a
    // bare `Instigator`.
    std::vector<ParamProp> dmg = {{"DamagedActor", "ObjectProperty", 0},
                                  {"Instigator", "ObjectProperty", 8},
                                  {"InstigatorController", "ObjectProperty", 16},
                                  {"DamageCauser", "ObjectProperty", 24},
                                  {"DamageType", "ClassProperty", 32}};
    CHECK(EventsParse::MatchParamRole(dmg, ParamRole::Victim) == 0, "DamagedActor is the victim");
    CHECK(EventsParse::MatchParamRole(dmg, ParamRole::Killer) == 2, "InstigatorController beats Instigator");
    CHECK(EventsParse::MatchParamRole(dmg, ParamRole::Causer) == 3, "DamageCauser is the causer");
    CHECK(EventsParse::MatchParamRole(dmg, ParamRole::Weapon) == 4, "DamageType fills the weapon role");

    // A weapon may be a class/struct reference; that is the ONE role where the object requirement is
    // relaxed, and it must not leak into the others.
    std::vector<ParamProp> wpn = {{"Weapon", "StructProperty", 0}};
    CHECK(EventsParse::MatchParamRole(wpn, ParamRole::Weapon) == 0, "a struct Weapon is allowed");
    CHECK(EventsParse::MatchParamRole(wpn, ParamRole::Victim) == -1, "a Weapon is not a victim");

    // Case-insensitive, and a negative offset (an unresolved property) is skipped.
    std::vector<ParamProp> mixed = {{"m_victimCharacter", "ObjectProperty", -1},
                                    {"m_VictimCharacter", "ObjectProperty", 8}};
    CHECK(EventsParse::MatchParamRole(mixed, ParamRole::Victim) == 1, "an unresolved offset is skipped");

    // Object-property type recognition, including the weak/soft forms a multicast often uses.
    CHECK(EventsParse::IsObjectPropertyType("WeakObjectProperty"), "weak object");
    CHECK(EventsParse::IsObjectPropertyType("InterfaceProperty"), "interface");
    CHECK(!EventsParse::IsObjectPropertyType("ByteProperty"), "byte is not an object");
    CHECK(!EventsParse::IsObjectPropertyType("EnumProperty"), "enum is not an object");
}

static void TestDisplayNameDetector() {
    // Real values read off the LIVE server's DuneNpcCharacter::m_Name.
    CHECK(EventsParse::LooksLikeDisplayName("Mobula Gang Member"), "multi-word name");
    CHECK(EventsParse::LooksLikeDisplayName("Slaver Trapper"), "multi-word name");
    CHECK(EventsParse::LooksLikeDisplayName("Ariste Atreides"), "a character's name");
    CHECK(EventsParse::LooksLikeDisplayName("Kindjal"), "a single real word");
    // ...and the identifiers that must never reach Takaro as a name.
    CHECK(!EventsParse::LooksLikeDisplayName("BP_Npc_SoldierBase_Character_Baked_C"), "a BP class");
    CHECK(!EventsParse::LooksLikeDisplayName("BP_IdleCivilian_C"), "a BP class");
    CHECK(!EventsParse::LooksLikeDisplayName("T3_Band_Slv_Reg_Marksman"), "a spawn-config row key");
    CHECK(!EventsParse::LooksLikeDisplayName("DuneNpcCharacter"), "internal CamelCase");
    CHECK(!EventsParse::LooksLikeDisplayName("ADuneCritterBase"), "a UE class name");
    CHECK(!EventsParse::LooksLikeDisplayName("SoldierBase"), "internal CamelCase, single token");
    CHECK(!EventsParse::LooksLikeDisplayName("Thing07"), "a numbered single token");
    CHECK(!EventsParse::LooksLikeDisplayName("SPICE"), "ALLCAPS");
    CHECK(!EventsParse::LooksLikeDisplayName(""), "empty");
    // A space rescues a token that would otherwise look internal, which is why the real names pass.
    CHECK(EventsParse::LooksLikeDisplayName("Artisan Disruptor M11"), "trailing token with digits is fine with spaces");
}


// ---- the presence reconciler's diff -------------------------------------------------------------
// The live sweep needs a game process; the BOOKKEEPING does not, and the bookkeeping is where a
// double-emitted connect or a flapping disconnect would come from. Lane L6b-0 added the reconciler
// because `player-connected` must not depend on one vtable slot; these checks are what stop it from
// being a second source of wrong events.
static void TestPresenceDiff() {
    // Three fake controller pointers. Never dereferenced — the diff only ever compares them.
    const void* A = (const void*)0x1000;
    const void* B = (const void*)0x2000;
    const void* C = (const void*)0x3000;
    auto nothingElseKnows = [](const void*) { return false; };
    // Whatever the diff has adopted is what the registry would say in the real plugin, so the
    // "preAnnounced" oracle for the disconnect half is modelled by a mutable set.
    std::vector<const void*> registryOnline;
    auto registryKnows = [&registryOnline](const void* c) {
        for (const void* x : registryOnline)
            if (x == c) return true;
        return false;
    };

    PresenceDiff::Options opt;  // missesBeforeDisconnect == 2
    std::vector<PresenceDiff::Tracked> t;

    // 1. A first sighting is a connect, and it is NOT announced until the caller says it managed to.
    auto r = PresenceDiff::Apply(t, {A}, opt, nothingElseKnows);
    CHECK(r.connects.size() == 1 && r.connects[0] == A, "first sighting yields one connect");
    CHECK(r.disconnects.empty(), "no disconnect on a first sighting");
    // The caller failed to announce (no game thread): the NEXT sweep must offer it again.
    r = PresenceDiff::Apply(t, {A}, opt, nothingElseKnows);
    CHECK(r.connects.size() == 1 && r.connects[0] == A, "an un-announced connect is retried");
    PresenceDiff::MarkAnnounced(t, A);
    registryOnline.push_back(A);
    // 2. Once announced, a still-connected controller is silent.
    r = PresenceDiff::Apply(t, {A}, opt, registryKnows);
    CHECK(r.connects.empty() && r.disconnects.empty(), "an announced, still-present player is silent");

    // 3. One missed sweep is debounced, not a disconnect.
    r = PresenceDiff::Apply(t, {}, opt, registryKnows);
    CHECK(r.disconnects.empty(), "a single absence is debounced");
    CHECK(t.size() == 1, "the debounced entry is kept");
    // ...the second consecutive absence confirms it.
    r = PresenceDiff::Apply(t, {}, opt, registryKnows);
    CHECK(r.disconnects.size() == 1 && r.disconnects[0] == A, "two consecutive absences confirm a disconnect");
    CHECK(t.empty(), "a confirmed disconnect is dropped from the tracked set");

    // 4. An absence coming back before the debounce expires produces NOTHING at all (no flap).
    t.clear();
    registryOnline.clear();
    PresenceDiff::Apply(t, {B}, opt, nothingElseKnows);
    PresenceDiff::MarkAnnounced(t, B);
    registryOnline.push_back(B);
    PresenceDiff::Apply(t, {}, opt, registryKnows);
    r = PresenceDiff::Apply(t, {B}, opt, registryKnows);
    CHECK(r.connects.empty() && r.disconnects.empty(), "a one-sweep blip emits nothing");
    r = PresenceDiff::Apply(t, {}, opt, registryKnows);
    CHECK(r.disconnects.empty(), "the miss counter was reset by the reappearance");

    // 5. The dedupe with the PostLogin detour: a controller the registry already holds online is
    //    adopted as announced, so the sweep never emits a second player-connected.
    t.clear();
    registryOnline.clear();
    registryOnline.push_back(C);
    r = PresenceDiff::Apply(t, {C}, opt, registryKnows);
    CHECK(r.connects.empty(), "a controller the hook already announced is adopted silently");
    CHECK(t.size() == 1 && t[0].announced, "...and is marked announced");

    // 6. The dedupe with the Logout detour: once the registry says offline, the confirmed absence is
    //    dropped instead of emitting a second player-disconnected.
    registryOnline.clear();
    PresenceDiff::Apply(t, {}, opt, registryKnows);
    r = PresenceDiff::Apply(t, {}, opt, registryKnows);
    CHECK(r.disconnects.empty(), "the Logout detour got there first: no duplicate disconnect");
    CHECK(r.droppedUnannounced == 1, "...and the drop is counted");
    CHECK(t.empty(), "the entry is still retired");

    // 7. The tracked set is bounded.
    t.clear();
    PresenceDiff::Options small;
    small.maxTracked = 2;
    r = PresenceDiff::Apply(t, {A, B, C}, small, nothingElseKnows);
    CHECK(t.size() == 2, "maxTracked is honoured");
    CHECK(r.connects.size() == 2, "and only the tracked ones are announced");
    // A null pointer is never tracked.
    t.clear();
    r = PresenceDiff::Apply(t, {nullptr}, opt, nothingElseKnows);
    CHECK(t.empty() && r.connects.empty(), "a null controller is ignored");
}

// -----------------------------------------------------------------------------------------------
// LANE L2b: the frame-bool read and the death decision table.
//
// This is the lane's whole correctness argument in unit-testable form. On the live server L2's
// `bIsDeath != 0` gate dropped 100 % of real deaths (defeatsDropped == hits, emitted == 0), and the
// two candidate causes were a bitfield misread and wrong semantics. The reflected layout falsified
// the bitfield hypothesis (the bools sit at DISTINCT CONSECUTIVE byte offsets 0x10/0x11/0x12/0x13,
// whereas packed bitfields share one offset and differ only by mask), so the table below encodes
// the surviving reading: `bShouldEnterDbno` is the knock-down gate, `bIsDeath` is descriptive.
static void TestFrameBoolRead() {
    using EventsParse::BoolRead;
    CHECK(EventsParse::ReadFrameBool(0, true) == BoolRead::False, "0 is false");
    CHECK(EventsParse::ReadFrameBool(1, true) == BoolRead::True, "1 is true");
    // A failed read is never an answer.
    CHECK(EventsParse::ReadFrameBool(1, false) == BoolRead::Unreadable, "a failed read must be Unreadable");
    CHECK(EventsParse::ReadFrameBool(0, false) == BoolRead::Unreadable, "a failed read must be Unreadable");
    // THE BITFIELD CANARY. A UHT function parameter is a whole bool holding 0 or 1. Any other byte
    // means the offset is not what we think it is (a packed bitfield, a shifted frame), and the
    // honest answer is "unknown" rather than a truthy misread that would fabricate deaths.
    for (unsigned v : {2u, 3u, 0x10u, 0x80u, 0xffu})
        CHECK(EventsParse::ReadFrameBool((uint8_t)v, true) == BoolRead::Unreadable,
              "byte 0x%02x must be Unreadable, not a bool", v);
    EQ(std::string(EventsParse::BoolReadName(BoolRead::True)), "true");
    EQ(std::string(EventsParse::BoolReadName(BoolRead::False)), "false");
    EQ(std::string(EventsParse::BoolReadName(BoolRead::Unreadable)), "unreadable");
}

static void TestDeathDecisionTable() {
    using EventsParse::BoolRead;
    using EventsParse::DeathFacts;
    using EventsParse::DeathVerdict;

    // 1. ReceiveMulticastKill is never an event, whatever else is true (events.h, F20).
    {
        DeathFacts f;
        f.isMulticastKillHint = true;
        f.isDeath = BoolRead::True;
        CHECK(EventsParse::DecideDeath(f).verdict == DeathVerdict::Hint, "multicast kill must stay a hint");
    }
    // 2. THE REGRESSION THIS LANE EXISTS FOR. Both real deaths on the live server arrived as
    //    ReceiveMulticastDeathOrDefeat with bIsDeath=0 and no DBNO parameter. They MUST emit.
    {
        DeathFacts f;
        f.isDeath = BoolRead::False;  // "DEFEATED", which is what Dune's own UI printed
        EventsParse::DeathDecision d = EventsParse::DecideDeath(f);
        CHECK(d.verdict == DeathVerdict::Emit, "a defeat is a lethal Dune death and must emit");
        EQ(std::string(d.deathKind), "defeat");
    }
    // 3. bIsDeath=true still emits, and is labelled differently.
    {
        DeathFacts f;
        f.isDeath = BoolRead::True;
        EventsParse::DeathDecision d = EventsParse::DecideDeath(f);
        CHECK(d.verdict == DeathVerdict::Emit, "bIsDeath=true must emit");
        EQ(std::string(d.deathKind), "death");
    }
    // 4. An unreadable bool must not block an event (every capability degrades, nothing fails).
    {
        DeathFacts f;  // both bools default to Unreadable
        EventsParse::DeathDecision d = EventsParse::DecideDeath(f);
        CHECK(d.verdict == DeathVerdict::Emit, "an unreadable bIsDeath must not swallow a death");
        EQ(std::string(d.deathKind), "unknown");
    }
    // 5. THE KNOCK-DOWN GATE. bShouldEnterDbno=true is down-but-not-out and is not a death.
    {
        DeathFacts f;
        f.dbno = BoolRead::True;
        f.isDeath = BoolRead::False;
        CHECK(EventsParse::DecideDeath(f).verdict == DeathVerdict::DropKnockDown, "DBNO must not be a death");
    }
    // ...but bIsDeath=true does NOT override DBNO: a finishing blow arrives as its own later call,
    // and reporting the knock-down as well would double-count the victim.
    {
        DeathFacts f;
        f.dbno = BoolRead::True;
        f.isDeath = BoolRead::True;
        CHECK(EventsParse::DecideDeath(f).verdict == DeathVerdict::DropKnockDown, "DBNO gate is unconditional");
    }
    // A false or unreadable DBNO flag never refuses anything.
    {
        DeathFacts f;
        f.dbno = BoolRead::False;
        CHECK(EventsParse::DecideDeath(f).verdict == DeathVerdict::Emit, "DBNO=false must emit");
        f.dbno = BoolRead::Unreadable;
        CHECK(EventsParse::DecideDeath(f).verdict == DeathVerdict::Emit, "DBNO unreadable must emit");
    }
    // 6. THE PLAYER LIFE-STATE READ-BACK. It can only ever refuse, and only when it is fresh.
    {
        DeathFacts f;
        f.victimIsPlayer = true;
        f.isDeath = BoolRead::False;
        f.haveLifeState = true;
        f.lifeStateFresh = true;
        f.lifeState = EventsParse::kLifeStateAlive;
        CHECK(EventsParse::DecideDeath(f).verdict == DeathVerdict::DropKnockDown,
              "a player still Alive after the call did not die");
        // A stale read (the pump stalled) must be ignored entirely, not trusted.
        f.lifeStateFresh = false;
        CHECK(EventsParse::DecideDeath(f).verdict == DeathVerdict::Emit, "a stale life-state read must be ignored");
        // A dead life state emits.
        f.lifeStateFresh = true;
        f.lifeState = 1;
        CHECK(EventsParse::DecideDeath(f).verdict == DeathVerdict::Emit, "lifeState != Alive must emit");
        // bIsDeath=true overrides the read-back.
        f.lifeState = EventsParse::kLifeStateAlive;
        f.isDeath = BoolRead::True;
        CHECK(EventsParse::DecideDeath(f).verdict == DeathVerdict::Emit, "bIsDeath=true overrides the read-back");
    }
    // The read-back applies to PLAYERS ONLY: an NPC has no revive mechanic and no dead flag on this
    // build, so the event firing IS the death.
    {
        DeathFacts f;
        f.victimIsPlayer = false;
        f.isDeath = BoolRead::False;
        f.haveLifeState = true;
        f.lifeStateFresh = true;
        f.lifeState = EventsParse::kLifeStateAlive;
        CHECK(EventsParse::DecideDeath(f).verdict == DeathVerdict::Emit, "an NPC death must not be gated on life state");
    }
    // 7. DEDUPE. A single death fires several of these functions; the victim is reported once.
    {
        DeathFacts f;
        f.duplicate = true;
        f.isDeath = BoolRead::True;
        CHECK(EventsParse::DecideDeath(f).verdict == DeathVerdict::DropDuplicate, "a repeat must be dropped");
        // Dedupe outranks the DBNO gate only in that both refuse; check the reason is the specific one.
        f.dbno = BoolRead::True;
        CHECK(EventsParse::DecideDeath(f).verdict == DeathVerdict::DropDuplicate, "duplicate is decided first");
    }
    // 8. TOTALITY. Every combination yields a decision with a non-empty reason and a valid deathKind.
    {
        const BoolRead vals[] = {BoolRead::False, BoolRead::True, BoolRead::Unreadable};
        size_t n = 0;
        for (BoolRead dbno : vals)
            for (BoolRead isd : vals)
                for (int player = 0; player < 2; player++)
                    for (int have = 0; have < 2; have++)
                        for (int fresh = 0; fresh < 2; fresh++)
                            for (int dup = 0; dup < 2; dup++)
                                for (int hint = 0; hint < 2; hint++)
                                    for (uint8_t ls = 0; ls < 3; ls++) {
                                        DeathFacts f;
                                        f.dbno = dbno;
                                        f.isDeath = isd;
                                        f.victimIsPlayer = player != 0;
                                        f.haveLifeState = have != 0;
                                        f.lifeStateFresh = fresh != 0;
                                        f.duplicate = dup != 0;
                                        f.isMulticastKillHint = hint != 0;
                                        f.lifeState = ls;
                                        EventsParse::DeathDecision d = EventsParse::DecideDeath(f);
                                        std::string kind = d.deathKind;
                                        if (d.reason[0] == 0 ||
                                            (kind != "death" && kind != "defeat" && kind != "unknown"))
                                            CHECK(false, "incomplete decision for a fact combination");
                                        n++;
                                    }
        CHECK(n == 3 * 3 * 2 * 2 * 2 * 2 * 2 * 3, "the totality sweep did not cover the space (%zu)", n);
    }
}

// ------------------------------------------------------------------------------------------------
// LANE L2c: victim classification, self attribution, class-property reads, the delayed connect
// announce and the position-source rule. Each block names the live event that motivated it.
static void TestVictimClassification() {
    using EventsParse::DeathEventType;
    using EventsParse::VictimFacts;
    // 1. THE REGRESSION. /events seq 4 and seq 5: the victim's class chain derives
    //    DunePlayerCharacter but the registry did not recognise the freshly respawned pawn. Under the
    //    old code this became entity-killed with entity:null and entityCode:"BP_DunePlayerCharacter_C".
    {
        VictimFacts f;
        f.classChainIsPlayer = true;
        f.identityResolved = false;
        f.haveDisplayName = false;
        EventsParse::VictimClassification c = EventsParse::ClassifyVictim(f);
        CHECK(c.type == DeathEventType::PlayerDeath,
              "a player class chain with an unresolved identity must still be player-death (seq 4/5 regression)");
        CHECK(!c.identityComplete, "an unresolved identity must be flagged identityComplete:false");
        CHECK(!c.dropUnnamed, "dropUnnamed is meaningless for a player-death");
        CHECK(c.reason[0] != 0, "every classification names its reason");
    }
    // 2. The happy path: class chain AND identity (seq 1, seq 9).
    {
        VictimFacts f;
        f.classChainIsPlayer = true;
        f.identityResolved = true;
        EventsParse::VictimClassification c = EventsParse::ClassifyVictim(f);
        CHECK(c.type == DeathEventType::PlayerDeath, "a resolved player victim is player-death");
        CHECK(c.identityComplete, "a resolved identity is complete");
    }
    // 3. A player class chain can NEVER become entity-killed, whatever else is true. This is the rule
    //    the campaign asked for, so it is asserted exhaustively rather than by example.
    {
        for (int ident = 0; ident < 2; ident++)
            for (int named = 0; named < 2; named++) {
                VictimFacts f;
                f.classChainIsPlayer = true;
                f.identityResolved = ident != 0;
                f.haveDisplayName = named != 0;
                CHECK(EventsParse::ClassifyVictim(f).type == DeathEventType::PlayerDeath,
                      "a player class chain must never be entity-killed");
            }
    }
    // 4. Identity promotes when the class chain was unreadable.
    {
        VictimFacts f;
        f.classChainIsPlayer = false;
        f.identityResolved = true;
        EventsParse::VictimClassification c = EventsParse::ClassifyVictim(f);
        CHECK(c.type == DeathEventType::PlayerDeath, "a registry-resolved victim is a player even with no class chain");
        CHECK(c.identityComplete, "identity-only resolution is still complete");
    }
    // 5. A named NPC: entity-killed, publishable (seq 3, "Scavenger Thug").
    {
        VictimFacts f;
        f.haveDisplayName = true;
        EventsParse::VictimClassification c = EventsParse::ClassifyVictim(f);
        CHECK(c.type == DeathEventType::EntityKilled, "a named non-player victim is entity-killed");
        CHECK(!c.dropUnnamed, "a named entity must not be flagged for dropping");
    }
    // 6. An unnamed NPC: entity-killed, but flagged so the sidecar drops it. A class name must never
    //    become a creature's name (memory: catalogue-human-names).
    {
        VictimFacts f;
        f.haveDisplayName = false;
        EventsParse::VictimClassification c = EventsParse::ClassifyVictim(f);
        CHECK(c.type == DeathEventType::EntityKilled, "an unnamed non-player victim is still entity-killed");
        CHECK(c.dropUnnamed, "an unnamed entity must be flagged for the sidecar to drop");
    }
    // 7. TOTALITY: every combination decides, names a reason, and never flags dropUnnamed on a
    //    player-death.
    {
        size_t n = 0;
        for (int chain = 0; chain < 2; chain++)
            for (int ident = 0; ident < 2; ident++)
                for (int named = 0; named < 2; named++) {
                    VictimFacts f;
                    f.classChainIsPlayer = chain != 0;
                    f.identityResolved = ident != 0;
                    f.haveDisplayName = named != 0;
                    EventsParse::VictimClassification c = EventsParse::ClassifyVictim(f);
                    if (c.reason[0] == 0) CHECK(false, "a classification with no reason");
                    if (c.type == DeathEventType::PlayerDeath && c.dropUnnamed)
                        CHECK(false, "a player-death must never be flagged dropUnnamed");
                    if (c.type == DeathEventType::EntityKilled && c.dropUnnamed == f.haveDisplayName)
                        CHECK(false, "dropUnnamed must be exactly the absence of a display name");
                    n++;
                }
        CHECK(n == 8, "the classification totality sweep did not cover the space (%zu)", n);
    }
}

static void TestSelfAttribution() {
    using EventsParse::AttributionFacts;
    // /events seq 10: TakaroTest's own death carried killer == TakaroTest.
    {
        AttributionFacts f;
        f.haveKiller = true;
        f.killerIsVictimActor = true;
        CHECK(EventsParse::IsSelfAttribution(f), "the killer being the victim actor is a self/environment death");
    }
    {
        AttributionFacts f;
        f.haveKiller = true;
        f.killerIsVictimPlayer = true;
        CHECK(EventsParse::IsSelfAttribution(f),
              "the killer resolving to the same tracked player is a self/environment death");
    }
    // A real killer (seq 1: an NPC soldier; seq 3: the player) must survive untouched.
    {
        AttributionFacts f;
        f.haveKiller = true;
        CHECK(!EventsParse::IsSelfAttribution(f), "a distinct killer is not a self death");
    }
    // No killer at all is UNATTRIBUTED, which is a different statement from self/environment: it must
    // not be upgraded into a claim about the cause.
    {
        AttributionFacts f;
        f.killerIsVictimActor = true;
        f.killerIsVictimPlayer = true;
        CHECK(!EventsParse::IsSelfAttribution(f), "with no resolved killer there is nothing to call self");
    }
    CHECK(std::string(EventsParse::kSelfAttribution) == "self-or-environment",
          "the attribution string is the contract the sidecar matches on");
}

static void TestClassPropertyDetector() {
    // The live cause of weaponCode:"BlueprintGeneratedClass": DeathDefeatCausingDamageType is a
    // ClassProperty, so the frame holds a UClass* and the pointed-to object's OWN name is the answer.
    CHECK(EventsParse::PropertyHoldsClassPointer("ClassProperty"), "ClassProperty holds a UClass*");
    CHECK(EventsParse::PropertyHoldsClassPointer("SoftClassProperty"), "SoftClassProperty holds a class");
    CHECK(EventsParse::PropertyHoldsClassPointer("ClassPtrProperty"), "ClassPtrProperty holds a class");
    // An instance reference must keep the old read (the class OF the object).
    CHECK(!EventsParse::PropertyHoldsClassPointer("ObjectProperty"), "ObjectProperty holds an instance");
    CHECK(!EventsParse::PropertyHoldsClassPointer("WeakObjectProperty"), "WeakObjectProperty holds an instance");
    CHECK(!EventsParse::PropertyHoldsClassPointer("StructProperty"), "StructProperty is not a class pointer");
    CHECK(!EventsParse::PropertyHoldsClassPointer(""), "an unknown type is not a class pointer");
    // A ClassProperty is still an object-reference type for the role matcher's purposes.
    CHECK(EventsParse::IsObjectPropertyType("ClassProperty"), "ClassProperty is an object reference");
}

static void TestConnectAnnounceDecision() {
    using EventsParse::ConnectAnnounce;
    using EventsParse::ConnectIdentityFacts;
    // The live PostLogin state (seq 1/7/9): accountId readable, persistence character name not yet.
    {
        ConnectIdentityFacts f;
        f.haveAccountId = true;
        f.havePersistenceCharacterName = false;
        f.ageMs = 0;
        CHECK(EventsParse::DecideConnectAnnounce(f) == ConnectAnnounce::Retry,
              "PostLogin with no character name must be held and retried, not announced as 'Tester'");
    }
    // Once the persistence component populates, announce.
    {
        ConnectIdentityFacts f;
        f.haveAccountId = true;
        f.havePersistenceCharacterName = true;
        f.ageMs = 4000;
        CHECK(EventsParse::DecideConnectAnnounce(f) == ConnectAnnounce::Announce,
              "a complete identity announces immediately");
    }
    // A complete identity announces even past the grace window.
    {
        ConnectIdentityFacts f;
        f.haveAccountId = true;
        f.havePersistenceCharacterName = true;
        f.ageMs = 600000;
        CHECK(EventsParse::DecideConnectAnnounce(f) == ConnectAnnounce::Announce, "completeness beats the clock");
    }
    // The bound: a connect is never lost, only ever flagged.
    {
        ConnectIdentityFacts f;
        f.haveAccountId = true;
        f.ageMs = EventsParse::kConnectGraceMs;
        CHECK(EventsParse::DecideConnectAnnounce(f) == ConnectAnnounce::AnnounceIncomplete,
              "at the grace boundary the connect is emitted, flagged incomplete");
        f.ageMs = EventsParse::kConnectGraceMs * 10;
        CHECK(EventsParse::DecideConnectAnnounce(f) == ConnectAnnounce::AnnounceIncomplete,
              "a connect is never dropped for being incomplete");
    }
    // Nothing readable at all still eventually announces: the edge is real even when the identity is not.
    {
        ConnectIdentityFacts f;
        f.ageMs = 99999;
        CHECK(EventsParse::DecideConnectAnnounce(f) == ConnectAnnounce::AnnounceIncomplete,
              "an unidentifiable connect is announced incomplete, never swallowed");
        f.ageMs = 1;
        CHECK(EventsParse::DecideConnectAnnounce(f) == ConnectAnnounce::Retry, "and is retried first");
    }
    // An explicit grace of 0 disables the hold entirely.
    {
        ConnectIdentityFacts f;
        CHECK(EventsParse::DecideConnectAnnounce(f, 0) == ConnectAnnounce::AnnounceIncomplete,
              "graceMs=0 means announce immediately, flagged");
    }
    CHECK(std::string(EventsParse::ConnectAnnounceName(ConnectAnnounce::Retry)) == "retry", "decision names");
    // TOTALITY.
    {
        const uint64_t ages[] = {0, 1, 9999, EventsParse::kConnectGraceMs, 100000};
        size_t n = 0;
        for (int a = 0; a < 2; a++)
            for (int nm = 0; nm < 2; nm++)
                for (uint64_t age : ages) {
                    ConnectIdentityFacts f;
                    f.haveAccountId = a != 0;
                    f.havePersistenceCharacterName = nm != 0;
                    f.ageMs = age;
                    ConnectAnnounce d = EventsParse::DecideConnectAnnounce(f);
                    if (f.haveAccountId && f.havePersistenceCharacterName && d != ConnectAnnounce::Announce)
                        CHECK(false, "a complete identity must always announce");
                    if (d == ConnectAnnounce::Retry && age >= EventsParse::kConnectGraceMs)
                        CHECK(false, "the retry window must be bounded");
                    n++;
                }
        CHECK(n == 2 * 2 * 5, "the connect totality sweep did not cover the space (%zu)", n);
    }
}

// LANE L2d. The wielded weapon. The live bug was `weapon: ""` on every Takaro `entity-killed`, and the
// failure mode a fix like this invites is the OPPOSITE one: confidently naming the firearm a player
// happens to carry as the thing he stabbed something with. So the sweep below asserts both halves —
// that a real match is taken, and that a mismatch produces nothing at all.
static void TestWieldedWeaponDecision() {
    using EventsParse::DecideWieldedWeapon;
    using EventsParse::WeaponSource;
    using EventsParse::WieldedWeaponFacts;

    {  // the melee names this build actually emits, plus the ones that must NOT count as melee
        CHECK(EventsParse::IsMeleeDamageTypeName("BP_DmgType_Melee_Quick_C"), "live melee damage type");
        CHECK(EventsParse::IsMeleeDamageTypeName("BP_DmgType_Melee_Slow_Unshielded_C"), "live melee damage type 2");
        CHECK(EventsParse::IsMeleeDamageTypeName("bp_dmgtype_melee_x"), "melee match is case-insensitive");
        CHECK(!EventsParse::IsMeleeDamageTypeName("BP_DmgType_Dart_Light_C"), "a dart is not melee");
        CHECK(!EventsParse::IsMeleeDamageTypeName(""), "an empty damage type is not melee");
    }

    {  // Tester's exact case: a melee kill by a player carrying both a knife and a gun
        WieldedWeaponFacts f;
        f.haveMeleeName = true;
        f.meleeDamageTypeMatches = true;
        f.damageTypeIsMelee = true;
        f.haveRangedName = true;  // the firearm is present and must lose
        f.weaponInHand = true;
        auto w = DecideWieldedWeapon(f);
        CHECK(w.source == WeaponSource::MeleeDamageTypeMatch, "a melee class-pointer match wins");
        CHECK(w.useMelee, "the melee name is the one to publish");
        CHECK(w.confidence == 2, "a class-pointer match is the strongest evidence");
    }
    {  // a ranged kill: the weapon component's cached damage type is the one that killed
        WieldedWeaponFacts f;
        f.haveRangedName = true;
        f.rangedDamageTypeMatches = true;
        f.haveMeleeName = true;  // a cached knife is always present and must lose
        auto w = DecideWieldedWeapon(f);
        CHECK(w.source == WeaponSource::RangedDamageTypeMatch, "a ranged class-pointer match wins");
        CHECK(!w.useMelee, "the weapon component's name is the one to publish");
        CHECK(w.confidence == 2, "a class-pointer match is the strongest evidence");
    }
    {  // THE ANTI-REGRESSION: a melee damage type, no cached melee name, a gun in hand -> nothing
        WieldedWeaponFacts f;
        f.damageTypeIsMelee = true;
        f.haveRangedName = true;
        f.weaponInHand = true;
        f.weaponComponentActive = true;
        auto w = DecideWieldedWeapon(f);
        CHECK(w.source == WeaponSource::None, "a melee kill must never be attributed to the killer's firearm");
        CHECK(w.confidence == 0, "no evidence means no confidence");
    }
    {  // category fallback: the damage type only says melee
        WieldedWeaponFacts f;
        f.damageTypeIsMelee = true;
        f.haveMeleeName = true;
        auto w = DecideWieldedWeapon(f);
        CHECK(w.source == WeaponSource::MeleeCategory && w.useMelee && w.confidence == 1, "melee category fallback");
    }
    {  // in-hand fallback: a non-melee damage type and a weapon actually out
        WieldedWeaponFacts f;
        f.haveRangedName = true;
        f.weaponComponentActive = true;
        auto w = DecideWieldedWeapon(f);
        CHECK(w.source == WeaponSource::WeaponInHand && !w.useMelee && w.confidence == 1, "in-hand fallback");
    }
    {  // a holstered, inactive weapon is not evidence of anything
        WieldedWeaponFacts f;
        f.haveRangedName = true;
        auto w = DecideWieldedWeapon(f);
        CHECK(w.source == WeaponSource::None, "a name with no in-hand/active signal is not a kill weapon");
    }
    {  // a damage-type match with no NAME cannot be published
        WieldedWeaponFacts f;
        f.meleeDamageTypeMatches = true;
        f.rangedDamageTypeMatches = true;
        auto w = DecideWieldedWeapon(f);
        CHECK(w.source == WeaponSource::None, "a match without a name yields nothing rather than an empty code");
    }
    {  // the source names are stable strings the report and the sidecar log quote
        CHECK(std::string(EventsParse::WeaponSourceName(WeaponSource::None)) == "none", "source name: none");
        CHECK(std::string(EventsParse::WeaponSourceName(WeaponSource::MeleeDamageTypeMatch)) ==
                  "meleeCache:damageTypeClassMatch",
              "source name: melee match");
    }

    {  // TOTALITY: all 128 fact combinations. Every one must be decidable, and the two invariants that
       // matter must hold on every single one of them.
        size_t n = 0;
        for (int bits = 0; bits < 128; bits++) {
            WieldedWeaponFacts f;
            f.haveMeleeName = bits & 1;
            f.meleeDamageTypeMatches = bits & 2;
            f.haveRangedName = bits & 4;
            f.rangedDamageTypeMatches = bits & 8;
            f.weaponInHand = bits & 16;
            f.weaponComponentActive = bits & 32;
            f.damageTypeIsMelee = bits & 64;
            auto w = DecideWieldedWeapon(f);
            if (!w.reason || !*w.reason) CHECK(false, "every decision must carry a reason (bits=%d)", bits);
            // 1. A chosen slot must actually have a name, or the payload would carry "".
            if (w.source != WeaponSource::None && ((w.useMelee && !f.haveMeleeName) || (!w.useMelee && !f.haveRangedName)))
                CHECK(false, "a source was chosen whose name is empty (bits=%d)", bits);
            // 2. A melee damage type may never be answered with the weapon component's name.
            if (f.damageTypeIsMelee && !f.rangedDamageTypeMatches && w.source != WeaponSource::None && !w.useMelee)
                CHECK(false, "a melee damage type was answered with a firearm (bits=%d)", bits);
            if ((w.confidence == 0) != (w.source == WeaponSource::None))
                CHECK(false, "confidence and source disagree (bits=%d)", bits);
            n++;
        }
        CHECK(n == 128, "the wielded-weapon totality sweep did not cover the space (%zu)", n);
    }
}

static void TestPositionSourceRule() {
    using EventsParse::PositionSource;
    // The origin detector. Dune's coordinates are centimetres; a real player is hundreds of metres out.
    CHECK(EventsParse::IsOriginPosition(0, 0, 0), "(0,0,0) is the origin");
    CHECK(EventsParse::IsOriginPosition(0.5, -0.5, 0.25), "sub-centimetre noise is still the origin");
    CHECK(!EventsParse::IsOriginPosition(174940.6743, 294854.2815, 1096.299878),
          "a real live position (seq 2) is not the origin");
    CHECK(!EventsParse::IsOriginPosition(0, 0, 2.0), "one non-zero component is enough to be a position");
    CHECK(!EventsParse::IsOriginPosition(-5000, 0, 0), "negative coordinates are positions too");
    // The rule. This is the fix for the sidecar's 24x `plugin-origin-rejected:playerState`.
    CHECK(EventsParse::DecidePositionSource(true, true, false) == PositionSource::Pawn,
          "the pawn always wins when it has a position");
    CHECK(EventsParse::DecidePositionSource(true, true, true) == PositionSource::Pawn,
          "the pawn wins even when the playerState would also answer");
    CHECK(EventsParse::DecidePositionSource(false, true, true) == PositionSource::None,
          "a playerState position at the origin is NOT a position (the 24 rejections)");
    CHECK(EventsParse::DecidePositionSource(false, true, false) == PositionSource::PlayerState,
          "a non-origin playerState position is usable and labelled as such");
    CHECK(EventsParse::DecidePositionSource(false, false, false) == PositionSource::None,
          "no readable position is an honest 'unknown'");
    CHECK(std::string(EventsParse::PositionSourceName(PositionSource::None)) == "unknown",
          "None serialises as the pre-existing 'unknown' so /players keeps its shape");
    CHECK(std::string(EventsParse::PositionSourceName(PositionSource::Pawn)) == "pawn", "pawn spelling");
    CHECK(std::string(EventsParse::PositionSourceName(PositionSource::PlayerState)) == "playerState",
          "playerState spelling");
    // TOTALITY: the source is never PlayerState for an origin position, and never anything but Pawn
    // when a pawn answered.
    {
        size_t n = 0;
        for (int p = 0; p < 2; p++)
            for (int s = 0; s < 2; s++)
                for (int o = 0; o < 2; o++) {
                    PositionSource d = EventsParse::DecidePositionSource(p != 0, s != 0, o != 0);
                    if (p && d != PositionSource::Pawn) CHECK(false, "a pawn position must always win");
                    if (d == PositionSource::PlayerState && (o != 0 || s == 0))
                        CHECK(false, "playerState may only be chosen for a real, non-origin position");
                    n++;
                }
        CHECK(n == 8, "the position totality sweep did not cover the space (%zu)", n);
    }
}

int main() {
    TouchMarker();
    TestElfParser();
    TestSignatureMatcher();
    TestVTableClassName();
    TestUtf16LiteralSearch();
    TestRipTarget();
    TestProcessEventFingerprint();
    TestJson();
    TestRedaction();
    TestRingBuffer();
    TestCapabilities();
    TestLogLineSplit();
    TestLogRedaction();
    TestLogNoiseFilter();
    TestEngineVersionLine();
    TestIdShapes();
    TestDuneSecretRedaction();
    TestParamRoleMatcher();
    TestDisplayNameDetector();
    TestPresenceDiff();
    TestFrameBoolRead();
    TestDeathDecisionTable();
    TestVictimClassification();
    TestSelfAttribution();
    TestClassPropertyDetector();
    TestConnectAnnounceDecision();
    TestWieldedWeaponDecision();
    TestPositionSourceRule();
    printf("%s: %d checks, %d failed\n", g_failed ? "FAILED" : "PASSED", g_ran, g_failed);
    return g_failed ? 1 : 0;
}
