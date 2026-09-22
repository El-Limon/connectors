"""``dashboard show`` — read the maintenance dashboard and say how the tracker is doing.

It is a read and nothing but a read: no issue is created, no checkpoint moves, no byte of
the dashboard is written back. ``health`` is derived here from what the board already
holds rather than stored in it, so two people asking at the same moment get the same
answer and neither of them changes it.

Failing a process because a source failed is ``run``'s job: this command exits 0 whether
the board is healthy, degraded or absent. Only a tracker or authentication failure (9) and
an unknown ``--game`` (2) make it nonzero.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .. import github, output, paths, redact
from ..catalog.loader import Catalog
from ..exit_codes import OK
from ..github import DEFAULT_API_URL
from ..tracker import checkpoints, dashboard, issues
from . import load_catalog

SCHEMA = "takaro-maint-dashboard-show/1"
#: The same em dash the dashboard's own tables use for an empty cell.
DASH = "—"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("dashboard", help="read the maintenance dashboard")
    inner = parser.add_subparsers(dest="dashboard_command", metavar="<subcommand>")

    show = inner.add_parser("show", help="last success, per-source outcome, observed heads and filed work")
    show.add_argument("--game", default=None, help="restrict the source list to one catalog game")
    show.add_argument("--repo", default=None, help="tracker repository (default: $TAKARO_MAINT_REPO, then origin)")
    show.add_argument("--api-url", default=None, help="GitHub API base (default: $TAKARO_MAINT_GITHUB_API_URL)")
    show.add_argument("--format", default="json", choices=["json", "table"])
    show.add_argument("--out", default=None, help="also write the JSON document to this file (mode 0600)")
    show.set_defaults(handler=_show, op="dashboard show")

    parser.set_defaults(handler=None, op="dashboard")


def _watched_keys(catalog: Catalog, game_id: str | None) -> list[str]:
    """Every source the catalog watches, as ``<game>/<source>``.

    Listed from the catalog rather than from the board, so a source nobody has scanned yet
    is visible as ``uninitialized`` instead of simply missing from the answer.
    """
    games = [catalog.game(game_id)] if game_id else [catalog.games[key] for key in sorted(catalog.games)]
    keys: list[str] = []
    for game in games:
        for source_id, record in sorted((game.record.get("sources") or {}).items()):
            if record.get("watch"):
                keys.append(f"{game.id}/{source_id}")
    return keys


def _source_document(entry: dict[str, Any]) -> dict[str, Any]:
    """One source's row. An empty entry is a source the dashboard has never recorded."""
    checkpoint = checkpoints.from_json(entry.get("checkpoint"))
    summary = None if checkpoint is None else {**checkpoint.summary(), "at": checkpoint.at or None}
    return {
        "status": str(entry.get("status") or "uninitialized"),
        "history": entry.get("history"),
        "heads": dict(entry.get("heads") or {}),
        "checkpoint": summary,
        "lastSuccess": entry.get("lastSuccess"),
        "lastError": entry.get("lastError"),
    }


def _web_url(api_url: str, repo: str, issue: int | None) -> str | None:
    """The browser URL, but only for github.com.

    A fake tracker in a test and an enterprise host both answer on another origin, and
    their web address is not derivable from their API address — so it is ``null`` rather
    than a guess somebody might follow.
    """
    if issue is None or api_url.rstrip("/") != DEFAULT_API_URL:
        return None
    return f"https://github.com/{repo}/issues/{issue}"


def _health(issue: int | None, last_success: Any, sources: dict[str, Any]) -> str:
    if issue is None:
        return "absent"
    if last_success and all(entry["status"] == "ok" for entry in sources.values()):
        return "ok"
    return "degraded"


def _table(document: dict[str, Any]) -> str:
    """The two tables the dashboard issue itself renders, under one header line."""
    issue = document["issue"]
    if issue is None:
        header = f"dashboard: none ({document['health']})"
    else:
        header = f"dashboard #{issue} ({document['health']}) last success {document['lastSuccess'] or DASH}"

    source_rows: list[str] = []
    for key, entry in document["sources"].items():
        heads = ", ".join(f"{branch} {rev}" for branch, rev in sorted(entry["heads"].items())) or DASH
        checkpoint = entry["checkpoint"] or {}
        seen = str(checkpoint.get("seen") or 0)
        if checkpoint.get("floor"):
            seen += f" (floor {checkpoint['floor']})"
        source_rows.append(
            f"| {key} | {entry['status']} | {heads} | {seen} "
            f"| {entry['lastSuccess'] or DASH} | {entry['lastError'] or DASH} |"
        )

    work_rows = [
        f"| {work_identity} | #{entry.get('issue')} | {entry.get('state', DASH)} | {entry.get('since', DASH)} |"
        for work_identity, entry in document["work"].items()
    ]

    lines = [
        header,
        "",
        "## Sources",
        "",
        "| Source | Status | Head | Seen | Last success | Last error |",
        "| --- | --- | --- | --- | --- | --- |",
        *(source_rows or [f"| (none) | {DASH} | {DASH} | {DASH} | {DASH} | {DASH} |"]),
        "",
        "## Work",
        "",
        "| Work | Issue | State | Since |",
        "| --- | --- | --- | --- |",
        *(work_rows or [f"| (none) | {DASH} | {DASH} | {DASH} |"]),
    ]
    return "\n".join(lines)


def _write_out(path: str, document: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(redact.redact(json.dumps(document, indent=2, ensure_ascii=False)) + "\n", encoding="utf-8")
    os.chmod(destination, 0o600)


def _show(args: Any) -> int:
    catalog = load_catalog()
    watched = _watched_keys(catalog, args.game)
    client = github.client(args.repo, None, args.api_url, paths.repo_root())
    board = dashboard.load(client, issues.LookupCache())

    recorded: dict[str, Any] = board.data.get("sources") or {}
    scope = {key for key in recorded if args.game is None or str(key).startswith(f"{args.game}/")}
    sources = {key: _source_document(recorded.get(key) or {}) for key in sorted(set(watched) | scope)}

    work = {key: dict(value) for key, value in sorted((board.data.get("work") or {}).items())}
    targets = {key: value for key, value in sorted((board.data.get("targets") or {}).items())}
    statuses = [entry["status"] for entry in sources.values()]
    last_success = board.data.get("lastSuccess")

    fields: dict[str, Any] = {
        "schema": SCHEMA,
        "repo": client.repo,
        "issue": board.issue,
        "url": _web_url(client.api_url, client.repo, board.issue),
        "updatedAt": board.data.get("updatedAt"),
        "lastSuccess": last_success,
        "health": _health(board.issue, last_success, sources),
        "counts": {
            "sources": len(sources),
            "ok": statuses.count("ok"),
            "failed": statuses.count("failed"),
            "uninitialized": statuses.count("uninitialized"),
            "work": len(work),
        },
        "sources": sources,
        "work": work,
        "targets": targets,
    }

    if args.format == "table":
        output.raw(_table(fields))
    else:
        output.emit("dashboard show", True, **fields)
    if args.out:
        _write_out(args.out, {"schemaVersion": 1, "op": "dashboard show", "ok": True, **fields})
    return OK
