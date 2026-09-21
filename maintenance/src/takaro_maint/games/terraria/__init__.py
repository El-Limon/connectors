"""The Terraria adapter: a TShock server image, a .NET plugin and a Node bridge.

Three things are unlike every other game here.

The server is *only* a container image. TShock publishes a release zip too, and the target
pins it as ``inputs.server`` because that is the version identity a maintainer and
``catalog validate --online`` can both check — but the bytes that run are the image's, and
the assemblies the plugin compiles against are extracted from that same image by digest.

The connector is two artifacts, not one: a TShock plugin (``plugins/``) and a Node bridge
(``bridge/``) that holds the websocket to Takaro and drives the server over TShock's REST
API. Both are built per target and deployed together.

And the server is configured entirely on its command line: TShock reads no environment
variable for the world to load, so this adapter hands the runner a ``container_command``
and the run never sees the interactive world-selection menu.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

from ... import output, paths
from ...exit_codes import BuildFailed, ConflictError
from ..base import BuildResult

GAME_ID = "terraria"
REFERENCES_ROOT = "games/terraria/_data/refs"
DIST_ROOT = "games/terraria/_data/dist"
BUILD_SCRIPT = "games/terraria/scripts/build-release.sh"

#: The one folder a plugin zip may write, and the file the server actually loads from it.
PLUGIN_FOLDER = "TakaroTerrariaEvents"
PLUGIN_DLL = "TakaroTerrariaEvents.dll"
#: The one folder a bridge zip may write; the operator's TakaroConfig.txt sits beside it.
BRIDGE_FOLDER = "TakaroTerrariaBridge"

#: The world one verification run creates, inside the container's ``/worlds`` mount.
VERIFY_WORLD = "takaro-verify"

# What the two banners look like. Terraria's own line carries the game version; TShock's
# "now running" line carries the loader version. Neither line carries both.
_TERRARIA_BANNER = re.compile(r"Terraria Server v(?P<version>[0-9][0-9.]*)")
_TSHOCK_BANNER = re.compile(r"TShock (?P<version>[0-9][0-9.]*)[^\n]*now running")


def _env_key(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


class TerrariaAdapter:
    id = GAME_ID

    # -- description ----------------------------------------------------------
    def env(self, resolved: dict[str, Any], prefix: str) -> dict[str, str]:
        """What the rig, the scripts and CI read. No ``_JAVA``: this server runs on .NET."""
        server = resolved["inputs"]["server"]
        deps = resolved["build"]["deps"]
        env = {
            f"{prefix}_TARGET": str(resolved["id"]),
            f"{prefix}_FINGERPRINT": str(resolved["fingerprint"]),
            f"{prefix}_FP16": str(resolved["fp16"]),
            f"{prefix}_IMAGE": str(resolved["containerRef"]),
            f"{prefix}_IMAGE_DIGEST": str(resolved["runtime"]["container"]["digest"]),
            f"{prefix}_TSHOCK_TAG": str(resolved["runtime"]["container"]["tag"]),
            f"{prefix}_TOOLCHAIN": str(resolved["toolchainRef"]),
            f"{prefix}_REVISION": str(resolved["revision"]),
            f"{prefix}_SERVER_ASSET": str(server["asset"]),
            f"{prefix}_SERVER_URL": str(resolved["resolvedUrls"].get("server", "")),
            f"{prefix}_SERVER_SHA256": str(server["sha256"]),
            f"{prefix}_PLUGIN_ARTIFACT": str(resolved["artifactFileNames"]["plugin"]),
            f"{prefix}_BRIDGE_ARTIFACT": str(resolved["artifactFileNames"]["bridge"]),
            f"{prefix}_REFERENCES_DIR": f"{REFERENCES_ROOT}/{resolved['fp16']}",
            f"{prefix}_REFERENCES": ";".join(str(path) for path in resolved["build"]["references"]),
            f"{prefix}_BRIDGE_IMAGE": str(deps["bridge-runtime"]["resolvedCoordinate"]),
        }
        # Every pinned dependency, so a script can check a file against the catalog before
        # it compiles anything against it.
        for name, dep in sorted(deps.items()):
            key = _env_key(name)
            env[f"{prefix}_DEP_{key}_URL"] = str(dep.get("resolvedCoordinate", dep["coordinate"]))
            env[f"{prefix}_DEP_{key}_SHA256"] = str(dep["sha256"])
        return env

    def preserve_globs(self, resolved: dict[str, Any]) -> list[str]:
        return list(resolved.get("preserve", []))

    def parse_runtime_identity(self, log_line: str) -> dict[str, Any] | None:
        """One banner line, as far as it goes. ``verify`` merges the two."""
        terraria = _TERRARIA_BANNER.search(log_line)
        if terraria:
            return {"gameVersion": terraria.group("version"), "loader": "tshock", "loaderVersion": None}
        tshock = _TSHOCK_BANNER.search(log_line)
        if tshock:
            return {"gameVersion": None, "loader": "tshock", "loaderVersion": tshock.group("version")}
        return None

    # -- build ----------------------------------------------------------------
    def artifact_paths(self, resolved: dict[str, Any], version: str, repo_root: Path) -> dict[str, Path]:
        out = repo_root / DIST_ROOT / resolved["fp16"]
        return {
            component["role"]: out / component["artifact"].replace("{version}", version)
            for component in resolved["components"]
        }

    def build(
        self,
        resolved: dict[str, Any],
        version: str,
        out: Path,
        toolchain: str,
        repo_root: Path,
        gradle_args: list[str] | None = None,
        source_revision: str | None = None,
    ) -> BuildResult:
        """Run the tracked release script; both roles are always built in pinned containers.

        ``toolchain`` and ``gradle_args`` are accepted for parity with the Gradle games and
        change nothing: the host has no .NET SDK, and determinism here comes from
        ``SOURCE_DATE_EPOCH`` plus a clean stage rather than from a task graph.
        """
        del toolchain, gradle_args
        dist = repo_root / DIST_ROOT / resolved["fp16"]
        dist.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ)
        environment["TAKARO_MAINT_REPO_ROOT"] = str(repo_root)
        if source_revision:
            environment["TAKARO_SOURCE_REVISION"] = source_revision
        if "SOURCE_DATE_EPOCH" not in environment:
            stamp = subprocess.run(
                ["git", "-C", str(repo_root), "log", "-1", "--format=%ct"],
                capture_output=True,
                text=True,
                check=False,
            )
            if stamp.returncode == 0 and stamp.stdout.strip().isdigit():
                environment["SOURCE_DATE_EPOCH"] = stamp.stdout.strip()
        command = ["bash", str(repo_root / BUILD_SCRIPT), version, str(dist), "--target", str(resolved["id"])]
        output.info(f"building {resolved['id']} {version} (pinned .NET SDK and Node containers)")
        completed = subprocess.run(
            command, cwd=str(repo_root), capture_output=True, text=True, env=environment, check=False
        )
        log = completed.stdout + completed.stderr
        if completed.returncode != 0:
            output.error(log[-8000:])
            raise BuildFailed(f"{BUILD_SCRIPT} exited {completed.returncode}", target=str(resolved["id"]))
        del out
        return BuildResult(artifacts=self.artifact_paths(resolved, version, repo_root), log=log)

    # -- runtime --------------------------------------------------------------
    def runtime_env(self, resolved: dict[str, Any], takaro: dict[str, str]) -> dict[str, str]:
        """TShock reads nothing about Takaro from the environment; the bridge reads its file."""
        del takaro
        return {
            **{str(k): str(v) for k, v in resolved["runtime"]["container"].get("env", {}).items()},
            "HOME": "/tmp",
            "DOTNET_CLI_HOME": "/tmp",
        }

    def container_options(self, resolved: dict[str, Any]) -> list[str]:
        """The image runs as root; without this it leaves root-owned files in the data dir."""
        del resolved
        return ["--user", f"{os.getuid()}:{os.getgid()}"]

    def container_command(self, resolved: dict[str, Any], data_dir: Path) -> list[str]:
        """The world, on the command line, because TShock reads no environment variable for it.

        The image's entrypoint already carries ``-configpath``, ``-worldselectpath`` and
        ``-additionalplugins``; without a world the server stops on its interactive
        world-selection menu and never finishes booting.
        """
        del resolved, data_dir
        return [
            "-world",
            f"/worlds/{VERIFY_WORLD}.wld",
            "-worldname",
            VERIFY_WORLD,
            "-autocreate",
            "1",
            "-difficulty",
            "0",
            "-maxplayers",
            "8",
            "-port",
            "7777",
        ]

    def container_mounts(self, resolved: dict[str, Any], data_dir: Path) -> list[str]:
        """The three volumes the image declares. ``bridge/`` is the sidecar's, not the server's."""
        del resolved
        for relative in ("tshock/logs", "worlds", "plugins", "bridge"):
            (data_dir / relative).mkdir(parents=True, exist_ok=True)
        return [
            f"{data_dir / 'tshock'}:/tshock",
            f"{data_dir / 'worlds'}:/worlds",
            f"{data_dir / 'plugins'}:/plugins",
        ]

    # -- deploy ---------------------------------------------------------------
    def after_deploy(self, dest: Path, component: dict[str, Any], artifact: Path) -> None:
        """Unpack what the server loads, and never the operator's own configuration."""
        install_dir = dest / paths.safe_relative(component["installDir"], field="components[].installDir")
        if component["role"] == "plugin":
            self._deploy_plugin(install_dir, artifact)
        elif component["role"] == "bridge":
            self._deploy_bridge(install_dir, artifact)

    def _entries(self, artifact: Path, folder: str) -> list[str]:
        """Every zip entry, checked to be inside ``folder`` before anything is extracted."""
        with zipfile.ZipFile(artifact) as archive:
            names = archive.namelist()
        entries: list[str] = []
        for name in names:
            relative = name.rstrip("/")
            if not relative:
                continue
            if not relative.startswith(f"{folder}/"):
                raise ConflictError(
                    f"{artifact.name} holds '{name}', outside the single {folder}/ folder; nothing was extracted"
                )
            paths.safe_relative(relative, field="artifact zip entry")
            entries.append(relative)
        return entries

    def _deploy_plugin(self, install_dir: Path, artifact: Path) -> None:
        """TShock loads ``plugins/<name>.dll`` from the top level, so the one DLL lands there."""
        self._entries(artifact, PLUGIN_FOLDER)
        target = install_dir / PLUGIN_DLL
        staged = install_dir / (PLUGIN_DLL + ".tmp")
        with zipfile.ZipFile(artifact) as archive, archive.open(f"{PLUGIN_FOLDER}/{PLUGIN_DLL}") as source:
            staged.write_bytes(source.read())
        os.replace(staged, target)
        for stale in sorted(install_dir.glob("takaro-terraria-plugin-*.zip")):
            if stale.name != artifact.name:
                stale.unlink()
        output.info(f"unpacked {PLUGIN_DLL} into {install_dir.name}/")

    def _deploy_bridge(self, install_dir: Path, artifact: Path) -> None:
        """The bridge folder is replaced wholesale; ``bridge/TakaroConfig.txt`` is never touched."""
        self._entries(artifact, BRIDGE_FOLDER)
        folder = install_dir / BRIDGE_FOLDER
        shutil.rmtree(folder, ignore_errors=True)
        with zipfile.ZipFile(artifact) as archive:
            archive.extractall(install_dir)
        for stale in sorted(install_dir.glob("takaro-terraria-bridge-*.zip")):
            if stale.name != artifact.name:
                stale.unlink()
        output.info(f"unpacked {BRIDGE_FOLDER}/ into {install_dir.name}/")


GAME = TerrariaAdapter()
