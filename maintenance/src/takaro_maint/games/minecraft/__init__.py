"""The Minecraft game adapter: one Gradle build, one target project per catalog target."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from ... import output
from ...exit_codes import BuildFailed, ConflictError
from ..base import BuildResult, common_env
from . import fabric

_PLATFORMS = {"fabric": fabric}


def _platform(resolved: dict[str, Any]) -> Any:
    platform = resolved["platform"]
    if platform not in _PLATFORMS:
        raise ConflictError(
            f"the Minecraft adapter has no support for platform '{platform}' yet "
            f"(known: {', '.join(sorted(_PLATFORMS))})"
        )
    return _PLATFORMS[platform]


class MinecraftAdapter:
    id = "minecraft"

    def env(self, resolved: dict[str, Any], prefix: str) -> dict[str, str]:
        return {**common_env(resolved, prefix), **_platform(resolved).env(resolved, prefix)}

    def artifact_paths(self, resolved: dict[str, Any], version: str, repo_root: Path) -> dict[str, Path]:
        """The exact file each component role must have produced — never a glob."""
        project = resolved["build"]["gradleProject"]
        libs = repo_root / "games" / "minecraft" / "mod" / "targets" / project / "build" / "libs"
        return {
            component["role"]: libs / component["artifact"].replace("{version}", version)
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
        project = resolved["build"]["gradleProject"]
        mod_dir = repo_root / "games" / "minecraft" / "mod"
        gradle = shlex.split(os.environ.get("TAKARO_MAINT_GRADLE", "./gradlew"))
        task = [
            *gradle,
            f":{project}:build",
            f"-Pversion={version}",
            # A container toolchain has no git and no worktree metadata, so the revision is
            # handed to Gradle rather than guessed at inside the build.
            f"-PtakaroSourceRevision={source_revision or 'unknown'}",
            "--no-daemon",
            "-Dorg.gradle.workers.max=2",
            *(gradle_args or []),
        ]
        if toolchain == "container":
            cache = Path(os.environ.get("TAKARO_MAINT_CACHE", Path.home() / ".cache" / "takaro-maint"))
            (cache / "gradle").mkdir(parents=True, exist_ok=True)
            (cache / "home").mkdir(parents=True, exist_ok=True)
            docker = shlex.split(os.environ.get("TAKARO_MAINT_DOCKER", "docker"))
            command = [
                *docker,
                "run",
                "--rm",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "-v",
                f"{repo_root}:{repo_root}",
                "-v",
                f"{cache}:{cache}",
                "-w",
                str(mod_dir),
                "-e",
                f"GRADLE_USER_HOME={cache / 'gradle'}",
                "-e",
                f"HOME={cache / 'home'}",
                resolved["toolchainRef"],
                *task,
            ]
        else:
            command = task
        output.info(f"building {project} {version} ({toolchain} toolchain)")
        output.debug("exec " + " ".join(shlex.quote(part) for part in command))
        completed = subprocess.run(command, cwd=mod_dir, capture_output=True, text=True, check=False)
        log = completed.stdout + completed.stderr
        if completed.returncode != 0:
            output.error(log[-8000:])
            raise BuildFailed(f"gradle :{project}:build exited {completed.returncode}", project=project)
        return BuildResult(artifacts=self.artifact_paths(resolved, version, repo_root), log=log)

    def runtime_env(self, resolved: dict[str, Any], takaro: dict[str, str]) -> dict[str, str]:
        return {**_platform(resolved).runtime_env(resolved), **takaro}

    def parse_runtime_identity(self, log_line: str) -> dict[str, Any] | None:
        for module in _PLATFORMS.values():
            parsed = module.parse_runtime_identity(log_line)
            if parsed is not None:
                return parsed  # type: ignore[no-any-return]
        return None

    def preserve_globs(self, resolved: dict[str, Any]) -> list[str]:
        return list(resolved.get("preserve", []))


GAME = MinecraftAdapter()
