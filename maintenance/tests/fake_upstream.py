"""An in-process stand-in for Mojang and FabricMC.

Tests point a catalog copy at this server's base URL, so download, integrity and
failure behaviour are exercised through the real command with real HTTP.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FakeUpstream:
    """Serves a fixed set of paths and records every one that was asked for."""

    files: dict[str, bytes] = field(default_factory=dict)
    requested: list[str] = field(default_factory=list)
    status_overrides: dict[str, int] = field(default_factory=dict)
    _server: http.server.ThreadingHTTPServer | None = field(default=None, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)

    def add(self, path: str, payload: bytes) -> str:
        self.files[path] = payload
        return hashlib.sha256(payload).hexdigest()

    def add_json(self, path: str, document: object) -> str:
        return self.add(path, json.dumps(document).encode("utf-8"))

    def add_file(self, path: str, file: Path) -> str:
        return self.add(path, file.read_bytes())

    @property
    def base_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> FakeUpstream:
        upstream = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # noqa: A003 - silence the default logging
                pass

            def do_GET(self) -> None:  # noqa: N802 - http.server's naming
                upstream.requested.append(self.path)
                override = upstream.status_overrides.get(self.path)
                if override:
                    self.send_error(override)
                    return
                payload = upstream.files.get(self.path)
                if payload is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

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
