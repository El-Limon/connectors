"""``<dest>/.takaro/installed-target.json``: what a game directory currently holds."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import net
from ..catalog import schema
from ..exit_codes import ConflictError

LEDGER_RELATIVE = Path(".takaro") / "installed-target.json"


def ledger_path(dest: Path) -> Path:
    return dest / LEDGER_RELATIVE


@dataclass(frozen=True)
class Ledger:
    data: dict[str, Any]

    @property
    def fingerprint(self) -> str:
        return str(self.data["fingerprint"])

    @property
    def target(self) -> str:
        return str(self.data["target"])

    @property
    def world_revision(self) -> str:
        return str(self.data.get("world", {}).get("revision", ""))


def read_ledger(dest: Path) -> Ledger | None:
    path = ledger_path(dest)
    if not path.is_file():
        return None
    try:
        return Ledger(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise ConflictError(f"{path} is not valid JSON ({exc}); reinstall to rewrite it") from exc


def write_ledger(dest: Path, data: dict[str, Any]) -> Path:
    errors = schema.errors_for("installed-ledger.schema.json", data)
    if errors:
        raise ConflictError("refusing to write an invalid ledger: " + "; ".join(errors))
    path = ledger_path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return path


def check_ledger(dest: Path, target_record: dict[str, Any], fingerprint: str) -> list[str]:
    """Empty when the directory really holds this target; otherwise every reason it does not."""
    reasons: list[str] = []
    ledger = read_ledger(dest)
    if ledger is None:
        return [f"no ledger at {ledger_path(dest)}"]
    if ledger.target != target_record["id"]:
        reasons.append(f"ledger holds target '{ledger.target}', expected '{target_record['id']}'")
    if ledger.fingerprint != fingerprint:
        reasons.append(f"ledger fingerprint {ledger.fingerprint} != catalog fingerprint {fingerprint}")
    for entry in ledger.data.get("inputs", []):
        path = dest / entry["path"]
        if not path.is_file():
            reasons.append(f"missing input file {entry['path']}")
            continue
        digests = net.hash_file(path)
        if entry.get("sha256") and digests["sha256"] != entry["sha256"]:
            reasons.append(f"{entry['path']} sha256 {digests['sha256']} != recorded {entry['sha256']}")
        if entry.get("sha1") and digests["sha1"] != entry["sha1"]:
            reasons.append(f"{entry['path']} sha1 {digests['sha1']} != recorded {entry['sha1']}")
        if digests["size"] != entry["size"]:
            reasons.append(f"{entry['path']} size {digests['size']} != recorded {entry['size']}")
    artifact = ledger.data.get("artifact")
    if artifact:
        path = dest / artifact["path"]
        if not path.is_file():
            reasons.append(f"missing artifact {artifact['path']}")
        elif net.sha256_file(path) != artifact["sha256"]:
            reasons.append(f"{artifact['path']} does not match the recorded artifact sha256")
    return reasons
