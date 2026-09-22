"""Where one maintenance issue has got to, recomputed from durable facts every run.

A support issue is filed when upstream publishes something, and it should close when what it
asked for is published in a release — not when someone merged a branch, and not because a
pull request said ``Closes``. So nothing here remembers: every run recomputes the state from
four things that are still true tomorrow — the issue's own marker, the pull requests that
reference it, the catalog on the default branch, and the latest stable release's
compatibility record — and writes that state back. A run that died half-way leaves nothing to
repair, because the next run reaches the same conclusion from the same facts.

Two blocks carry the result. The state token lives inside the scan's owned block, where
``readiness`` and ``scan`` already read it, and is the single machine-read state. Everything
a person would want to read — which pull request, which target, which release, and what is
still missing — lives in a *second* block after the owned block, because the scan regenerates
the owned block in full on every run and anything else written inside it would be erased.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .. import fingerprint as fp
from .. import readiness
from ..exit_codes import TrackerError
from ..github import GitHub
from . import issues
from .prs import PullRef
from .release_reconcile import ReleaseVerdict

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from ..catalog.loader import Catalog, Game

DETECTED = "detected"
BLOCKED = readiness.BLOCKED
READY_FOR_AGENT = readiness.READY_FOR_AGENT
IMPLEMENTATION_PR = "implementation-pr"
AWAITING_RELEASE = "awaiting-release"
RELEASED = "released"

#: The states this command writes into the owned block, earliest first.
STATES: tuple[str, ...] = (DETECTED, BLOCKED, READY_FOR_AGENT, IMPLEMENTATION_PR, AWAITING_RELEASE, RELEASED)

#: States another stage owns. Reported, never written and never overwritten.
SUPERSEDED = readiness.SUPERSEDED
REVIEW = readiness.REVIEW

LIFECYCLE_BEGIN = "<!-- takaro-maint:lifecycle:begin -->"
LIFECYCLE_END = "<!-- takaro-maint:lifecycle:end -->"
LIFECYCLE_MARK = "<!-- takaro-maint:lifecycle="
LIFECYCLE_MARK_END = " -->"

#: Input kinds that can identify a target as the one a marker is about. A Mojang marker names a
#: game version; each Mojang-side kind records that version under its own field name. Everything
#: else in a record's ``inputs`` (a Fabric launcher, an API jar, a universal jar) is a build
#: detail, not an identity.
MOJANG_VERSION_FIELDS: dict[str, str] = {
    "mojang-version": "version",
    "paper-build": "gameVersion",
    "neoforge-installer": "gameVersion",
}
IDENTITY_KINDS: tuple[str, ...] = (*MOJANG_VERSION_FIELDS, "steam-depots")

DASH = "—"


@dataclass(frozen=True)
class MainTarget:
    """One catalog target on the reconcile ref that matches an issue's identity."""

    id: str
    status: str
    revision: str
    platform: str
    record: dict[str, Any]
    ref: str

    @property
    def fingerprint(self) -> str:
        return fp.fingerprint(self.record)

    @property
    def fp16(self) -> str:
        return self.fingerprint[:16]

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "revision": self.revision,
            "platform": self.platform,
            "fp16": self.fp16,
        }


@dataclass
class Facts:
    """Everything one issue's state is computed from. Nothing else is consulted."""

    marker: dict[str, str]
    body_state: str | None
    readiness_rows: dict[str, readiness.Row] | None
    prs: list[PullRef]
    targets: list[MainTarget]
    release: ReleaseVerdict | None
    previous: dict[str, Any] | None
    catalog_ref: str
    connector: str
    now: str
    retired: list[str] = field(default_factory=list)
    catalog_note: str | None = None


@dataclass(frozen=True)
class Decision:
    """The state one issue is in, why, and what a reader should be shown."""

    state: str
    since: str
    catalog_ref: str
    reasons: list[str] = field(default_factory=list)
    prs: list[PullRef] = field(default_factory=list)
    ignored: list[PullRef] = field(default_factory=list)
    targets: list[MainTarget] = field(default_factory=list)
    release: dict[str, Any] | None = None

    def payload(self) -> dict[str, Any]:
        """The hidden JSON line. No run timestamp: an unchanged decision renders identically."""
        release = self.release
        return {
            "catalogRef": self.catalog_ref,
            "prs": sorted(ref.number for ref in self.prs),
            "release": (
                {
                    "tag": release["tag"],
                    "version": release["version"],
                    "htmlUrl": release["htmlUrl"],
                    "artifacts": release["artifacts"],
                }
                if release
                else None
            ),
            "since": self.since,
            "state": self.state,
            "targets": sorted(target.id for target in self.targets),
        }


# -- the catalog on the reconcile ref ------------------------------------------
def game_for(marker: dict[str, str], catalog: Catalog) -> Game | None:
    """The catalog game a support marker is about, or ``None``.

    Mojang-style markers name the game in ``component``. A Steam marker names an app id
    instead, so the join is through whichever game watches that app — implemented here so a
    later Steam issue extends one function rather than every caller.
    """
    component = marker.get("component")
    if component and component in catalog.games:
        return catalog.games[component]
    app = marker.get("app")
    if not app:
        return None
    for _, game in sorted(catalog.games.items()):
        for _, source in sorted((game.record.get("sources") or {}).items()):
            watch = source.get("watch") or {}
            if str(watch.get("app") or "") == app:
                return game
    return None


def remote_targets(client: GitHub, game_id: str, ref: str) -> tuple[list[dict[str, Any]], str | None]:
    """Every target record of ``game_id`` as it stands on ``ref``, or a note saying why not.

    Read through the contents API rather than from the checkout on purpose: the branch this
    command runs from is precisely the branch that is *not* yet merged, and a target that only
    exists there has not been published to anyone.
    """
    try:
        listing = client.contents(f"catalog/{game_id}/targets", ref)
    except TrackerError as exc:
        if "HTTP 404" in exc.message:
            return [], f"catalog/{game_id}/targets is not on {ref}"
        raise
    if not isinstance(listing, list):
        return [], f"catalog/{game_id}/targets is not a directory on {ref}"

    records: list[dict[str, Any]] = []
    for entry in sorted(listing, key=lambda item: str(item.get("path") or "")):
        if entry.get("type") != "file" or not str(entry.get("name") or "").endswith(".json"):
            continue
        payload = client.contents(str(entry["path"]), ref)
        record = _decode(payload)
        if isinstance(record, dict):
            records.append(record)
    return records, None


def _decode(payload: dict[str, Any]) -> Any:
    """A contents payload as JSON, or ``None`` when it is not a record this tool can read."""
    content = payload.get("content")
    if content is None:
        return None
    raw = base64.b64decode(content) if payload.get("encoding") == "base64" else str(content).encode("utf-8")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


# -- identity ------------------------------------------------------------------
def _identity_input(record: dict[str, Any]) -> dict[str, Any] | None:
    """The first input whose kind can identify a target. Insertion order is the catalog's."""
    for spec in (record.get("inputs") or {}).values():
        if isinstance(spec, dict) and str(spec.get("kind") or "") in IDENTITY_KINDS:
            return spec
    return None


def _matches(marker: dict[str, str], record: dict[str, Any]) -> bool:
    spec = _identity_input(record)
    if spec is None:
        return False
    kind = str(spec["kind"])
    rev = marker.get("rev")
    if kind in MOJANG_VERSION_FIELDS:
        return (
            marker.get("provider") == "mojang"
            and str(spec.get(MOJANG_VERSION_FIELDS[kind])) == rev
            and str(record.get("revision")) == rev
        )
    if kind == "steam-depots":
        # Current markers carry the catalog join key directly. Accept the prefix of the
        # compound revision too so issues filed by the first Steam tracker release remain
        # reconcilable after this fix: ``<buildid>[.<manifest digest>]+<branch>``.
        marker_buildid = marker.get("buildid") or str(rev or "").partition(".")[0].partition("+")[0]
        marker_app = marker.get("app")
        return (
            marker.get("provider") == "steam"
            and (marker_app is None or str(spec.get("app")) == marker_app)
            and str(spec.get("branch")) == marker.get("branch")
            and str(spec.get("buildid")) == marker_buildid
        )
    return False  # pragma: no cover - IDENTITY_KINDS and this table are edited together


def identity_matches(
    marker: dict[str, str],
    records: list[dict[str, Any]],
    *,
    game_id: str,
    ref: str,
) -> tuple[list[MainTarget], list[str]]:
    """``(the live targets this marker is about, the ids of the retired ones)``."""
    live: list[MainTarget] = []
    retired: list[str] = []
    for record in sorted(records, key=lambda item: str(item.get("id") or "")):
        if str(record.get("game") or "") != game_id or not _matches(marker, record):
            continue
        status = str((record.get("support") or {}).get("status") or "")
        if status == "retired":
            retired.append(str(record["id"]))
            continue
        live.append(
            MainTarget(
                id=str(record["id"]),
                status=status,
                revision=str(record.get("revision") or ""),
                platform=str(record.get("platform") or ""),
                record=record,
                ref=ref,
            )
        )
    return live, retired


def matching_targets(
    marker: dict[str, str],
    records: list[dict[str, Any]],
    *,
    game_id: str,
    ref: str,
) -> list[MainTarget]:
    """The non-retired targets on ``ref`` that this marker's identity picks out."""
    return identity_matches(marker, records, game_id=game_id, ref=ref)[0]


# -- the decision --------------------------------------------------------------
def decide(facts: Facts) -> Decision:
    """The one state these facts support, by a precedence that only ever looks forward."""
    counting = [ref for ref in facts.prs if ref.counts]
    ignored = [ref for ref in facts.prs if not ref.counts]
    reasons: list[str] = [f"{target_id} is retired on {facts.catalog_ref}" for target_id in facts.retired]

    # ``maintained`` on the catalog ref is part of what the issue asks for — its acceptance
    # checklist ends with the promotion — and a target's support status is deliberately not
    # part of its fingerprint, so a release claiming ``maintained`` cannot stand in for it.
    promoted = all(target.status == "maintained" for target in facts.targets)
    if facts.targets and promoted and facts.release is not None and facts.release.ok:
        return _decided(facts, RELEASED, reasons, counting, ignored, facts.release.release)

    if facts.targets:
        reasons += (
            facts.release.reasons if facts.release is not None else [f"no stable release for {facts.connector} yet"]
        )
        reasons += [f"catalog on {facts.catalog_ref} has {target.id} ({target.status})" for target in facts.targets]
        return _decided(facts, AWAITING_RELEASE, reasons, counting, ignored, None)

    if counting:
        reasons += [ref.describe() for ref in counting]
        if any(ref.merged for ref in counting):
            reasons.append(f"merged, not on {facts.catalog_ref} yet")
        if facts.catalog_note:
            reasons.append(facts.catalog_note)
        return _decided(facts, IMPLEMENTATION_PR, reasons, counting, ignored, None)

    # Nothing later holds, so readiness recomputes from its own rows. ``current`` is None on
    # purpose: a pull request closed without merging walks the state back, which is the
    # intended outcome.
    state = (
        readiness.state_for(facts.readiness_rows, branch=facts.marker.get("branch", "release"), current=None)
        if facts.readiness_rows is not None
        else DETECTED
    )
    return _decided(facts, state, reasons, counting, ignored, None)


def _decided(
    facts: Facts,
    state: str,
    reasons: list[str],
    prs: list[PullRef],
    ignored: list[PullRef],
    release: dict[str, Any] | None,
) -> Decision:
    previous = facts.previous or {}
    since = str(previous["since"]) if previous.get("state") == state and previous.get("since") else facts.now
    return Decision(
        state=state,
        since=since,
        catalog_ref=facts.catalog_ref,
        reasons=reasons,
        prs=prs,
        ignored=ignored,
        targets=list(facts.targets),
        release=release,
    )


# -- what is written to the issue ----------------------------------------------
def parse_lifecycle(body: str) -> dict[str, Any] | None:
    """The hidden lifecycle payload a previous run wrote, or ``None``.

    A mangled line reads as absent, exactly like ``readiness.parse_rows``: the next render
    replaces it, and ``since`` restarts rather than being invented from something unreadable.
    """
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(LIFECYCLE_MARK) and stripped.endswith(LIFECYCLE_MARK_END):
            raw = stripped[len(LIFECYCLE_MARK) : -len(LIFECYCLE_MARK_END)]
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return None
            return payload if isinstance(payload, dict) else None
    return None


def _release_cell(release: dict[str, Any] | None) -> str:
    if not release:
        return DASH
    artifacts = ", ".join(f"{artifact['name']} `{str(artifact['sha256'])[:12]}`" for artifact in release["artifacts"])
    verified = f"verified {release.get('executed') or 'none'} ({release.get('takaro') or 'none'})"
    return f"[{release['tag']}]({release['htmlUrl']}) v{release['version']}: {artifacts}; {verified}"


def render_block(decision: Decision) -> str:
    """The Lifecycle block: one table a person reads, one hidden line the next run reads."""
    targets = ", ".join(f"`{t.id}` ({t.status}, fingerprint `{t.fp16}`)" for t in decision.targets) or DASH
    return "\n".join(
        [
            LIFECYCLE_BEGIN,
            "## Lifecycle",
            "",
            "| Field | Value |",
            "| --- | --- |",
            f"| State | `{decision.state}` since {decision.since} |",
            f"| Implementation | {', '.join(ref.describe() for ref in decision.prs) or DASH} |",
            f"| Catalog on {decision.catalog_ref} | {targets} |",
            f"| Release | {_release_cell(decision.release)} |",
            f"| Why not further | {'; '.join(decision.reasons) or DASH} |",
            "",
            LIFECYCLE_MARK + json.dumps(decision.payload(), sort_keys=True, separators=(",", ":")) + LIFECYCLE_MARK_END,
            LIFECYCLE_END,
        ]
    )


def _write_state(body: str, state: str) -> str | None:
    """Replace the state token inside the owned block, or ``None`` when there is none."""
    before, begin, rest = body.partition(issues.OWNED_BEGIN)
    if not begin:
        return None
    block, end, after = rest.partition(issues.OWNED_END)
    match = issues.STATE_RE.search(block)
    if match is None or not end:
        return None
    rewritten = block[: match.start()] + f"<!-- takaro-maint:state={state} -->" + block[match.end() :]
    return before + begin + rewritten + end + after


def apply(body: str, decision: Decision) -> str | None:
    """``body`` with the state token and the Lifecycle block brought up to date.

    ``None`` means the body has no owned block, no state token in it, or a Lifecycle block that
    begins and never ends — someone edited it beyond recognition, and guessing where the block
    used to stop would silently delete whatever they wrote below it.
    """
    updated = _write_state(body or "", decision.state)
    if updated is None:
        return None
    block = render_block(decision)
    before, begin, rest = updated.partition(LIFECYCLE_BEGIN)
    if not begin:
        # No block yet, or a human deleted both markers: append one fresh block, never a second.
        return before.rstrip("\n") + "\n\n" + block + "\n"
    _, end, after = rest.partition(LIFECYCLE_END)
    if not end:
        return None
    return before + block + after
