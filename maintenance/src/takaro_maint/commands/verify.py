"""``verify`` — boot the target for real and prove the connector works on it."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .. import output
from ..exit_codes import OK, VERIFICATION, UsageError
from ..verify.runner import CHECK_IDS, RunOptions, cleanup_orphans, run_targets
from . import add_selection_arguments, select_many


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("verify", help="run a target's verification checks against a real server")
    add_selection_arguments(parser, multiple=True)
    parser.add_argument("--artifacts", required=True, help="a build output directory with a build-manifest.json")
    parser.add_argument("--out", required=True, help="where reports and logs are written")
    parser.add_argument("--checks", default=None, help=f"comma-separated subset of: {', '.join(CHECK_IDS)}")
    parser.add_argument("--parallel", type=int, default=1, help="targets to verify at once (only 1 today)")
    parser.add_argument("--startup-timeout", type=float, default=300.0)
    parser.add_argument("--takaro", default="local", choices=["local", "hosted"])
    parser.add_argument("--run-id", default="local", help="label and container-name suffix for this run")
    parser.add_argument("--label", action="append", default=[], help="extra docker label, repeatable")
    parser.add_argument("--keep-on-failure", action="store_true", help="keep the data dir when a check fails")
    parser.add_argument("--cleanup-orphans", action="store_true", help="remove containers from an earlier run first")
    parser.add_argument("--negative", action="store_true", help="reserved: restart and reconnect checks (#152)")
    parser.set_defaults(handler=_verify, op="verify")


def _verify(args: Any) -> int:
    if args.takaro == "hosted":
        raise UsageError("hosted mode ships with #152; use --takaro local")
    if args.parallel != 1:
        raise UsageError("--parallel is reserved; only one target at a time is supported today")

    catalog, targets = select_many(args)
    artifacts = Path(args.artifacts).expanduser().resolve()
    if not (artifacts / "build-manifest.json").is_file():
        raise UsageError(f"{artifacts} holds no build-manifest.json; run `takaro-maint build --out {artifacts}` first")

    only = [c.strip() for c in args.checks.split(",")] if args.checks else None
    if only:
        unknown = sorted(set(only) - set(CHECK_IDS))
        if unknown:
            raise UsageError(f"unknown check(s) {unknown}; known checks: {', '.join(CHECK_IDS)}")

    if args.cleanup_orphans:
        removed = cleanup_orphans(args.run_id)
        if removed:
            output.info(f"removed {len(removed)} container(s) left by an earlier run")

    options = RunOptions(
        artifacts=artifacts,
        out=Path(args.out).expanduser().resolve(),
        run_id=args.run_id,
        labels=list(args.label),
        startup_timeout=args.startup_timeout,
        only=only,
        keep_on_failure=args.keep_on_failure,
        negative=args.negative,
    )
    reports = run_targets(catalog, targets, options)
    ok = all(report["outcome"] == "pass" for report in reports)
    output.emit(
        "verify",
        ok,
        takaro=args.takaro,
        runId=args.run_id,
        out=str(options.out),
        reports=[
            {
                "target": report["target"]["id"],
                "level": report["level"],
                "outcome": report["outcome"],
                "checks": [{"id": c["id"], "status": c["status"]} for c in report["checks"]],
            }
            for report in reports
        ],
    )
    return OK if ok else VERIFICATION
