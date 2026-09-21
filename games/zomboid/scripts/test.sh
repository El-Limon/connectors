#!/usr/bin/env bash
# Runs the agent's and core's JUnit suites against one catalog target's game jar.
#
# Usage: test.sh [--target <catalog target id>]
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/../../.." && pwd)
# shellcheck source=lib-target.sh
. "${SCRIPT_DIR}/lib-target.sh"
# shellcheck source=lib-gradle.sh
. "${SCRIPT_DIR}/lib-gradle.sh"

zomboid_parse_target_flag "$@"
zomboid_resolve_target "${TARGET}"

"${SCRIPT_DIR}/setup-environment.sh" --target "${ZOMBOID_TARGET}"

mapfile -t properties < <(zomboid_gradle_target_properties "${REPO_ROOT}" "")
echo "Running the Zomboid JUnit suites for ${ZOMBOID_TARGET} in ${ZOMBOID_TOOLCHAIN}..."
# shellcheck disable=SC2086 # EXTRA_GRADLE_ARGS is a deliberate word-split of caller flags
zomboid_gradle "${REPO_ROOT}" test "${properties[@]}" ${EXTRA_GRADLE_ARGS}
