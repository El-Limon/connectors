"""Where things live. Never derived from the caller's working directory."""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT: Path | None = None


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
