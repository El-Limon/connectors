"""One upstream fact, as the JSON document the tracker carries.

A provider builds an :class:`~takaro_maint.providers.base.Observation`; this module is the
only place that turns it into the wire document and checks it against its schema. The
document is what a maintenance issue quotes, so its shape is validated, not assumed.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from .catalog import schema
from .providers.base import Observation

SCHEMA_NAME = "observation.schema.json"

#: The alphabet `observation.schema.json` allows in a `rev` (and a `branch`). A provider
#: that builds a revision out of an upstream string has to pass it through `safe_rev`
#: first: a valid GitHub tag like `carbon@2.0` is not a valid revision, and an
#: unsanitised one would fail the whole scan rather than that one source.
REV_RE = re.compile(r"^[A-Za-z0-9._+/-]+$")


def safe_rev(text: str) -> str:
    """An upstream string as a revision: anything outside the alphabet becomes a dash.

    Runs collapse and the ends are stripped, so `carbon@2.0` is `carbon-2.0` and
    `v1.0 (final)` is `v1.0-final`. The raw upstream string stays in the facts, which is
    where anyone reading the observation looks for it.
    """
    return re.sub(r"[^A-Za-z0-9._+/-]+", "-", text).strip("-") or "unknown"


def utcnow() -> str:
    """The one clock every observation and checkpoint reads (pinned in tests)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_json(observation: Observation, *, game: str, source_id: str) -> dict[str, Any]:
    """The observation document: stable key order, no provider objects."""
    return {
        "schemaVersion": 1,
        "provider": observation.provider,
        "component": observation.component,
        "branch": observation.branch,
        "rev": observation.rev,
        "kind": observation.kind,
        "identity": observation.identity,
        "facts": observation.facts,
        "observedAt": observation.observed_at,
        "source": {"game": game, "id": source_id},
    }


def validate(document: dict[str, Any]) -> list[str]:
    """Schema errors for one observation document; empty when it validates."""
    return schema.errors_for(SCHEMA_NAME, document)
