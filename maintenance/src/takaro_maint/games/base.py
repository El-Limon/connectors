"""What a game must be able to do for the maintenance command."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass
class BuildResult:
    """What a game's build produced for one target."""

    artifacts: dict[str, Path] = field(default_factory=dict)
    log: str = ""


@runtime_checkable
class GameAdapter(Protocol):
    """The per-game behaviour every command dispatches through."""

    id: str

    def env(self, resolved: dict[str, Any], prefix: str) -> dict[str, str]:
        """Deployment environment keys for this resolution (rig and CI consume these)."""
        ...

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
        """Run the game's build system for one target."""
        ...

    def artifact_paths(self, resolved: dict[str, Any], version: str, repo_root: Path) -> dict[str, Path]:
        """role -> the exact file the build must have produced. Never a glob."""
        ...

    def runtime_env(self, resolved: dict[str, Any], takaro: dict[str, str]) -> dict[str, str]:
        """Container environment for a verification run."""
        ...

    def parse_runtime_identity(self, log_line: str) -> dict[str, Any] | None:
        """Pull the runtime identity out of a server log line, when it carries one."""
        ...

    def preserve_globs(self, resolved: dict[str, Any]) -> list[str]:
        """Paths under the install dir that an install must never replace."""
        ...


def common_env(resolved: dict[str, Any], prefix: str) -> dict[str, str]:
    """The keys every game exposes, before its own additions."""
    return {
        f"{prefix}_TARGET": str(resolved["id"]),
        f"{prefix}_FINGERPRINT": str(resolved["fingerprint"]),
        f"{prefix}_FP16": str(resolved["fp16"]),
        f"{prefix}_IMAGE": str(resolved["containerRef"]),
        f"{prefix}_JAVA": str(resolved["runtime"]["java"]),
    }
