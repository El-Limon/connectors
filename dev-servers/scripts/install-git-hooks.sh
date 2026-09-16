#!/usr/bin/env bash
# Installs git hooks that check whether deployed connectors have gone stale
# after the repo changes (git pull, merge, branch switch).
#
# Usage: install-git-hooks.sh [--auto] [--uninstall]
#   --auto       rebuild and redeploy automatically instead of only reporting
#   --uninstall  remove the hooks again
#
# The hooks only ever report or redeploy; they never start or stop a game
# without you asking, so a `git pull` can't disrupt a running test session.
set -euo pipefail
DS_LIB="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../lib" && pwd)"
# shellcheck source=../lib/common.sh
. "${DS_LIB}/common.sh"

MODE="check"
UNINSTALL=0
for arg in "$@"; do
    case "$arg" in
        --auto)      MODE="auto" ;;
        --uninstall) UNINSTALL=1 ;;
        *)           ds_die "unknown option ${arg}" ;;
    esac
done

HOOK_DIR="$(git -C "$REPO_ROOT" rev-parse --git-path hooks)"
HOOK_DIR="$(cd "$REPO_ROOT" && cd "$HOOK_DIR" && pwd)"
MARKER="# >>> takaro dev-servers connector sync >>>"
HOOKS=(post-merge post-checkout post-rewrite)

remove_block() {
    local file="$1"
    [ -f "$file" ] || return 0
    python3 - "$file" "$MARKER" <<'PY'
import sys
path, marker = sys.argv[1], sys.argv[2]
end = marker.replace('>>>', '<<<')
lines = open(path).readlines()
out, skip = [], False
for line in lines:
    if line.startswith(marker):
        skip = True
        continue
    if skip and line.startswith(end):
        skip = False
        continue
    if not skip:
        out.append(line)
open(path, 'w').writelines(out)
PY
    # Drop the file entirely if only a shebang is left behind.
    if [ "$(grep -cvE '^\s*(#!.*)?\s*$' "$file")" -eq 0 ]; then rm -f "$file"; fi
}

if [ "$UNINSTALL" -eq 1 ]; then
    for h in "${HOOKS[@]}"; do
        remove_block "${HOOK_DIR}/${h}"
        ds_ok "removed sync block from ${h}"
    done
    exit 0
fi

# $REPO must stay literal here — it is expanded inside the generated hook, not now.
# shellcheck disable=SC2016
if [ "$MODE" = "auto" ]; then
    ACTION='"$REPO/dev-servers/scripts/sync-connectors.sh" --restart || true'
else
    ACTION='"$REPO/dev-servers/scripts/sync-connectors.sh" --check || true'
fi

for h in "${HOOKS[@]}"; do
    file="${HOOK_DIR}/${h}"
    remove_block "$file"                      # keep it idempotent
    [ -f "$file" ] || printf '#!/usr/bin/env bash\n' > "$file"
    cat >> "$file" <<EOF
${MARKER}
# Added by dev-servers/scripts/install-git-hooks.sh — safe to remove.
REPO="\$(git rev-parse --show-toplevel)"
if [ -x "\$REPO/dev-servers/scripts/sync-connectors.sh" ]; then
    ${ACTION}
fi
${MARKER//>>>/<<<}
EOF
    chmod +x "$file"
    ds_ok "installed ${h} hook (${MODE} mode)"
done

echo
ds_info "Hooks fire on: git pull / git merge / git checkout / git rebase"
if [ "$MODE" = "check" ]; then
    ds_info "They only REPORT. Re-run with --auto to rebuild and restart automatically."
else
    ds_info "They rebuild, redeploy and restart affected games automatically."
fi
ds_info "Remove them with: dev-servers/scripts/install-git-hooks.sh --uninstall"
