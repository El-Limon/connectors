"""Providers fetch a target's inputs and (later) observe upstream for new ones."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Observation:
    """One upstream fact a provider saw. Consumed by the release-tracking issues."""

    provider: str
    component: str
    branch: str
    rev: str
    kind: str  # game | framework | branch-review
    identity: str
    facts: dict[str, Any] = field(default_factory=dict)
    observed_at: str = ""


@dataclass(frozen=True)
class ProviderResult:
    source_id: str
    status: str  # ok | failed
    error: str | None = None
    observations: list[Observation] = field(default_factory=list)
    heads: dict[str, str] = field(default_factory=dict)
    history: str = "heads-only"  # full | heads-only


class Provider:
    """Base class: fetching is implemented here, observing arrives with the tracker issues."""

    id: str = "base"

    def fetch_input(
        self,
        input_spec: dict[str, Any],
        source: dict[str, Any],
        dest: Path,
        cache: Path,
    ) -> Path:
        raise NotImplementedError(f"provider '{self.id}' cannot fetch {input_spec.get('kind')} inputs")

    def observe(self, source: dict[str, Any]) -> ProviderResult:
        raise NotImplementedError(f"provider '{self.id}' does not observe upstream yet (release tracking: #153/#154)")
