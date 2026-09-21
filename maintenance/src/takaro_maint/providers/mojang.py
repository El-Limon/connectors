"""Mojang piston: the version manifest and the dedicated server jar it names."""

from __future__ import annotations

import json
import tempfile
import urllib.parse
from dataclasses import replace
from pathlib import Path
from typing import Any

from .. import net, observations, paths
from ..exit_codes import IntegrityError, UpstreamUnavailable
from ..tracker import identity
from .base import Observation, Provider, ProviderResult


def _channels(watch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The channels this provider understands, skipping disabled and unusable ones.

    A channel it cannot interpret is ignored rather than guessed at, so a later issue can
    add a snapshot channel (or a channel with `"enabled": false`) without this code moving.
    """
    channels: dict[str, dict[str, Any]] = {}
    for key, channel in (watch.get("channels") or {}).items():
        if not isinstance(channel, dict) or not channel.get("enabled", True):
            continue
        types = {str(item) for item in channel.get("types") or []}
        if not types:
            continue
        channels[str(key)] = {"branch": str(channel.get("branch") or key), "types": types}
    return channels


class MojangProvider(Provider):
    id = "mojang"

    def fetch_input(
        self,
        input_spec: dict[str, Any],
        source: dict[str, Any],
        dest: Path,
        cache: Path,
    ) -> Path:
        """Fetch the server jar, having confirmed the manifest that vouches for it."""
        if input_spec["kind"] != "mojang-version":
            return super().fetch_input(input_spec, source, dest, cache)
        manifest_url = source["manifestUrl"]
        manifest_blob = net.download(
            manifest_url,
            net.Expectation(sha1=input_spec["manifest"]["sha1"]),
            cache,
        )
        manifest = json.loads(manifest_blob.read_text(encoding="utf-8"))
        declared = manifest["downloads"]["server"]
        expected = input_spec["server"]
        if declared["sha1"] != expected["sha1"] or int(declared["size"]) != int(expected["size"]):
            raise IntegrityError(
                f"{manifest_url}: manifest names server sha1 {declared['sha1']} size {declared['size']}, "
                f"the catalog pins sha1 {expected['sha1']} size {expected['size']}",
                url=manifest_url,
            )
        blob = net.download(
            source["serverUrl"],
            net.Expectation(sha1=expected["sha1"], size=int(expected["size"])),
            cache,
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob.read_bytes())
        return dest

    def observe(self, source: dict[str, Any]) -> ProviderResult:
        """Every release the manifest lists, as lightweight observations.

        ``source`` is the game record's source mapping enriched by the scan with ``id``,
        ``game`` and ``checkpoint`` (see ``maintenance/docs/discovery.md``). Mojang
        publishes its whole history in one document, so this reports ``history: "full"``
        and leaves the filtering to the caller's checkpoint; no per-version JSON is
        fetched here. ``latest`` is used for nothing but reporting the heads.
        """
        watch = source.get("watch") or {}
        base_url = str(source["baseUrl"]).rstrip("/")
        manifest_url = base_url + str(watch["manifestPath"])
        with tempfile.TemporaryDirectory(prefix="takaro-maint-observe-") as tmp:
            destination = Path(tmp) / "version_manifest.json"
            net.fetch(manifest_url, destination, net.Expectation(), no_cache=True)
            text = destination.read_text(encoding="utf-8")
        try:
            manifest = json.loads(text)
        except json.JSONDecodeError as exc:
            raise UpstreamUnavailable(f"{manifest_url}: manifest is not JSON ({exc})", url=manifest_url) from exc

        channels = _channels(watch)
        accepted = {
            version_type: channel["branch"] for channel in channels.values() for version_type in channel["types"]
        }
        latest = manifest.get("latest") or {}
        heads = {
            channel["branch"]: str(latest[key]) for key, channel in channels.items() if latest.get(key) is not None
        }

        component = str(watch.get("component") or source.get("game") or "")
        kind = str(watch.get("kind") or "game")
        observed_at = observations.utcnow()
        seen: list[Observation] = []
        for entry in manifest.get("versions") or []:
            branch = accepted.get(str(entry.get("type")))
            if branch is None:
                continue
            rev = str(entry["id"])
            seen.append(
                Observation(
                    provider=self.id,
                    component=component,
                    branch=branch,
                    rev=rev,
                    kind=kind,
                    identity=identity.canonical(self.id, component, branch, rev),
                    facts={
                        "id": rev,
                        "type": str(entry["type"]),
                        # 'time' is a last-modified stamp and moves when Mojang republishes;
                        # ordering and floors always use 'releaseTime'.
                        "releaseTime": str(entry["releaseTime"]),
                        "manifestList": {"url": manifest_url},
                        "manifest": {
                            "url": base_url + urllib.parse.urlsplit(str(entry["url"])).path,
                            "sha1": str(entry["sha1"]),
                        },
                    },
                    observed_at=observed_at,
                )
            )
        return ProviderResult(
            source_id=str(source.get("id") or self.id),
            status="ok",
            heads=heads,
            observations=seen,
            history="full",
        )

    def enrich(self, observation: Observation, source: dict[str, Any]) -> Observation:
        """Add the server jar and the Java major version from the per-version JSON.

        Only the URL path of the manifest entry is used, joined to the source's
        ``baseUrl``, so a test upstream can serve it. The entry's sha1 is enforced: a
        mismatch is an integrity failure, never a silently different manifest.
        """
        manifest = dict(observation.facts.get("manifest") or {})
        url = str(manifest["url"])
        blob = net.download(url, net.Expectation(sha1=str(manifest["sha1"])), paths.cache_dir())
        try:
            document = json.loads(blob.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise UpstreamUnavailable(f"{url}: version metadata is not JSON ({exc})", url=url) from exc
        try:
            server = document["downloads"]["server"]
            facts = {
                **observation.facts,
                "server": {"url": str(server["url"]), "sha1": str(server["sha1"]), "size": int(server["size"])},
                "javaMajor": int(document["javaVersion"]["majorVersion"]),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise UpstreamUnavailable(
                f"{url}: version metadata names no dedicated server jar ({exc!r})", url=url
            ) from exc
        return replace(observation, facts=facts)


PROVIDER = MojangProvider()
