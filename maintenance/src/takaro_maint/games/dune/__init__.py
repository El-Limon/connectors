"""The Dune: Awakening adapter: a battlegroup of containers, a sidecar and an optional plugin.

Three things make this game unlike the others here.

The "server" is not a directory of game files. Steam tool app 4754530 serves a depot that
holds Funcom's own container images as tarballs plus their k3s setup scripts, so an install
is a download followed by ``docker load`` -- a step the shared Steam install path has no
seam for yet. Until it does, this adapter deliberately defines no ``install``: the rig
reads its pins out of the same catalog record and performs that download itself, and the
target declares no ``devServers`` block so nothing claims a ledger that was never written.

The connector is the sidecar, not the plugin. The sidecar reaches the battlegroup's
RabbitMQ and Postgres and carries almost the whole capability matrix on its own; the
LD_PRELOAD plugin adds kill attribution, live position and precise connect/disconnect, and
is shipped as a second, optional artifact.

And the plugin is pinned to the map binary. The server ships no function symbols, so the
plugin resolves the game's code through RTTI and vtables and re-validates every address at
load; on a build other than the pinned one it reports the affected capability as degraded
rather than guessing. That is why the target's revision is the thing that matters here, and
why one pinned image is used for both halves of the build: node:*-bookworm (not slim)
carries the g++ the plugin needs, on the same glibc as the Funcom server image.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

from ... import output, paths
from ...exit_codes import BuildFailed, ConflictError
from ..base import BuildResult

GAME_ID = "dune"
DIST_ROOT = "games/dune/_data/dist"
BUILD_SCRIPT = "games/dune/scripts/build-release.sh"

#: The single top-level folder each role's archive may write, and the folder the operator
#: ends up using. An archive that holds anything else is refused rather than extracted.
ROLE_FOLDER = {"sidecar": "TakaroDuneSidecar", "plugin": "TakaroDune"}

#: Operator state that lives inside the sidecar folder and never inside the archive. The
#: folder is replaced wholesale on an upgrade so a removed file cannot survive it, which
#: would otherwise take the operator's own configuration with it.
DEPLOY_PRESERVED = {"sidecar": (".env",), "plugin": ()}

#: The file inside the depot that identifies the image bundle, and the tag it names.
IMAGE_VERSION_FILE = "images/battlegroup/version.txt"
SERVER_IMAGE_BUNDLE = "images/battlegroup/server.tar"

# What the map server prints about itself while it comes up:
#   "LogInit: Build: ++Dune+Release-2118731"
#   "LogInit: Engine Version: 5.2.1-2118731+++Dune+Release"
# Neither banner carries the other's number, so both are parsed and the verify runner
# merges them -- the same split every Unreal game here has.
_BUILD_BANNER = re.compile(r"LogInit: Build: \+\+(?P<build>Dune\+Release-\d+)")
_ENGINE_BANNER = re.compile(r"LogInit: Engine Version: (?P<engine>\d+\.\d+\.\d+)-\d+\+\+\+Dune\+Release")


def _env_key(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


class DuneAdapter:
    id = GAME_ID

    # -- description ----------------------------------------------------------
    def env(self, resolved: dict[str, Any], prefix: str) -> dict[str, str]:
        """What the rig, the build script and CI read. No ``_JAVA``: nothing here is a JVM."""
        server = resolved["inputs"]["server"]
        depots = ";".join(f"{depot}:{server['depots'][depot]['manifest']}" for depot in sorted(server["depots"]))
        env = {
            f"{prefix}_TARGET": str(resolved["id"]),
            f"{prefix}_FINGERPRINT": str(resolved["fingerprint"]),
            f"{prefix}_FP16": str(resolved["fp16"]),
            f"{prefix}_IMAGE": str(resolved["containerRef"]),
            f"{prefix}_TOOLCHAIN": str(resolved["toolchainRef"]),
            f"{prefix}_REVISION": str(resolved["revision"]),
            f"{prefix}_STEAM_APP": str(server["app"]),
            f"{prefix}_STEAM_BRANCH": str(server["branch"]),
            f"{prefix}_STEAM_BUILDID": str(server["buildid"]),
            f"{prefix}_STEAM_DEPOTS": depots,
        }
        # The image tag is the identity of what `docker load` puts on the host, and both the
        # rig's compose file and the operator's own need it. It is the content of a pinned
        # file in the depot, so it is read from the record's notes-free facts: the bundle's
        # declared sha256 proves the file, the tag itself is the target's image tag.
        env[f"{prefix}_IMAGE_BUNDLE_SHA256"] = str(server["files"][SERVER_IMAGE_BUNDLE]["sha256"])
        env[f"{prefix}_IMAGE_VERSION_SHA256"] = str(server["files"][IMAGE_VERSION_FILE]["sha256"])
        # One key per role, so no script spells an artifact name out itself.
        for role, name in sorted(resolved["artifactFileNames"].items()):
            env[f"{prefix}_ARTIFACT_{_env_key(role)}"] = str(name)
        for component in resolved["components"]:
            env[f"{prefix}_INSTALL_DIR_{_env_key(str(component['role']))}"] = str(component["installDir"])
        # The dependency URLs and hashes the build checks the lockfile against.
        for name, dep in sorted(resolved["build"]["deps"].items()):
            key = _env_key(name)
            env[f"{prefix}_DEP_{key}_URL"] = str(dep.get("resolvedCoordinate", dep["coordinate"]))
            env[f"{prefix}_DEP_{key}_SHA256"] = str(dep["sha256"])
        return env

    def preserve_globs(self, resolved: dict[str, Any]) -> list[str]:
        return list(resolved.get("preserve", []))

    def parse_runtime_identity(self, log_line: str) -> dict[str, Any] | None:
        """The build or the engine version, from whichever banner this line is."""
        build = _BUILD_BANNER.search(log_line)
        if build:
            return {"gameVersion": build.group("build"), "loader": "unreal", "loaderVersion": None}
        engine = _ENGINE_BANNER.search(log_line)
        if engine:
            return {"gameVersion": None, "loader": "unreal", "loaderVersion": engine.group("engine")}
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
        """Run the tracked release script; it always builds inside the pinned image.

        ``toolchain`` is accepted for parity with the Gradle games and changes nothing: the
        host is not assumed to have Node or a C++ toolchain, so ``host`` would be a promise
        this adapter cannot keep. ``gradle_args`` mean nothing to a script build;
        determinism comes from ``SOURCE_DATE_EPOCH`` and a clean stage.
        """
        del toolchain, gradle_args
        dist = repo_root / DIST_ROOT / resolved["fp16"]
        dist.mkdir(parents=True, exist_ok=True)
        # A half-finished build must not be able to report over yesterday's bytes.
        for artifact in self.artifact_paths(resolved, version, repo_root).values():
            artifact.unlink(missing_ok=True)
            artifact.with_suffix(artifact.suffix + ".meta.json").unlink(missing_ok=True)
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
        output.info(f"building {resolved['id']} {version} (node container toolchain)")
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
        """The pinned image's own environment, and nothing of Takaro's.

        The game containers never receive a Takaro value: everything Takaro talks to lives
        in the sidecar. A runtime verification for this target is not attempted on a hosted
        runner at all -- see the target record -- so this is the honest minimum rather than
        a boot recipe.
        """
        del takaro
        return {str(k): str(v) for k, v in resolved["runtime"]["container"].get("env", {}).items()}

    # -- deploy ---------------------------------------------------------------
    def after_deploy(self, dest: Path, component: dict[str, Any], artifact: Path) -> None:
        """Unpack one role's archive into its install dir, replacing the folder atomically."""
        role = str(component["role"])
        folder_name = ROLE_FOLDER.get(role)
        if folder_name is None:
            raise ConflictError(f"no folder is defined for the '{role}' role; nothing was extracted")
        install_dir = dest / paths.safe_relative(component["installDir"], field="components[].installDir")
        install_dir.mkdir(parents=True, exist_ok=True)
        folder = install_dir / folder_name

        with tarfile.open(artifact, "r:gz") as archive:
            members = archive.getmembers()
            for member in members:
                relative = member.name.rstrip("/")
                if not relative or relative == ".":
                    continue
                if relative != folder_name and not relative.startswith(f"{folder_name}/"):
                    raise ConflictError(
                        f"{artifact.name} holds '{member.name}', outside the single {folder_name}/ "
                        "folder; nothing was extracted"
                    )
                paths.safe_relative(relative, field="artifact archive entry")
                if member.issym() or member.islnk():
                    raise ConflictError(f"{artifact.name} holds a link entry '{member.name}'; nothing was extracted")
            preserved = []
            for name in DEPLOY_PRESERVED.get(role, ()):
                kept = folder / name
                if kept.is_file():
                    preserved.append((name, kept.read_bytes(), kept.stat().st_mode & 0o777))
            shutil.rmtree(folder, ignore_errors=True)
            # Every entry was checked above; `filter="data"` is the second lock, not the first.
            archive.extractall(install_dir, members=members, filter="data")

        for name, body, mode in preserved:
            restored = folder / name
            if restored.exists():
                continue
            restored.parent.mkdir(parents=True, exist_ok=True)
            restored.write_bytes(body)
            os.chmod(restored, mode)
            output.info(f"kept the existing {folder_name}/{name}")

        # An older artifact for the same role must not stay behind and look installed.
        stem = artifact.name.split("-linux-")[0]
        for stale in sorted(install_dir.glob(f"{stem}-*.tar.gz")):
            if stale.name != artifact.name:
                stale.unlink()

        stamp = folder / "takaro-target.json"
        if stamp.is_file():
            try:
                data = json.loads(stamp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
            version = data.get("connectorVersion")
            if version:
                output.info(f"unpacked {folder_name} {version} into {component['installDir']}/")


GAME = DuneAdapter()
