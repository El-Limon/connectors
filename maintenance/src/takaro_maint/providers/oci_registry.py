"""An OCI registry, as a provider any game can point at without new Python.

Several connectors run against a server that upstream publishes as a container image
rather than as a download: the image *is* the release. This provider observes such a
repository — its tag list and, for each watched tag, the manifest digest the registry
currently serves — so a catalog can pin the image by digest and a scan can say whether
the image a new game release needs has been published yet.

What it reads
-------------
A **tag** is a mutable label; a **digest** is the content address of the manifest bytes
the registry served for it. The digest is computed here, from the body, and compared with
the ``Docker-Content-Digest`` header when the registry sends one: a disagreement means
something between the registry and this process rewrote the document, which fails the
source rather than being reconciled. An image **index** (a multi-platform manifest list)
is resolved down to the one entry whose ``platform`` matches the watch, so a row never
claims an artifact for an architecture nobody runs.

Access
------
Public repositories on ghcr.io and Docker Hub answer anonymously, but only after a bearer
challenge: the first ``GET`` comes back ``401`` with a ``WWW-Authenticate: Bearer
realm=…,service=…,scope=…`` header, the realm is asked for a token, and the request is
retried once with it. The token is a credential for the rest of the process and is never
written to stdout, stderr, a fact or a log line.

Limits this provider states rather than papers over
---------------------------------------------------
* **Tags are mutable.** A tag is observed *at the digest it currently resolves to*, and
  the revision folds that digest in (``6.1.0.911459f0``), so re-pushing a tag is a new
  revision rather than a silent replacement. What was published under that tag between
  two scans was never seen, so ``history`` is ``heads-only`` and the replaced digest is
  not claimed.
* **Images carry no game version.** Nothing in a manifest says which upstream release an
  image was built from, so the watch's channel declares ``gameRevision`` — a template over
  the tag (``"v{tag}"``) naming the release the game watch observes. Without it
  ``facts.gameVersion`` is absent and the readiness table cannot join this row to a game
  issue; that is reported as a missing row, never guessed at.
* **Floating tags are never observed.** ``latest``, ``stable``, ``6`` and ``6.1`` are tags
  like any other, and a channel's ``tag`` selector is what keeps them out: they match no
  channel, so no manifest is ever fetched for them and nothing is ever pinned to one.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import urllib.error
from dataclasses import replace
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse, urlsplit

from .. import channels, net, observations, readiness
from ..exit_codes import UpstreamUnavailable, UsageError
from ..tracker import identity
from .base import Observation, Provider, ProviderResult

#: What a manifest request accepts: both OCI and Docker media types, index before manifest,
#: so a multi-platform repository answers with the index rather than one architecture.
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

OBSERVATION_LIMIT = (
    "heads-only: a registry tag is mutable, so it is observed at the digest it resolves to "
    "right now and the revision carries that digest; an image pushed over the same tag "
    "between two scans was never seen and is not claimed."
)

#: ``owner/name``, the only repository shape a registry path accepts here.
_REPOSITORY_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+$")

_CHALLENGE_RE = re.compile(r'(?P<key>[a-z_]+)="(?P<value>[^"]*)"')
_LINK_RE = re.compile(r'<(?P<url>[^>]+)>\s*;\s*rel="next"')


def _compile(pattern: str, where: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise UsageError(f"{where} is not a valid regular expression: {pattern!r} ({exc})") from exc


def _selector(pattern: str, where: str) -> re.Pattern[str]:
    """``regex:<pattern>`` or an exact string — the grammar the other watches already use."""
    if pattern.startswith("regex:"):
        return _compile(pattern[len("regex:") :], where)
    return re.compile(f"^{re.escape(pattern)}$")


def _sort_key(tag: str) -> list[tuple[int, int, str]]:
    """Numeric-aware ordering of a dotted/dashed tag, newest last.

    ``6.1.0`` sorts after ``6.0.0`` and after ``6.0.0-pre3`` because a numeric part
    compares as a number and a word compares *before* every number of the same position:
    a suffix is a prerelease of the version it hangs off, so ``6.1.0`` has to beat
    ``6.1.0-pre3`` while ``6.1.0.1`` still beats ``6.1.0``. A terminator closes every key
    so the shorter of two otherwise-equal tags is the smaller one. Nothing here decides
    that one *release* is newer than another — only that, inside one channel whose tags
    share a grammar, this is the order the registry's own version numbers give.
    """
    key: list[tuple[int, int, str]] = []
    for part in re.split(r"[.-]", tag):
        # Each run of digits is its own number: "pre10" has to sort after "pre9", which it
        # does not while the whole word is compared as a string.
        for token in re.findall(r"\d+|\D+", part):
            if token.isdigit():
                key.append((2, int(token), ""))
            else:
                key.append((0, 0, token))
    key.append((1, 0, ""))
    return key


def _refuse_a_hostile_realm(realm: str) -> None:
    """A token realm has to be an https URL to a named host, or it is not followed at all."""
    parsed = urlparse(realm)
    host = parsed.hostname or ""
    literal = True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        literal = False
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or host == "localhost" or literal:
        raise UpstreamUnavailable(f"the registry's bearer realm {realm!r} is not an https URL to a named host")


class OciRegistryProvider(Provider):
    id = "oci-registry"

    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}

    # -- HTTP ------------------------------------------------------------------
    def _open(self, url: str, headers: dict[str, str]) -> tuple[bytes, Any]:
        response = net.get_transport().open(url, headers)
        try:
            body = response.read()
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()
        return body, getattr(response, "headers", None)

    def _challenge(self, exc: urllib.error.HTTPError) -> dict[str, str] | None:
        """The bearer challenge a 401 carries, or ``None`` when it carries none."""
        headers = getattr(exc, "headers", None)
        header = headers.get("WWW-Authenticate") if headers is not None else None
        if not header or not str(header).lower().startswith("bearer "):
            return None
        return {match.group("key"): match.group("value") for match in _CHALLENGE_RE.finditer(str(header))}

    def _authorize(self, challenge: dict[str, str]) -> str:
        """Exchange an anonymous bearer challenge for a token. The token is never printed.

        The realm comes from a ``WWW-Authenticate`` header, which is to say from whoever
        answered the request -- so it is a URL an attacker controls. Only an HTTPS URL to
        a named public host is followed, keeping local files and cloud metadata services
        out of reach; challenge values are encoded before entering its query string.
        """
        realm = challenge.get("realm")
        if not realm:
            raise UpstreamUnavailable("the registry's bearer challenge names no realm")
        _refuse_a_hostile_realm(realm)
        query = urlencode(
            sorted((key, value) for key, value in challenge.items() if key != "realm" and value), safe=":/"
        )
        url = f"{realm}?{query}" if query else realm
        try:
            body, _ = self._open(url, {"Accept": "application/json"})
        except (urllib.error.URLError, OSError) as exc:
            raise UpstreamUnavailable(f"{realm} refused an anonymous token ({exc})", url=realm) from exc
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpstreamUnavailable(f"{realm}: the token response is not JSON ({exc})", url=realm) from exc
        token = document.get("token") or document.get("access_token")
        if not token:
            raise UpstreamUnavailable(f"{realm}: the token response carries no token", url=realm)
        return str(token)

    def _get(self, url: str, accept: str) -> tuple[bytes, Any]:
        """One registry GET, answering a bearer challenge once. Never falls back to another URL."""
        host = urlparse(url).netloc
        for attempt in (1, 2):
            headers = {"Accept": accept}
            token = self._tokens.get(host)
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                return self._open(url, headers)
            except urllib.error.HTTPError as exc:
                challenge = self._challenge(exc) if exc.code == 401 and attempt == 1 else None
                if challenge is None:
                    raise UpstreamUnavailable(f"{url} returned HTTP {exc.code}", url=url, status=exc.code) from exc
                self._tokens[host] = self._authorize(challenge)
            except urllib.error.URLError as exc:
                raise UpstreamUnavailable(f"{url} is unreachable: {exc.reason}", url=url) from exc
            except OSError as exc:
                raise UpstreamUnavailable(f"{url} is unreachable: {exc}", url=url) from exc
        raise UpstreamUnavailable(f"{url} kept answering the bearer challenge", url=url)

    def _get_json(self, url: str, accept: str) -> tuple[Any, Any, bytes]:
        body, headers = self._get(url, accept)
        try:
            return json.loads(body.decode("utf-8")), headers, body
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpstreamUnavailable(
                f"{url}: the registry answered with something that is not JSON ({exc})", url=url
            ) from exc

    # -- registry endpoints ----------------------------------------------------
    def _tags(self, base: str, repository: str) -> list[str]:
        """Every tag the repository lists, following ``Link: rel="next"`` while there is one."""
        url = f"{base}/v2/{repository}/tags/list?n=1000"
        tags: list[str] = []
        seen_urls: set[str] = set()
        while url and url not in seen_urls:
            seen_urls.add(url)
            document, headers, _ = self._get_json(url, "application/json")
            if not isinstance(document, dict) or not isinstance(document.get("tags"), list):
                raise UpstreamUnavailable(f"{url}: the tag listing has no 'tags' array", url=url)
            tags += [str(tag) for tag in document["tags"]]
            link = headers.get("Link") if headers is not None else None
            match = _LINK_RE.search(str(link)) if link else None
            next_url = urljoin(base + "/", match.group("url")) if match else ""
            if next_url:
                origin = urlsplit(base)
                destination = urlsplit(next_url)
                if destination.scheme != origin.scheme or destination.netloc != origin.netloc:
                    raise UpstreamUnavailable(
                        f"{url}: the tag listing's next link leaves {base} for {next_url}", url=url
                    )
            url = next_url
        return tags

    def _manifest(self, base: str, repository: str, reference: str) -> tuple[dict[str, Any], str, bytes, str]:
        """``(document, digest, body, url)`` for one tag or digest reference."""
        url = f"{base}/v2/{repository}/manifests/{reference}"
        document, headers, body = self._get_json(url, MANIFEST_ACCEPT)
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        published = headers.get("Docker-Content-Digest") if headers is not None else None
        if published and str(published).strip() != digest:
            raise UpstreamUnavailable(
                f"{url}: digest header disagrees with the manifest body "
                f"(header {str(published).strip()}, body {digest})",
                url=url,
            )
        # Asking by digest is the whole point of a pinned observation, so the bytes that
        # come back have to be the bytes that digest names -- a header is not evidence.
        if reference.startswith("sha256:") and reference != digest:
            raise UpstreamUnavailable(
                f"{url}: the body does not hash to the digest it was fetched by (asked for {reference}, got {digest})",
                url=url,
            )
        if not isinstance(document, dict):
            raise UpstreamUnavailable(f"{url}: the manifest is not an object", url=url)
        return document, digest, body, url

    def _platform_digest(self, document: dict[str, Any], digest: str, want: dict[str, str], url: str) -> str:
        """The one entry of an index that matches the watched platform; the digest itself otherwise."""
        entries = document.get("manifests")
        if not isinstance(entries, list):
            return digest
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            platform = entry.get("platform") or {}
            if all(str(platform.get(key) or "") == value for key, value in want.items()):
                return str(entry["digest"])
        present = sorted(
            f"{(e.get('platform') or {}).get('os')}/{(e.get('platform') or {}).get('architecture')}"
            for e in entries
            if isinstance(e, dict)
        )
        wanted = f"{want.get('os')}/{want.get('architecture')}"
        raise UpstreamUnavailable(f"{url}: the index has no {wanted} manifest; it carries {present}", url=url)

    # -- discovery -------------------------------------------------------------
    def observe(self, source: dict[str, Any]) -> ProviderResult:
        """One observation per (enabled channel, matching tag), newest ``window`` tags first."""
        watch = source.get("watch") or {}
        component = str(watch.get("component") or "")
        repository = str(watch.get("repository") or "")
        if not component:
            raise UsageError(f"the '{self.id}' watch block of source '{source.get('id')}' declares no 'component'")
        if not _REPOSITORY_RE.match(repository):
            raise UsageError(
                f"the '{self.id}' watch block of source '{source.get('id')}' declares no 'repository' "
                f"of the form owner/name (got {repository!r})"
            )

        base = str(source["baseUrl"]).rstrip("/")
        host = urlparse(base).netloc
        platform = watch.get("platform") or {}
        want = {
            "os": str(platform.get("os") or "linux"),
            "architecture": str(platform.get("architecture") or "amd64"),
        }
        window = int(watch.get("window") or 5)
        kind = str(watch.get("kind") or "framework")
        checkpoint = source.get("checkpoint")
        now = observations.utcnow()

        tags = self._tags(base, repository)
        seen: list[Observation] = []
        heads: dict[str, str] = {}
        for key, channel in sorted(channels.enabled_channels(watch).items()):
            branch = str(channel["branch"])
            selector = _selector(str(channel.get("tag") or f"regex:^{re.escape(key)}$"), f"watch.channels.{key}.tag")
            matching = sorted((tag for tag in tags if selector.match(tag)), key=_sort_key, reverse=True)[:window]
            for tag in matching:
                document, digest, body, url = self._manifest(base, repository, tag)
                platform_digest = self._platform_digest(document, digest, want, url)
                # An OCI tag may carry `_`, which the observation schema's `rev` does not.
                rev = observations.safe_rev(f"{tag}.{digest.split(':', 1)[1][:8]}")
                facts: dict[str, Any] = {
                    "tag": tag,
                    "digest": digest,
                    "platformDigest": platform_digest,
                    "mediaType": str(document.get("mediaType") or ""),
                    "artifact": readiness.artifact(
                        f"{host}/{repository}:{tag}",
                        url,
                        sha256=digest.split(":", 1)[1],
                        size=len(body),
                    ),
                    "channel": key,
                    "listing": {"url": f"{base}/v2/{repository}/tags/list?n=1000"},
                    "releaseTime": channels.first_seen(checkpoint, rev, now),
                    "observationLimit": OBSERVATION_LIMIT,
                }
                game_revision = channel.get("gameRevision")
                if game_revision:
                    facts["gameVersion"] = str(game_revision).replace("{tag}", tag)
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

    def enrich(self, observation: Observation, source: dict[str, Any]) -> Observation:
        """Read the platform manifest's config blob for the image's own labels."""
        watch = source.get("watch") or {}
        base = str(source["baseUrl"]).rstrip("/")
        repository = str(watch.get("repository") or "")
        reference = str(observation.facts.get("platformDigest") or observation.facts.get("digest") or "")
        if not reference:
            return observation
        try:
            manifest, _, _, url = self._manifest(base, repository, reference)
            config_digest = str((manifest.get("config") or {}).get("digest") or "")
            if not config_digest:
                raise UpstreamUnavailable(f"{url}: the platform manifest names no config blob", url=url)
            blob_url = f"{base}/v2/{repository}/blobs/{config_digest}"
            config, _, blob = self._get_json(blob_url, "application/json")
            # The labels this reports become the observation's facts; unhashed bytes from a
            # proxy could put any version, revision or date in them.
            blob_digest = "sha256:" + hashlib.sha256(blob).hexdigest()
            if blob_digest != config_digest:
                raise UpstreamUnavailable(
                    f"{blob_url}: the config blob does not hash to the digest the manifest names "
                    f"(manifest {config_digest}, body {blob_digest})",
                    url=blob_url,
                )
        except Exception:
            # The scan is about to mark this whole source failed; a readiness row built
            # from the listing it already recorded would be a claim on a broken source.
            readiness.registry().forget(observation.component)
            raise
        labels = {str(k): str(v) for k, v in ((config.get("config") or {}).get("Labels") or {}).items()}
        facts = {
            **observation.facts,
            "configDigest": config_digest,
            "labels": labels,
        }
        created = labels.get("org.opencontainers.image.created") or str(config.get("created") or "")
        if created:
            facts["created"] = created
            facts["releaseTime"] = created
        revision = labels.get("org.opencontainers.image.revision")
        if revision:
            facts["revision"] = revision
        enriched = replace(observation, facts=facts)
        if observation.kind == "framework":
            readiness.registry().observed(observation.component, enriched)
        return enriched


PROVIDER = OciRegistryProvider()
