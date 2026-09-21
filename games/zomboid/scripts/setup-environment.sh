#!/usr/bin/env bash
# Stages exactly the server jar the agent compiles against, for one catalog target.
#
# The only source is `takaro-maint steam references`, which downloads java/projectzomboid.jar
# from the pinned depot manifests. A bind mount, a running container or a SteamCMD branch
# head are not sources: none of them says which build it just handed over, and a reference
# whose hash is not the target's pin is refused (exit 5) rather than warned about.
#
# Usage: setup-environment.sh [--target <catalog target id>]
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
# shellcheck source=lib-target.sh
. "${SCRIPT_DIR}/lib-target.sh"

zomboid_parse_target_flag "$@"
zomboid_resolve_target "${TARGET}"

cd "${PROJECT_ROOT}"
echo "Setting up the Project Zomboid build environment for ${ZOMBOID_TARGET} (${ZOMBOID_FP16})..."

REFERENCES="./_data/references/${ZOMBOID_FP16}"
JAR="${REFERENCES}/projectzomboid.jar"

# Exactly the file build.references selects, from the pinned depot manifests.
"${TAKARO_MAINT}" steam references \
    --game zomboid --target "${ZOMBOID_TARGET}" --dest "${REFERENCES}"

# The one file whose hash decides whether this agent can be built at all. The catalog says
# what it must be; there is no override, because an override asserts nothing about the
# build it would let through.
printf '%s  %s\n' "${ZOMBOID_GAME_JAR_SHA256}" "${JAR}" \
    | sha256sum --check --status \
    || { echo "error: ${JAR} is not the one ${ZOMBOID_TARGET} pins" >&2; exit 5; }

# `just zomboid-build` and a bare `./gradlew build` compile against _deps/projectzomboid.jar,
# so that name is kept as a link into the fingerprint-scoped reference directory.
mkdir -p ./_deps
ln -sfn "../_data/references/${ZOMBOID_FP16}/projectzomboid.jar" ./_deps/projectzomboid.jar

echo "Environment ready: ${PROJECT_ROOT}/${JAR#./}"
echo "    sha256 ${ZOMBOID_GAME_JAR_SHA256}"
echo "Build the agent with: ./scripts/build-release.sh <version> <out-dir> --target ${ZOMBOID_TARGET}"
