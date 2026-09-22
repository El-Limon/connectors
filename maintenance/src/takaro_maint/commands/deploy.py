"""``deploy`` — put a built artifact into an installed game directory, by manifest row."""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path
from typing import Any

from .. import net, output, paths
from ..exit_codes import OK, ConflictError
from ..games import adapter_for
from ..install.ledger import artifacts_of, read_ledger, write_ledger
from ..publish import read_manifest
from . import add_selection_arguments, select_one

# Artifact names earlier releases used for the same role; removed on deploy so a
# server never loads two Takaro mods at once.
LEGACY_NAMES = ("TakaroMinecraft.jar",)
LEGACY_PREFIXES = ("takaro-minecraft-mod-", "takaro-fabric-", "takaro-paper-", "takaro-neoforge-")


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("deploy", help="deploy a built artifact into an installed game directory")
    add_selection_arguments(parser)
    parser.add_argument("--dest", required=True, help="the installed game directory")
    parser.add_argument("--from", dest="manifest", required=True, help="the build-manifest.json to deploy from")
    parser.set_defaults(handler=_deploy, op="deploy")


def _deploy(args: Any) -> int:
    _, target = select_one(args)
    dest = Path(args.dest).expanduser().resolve()
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = read_manifest(manifest_path)

    ledger = read_ledger(dest)
    if ledger is None:
        raise ConflictError(f"{dest} holds no installed target; run `takaro-maint install` first")
    if ledger.fingerprint != target.fingerprint:
        raise ConflictError(
            f"{dest} holds target '{ledger.target}' ({ledger.fingerprint[:16]}), "
            f"the build is for '{target.id}' ({target.fp16})"
        )

    deployed: list[dict[str, Any]] = []
    removed: list[str] = []
    rows_by_role = {row["role"]: row for row in artifacts_of(ledger.data)}
    for component in target.record["components"]:
        role = component["role"]
        rows = [r for r in manifest["artifacts"] if r["role"] == role and r["target"] == target.id]
        if not rows:
            raise ConflictError(
                f"{manifest_path.name} has no '{role}' artifact for target '{target.id}' "
                f"(it holds: {sorted({(r['role'], r['target']) for r in manifest['artifacts']})})"
            )
        if len(rows) > 1:
            raise ConflictError(f"{manifest_path.name} has {len(rows)} '{role}' rows for target '{target.id}'")
        row = rows[0]
        file_name = paths.safe_relative(row["file"], field="build-manifest artifacts[].file")
        source = manifest_path.parent / file_name
        if not source.is_file():
            raise ConflictError(f"{row['file']} is named by the manifest but missing next to it")
        actual = net.sha256_file(source)
        if actual != row["sha256"]:
            raise ConflictError(f"{row['file']} sha256 {actual} != manifest {row['sha256']}")

        install_dir = dest / paths.safe_relative(component["installDir"], field="components[].installDir")
        install_dir.mkdir(parents=True, exist_ok=True)
        superseded = [
            existing
            for existing in sorted(install_dir.iterdir())
            if existing.is_file()
            and existing.name != row["file"]
            and (existing.name in LEGACY_NAMES or any(existing.name.startswith(p) for p in LEGACY_PREFIXES))
        ]

        # Stage, fsync and swap before removing anything: a copy that dies halfway (ENOSPC,
        # permissions, a kill) must leave the previously deployed connector in place rather than
        # a directory with no connector at all and a ledger that still describes the old one.
        staged = install_dir / (row["file"] + ".tmp")
        try:
            shutil.copy2(source, staged)
            with staged.open("rb") as handle:
                os.fsync(handle.fileno())
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        os.replace(staged, install_dir / row["file"])
        for existing in superseded:
            existing.unlink()
            removed.append(f"{component['installDir']}/{existing.name}")
        # A game whose artifact is not loaded as it lands -- an archive the server expects
        # unpacked, say -- unpacks it here, once the file itself is in place.
        adapter_for(target.game).after_deploy(dest, component, install_dir / row["file"])
        relative = f"{component['installDir']}/{row['file']}"
        deployed.append({"role": role, "path": relative, "sha256": row["sha256"]})
        output.info(f"deployed {relative} ({row['sha256'][:16]}…)")

        # Every role this directory holds stays attested. A two-role game deploys twice,
        # and a ledger that only ever remembered the last one left the other unguarded --
        # `ledger check` could not tell a tampered first artifact from a good one. The
        # ledger is rewritten after each role rather than once at the end, so a run that
        # dies between roles still attests the ones that landed.
        rows_by_role.update(
            {
                role: {
                    "role": role,
                    "path": relative,
                    "sha256": row["sha256"],
                    "connectorVersion": manifest["version"],
                    "sourceRevision": manifest["sourceRevision"],
                }
            }
        )
        data = dict(ledger.data)
        data["artifacts"] = [rows_by_role[key] for key in sorted(rows_by_role)]
        data.pop("artifact", None)
        write_ledger(dest, data)
        ledger = read_ledger(dest)
        assert ledger is not None

    output.emit(
        "deploy",
        True,
        game=target.game,
        target=target.id,
        dest=str(dest),
        deployed=deployed,
        removed=removed,
        connectorVersion=manifest["version"],
    )
    return OK
