#!/usr/bin/env bash
# The tool container against the host: same command line, same stdout, same exit code — and
# the image is a toolchain, not a second implementation.
#
# It builds maintenance/Dockerfile, then runs the same argument lists on the host launcher and
# through the image with this checkout mounted at /repo, and compares bytes. It also proves the
# things only the image can be asked: nothing named like a credential is baked in, the code it
# carries is byte-identical to the checkout, and DepotDownloader and steamcmd are present
# without a network. Run it from anywhere; it prints PASS per case and ALL PASS at the end,
# and exits non-zero on the first case that does not behave as stated.
#
# maintenance-ci.yml does not run this (a docker build per pull request is disproportionate):
# it is the local proof, and maintenance/docs/operations.md says so.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
MAINT="$REPO_ROOT/maintenance/bin/takaro-maint"
PLANTED_TOKEN="plantedtoken0123456789"

if ! command -v docker >/dev/null 2>&1; then
  echo "SKIP: docker missing"
  exit 0
fi

IMAGE="takaro-maint:parity-$$"
WORK="$(mktemp -d)"
cleanup() {
  docker rmi -f "$IMAGE" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# Extra docker labels a caller wants on the build and on every container (TTL, owner, run id).
# Empty by default, so nothing about any particular runner is written into this file.
LABELS=()
if [ -n "${TM_DOCKER_LABELS:-}" ]; then
  read -r -a LABELS <<<"$TM_DOCKER_LABELS"
fi
# Every docker invocation below expands LABELS through this form, which stays empty-safe
# under `set -u`.

CASE=0
pass() {
  CASE=$((CASE + 1))
  printf 'PASS %s: %s\n' "$CASE" "$1"
}
fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

# Run a command, keeping its stdout, stderr and exit code instead of ending the script.
rc=0
capture() {
  local out="$1" err="$2"
  shift 2
  rc=0
  "$@" >"$out" 2>"$err" || rc=$?
}

# The documented way to run the image: this checkout mounted at /repo, nothing else.
in_container() {
  docker run --rm ${LABELS[@]+"${LABELS[@]}"} -v "$REPO_ROOT:/repo" "$IMAGE" "$@"
}

echo "building $IMAGE (this takes a few minutes the first time)"
if ! docker build ${LABELS[@]+"${LABELS[@]}"} \
  --build-arg UID="$(id -u)" --build-arg GID="$(id -g)" \
  -t "$IMAGE" -f "$REPO_ROOT/maintenance/Dockerfile" "$REPO_ROOT/maintenance" >"$WORK/build.log" 2>&1; then
  tail -40 "$WORK/build.log" >&2
  fail "maintenance/Dockerfile does not build"
fi

# -- C1: the version string -----------------------------------------------------------------
capture "$WORK/c1.host" "$WORK/c1.host.err" "$MAINT" --version
[ "$rc" = 0 ] || fail "C1: the host leg exited $rc"
capture "$WORK/c1.cont" "$WORK/c1.cont.err" in_container --version
[ "$rc" = 0 ] || fail "C1: the container leg exited $rc"
cmp -s "$WORK/c1.host" "$WORK/c1.cont" || fail "C1: --version differs between the host and the container"
pass "--version is the same command in both places"

# -- C2: the catalog the container reads is the mounted one ----------------------------------
capture "$WORK/c2.host" "$WORK/c2.host.err" "$MAINT" targets list --format json
[ "$rc" = 0 ] || fail "C2: the host leg exited $rc"
capture "$WORK/c2.cont" "$WORK/c2.cont.err" in_container targets list --format json
[ "$rc" = 0 ] || fail "C2: the container leg exited $rc"
cmp -s "$WORK/c2.host" "$WORK/c2.cont" || fail "C2: 'targets list' differs between the host and the container"
pass "'targets list --format json' is byte-identical in both places"

# -- C3: validation, with the container cut off from the network -----------------------------
# --network none also proves the run-time `uv run --frozen` fetches nothing: the venv, the
# managed interpreter and every dependency are already in the image.
capture "$WORK/c3.host" "$WORK/c3.host.err" "$MAINT" catalog validate
[ "$rc" = 0 ] || fail "C3: the host leg exited $rc"
capture "$WORK/c3.cont" "$WORK/c3.cont.err" \
  docker run --rm --network none ${LABELS[@]+"${LABELS[@]}"} -v "$REPO_ROOT:/repo" "$IMAGE" catalog validate
[ "$rc" = 0 ] || fail "C3: the offline container leg exited $rc"
cmp -s "$WORK/c3.host" "$WORK/c3.cont" || fail "C3: 'catalog validate' differs between the host and the container"
pass "'catalog validate' is byte-identical, and the container needs no network to answer"

# -- C4: the tracker path with no credential anywhere ----------------------------------------
# The host leg gets a PATH with only what the launcher itself needs, so that neither `gh` nor a
# developer's own tooling can supply a token the container would not have.
BIN="$WORK/bin"
mkdir -p "$BIN"
for tool in env bash uv; do
  ln -s "$(command -v "$tool")" "$BIN/$tool"
done
capture "$WORK/c4.host" "$WORK/c4.host.err" env -u GH_TOKEN "PATH=$BIN" "$MAINT" run --repo example/none
[ "$rc" = 9 ] || fail "C4: the host leg exited $rc, not 9"
capture "$WORK/c4.cont" "$WORK/c4.cont.err" \
  docker run --rm --network none ${LABELS[@]+"${LABELS[@]}"} -v "$REPO_ROOT:/repo" "$IMAGE" run --repo example/none
[ "$rc" = 9 ] || fail "C4: the container leg exited $rc, not 9"
cmp -s "$WORK/c4.host" "$WORK/c4.cont" || fail "C4: the credential-less report differs between the two legs"
grep -q 'GH_TOKEN' "$WORK/c4.host" || fail "C4: the report does not name GH_TOKEN"
pass "without a credential both legs exit 9 with the same report"

# -- C5: a planted credential reaches no output and no layer ---------------------------------
capture "$WORK/c5.host" "$WORK/c5.host.err" \
  env "GH_TOKEN=$PLANTED_TOKEN" "$MAINT" --verbose run --repo example/none --api-url http://127.0.0.1:9/
[ "$rc" = 9 ] || fail "C5: the host leg exited $rc, not 9"
capture "$WORK/c5.cont" "$WORK/c5.cont.err" \
  docker run --rm --network none ${LABELS[@]+"${LABELS[@]}"} -v "$REPO_ROOT:/repo" \
  -e "GH_TOKEN=$PLANTED_TOKEN" "$IMAGE" --verbose run --repo example/none --api-url http://127.0.0.1:9/
[ "$rc" = 9 ] || fail "C5: the container leg exited $rc, not 9"
for file in c5.host c5.host.err c5.cont c5.cont.err; do
  if grep -qF "$PLANTED_TOKEN" "$WORK/$file"; then fail "C5: the planted token reached $file"; fi
done
docker image inspect --format '{{.Config.Env}}' "$IMAGE" >"$WORK/c5.env"
docker history --no-trunc "$IMAGE" >"$WORK/c5.history"
for file in c5.env c5.history; do
  if grep -qiE 'token|password|secret' "$WORK/$file"; then fail "C5: $file names a credential"; fi
  if grep -qF "$PLANTED_TOKEN" "$WORK/$file"; then fail "C5: the planted token is baked into $file"; fi
done
pass "a planted credential appears in no output, no image environment and no layer"

# -- C6: one implementation, and the tooling is already in the image -------------------------
# Deliberately unmounted: what is compared is the copy the image was built with, so a second
# scanner smuggled into the image would show up as a difference against the checkout.
hashes='cd /repo/maintenance/src && find . -type f ! -path "*__pycache__*" | sort | xargs sha256sum'
capture "$WORK/c6.image" "$WORK/c6.image.err" \
  docker run --rm --network none ${LABELS[@]+"${LABELS[@]}"} --entrypoint sh "$IMAGE" -c "$hashes"
[ "$rc" = 0 ] || fail "C6: hashing the image's sources exited $rc"
capture "$WORK/c6.host" "$WORK/c6.host.err" sh -c "cd '$REPO_ROOT/maintenance/src' && \
  find . -type f ! -path '*__pycache__*' | sort | xargs sha256sum"
[ "$rc" = 0 ] || fail "C6: hashing the checkout's sources exited $rc"
diff -u "$WORK/c6.host" "$WORK/c6.image" >"$WORK/c6.diff" ||
  fail "C6: the image's sources differ from the checkout's ($(wc -l <"$WORK/c6.diff") diff lines)"
ensure='from pathlib import Path; from takaro_maint.steam import depotdownloader; print(depotdownloader.ensure(Path.home() / ".cache" / "takaro-maint"))'
capture "$WORK/c6.dd" "$WORK/c6.dd.err" \
  docker run --rm --network none ${LABELS[@]+"${LABELS[@]}"} --entrypoint uv "$IMAGE" \
  run --frozen --project /repo/maintenance python -c "$ensure"
[ "$rc" = 0 ] || fail "C6: DepotDownloader is not in the image (offline ensure exited $rc)"
capture "$WORK/c6.steam" "$WORK/c6.steam.err" \
  docker run --rm --network none ${LABELS[@]+"${LABELS[@]}"} --entrypoint steamcmd "$IMAGE" +quit
[ "$rc" = 0 ] || fail "C6: steamcmd is not runnable offline (exited $rc)"
pass "the image ships the checkout's code and nothing else, with DepotDownloader and steamcmd baked in"

# -- C7: the fixture scenario inside the container -------------------------------------------
# --network none keeps loopback, which is all the in-process fakes use: a fresh "runner" with
# no state but the mount resumes from the tracker alone.
capture "$WORK/c7.out" "$WORK/c7.err" \
  docker run --rm --network none ${LABELS[@]+"${LABELS[@]}"} -v "$REPO_ROOT:/repo" --entrypoint uv "$IMAGE" \
  run --frozen --project /repo/maintenance pytest -q \
  /repo/maintenance/tests/test_dashboard_show.py /repo/maintenance/tests/test_scan_dedup.py -p no:cacheprovider
if [ "$rc" != 0 ]; then
  tail -40 "$WORK/c7.out" "$WORK/c7.err" >&2
  fail "C7: the fixture scenario failed inside the container (exit $rc)"
fi
pass "the fixture scenario passes inside the container, offline"

echo "ALL PASS ($CASE cases)"
