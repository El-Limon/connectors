"""The verification report: the evidence a run produced, validated against its schema."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any

from .. import __version__, net
from ..catalog import ids, schema
from ..exit_codes import VerificationFailed

LEVELS = ("build", "contract", "startup", "protocol")
LEVEL_CHECKS: dict[str, tuple[str, ...]] = {
    "build": ("build",),
    "contract": ("build",),
    "startup": ("startup",),
    "protocol": (
        "connector-load",
        "identify",
        "heartbeat",
        "players",
        "catalog-items",
        "catalog-entities",
        "console",
        "shutdown",
    ),
}


def level_for(checks: list[dict[str, Any]]) -> str:
    """The highest class whose checks all passed; never ``gameplay``."""
    status = {check["id"]: check["status"] for check in checks}
    reached = "build"
    for level in LEVELS:
        required = LEVEL_CHECKS[level]
        if required and all(status.get(check_id) == "pass" for check_id in required):
            reached = level
        else:
            break
    return reached


def repo_identity(repo_root: Path) -> tuple[str, str, bool]:
    """``owner/repo`` from the origin remote, plus HEAD and whether the tree is dirty."""

    def git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], cwd=repo_root, capture_output=True, text=True, check=True
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return ""

    remote = git("remote", "get-url", "origin")
    repo = "unknown"
    if remote:
        cleaned = remote.removesuffix(".git")
        if ":" in cleaned and "//" not in cleaned:
            repo = cleaned.split(":", 1)[1]
        else:
            repo = "/".join(cleaned.rstrip("/").split("/")[-2:])
    return repo, git("rev-parse", "HEAD") or "unknown", bool(git("status", "--porcelain"))


def build_report(
    *,
    target: Any,
    game_record: dict[str, Any],
    manifest: dict[str, Any],
    artifacts_dir: Path,
    runtime: dict[str, Any],
    checks: list[dict[str, Any]],
    started_at: str,
    logs: list[Path],
    repo_root: Path,
    takaro: str = "local",
) -> dict[str, Any]:
    repo, revision, dirty = repo_identity(repo_root)
    inputs: dict[str, Any] = {}
    for name, spec in target.record["inputs"].items():
        if spec["kind"] == "mojang-version":
            inputs[name] = {
                "url": ids.resolved_url(game_record, spec["server"]["source"], spec["server"]["path"]),
                "sha1": spec["server"]["sha1"],
                "size": int(spec["server"]["size"]),
            }
        else:
            inputs[name] = {
                "url": ids.resolved_url(game_record, spec["source"], spec["path"]),
                "sha256": spec["sha256"],
            }
    rows = [row for row in manifest["artifacts"] if row["target"] == target.id]
    container = target.record["runtime"]["container"]
    report = {
        "schemaVersion": 1,
        "kind": "runtime-verification",
        "tool": {"name": "takaro-maint", "version": __version__},
        "source": {"repo": repo, "revision": revision, "dirty": dirty},
        "target": {
            "game": target.game,
            "id": target.id,
            "fingerprint": target.fingerprint,
            "inputs": inputs,
        },
        "artifacts": [
            {
                "role": row["role"],
                "file": row["file"],
                "sha256": row["sha256"],
                "connectorVersion": manifest["version"],
            }
            for row in rows
        ],
        "runtime": {
            "image": {"ref": ids.container_ref(container), "digest": container["digest"]},
            "gameVersion": runtime.get("gameVersion"),
            "loader": runtime.get("loader"),
            "loaderVersion": runtime.get("loaderVersion"),
            "java": runtime.get("java"),
        },
        "takaro": takaro,
        "level": level_for(checks),
        "checks": checks,
        "outcome": "pass" if all(c["status"] != "fail" for c in checks) else "fail",
        "startedAt": started_at,
        "finishedAt": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "logs": [{"name": log.name, "sha256": net.sha256_file(log)} for log in logs if log.is_file()],
        "coverage": {"gameplay": "not covered - recorded client evidence pending (#174)"},
    }
    del artifacts_dir
    return report


def write_report(out: Path, report: dict[str, Any]) -> Path:
    errors = schema.errors_for("verify-report.schema.json", report)
    if errors:
        raise VerificationFailed("the verification report does not validate: " + "; ".join(errors))
    out.mkdir(parents=True, exist_ok=True)
    path = out / "report.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
