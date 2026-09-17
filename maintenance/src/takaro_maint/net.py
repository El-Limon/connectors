"""HTTP downloads with a content-addressed cache. Never falls back to a second URL."""

from __future__ import annotations

import hashlib
import os
import shutil
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Protocol

from . import __version__, output
from .exit_codes import IntegrityError, UpstreamUnavailable

USER_AGENT = f"takaro-connectors-maint/{__version__} (+https://github.com/gettakaro/connectors)"
TIMEOUT_SECONDS = 60


class Transport(Protocol):
    """How bytes are fetched. Swapped in tests for an in-process fake."""

    def open(self, url: str, headers: dict[str, str]) -> IO[bytes]: ...


class UrllibTransport:
    def open(self, url: str, headers: dict[str, str]) -> IO[bytes]:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
        return urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)  # noqa: S310


_transport: Transport = UrllibTransport()


def set_transport(transport: Transport) -> None:
    global _transport
    _transport = transport


def get_transport() -> Transport:
    return _transport


@dataclass(frozen=True)
class Expectation:
    """What the catalog says the bytes must be."""

    sha256: str | None = None
    sha1: str | None = None
    size: int | None = None

    def satisfied_by(self, digests: dict[str, Any]) -> tuple[bool, str]:
        if self.sha256 and digests["sha256"] != self.sha256:
            return False, f"sha256 expected {self.sha256} actual {digests['sha256']}"
        if self.sha1 and digests["sha1"] != self.sha1:
            return False, f"sha1 expected {self.sha1} actual {digests['sha1']}"
        if self.size is not None and digests["size"] != self.size:
            return False, f"size expected {self.size} actual {digests['size']}"
        return True, ""


def _hash_file(path: Path) -> dict[str, Any]:
    sha256 = hashlib.sha256()
    sha1 = hashlib.sha1()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            sha256.update(chunk)
            sha1.update(chunk)
    return {"sha256": sha256.hexdigest(), "sha1": sha1.hexdigest(), "size": size}


def hash_file(path: Path) -> dict[str, Any]:
    return _hash_file(path)


def sha256_file(path: Path) -> str:
    return _hash_file(path)["sha256"]


def _fetch_to(url: str, target: Path, headers: dict[str, str]) -> dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    attempts = 0
    while True:
        attempts += 1
        try:
            with _transport.open(url, headers) as response, target.open("wb") as handle:
                shutil.copyfileobj(response, handle, length=1024 * 1024)
            break
        except urllib.error.HTTPError as exc:
            raise UpstreamUnavailable(f"{url} returned HTTP {exc.code}", url=url, status=exc.code) from exc
        except TimeoutError as exc:
            if attempts >= 2:
                raise UpstreamUnavailable(f"{url} timed out", url=url) from exc
            output.debug(f"timeout on {url}, retrying the same URL once")
        except urllib.error.URLError as exc:
            raise UpstreamUnavailable(f"{url} is unreachable: {exc.reason}", url=url) from exc
        except OSError as exc:
            raise UpstreamUnavailable(f"{url} is unreachable: {exc}", url=url) from exc
    return _hash_file(target)


def fetch(url: str, dest: Path, expect: Expectation, *, no_cache: bool = False) -> dict[str, Any]:
    """Download ``url`` straight to ``dest``, verifying ``expect``. No cache involved."""
    headers = {"Cache-Control": "no-cache", "Pragma": "no-cache"} if no_cache else {}
    output.debug(f"GET {url}")
    digests = _fetch_to(url, dest, headers)
    ok, reason = expect.satisfied_by(digests)
    if not ok:
        dest.unlink(missing_ok=True)
        raise IntegrityError(f"{url}: {reason}", url=url, **digests)
    return digests


def download(url: str, expect: Expectation, cache: Path) -> Path:
    """Fetch ``url`` into the content-addressed cache and return the blob path.

    A cache hit is re-hashed before use; a corrupt blob is deleted and fetched once more.
    """
    blob = cache / "blobs" / "sha256" / expect.sha256 if expect.sha256 else None
    if blob is not None and blob.exists():
        digests = _hash_file(blob)
        ok, reason = expect.satisfied_by(digests)
        if ok:
            output.debug(f"cache hit {url}")
            return blob
        output.warn(f"corrupt cache blob for {url} ({reason}); re-downloading")
        blob.unlink(missing_ok=True)

    tmp = cache / "tmp" / uuid.uuid4().hex
    try:
        digests = _fetch_to(url, tmp, {})
        ok, reason = expect.satisfied_by(digests)
        if not ok:
            raise IntegrityError(f"{url}: {reason}", url=url, **digests)
        final = cache / "blobs" / "sha256" / digests["sha256"]
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, final)
        return final
    finally:
        tmp.unlink(missing_ok=True)
