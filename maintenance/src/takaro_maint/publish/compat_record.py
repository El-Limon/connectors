"""The compatibility record: what one release contains, and what it was built from.

One JSON document per release, shipped as an asset beside the artifacts. It answers the
questions a consumer asks after the fact — which target is this jar for, which game version
and loader does it pin, which source commit produced it, and was it actually verified — without
needing the repository, the workflow run or the catalog at hand.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import urllib.parse
from pathlib import Path
from typing import Any

from .. import __version__
from .. import fingerprint as fp
from ..catalog import schema
from ..exit_codes import ConflictError

SCHEMA = "compat-record.schema.json"

#: Keys an input entry may carry beyond ``kind`` and ``url``, in the order they are written.
_INPUT_KEYS = ("sha256", "sha1", "size", "version")


def generated_at(commit_epoch: int | None) -> str:
    """The record's timestamp, taken from the release's inputs rather than from the clock.

    Two assemblies of the same commit, the same build outputs and the same catalog have to
    produce the same record bytes — otherwise ``SHA256SUMS`` differs, and a stable retry that
    should be a no-op conflicts with the assets the interrupted run already uploaded. So the
    stamp follows ``SOURCE_DATE_EPOCH`` when the caller sets one and the source commit's own
    commit time otherwise, exactly like ``scripts/lib/package.sh`` does for archives. A
    checkout that can answer neither falls back to the current time and is, by construction,
    not reproducible.
    """
    for candidate in (os.environ.get("SOURCE_DATE_EPOCH"), commit_epoch):
        if candidate is None:
            continue
        try:
            epoch = int(str(candidate).strip())
        except ValueError:
            continue
        if epoch >= 0:
            return dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat().replace("+00:00", "Z")
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def record_name(connector: str, version: str) -> str:
    return f"takaro-{connector}-{version}.compat.json"


def report_name(connector: str, version: str, target_id: str) -> str:
    return f"takaro-{connector}-{version}.verify-{target_id}.json"


def download_url(repo: str, tag: str, name: str) -> str:
    """The public download link for one asset. Valid for a draft only once it is published."""
    quoted = urllib.parse.quote(name, safe="._+-")
    return f"https://github.com/{repo}/releases/download/{urllib.parse.quote(tag, safe='._+-')}/{quoted}"


def catalog_sha256(game_record: dict[str, Any], target_records: dict[str, dict[str, Any]]) -> str:
    """One hash over the game record and every non-retired target record.

    Canonicalised the same way a target fingerprint is, so the value a release records can be
    recomputed from a checkout without knowing how the catalog files happened to be formatted.
    """
    canonical = fp.canonical({"game": game_record, "targets": target_records})
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def input_entries(record: dict[str, Any], resolved_urls: dict[str, str]) -> dict[str, dict[str, Any]]:
    """The pinned inputs of one target, flattened to ``name -> {kind, url, hashes}``.

    A ``mojang-version`` input is two downloads (the version manifest and the server jar), so it
    becomes two entries; everything else is one. The keys match ``targets resolve``'s
    ``resolvedUrls`` exactly, which is what makes the record checkable against the catalog.
    """
    entries: dict[str, dict[str, Any]] = {}
    for name, spec in record.get("inputs", {}).items():
        kind = str(spec["kind"])
        if kind == "mojang-version":
            version = spec.get("version")
            for part in ("manifest", "server"):
                key = f"{name}.{part}"
                entries[key] = _entry(kind, resolved_urls.get(key), {**spec[part], "version": version})
        else:
            entries[name] = _entry(kind, resolved_urls.get(name), spec)
    return entries


def _entry(kind: str, url: str | None, spec: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {"kind": kind, "url": url}
    for key in _INPUT_KEYS:
        value = spec.get(key)
        if value is not None:
            entry[key] = value
    return entry


def build(
    *,
    connector: str,
    version: str,
    channel: str,
    tag: str,
    mode: str,
    repo: str,
    source_commit: str,
    source_tag: str | None,
    dirty: bool,
    stamp: str,
    catalog: dict[str, Any] | None,
    catalog_hash: str | None,
    targets: dict[str, dict[str, Any]],
    aliases: dict[str, dict[str, Any]],
    assets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the record. ``assets`` is every asset but ``SHA256SUMS`` and the record itself."""
    return {
        "schemaVersion": 1,
        "kind": "compat-record",
        "connector": connector,
        "version": version,
        "channel": channel,
        "tag": tag,
        "mode": mode,
        "generatedAt": stamp,
        "tool": {"name": "takaro-maint", "version": __version__},
        "source": {
            "repo": repo,
            "commit": source_commit,
            "tag": source_tag,
            "dirty": dirty,
            "catalogSha256": catalog_hash,
        },
        "catalog": catalog,
        "targets": targets,
        "aliases": aliases,
        "assets": assets,
        "checksums": "SHA256SUMS",
        "self": record_name(connector, version),
    }


def validate(record: dict[str, Any]) -> None:
    errors = schema.errors_for(SCHEMA, record)
    if errors:
        raise ConflictError(
            "the compatibility record does not validate against its schema: " + "; ".join(errors),
            schemaErrors=errors,
        )


def write(out: Path, record: dict[str, Any]) -> Path:
    """Validate first, then write. An invalid record is never published."""
    validate(record)
    path = out / str(record["self"])
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def read(path: Path) -> dict[str, Any]:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConflictError(f"{path.name}: not a readable compatibility record ({exc})") from exc
    validate(record)
    return record


def loads(name: str, payload: bytes) -> dict[str, Any]:
    """Parse and validate a record that came off the wire rather than off disk."""
    try:
        record = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConflictError(f"{name}: not a readable compatibility record ({exc})") from exc
    validate(record)
    return record
