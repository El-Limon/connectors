"""`artifact validate`: a file has to carry the identity of the target it claims."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from conftest import make_jar


def fingerprint_of(run: Any) -> str:
    _, payload, _ = run("targets", "resolve", "--game", "minecraft", "--target", "fabric-26.2")
    return str(payload["fingerprint"])


def validate(run: Any, *files: Path) -> tuple[int, Any, str]:
    return run("artifact", "validate", "--game", "minecraft", "--target", "fabric-26.2", *[str(f) for f in files])


def test_a_correctly_stamped_jar_validates(run: Any, tmp_path: Path) -> None:
    jar = make_jar(
        tmp_path / "takaro-minecraft-mod-fabric-26.2-0.1.1.jar",
        target="fabric-26.2",
        fingerprint=fingerprint_of(run),
        revision="26.2",
    )

    code, payload, _ = validate(run, jar)

    assert code == 0, payload
    assert payload["files"][0]["ok"] is True


def test_a_jar_stamped_with_another_target_is_a_conflict(run: Any, tmp_path: Path) -> None:
    jar = make_jar(
        tmp_path / "other.jar",
        target="fabric-26.1.2",
        fingerprint=fingerprint_of(run),
        revision="26.2",
    )

    code, payload, _ = validate(run, jar)

    assert code == 7
    assert any("Takaro-Target" in problem for problem in payload["files"][0]["problems"])


def test_a_jar_with_a_stale_fingerprint_is_a_conflict(run: Any, tmp_path: Path) -> None:
    jar = make_jar(
        tmp_path / "stale.jar",
        target="fabric-26.2",
        fingerprint="0" * 64,
        revision="26.2",
    )

    code, payload, _ = validate(run, jar)

    assert code == 7
    assert any("Fingerprint" in problem for problem in payload["files"][0]["problems"])


def test_a_jar_for_another_game_version_is_a_conflict(run: Any, tmp_path: Path) -> None:
    jar = make_jar(
        tmp_path / "wrong-game.jar",
        target="fabric-26.2",
        fingerprint=fingerprint_of(run),
        revision="26.1.2",
    )

    code, payload, _ = validate(run, jar)

    assert code == 7
    assert any("Takaro-Game-Version" in problem for problem in payload["files"][0]["problems"])


def test_a_jar_without_the_target_json_is_a_conflict(run: Any, tmp_path: Path) -> None:
    jar = make_jar(
        tmp_path / "unstamped.jar",
        target="fabric-26.2",
        fingerprint=fingerprint_of(run),
        revision="26.2",
        stamp=False,
    )

    code, payload, _ = validate(run, jar)

    assert code == 7
    assert any("takaro-target.json" in problem for problem in payload["files"][0]["problems"])


def test_a_jar_missing_a_manifest_attribute_is_a_conflict(run: Any, tmp_path: Path) -> None:
    jar = make_jar(
        tmp_path / "no-version.jar",
        target="fabric-26.2",
        fingerprint=fingerprint_of(run),
        revision="26.2",
        attributes={"Takaro-Connector-Version": ""},
    )

    code, payload, _ = validate(run, jar)

    assert code == 7
    assert any("Takaro-Connector-Version" in problem for problem in payload["files"][0]["problems"])


def test_a_file_that_is_not_a_zip_is_a_conflict(run: Any, tmp_path: Path) -> None:
    jar = tmp_path / "broken.jar"
    jar.write_bytes(b"this is not a jar at all")

    code, payload, _ = validate(run, jar)

    assert code == 7
    assert payload["files"][0]["ok"] is False


def test_a_non_jar_needs_a_meta_json_beside_it(run: Any, tmp_path: Path) -> None:
    payload_file = tmp_path / "takaro.zip"
    payload_file.write_bytes(b"a packaged sidecar, say")

    code, payload, _ = validate(run, payload_file)

    assert code == 7
    assert "meta.json" in payload["files"][0]["problems"][0]


def test_a_non_jar_with_a_matching_meta_json_validates(run: Any, tmp_path: Path) -> None:
    payload_file = tmp_path / "takaro.zip"
    payload_file.write_bytes(b"a packaged sidecar, say")
    (tmp_path / "takaro.zip.meta.json").write_text(
        json.dumps({"target": "fabric-26.2", "fingerprint": fingerprint_of(run)})
    )

    code, payload, _ = validate(run, payload_file)

    assert code == 0, payload


def test_several_files_are_all_reported(run: Any, tmp_path: Path) -> None:
    good = make_jar(tmp_path / "good.jar", target="fabric-26.2", fingerprint=fingerprint_of(run), revision="26.2")
    bad = make_jar(tmp_path / "bad.jar", target="fabric-26.1.2", fingerprint="0" * 64, revision="26.2")

    code, payload, _ = validate(run, good, bad)

    assert code == 7
    assert [entry["ok"] for entry in payload["files"]] == [True, False]


def test_a_missing_file_is_reported_rather_than_crashing(run: Any, tmp_path: Path) -> None:
    code, payload, _ = validate(run, tmp_path / "not-here.jar")

    assert code == 7
    assert "missing" in payload["files"][0]["problems"][0]


def test_a_stamp_that_disagrees_with_the_manifest_is_a_conflict(run: Any, tmp_path: Path) -> None:
    jar = tmp_path / "mixed.jar"
    fingerprint = fingerprint_of(run)
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr(
            "META-INF/MANIFEST.MF",
            "Manifest-Version: 1.0\n"
            "Takaro-Target: fabric-26.2\n"
            f"Takaro-Target-Fingerprint: {fingerprint}\n"
            "Takaro-Connector-Version: 0.1.1\n"
            "Takaro-Source-Revision: deadbeef\n"
            "Takaro-Game-Version: 26.2\n\n",
        )
        archive.writestr(
            "META-INF/takaro-target.json",
            json.dumps({"target": "fabric-26.2", "fingerprint": "0" * 64}),
        )

    code, payload, _ = validate(run, jar)

    assert code == 7
    assert any("disagrees with the catalog" in problem for problem in payload["files"][0]["problems"])
