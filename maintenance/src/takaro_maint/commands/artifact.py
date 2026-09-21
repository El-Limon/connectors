"""``artifact validate`` — does this file really come from this target?"""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path
from typing import Any

from .. import output
from ..exit_codes import CONFLICT, OK
from . import add_selection_arguments, select_one

MANIFEST_ATTRS = (
    "Takaro-Target",
    "Takaro-Target-Fingerprint",
    "Takaro-Connector-Version",
    "Takaro-Source-Revision",
    "Takaro-Game-Version",
)
TARGET_JSON = "META-INF/takaro-target.json"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("artifact", help="inspect built artifacts")
    inner = parser.add_subparsers(dest="artifact_command", metavar="<subcommand>")
    validate = inner.add_parser("validate", help="check that files carry this target's identity")
    add_selection_arguments(validate)
    validate.add_argument("files", nargs="+", help="artifacts to check")
    validate.set_defaults(handler=_validate, op="artifact validate")
    parser.set_defaults(handler=None, op="artifact")


def read_jar_manifest(path: Path) -> dict[str, str]:
    """Java manifest attributes, continuation lines joined."""
    with zipfile.ZipFile(path) as archive:
        raw = archive.read("META-INF/MANIFEST.MF").decode("utf-8")
    attributes: dict[str, str] = {}
    key: str | None = None
    for line in raw.split("\r\n" if "\r\n" in raw else "\n"):
        if line.startswith(" ") and key:
            attributes[key] += line[1:]
        elif ":" in line:
            key, _, value = line.partition(":")
            attributes[key] = value.strip()
    return attributes


def validate_file(path: Path, record: dict[str, Any], fingerprint: str) -> list[str]:
    """Empty when the file belongs to this target; otherwise every reason it does not."""
    problems: list[str] = []
    if not path.is_file():
        return [f"{path.name}: missing"]
    if path.suffix != ".jar":
        meta_path = path.with_name(path.name + ".meta.json")
        if not meta_path.is_file():
            return [f"{path.name}: no {meta_path.name} beside a non-jar artifact"]
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("target") != record["id"]:
            problems.append(f"{path.name}: meta target {meta.get('target')} != {record['id']}")
        if meta.get("fingerprint") != fingerprint:
            problems.append(f"{path.name}: meta fingerprint {meta.get('fingerprint')} != {fingerprint}")
        return problems

    try:
        attributes = read_jar_manifest(path)
    except (KeyError, zipfile.BadZipFile) as exc:
        return [f"{path.name}: no readable jar manifest ({exc})"]
    for attribute in MANIFEST_ATTRS:
        if not attributes.get(attribute):
            problems.append(f"{path.name}: manifest attribute {attribute} is missing")
    if attributes.get("Takaro-Target") not in (None, record["id"]):
        problems.append(f"{path.name}: Takaro-Target {attributes['Takaro-Target']} != {record['id']}")
    if attributes.get("Takaro-Target-Fingerprint") not in (None, fingerprint):
        problems.append(
            f"{path.name}: Takaro-Target-Fingerprint {attributes['Takaro-Target-Fingerprint']} != {fingerprint}"
        )
    if attributes.get("Takaro-Game-Version") not in (None, record["revision"]):
        problems.append(f"{path.name}: Takaro-Game-Version {attributes['Takaro-Game-Version']} != {record['revision']}")

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if TARGET_JSON not in names:
            problems.append(f"{path.name}: {TARGET_JSON} is missing")
        else:
            try:
                stamped = json.loads(archive.read(TARGET_JSON).decode("utf-8"))
            except json.JSONDecodeError as exc:
                problems.append(f"{path.name}: {TARGET_JSON} is not valid JSON ({exc})")
                stamped = {}
            if stamped.get("target") != record["id"]:
                problems.append(f"{path.name}: {TARGET_JSON} target {stamped.get('target')} != {record['id']}")
            if stamped.get("fingerprint") != fingerprint:
                problems.append(f"{path.name}: {TARGET_JSON} fingerprint disagrees with the catalog")
    return problems


def _validate(args: Any) -> int:
    _, target = select_one(args)
    results = []
    problems: list[str] = []
    for name in args.files:
        path = Path(name).expanduser()
        file_problems = validate_file(path, target.record, target.fingerprint)
        problems += file_problems
        results.append({"file": path.name, "ok": not file_problems, "problems": file_problems})
    for problem in problems:
        output.error(problem)
    output.emit(
        "artifact validate",
        not problems,
        game=target.game,
        target=target.id,
        fingerprint=target.fingerprint,
        files=results,
    )
    return OK if not problems else CONFLICT
