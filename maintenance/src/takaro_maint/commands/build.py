"""``build`` — run the game's build for one or more targets and describe what came out."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Any

from .. import output, paths
from ..catalog.loader import resolve
from ..exit_codes import OK, ConflictError
from ..games import adapter_for
from ..publish import artifact_row, source_revision, write_checksums, write_manifest, write_meta
from . import add_selection_arguments, select_many
from .artifact import validate_file


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("build", help="build a target's artifacts and write a build manifest")
    add_selection_arguments(parser, multiple=True)
    parser.add_argument("--version", required=True, help="connector version to stamp into the artifacts")
    parser.add_argument("--out", required=True, help="directory the artifacts and manifest are written to")
    parser.add_argument("--toolchain", default="host", choices=["host", "container"])
    parser.add_argument(
        "--gradle-args",
        nargs=argparse.REMAINDER,
        default=None,
        help="extra arguments passed through to the build system",
    )
    parser.set_defaults(handler=_build, op="build")


def _watched_paths(game_id: str) -> list[str]:
    return [f"games/{game_id}", f"catalog/{game_id}"]


def _build(args: Any) -> int:
    catalog, targets = select_many(args)
    adapter = adapter_for(args.game)
    repo_root = paths.repo_root()
    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    revision, dirty = source_revision(repo_root, _watched_paths(args.game))

    rows: list[dict[str, Any]] = []
    copied: list[Path] = []
    toolchain: dict[str, Any] = {}
    for target in targets:
        resolved = resolve(catalog, target)
        toolchain = resolved["build"]["toolchain"]
        result = adapter.build(
            resolved,
            args.version,
            out,
            args.toolchain,
            repo_root,
            gradle_args=args.gradle_args,
            source_revision=revision + ("-dirty" if dirty else ""),
        )
        for role, produced in sorted(result.artifacts.items()):
            if not produced.is_file():
                raise ConflictError(
                    f"{target.id}: the build did not produce {produced.name} "
                    f"(looked for the exact catalog-derived name, never a glob)",
                    target=target.id,
                    role=role,
                    expected=produced.name,
                )
            problems = validate_file(produced, target.record, target.fingerprint)
            if problems:
                raise ConflictError(
                    f"{target.id}: {produced.name} does not carry this target's identity: " + "; ".join(problems),
                    target=target.id,
                    role=role,
                )
            destination = out / produced.name
            shutil.copy2(produced, destination)
            copied.append(destination)
            row = artifact_row(role, target.id, target.fingerprint, destination)
            rows.append(row)
            write_meta(out, row, connector=args.game, version=args.version, revision=revision)
            output.info(f"{target.id}: {destination.name} sha256 {row['sha256']}")

    manifest = write_manifest(
        out,
        connector=args.game,
        version=args.version,
        revision=revision,
        dirty=dirty,
        toolchain=toolchain,
        mode=args.toolchain,
        artifacts=rows,
    )
    checksums = write_checksums(out, copied)
    output.emit(
        "build",
        True,
        connector=args.game,
        version=args.version,
        sourceRevision=revision,
        dirty=dirty,
        out=str(out),
        manifest=manifest.name,
        checksums=checksums.name,
        artifacts=rows,
    )
    return OK
