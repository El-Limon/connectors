"""Did the latest stable release actually ship this target?

"Released" is the one lifecycle state that closes an issue, so it is the one that has to be
provable from what the release itself carries rather than from what a pull request claimed.
The proof is the compatibility record beside the artifacts: it names the target, pins its
fingerprint, records the verification that was executed and lists every asset with its
sha256. This module downloads that record and ``SHA256SUMS`` — the two small files — and
checks the artifacts by name, size and digest against the release's own asset list.

The artifacts themselves are never downloaded. A release set can be hundreds of megabytes,
and GitHub already publishes each asset's size and, for anything uploaded since 2024, its
``digest``; combined with a ``SHA256SUMS`` that the record's own hash is checked against,
downloading the jars would prove nothing the asset list does not already prove.

A missing or unreadable record is not an error. It means the release predates the record (or
was assembled by hand), the issue stays where it is, and the reason says so on the issue.
"""

from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..exit_codes import MaintError
from ..publish import channels as publish_channels
from ..publish import compat_record
from ..publish.assemble import rank
from ..publish.release_client import ReleaseClient

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from .lifecycle import MainTarget

CHECKSUMS = "SHA256SUMS"


@dataclass(frozen=True)
class ReleaseFacts:
    """Everything the latest stable release of one connector says about itself."""

    tag: str
    version: str | None
    html_url: str
    published_at: str | None
    assets: dict[str, dict[str, Any]] = field(default_factory=dict)
    sums: dict[str, str] | None = None
    record: dict[str, Any] | None = None
    note: str | None = None

    def summary(self, connector: str) -> dict[str, Any]:
        return {
            "connector": connector,
            "tag": self.tag,
            "version": self.version,
            "htmlUrl": self.html_url,
            "publishedAt": self.published_at,
            "record": self.record is not None,
            "note": self.note,
        }


@dataclass(frozen=True)
class ReleaseVerdict:
    """Whether one release proves one target was published, and why not when it does not."""

    ok: bool
    reasons: list[str] = field(default_factory=list)
    release: dict[str, Any] | None = None


def _stamp(release: dict[str, Any]) -> str:
    return str(release.get("published_at") or release.get("created_at") or "")


def latest_stable(client: ReleaseClient, connector: str) -> ReleaseFacts | None:
    """The newest published, non-prerelease release tagged ``<connector>-v…``, or ``None``.

    Only the latest one is ever consulted. An older release that shipped a target the newest
    one dropped does not make that target published today, and a consumer following the
    connector's releases would not find it either.
    """
    candidates = [
        release
        for release in client.list_releases()
        if release.get("draft") is False
        and release.get("prerelease") is False
        and str(release.get("tag_name") or "").startswith(f"{connector}-v")
    ]
    if not candidates:
        return None
    # GitHub lists newest first, so an undated release keeps its listing position rather than
    # losing to one that happens to carry a timestamp.
    best = candidates[0]
    for release in candidates[1:]:
        if _stamp(release) > _stamp(best):
            best = release
    return _facts(client, best, connector)


def _facts(client: ReleaseClient, release: dict[str, Any], connector: str) -> ReleaseFacts:
    tag = str(release["tag_name"])
    version = tag.removeprefix(f"{connector}-v") or None
    assets = {str(asset["name"]): asset for asset in client.assets(int(release["id"]))}
    base = ReleaseFacts(
        tag=tag,
        version=version,
        html_url=str(release.get("html_url") or ""),
        published_at=str(release.get("published_at") or "") or None,
        assets=assets,
    )

    # The record is checked for first: a release without one cannot be checked at all, and
    # that is the more useful thing to tell a reader than which checksum file is absent.
    named = sorted(fnmatch.filter(assets, f"takaro-{connector}-*.compat.json"))
    if not named:
        return _noted(base, f"{tag} carries no compatibility record")
    if len(named) > 1:
        return _noted(base, f"{tag} carries {len(named)} compatibility records")
    if CHECKSUMS not in assets:
        return _noted(base, f"{tag} carries no {CHECKSUMS}")

    sums = publish_channels.parse_checksums(client.download(assets[CHECKSUMS]))
    payload = client.download(assets[named[0]])
    if sums.get(named[0]) != hashlib.sha256(payload).hexdigest():
        return _noted(base, f"{named[0]} does not match {CHECKSUMS}")
    try:
        record = compat_record.loads(named[0], payload)
    except MaintError as exc:
        return _noted(base, f"{named[0]} is not a valid compatibility record: {exc.message}")

    described = _describes(record, tag=tag, connector=connector, repo=client.repo)
    if described is not None:
        return _noted(base, f"{named[0]} describes {described}")
    return ReleaseFacts(
        tag=tag,
        version=str(record.get("version") or "") or version,
        html_url=base.html_url,
        published_at=base.published_at,
        assets=assets,
        sums=sums,
        record=record,
    )


def _noted(base: ReleaseFacts, note: str) -> ReleaseFacts:
    return ReleaseFacts(
        tag=base.tag,
        version=base.version,
        html_url=base.html_url,
        published_at=base.published_at,
        assets=base.assets,
        note=note,
    )


def _describes(record: dict[str, Any], *, tag: str, connector: str, repo: str) -> str | None:
    """How the record disagrees with the release it is an asset of, or ``None``."""
    source_repo = str((record.get("source") or {}).get("repo") or "")
    for label, found, wanted in (
        ("tag", str(record.get("tag") or ""), tag),
        ("connector", str(record.get("connector") or ""), connector),
        ("repo", source_repo, repo),
    ):
        if found != wanted:
            return f"{label} {found or '(none)'}, not {wanted}"
    return None


def verdict(facts: ReleaseFacts | None, target: MainTarget, *, connector: str) -> ReleaseVerdict:
    """Whether ``facts`` proves ``target`` is published, with a named reason per mismatch."""
    if facts is None:
        return ReleaseVerdict(False, [f"no stable release for {connector} yet"])
    if facts.note is not None:
        return ReleaseVerdict(False, [facts.note])

    record = facts.record or {}
    tag = facts.tag
    entry = (record.get("targets") or {}).get(target.id)
    if entry is None:
        return ReleaseVerdict(False, [f"{tag} does not list {target.id}"])

    reasons: list[str] = []
    shipped = str(entry.get("fingerprint") or "")
    if shipped != target.fingerprint:
        reasons.append(f"{tag} ships {target.id} with fingerprint {shipped[:16]}, {target.ref} has {target.fp16}")
    if str(entry.get("status") or "") != "maintained":
        reasons.append(f"{tag} ships {target.id} as candidate; promote it to maintained")

    verification = entry.get("verification") or {}
    executed = str(verification.get("executed") or "none")
    required = str((target.record.get("verification") or {}).get("required") or "none")
    if verification.get("outcome") != "pass" or rank(executed) < rank(required):
        reasons.append(f"{target.id} verified only to {executed}, requires {required}")

    for artifact in entry.get("artifacts") or []:
        reasons += _artifact_reasons(facts, str(artifact["name"]), str(artifact["sha256"]), int(artifact["size"]))
    report = verification.get("report")
    if report:
        reasons += _report_reasons(facts, str(report))

    return ReleaseVerdict(
        not reasons,
        reasons,
        {
            "tag": tag,
            "version": facts.version,
            "htmlUrl": facts.html_url,
            "artifacts": [{"name": str(a["name"]), "sha256": str(a["sha256"])} for a in entry.get("artifacts") or []],
            "executed": executed,
            "takaro": verification.get("takaro"),
        },
    )


def _artifact_reasons(facts: ReleaseFacts, name: str, sha256: str, size: int) -> list[str]:
    """One artifact the record describes, checked without downloading a byte of it."""
    asset = facts.assets.get(name)
    if asset is None:
        return [f"asset missing: {name}"]
    reasons: list[str] = []
    if (facts.sums or {}).get(name) != sha256:
        reasons.append(f"{CHECKSUMS} disagrees for {name}")
    if int(asset.get("size") or -1) != size:
        reasons.append(f"size differs for {name}")
    digest = str(asset.get("digest") or "")
    if digest and digest != f"sha256:{sha256}":
        reasons.append(f"GitHub digest differs for {name}")
    return reasons


def _report_reasons(facts: ReleaseFacts, name: str) -> list[str]:
    """The verification report named by the record.

    The record does not carry the report's own hash, so ``SHA256SUMS`` and GitHub's digest
    are the whole check: the report has to be there, listed, and listed as what was uploaded.
    """
    asset = facts.assets.get(name)
    if asset is None:
        return [f"asset missing: {name}"]
    listed = (facts.sums or {}).get(name)
    digest = str(asset.get("digest") or "")
    if listed is None or (digest and digest != f"sha256:{listed}"):
        return [f"{CHECKSUMS} disagrees for {name}"]
    return []
