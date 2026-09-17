"""Where things live. Never derived from the caller's working directory."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

from .exit_codes import UsageError

_REPO_ROOT: Path | None = None

# One path segment a record may name: no empty segment, no "..", no leading dot or dash,
# no drive letter, no backslash. Anchored with fullmatch, so a trailing newline is out too.
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")


def safe_relative(value: str, *, field: str) -> PurePosixPath:
    """A record-supplied install path, checked before anything joins it to a directory.

    ``Path("/dest") / "/etc/passwd"`` is ``/etc/passwd`` and ``"../.."`` climbs out, so a
    catalog record or a build manifest could otherwise read and overwrite files outside
    ``--dest``. Every join under ``install/`` and ``deploy`` goes through here first.
    """
    text = str(value)
    segments = text.split("/")
    if not text or not all(_SEGMENT.fullmatch(segment) for segment in segments):
        raise UsageError(
            f"{field} must be a relative path inside the install directory "
            f"(no leading '/', no '..', no backslash), not {value!r}"
        )
    return PurePosixPath(text)


def default_repo_root() -> Path:
    """The repository this package was installed from: ``maintenance/src/takaro_maint`` → repo root."""
    return Path(__file__).resolve().parents[3]


def set_repo_root(root: str | os.PathLike[str] | None) -> None:
    global _REPO_ROOT
    _REPO_ROOT = Path(root).resolve() if root is not None else None


def repo_root() -> Path:
    return _REPO_ROOT if _REPO_ROOT is not None else default_repo_root()


def catalog_root() -> Path:
    return repo_root() / "catalog"


def schema_dir() -> Path:
    return catalog_root() / "schema" / "v1"


def cache_dir() -> Path:
    override = os.environ.get("TAKARO_MAINT_CACHE")
    if override:
        return Path(override).expanduser().resolve()
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")).expanduser() / "takaro-maint"
