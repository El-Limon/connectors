"""`install`: integrity, atomicity, preservation and world compatibility."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from conftest import sha256


def tree(root: Path) -> dict[str, str]:
    """Every regular file under ``root`` with its hash, for before/after comparisons."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def install(run: Any, wired: Any, dest: Path, *extra: str) -> tuple[int, Any, str]:
    return run(
        "install", "--game", "minecraft", "--target", "fabric-26.2", "--dest", str(dest), *extra, repo=wired.root
    )


def test_a_fresh_install_lays_out_every_pinned_input(run: Any, wired: Any, tmp_path: Path) -> None:
    dest = tmp_path / "server"

    code, payload, _ = install(run, wired, dest)

    assert code == 0, payload
    assert payload["status"] == "installed"
    assert (dest / "server.jar").is_file()
    assert (dest / "fabric-server-mc.26.2-loader.0.19.5-launcher.1.1.2.jar").is_file()
    assert (dest / "mods/fabric-api-0.160.0+26.2.jar").is_file()
    ledger = json.loads((dest / ".takaro/installed-target.json").read_text())
    assert ledger["target"] == "fabric-26.2"
    assert ledger["world"]["revision"] == "26.2"
    assert {entry["name"] for entry in ledger["inputs"]} == {"game", "loader", "fabricApi"}


def test_the_ledger_validates_against_its_schema(run: Any, wired: Any, tmp_path: Path, repo_root: Path) -> None:
    from jsonschema import Draft202012Validator

    dest = tmp_path / "server"
    install(run, wired, dest)

    schema = json.loads((repo_root / "catalog/schema/v1/installed-ledger.schema.json").read_text())
    ledger = json.loads((dest / ".takaro/installed-target.json").read_text())

    assert list(Draft202012Validator(schema).iter_errors(ledger)) == []


def test_a_second_install_is_a_no_op(run: Any, wired: Any, tmp_path: Path) -> None:
    dest = tmp_path / "server"
    install(run, wired, dest)
    before = tree(dest)

    code, payload, _ = install(run, wired, dest)

    assert code == 0
    assert payload["status"] == "already-installed"
    assert tree(dest) == before


def test_a_wrong_hash_exits_five_and_changes_nothing(run: Any, wired: Any, tmp_path: Path) -> None:
    dest = tmp_path / "server"
    install(run, wired, dest)
    before = tree(dest)

    record = wired.target()
    record["inputs"]["fabricApi"]["sha256"] = "0" * 64
    record["build"]["deps"]["fabric-api"]["sha256"] = "0" * 64
    wired.save(record)

    code, payload, _ = install(run, wired, dest)

    assert code == 5
    assert "sha256" in payload["error"]
    assert tree(dest) == before
    assert not (dest / ".takaro/staging").exists()


def test_an_unavailable_input_exits_four_without_a_fallback_url(run: Any, wired: Any, tmp_path: Path) -> None:
    record = wired.target()
    wired.upstream.status_overrides[record["inputs"]["fabricApi"]["path"]] = 503
    wired.upstream.requested.clear()
    dest = tmp_path / "server"

    code, payload, _ = install(run, wired, dest)

    assert code == 4
    assert wired.upstream.requested.count(record["inputs"]["fabricApi"]["path"]) == 1
    assert not any(
        "fabric-api" in path and path != record["inputs"]["fabricApi"]["path"] for path in wired.upstream.requested
    )


def test_a_failed_install_leaves_a_working_install_byte_identical(run: Any, wired: Any, tmp_path: Path) -> None:
    dest = tmp_path / "server"
    install(run, wired, dest)
    before = tree(dest)

    record = wired.target()
    wired.upstream.status_overrides[record["inputs"]["loader"]["path"]] = 500
    record["inputs"]["loader"]["sha256"] = "1" * 64
    wired.save(record)

    code, _, _ = install(run, wired, dest)

    assert code != 0
    assert tree(dest) == before


def test_protected_paths_survive_a_reinstall(run: Any, wired: Any, tmp_path: Path) -> None:
    dest = tmp_path / "server"
    install(run, wired, dest)

    protected = {
        "world/level.dat": b"a world nobody wants to lose",
        "config/takaro.json": b'{"token":"kept"}',
        "server.properties": b"motd=mine\n",
        "ops.json": b"[]",
    }
    for relative, payload in protected.items():
        path = dest / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    # A new target for the same revision: everything is re-staged, nothing protected moves.
    record = wired.target()
    record["support"]["notes"] = "a change that alters the fingerprint via inputs"
    record["inputs"]["fabricApi"]["installPath"] = "mods/fabric-api-0.160.0+26.2.jar"
    record["runtime"]["container"]["env"]["EXTRA"] = "1"
    wired.save(record)

    code, _, _ = install(run, wired, dest)

    assert code == 0
    for relative, payload in protected.items():
        assert (dest / relative).read_bytes() == payload


def test_an_incompatible_world_is_refused_without_a_flag(run: Any, wired: Any, tmp_path: Path) -> None:
    dest = tmp_path / "server"
    install(run, wired, dest)
    (dest / "world").mkdir()
    (dest / "world/level.dat").write_bytes(b"old world")
    ledger_file = dest / ".takaro/installed-target.json"
    ledger = json.loads(ledger_file.read_text())
    ledger["world"]["revision"] = "26.1.2"
    ledger_file.write_text(json.dumps(ledger))

    code, payload, _ = install(run, wired, dest)

    assert code == 7
    assert "26.1.2" in payload["error"]
    assert (dest / "world/level.dat").read_bytes() == b"old world"


def test_fresh_world_moves_the_old_world_aside_instead_of_deleting_it(run: Any, wired: Any, tmp_path: Path) -> None:
    dest = tmp_path / "server"
    install(run, wired, dest)
    (dest / "world").mkdir()
    (dest / "world/level.dat").write_bytes(b"old world")
    ledger_file = dest / ".takaro/installed-target.json"
    ledger = json.loads(ledger_file.read_text())
    ledger["world"]["revision"] = "26.1.2"
    ledger_file.write_text(json.dumps(ledger))

    code, payload, _ = install(run, wired, dest, "--fresh-world")

    assert code == 0
    assert payload["worldMovedTo"].startswith(".takaro/worlds/26.1.2-")
    moved = list((dest / ".takaro/worlds").rglob("level.dat"))
    assert len(moved) == 1
    assert moved[0].read_bytes() == b"old world"


def test_reuse_world_records_the_new_revision(run: Any, wired: Any, tmp_path: Path) -> None:
    dest = tmp_path / "server"
    install(run, wired, dest)
    (dest / "world").mkdir()
    (dest / "world/level.dat").write_bytes(b"old world")
    ledger_file = dest / ".takaro/installed-target.json"
    ledger = json.loads(ledger_file.read_text())
    ledger["world"]["revision"] = "26.1.2"
    ledger_file.write_text(json.dumps(ledger))

    code, _, stderr = install(run, wired, dest, "--reuse-world")

    assert code == 0
    assert "reusing a world" in stderr
    assert json.loads(ledger_file.read_text())["world"]["revision"] == "26.2"
    assert (dest / "world/level.dat").read_bytes() == b"old world"


def test_dry_run_writes_nothing(run: Any, wired: Any, tmp_path: Path) -> None:
    dest = tmp_path / "server"

    code, payload, _ = install(run, wired, dest, "--dry-run")

    assert code == 0
    assert payload["status"] == "dry-run"
    assert {entry["name"] for entry in payload["inputs"]} == {"game", "loader", "fabricApi"}
    assert not dest.exists() or tree(dest) == {}


def test_rollback_is_refused_for_a_game_that_keeps_no_previous_install(run: Any, wired: Any, tmp_path: Path) -> None:
    code, payload, _ = install(run, wired, tmp_path / "server", "--rollback")

    assert code == 2
    assert "roll back" in payload["error"]


def test_a_corrupt_cache_blob_is_redownloaded(run: Any, wired: Any, tmp_path: Path, monkeypatch: Any) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setenv("TAKARO_MAINT_CACHE", str(cache))
    dest = tmp_path / "server"
    install(run, wired, dest)

    record = wired.target()
    blob = cache / "blobs" / "sha256" / record["inputs"]["fabricApi"]["sha256"]
    assert blob.is_file()
    blob.write_bytes(b"corrupted on disk")

    code, _, stderr = install(run, wired, tmp_path / "second")

    assert code == 0
    assert "corrupt cache blob" in stderr
    assert (
        sha256((tmp_path / "second/mods/fabric-api-0.160.0+26.2.jar").read_bytes())
        == (record["inputs"]["fabricApi"]["sha256"])
    )


def test_a_mojang_manifest_that_disagrees_with_the_record_exits_five(run: Any, wired: Any, tmp_path: Path) -> None:
    record = wired.target()
    manifest_path = record["inputs"]["game"]["manifest"]["path"]
    manifest = json.loads(wired.upstream.files[manifest_path])
    manifest["downloads"]["server"]["sha1"] = "0" * 40
    payload_bytes = json.dumps(manifest).encode("utf-8")
    record["inputs"]["game"]["manifest"]["sha1"] = hashlib.sha1(payload_bytes).hexdigest()
    wired.save(record)
    wired.upstream.files[manifest_path] = payload_bytes

    code, payload, _ = install(run, wired, tmp_path / "server")

    assert code == 5
    assert "manifest names server sha1" in payload["error"]


def test_an_install_path_that_escapes_the_destination_is_refused(run: Any, wired: Any, tmp_path: Path) -> None:
    dest = tmp_path / "server"
    outside = tmp_path / "escaped.jar"
    record = wired.target()
    record["inputs"]["fabricApi"]["installPath"] = "../escaped.jar"
    wired.save(record)

    code, payload, _ = install(run, wired, dest)

    assert code == 2
    assert "relative path inside the install directory" in payload["error"]
    assert not outside.exists()
    assert tree(dest) == {}


def test_an_absolute_install_path_is_refused(run: Any, wired: Any, tmp_path: Path) -> None:
    dest = tmp_path / "server"
    outside = tmp_path / "absolute.jar"
    record = wired.target()
    record["inputs"]["fabricApi"]["installPath"] = str(outside)
    wired.save(record)

    code, payload, _ = install(run, wired, dest)

    assert code == 2
    assert "relative path inside the install directory" in payload["error"]
    assert not outside.exists()
