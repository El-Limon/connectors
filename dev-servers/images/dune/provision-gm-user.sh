#!/usr/bin/env bash
# Provision the AMQP account the Takaro sidecar publishes GM commands as.
#
# WHY A USER LITERALLY NAMED `fls`
#   The map process refuses any server command whose AMQP `user_id` property is not
#   `fls` ("Invalid Sender ID, we only accept server commands from 'fls'."), and
#   RabbitMQ refuses to publish a message whose `user_id` differs from the connection's
#   authenticated user. So the publisher must BE `fls`.
#   Funcom ship no such account: the map server mints its own broker users at runtime
#   over the management API, and their broker resolves every login through the HTTP
#   backend (text-router), which only knows those service accounts. That is why the
#   rig's game.conf puts `auth_backends.1 = internal` ahead of the cached HTTP backend —
#   with that one line a locally created `fls` user can log in, and the sidecar needs no
#   Docker socket and no `rabbitmqctl eval`.
#
# Usage: provision-gm-user.sh <container> <password> [user]
#   e.g. provision-gm-user.sh takaro-dev-dune-game-rmq-1 "$DUNE_GM_AMQP_PASS"
#
# Idempotent. The password is never echoed.
set -euo pipefail

CONTAINER="${1:?usage: provision-gm-user.sh <game-rmq container> <password> [user]}"
PASSWORD="${2:?a password is required}"
USER_NAME="${3:-fls}"

rmq() { docker exec -i "$CONTAINER" rabbitmqctl "$@"; }

echo "==> waiting for ${CONTAINER} to answer rabbitmqctl"
for _ in $(seq 1 60); do
    rmq -q status >/dev/null 2>&1 && break
    sleep 2
done
rmq -q status >/dev/null || { echo "error: ${CONTAINER} never came up" >&2; exit 1; }

if rmq -q list_users --formatter=json 2>/dev/null | grep -q "\"${USER_NAME}\""; then
    echo "==> ${USER_NAME} exists; resetting its password"
    rmq -q change_password "$USER_NAME" "$PASSWORD"
else
    echo "==> creating user ${USER_NAME}"
    rmq -q add_user "$USER_NAME" "$PASSWORD"
fi

# `management` (not administrator) is enough to read queue/exchange stats over the
# HTTP API for verification; publishing needs only the permissions below.
rmq -q set_user_tags "$USER_NAME" management

# configure: nothing — we declare nothing on the game broker with this account.
# write:     the exchanges we publish to (GM commands + chat out).
# read:      the chat.intercept fan-out and our own durable queue.
rmq -q set_permissions -p / "$USER_NAME" \
    '^takaro_.*$' \
    '^(heartbeats|chat\..*|takaro_.*)$' \
    '^(chat\..*|takaro_.*)$'

# Enabling the internal backend also revives the stock guest/guest account (tagged
# administrator). The game broker's AMQPS listener is published on the host, so remove it
# there; do the admin broker too, so neither has a default login.
for c in "$CONTAINER" "${DUNE_ADMIN_RMQ_CONTAINER:-takaro-dev-dune-admin-rmq}"; do
    docker inspect "$c" >/dev/null 2>&1 || continue
    if docker exec -i "$c" rabbitmqctl -q list_users --formatter=json 2>/dev/null | grep -q '"guest"'; then
        echo "==> deleting the default guest user on ${c}"
        docker exec -i "$c" rabbitmqctl -q delete_user guest || true
    fi
done

echo "==> done. permissions for ${USER_NAME}:"
rmq -q list_user_permissions "$USER_NAME"
