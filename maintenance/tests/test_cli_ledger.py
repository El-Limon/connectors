"""`ledger check`: does this directory really hold the target we think it does?"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def install(run: Any, wired: Any, dest: Path) -> None:
    code, payload, _ = run(
        "install", "--game", "minecraft", "--target", "fabric-26.2", "--dest", str(dest), repo=wired.root
    )
    assert code == 0, payload


def check(run: Any, wired: Any, dest: Path) -> tuple[int, Any, str]:
    return run(
        "ledger", "check", "--game", "minecraft", "--target", "fabric-26.2", "--dest", str(dest), repo=wired.root
    )


def test_a_fresh_install_checks_out(run: Any, wired: Any, tmp_path: Path) -> None:
    dest = tmp_path / "server"
    install(run, wired, dest)

    code, payload, _ = check(run, wired, dest)

    assert code == 0
    assert payload["reasons"] == []


def test_a_missing_ledger_is_a_conflict(run: Any, wired: Any, tmp_path: Path) -> None:
    dest = tmp_path / "server"
    dest.mkdir()

    code, payload, _ = check(run, wired, dest)

    assert code == 7
    assert "no ledger" in payload["reasons"][0]


def test_a_stale_fingerprint_is_a_conflict(run: Any, wired: Any, tmp_path: Path) -> None:
    dest = tmp_path / "server"
    install(run, wired, dest)
    ledger_file = dest / ".takaro/installed-target.json"
    ledger = json.loads(ledger_file.read_text())
    ledger["fingerprint"] = "0" * 64
    ledger_file.write_text(json.dumps(ledger))

    code, payload, _ = check(run, wired, dest)

    assert code == 7
    assert any("fingerprint" in reason for reason in payload["reasons"])


def test_a_ledger_for_another_target_is_a_conflict(run: Any, wired: Any, tmp_path: Path) -> None:
    dest = tmp_path / "server"
    install(run, wired, dest)
    ledger_file = dest / ".takaro/installed-target.json"
    ledger = json.loads(ledger_file.read_text())
    ledger["target"] = "fabric-26.1.2"
    ledger_file.write_text(json.dumps(ledger))

    code, payload, _ = check(run, wired, dest)

    assert code == 7
    assert any("fabric-26.1.2" in reason for reason in payload["reasons"])


def test_a_tampered_input_is_a_conflict(run: Any, wired: Any, tmp_path: Path) -> None:
    dest = tmp_path / "server"
    install(run, wired, dest)
    (dest / "mods/fabric-api-0.160.0+26.2.jar").write_bytes(b"someone swapped the API jar")

    code, payload, _ = check(run, wired, dest)

    assert code == 7
    assert any("fabric-api" in reason for reason in payload["reasons"])


def test_a_missing_input_is_a_conflict(run: Any, wired: Any, tmp_path: Path) -> None:
    dest = tmp_path / "server"
    install(run, wired, dest)
    (dest / "server.jar").unlink()

    code, payload, _ = check(run, wired, dest)

    assert code == 7
    assert any("server.jar" in reason for reason in payload["reasons"])


def test_every_reason_is_reported_not_just_the_first(run: Any, wired: Any, tmp_path: Path) -> None:
    dest = tmp_path / "server"
    install(run, wired, dest)
    (dest / "server.jar").unlink()
    (dest / "mods/fabric-api-0.160.0+26.2.jar").write_bytes(b"swapped")

    code, payload, _ = check(run, wired, dest)

    assert code == 7
    assert len(payload["reasons"]) >= 2
