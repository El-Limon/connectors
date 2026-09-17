#!/usr/bin/env bash
# Rig focus: declare which games this box is actually working on, then make the
# running set match that declaration.
#
# Only game servers with an active campaign should run on this box. Everything
# else costs RAM, disk and attention for nothing. `focus.sh` keeps an explicit
# "active set" in dev-servers/_data/.focus and `focus.sh apply` reconciles the
# running containers with it — never on a timer, always because you asked.
#
# Usage:
#   focus.sh status [--porcelain]      what is declared vs what is running
#   focus.sh add <game>... [--note S]  add games to the active set
#   focus.sh drop <game>...            remove games from the active set
#   focus.sh set <game>... [--note S]  replace the active set
#   focus.sh release <game>...         drop, then stop those games
#   focus.sh apply [options]           reconcile the running set with the active set
#
# apply options:
#   --dry-run        print the plan and stop
#   --force          stop games whose rig lock looks live (ask first — really)
#   --allow-empty    allow an empty active set (stops every managed game)
#   --idle-hours N   a rig lock older than N hours counts as idle (default 6)
#
# apply only ever touches games in dev-servers' own registry. Anything else on
# this docker host — a private server, an unregistered rig — is listed as
# "unmanaged (never touched)" and left strictly alone.
set -euo pipefail
# shellcheck source=../lib/common.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../lib" && pwd)/common.sh"

DS_SCRIPTS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# ── State file ───────────────────────────────────────────────────────────────
# One line per declared game: "<game> since=<UTC ISO8601> <note>"

focus_games() {
    [ -f "$DS_FOCUS" ] || return 0
    awk 'NF && $1 !~ /^#/ {print $1}' "$DS_FOCUS"
}

focus_has() { focus_games | grep -qx -- "$1"; }

focus_since() {
    [ -f "$DS_FOCUS" ] || return 0
    awk -v g="$1" '$1==g {sub(/^since=/,"",$2); print $2; exit}' "$DS_FOCUS"
}

focus_note() {
    [ -f "$DS_FOCUS" ] || return 0
    awk -v g="$1" '$1==g {$1=""; $2=""; sub(/^[[:space:]]+/,""); print; exit}' "$DS_FOCUS"
}

# focus_write <game>... — atomic replace, preserving since= of games already declared.
focus_write() {
    local note="$1"; shift
    local tmp game since old_note
    mkdir -p "$(dirname "$DS_FOCUS")"
    tmp="$(mktemp "${DS_FOCUS}.XXXXXX")"
    {
        printf '# dev-servers rig focus — the games this box is actively working on.\n'
        printf '# Managed by dev-servers/scripts/focus.sh; apply is always explicit.\n'
        for game in "$@"; do
            since="$(focus_since "$game")"
            [ -n "$since" ] || since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
            old_note="$(focus_note "$game")"
            [ -n "$note" ] && old_note="$note"
            printf '%s since=%s %s\n' "$game" "$since" "$old_note"
        done
    } > "$tmp"
    mv -f "$tmp" "$DS_FOCUS"
}

# ── Rig locks ────────────────────────────────────────────────────────────────
# A campaign holds ~/<campaign>/.runtime/<game>-rig.lock. It is advisory: we use
# it only to avoid stopping a rig somebody else is driving right now.

ds_lock_path() {
    local game="$1" dirs pattern p
    pattern="${DEV_SERVERS_RIG_LOCK_DIRS:-}"
    [ -n "$pattern" ] || pattern="${HOME}/*/.runtime"
    # Word-split on whitespace, then glob-expand each entry.
    read -r -a dirs <<< "$pattern"
    for p in "${dirs[@]}"; do
        for d in $p; do
            [ -f "${d}/${game}-rig.lock" ] && { printf '%s' "${d}/${game}-rig.lock"; return 0; }
        done
    done
    return 1
}

# none | done | idle | live
ds_lock_state() {
    local game="$1" path age_h mtime now
    path="$(ds_lock_path "$game")" || { printf 'none'; return; }
    if grep -qiE '(^|[[:space:]])(cell|state|status)=done([[:space:]]|$)' "$path" 2>/dev/null; then
        printf 'done'; return
    fi
    if ! flock -n "$path" -c true 2>/dev/null; then
        printf 'live'; return
    fi
    local pid
    pid="$(grep -oE '(^|[[:space:]])pid=[0-9]+' "$path" 2>/dev/null | head -1 | tr -dc '0-9' || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        printf 'live'; return
    fi
    mtime="$(stat -c %Y "$path" 2>/dev/null || echo 0)"
    now="$(date +%s)"
    age_h=$(( (now - mtime) / 3600 ))
    if [ "$age_h" -lt "$IDLE_HOURS" ]; then printf 'live'; else printf 'idle'; fi
}

# ── Unmanaged projects ───────────────────────────────────────────────────────
# Every running compose project on this host that is not a registry project.
ds_unmanaged_projects() {
    local managed g name rest
    managed="$(for g in $(ds_game_ids); do ds_compose_project "$g"; echo; done | sort -u)"
    docker compose ls --format json 2>/dev/null | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    rows = []
for r in rows:
    cfg = (r.get("ConfigFiles") or "").split(",")[0].split("/")[-1]
    print(r.get("Name", ""), cfg)
' | while read -r name rest; do
        [ -n "$name" ] || continue
        printf '%s\n' "$managed" | grep -qx -- "$name" && continue
        printf '%s [%s]\n' "$name" "$rest"
    done
}

# ── Commands ─────────────────────────────────────────────────────────────────
cmd_status() {
    local porcelain=0 arg game running lock drift=0
    for arg in "$@"; do
        case "$arg" in
            --porcelain) porcelain=1 ;;
            *) ds_die "unknown option ${arg}" ;;
        esac
    done
    for game in $(ds_game_ids); do
        if ds_is_running "$game"; then running=1; else running=0; fi
        if focus_has "$game"; then infocus=1; else infocus=0; fi
        lock="$(ds_lock_state "$game")"
        [ "$infocus" != "$running" ] && drift=1
        if [ "$porcelain" -eq 1 ]; then
            printf '%s\tfocus=%s\trunning=%s\tlock=%s\tsince=%s\tnote=%s\n' \
                "$game" "$infocus" "$running" "$lock" "$(focus_since "$game")" "$(focus_note "$game")"
        fi
    done
    if [ "$porcelain" -eq 1 ]; then
        [ "$drift" -eq 0 ] || return 1
        return 0
    fi

    ds_info "Active set (dev-servers/_data/.focus)"
    if [ -z "$(focus_games)" ]; then
        printf '  (empty — nothing is declared)\n'
    else
        for game in $(focus_games); do
            ds_is_running "$game" && running="running" || running="STOPPED"
            printf '  %-20s %-8s lock=%-5s since=%s %s\n' \
                "$game" "$running" "$(ds_lock_state "$game")" "$(focus_since "$game")" "$(focus_note "$game")"
        done
    fi
    echo
    ds_info "Running but not declared (drift)"
    local any=0
    for game in $(ds_game_ids); do
        focus_has "$game" && continue
        ds_is_running "$game" || continue
        any=1
        printf '  %-20s lock=%s\n' "$game" "$(ds_lock_state "$game")"
    done
    [ "$any" -eq 1 ] || printf '  (none)\n'
    echo
    ds_info "Unmanaged projects (never touched)"
    local u
    u="$(ds_unmanaged_projects)"
    if [ -n "$u" ]; then printf '%s\n' "$u" | sed 's/^/  /'; else printf '  (none)\n'; fi
    [ "$drift" -eq 0 ] || return 1
}

parse_games_and_note() {
    NOTE=""
    SELECT=()
    local expect_note=0 arg
    for arg in "$@"; do
        if [ "$expect_note" -eq 1 ]; then NOTE="$arg"; expect_note=0; continue; fi
        case "$arg" in
            --note) expect_note=1 ;;
            --note=*) NOTE="${arg#--note=}" ;;
            -*) ds_die "unknown option ${arg}" ;;
            *) ds_validate_game "$arg"; SELECT+=("$arg") ;;
        esac
    done
    [ "$expect_note" -eq 0 ] || ds_die "--note needs a value"
}

cmd_add() {
    parse_games_and_note "$@"
    [ ${#SELECT[@]} -gt 0 ] || ds_die "usage: focus.sh add <game>... [--note \"why\"]"
    local merged=() game
    mapfile -t merged < <(focus_games)
    for game in "${SELECT[@]}"; do
        focus_has "$game" || merged+=("$game")
    done
    focus_write "$NOTE" "${merged[@]}"
    ds_ok "active set: $(focus_games | tr '\n' ' ')"
}

cmd_drop() {
    parse_games_and_note "$@"
    [ ${#SELECT[@]} -gt 0 ] || ds_die "usage: focus.sh drop <game>..."
    local kept=() game
    for game in $(focus_games); do
        printf '%s\n' "${SELECT[@]}" | grep -qx -- "$game" && continue
        kept+=("$game")
    done
    focus_write "" ${kept[@]+"${kept[@]}"}
    ds_ok "active set: $(focus_games | tr '\n' ' ')"
}

cmd_set() {
    parse_games_and_note "$@"
    [ ${#SELECT[@]} -gt 0 ] || ds_die "usage: focus.sh set <game>... [--note \"why\"]"
    focus_write "$NOTE" "${SELECT[@]}"
    ds_ok "active set: $(focus_games | tr '\n' ' ')"
}

cmd_release() {
    parse_games_and_note "$@"
    [ ${#SELECT[@]} -gt 0 ] || ds_die "usage: focus.sh release <game>..."
    cmd_drop "$@"
    ds_info "Stopping released games: ${SELECT[*]}"
    "${DS_SCRIPTS}/stop.sh" "${SELECT[@]}"
}

cmd_apply() {
    local dry=0 force=0 allow_empty=0 arg expect_hours=0
    for arg in "$@"; do
        if [ "$expect_hours" -eq 1 ]; then IDLE_HOURS="$arg"; expect_hours=0; continue; fi
        case "$arg" in
            --dry-run) dry=1 ;;
            --force) force=1 ;;
            --allow-empty) allow_empty=1 ;;
            --idle-hours) expect_hours=1 ;;
            --idle-hours=*) IDLE_HOURS="${arg#--idle-hours=}" ;;
            *) ds_die "unknown option ${arg}" ;;
        esac
    done
    [ "$expect_hours" -eq 0 ] || ds_die "--idle-hours needs a value"
    case "$IDLE_HOURS" in ''|*[!0-9]*) ds_die "--idle-hours must be a whole number of hours" ;; esac

    local active=() game
    mapfile -t active < <(focus_games)
    if [ ${#active[@]} -eq 0 ] && [ "$allow_empty" -ne 1 ]; then
        ds_die "the active set is empty — apply would stop every managed game.
  Declare what you are working on: focus.sh set <game>... --note \"why\"
  Or say you really mean it:      focus.sh apply --allow-empty"
    fi

    local to_stop=() to_start=() keep=() protected=()
    for game in $(ds_game_ids); do
        if focus_has "$game"; then
            if ds_is_running "$game"; then keep+=("$game"); else to_start+=("$game"); fi
        else
            ds_is_running "$game" || continue
            if [ "$(ds_lock_state "$game")" = "live" ] && [ "$force" -ne 1 ]; then
                protected+=("$game")
            else
                to_stop+=("$game")
            fi
        fi
    done

    ds_info "focus apply plan (idle threshold: ${IDLE_HOURS}h)"
    printf '  stop:      %s\n' "${to_stop[*]:-(none)}"
    printf '  keep:      %s\n' "${keep[*]:-(none)}"
    printf '  start:     %s\n' "${to_start[*]:-(none)}"
    printf '  protected: %s\n' "${protected[*]:-(none)}"
    printf '  unmanaged (never touched):\n'
    local u
    u="$(ds_unmanaged_projects)"
    if [ -n "$u" ]; then printf '%s\n' "$u" | sed 's/^/    /'; else printf '    (none)\n'; fi

    local budget after=0
    budget="${DEV_SERVERS_RAM_BUDGET_GB:-40}"
    for game in ${keep[@]+"${keep[@]}"} ${to_start[@]+"${to_start[@]}"} ${protected[@]+"${protected[@]}"}; do
        after=$((after + $(ds_ram_gb "$game")))
    done
    printf '  budget after: ~%d GB of %d GB\n' "$after" "$budget"

    [ "$dry" -eq 0 ] || return 0

    if [ ${#protected[@]} -gt 0 ]; then
        ds_warn "these games hold a live rig lock — another campaign is driving them:"
        for game in "${protected[@]}"; do
            printf '    %s  %s\n' "$game" "$(ds_lock_path "$game" || echo '(no lock file)')"
            sed 's/^/      | /' "$(ds_lock_path "$game")" 2>/dev/null | head -10 || true
        done
        ds_die "refusing to stop a live rig. Talk to whoever holds it, or add it to the active set.
  --force overrides this; do not use it to win an argument with another campaign."
    fi

    if [ ${#to_stop[@]} -gt 0 ]; then
        # start.sh refuses to start a game without an install marker, so a game
        # that is running unmarked (installed by hand or by another lane) would
        # become unstartable the moment we stop it. Mark it first.
        for game in "${to_stop[@]}"; do
            if [ ! -f "$(ds_marker "$game")" ]; then
                ds_info "recording install marker for ${game} (running but unmarked)"
                mkdir -p "$(dirname "$(ds_marker "$game")")"
                printf 'marked by focus.sh apply on %s (was running without a marker)\n' \
                    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$(ds_marker "$game")"
            fi
        done
        "${DS_SCRIPTS}/stop.sh" "${to_stop[@]}"
    fi

    if [ ${#to_start[@]} -gt 0 ]; then
        # Deliberately no --force: the RAM budget gate stays authoritative.
        "${DS_SCRIPTS}/start.sh" "${to_start[@]}"
    fi

    echo
    cmd_status || true
}

# ── Main ─────────────────────────────────────────────────────────────────────
ds_load_env
IDLE_HOURS="${DEV_SERVERS_FOCUS_IDLE_HOURS:-6}"

[ $# -gt 0 ] || usage 1
SUB="$1"; shift
case "$SUB" in
    status)  cmd_status "$@" ;;
    add)     cmd_add "$@" ;;
    drop)    cmd_drop "$@" ;;
    set)     cmd_set "$@" ;;
    release) cmd_release "$@" ;;
    apply)   cmd_apply "$@" ;;
    -h|--help|help) usage 0 ;;
    *) ds_die "unknown subcommand '${SUB}'. Try: focus.sh --help" ;;
esac
