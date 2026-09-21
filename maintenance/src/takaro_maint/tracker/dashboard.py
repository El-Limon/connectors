"""The dashboard issue: the only place a scan keeps state.

One issue per repository holds every source's checkpoint, the identity of every piece of
work filed so far, and a snapshot of the catalog's targets. It is written with a
compare-and-swap (re-read the body immediately before the PATCH and refuse if it moved),
so two runners can never silently overwrite each other's checkpoints. A human table sits
above the machine block for the people who read it; text outside the two dashboard markers
is theirs and is preserved on every rewrite.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..exit_codes import TrackerError
from ..github import GitHub
from . import checkpoints, identity, issues
from .checkpoints import Checkpoint

TITLE = "Connector maintenance dashboard"
BEGIN = "<!-- takaro-maint:dashboard:begin -->"
END = "<!-- takaro-maint:dashboard:end -->"
SCHEMA = "takaro-maint-dashboard/1"

INTRO = (
    "State for `takaro-maint scan`. Everything between the dashboard markers is rewritten by the "
    "command (compare-and-swap: a concurrent edit makes the run stop rather than overwrite); edit "
    "anything else freely. Closing this issue resets nothing: the next scan still finds it "
    "by its marker, closed or not, and rewrites it in place."
)

#: GitHub refuses a body over 65536 characters, so the writer stays well inside that.
MAX_BODY = 60000
#: And a single source never keeps more than this many revision ids by name.
MAX_SEEN = 1000

_FENCE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)
DASH = "—"


@dataclass
class Dashboard:
    """The parsed dashboard plus exactly what compare-and-swap needs to verify."""

    issue: int | None = None
    data: dict[str, Any] = field(default_factory=dict)
    raw_body: str = ""
    updated_at: str | None = None

    @classmethod
    def empty(cls) -> Dashboard:
        return cls(
            issue=None,
            data={
                "schema": SCHEMA,
                "updatedAt": None,
                "lastSuccess": None,
                "sources": {},
                "work": {},
                "targets": {},
            },
        )

    # -- reading --------------------------------------------------------------
    def sources(self) -> dict[str, Any]:
        return self.data.setdefault("sources", {})

    def source(self, key: str) -> dict[str, Any]:
        return self.sources().setdefault(key, {})

    def checkpoint(self, key: str) -> Checkpoint | None:
        """``None`` means this source has never been initialised."""
        return checkpoints.from_json(self.sources().get(key, {}).get("checkpoint"))

    # -- writing --------------------------------------------------------------
    def apply_source(
        self,
        key: str,
        *,
        status: str,
        history: str,
        heads: dict[str, str],
        checkpoint: Checkpoint | None,
        last_error: str | None,
        at: str,
    ) -> None:
        entry = self.source(key)
        entry["status"] = status
        entry["history"] = history
        if heads:
            entry["heads"] = dict(heads)
        else:
            entry.setdefault("heads", {})
        if checkpoint is not None:
            entry["checkpoint"] = checkpoints.to_json(checkpoint)
        else:
            entry.setdefault("checkpoint", None)
        entry["lastError"] = last_error
        if status == "ok":
            entry["lastSuccess"] = at

    def set_work(self, work_identity: str, *, issue: int, state: str, at: str) -> None:
        work = self.data.setdefault("work", {})
        existing = work.get(work_identity) or {}
        work[work_identity] = {
            "issue": issue,
            "state": state,
            "since": existing.get("since") or at,
        }

    def set_targets(self, game: str, rows: list[dict[str, Any]]) -> None:
        self.data.setdefault("targets", {})[game] = rows


# -- rendering ----------------------------------------------------------------
def _source_rows(data: dict[str, Any]) -> list[str]:
    rows = []
    for key, entry in sorted((data.get("sources") or {}).items()):
        heads = entry.get("heads") or {}
        head = ", ".join(f"{branch} {rev}" for branch, rev in sorted(heads.items())) or DASH
        checkpoint = entry.get("checkpoint") or {}
        seen = len(checkpoint.get("seen") or []) if checkpoint else 0
        floor = checkpoint.get("floor") if checkpoint else None
        seen_cell = f"{seen}" + (f" (floor {floor})" if floor else "")
        rows.append(
            f"| {key} | {entry.get('status', DASH)} | {head} | {seen_cell} "
            f"| {entry.get('lastSuccess') or DASH} | {entry.get('lastError') or DASH} |"
        )
    return rows or [f"| (none) | {DASH} | {DASH} | {DASH} | {DASH} | {DASH} |"]


def _work_rows(data: dict[str, Any]) -> list[str]:
    rows = [
        f"| {work_identity} | #{entry.get('issue')} | {entry.get('state', DASH)} | {entry.get('since', DASH)} |"
        for work_identity, entry in sorted((data.get("work") or {}).items())
    ]
    return rows or [f"| (none) | {DASH} | {DASH} | {DASH} |"]


def render_block(data: dict[str, Any]) -> str:
    """The generated region: the human tables and then the machine state."""
    lines = [
        BEGIN,
        "## Sources",
        "",
        "| Source | Status | Head | Seen | Last success | Last error |",
        "| --- | --- | --- | --- | --- | --- |",
        *_source_rows(data),
        "",
        "## Work",
        "",
        "| Work | Issue | State | Since |",
        "| --- | --- | --- | --- |",
        *_work_rows(data),
        "",
        "## State",
        "",
        "```json",
        json.dumps(data, sort_keys=True, indent=1),
        "```",
        END,
    ]
    return "\n".join(lines)


def render(board: Dashboard) -> str:
    """The whole body, preserving whatever a human wrote outside the markers."""
    block = render_block(board.data)
    if not board.raw_body:
        return "\n\n".join([identity.render_marker(identity.DASHBOARD_MARKER), INTRO, block]) + "\n"
    before, begin, rest = board.raw_body.partition(BEGIN)
    if not begin:
        return board.raw_body.rstrip("\n") + "\n\n" + block + "\n"
    _, end, after = rest.partition(END)
    return before + block + (after if end else "\n")


def cap(board: Dashboard) -> None:
    """Keep the rendered body inside ``MAX_BODY`` by trimming the oldest seen revisions."""
    sources = board.sources()
    for entry in sources.values():
        checkpoint = checkpoints.from_json(entry.get("checkpoint"))
        if checkpoint is not None and len(checkpoint.seen) > MAX_SEEN:
            entry["checkpoint"] = checkpoints.to_json(checkpoints.trim(checkpoint, MAX_SEEN))
    while len(render(board)) > MAX_BODY:
        largest: tuple[str, int] | None = None
        for key, entry in sources.items():
            count = len((entry.get("checkpoint") or {}).get("seen") or [])
            if largest is None or count > largest[1]:
                largest = (key, count)
        if largest is None or largest[1] == 0:
            return  # nothing left to trim; the body is large for another reason
        checkpoint = checkpoints.from_json(sources[largest[0]]["checkpoint"])
        assert checkpoint is not None
        keep = max(0, largest[1] - max(1, largest[1] // 10))
        sources[largest[0]]["checkpoint"] = checkpoints.to_json(checkpoints.trim(checkpoint, keep))


# -- transport ----------------------------------------------------------------
def load(client: GitHub, cache: issues.LookupCache | None = None) -> Dashboard:
    """The dashboard as the tracker holds it, or an empty one when it does not exist yet."""
    found = issues.find(client, identity.DASHBOARD_MARKER, cache or issues.LookupCache())
    if found is None:
        return Dashboard.empty()
    body = str(found.get("body") or "")
    match = _FENCE.search(body.partition(BEGIN)[2].partition(END)[0])
    if match is None:
        raise TrackerError(f"dashboard #{found['number']} body is not readable; fix it by hand")
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise TrackerError(f"dashboard #{found['number']} body is not readable; fix it by hand ({exc})") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise TrackerError(f"dashboard #{found['number']} body is not readable; fix it by hand (wrong schema)")
    updated = found.get("updated_at")
    return Dashboard(
        issue=int(found["number"]),
        data=data,
        raw_body=body,
        updated_at=str(updated) if updated else None,
    )


def save(client: GitHub, board: Dashboard) -> tuple[int, bool]:
    """Create or compare-and-swap the dashboard. Returns ``(issue number, created)``."""
    cap(board)
    body = render(board)
    if board.issue is None:
        # The lookup that said "there is no dashboard" may have read a stale index: GitHub
        # is read-after-write eventually consistent for both the issue search and the issue
        # listing, and a dashboard created seconds earlier can be invisible to the next
        # run. Re-check once with a fresh, uncached lookup, because creating a second
        # dashboard would split the state in two. A dashboard that has appeared since is a
        # rerun, not a duplicate.
        appeared = issues.find(client, identity.DASHBOARD_MARKER, issues.LookupCache())
        if appeared is not None:
            raise TrackerError(f"dashboard #{appeared['number']} appeared during the scan; rerun")
        created = client.issue_create(TITLE, body, [identity.LABEL])
        board.issue = int(created["number"])
        board.raw_body = body
        return board.issue, True

    current = client.issue_get(board.issue)
    current_updated = current.get("updated_at")
    moved = str(current.get("body") or "") != board.raw_body or (
        bool(board.updated_at) and bool(current_updated) and str(current_updated) != board.updated_at
    )
    if moved:
        raise TrackerError(f"dashboard #{board.issue} changed during the scan; rerun")
    client.issue_update(board.issue, body=body)
    board.raw_body = body
    return board.issue, False
