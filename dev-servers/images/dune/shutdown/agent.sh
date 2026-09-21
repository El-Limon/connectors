#!/bin/sh
# The `shutdown-agent` container: the ONLY thing on this rig with the Docker socket.
# It watches one file on a shared volume and can do exactly one thing — stop, then restart,
# the map container named in TARGET. It never reads a command from the request file.
set -u
DIR=/shutdown
TARGET="${TARGET:?TARGET container name required}"
RESTART_AFTER="${RESTART_AFTER:-10}"
# The UE map server saves the world on SIGTERM. 120 s was NOT enough on this rig (L6d: exit 137, i.e.
# Docker SIGKILLed it), so this matches compose's own `stop_grace_period: 4m` for the survival service.
STOP_TIMEOUT="${STOP_TIMEOUT:-240}"
echo "shutdown-agent watching $DIR/request for $TARGET (restart after ${RESTART_AFTER}s)"
while true; do
  if [ -f "$DIR/request" ]; then
    rm -f "$DIR/request"
    echo "$(date -u +%FT%TZ) stop requested for $TARGET"
    if docker stop -t "$STOP_TIMEOUT" "$TARGET" >/dev/null 2>&1; then
      echo "stopped $TARGET at $(date -u +%FT%TZ)" > "$DIR/ack"
      echo "$(date -u +%FT%TZ) stopped $TARGET; restarting in ${RESTART_AFTER}s"
      sleep "$RESTART_AFTER"
      docker start "$TARGET" >/dev/null 2>&1 && echo "$(date -u +%FT%TZ) started $TARGET"
    else
      echo "failed to stop $TARGET at $(date -u +%FT%TZ)" > "$DIR/ack"
    fi
  fi
  sleep 1
done
