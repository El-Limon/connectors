"""The build manifest: the row every later command matches an artifact against."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any

from .. import net
from ..catalog import schema
from ..exit_codes import ConflictError


def source_revision(repo_root: Path, watched: list[str]) -> tuple[str, bool]:
    """``git rev-parse HEAD`` plus whether any watched path is dirty."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown", True
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", *watched],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return head, bool(status)


def watched_paths(game_id: str) -> list[str]:
    """The paths whose uncommitted changes make a build or a verification of ``game_id`` dirty."""
    return [f"games/{game_id}", f"catalog/{game_id}"]


def artifact_row(role: str, target_id: str, fingerprint: str, file: Path) -> dict[str, Any]:
    digests = net.hash_file(file)
    return {
        "role": role,
        "target": target_id,
        "fingerprint": fingerprint,
        "file": file.name,
        "sha256": digests["sha256"],
        "size": int(digests["size"]),
    }


def write_manifest(
    out: Path,
    *,
    connector: str,
    version: str,
    revision: str,
    dirty: bool,
    toolchain: dict[str, Any],
    mode: str,
    artifacts: list[dict[str, Any]],
) -> Path:
    manifest = {
        "schemaVersion": 1,
        "connector": connector,
        "version": version,
        "sourceRevision": revision,
        "dirty": dirty,
        "builtAt": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "toolchain": {
            "image": toolchain["image"],
            "tag": toolchain["tag"],
            "digest": toolchain["digest"],
            "mode": mode,
        },
        "artifacts": artifacts,
    }
    errors = schema.errors_for("build-manifest.schema.json", manifest)
    if errors:
        raise ConflictError("refusing to write an invalid build manifest: " + "; ".join(errors))
    path = out / "build-manifest.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def write_meta(out: Path, row: dict[str, Any], *, connector: str, version: str, revision: str) -> Path:
    path = out / (row["file"] + ".meta.json")
    payload = {**row, "connector": connector, "version": version, "sourceRevision": revision}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConflictError(f"{path}: not a readable build manifest ({exc})") from exc
    errors = schema.errors_for("build-manifest.schema.json", manifest)
    if errors:
        raise ConflictError(f"{path} is not a valid build manifest: " + "; ".join(errors))
    return manifest  # type: ignore[no-any-return]
