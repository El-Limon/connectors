"""An in-process GitHub API stand-in with inspectable state and injectable failures."""

from __future__ import annotations

import http.server
import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse


@dataclass
class FakeGitHub:
    """Serves the handful of endpoints the maintenance commands use."""

    issues: list[dict[str, Any]] = field(default_factory=list)
    releases: list[dict[str, Any]] = field(default_factory=list)
    assets: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    contents: dict[str, str] = field(default_factory=dict)
    requests: list[tuple[str, str]] = field(default_factory=list)
    authorizations: list[str] = field(default_factory=list)
    writes: int = 0
    _fail_after: int | None = field(default=None, init=False)
    _fail_on: re.Pattern[str] | None = field(default=None, init=False)
    _server: http.server.ThreadingHTTPServer | None = field(default=None, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)

    def fail_after(self, writes: int) -> None:
        """Serve ``writes`` mutating calls, then fail every later one."""
        self._fail_after = writes

    def fail_on(self, pattern: str) -> None:
        """Fail every request whose path matches ``pattern``."""
        self._fail_on = re.compile(pattern)

    @property
    def api_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def _should_fail(self, path: str, method: str) -> bool:
        if self._fail_on is not None and self._fail_on.search(path):
            return True
        if self._fail_after is not None and method != "GET":
            return self.writes >= self._fail_after
        return False

    def __enter__(self) -> FakeGitHub:
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # noqa: A003
                pass

            def _reply(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method: str) -> None:
                parsed = urlparse(self.path)
                fake.requests.append((method, self.path))
                fake.authorizations.append(self.headers.get("Authorization", ""))
                if fake._should_fail(self.path, method):
                    self._reply(500, {"message": "injected failure"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"null") if length else None
                if method != "GET":
                    fake.writes += 1
                self._route(method, parsed.path, parse_qs(parsed.query), body)

            def _route(self, method: str, path: str, query: dict[str, list[str]], body: Any) -> None:
                if path == "/search/issues":
                    term = (query.get("q") or [""])[0]
                    items = [issue for issue in fake.issues if term.split(" ")[-1] in json.dumps(issue)]
                    self._reply(200, {"total_count": len(items), "items": items})
                    return
                if re.fullmatch(r"/repos/[^/]+/[^/]+/issues", path) and method == "GET":
                    page = int((query.get("page") or ["1"])[0])
                    per_page = 2
                    start = (page - 1) * per_page
                    chunk = fake.issues[start : start + per_page]
                    headers = {}
                    if start + per_page < len(fake.issues):
                        headers["Link"] = f'<{fake.api_url}{path}?page={page + 1}>; rel="next"'
                    self._reply(200, chunk, headers)
                    return
                if re.fullmatch(r"/repos/[^/]+/[^/]+/issues", path) and method == "POST":
                    issue = {"number": len(fake.issues) + 1, "state": "open", **(body or {})}
                    fake.issues.append(issue)
                    self._reply(201, issue)
                    return
                match = re.fullmatch(r"/repos/[^/]+/[^/]+/issues/(\d+)", path)
                if match:
                    number = int(match.group(1))
                    issue = next((i for i in fake.issues if i["number"] == number), None)
                    if issue is None:
                        self._reply(404, {"message": "not found"})
                        return
                    if method == "PATCH":
                        issue.update(body or {})
                    self._reply(200, issue)
                    return
                match = re.fullmatch(r"/repos/[^/]+/[^/]+/releases/tags/(.+)", path)
                if match:
                    tag = match.group(1)
                    release = next((r for r in fake.releases if r["tag_name"] == tag), None)
                    self._reply(200 if release else 404, release or {"message": "not found"})
                    return
                match = re.fullmatch(r"/repos/[^/]+/[^/]+/releases/(\d+)/assets", path)
                if match:
                    self._reply(200, fake.assets.get(int(match.group(1)), []))
                    return
                match = re.fullmatch(r"/repos/[^/]+/[^/]+/releases/assets/(\d+)", path)
                if match:
                    asset_id = int(match.group(1))
                    if method == "DELETE":
                        for assets in fake.assets.values():
                            assets[:] = [a for a in assets if a["id"] != asset_id]
                        self._reply(204, None)
                        return
                    self._reply(200, {"id": asset_id})
                    return
                match = re.fullmatch(r"/repos/[^/]+/[^/]+/contents/(.+)", path)
                if match:
                    name = match.group(1)
                    if name in fake.contents:
                        self._reply(200, {"path": name, "content": fake.contents[name]})
                    else:
                        self._reply(404, {"message": "not found"})
                    return
                self._reply(404, {"message": f"no fake route for {path}"})

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
