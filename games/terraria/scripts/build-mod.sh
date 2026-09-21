#!/usr/bin/env bash
# Compiles the TShock plugin for one catalog target, in that target's pinned .NET SDK image.
#
# The host's own `dotnet` is never used: two builds of one commit have to produce identical
# bytes, and only the pinned SDK can promise that.
#
# `IncludeSourceRevisionInInformationalVersion=false` is part of that promise, not decoration:
# without it the SDK asks whatever git tree happens to be mounted for a SourceRevisionId and
# appends `+<sha>` to the assembly's InformationalVersion. That sha is the checkout's HEAD, which
# on a pull request is GitHub's throwaway merge commit -- a revision that exists in no clone --
# while a build from a worktree resolves none at all. Either way the DLL stops matching a rebuild
# of the commit it is named after. The version the plugin reports is `InformationalVersion` alone.
#
# Usage: build-mod.sh [--target <catalog target id>]
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
REPO_ROOT=$(cd -- "${PROJECT_ROOT}/../.." && pwd)
# shellcheck source=lib-target.sh
. "${SCRIPT_DIR}/lib-target.sh"

terraria_parse_target_flag "$@"
terraria_resolve_target "${TARGET}"

cd "${PROJECT_ROOT}"
REFS="_data/refs/${TERRARIA_FP16}"
if [ ! -f "${REFS}/.takaro/references.json" ]; then
    echo "error: no reference assemblies for ${TERRARIA_TARGET} in games/terraria/${REFS}" >&2
    echo "       run ./scripts/setup-environment.sh --target ${TERRARIA_TARGET} first" >&2
    exit 2
fi

# A release is built from nothing but the sources and the pinned references: msbuild's
# intermediate output decides what is copied out, so it never carries over from an earlier
# build of another target or version.
rm -rf ./mod/TakaroTerrariaEvents/bin ./mod/TakaroTerrariaEvents/obj ./_data/build

docker run --rm \
    --user "$(id -u):$(id -g)" \
    -v "${REPO_ROOT}:${REPO_ROOT}" \
    -w "${REPO_ROOT}/games/terraria" \
    -e HOME=/tmp \
    -e DOTNET_CLI_HOME=/tmp \
    -e NUGET_PACKAGES=/tmp/nuget \
    -e DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    -e DOTNET_NOLOGO=1 \
    "${TERRARIA_TOOLCHAIN}" \
    dotnet publish mod/TakaroTerrariaEvents/TakaroTerrariaEvents.csproj \
        -c Release \
        -o _data/build/TakaroTerrariaEvents \
        -p:TShockReferencePath="${REPO_ROOT}/games/terraria/${REFS}" \
        -p:Deterministic=true \
        -p:ContinuousIntegrationBuild=true \
        -p:DebugType=none \
        -p:InformationalVersion="${VERSION:-dev}" \
        -p:IncludeSourceRevisionInInformationalVersion=false

echo "Built games/terraria/_data/build/TakaroTerrariaEvents/TakaroTerrariaEvents.dll"
