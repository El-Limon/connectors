"""Credentials come from the environment and must never reach stdout, stderr or a log."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

SECRET = "s3cret-registration-value"


@pytest.fixture(autouse=True)
def _tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAKARO_REGISTRATION_TOKEN", SECRET)
    monkeypatch.setenv("TAKARO_IDENTITY_TOKEN", "identity-" + SECRET)
    monkeypatch.setenv("SOME_API_PASSWORD", "another-" + SECRET)


def test_resolve_env_never_prints_a_token(run: Any) -> None:
    code, payload, stderr = run("targets", "resolve", "--game", "minecraft", "--format", "env")

    assert code == 0
    assert SECRET not in payload
    assert SECRET not in stderr


def test_verbose_install_never_prints_a_token(run: Any, wired: Any, tmp_path: Path) -> None:
    code, payload, stderr = run(
        "--verbose",
        "install",
        "--game",
        "minecraft",
        "--target",
        "fabric-26.2",
        "--dest",
        str(tmp_path / "server"),
        repo=wired.root,
    )

    assert code == 0
    assert SECRET not in str(payload)
    assert SECRET not in stderr


def test_an_error_message_never_prints_a_token(run: Any) -> None:
    code, payload, stderr = run("targets", "resolve", "--game", "minecraft", "--target", "nope")

    assert code == 3
    assert SECRET not in str(payload)
    assert SECRET not in stderr


def test_the_fake_takaro_log_redacts_the_tokens_it_receives(tmp_path: Path) -> None:
    import asyncio
    import json

    import websockets

    from takaro_maint.verify.fake_takaro import FakeTakaro

    log = tmp_path / "fake-takaro.log"

    async def scenario() -> None:
        fake = FakeTakaro(host="127.0.0.1", log_path=log)
        port = await fake.start()
        async with websockets.connect(f"ws://127.0.0.1:{port}/") as client:
            await client.send(
                json.dumps(
                    {
                        "type": "identify",
                        "payload": {"identityToken": "identity-" + SECRET, "registrationToken": SECRET},
                    }
                )
            )
            await client.recv()
        await fake.stop()

    asyncio.run(scenario())

    text = log.read_text()
    assert SECRET not in text
    assert "<redacted>" in text
    assert '"type": "identify"' in text or '"type":"identify"' in text


def test_short_values_are_not_treated_as_secrets(monkeypatch: pytest.MonkeyPatch, run: Any) -> None:
    """A two-character token value would otherwise redact half the output."""
    monkeypatch.setenv("TAKARO_REGISTRATION_TOKEN", "26")

    code, payload, _ = run("targets", "resolve", "--game", "minecraft", "--format", "env")

    assert code == 0
    assert "MC" not in payload or "26.2" in payload
    assert "<redacted>" not in payload
