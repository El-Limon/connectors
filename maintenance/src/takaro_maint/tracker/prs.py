"""The pull requests that reference a maintenance issue.

Found by listing, never by search. GitHub's issue search lags behind by seconds to minutes
and ignores punctuation, so ``Refs #12`` and ``refs 12`` are the same query to it — which is
exactly the distinction this module exists to make. One paginated ``state=all`` listing per
run is cheap, current and exact, and a pull request opened moments before a run that the
listing has not caught up with is simply seen by the next run: the state is recomputed from
scratch every time, so nothing is lost by being late.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..github import GitHub

#: ``Refs #12``, ``refs: #12``, ``Closes #12`` — and never ``#12`` on its own, because a bare
#: number in prose is a mention, not a claim that this pull request implements the issue.
REFERENCE_RE = re.compile(
    r"\b(?P<kw>refs?|references?|closes?|closed|fix(?:es|ed)?|resolves?|resolved)\s*:?\s+#(?P<n>\d+)\b",
    re.IGNORECASE,
)

#: Keywords GitHub itself acts on: a merge closes the issue whatever this tool thinks.
CLOSING_KEYWORDS = frozenset({"close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves", "resolved"})


@dataclass(frozen=True)
class PullRef:
    """One pull request, as far as one maintenance issue is concerned."""

    number: int
    state: str
    merged_at: str | None
    base_ref: str
    html_url: str
    keyword: str
    draft: bool = False

    @property
    def merged(self) -> bool:
        return bool(self.merged_at)

    @property
    def counts(self) -> bool:
        """Open or merged. A pull request closed without merging implemented nothing."""
        return self.state == "open" or self.merged

    @property
    def closes_on_merge(self) -> bool:
        return self.keyword.lower() in CLOSING_KEYWORDS

    def describe(self) -> str:
        if self.merged:
            return f"#{self.number} (merged into {self.base_ref} at {self.merged_at})"
        return f"#{self.number} ({self.state})"


def list_pulls(client: GitHub) -> list[dict[str, Any]]:
    """Every pull request of the repository, open, closed and merged alike."""
    return [item for item in client.paginate(f"/repos/{client.repo}/pulls?state=all&per_page=100") if item]


def _pull_ref(pull: dict[str, Any], keyword: str) -> PullRef:
    base = pull.get("base") or {}
    return PullRef(
        number=int(pull["number"]),
        state=str(pull.get("state") or "open"),
        merged_at=str(pull["merged_at"]) if pull.get("merged_at") else None,
        base_ref=str(base.get("ref") or ""),
        html_url=str(pull.get("html_url") or ""),
        keyword=keyword,
        draft=bool(pull.get("draft")),
    )


def references(pulls: list[dict[str, Any]]) -> dict[int, list[PullRef]]:
    """``issue number -> the pull requests that name it``, sorted by pull request number.

    A pull request that names the same issue twice is one reference. When one of those
    mentions uses a closing keyword that is the keyword recorded, because that is the one
    GitHub will act on and therefore the one worth warning about.
    """
    found: dict[int, list[PullRef]] = {}
    for pull in sorted(pulls, key=lambda item: int(item.get("number") or 0)):
        text = f"{pull.get('title') or ''}\n{pull.get('body') or ''}"
        keywords: dict[int, str] = {}
        for match in REFERENCE_RE.finditer(text):
            number = int(match.group("n"))
            keyword = match.group("kw")
            held = keywords.get(number)
            if held is None or (keyword.lower() in CLOSING_KEYWORDS and held.lower() not in CLOSING_KEYWORDS):
                keywords[number] = keyword
        for number, keyword in keywords.items():
            found.setdefault(number, []).append(_pull_ref(pull, keyword))
    return found


def warnings_for(issue: int, refs: list[PullRef]) -> list[str]:
    """What a maintainer needs to be told about how these pull requests are written."""
    return [
        f"PR #{ref.number} will close #{issue} on merge; maintenance PRs should use Refs #{issue}"
        for ref in refs
        if ref.closes_on_merge and ref.state == "open"
    ]
