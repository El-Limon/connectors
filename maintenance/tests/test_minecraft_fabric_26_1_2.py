"""The Fabric 26.1.2 backport target: its pins, and that it is selectable on its own.

Every assertion drives the real command, so what is checked is the behaviour a release
and the rig actually see: the record's pins, that 26.2 stays the default, and that an
artifact built for one Fabric target is refused by the other.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import FIXTURES, Wired, make_jar, point_at, read_target, sha1, sha256, write_target
from fake_upstream import FakeUpstream
from test_cli_build import artifact_spec
from test_cli_build import gradle_stub as _gradle_stub

# The Fabric build stub is the same one the single-target build tests use; re-exporting it
# under its own name is how pytest finds a fixture defined in another test module.
gradle_stub = _gradle_stub

BACKPORT = "fabric-26.1.2"
CURRENT = "fabric-26.2"
EXPECTED_ENV_KEYS = {
    "MC_FABRIC_26_1_2_TARGET",
    "MC_FABRIC_26_1_2_FINGERPRINT",
    "MC_FABRIC_26_1_2_FP16",
    "MC_FABRIC_26_1_2_IMAGE",
    "MC_FABRIC_26_1_2_JAVA",
    "MC_FABRIC_26_1_2_VERSION",
    "MC_FABRIC_26_1_2_LOADER_VERSION",
    "MC_FABRIC_26_1_2_LAUNCHER_VERSION",
    "MC_FABRIC_26_1_2_LAUNCHER",
}


def pinned_loader(repo_root: Path) -> str:
    """The loader the record actually declares, so these tests follow the pin."""
    return str(read_target(repo_root, BACKPORT)["inputs"]["loader"]["loaderVersion"])


def fingerprint(run: Any, target_id: str, repo: Path | None = None) -> str:
    _, payload, _ = run("targets", "resolve", "--game", "minecraft", "--target", target_id, repo=repo)
    return str(payload["fingerprint"])


@pytest.fixture
def wired_backport(catalog_copy: Path) -> Any:
    """Serve stand-ins for the four 26.1.2 downloads and re-pin the copied record to them.

    `conftest.wired` does this for the default target; the backport needs its own because
    both records live in the same catalog and only one of them may be re-pinned.
    """
    with FakeUpstream() as upstream:
        record = read_target(catalog_copy, BACKPORT)
        loader_version = str(record["inputs"]["loader"]["loaderVersion"])
        api_version = str(record["inputs"]["fabricApi"]["version"])

        server_bytes = (FIXTURES / "upstream/mojang/26.1.2/server.jar").read_bytes()
        launcher_bytes = (FIXTURES / "upstream/fabric/26.1.2/launcher.jar").read_bytes()
        api_bytes = (FIXTURES / f"upstream/fabric/26.1.2/fabric-api-{api_version}.jar").read_bytes()
        loader_bytes = (FIXTURES / f"upstream/fabric/26.1.2/fabric-loader-{loader_version}.jar").read_bytes()

        manifest = json.loads((FIXTURES / "upstream/mojang/26.1.2/26.1.2.json").read_text())
        manifest["downloads"]["server"] = {
            "sha1": sha1(server_bytes),
            "size": len(server_bytes),
            "url": upstream.base_url + record["inputs"]["game"]["server"]["path"],
        }
        manifest_bytes = json.dumps(manifest).encode("utf-8")

        game = record["inputs"]["game"]
        game["manifest"]["path"] = f"/v1/packages/{sha1(manifest_bytes)}/26.1.2.json"
        game["manifest"]["sha1"] = sha1(manifest_bytes)
        game["server"]["sha1"] = sha1(server_bytes)
        game["server"]["size"] = len(server_bytes)
        record["inputs"]["loader"]["sha256"] = sha256(launcher_bytes)
        record["inputs"]["fabricApi"]["sha256"] = sha256(api_bytes)
        record["build"]["deps"]["fabric-api"]["sha256"] = sha256(api_bytes)
        record["build"]["deps"]["fabric-loader"]["sha256"] = sha256(loader_bytes)
        write_target(catalog_copy, record, BACKPORT)
        point_at(catalog_copy, upstream.base_url)

        upstream.add(game["manifest"]["path"], manifest_bytes)
        upstream.add(game["server"]["path"], server_bytes)
        upstream.add(record["inputs"]["loader"]["path"], launcher_bytes)
        upstream.add(record["inputs"]["fabricApi"]["path"], api_bytes)
        upstream.add(f"/net/fabricmc/fabric-loader/{loader_version}/fabric-loader-{loader_version}.jar", loader_bytes)
        yield Wired(catalog_copy, upstream)


def test_the_record_pins_the_verified_upstream_values(repo_root: Path) -> None:
    record = read_target(repo_root, BACKPORT)
    loader = record["inputs"]["loader"]
    game = record["inputs"]["game"]
    current = read_target(repo_root, CURRENT)

    assert record["revision"] == "26.1.2"
    assert record["default"] is False
    assert record["support"]["status"] in {"candidate", "maintained"}

    assert game["manifest"]["sha1"] == "0b5a79a91e76c2df466c79fb04fd1f355836dea0"
    assert game["server"]["sha1"] == "97ccd4c0ed3f81bbb7bfacddd1090b0c56f9bc51"
    assert game["server"]["size"] == 60417480
    assert game["javaMajor"] == 25

    # The loader is whatever a boot demonstrated; every place that repeats it must agree.
    version = loader["loaderVersion"]
    assert record["runtime"]["container"]["env"]["FABRIC_LOADER_VERSION"] == version
    assert loader["installPath"] == f"fabric-server-mc.26.1.2-loader.{version}-launcher.1.1.2.jar"
    assert record["build"]["deps"]["fabric-loader"]["coordinate"] == f"net.fabricmc:fabric-loader:{version}"
    assert loader["launcherVersion"] == "1.1.2"
    assert record["inputs"]["fabricApi"]["version"] == "0.155.3+26.1.2"

    for field in (
        loader["sha256"],
        record["inputs"]["fabricApi"]["sha256"],
        record["build"]["deps"]["fabric-api"]["sha256"],
        record["build"]["deps"]["fabric-loader"]["sha256"],
    ):
        assert isinstance(field, str)
        assert len(field) == 64
        assert set(field) <= set("0123456789abcdef")

    assert record["devServers"]["gameId"] == "minecraft-fabric-26.1.2"
    # The 26.x line shares one Java 25 image pair; only the env block differs.
    assert record["runtime"]["container"]["image"] == current["runtime"]["container"]["image"]
    assert record["runtime"]["container"]["tag"] == current["runtime"]["container"]["tag"]
    assert record["runtime"]["container"]["digest"] == current["runtime"]["container"]["digest"]
    assert record["build"]["toolchain"] == current["build"]["toolchain"]


def test_both_fabric_targets_validate_together(run: Any) -> None:
    code, payload, _ = run("catalog", "validate")

    assert code == 0, payload
    assert payload["failures"] == []
    files = {check["file"] for check in payload["checks"]}
    assert f"catalog/minecraft/targets/{BACKPORT}.json" in files
    assert f"catalog/minecraft/targets/{CURRENT}.json" in files


def test_the_default_is_still_26_2(run: Any) -> None:
    code, payload, _ = run("targets", "resolve", "--game", "minecraft")

    assert code == 0
    assert payload["id"] == CURRENT

    code, payload, _ = run("targets", "resolve", "--game", "minecraft", "--target", BACKPORT)

    assert code == 0
    assert payload["id"] == BACKPORT
    assert payload["default"] is False


def test_each_rig_game_selects_exactly_its_own_target(run: Any) -> None:
    code, payload, _ = run("targets", "list", "--rig-game", "minecraft-fabric-26.1.2")

    assert code == 0
    assert [row["id"] for row in payload["targets"]] == [BACKPORT]

    code, payload, _ = run("targets", "list", "--rig-game", "minecraft-fabric")

    assert code == 0
    assert [row["id"] for row in payload["targets"]] == [CURRENT]


def test_the_two_targets_have_distinct_identities(run: Any, repo_root: Path) -> None:
    _, backport, _ = run("targets", "resolve", "--game", "minecraft", "--target", BACKPORT)
    _, current, _ = run("targets", "resolve", "--game", "minecraft", "--target", CURRENT)
    version = pinned_loader(repo_root)

    assert backport["fingerprint"] != current["fingerprint"]
    assert backport["artifactFileNames"]["server-mod"] == "takaro-minecraft-mod-fabric-26.1.2-{version}.jar"
    assert backport["resolvedUrls"]["loader"].endswith(f"/26.1.2/{version}/1.1.2/server/jar")
    assert "97ccd4c0" in backport["resolvedUrls"]["game.server"]


def test_resolve_env_for_the_rig_carries_the_backport_pins(run: Any, repo_root: Path) -> None:
    code, payload, _ = run(
        "targets",
        "resolve",
        "--game",
        "minecraft",
        "--target",
        BACKPORT,
        "--format",
        "env",
        "--prefix",
        "MC_FABRIC_26_1_2",
    )

    assert code == 0
    values = dict(line.split("=", 1) for line in payload.strip().splitlines())
    version = pinned_loader(repo_root)

    assert set(values) == EXPECTED_ENV_KEYS
    assert values["MC_FABRIC_26_1_2_TARGET"] == BACKPORT
    assert values["MC_FABRIC_26_1_2_VERSION"] == "26.1.2"
    assert values["MC_FABRIC_26_1_2_LOADER_VERSION"] == version
    assert values["MC_FABRIC_26_1_2_LAUNCHER"] == f"fabric-server-mc.26.1.2-loader.{version}-launcher.1.1.2.jar"


def test_a_jar_for_the_other_fabric_target_is_rejected_both_ways(run: Any, catalog_copy: Path, tmp_path: Path) -> None:
    backport_fp = fingerprint(run, BACKPORT, catalog_copy)
    current_fp = fingerprint(run, CURRENT, catalog_copy)

    current_jar = make_jar(
        tmp_path / "takaro-minecraft-mod-fabric-26.2-0.1.1.jar",
        target=CURRENT,
        fingerprint=current_fp,
        revision="26.2",
    )
    backport_jar = make_jar(
        tmp_path / "takaro-minecraft-mod-fabric-26.1.2-0.1.1.jar",
        target=BACKPORT,
        fingerprint=backport_fp,
        revision="26.1.2",
    )

    code, payload, _ = run(
        "artifact", "validate", "--game", "minecraft", "--target", BACKPORT, str(current_jar), repo=catalog_copy
    )

    assert code == 7
    problems = payload["files"][0]["problems"]
    assert any("Takaro-Target" in problem for problem in problems)

    code, payload, _ = run(
        "artifact", "validate", "--game", "minecraft", "--target", CURRENT, str(backport_jar), repo=catalog_copy
    )

    assert code == 7
    assert any("Takaro-Target" in problem for problem in payload["files"][0]["problems"])

    # Each jar still validates against the target it was stamped for.
    assert (
        run("artifact", "validate", "--game", "minecraft", "--target", BACKPORT, str(backport_jar), repo=catalog_copy)[
            0
        ]
        == 0
    )


def both_target_jars(run: Any, repo: Path) -> dict[str, dict[str, Any]]:
    """What the gradle stub writes for a build that selects both Fabric targets."""
    return {
        f"games/minecraft/mod/targets/{CURRENT}/build/libs/takaro-minecraft-mod-fabric-26.2-0.1.1.jar": artifact_spec(
            fingerprint(run, CURRENT, repo)
        ),
        (
            f"games/minecraft/mod/targets/{BACKPORT}/build/libs/takaro-minecraft-mod-fabric-26.1.2-0.1.1.jar"
        ): artifact_spec(fingerprint(run, BACKPORT, repo), target=BACKPORT, revision="26.1.2"),
    }


def test_build_selects_both_targets_with_independent_outputs(
    run: Any, catalog_copy: Path, gradle_stub: Any, tmp_path: Path
) -> None:
    gradle_stub(catalog_copy, both_target_jars(run, catalog_copy))
    out = tmp_path / "dist"

    code, payload, _ = run(
        "build",
        "--game",
        "minecraft",
        "--target",
        BACKPORT,
        "--target",
        CURRENT,
        "--version",
        "0.1.1",
        "--out",
        str(out),
        repo=catalog_copy,
    )

    assert code == 0, payload
    rows = payload["artifacts"]
    assert {row["target"] for row in rows} == {BACKPORT, CURRENT}
    assert len({row["fingerprint"] for row in rows}) == 2
    assert len((out / "SHA256SUMS").read_text().strip().splitlines()) == 2

    gradle_stub(catalog_copy, both_target_jars(run, catalog_copy))
    alone = tmp_path / "dist-backport"

    code, payload, _ = run(
        "build",
        "--game",
        "minecraft",
        "--target",
        BACKPORT,
        "--version",
        "0.1.1",
        "--out",
        str(alone),
        repo=catalog_copy,
    )

    assert code == 0, payload
    assert [row["target"] for row in payload["artifacts"]] == [BACKPORT]
    assert sorted(p.name for p in alone.glob("*.jar")) == ["takaro-minecraft-mod-fabric-26.1.2-0.1.1.jar"]


def test_install_lays_out_the_26_1_2_inputs(run: Any, wired_backport: Any, tmp_path: Path) -> None:
    root = wired_backport.root
    record = read_target(root, BACKPORT)
    dest = tmp_path / "server"

    code, payload, _ = run("install", "--game", "minecraft", "--target", BACKPORT, "--dest", str(dest), repo=root)

    assert code == 0, payload
    launcher = record["inputs"]["loader"]["installPath"]
    api = record["inputs"]["fabricApi"]["installPath"]
    assert (dest / "server.jar").read_bytes() == (FIXTURES / "upstream/mojang/26.1.2/server.jar").read_bytes()
    assert (dest / launcher).read_bytes() == (FIXTURES / "upstream/fabric/26.1.2/launcher.jar").read_bytes()
    assert (dest / api).is_file()
    assert api == "mods/fabric-api-0.155.3+26.1.2.jar"

    ledger = json.loads((dest / ".takaro/installed-target.json").read_text())
    assert ledger["target"] == BACKPORT

    assert run("ledger", "check", "--game", "minecraft", "--target", BACKPORT, "--dest", str(dest), repo=root)[0] == 0
    assert run("ledger", "check", "--game", "minecraft", "--target", CURRENT, "--dest", str(dest), repo=root)[0] == 7


def test_deploying_the_26_2_row_into_a_26_1_2_install_is_a_conflict(
    run: Any, wired_backport: Any, gradle_stub: Any, tmp_path: Path
) -> None:
    root = wired_backport.root
    dest = tmp_path / "server"
    assert run("install", "--game", "minecraft", "--target", BACKPORT, "--dest", str(dest), repo=root)[0] == 0

    gradle_stub(root, both_target_jars(run, root))
    dist = tmp_path / "dist"
    assert (
        run(
            "build",
            "--game",
            "minecraft",
            "--target",
            BACKPORT,
            "--target",
            CURRENT,
            "--version",
            "0.1.1",
            "--out",
            str(dist),
            repo=root,
        )[0]
        == 0
    )

    code, payload, _ = run(
        "deploy",
        "--game",
        "minecraft",
        "--target",
        CURRENT,
        "--dest",
        str(dest),
        "--from",
        str(dist / "build-manifest.json"),
        repo=root,
    )

    assert code == 7
    assert BACKPORT in payload["error"]

    code, payload, _ = run(
        "deploy",
        "--game",
        "minecraft",
        "--target",
        BACKPORT,
        "--dest",
        str(dest),
        "--from",
        str(dist / "build-manifest.json"),
        repo=root,
    )

    assert code == 0, payload
    assert (dest / "mods/takaro-minecraft-mod-fabric-26.1.2-0.1.1.jar").is_file()
    assert [p.name for p in (dest / "mods").iterdir() if "fabric-26.2" in p.name] == []


def test_the_docs_block_lists_both_targets(run: Any, repo_root: Path) -> None:
    code, payload, _ = run("docs", "render", "--game", "minecraft")
    version = pinned_loader(repo_root)
    status = read_target(repo_root, BACKPORT)["support"]["status"]

    assert code == 0
    assert (
        f"| `{BACKPORT}` | 26.1.2 | fabric | loader {version} / API 0.155.3+26.1.2 | 25 | {status} | protocol |"
        in payload["block"]
    )
    assert f"| `{CURRENT}` | 26.2 | fabric |" in payload["block"]


def test_the_gradle_project_shares_the_fabric_recipe(repo_root: Path) -> None:
    project = repo_root / "games/minecraft/mod/targets" / BACKPORT
    build_file = project / "build.gradle.kts"

    assert build_file.is_file()
    text = build_file.read_text()
    assert 'id("takaro.target-base")' in text
    assert "libs.plugins.fabric.loom" in text

    # With no overlay sources the recipe is shared verbatim; an overlay (a demonstrated
    # API difference) is the only reason for this file to diverge from the 26.2 one.
    if not (project / "src").exists():
        current = (repo_root / "games/minecraft/mod/targets" / CURRENT / "build.gradle.kts").read_bytes()
        assert build_file.read_bytes() == current
