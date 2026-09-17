"""A local stand-in for Takaro that speaks the Generic Connector protocol.

The frame shapes are the ones ``games/7d2d/tests/fixtures/generic-protocol.json`` pins:
an ``identify`` handshake, ``request``/``response`` envelopes carrying a ``requestId`` and
a JSON-string ``args``, and ``gameEvent`` pushes from the connector.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websockets
from websockets.asyncio.server import Server, ServerConnection, serve

from .. import redact


@dataclass
class FakeTakaro:
    """One connector connection at a time, which is all a single server needs."""

    host: str = "127.0.0.1"
    port: int = 0
    log_path: Path | None = None
    game_server_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    _server: Server | None = field(default=None, init=False)
    _connection: ServerConnection | None = field(default=None, init=False)
    _pending: dict[str, asyncio.Future[Any]] = field(default_factory=dict, init=False)
    _connected: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    identified: dict[str, Any] | None = field(default=None, init=False)
    events: list[dict[str, Any]] = field(default_factory=list, init=False)

    async def start(self) -> int:
        self._server = await serve(self._handle, self.host, self.port)
        self.port = next(iter(self._server.sockets)).getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}/"

    def _log(self, direction: str, frame: Any) -> None:
        if self.log_path is None:
            return
        line = json.dumps({"direction": direction, "frame": frame}, ensure_ascii=False)
        tokens = []
        if isinstance(frame, dict) and isinstance(frame.get("payload"), dict):
            tokens = [
                str(value)
                for key, value in frame["payload"].items()
                if "token" in key.lower() and isinstance(value, str)
            ]
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(redact.redact(line, tokens) + "\n")

    async def _send(self, connection: ServerConnection, frame: dict[str, Any]) -> None:
        self._log("out", frame)
        await connection.send(json.dumps(frame))

    async def _handle(self, connection: ServerConnection) -> None:
        self._connection = connection
        try:
            async for raw in connection:
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self._log("in", frame)
                await self._dispatch(connection, frame)
        except websockets.ConnectionClosed:
            pass
        finally:
            if self._connection is connection:
                self._connection = None

    async def _dispatch(self, connection: ServerConnection, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == "identify":
            self.identified = frame.get("payload", {})
            await self._send(
                connection,
                {
                    "type": "identifyResponse",
                    "payload": {"gameServerId": self.game_server_id, "status": "authenticated"},
                },
            )
            self._connected.set()
            return
        if kind in ("response", "error"):
            future = self._pending.pop(str(frame.get("requestId")), None)
            if future is not None and not future.done():
                if kind == "error":
                    future.set_exception(RuntimeError(json.dumps(frame.get("payload"))))
                else:
                    future.set_result(frame.get("payload"))
            return
        if kind == "gameEvent":
            self.events.append(frame.get("payload", {}))

    async def wait_for_identify(self, timeout: float) -> dict[str, Any]:
        await asyncio.wait_for(self._connected.wait(), timeout)
        assert self.identified is not None
        return self.identified

    async def request(self, action: str, args: dict[str, Any] | None = None, *, timeout: float = 30.0) -> Any:
        """Send one ``request`` frame and resolve on its matching ``response``."""
        if self._connection is None:
            raise RuntimeError("no connector is connected")
        request_id = str(uuid.uuid4())
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send(
            self._connection,
            {
                "type": "request",
                "requestId": request_id,
                "payload": {"action": action, "args": json.dumps(args or {})},
            },
        )
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)

    async def ping(self, *, timeout: float = 5.0) -> float:
        """One RFC 6455 ping, awaited to its pong. Returns the round trip in seconds."""
        if self._connection is None:
            raise RuntimeError("no connector is connected")
        loop = asyncio.get_running_loop()
        started = loop.time()
        pong_waiter = await self._connection.ping()
        await asyncio.wait_for(pong_waiter, timeout)
        return loop.time() - started
