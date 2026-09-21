"""Thunderstore packages, as a provider any game can point at without new Python.

Several connectors are loaded by a modding framework that ships as a Thunderstore package
rather than as a GitHub release — BepInExPack for Valheim is the first of them — and each
of them would otherwise arrive with its own little fetcher. This provider is the shared
one: a game declares a source and a watch block in its catalog record and gets acquisition
(``fetch_input``), addressing (``input_url``) and discovery (``observe``) from here.

Two things about Thunderstore shape what this file can promise.

*The zip carries no published digest.* The package index publishes a download URL, a
version number and a timestamp, and nothing else about the bytes. So a pinned package's
``sha256`` is always ``self-recorded``: somebody downloaded it twice, watched the two
downloads agree and wrote the hash into the target record. ``fetch_input`` then checks
every later download against that record, which is the guarantee that matters.

*The API exposes only the current head.* ``/api/experimental/package/<ns>/<name>/``
answers with the package plus its ``latest`` version — versions published and superseded
between two scans are simply not in the document. That is why ``observe`` reports
``history="heads-only"`` and attaches ``OBSERVATION_LIMIT`` to every observation: a scan
here can say "the head is 5.4.2350 now", never "these are all the versions since".

``latest`` is therefore a *channel key* — the moving pointer a scan reads — and never a
coordinate: a pinned input names an exact ``version``, and the download URL this provider
builds always carries that version.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import channels, net, observations, readiness
from ..exit_codes import UpstreamUnavailable, UsageError
from ..tracker import identity
from .base import Observation, Provider, ProviderResult

#: Thunderstore package versions are strict three-part numbers; the schema pins the same
#: grammar for ``inputs.<name>.version``, so an observation that does not match it cannot
#: become a target and is reported as an unusable upstream rather than filed.
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")

#: The one channel key this provider understands. Thunderstore has no branches: a package
#: has one published head, and the experimental endpoint calls it ``latest``.
LATEST = "latest"

OBSERVATION_LIMIT = (
    "heads-only: the Thunderstore package endpoint exposes only the package's latest "
    "version, so versions published between two scans are not observed and are not claimed."
)


class ThunderstoreProvider(Provider):
    id = "thunderstore"

    # -- acquisition -----------------------------------------------------------
    def _download_url(self, input_spec: dict[str, Any], source: dict[str, Any]) -> str:
        base = str(source["baseUrl"]).rstrip("/")
        return f"{base}/package/download/{input_spec['namespace']}/{input_spec['name']}/{input_spec['version']}/"

    def fetch_input(
        self,
        input_spec: dict[str, Any],
        source: dict[str, Any],
        dest: Path,
        cache: Path,
    ) -> Path:
        if input_spec.get("kind") != "thunderstore-package":
            return super().fetch_input(input_spec, source, dest, cache)
        url = self._download_url(input_spec, source)
        expect = net.Expectation(sha256=input_spec.get("sha256"), size=input_spec.get("size"))
        blob = net.download(url, expect, cache)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob.read_bytes())
        return dest

    def input_url(self, input_spec: dict[str, Any], source: dict[str, Any]) -> str | None:
        if input_spec.get("kind") != "thunderstore-package":
            return None
        return str(input_spec.get("resolvedCoordinate") or self._download_url(input_spec, source))

    # -- discovery -------------------------------------------------------------
    def _document(self, source: dict[str, Any], watch: dict[str, Any]) -> tuple[dict[str, Any], str]:
        base = str(source["baseUrl"]).rstrip("/")
        url = f"{base}/api/experimental/package/{watch['namespace']}/{watch['name']}/"
        document = readiness.fetch_json(url, what="the Thunderstore package")
        if not isinstance(document, dict):
            raise UpstreamUnavailable(f"{url}: the Thunderstore package is not an object", url=url)
        return document, url

    def observe(self, source: dict[str, Any]) -> ProviderResult:
        """One observation for the package's current head, per enabled channel."""
        watch = source.get("watch") or {}
        for key in ("component", "namespace", "name"):
            if not watch.get(key):
                raise UsageError(f"the '{self.id}' watch block of source '{source.get('id')}' declares no {key!r}")

        enabled = channels.enabled_channels(watch)
        unknown = sorted(key for key in enabled if key != LATEST)
        if unknown:
            raise UsageError(
                f"the '{self.id}' watch block of source '{source.get('id')}' declares channel(s) "
                f"{unknown}; a Thunderstore package has one published head, watched as '{LATEST}'"
            )

        document, url = self._document(source, watch)
        latest = document.get("latest")
        if not isinstance(latest, dict):
            raise UpstreamUnavailable(f"{url}: the package document carries no 'latest' version", url=url)
        rev = str(latest.get("version_number") or "")
        if not VERSION_RE.match(rev):
            raise UpstreamUnavailable(
                f"{url}: the latest version is {rev!r}, which is not a Thunderstore <major>.<minor>.<patch>",
                url=url,
            )

        component = str(watch["component"])
        kind = str(watch.get("kind") or "framework")
        checkpoint = source.get("checkpoint")
        now = observations.utcnow()
        namespace, name = str(watch["namespace"]), str(watch["name"])

        seen: list[Observation] = []
        heads: dict[str, str] = {}
        for key, channel in sorted(enabled.items()):
            branch = str(channel["branch"])
            facts: dict[str, Any] = {
                # Thunderstore publishes no digest for the zip, so the artifact is named
                # and addressed here and hashed by whoever pins it.
                "artifact": readiness.artifact(
                    f"{namespace}-{name}-{rev}.zip",
                    str(latest.get("download_url") or ""),
                    sha256=None,
                    size=None,
                ),
                "releaseTime": str(latest.get("date_created") or "") or channels.first_seen(checkpoint, rev, now),
                "dateUpdated": str(document.get("date_updated") or ""),
                "package": f"{namespace}/{name}",
                "listing": {"url": url},
                "channel": key,
                "observationLimit": OBSERVATION_LIMIT,
            }
            seen.append(
                Observation(
                    provider=self.id,
                    component=component,
                    branch=branch,
                    rev=rev,
                    kind=kind,
                    identity=identity.canonical(self.id, component, branch, rev),
                    facts=facts,
                    observed_at=now,
                )
            )
            heads.setdefault(branch, rev)

        result = ProviderResult(
            source_id=str(source.get("id") or self.id),
            status="ok",
            heads=heads,
            observations=seen,
            history="heads-only",
        )
        if kind == "framework":
            readiness.registry().record(watch=watch, result=result)
        return result


PROVIDER = ThunderstoreProvider()
