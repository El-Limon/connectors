"""What a source has already been seen to publish.

A checkpoint is the answer to "have I filed this before?" for one watched source. It holds
the revisions seen (each with its upstream release time) and a floor: everything released
at or before the floor counts as seen even though its id was dropped to keep the dashboard
body inside GitHub's limit. Checkpoints are computed in memory during a run and committed
only through :mod:`takaro_maint.tracker.dashboard`, so a failed run advances nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

#: One ``(revision, releaseTime)`` pair, newest first inside a checkpoint.
Entry = tuple[str, str]


@dataclass(frozen=True)
class Checkpoint:
    seen: list[Entry] = field(default_factory=list)
    floor: str | None = None
    at: str = ""

    @property
    def ids(self) -> set[str]:
        return {rev for rev, _ in self.seen}

    def summary(self) -> dict[str, Any]:
        return {"seen": len(self.seen), "floor": self.floor}


def _ordered(entries: list[Entry]) -> list[Entry]:
    """Newest release first, ties broken by revision so the order is total."""
    return sorted(entries, key=lambda entry: (entry[1], entry[0]), reverse=True)


def seed(candidates: list[Entry], *, at: str) -> Checkpoint:
    """Bootstrap: everything already published counts as seen, with no floor."""
    return Checkpoint(seen=_ordered(list(candidates)), floor=None, at=at)


def is_seen(checkpoint: Checkpoint, rev: str, release_time: str) -> bool:
    """Ids first, then the floor — the floor is what survives trimming."""
    if rev in checkpoint.ids:
        return True
    return checkpoint.floor is not None and release_time <= checkpoint.floor


def advance(checkpoint: Checkpoint, entries: list[Entry], *, at: str) -> Checkpoint:
    """Add reconciled revisions. Ids already present are not duplicated."""
    known = checkpoint.ids
    merged = list(checkpoint.seen) + [entry for entry in entries if entry[0] not in known]
    return Checkpoint(seen=_ordered(merged), floor=checkpoint.floor, at=at)


def trim(checkpoint: Checkpoint, keep: int) -> Checkpoint:
    """Drop the oldest entries beyond ``keep`` and raise the floor to the newest dropped.

    Deterministic: the same checkpoint trimmed to the same size always yields the same
    floor, and every dropped revision still counts as seen because its release time is at
    or below that floor.
    """
    ordered = _ordered(list(checkpoint.seen))
    if keep < 0 or len(ordered) <= keep:
        return replace(checkpoint, seen=ordered)
    kept, dropped = ordered[:keep], ordered[keep:]
    floor = max([checkpoint.floor or ""] + [release_time for _, release_time in dropped]) or None
    return Checkpoint(seen=kept, floor=floor, at=checkpoint.at)


def to_json(checkpoint: Checkpoint) -> dict[str, Any]:
    return {
        "seen": [[rev, release_time] for rev, release_time in checkpoint.seen],
        "floor": checkpoint.floor,
        "at": checkpoint.at,
    }


def from_json(document: dict[str, Any] | None) -> Checkpoint | None:
    """``None`` means "this source was never initialised" — not "nothing seen"."""
    if not document:
        return None
    seen: list[Entry] = []
    for item in document.get("seen") or []:
        if isinstance(item, list | tuple) and len(item) == 2:
            seen.append((str(item[0]), str(item[1])))
        else:  # tolerate a hand-edited bare id: it still counts as seen
            seen.append((str(item), ""))
    floor = document.get("floor")
    return Checkpoint(seen=_ordered(seen), floor=str(floor) if floor else None, at=str(document.get("at") or ""))
