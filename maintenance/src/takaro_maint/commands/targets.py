"""``targets list`` and ``targets resolve`` — the single resolution every entry point reads."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .. import output
from ..catalog.loader import resolve
from ..exit_codes import OK
from . import load_catalog, select_one


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("targets", help="list and resolve catalog targets")
    inner = parser.add_subparsers(dest="targets_command", metavar="<subcommand>")

    listing = inner.add_parser("list", help="list targets")
    listing.add_argument("--game", default=None)
    listing.add_argument("--platform", default=None)
    listing.add_argument("--status", default=None, help="comma-separated support statuses to keep")
    listing.add_argument("--rig-game", default=None, help="filter on devServers.gameId")
    listing.add_argument("--format", default="json", choices=["json", "table", "gha"])
    listing.set_defaults(handler=_list, op="targets list")

    resolver = inner.add_parser("resolve", help="resolve one target to its full deployment description")
    resolver.add_argument("--game", required=True)
    resolver.add_argument("--target", default=None)
    resolver.add_argument("--platform", default=None)
    resolver.add_argument("--format", default="json", choices=["json", "env", "gha"])
    resolver.add_argument("--prefix", default="TAKARO_TARGET", help="key prefix for --format env")
    resolver.add_argument("--out", default=None, help="write the output to this file (mode 0600)")
    resolver.set_defaults(handler=_resolve, op="targets resolve")

    parser.set_defaults(handler=None, op="targets")


def _list(args: Any) -> int:
    catalog = load_catalog()
    statuses = {s.strip() for s in args.status.split(",")} if args.status else None
    rows = []
    for target in catalog.all_targets():
        if args.game and target.game != args.game:
            continue
        if args.platform and target.platform != args.platform:
            continue
        if statuses and target.status not in statuses:
            continue
        if args.rig_game and target.rig_game != args.rig_game:
            continue
        rows.append(target.summary())
    rows.sort(key=lambda row: (row["game"], row["id"]))

    if args.format == "gha":
        output.raw("targets=" + json.dumps(rows, separators=(",", ":")))
    elif args.format == "table":
        header = f"{'GAME':<12} {'TARGET':<16} {'PLATFORM':<10} {'REVISION':<12} {'FP16':<18} STATUS"
        lines = [header] + [
            f"{r['game']:<12} {r['id']:<16} {r['platform']:<10} {r['revision']:<12} {r['fp16']:<18} {r['status']}"
            for r in rows
        ]
        output.raw("\n".join(lines))
    else:
        output.emit("targets list", True, count=len(rows), targets=rows)
    return OK


def _env_lines(env: dict[str, str]) -> str:
    return "\n".join(f"{key}={value}" for key, value in sorted(env.items()))


def _write_out(path: str, text: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    os.chmod(destination, 0o600)


def _resolve(args: Any) -> int:
    catalog, target = select_one(args)
    resolved = resolve(catalog, target, prefix=args.prefix)

    if args.format == "env":
        text = _env_lines(resolved["env"])
        if args.out:
            _write_out(args.out, text)
            output.info(f"wrote {len(resolved['env'])} keys to {args.out}")
        else:
            output.raw(text)
    elif args.format == "gha":
        fields = {
            "target": resolved["id"],
            "game": resolved["game"],
            "platform": resolved["platform"],
            "revision": resolved["revision"],
            "fingerprint": resolved["fingerprint"],
            "fp16": resolved["fp16"],
            "image": resolved["containerRef"],
            "toolchain": resolved["toolchainRef"],
            "java": str(resolved["runtime"]["java"]),
            "gradle_project": resolved["build"]["gradleProject"],
        }
        text = "\n".join(f"{key}={value}" for key, value in fields.items())
        text += "\nenv=" + json.dumps(resolved["env"], separators=(",", ":"))
        if args.out:
            _write_out(args.out, text)
        else:
            output.raw(text)
    else:
        if args.out:
            _write_out(args.out, json.dumps(resolved, indent=2, ensure_ascii=False))
        output.emit("targets resolve", True, **resolved)
    return OK
