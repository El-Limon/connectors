"""Providers fetch a target's inputs and (later) observe upstream for new ones."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from ..catalog.loader import Target


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

    def input_url(self, input_spec: dict[str, Any], source: dict[str, Any]) -> str | None:
        """The URL this input names, for the inputs that are not one plain download.

        Inputs that carry a ``path`` never reach here. A provider whose inputs name a set
        of files rather than a single URL answers with the identifier that pins that set,
        so reports and compat records can record what was installed; ``None`` means the
        mechanism has no URL to record.
        """
        del input_spec, source
        return None

    def observe(self, source: dict[str, Any]) -> ProviderResult:
        raise NotImplementedError(f"provider '{self.id}' does not observe upstream yet (release tracking: #153/#154)")

    def presentation(self, observation: Observation, game_name: str) -> dict[str, Any] | None:
        """How this provider's ``kind=game`` observations read as a maintenance issue.

        The tracker renders a generic issue that names no upstream mechanism: what was
        seen, which targets it affects, and the catalog/build/verify steps that turn it
        into a target. A provider whose upstream says more than that answers here, and
        every key is optional:

        ``title``
            the issue title, when "<game> <rev>: new stable release needs a target" is wrong.
        ``intro``
            the prose above the owned block. Written once, when the issue is filed, and
            never rewritten afterwards.
        ``observationRows``
            the body rows of the Observation table, usually
            ``tracker.issues.observation_rows(observation, [...])`` with the provider's own
            rows spliced in.
        ``readinessLines``
            what the Readiness section says while no framework has been observed.
        ``nextSteps``
            the numbered reproduction steps, usually
            ``tracker.issues.next_steps(observation, pin=...)``.

        ``None`` (the default) and an absent key both mean "the generic rendering is
        right", so no provider has to restate what it does not change.
        """
        del observation, game_name
        return None

    def covers(self, observation: Observation, targets: list[Target]) -> bool | None:
        """Whether one of ``targets`` already ships exactly what ``observation`` saw.

        Asked once per head while a source is being bootstrapped, which is the one moment
        the scan decides between "already shipped, record it as seen" and "file an issue".
        ``None`` (the default) means the provider has no opinion and the caller compares
        the observation's revision with each target's ``revision`` — right whenever a
        target is named by the same string the provider observes. A provider whose
        upstream identity is richer than that string (an app on a branch at a build id,
        say) reads the targets' inputs here and answers for itself.
        """
        del observation, targets
        return None
