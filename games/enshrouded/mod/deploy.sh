#!/usr/bin/env bash
# An installed Enshrouded server is described by its ledger, and the DLL that belongs next
# to it is the one the catalog target names. Build both components for the resolved target
# and let `takaro-maint deploy` place them, so what ends up next to enshrouded_server.exe is
# recorded rather than remembered.
set -euo pipefail
cat >&2 <<'MSG'
mod/deploy.sh has been replaced.

  maintenance/bin/takaro-maint build  --game enshrouded --version <v> --out <dir>
  maintenance/bin/takaro-maint deploy --game enshrouded --dest <server dir> --from <dir>/build-manifest.json

Stop the game container first: a running server holds dbghelp.dll open.
See games/enshrouded/DEVELOPMENT.md.
MSG
exit 2
