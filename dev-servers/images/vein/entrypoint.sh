#!/usr/bin/env bash
# VEIN dedicated server entrypoint for the Takaro dev rig.
#
# 1. installs/updates the depot only when it has to
# 2. renders Game.ini / Engine.ini from env (unknown keys survive)
# 3. neuters crashpad/Sentry so plugin crashes never reach Ramjet
# 4. drops to uid 1000 (steam) and exec's the game with LD_PRELOAD set ONLY here
set -euo pipefail

log() { printf '[takaro-entrypoint] %s\n' "$*"; }

APPID="${VEIN_APPID:-2131400}"
DIR="${VEIN_DIR:-/home/steam/vein}"
BIN_REL="Vein/Binaries/Linux/VeinServer-Linux-Test"
BIN="${DIR}/${BIN_REL}"
CFG_DIR="${DIR}/Vein/Saved/Config/LinuxServer"
STEAMCMD="${STEAMCMDDIR:-/home/steam/steamcmd}/steamcmd.sh"

mkdir -p "$DIR"
if [ "$(id -u)" = "0" ]; then
    # The host bind-mount may arrive root-owned; the game must run as `steam`.
    if [ "$(stat -c '%u' "$DIR")" != "1000" ]; then
        log "chown ${DIR} -> steam"
        chown steam:steam "$DIR"
    fi
fi

run_as_steam() {
    if [ "$(id -u)" = "0" ]; then
        setpriv --reuid=steam --regid=steam --init-groups "$@"
    else
        "$@"
    fi
}

# ── 1. depot ────────────────────────────────────────────────────────────────
need_install=0
[ -s "$BIN" ] || { need_install=1; log "server binary missing -> install required"; }
case "${VEIN_AUTO_UPDATE:-false}" in
    true|TRUE|1|yes) need_install=1; log "VEIN_AUTO_UPDATE is on -> update requested" ;;
esac

if [ "$need_install" = "1" ]; then
    log "SteamCMD: app_update ${APPID} into ${DIR} (anonymous)"
    # NOTE: no `validate` here on purpose — a validate run deletes anything it
    # does not know about in the tree. LD_PRELOAD is NOT set for this call.
    n=0
    until run_as_steam env -u LD_PRELOAD HOME=/home/steam "$STEAMCMD" \
            +force_install_dir "$DIR" \
            +login anonymous \
            +app_update "$APPID" \
            +quit; do
        n=$((n + 1))
        [ "$n" -ge 4 ] && { log "SteamCMD failed ${n} times, giving up"; exit 1; }
        log "SteamCMD attempt ${n} failed (this is common on the first run); retrying in 15s"
        sleep 15
    done
    log "SteamCMD finished"
fi

[ -s "$BIN" ] || { log "FATAL: ${BIN} still missing after install"; exit 1; }
chmod +x "$BIN" 2>/dev/null || true

# ── 2. config ───────────────────────────────────────────────────────────────
mkdir -p "$CFG_DIR" "${DIR}/Vein/Saved/Logs"
bool() { case "${1:-}" in true|TRUE|1|yes|True) echo True ;; *) echo False ;; esac; }

admin_json() {
    # VEIN_ADMIN_STEAMIDS: comma/space separated -> one ini line per id.
    python3 - "$1" <<'PY'
import json, re, sys
ids = [x for x in re.split(r'[,\s]+', sys.argv[1].strip()) if x]
print(json.dumps(ids))
PY
}

ADMINS="$(admin_json "${VEIN_ADMIN_STEAMIDS:-}")"
SUPERADMINS="$(admin_json "${VEIN_SUPERADMIN_STEAMIDS:-${VEIN_ADMIN_STEAMIDS:-}}")"

python3 - "$CFG_DIR" "$ADMINS" "$SUPERADMINS" <<'PY' > /tmp/game-ini.json
import json, os, sys
admins, supers = json.loads(sys.argv[2]), json.loads(sys.argv[3])
env = os.environ.get
def b(name, default="false"):
    return "True" if str(env(name, default)).lower() in ("1","true","yes") else "False"
cfg = {
  "[/Script/Engine.GameSession]": {
      "MaxPlayers": env("VEIN_MAXPLAYERS", "8"),
  },
  "[/Script/Vein.VeinGameSession]": {
      "ServerName": env("VEIN_SERVER_NAME", "Takaro Dev Vein"),
      "ServerDescription": env("VEIN_SERVER_DESCRIPTION", ""),
      "bPublic": b("VEIN_PUBLIC", "true"),
      "Password": env("VEIN_SERVER_PASSWORD", ""),
      # UE array config properties need the "+Key=value" line syntax, one line per
      # entry. Plain "AdminSteamIDs=<id>" parses but leaves the array EMPTY
      # (observed 2026-09-17: "LogVein: Admins set:" with nothing after it).
      # The plain keys are listed with an empty list so any stale plain line from
      # an earlier render is removed.
      "AdminSteamIDs": [],
      "SuperAdminSteamIDs": [],
      "+AdminSteamIDs": ['"%s"' % a for a in admins],
      "+SuperAdminSteamIDs": ['"%s"' % a for a in supers],
      # Anti-cheat OFF: the Takaro plugin is an LD_PRELOAD into the game process.
      "bAntiCheatProtected": "False",
      "HTTPPort": env("VEIN_HTTP_PORT", "8080"),
  },
  "[OnlineSubsystemSteam]": {
      "GameServerQueryPort": env("VEIN_QUERY_PORT", "27015"),
      "bVACEnabled": "False",
  },
}
print(json.dumps(cfg))
PY
python3 /usr/local/bin/render-config.py "${CFG_DIR}/Game.ini" < /tmp/game-ini.json
rm -f /tmp/game-ini.json
log "rendered ${CFG_DIR}/Game.ini"

# Keep the built-in HTTP API loopback-only: it is reachable from the sidecar
# (which shares this network namespace) and from nowhere else.
printf '%s' '{"[HTTPServer.Listeners]": {"DefaultBindAddress": "127.0.0.1"}}' \
  | python3 /usr/local/bin/render-config.py "${CFG_DIR}/Engine.ini"
log "rendered ${CFG_DIR}/Engine.ini"

if [ "$(id -u)" = "0" ]; then
    chown -R steam:steam "$CFG_DIR" "${DIR}/Vein/Saved/Logs" 2>/dev/null || true
fi

# ── 3. crash reporting off ────────────────────────────────────────────────────
# Our plugin will crash this process during development. Nothing about that may
# reach Ramjet Studios.
#
# HARD-LEARNED (2026-09-17): the depot DOES ship
#   Vein/Plugins/Sentry/Binaries/Linux/crashpad_handler
# and removing its exec bit does NOT disable crash reporting — it kills the
# server at boot:
#   [FATAL spawn_subprocess.cc:222] posix_spawn .../crashpad_handler:
#   Permission denied (13)  →  Signal 5 caught.
# So the handler is kept executable and Sentry is disarmed through its own
# settings instead.
while IFS= read -r handler; do
    [ -n "$handler" ] || continue
    [ -x "$handler" ] || { log "restoring exec bit on ${handler#"$DIR"/} (crashpad must be spawnable)"; chmod +x "$handler" || true; }
done <<< "$(find "$DIR" -maxdepth 8 -type f \
        \( -name 'crashpad_handler*' -o -name 'CrashReportClient*' \) 2>/dev/null || true)"

printf '%s' '{"[/Script/Sentry.SentrySettings]": {"Dsn": "", "InitAutomatically": "False", "EnableAutomaticCrashCapturing": "False", "UploadDumpsToSentry": "False"}}' \
  | python3 /usr/local/bin/render-config.py "${CFG_DIR}/Engine.ini"
export SENTRY_DSN="" UE_SENTRY_DSN="" SENTRY_ENVIRONMENT="takaro-dev"
log "Sentry disarmed in Engine.ini (Dsn empty, InitAutomatically=False)"
if [ "$(id -u)" = "0" ]; then
    # The game runs as steam and rewrites its own config on shutdown; every file
    # we render must belong to it or the save fails with errno=13.
    chown -R steam:steam "${DIR}/Vein/Saved" 2>/dev/null || true
fi

# ── 4. launch ───────────────────────────────────────────────────────────────
PRELOAD=""
if [ -n "${TAKARO_PLUGIN_SO:-}" ] && [ -s "${TAKARO_PLUGIN_SO}" ]; then
    PRELOAD="${TAKARO_PLUGIN_SO}"
    log "LD_PRELOAD=${PRELOAD}"
else
    log "no Takaro plugin to preload (${TAKARO_PLUGIN_SO:-unset})"
fi

cd "$DIR"
# shellcheck disable=SC2086  # VEIN_EXTRA_ARGS is intentionally word-split
set -- "./${BIN_REL}" \
    -Port="${VEIN_PORT:-7777}" \
    -QueryPort="${VEIN_QUERY_PORT:-27015}" \
    -log ${VEIN_EXTRA_ARGS:-}

log "exec $*"
# exec => the game is PID 1 and receives SIGTERM from `docker stop` directly.
if [ "$(id -u)" = "0" ]; then
    exec setpriv --reuid=steam --regid=steam --init-groups \
        env LD_PRELOAD="${PRELOAD}" "$@"
else
    exec env LD_PRELOAD="${PRELOAD}" "$@"
fi
