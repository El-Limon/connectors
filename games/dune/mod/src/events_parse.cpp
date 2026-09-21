// Pure parsing helpers for the Dune: Awakening server log. See events_parse.h.
#include "events_parse.h"

#include <cctype>
#include <cstring>

namespace {

bool AllDigits(const std::string& s) {
    if (s.empty()) return false;
    for (char c : s)
        if (!isdigit((unsigned char)c)) return false;
    return true;
}

bool AllHex(const std::string& s) {
    if (s.empty()) return false;
    for (char c : s)
        if (!isxdigit((unsigned char)c)) return false;
    return true;
}

std::string Trim(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && (s[a] == ' ' || s[a] == '\t' || s[a] == '\r')) a++;
    while (b > a && (s[b - 1] == ' ' || s[b - 1] == '\t' || s[b - 1] == '\r')) b--;
    return s.substr(a, b - a);
}

// Removes the value of `?<key>=` / `<key>=` up to the next delimiter, in place.
void MaskAfter(std::string& s, const char* key, const char* delims) {
    size_t klen = strlen(key);
    size_t pos = 0;
    while ((pos = s.find(key, pos)) != std::string::npos) {
        size_t v = pos + klen;
        size_t end = v;
        while (end < s.size() && !strchr(delims, s[end])) end++;
        if (end > v) {
            s.replace(v, end - v, "<redacted>");
            pos = v + 10;
        } else {
            pos = v;
        }
    }
}

}  // namespace

EventsParse::LogLine EventsParse::SplitLogLine(const std::string& raw) {
    LogLine out;
    std::string s = raw;
    while (!s.empty() && (s.back() == '\r' || s.back() == '\n')) s.pop_back();
    size_t body = 0;
    if (!s.empty() && s[0] == '[') {
        size_t t1 = s.find(']');
        if (t1 != std::string::npos && t1 + 1 < s.size() && s[t1 + 1] == '[') {
            size_t t2 = s.find(']', t1 + 2);
            if (t2 != std::string::npos) {
                out.hasPrefix = true;
                out.timestamp = s.substr(1, t1 - 1);
                out.frame = Trim(s.substr(t1 + 2, t2 - (t1 + 2)));
                body = t2 + 1;
            }
        }
    }
    std::string rest = s.substr(body);
    // "Category: message" - the category is a bare identifier, so a colon inside the message never
    // wins (a chat message is full of colons).
    size_t colon = rest.find(": ");
    if (colon != std::string::npos && colon > 0 && colon <= 48) {
        bool ident = true;
        for (size_t i = 0; i < colon; i++)
            if (!isalnum((unsigned char)rest[i]) && rest[i] != '_') { ident = false; break; }
        if (ident) {
            out.category = rest.substr(0, colon);
            out.message = rest.substr(colon + 2);
            return out;
        }
    }
    out.message = rest;
    return out;
}

std::string EventsParse::ParseEngineVersionLine(const std::string& raw) {
    LogLine l = SplitLogLine(raw);
    // The engine prints it through LogInit at Display verbosity; accept the bare form too, since the
    // very first lines of the log have no category prefix at all.
    static const char* kKey = "Unreal Engine version: ";
    const std::string& hay = l.message.empty() ? raw : l.message;
    size_t p = hay.find(kKey);
    if (p == std::string::npos) return "";
    return Trim(hay.substr(p + strlen(kKey)));
}

std::string EventsParse::RedactLogLine(const std::string& in) {
    // Two shapes, because one redactor cannot serve both.
    //
    // 1. A stock UE URL line - `LogNet: Login request: ?Password=<pw>?Name=<n>?...`. Its fields are
    //    separated by '?', which common.cpp's Redact() does not treat as a value terminator, so
    //    Redact() alone would eat the display name along with the password. Here the '?'-delimited
    //    secrets are masked individually and Redact() is deliberately NOT run on the line, so the
    //    non-secret fields the connect fallback needs survive.
    // 2. Everything else - the ini / JSON / CLI forms Redact() already handles, plus Dune's own
    //    secret keys, which reach the log through `-ini:engine:[...]:Key=Value` command lines and
    //    through the notification-system error messages.
    const bool urlLine = (in.find("Login request:") != std::string::npos ||
                          in.find("Join request:") != std::string::npos) &&
                         in.find("?Name=") != std::string::npos;
    std::string s = in;
    static const char* kUrlKeys[] = {"Password=", "password=", "Ticket=", "ticket=", "AuthTicket=",
                                     "Token=",    "token=",    "?p=",     "Secret=", "secret="};
    for (const char* k : kUrlKeys) MaskAfter(s, k, "?& \t");
    // Dune's own keys. The delimiters include '"' and ',' because these appear inside the engine's
    // `-ini:` command-line echo and inside JSON-ish diagnostic messages.
    static const char* kDuneKeys[] = {
        "ServerCommandsAuthToken=", "ServerCommandsAuthToken\":", "ServiceAuthToken=", "ServiceAuthToken\":",
        "Bgd.ServerLoginPassword=", "DatabasePassword=",           "DatabasePassword\":",
        "FuncomLiveServices__ServiceAuthToken=", "RMQ_HTTP_TOKEN_AUTH_SECRET=",
    };
    for (const char* k : kDuneKeys) MaskAfter(s, k, "?&,\" \t");
    if (!urlLine) s = Redact(s);
    return s;
}

bool EventsParse::IsNoise(const std::string& line) {
    if (line.empty()) return true;
    static const char* kNoise[] = {
        // Stock UE chatter that says nothing on a dedicated server.
        "LogNetTraffic", "LogNetSerialization", "LogPackageName: Verbose", "LogStreaming: Display: Flushing",
        "LogSlate: Verbose", "LogAudio", "LogRHI: Verbose",
        // Dune ships these already turned up in DefaultEngine.ini [Core.Log]; at Verbose they are
        // per-frame or per-object and would drown the ring buffer.
        "LogWorldPersistenceSubsystem: Verbose", "LogAuto: Verbose", "LogHandshake: Display",
        "LogBattlegroupDirectorClientSubsystem: VeryVerbose",
    };
    for (const char* n : kNoise)
        if (line.find(n) != std::string::npos) return true;
    return false;
}

bool EventsParse::LooksLikeSteamId64(const std::string& s) {
    return s.size() == 17 && AllDigits(s) && s.rfind("7656119", 0) == 0;
}

bool EventsParse::LooksLikeFlsId(const std::string& s) {
    if (s.size() != 16 || !AllHex(s)) return false;
    // An all-zero id is what an uninitialised PlayerState reports; it is never a real player.
    for (char c : s)
        if (c != '0') return true;
    return false;
}

// ---------------------------------------------------------------------------------------------
// LANE L2: the role matcher. See the long note in events_parse.h for why this lives in the pure,
// unit-tested module rather than next to the detour that uses it.

bool EventsParse::IsObjectPropertyType(const std::string& type) {
    return type == "ObjectProperty" || type == "WeakObjectProperty" || type == "InterfaceProperty" ||
           type == "ClassProperty" || type == "SoftObjectProperty" || type == "LazyObjectProperty";
}

int EventsParse::MatchParamRole(const std::vector<ParamProp>& props, ParamRole role) {
    // Priority-ordered, most specific first.
    static const char* kVictim[] = {"victim",         "deadcharacter", "killedcharacter", "defeated",
                                    "deadactor",      "damagedactor",  "targetcharacter", "targetactor",
                                    "target",         nullptr};
    static const char* kKiller[] = {"killer",    "eventinstigator", "instigatorcontroller", "instigatorcharacter",
                                    "instigator", "attacker",       "damagedealer",         nullptr};
    static const char* kCauser[] = {"damagecauser", "causer", "damagesource", "sourceactor", nullptr};
    static const char* kWeapon[] = {"weapon", "damagetype", "abilityclass", nullptr};
    const char* const* keys = kVictim;
    switch (role) {
        case ParamRole::Killer: keys = kKiller; break;
        case ParamRole::Causer: keys = kCauser; break;
        case ParamRole::Weapon: keys = kWeapon; break;
        case ParamRole::Victim: break;
    }
    // A weapon may legitimately be a class or struct reference (a damage type), so the
    // object-reference requirement is relaxed only for that role.
    const bool requireObject = role != ParamRole::Weapon;
    for (size_t k = 0; keys[k]; k++) {
        for (size_t i = 0; i < props.size(); i++) {
            if (props[i].offset < 0) continue;
            if (requireObject && !IsObjectPropertyType(props[i].type)) continue;
            std::string lower = props[i].name;
            for (auto& c : lower) c = (char)tolower((unsigned char)c);
            if (lower.find(keys[k]) == std::string::npos) continue;
            // `DamageCauser` contains neither "victim" nor "killer", but a name like
            // `InstigatorDamageCauser` would match both — the causer keys are more specific, so a
            // parameter already claimed by a more specific role must not be re-claimed here. That is
            // the caller's job (it fills the roles in order); what this function guarantees is only
            // that the FIRST matching key wins, deterministically.
            return (int)i;
        }
    }
    return -1;
}

// ---------------------------------------------------------------------------------------------
// LANE L2: is this string a DISPLAY name, or a developer identifier?
//
// `memory: catalogue-human-names` — `listItems`/`listEntities` and every `entity-killed` must carry a
// display name, never a dev/class name. On this build the question is live rather than theoretical:
// `DuneNpcCharacter::m_Name` turned out to hold real names ("Mobula Gang Member", "Slaver Trapper",
// "Ariste Atreides") on the running server, while the AI spawner's own log speaks in row keys
// ("T3_Band_Slv_Reg_Marksman"). The two are easy to tell apart, and the cost of being wrong is
// shipping `BP_Npc_SoldierBase_Character_Baked_C` to Takaro as a creature's name.
//
// The rules mirror the sidecar catalogue generator's `isDevName()` so that the plugin and the
// sidecar agree about what a name is:
//   * an underscore anywhere, or a UE asset prefix (BP_/DT_/SK_/SM_/T_/UI_/WBP_/ABP_), is a dev name;
//   * a trailing `_C`, or ALLCAPS, is a dev name;
//   * an internal CamelCase run with no spaces ("SoldierBase") is a dev name;
//   * digits mixed into a single token ("Thing07") is a dev name;
//   * what is left — words, spaces, apostrophes, hyphens — is a display name.
bool EventsParse::LooksLikeDisplayName(const std::string& s) {
    if (s.size() < 2 || s.size() > 96) return false;
    if (s.find('_') != std::string::npos) return false;
    bool hasSpace = false, hasLower = false, hasUpper = false, hasDigit = false;
    for (unsigned char c : s) {
        if (c == ' ') hasSpace = true;
        else if (islower(c)) hasLower = true;
        else if (isupper(c)) hasUpper = true;
        else if (isdigit(c)) hasDigit = true;
        else if (c != '\'' && c != '-' && c != '.' && c != ',') return false;  // no exotic punctuation
    }
    if (!hasLower) return false;              // ALLCAPS or digits-only is not a display name
    if (hasDigit && !hasSpace) return false;  // `Thing07`
    if (hasSpace) return true;                // multi-word names are display names
    // A single token: only a display name when it is not internal CamelCase ("Kindjal" yes,
    // "SoldierBase" no). Count uppercase letters after the first character.
    size_t innerUpper = 0;
    for (size_t i = 1; i < s.size(); i++)
        if (isupper((unsigned char)s[i])) innerUpper++;
    (void)hasUpper;
    return innerUpper == 0;
}

// ---------------------------------------------------------------------------------------------
// LANE L2b: bool-from-frame read, and the death decision table. See events_parse.h for the two
// hypotheses this replaces and for which one the live build falsified.

EventsParse::BoolRead EventsParse::ReadFrameBool(uint8_t raw, bool readOk) {
    if (!readOk) return BoolRead::Unreadable;
    if (raw == 0) return BoolRead::False;
    if (raw == 1) return BoolRead::True;
    // A UHT function parameter is a whole `bool` holding exactly 0 or 1. Any other byte means the
    // offset is not what we think it is (a packed bitfield, a shifted frame, a different build), so
    // the honest answer is "unknown" and the caller must not gate on it.
    return BoolRead::Unreadable;
}

const char* EventsParse::BoolReadName(EventsParse::BoolRead b) {
    switch (b) {
        case BoolRead::False: return "false";
        case BoolRead::True: return "true";
        default: return "unreadable";
    }
}

EventsParse::DeathDecision EventsParse::DecideDeath(const EventsParse::DeathFacts& f) {
    DeathDecision d;
    d.deathKind = f.isDeath == BoolRead::True ? "death" : (f.isDeath == BoolRead::False ? "defeat" : "unknown");

    // 1. Direction is unproven for ReceiveMulticastKill, so it can never be an event (events.h, F20).
    if (f.isMulticastKillHint) {
        d.verdict = DeathVerdict::Hint;
        d.reason = "ReceiveMulticastKill: self is the killer, direction unproven - killer hint only";
        return d;
    }
    // 2. One death per victim per window, whichever function reported it first.
    if (f.duplicate) {
        d.verdict = DeathVerdict::DropDuplicate;
        d.reason = "the same victim was already reported inside the dedupe window";
        return d;
    }
    // 3. THE knock-down gate. `bShouldEnterDbno` is the engine's own down-but-not-out flag and the
    //    only parameter on any of these functions that means "not dead". A true here is a revivable
    //    knock-down and must not be a death.
    if (f.dbno == BoolRead::True) {
        d.verdict = DeathVerdict::DropKnockDown;
        d.reason = "bShouldEnterDbno=true: down-but-not-out, not a death";
        return d;
    }
    // 4. Corroboration, players only, and only when it can refuse. A player is the one victim type
    //    with a revive mechanic; if its PlayerState still reads Alive a moment after the call, the
    //    call was not lethal. `bIsDeath=true` overrides, a stale read is ignored, and an unreadable
    //    life state never blocks an event (every capability degrades, nothing fails).
    if (f.victimIsPlayer && f.isDeath != BoolRead::True && f.haveLifeState && f.lifeStateFresh &&
        f.lifeState == kLifeStateAlive) {
        d.verdict = DeathVerdict::DropKnockDown;
        d.reason = "the victim's PlayerState still reads m_LifeState=Alive after the call";
        return d;
    }
    // 5. Everything else is a death. `bIsDeath=false` is Dune's "DEFEATED", which IS a death: it is
    //    what the game's own UI prints, and the down-but-not-out case is covered by rule 3.
    d.verdict = DeathVerdict::Emit;
    d.reason = f.isDeath == BoolRead::True    ? "bIsDeath=true"
               : f.isDeath == BoolRead::False ? "defeat (bIsDeath=false) is a lethal Dune death; no DBNO flag set"
                                              : "no readable bIsDeath and no DBNO flag set";
    return d;
}

// ---------------------------------------------------------------------------------------------
// LANE L2c. See the long notes in events_parse.h for the live evidence behind each rule.

EventsParse::VictimClassification EventsParse::ClassifyVictim(const EventsParse::VictimFacts& f) {
    VictimClassification c;
    // 1. THE CLASS CHAIN DECIDES. It is readable for as long as the object is and survives every
    //    respawn, so it — and never the registry — decides whether a player died.
    if (f.classChainIsPlayer) {
        c.type = DeathEventType::PlayerDeath;
        c.identityComplete = f.identityResolved;
        c.reason = f.identityResolved
                       ? "the victim's class chain derives a player class and the registry resolved it"
                       : "the victim's class chain derives a player class; identity unresolved, emitted as an "
                         "incomplete player-death rather than a creature kill";
        return c;
    }
    // 2. The registry can still be right when the class chain was unreadable (a class pointer that
    //    failed LooksLikeUObject, a build that renamed the player class). Identity then promotes.
    if (f.identityResolved) {
        c.type = DeathEventType::PlayerDeath;
        c.identityComplete = true;
        c.reason = "the class chain did not say player, but the registry resolved the victim to a tracked player";
        return c;
    }
    // 3. A non-player victim. Only nameable creatures may be published (catalogue-human-names).
    c.type = DeathEventType::EntityKilled;
    c.identityComplete = true;
    c.dropUnnamed = !f.haveDisplayName;
    c.reason = f.haveDisplayName ? "a non-player victim with a human display name"
                                 : "a non-player victim with no display name: entity is null and the sidecar "
                                   "must drop it rather than publish a class name";
    return c;
}

const char* const EventsParse::kSelfAttribution = "self-or-environment";

bool EventsParse::IsSelfAttribution(const EventsParse::AttributionFacts& f) {
    if (!f.haveKiller) return false;
    return f.killerIsVictimActor || f.killerIsVictimPlayer;
}

bool EventsParse::PropertyHoldsClassPointer(const std::string& type) {
    // `ClassProperty` is what this build reports for `DeathDefeatCausingDamageType`. The other two are
    // the same idea in the engine's soft/raw variants; listing them keeps a future build honest.
    return type == "ClassProperty" || type == "SoftClassProperty" || type == "ClassPtrProperty";
}

// ---------------------------------------------------------------------------------------------
// LANE L2d. The wielded weapon. See the long note in events_parse.h for the live layout.

bool EventsParse::IsMeleeDamageTypeName(const std::string& n) {
    if (n.empty()) return false;
    std::string low = n;
    for (auto& c : low) c = (char)tolower((unsigned char)c);
    return low.find("melee") != std::string::npos;
}

EventsParse::WieldedWeapon EventsParse::DecideWieldedWeapon(const EventsParse::WieldedWeaponFacts& f) {
    WieldedWeapon w;
    // 1/2. THE DAMAGE-TYPE CLASS POINTERS MATCHED. This is not an inference: the character caches the
    //      damage-type class of the weapon it is carrying, and the death frame carries the damage-type
    //      class that killed. Equal pointers mean that weapon dealt this damage.
    if (f.meleeDamageTypeMatches && f.haveMeleeName) {
        w.source = WeaponSource::MeleeDamageTypeMatch;
        w.useMelee = true;
        w.confidence = 2;
        w.reason = "the killer's cached melee weapon carries exactly the damage-type class that killed";
        return w;
    }
    if (f.rangedDamageTypeMatches && f.haveRangedName) {
        w.source = WeaponSource::RangedDamageTypeMatch;
        w.confidence = 2;
        w.reason = "the killer's weapon component caches exactly the damage-type class that killed";
        return w;
    }
    // 3. The damage type only says "melee". The cached melee weapon is then the only candidate, and
    //    the gun in the other hand is explicitly NOT one.
    if (f.damageTypeIsMelee) {
        if (f.haveMeleeName) {
            w.source = WeaponSource::MeleeCategory;
            w.useMelee = true;
            w.confidence = 1;
            w.reason = "a melee damage type and a cached melee weapon on the killer";
            return w;
        }
        w.reason = "a melee damage type but no cached melee weapon name; refusing to name the killer's firearm";
        return w;
    }
    // 4. Not a melee damage type, and the killer really does have a weapon out. Weaker than a pointer
    //    match (a second firearm would look the same) but it names the thing that was being used.
    if (f.haveRangedName && (f.weaponInHand || f.weaponComponentActive)) {
        w.source = WeaponSource::WeaponInHand;
        w.confidence = 1;
        w.reason = "a non-melee damage type and a weapon active in the killer's hands";
        return w;
    }
    return w;
}

const char* EventsParse::WeaponSourceName(EventsParse::WeaponSource s) {
    switch (s) {
        case WeaponSource::None: return "none";
        case WeaponSource::MeleeDamageTypeMatch: return "meleeCache:damageTypeClassMatch";
        case WeaponSource::RangedDamageTypeMatch: return "weaponComponent:damageTypeClassMatch";
        case WeaponSource::MeleeCategory: return "meleeCache:meleeDamageType";
        case WeaponSource::WeaponInHand: return "weaponComponent:inHand";
    }
    return "none";
}

EventsParse::ConnectAnnounce EventsParse::DecideConnectAnnounce(const EventsParse::ConnectIdentityFacts& f,
                                                                uint64_t graceMs) {
    // The account id is the sidecar's join key and the character name is the only human-facing field,
    // so "complete" means both. Neither is invented and neither is waited for forever.
    if (f.haveAccountId && f.havePersistenceCharacterName) return ConnectAnnounce::Announce;
    if (f.ageMs < graceMs) return ConnectAnnounce::Retry;
    return ConnectAnnounce::AnnounceIncomplete;
}

const char* EventsParse::ConnectAnnounceName(EventsParse::ConnectAnnounce a) {
    switch (a) {
        case ConnectAnnounce::Announce: return "announce";
        case ConnectAnnounce::Retry: return "retry";
        case ConnectAnnounce::AnnounceIncomplete: return "announceIncomplete";
    }
    return "?";
}

bool EventsParse::IsOriginPosition(double x, double y, double z, double eps) {
    return (x < 0 ? -x : x) <= eps && (y < 0 ? -y : y) <= eps && (z < 0 ? -z : z) <= eps;
}

EventsParse::PositionSource EventsParse::DecidePositionSource(bool pawnHasPosition, bool playerStateHasPosition,
                                                             bool playerStateAtOrigin) {
    if (pawnHasPosition) return PositionSource::Pawn;
    if (playerStateHasPosition && !playerStateAtOrigin) return PositionSource::PlayerState;
    return PositionSource::None;
}

const char* EventsParse::PositionSourceName(EventsParse::PositionSource s) {
    switch (s) {
        case PositionSource::Pawn: return "pawn";
        case PositionSource::PlayerState: return "playerState";
        case PositionSource::None: return "unknown";
    }
    return "unknown";
}
