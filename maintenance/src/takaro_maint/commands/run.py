"""``run`` — ``scan`` and then ``reconcile``, in one process and one JSON document.

The two commands answer different halves of the same question ("what is new upstream?" and
"where has each piece of work got to?"), and a scheduled job wants both. Running them
separately means two token resolutions, two dashboard reads and two stdout documents to
correlate; running them here means the reconcile sees the dashboard the scan just saved.

``scan`` writes its report straight to stdout, so it is captured rather than refactored: the
scan module is another issue's and stays exactly as it is.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from argparse import Namespace
from typing import Any

from .. import exit_codes, output
from . import reconcile as reconcile_command
from . import scan as scan_command

SCHEMA = "takaro-maint-run/1"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("run", help="scan, then reconcile, as one command and one report")
    parser.add_argument("--publish", action="store_true", help="actually write to the tracker (default: read-only)")
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="initialise a source: record what is already published and file only the uncovered heads",
    )
    parser.add_argument("--game", default=None, help="restrict the run to one catalog game")
    parser.add_argument("--source", action="append", default=None, help="watched source id; repeatable")
    parser.add_argument("--catalog-ref", default="main", help="the ref whose catalog counts as published")
    parser.add_argument("--repo", default=None, help="tracker repository (default: $TAKARO_MAINT_REPO, then origin)")
    parser.add_argument("--api-url", default=None, help="GitHub API base (default: $TAKARO_MAINT_GITHUB_API_URL)")
    parser.add_argument("--out", default=None, help="also write the combined report to this file (mode 0600)")
    parser.set_defaults(handler=_run, op="run")


def _scan_document(args: Any) -> tuple[int, dict[str, Any]]:
    """Run ``scan`` with its stdout captured, and hand back its exit code and report."""
    namespace = Namespace(
        publish=bool(args.publish),
        bootstrap=bool(args.bootstrap),
        game=args.game,
        source=args.source,
        repo=args.repo,
        api_url=args.api_url,
        out=None,
    )
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = scan_command._scan(namespace)
    document = json.loads(buffer.getvalue() or "{}")
    for key in ("schemaVersion", "op"):
        document.pop(key, None)
    return code, document


def _run(args: Any) -> int:
    scan_code, scan_document = _scan_document(args)

    reconcile_document: dict[str, Any] | None = None
    reconcile_code: int | None = None
    if scan_code != exit_codes.TRACKER:
        # A tracker failure is the one thing reconcile cannot work around: the token, the API
        # or the dashboard is broken, and running the second half would only repeat it.
        reconcile_code, reconcile_document = reconcile_command.execute(
            Namespace(
                publish=bool(args.publish),
                game=args.game,
                issue=None,
                catalog_ref=args.catalog_ref,
                repo=args.repo,
                api_url=args.api_url,
                out=None,
            )
        )

    code = scan_code or (reconcile_code or exit_codes.OK)
    ok = code == exit_codes.OK
    fields: dict[str, Any] = {
        "schema": SCHEMA,
        "mode": "publish" if args.publish else "read-only",
        "scan": scan_document,
        "reconcile": reconcile_document,
        "exitCodes": {"scan": scan_code, "reconcile": reconcile_code},
    }
    if not ok:
        fields["error"] = (
            str(scan_document.get("error") or "")
            or str((reconcile_document or {}).get("error") or "")
            or exit_codes.DESCRIPTIONS.get(code, "run failed")
        )
    output.emit("run", ok, **fields)
    if args.out:
        reconcile_command._write_out(args.out, {"schemaVersion": 1, "op": "run", "ok": ok, **fields})
    return code
