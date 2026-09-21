"""``reconcile`` — move every maintenance issue to the state its facts support.

Read-only by default, like ``scan``. ``--publish`` writes at most two things per issue: the
state token inside the owned block and the Lifecycle block after it, in one PATCH, plus the
close when — and only when — the latest stable release proves the work shipped. It never
merges anything, never publishes anything, never posts a comment, never reopens an issue and
never edits a closed one.

Every write is idempotent. The Lifecycle block carries no run timestamp, so a decision that
has not changed renders byte for byte identically and produces no PATCH at all; a run that
died between the body write and the close finds the body already right on the next run and
does the close it still owes.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .. import exit_codes, github, observations, output, paths, readiness, redact
from ..catalog.loader import Catalog, Game
from ..exit_codes import TrackerError, UsageError
from ..publish.release_client import ReleaseClient
from ..tracker import dashboard, identity, issues, lifecycle, prs
from ..tracker.release_reconcile import ReleaseFacts, ReleaseVerdict, latest_stable, verdict
from . import load_catalog

SCHEMA = "takaro-maint-reconcile/1"

#: Closed states this command reports and never touches.
DECLINED = "declined"
CLOSED_COMPLETED = "closed-completed"
CLOSED_BEFORE_RELEASE = "closed-before-release"
CLOSED_OTHER = "closed-other"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "reconcile",
        help="recompute every maintenance issue's lifecycle state from pull requests, the catalog and releases",
    )
    add_arguments(parser)
    parser.set_defaults(handler=_reconcile, op="reconcile")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The flags ``reconcile`` takes, shared with ``run``'s composite parser."""
    parser.add_argument("--publish", action="store_true", help="actually write to the tracker (default: read-only)")
    parser.add_argument("--game", default=None, help="restrict the run to one catalog game")
    parser.add_argument(
        "--issue",
        action="append",
        type=int,
        default=None,
        help="restrict the run to these issue numbers; repeatable",
    )
    parser.add_argument("--catalog-ref", default="main", help="the ref whose catalog counts as published")
    parser.add_argument("--repo", default=None, help="tracker repository (default: $TAKARO_MAINT_REPO, then origin)")
    parser.add_argument("--api-url", default=None, help="GitHub API base (default: $TAKARO_MAINT_GITHUB_API_URL)")
    parser.add_argument("--out", default=None, help="also write the report to this file (mode 0600)")


class Lookup:
    """What one run reads once and then reuses: pull requests, catalogs and releases."""

    def __init__(self, client: github.GitHub, ref: str) -> None:
        self.client = client
        self.ref = ref
        self.pulls = prs.list_pulls(client)
        self.references = prs.references(self.pulls)
        self.targets: dict[str, list[dict[str, Any]]] = {}
        self.notes: dict[str, str | None] = {}
        self.releases: dict[str, ReleaseFacts | None] = {}

    def catalog_on_ref(self, game: Game) -> list[dict[str, Any]]:
        if game.id not in self.targets:
            records, note = lifecycle.remote_targets(self.client, game.id, self.ref)
            self.targets[game.id] = records
            self.notes[game.id] = note
        return self.targets[game.id]

    def release_for(self, game: Game) -> ReleaseFacts | None:
        connector = _connector(game)
        if connector not in self.releases:
            self.releases[connector] = latest_stable(ReleaseClient(self.client), connector)
        return self.releases[connector]


def _connector(game: Game) -> str:
    return str(game.record.get("connector") or game.id)


def _games_in_scope(catalog: Catalog, wanted: str | None) -> dict[str, Game]:
    if wanted is None:
        return {key: catalog.games[key] for key in sorted(catalog.games)}
    if wanted not in catalog.games:
        known = ", ".join(sorted(catalog.games)) or "<none>"
        raise UsageError(f"no catalog game '{wanted}'; the catalog holds: {known}")
    return {wanted: catalog.games[wanted]}


def _closed_reason(issue: dict[str, Any], body_state: str | None) -> str:
    reason = str(issue.get("state_reason") or "completed")
    if reason == "not_planned":
        return DECLINED
    if reason != "completed":
        return CLOSED_OTHER
    return CLOSED_COMPLETED if body_state == lifecycle.RELEASED else CLOSED_BEFORE_RELEASE


def _entry(issue: int, marker: dict[str, str], game_id: str | None, body_state: str | None) -> dict[str, Any]:
    return {
        "issue": issue,
        "identity": identity.canonical(
            marker.get("provider", ""), marker.get("component", ""), marker.get("branch", ""), marker.get("rev", "")
        )
        if marker
        else None,
        "game": game_id,
        "from": body_state,
        "to": body_state,
        "action": "noop",
        "reason": None,
        "reasons": [],
        "prs": [],
        "targets": [],
        "release": None,
    }


def _moved(client: github.GitHub, issue: dict[str, Any]) -> bool:
    """Whether the issue changed between the listing and now. A moved issue is left alone."""
    fresh = client.issue_get(int(issue["number"]))
    if str(fresh.get("state") or "open") != "open":
        return True
    if identity.parse_marker(str(fresh.get("body") or "")) != identity.parse_marker(str(issue.get("body") or "")):
        return True
    before, after = issue.get("updated_at"), fresh.get("updated_at")
    return bool(before) and bool(after) and str(before) != str(after)


def _write_out(path: str, document: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(redact.redact(json.dumps(document, indent=2, ensure_ascii=False)) + "\n", encoding="utf-8")
    os.chmod(destination, 0o600)


def execute(args: Any) -> tuple[int, dict[str, Any]]:
    """The whole command, without touching stdout. ``run`` calls this directly."""
    catalog = load_catalog()
    games = _games_in_scope(catalog, args.game)
    client = github.client(args.repo, None, args.api_url, paths.repo_root())
    ref = str(args.catalog_ref)
    now = observations.utcnow()
    wanted: set[int] | None = set(args.issue) if args.issue else None

    report: dict[str, Any] = {
        "schema": SCHEMA,
        "mode": "publish" if args.publish else "read-only",
        "repo": client.repo,
        "catalogRef": ref,
        "dashboard": {"issue": None, "updated": False},
        "pulls": {"listed": 0, "referencing": 0},
        "catalog": {},
        "releases": {},
        "issues": [],
        "plan": [],
        "applied": [],
        "warnings": [],
    }

    try:
        cache = issues.LookupCache()
        board = dashboard.load(client, cache)
        report["dashboard"] = {"issue": board.issue, "updated": False}
        before_board = json.dumps(board.data, sort_keys=True)

        lookup = Lookup(client, ref)
        report["pulls"] = {"listed": len(lookup.pulls), "referencing": len(lookup.references)}
        for game in games.values():
            records = lookup.catalog_on_ref(game)
            report["catalog"][game.id] = {
                "ref": ref,
                "targets": [
                    lifecycle.MainTarget(
                        id=str(record["id"]),
                        status=str((record.get("support") or {}).get("status") or ""),
                        revision=str(record.get("revision") or ""),
                        platform=str(record.get("platform") or ""),
                        record=record,
                        ref=ref,
                    ).summary()
                    for record in sorted(records, key=lambda item: str(item.get("id") or ""))
                    if (record.get("support") or {}).get("status") != "retired"
                ],
                "note": lookup.notes.get(game.id),
            }
            facts = lookup.release_for(game)
            report["releases"][game.id] = facts.summary(_connector(game)) if facts else None

        seen: set[int] = set()
        for issue in sorted(client.issues_list(state="all", labels=identity.LABEL), key=_number):
            number = int(issue["number"])
            marker = identity.parse_marker(str(issue.get("body") or "")) or {}
            if marker.get("kind") != "support":
                continue
            found = lifecycle.game_for(marker, catalog)
            if found is None or found.id not in games:
                continue
            if wanted is not None and number not in wanted:
                continue
            seen.add(number)
            report["warnings"] += prs.warnings_for(number, lookup.references.get(number, []))
            _one_issue(issue, marker, found, lookup, board, report, args=args, client=client, now=now, ref=ref)

        for number in sorted((wanted or set()) - seen):
            entry = _entry(number, {}, None, None)
            entry["reason"] = "not-a-support-issue"
            report["issues"].append(entry)

        _save_dashboard(client, board, report, publish=bool(args.publish), before=before_board)
    except TrackerError as exc:
        report["error"] = exc.message
        output.error(exc.message)
        return exit_codes.TRACKER, report

    return exit_codes.OK, report


def _number(issue: dict[str, Any]) -> int:
    return int(issue.get("number") or 0)


def _one_issue(
    issue: dict[str, Any],
    marker: dict[str, str],
    game: Game,
    lookup: Lookup,
    board: dashboard.Dashboard,
    report: dict[str, Any],
    *,
    args: Any,
    client: github.GitHub,
    now: str,
    ref: str,
) -> None:
    """Decide one issue, record what that means, and write it when ``--publish`` says so."""
    number = int(issue["number"])
    body = str(issue.get("body") or "")
    body_state = issues.existing_state(body)
    entry = _entry(number, marker, game.id, body_state)
    report["issues"].append(entry)

    if str(issue.get("state") or "open") == "closed":
        entry["reason"] = _closed_reason(issue, body_state)
        output.info(f"#{number}: {entry['reason']}; left alone")
        return
    if body_state in (lifecycle.SUPERSEDED, lifecycle.REVIEW):
        entry["reason"] = body_state
        return

    live, retired = lifecycle.identity_matches(marker, lookup.catalog_on_ref(game), game_id=game.id, ref=ref)
    connector = _connector(game)
    release = lookup.release_for(game)
    decision = lifecycle.decide(
        lifecycle.Facts(
            marker=marker,
            body_state=body_state,
            readiness_rows=readiness.parse_rows(body),
            prs=lookup.references.get(number, []),
            targets=live,
            release=_combined(release, live, connector=connector),
            previous=lifecycle.parse_lifecycle(body),
            catalog_ref=ref,
            connector=connector,
            now=now,
            retired=retired,
            catalog_note=lookup.notes.get(game.id),
        )
    )

    entry.update(
        {
            "to": decision.state,
            "reasons": decision.reasons,
            "prs": sorted(pull.number for pull in decision.prs),
            "targets": sorted(target.id for target in decision.targets),
            "release": decision.release,
        }
    )

    new_body = lifecycle.apply(body, decision)
    if new_body is None:
        entry["reason"] = "unrecognised-body"
        return

    # The dashboard mirrors the decision whether or not the body needs a PATCH. A run that
    # wrote the issue and then lost the dashboard compare-and-swap leaves a body that is
    # already right and a dashboard that is not; without this the rerun would report
    # "unchanged" and never repair it.
    work = board.data.setdefault("work", {})
    held = work.get(str(entry["identity"]))
    work[str(entry["identity"])] = {"issue": number, "state": decision.state, "since": decision.since}

    closing = decision.state == lifecycle.RELEASED
    if new_body == body and not closing:
        entry["reason"] = "unchanged"
        return

    entry["action"] = "close" if closing else "update"
    action = "close-issue" if closing else "update-issue"
    report["plan"].append({"action": action, "issue": number, "state": decision.state})
    if not args.publish:
        return

    if _moved(client, issue):
        entry["action"] = "noop"
        entry["reason"] = "moved-during-run"
        report["plan"].pop()
        if held is None:
            work.pop(str(entry["identity"]), None)
        else:
            work[str(entry["identity"])] = held
        output.info(f"#{number}: moved during the run; the next run re-evaluates it")
        return

    fields: dict[str, Any] = {"body": new_body}
    if closing:
        fields.update({"state": "closed", "state_reason": "completed"})
    client.issue_update(number, **fields)
    issue["body"] = new_body
    report["applied"].append({"action": action, "issue": number, "state": decision.state})
    output.info(f"#{number}: {body_state or 'new'} -> {decision.state}")


def _combined(
    facts: ReleaseFacts | None,
    targets: list[lifecycle.MainTarget],
    *,
    connector: str,
) -> ReleaseVerdict | None:
    """One verdict over every matched target: released only when the release ships them all.

    The detail is merged across the targets rather than taken from the first, so the issue's
    Release row names every artifact the run actually proved, not one platform's worth of them.
    """
    if not targets:
        return None
    reasons: list[str] = []
    release: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = []
    for target in targets:
        one = verdict(facts, target, connector=connector)
        reasons += [reason for reason in one.reasons if reason not in reasons]
        if not one.ok or one.release is None:
            continue
        release = release or {**one.release}
        artifacts += [item for item in one.release["artifacts"] if item not in artifacts]
    if release is not None:
        release["artifacts"] = sorted(artifacts, key=lambda item: str(item["name"]))
    return ReleaseVerdict(not reasons, reasons, release)


def _save_dashboard(
    client: github.GitHub,
    board: dashboard.Dashboard,
    report: dict[str, Any],
    *,
    publish: bool,
    before: str,
) -> None:
    """Mirror the run into the dashboard, compare-and-swap, and never create one.

    Creating a dashboard is ``scan``'s job: a tracker with no dashboard has not been
    bootstrapped, and reconcile inventing one would hide that.
    """
    board.data["lifecycle"] = {
        "catalogRef": report["catalogRef"],
        "releases": {
            game_id: {key: facts[key] for key in ("tag", "version", "htmlUrl", "note")}
            for game_id, facts in report["releases"].items()
            if facts
        },
    }
    if board.issue is None or json.dumps(board.data, sort_keys=True) == before:
        return
    report["plan"].append({"action": "update-dashboard", "issue": board.issue})
    if not publish:
        return
    dashboard.save(client, board)
    report["dashboard"] = {"issue": board.issue, "updated": True}
    report["applied"].append({"action": "update-dashboard", "issue": board.issue})


def _reconcile(args: Any) -> int:
    code, report = execute(args)
    ok = code == exit_codes.OK
    fields = dict(report)
    if ok:
        fields.pop("error", None)
    elif not fields.get("error"):
        fields["error"] = exit_codes.DESCRIPTIONS.get(code, "reconcile failed")
    output.emit("reconcile", ok, **fields)
    if args.out:
        _write_out(args.out, {"schemaVersion": 1, "op": "reconcile", "ok": ok, **fields})
    return code
