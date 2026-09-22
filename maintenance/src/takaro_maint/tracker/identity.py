"""Deterministic identity for the work the scan files.

One observation is one issue, forever, identified by an HTML-comment marker on the first
line of the body. GitHub's issue search ignores punctuation, so it can only ever be a
pre-filter: the marker is parsed exactly, and a paginated listing is the fallback.
"""

from __future__ import annotations

import re
from typing import Any

MARKER_PREFIX = "<!-- takaro-maint: "
MARKER_SUFFIX = " -->"

#: Keys in the order a marker renders them; anything else is appended, sorted.
KEY_ORDER = ("kind", "provider", "component", "app", "branch", "buildid", "rev")

#: The label every issue the scan files carries. It must already exist in the repository:
#: the scan never creates labels.
LABEL = "connector-maintenance"

DASHBOARD_MARKER: dict[str, str] = {"kind": "dashboard", "v": "1"}

# A value is a revision, a branch or a small word: no whitespace, no quoting, no markup.
_TOKEN = re.compile(r"([a-z][a-z0-9_]*)=([A-Za-z0-9._+/-]+)")


def canonical(provider: str, component: str, branch: str, rev: str) -> str:
    """The work identity a dashboard entry is keyed by (no ``kind``: that is the issue's)."""
    return f"provider={provider} component={component} branch={branch} rev={rev}"


def render_marker(fields: dict[str, str]) -> str:
    """The marker line for ``fields``, always in the same order for the same dict."""
    known = [key for key in KEY_ORDER if key in fields]
    extra = sorted(key for key in fields if key not in KEY_ORDER)
    return MARKER_PREFIX + " ".join(f"{key}={fields[key]}" for key in known + extra) + MARKER_SUFFIX


def parse_marker(body: str) -> dict[str, str] | None:
    """The marker on the first non-empty line of ``body``, or ``None``.

    Only the first line counts, so a marker quoted further down (in a comment, or in a
    paste of another issue) can never claim an identity. Token order does not matter: the
    caller compares dicts, which is why a human reordering the marker is harmless.
    """
    for line in (body or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not (stripped.startswith(MARKER_PREFIX) and stripped.endswith(MARKER_SUFFIX)):
            return None
        inner = stripped[len(MARKER_PREFIX) : -len(MARKER_SUFFIX)].strip()
        fields: dict[str, str] = {}
        for token in inner.split():
            match = _TOKEN.fullmatch(token)
            if match is None:
                return None
            fields[match.group(1)] = match.group(2)
        return fields or None
    return None


def support_marker(observation: Any) -> dict[str, str]:
    """The marker identifying the support issue for one observation."""
    marker = {
        "kind": "support",
        "provider": observation.provider,
        "component": observation.component,
        "branch": observation.branch,
        "rev": observation.rev,
    }
    # A Steam revision also contains the manifest-set digest and branch. Keep that rich
    # revision as the issue identity, while carrying the two catalog join keys explicitly
    # so lifecycle reconciliation never has to mistake it for the bare build id.
    if observation.provider == "steam":
        for key in ("app", "buildid"):
            value = observation.facts.get(key)
            if value is not None:
                marker[key] = str(value)
    return marker


def search_terms(marker: dict[str, str]) -> str:
    """The search pre-filter for ``marker``.

    The last token is what a substring search can actually match — the plain revision for
    a support issue, ``kind=dashboard`` for the dashboard. Search over-matching is fine;
    :func:`parse_marker` decides.
    """
    tail = marker["rev"] if "rev" in marker else f"kind={marker['kind']}"
    return f'label:{LABEL} "takaro-maint" {tail}'
