"""An upstream timeout is retried on the same URL; anything else is not retried at all."""

from __future__ import annotations

import hashlib
import http.server
import io
import socket
import threading
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from takaro_maint import net
from takaro_maint.exit_codes import UpstreamUnavailable

PAYLOAD = b"a tiny blob"
URL = "http://127.0.0.1:1/blob.bin"


class ScriptedTransport:
    """Answers each ``open`` with the next scripted step: bytes, or an exception to raise."""

    def __init__(self, *steps: Any) -> None:
        self.steps = list(steps)
        self.calls: list[str] = []

    def open(self, url: str, headers: dict[str, str]) -> Any:
        del headers
        self.calls.append(url)
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return io.BytesIO(step)


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a scripted transport and record the delays instead of sleeping them."""
    delays: list[float] = []
    monkeypatch.setattr(net.time, "sleep", delays.append)
    previous = net.get_transport()

    def _install(*steps: Any) -> ScriptedTransport:
        transport = ScriptedTransport(*steps)
        net.set_transport(transport)
        return transport

    try:
        yield _install, delays
    finally:
        net.set_transport(previous)


def expect() -> net.Expectation:
    return net.Expectation(sha256=hashlib.sha256(PAYLOAD).hexdigest(), size=len(PAYLOAD))


def test_two_timeouts_then_bytes_still_lands_the_blob(scripted: Any, tmp_path: Path) -> None:
    install, delays = scripted
    transport = install(TimeoutError(), TimeoutError(), PAYLOAD)

    blob = net.download(URL, expect(), tmp_path / "cache")

    assert blob.read_bytes() == PAYLOAD
    assert transport.calls == [URL, URL, URL], "the retry must use the same URL"
    assert delays == [2.0, 6.0]


def test_a_connect_timeout_counts_as_a_timeout(scripted: Any, tmp_path: Path) -> None:
    install, delays = scripted
    transport = install(urllib.error.URLError(TimeoutError()), PAYLOAD)

    blob = net.download(URL, expect(), tmp_path / "cache")

    assert blob.read_bytes() == PAYLOAD
    assert len(transport.calls) == 2
    assert delays == [2.0]


def test_a_third_timeout_gives_up(scripted: Any, tmp_path: Path) -> None:
    install, delays = scripted
    transport = install(TimeoutError(), TimeoutError(), TimeoutError())

    with pytest.raises(UpstreamUnavailable) as caught:
        net.download(URL, expect(), tmp_path / "cache")

    assert str(caught.value).endswith("timed out after 3 attempts")
    assert len(transport.calls) == 3
    assert delays == [2.0, 6.0]


def test_an_http_error_is_never_retried(scripted: Any, tmp_path: Path) -> None:
    install, delays = scripted
    error = urllib.error.HTTPError(URL, 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]
    transport = install(error)

    with pytest.raises(UpstreamUnavailable) as caught:
        net.download(URL, expect(), tmp_path / "cache")

    assert caught.value.detail.get("status") == 503
    assert caught.value.code == 4
    assert len(transport.calls) == 1
    assert delays == []


def test_a_connection_refused_is_never_retried(scripted: Any, tmp_path: Path) -> None:
    install, delays = scripted
    transport = install(urllib.error.URLError(ConnectionRefusedError("refused")))

    with pytest.raises(UpstreamUnavailable) as caught:
        net.download(URL, expect(), tmp_path / "cache")

    assert "unreachable" in str(caught.value)
    assert len(transport.calls) == 1
    assert delays == []


def test_the_suite_may_not_reach_the_public_internet(tmp_path: Path) -> None:
    """The autouse guard in conftest refuses a non-loopback URL before any DNS lookup."""
    with pytest.raises(RuntimeError, match="outside its fake upstream"):
        net.download("https://example.invalid/x", expect(), tmp_path / "cache")


def test_the_production_transport_does_not_follow_a_redirect() -> None:
    requested: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802 - http.server's callback name
            requested.append(self.path)
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/private-destination")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"must not be reached")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        with pytest.raises(urllib.error.HTTPError) as caught:
            addresses: tuple[net.PinnedAddress, ...] = ((socket.AF_INET, socket.SOCK_STREAM, 6, (host, port)),)
            net.UrllibTransport().open_no_redirect(f"http://{host}:{port}/start", {}, addresses)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert caught.value.code == 302
    assert requested == ["/start"]


def test_the_pinned_https_connection_uses_the_address_without_resolving_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected: list[tuple[Any, ...]] = []
    server_names: list[str] = []

    class RawSocket:
        def settimeout(self, timeout: object) -> None:
            assert timeout == 3

        def bind(self, address: object) -> None:
            raise AssertionError(f"unexpected bind to {address}")

        def connect(self, address: tuple[Any, ...]) -> None:
            connected.append(address)

        def close(self) -> None:
            pass

    class Context:
        def wrap_socket(self, sock: RawSocket, *, server_hostname: str) -> RawSocket:
            server_names.append(server_hostname)
            return sock

    monkeypatch.setattr(net.socket, "socket", lambda *args: RawSocket())
    monkeypatch.setattr(
        net.socket,
        "getaddrinfo",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("hostname was resolved twice")),
    )
    address: net.PinnedAddress = (socket.AF_INET, socket.SOCK_STREAM, 6, ("93.184.216.34", 443))
    connection = net._PinnedHTTPSConnection(  # noqa: SLF001 - exercises the security-boundary primitive
        "auth.example",
        443,
        timeout=3,
        context=Context(),
        pinned_addresses=(address,),  # type: ignore[arg-type]
    )

    connection.connect()

    assert connected == [("93.184.216.34", 443)]
    assert server_names == ["auth.example"]
