#!/bin/sh
# Runs INSIDE the sidecar container as DUNE_SHUTDOWN_CMD.
#
# The sidecar deliberately has no Docker socket, so it cannot stop the map container itself and
# Dune's own `ServerShutdown` GM command only draws a countdown. This is the documented operator
# hook: a request file on a shared volume, picked up by the `shutdown-agent` container (which is
# the only thing holding the socket, read-only, and which can stop exactly one container).
#
# Exits 0 ONLY when the agent has confirmed the container actually stopped — the sidecar turns a
# non-zero exit into an honest ActionError instead of a quiet "verified: true".
set -u
DIR=/shutdown
REQ="$DIR/request"
ACK="$DIR/ack"
rm -f "$ACK" 2>/dev/null
date -u +%FT%TZ > "$REQ" || { echo "cannot write $REQ"; exit 1; }
i=0
while [ "$i" -lt 280 ]; do
  if [ -f "$ACK" ]; then
    cat "$ACK"
    grep -q '^stopped' "$ACK" && exit 0
    exit 1
  fi
  i=$((i+1)); sleep 1
done
echo "timeout: shutdown-agent did not acknowledge within 280s"
exit 1
