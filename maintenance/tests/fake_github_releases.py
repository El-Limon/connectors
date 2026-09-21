"""An in-process stand-in for the GitHub release API, including asset bytes.

The release endpoints move files, not only JSON, so this fake serves uploads and downloads as
real bytes and keeps them in memory. That is what lets a test assert what a publisher did rather
than what it said: which requests it made, in what order, and which bytes each release ended up
holding.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import pytest

#: A token that is obviously not real, long enough for the redactor to treat it as a secret.
TOKEN = "gh0-fixture-token-value"


@contextlib.contextmanager
def serving(monkeypatch: pytest.MonkeyPatch, repo: str) -> Iterator[FakeReleases]:
    """A running fake with ``GH_TOKEN`` pointed at it, the way CI hands the command a token."""
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    with FakeReleases(repo=repo) as server:
        yield server


@dataclass
class FakeReleases:
    """Releases, assets and tag refs for one repository."""

    repo: str = "o/r"
    digests: bool = True
    releases: list[dict[str, Any]] = field(default_factory=list)
    blobs: dict[int, bytes] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    requests: list[tuple[str, str]] = field(default_factory=list)
    uploads: int = 0
    _next_release: int = field(default=100, init=False)
    _next_asset: int = field(default=900, init=False)
    _fail_uploads_after: int | None = field(default=None, init=False)
    _fail_on: tuple[re.Pattern[str], str | None] | None = field(default=None, init=False)
    _corrupt: set[str] = field(default_factory=set, init=False)
    _server: http.server.ThreadingHTTPServer | None = field(default=None, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    cdn_requests: list[dict[str, str]] = field(default_factory=list)
    _cdn: http.server.ThreadingHTTPServer | None = field(default=None, init=False)
    _cdn_thread: threading.Thread | None = field(default=None, init=False)

    # -- injected failures ----------------------------------------------------
    def fail_uploads_after(self, count: int) -> None:
        """Serve ``count`` asset uploads, then fail every later one."""
        self._fail_uploads_after = count

    def fail_on(self, pattern: str, method: str | None = None) -> None:
        self._fail_on = (re.compile(pattern), method)

    def corrupt_download(self, name: str) -> None:
        """Serve different bytes than were uploaded, as a damaged or swapped asset would."""
        self._corrupt.add(name)

    def redirect_downloads(self) -> None:
        """Answer asset downloads with a 302 to a second host, the way GitHub does.

        The second host stands in for the signed CDN URL GitHub redirects to: it records every
        request's headers and, like a signed URL, refuses any request that carries an
        ``Authorization`` header with 403.
        """
        fake = self

        class CdnHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                fake.cdn_requests.append({key: value for key, value in self.headers.items()})
                if self.headers.get("Authorization"):
                    body = b'{"message":"credentials are not allowed on a signed URL"}'
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                asset_id = int(urlparse(self.path).path.rsplit("/", 1)[-1])
                payload = fake.blobs.get(asset_id, b"")
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._cdn = http.server.ThreadingHTTPServer(("127.0.0.1", 0), CdnHandler)
        self._cdn_thread = threading.Thread(target=self._cdn.serve_forever, daemon=True)
        self._cdn_thread.start()

    @property
    def cdn_url(self) -> str:
        assert self._cdn is not None
        host, port = self._cdn.server_address[:2]
        return f"http://{host}:{port}"

    # -- state a test builds or inspects --------------------------------------
    def add_release(
        self,
        tag: str,
        *,
        draft: bool = False,
        prerelease: bool = False,
        body: str = "",
        name: str = "",
        target_commitish: str = "main",
    ) -> dict[str, Any]:
        self._next_release += 1
        release = {
            "id": self._next_release,
            "tag_name": tag,
            "name": name or tag,
            "body": body,
            "draft": draft,
            "prerelease": prerelease,
            "target_commitish": target_commitish,
            "html_url": f"https://github.com/{self.repo}/releases/tag/{tag}",
            "upload_url": self._upload_url(self._next_release),
            "assets": [],
        }
        self.releases.append(release)
        return release

    def add_tag(self, tag: str, sha: str) -> None:
        self.tags[tag] = sha

    def add_asset(self, release: dict[str, Any], name: str, payload: bytes) -> dict[str, Any]:
        import hashlib

        self._next_asset += 1
        asset: dict[str, Any] = {
            "id": self._next_asset,
            "name": name,
            "size": len(payload),
            "browser_download_url": f"https://github.com/{self.repo}/releases/download/{release['tag_name']}/{name}",
            "digest": f"sha256:{hashlib.sha256(payload).hexdigest()}" if self.digests else None,
        }
        release["assets"].append(asset)
        self.blobs[asset["id"]] = payload
        return asset

    def release_for(self, tag: str) -> dict[str, Any] | None:
        return next((r for r in self.releases if r["tag_name"] == tag), None)

    def asset_names(self, tag: str) -> list[str]:
        release = self.release_for(tag)
        return sorted(a["name"] for a in release["assets"]) if release else []

    def asset_bytes(self, tag: str, name: str) -> bytes:
        release = self.release_for(tag)
        assert release is not None, f"no release for {tag}"
        asset = next(a for a in release["assets"] if a["name"] == name)
        return self.blobs[asset["id"]]

    # -- transport ------------------------------------------------------------
    @property
    def api_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def _upload_url(self, release_id: int) -> str:
        return f"{self.api_url}/uploads/repos/{self.repo}/releases/{release_id}/assets{{?name,label}}"

    def _should_fail(self, path: str, method: str) -> bool:
        if self._fail_on is not None:
            pattern, wanted = self._fail_on
            if pattern.search(path) and (wanted is None or wanted == method):
                return True
        return False

    def __enter__(self) -> FakeReleases:
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # noqa: A003
                pass

            def _json(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
                body = json.dumps(payload).encode("utf-8")
                self._send(status, body, "application/json", headers)

            def _send(self, status: int, body: bytes, kind: str, headers: dict[str, str] | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method: str) -> None:
                parsed = urlparse(self.path)
                fake.requests.append((method, self.path))
                if fake._should_fail(self.path, method):
                    self._json(500, {"message": "injected failure"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                self._route(method, unquote(parsed.path), parse_qs(parsed.query), raw)

            def _route(self, method: str, path: str, query: dict[str, list[str]], raw: bytes) -> None:
                repo = re.escape(fake.repo)

                match = re.fullmatch(rf"/uploads/repos/{repo}/releases/(\d+)/assets", path)
                if match and method == "POST":
                    # Asset bytes, not JSON: this is the one route whose body is never parsed.
                    self._upload(int(match.group(1)), (query.get("name") or [""])[0], raw)
                    return

                body = json.loads(raw) if raw else None

                if re.fullmatch(rf"/repos/{repo}/releases", path):
                    if method == "GET":
                        self._json(200, [self._public(r) for r in fake.releases])
                        return
                    if method == "POST":
                        created = fake.add_release(
                            str((body or {}).get("tag_name")),
                            draft=bool((body or {}).get("draft")),
                            prerelease=bool((body or {}).get("prerelease")),
                            body=str((body or {}).get("body") or ""),
                            name=str((body or {}).get("name") or ""),
                            target_commitish=str((body or {}).get("target_commitish") or "main"),
                        )
                        self._json(201, self._public(created))
                        return

                match = re.fullmatch(rf"/repos/{repo}/releases/tags/(.+)", path)
                if match:
                    release = fake.release_for(match.group(1))
                    # GitHub answers this endpoint for published releases only.
                    if release is None or release["draft"]:
                        self._json(404, {"message": "not found"})
                    else:
                        self._json(200, self._public(release))
                    return

                match = re.fullmatch(rf"/repos/{repo}/releases/(\d+)/assets", path)
                if match:
                    release = self._by_id(int(match.group(1)))
                    self._json(200, [dict(a) for a in release["assets"]] if release else [])
                    return

                match = re.fullmatch(rf"/repos/{repo}/releases/assets/(\d+)", path)
                if match:
                    self._asset(method, int(match.group(1)))
                    return

                match = re.fullmatch(rf"/repos/{repo}/releases/(\d+)", path)
                if match:
                    release = self._by_id(int(match.group(1)))
                    if release is None:
                        self._json(404, {"message": "not found"})
                        return
                    if method == "PATCH":
                        fields = dict(body or {})
                        release.update(fields)
                        # Real GitHub behaviour: patching a draft release without resending
                        # tag_name drops its tag association and renames it `untagged-<hash>`.
                        if release["draft"] and "tag_name" not in fields:
                            release["tag_name"] = f"untagged-{release['id']}"
                        if not release["draft"] and release["tag_name"] not in fake.tags:
                            fake.tags[release["tag_name"]] = str(release["target_commitish"])
                        self._json(200, self._public(release))
                        return
                    if method == "DELETE":
                        fake.releases.remove(release)
                        self._json(204, None)
                        return
                    self._json(200, self._public(release))
                    return

                match = re.fullmatch(rf"/repos/{repo}/git/ref/tags/(.+)", path)
                if match:
                    tag = match.group(1)
                    if tag not in fake.tags:
                        self._json(404, {"message": "not found"})
                        return
                    self._json(
                        200,
                        {"ref": f"refs/tags/{tag}", "object": {"sha": fake.tags[tag], "type": "commit"}},
                    )
                    return

                match = re.fullmatch(rf"/repos/{repo}/git/refs/tags/(.+)", path)
                if match and method == "DELETE":
                    fake.tags.pop(match.group(1), None)
                    self._json(204, None)
                    return

                self._json(404, {"message": f"no fake route for {path}"})

            def _by_id(self, release_id: int) -> dict[str, Any] | None:
                return next((r for r in fake.releases if r["id"] == release_id), None)

            def _public(self, release: dict[str, Any]) -> dict[str, Any]:
                published = {k: v for k, v in release.items() if k != "assets"}
                published["upload_url"] = fake._upload_url(int(release["id"]))
                return published

            def _upload(self, release_id: int, name: str, payload: bytes) -> None:
                release = self._by_id(release_id)
                if release is None:
                    self._json(404, {"message": "not found"})
                    return
                if fake._fail_uploads_after is not None and fake.uploads >= fake._fail_uploads_after:
                    self._json(500, {"message": "injected upload failure"})
                    return
                if any(a["name"] == name for a in release["assets"]):
                    self._json(422, {"message": "already_exists"})
                    return
                fake.uploads += 1
                self._json(201, fake.add_asset(release, name, payload))

            def _asset(self, method: str, asset_id: int) -> None:
                for release in fake.releases:
                    for asset in release["assets"]:
                        if asset["id"] != asset_id:
                            continue
                        if method == "DELETE":
                            release["assets"].remove(asset)
                            fake.blobs.pop(asset_id, None)
                            self._json(204, None)
                            return
                        if "octet-stream" in (self.headers.get("Accept") or ""):
                            if fake._cdn is not None:
                                self._send(
                                    302,
                                    b"",
                                    "application/octet-stream",
                                    {"Location": f"{fake.cdn_url}/cdn/{asset_id}"},
                                )
                                return
                            payload = fake.blobs[asset_id]
                            if asset["name"] in fake._corrupt:
                                payload = payload + b"corrupted"
                            self._send(200, payload, "application/octet-stream")
                            return
                        self._json(200, dict(asset))
                        return
                self._json(404, {"message": "not found"})

            def do_GET(self) -> None:  # noqa: N802
                self._handle("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._handle("POST")

            def do_PATCH(self) -> None:  # noqa: N802
                self._handle("PATCH")

            def do_DELETE(self) -> None:  # noqa: N802
                self._handle("DELETE")

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._cdn is not None:
            self._cdn.shutdown()
            self._cdn.server_close()
        if self._cdn_thread is not None:
            self._cdn_thread.join(timeout=5)
