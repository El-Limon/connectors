"""Stage everything first, then swap file by file. A failed install changes nothing."""

from __future__ import annotations

import fnmatch
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .. import output, paths


@dataclass
class _Move:
    final: Path
    backup: Path
    retired: bool = False
    placed: bool = False


def is_protected(relative: str, preserve: list[str]) -> bool:
    """True when ``relative`` (a path under the install dir) must never be replaced."""
    normalised = relative.replace(os.sep, "/")
    for pattern in preserve:
        clean = pattern.rstrip("/")
        if pattern.endswith("/"):
            # A directory glob protects the directory and everything under it.
            if fnmatch.fnmatch(normalised, clean) or fnmatch.fnmatch(normalised, clean + "/*"):
                return True
            head = normalised.split("/", 1)[0]
            if fnmatch.fnmatch(head, clean):
                return True
        elif fnmatch.fnmatch(normalised, pattern):
            return True
    return False


@dataclass
class StagedInstall:
    """A staging directory under ``<dest>/.takaro/staging/<fp16>`` and the swap it performs."""

    dest: Path
    fp16: str
    preserve: list[str] = field(default_factory=list)
    _staged: dict[str, Path] = field(default_factory=dict, init=False)

    @property
    def root(self) -> Path:
        return self.dest / ".takaro" / "staging" / self.fp16

    def __enter__(self) -> StagedInstall:
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        # Nothing staged may survive a run, successful or not: a leftover tree would look
        # like a half-finished install to the next person who opens the directory.
        shutil.rmtree(self.root, ignore_errors=True)
        parent = self.root.parent
        try:
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass

    def path_for(self, install_path: str) -> Path:
        relative = paths.safe_relative(install_path, field="installPath")
        staged = self.root / relative
        staged.parent.mkdir(parents=True, exist_ok=True)
        self._staged[install_path] = staged
        return staged

    def commit(self) -> list[str]:
        """Move every staged file into place, rolling every prior move back on failure."""
        placed: list[str] = []
        rollback = self.root / ".rollback"
        moves: list[_Move] = []
        try:
            for install_path, staged in sorted(self._staged.items()):
                if is_protected(install_path, self.preserve):
                    output.info(f"keeping protected {install_path}")
                    continue
                # Checked again at the write: this is the line that puts bytes into the
                # caller's directory, and an unchecked record value would escape it.
                relative = paths.safe_relative(install_path, field="installPath")
                final = self.dest / relative
                backup = rollback / relative
                final.parent.mkdir(parents=True, exist_ok=True)
                state = _Move(final=final, backup=backup)
                moves.append(state)
                if final.exists() or final.is_symlink():
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(final, backup)
                    state.retired = True
                os.replace(staged, final)
                state.placed = True
                placed.append(install_path)
        except BaseException:
            for state in reversed(moves):
                if state.placed and (state.final.exists() or state.final.is_symlink()):
                    state.final.unlink()
                if state.retired and (state.backup.exists() or state.backup.is_symlink()):
                    state.final.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(state.backup, state.final)
            raise
        shutil.rmtree(rollback, ignore_errors=True)
        return placed
