"""What a game must be able to do for the maintenance command."""

from __future__ import annotations

import os
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..exit_codes import ConflictError


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

    def install(self, catalog: Any, target: Any, resolved: dict[str, Any], args: Any) -> int | None:
        """This game's own install, or ``None`` to let the generic one run."""
        ...

    def after_deploy(self, dest: Path, component: dict[str, Any], artifact: Path) -> None:
        """Anything the artifact needs once it is in place -- unpacking it, say."""
        ...

    def container_mounts(self, resolved: dict[str, Any], data_dir: Path) -> list[str]:
        """The ``-v`` arguments a verification run's server container needs."""
        ...

    def container_options(self, resolved: dict[str, Any], data_dir: Path) -> list[str]:
        """Extra ``docker run`` options, appended after the runner's own."""
        ...

    def container_command(self, resolved: dict[str, Any], data_dir: Path) -> list[str] | None:
        """Arguments after the image reference, or ``None`` for the image's own Cmd."""
        ...


class BaseAdapter:
    """The defaults every adapter inherits, so the call sites can be plain method calls.

    These used to be looked up with ``getattr(adapter, "container_options", None)``, which
    meant a hook a game spelled differently -- or one a refactor renamed -- was silently
    never called: Conan's ``container_options`` was declared and never reached a
    ``docker run``. Subclassing makes every hook a real method with a checked signature.
    """

    def install(self, catalog: Any, target: Any, resolved: dict[str, Any], args: Any) -> int | None:
        """``None``: this game installs through the generic Steam/catalog path."""
        return None

    def after_deploy(self, dest: Path, component: dict[str, Any], artifact: Path) -> None:
        """Nothing: the artifact is loaded as it lands."""
        return None

    def container_mounts(self, resolved: dict[str, Any], data_dir: Path) -> list[str]:
        return [f"{data_dir}:/data"]

    def container_options(self, resolved: dict[str, Any], data_dir: Path) -> list[str]:
        return []

    def container_command(self, resolved: dict[str, Any], data_dir: Path) -> list[str] | None:
        """``None``: the image's own Cmd is what this server starts with."""
        return None


def open_zip(artifact: Path) -> zipfile.ZipFile:
    """``zipfile.ZipFile``, with a non-zip artifact as a deploy conflict rather than a crash.

    The manifest's sha256 says the bytes are the ones that were built; it says nothing
    about them being a zip. A `BadZipFile` escaping an `after_deploy` is a traceback and
    exit 1, which reads as a bug in the tool rather than a bad artifact.
    """
    try:
        return zipfile.ZipFile(artifact)
    except zipfile.BadZipFile as exc:
        raise ConflictError(f"{artifact.name} is not a zip archive; nothing was extracted ({exc})") from exc


def replace_directory(staged: Path, destination: Path, *, subject: str) -> None:
    """Replace one live directory, restoring it if the final rename fails.

    Extraction happens before this function is called.  A well-formed zip can still be
    structurally incomplete, though, so the staged root is checked before the live tree
    moves.  Once it does move, it remains beside the destination until the incoming tree
    is live; any failed second rename puts it back.
    """
    if not staged.is_dir():
        raise ConflictError(f"the artifact unpacked without {subject}; the installed {subject} is untouched")

    previous = destination.with_name(f".{destination.name}.previous")
    if previous.exists() or previous.is_symlink():
        raise ConflictError(
            f"cannot replace {subject}: recovery directory {previous} already exists; "
            "the installed connector is untouched"
        )

    replaced = destination.exists() or destination.is_symlink()
    if replaced:
        os.replace(destination, previous)
    try:
        os.replace(staged, destination)
    except BaseException:
        if replaced:
            try:
                os.replace(previous, destination)
            except OSError as restore_error:
                raise ConflictError(
                    f"could not install or restore {subject}; the previous tree remains at {previous} ({restore_error})"
                ) from restore_error
        raise
    if replaced:
        shutil.rmtree(previous)


def common_env(resolved: dict[str, Any], prefix: str) -> dict[str, str]:
    """The keys every game exposes, before its own additions.

    ``_JAVA`` only exists for a target that names a JVM: ``str(None)`` would have written
    the literal ``JAVA=None`` into the env file the rig sources.
    """
    env = {
        f"{prefix}_TARGET": str(resolved["id"]),
        f"{prefix}_FINGERPRINT": str(resolved["fingerprint"]),
        f"{prefix}_FP16": str(resolved["fp16"]),
        f"{prefix}_IMAGE": str(resolved["containerRef"]),
    }
    java = resolved["runtime"].get("java")
    if java is not None:
        env[f"{prefix}_JAVA"] = str(int(java))
    return env
