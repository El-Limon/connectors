"""The provider contract a new game reuses: input kinds and the generic GitHub release provider.

Every game after 7 Days to Die is meant to onboard with catalog data alone — a source, a
watch block and one pinned target — and never by teaching the core Python about itself.
These tests hold that line from the outside: the input kinds are checked through the real
``catalog validate``, and the release provider is exercised through the real commands
against a fixture listing and an in-process upstream.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import read_target, write_target
from takaro_maint.catalog import schema

VALID_RELEASE_ASSET = {
    "kind": "github-release-asset",
    "source": "carbon-releases",
    "hashOrigin": "upstream",
    "repo": "CarbonCommunity/Carbon",
    "tag": "v2.1.1272",
    "asset": "Carbon.Linux.Release.tar.gz",
    "sha256": "0" * 64,
    "size": 1234,
    "installPath": "carbon",
}

VALID_THUNDERSTORE_PACKAGE = {
    "kind": "thunderstore-package",
    "source": "thunderstore",
    "hashOrigin": "upstream",
    "namespace": "denikson",
    "name": "BepInExPack_Valheim",
    "version": "5.4.2202",
    "sha256": "1" * 64,
    "size": 4321,
    "installPath": "BepInExPack_Valheim",
}

VALID_GITHUB_SOURCE = {
    "kind": "github-source",
    "source": "carbon-releases",
    "hashOrigin": "self-recorded",
    "repo": "CarbonCommunity/Carbon",
    "commit": "a" * 40,
    "archive": "tar.gz",
    "sha256": "2" * 64,
}


def errors(kind: str, spec: dict[str, Any]) -> list[str]:
    return schema.errors_for(f"inputs/{kind}.schema.json", spec)


def failures(payload: dict[str, Any]) -> set[str]:
    return {check["id"] for check in payload["failures"]}


def _with_input(root: Path, spec: dict[str, Any]) -> None:
    """Hang one extra input off the default Fabric target of a catalog copy."""
    record = read_target(root)
    record["inputs"]["extra"] = spec
    write_target(root, record)


# -- T-C6 github-release-asset -------------------------------------------------
def test_github_release_asset_schema_accepts_a_pinned_tag_and_rejects_a_malformed_one() -> None:
    assert errors("github-release-asset", VALID_RELEASE_ASSET) == []

    assert errors("github-release-asset", {**VALID_RELEASE_ASSET, "repo": "CarbonCommunity"})
    assert errors("github-release-asset", {**VALID_RELEASE_ASSET, "sha256": "not-a-digest"})
    assert errors("github-release-asset", {**VALID_RELEASE_ASSET, "asset": "dir/Carbon.tar.gz"})
    assert errors("github-release-asset", {k: v for k, v in VALID_RELEASE_ASSET.items() if k != "tag"})
    assert errors("github-release-asset", {**VALID_RELEASE_ASSET, "unexpected": 1})


def test_a_floating_release_tag_fails_catalog_validation(run: Any, catalog_copy: Path) -> None:
    """``latest`` is a moving target: the catalog-wide floating-word check has to catch it."""
    _with_input(catalog_copy, {**VALID_RELEASE_ASSET, "tag": "latest"})

    code, payload, _ = run("catalog", "validate", repo=catalog_copy)

    assert code == 2
    assert "no-floating-words" in failures(payload)
    assert "input-kind-schema" not in failures(payload), "a floating tag is still a well-formed input"


def test_a_pinned_release_asset_passes_every_catalog_check(run: Any, catalog_copy: Path) -> None:
    _with_input(catalog_copy, VALID_RELEASE_ASSET)

    code, payload, _ = run("catalog", "validate", repo=catalog_copy)

    assert code == 0, json.dumps(payload, indent=2)


# -- T-C7 thunderstore-package -------------------------------------------------
def test_thunderstore_package_schema_accepts_a_pinned_version_and_rejects_a_malformed_one() -> None:
    assert errors("thunderstore-package", VALID_THUNDERSTORE_PACKAGE) == []

    assert errors("thunderstore-package", {**VALID_THUNDERSTORE_PACKAGE, "version": "5.4"})
    assert errors("thunderstore-package", {**VALID_THUNDERSTORE_PACKAGE, "namespace": "deni kson"})
    assert errors("thunderstore-package", {k: v for k, v in VALID_THUNDERSTORE_PACKAGE.items() if k != "sha256"})


def test_a_floating_thunderstore_version_fails_catalog_validation(run: Any, catalog_copy: Path) -> None:
    _with_input(catalog_copy, {**VALID_THUNDERSTORE_PACKAGE, "installPath": "latest"})

    code, payload, _ = run("catalog", "validate", repo=catalog_copy)

    assert code == 2
    assert "no-floating-words" in failures(payload)


# -- T-C8 github-source --------------------------------------------------------
def test_github_source_schema_requires_an_exact_commit() -> None:
    assert errors("github-source", VALID_GITHUB_SOURCE) == []

    assert errors("github-source", {**VALID_GITHUB_SOURCE, "commit": "main"})
    assert errors("github-source", {**VALID_GITHUB_SOURCE, "commit": "a" * 7})
    assert errors("github-source", {**VALID_GITHUB_SOURCE, "archive": "7z"})
    assert errors("github-source", {k: v for k, v in VALID_GITHUB_SOURCE.items() if k != "commit"})


def test_a_github_source_archive_passes_every_catalog_check(run: Any, catalog_copy: Path) -> None:
    _with_input(catalog_copy, VALID_GITHUB_SOURCE)

    code, payload, _ = run("catalog", "validate", repo=catalog_copy)

    assert code == 0, json.dumps(payload, indent=2)


def test_the_three_new_kinds_are_registered_with_the_catalog(run: Any, catalog_copy: Path) -> None:
    """The kind gate is the only registry there is: an unknown kind must fail there."""
    _with_input(catalog_copy, {**VALID_RELEASE_ASSET, "kind": "github-release-assets"})

    code, payload, _ = run("catalog", "validate", repo=catalog_copy)

    assert code == 2
    assert "input-kind-schema" in failures(payload)
    for kind in ("github-release-asset", "thunderstore-package", "github-source"):
        assert schema.has_schema(f"inputs/{kind}.schema.json")


# -- the generic github-release provider ---------------------------------------
LISTING_PATH = "/repos/CarbonCommunity/Carbon/releases?per_page=4"
ASSET_PATH = "/CarbonCommunity/Carbon/releases/download/production_build/Carbon.Linux.Release.tar.gz"
RELEASE_ASSET = "^Carbon\\.Linux\\.Release\\.tar\\.gz$"

CARBON_FIXTURE = Path(__file__).parent / "fixtures/providers/github_release/carbon-releases.json"


def carbon_listing() -> list[dict[str, Any]]:
    return json.loads(CARBON_FIXTURE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def release_provider() -> Any:
    from takaro_maint.providers import provider_for

    return provider_for("github-release")


def watch_block(**channel: Any) -> dict[str, Any]:
    return {
        "kind": "game",
        "component": "dummy",
        "repo": "CarbonCommunity/Carbon",
        "window": 4,
        "channels": {"release": {"branch": "release", "asset": RELEASE_ASSET, **channel}},
    }


def source_for(base_url: str, watch: dict[str, Any] | None = None) -> dict[str, Any]:
    source: dict[str, Any] = {"id": "carbon-api", "provider": "github-release", "baseUrl": base_url}
    if watch is not None:
        source["watch"] = watch
    return source


# -- T-C1 acquisition ----------------------------------------------------------
def test_a_release_asset_is_fetched_by_tag_and_verified(tmp_path: Path) -> None:
    from fake_upstream import FakeUpstream
    from takaro_maint.exit_codes import INTEGRITY, UPSTREAM, MaintError

    payload = b"carbon release bytes"
    with FakeUpstream() as upstream:
        digest = upstream.add(ASSET_PATH, payload)
        source = source_for(upstream.base_url)
        spec = {**VALID_RELEASE_ASSET, "tag": "production_build", "sha256": digest, "size": len(payload)}
        dest = tmp_path / "out" / "Carbon.Linux.Release.tar.gz"

        landed = release_provider().fetch_input(spec, source, dest, tmp_path / "cache")
        assert landed.read_bytes() == payload
        assert upstream.requested == [ASSET_PATH]

        with pytest.raises(MaintError) as wrong:
            release_provider().fetch_input({**spec, "sha256": "f" * 64}, source, dest, tmp_path / "cache")
        assert wrong.value.code == INTEGRITY

        # A cold cache, so the asset name really is fetched rather than answered from a
        # blob the first call already stored under this very digest.
        with pytest.raises(MaintError) as missing:
            release_provider().fetch_input(
                {**spec, "asset": "Carbon.Linux.Nothing.tar.gz"}, source, dest, tmp_path / "cold"
            )
        assert missing.value.code == UPSTREAM
        assert "404" in missing.value.message


# -- T-C2 addressing -----------------------------------------------------------
def test_input_url_names_the_release_asset_and_the_source_archive() -> None:
    source = source_for("https://github.com")
    provider = release_provider()

    assert provider.input_url(VALID_RELEASE_ASSET, source) == (
        "https://github.com/CarbonCommunity/Carbon/releases/download/v2.1.1272/Carbon.Linux.Release.tar.gz"
    )
    assert provider.input_url(VALID_GITHUB_SOURCE, source) == (
        f"https://github.com/CarbonCommunity/Carbon/archive/{'a' * 40}.tar.gz"
    )
    assert provider.input_url({**VALID_RELEASE_ASSET, "resolvedCoordinate": "https://pinned/x"}, source) == (
        "https://pinned/x"
    ), "a rolling tag is recorded as the immutable URL it resolved to"
    assert provider.input_url({"kind": "http-file"}, source) is None


# -- T-C3/T-C4 discovery -------------------------------------------------------
def observe_carbon(**channel: Any) -> Any:
    """Observe the recorded listing through the real provider and an in-process upstream."""
    from fake_upstream import FakeUpstream

    with FakeUpstream() as upstream:
        upstream.add_json(LISTING_PATH, carbon_listing())
        return release_provider().observe(source_for(upstream.base_url, watch_block(**channel)))


def test_an_immutable_release_tag_is_observed_once() -> None:
    result = observe_carbon(tag="production_build", versionPattern=r"v([0-9][0-9.]*)")

    assert [o.rev for o in result.observations] == ["2.0.259"]
    assert result.heads == {"release": "2.0.259"}
    assert result.history == "full", "an immutable tag is a complete history, not a head"

    only = result.observations[0]
    assert only.facts["artifact"]["sha256"] == "bfc3cf3d638d588fab94fd4d05a7e8ab2fae28fbbfb5962ecc8fdbd9bb7bb306"
    assert only.facts["artifact"]["size"] == 21387905
    assert only.facts["releaseTime"] == "2026-09-06T13:42:18Z"
    assert only.facts["prerelease"] is False
    assert "observationLimit" not in only.facts


def test_a_mutable_tag_is_pinned_by_its_asset_digest_and_says_so() -> None:
    result = observe_carbon(tag="production_build", mutable=True)

    assert [o.rev for o in result.observations] == ["production_build.bfc3cf3d"]
    assert result.history == "heads-only"
    assert "heads-only" in result.observations[0].facts["observationLimit"]


def test_a_reuploaded_mutable_tag_is_a_new_revision() -> None:
    from fake_upstream import FakeUpstream

    listing = carbon_listing()
    release = next(entry for entry in listing if entry["tag_name"] == "production_build")
    asset = next(item for item in release["assets"] if item["name"] == "Carbon.Linux.Release.tar.gz")
    asset["digest"] = "sha256:" + "ab" * 32
    asset["updated_at"] = "2026-09-22T09:00:00Z"

    with FakeUpstream() as upstream:
        upstream.add_json(LISTING_PATH, listing)
        watch = watch_block(tag="production_build", mutable=True)
        result = release_provider().observe(source_for(upstream.base_url, watch))

    assert [o.rev for o in result.observations] == ["production_build.abababab"]
    assert result.observations[0].facts["releaseTime"] == "2026-09-22T09:00:00Z"


def test_a_prerelease_selector_picks_the_rolling_edge_build() -> None:
    result = observe_carbon(
        tag="regex:^(production|edge)_build$",
        prerelease=True,
        mutable=True,
        asset="^Carbon\\.Linux\\.Debug\\.tar\\.gz$",
    )

    assert [o.rev for o in result.observations] == ["edge_build.068b844f"]
    assert result.observations[0].facts["prerelease"] is True


def test_every_observation_matches_the_observation_schema() -> None:
    """The scan refuses a provider whose observations do not validate; prove they do."""
    from takaro_maint import observations

    result = observe_carbon(tag="production_build", mutable=True)

    for observation in result.observations:
        document = observations.to_json(observation, game="dummy", source_id="carbon-api")
        assert observations.validate(document) == [], document


# -- T-C5 the ambiguous pattern ------------------------------------------------
def test_an_ambiguous_asset_pattern_is_an_upstream_failure() -> None:
    from takaro_maint.exit_codes import UPSTREAM, MaintError

    with pytest.raises(MaintError) as raised:
        observe_carbon(tag="production_build", asset="^Carbon\\.Linux\\..*$")

    assert raised.value.code == UPSTREAM
    assert "4 assets matching" in raised.value.message
    assert "Carbon.Linux.Release.tar.gz" in raised.value.message, "the message lists what it could have meant"


def test_a_broken_channel_pattern_is_a_usage_error_naming_its_key() -> None:
    """A regex comes out of the catalog, so a broken one names the key rather than crashing.

    ``scan`` isolates a source that reports one of its own errors; a raw ``re.error`` would
    escape that handling and abort the whole run, including every unrelated source in it.
    """
    from takaro_maint.exit_codes import USAGE, MaintError

    for key, channel in (
        ("asset", {"asset": "^Carbon\\.Linux\\.(Release$"}),
        ("tag", {"tag": "regex:^(production_build$"}),
        ("versionPattern", {"tag": "production_build", "versionPattern": "v([0-9]+"}),
    ):
        with pytest.raises(MaintError) as raised:
            observe_carbon(**channel)

        assert raised.value.code == USAGE, key
        assert f"watch.channels.release.{key}" in raised.value.message, raised.value.message
        assert "not a valid regular expression" in raised.value.message


def test_a_watch_block_without_a_repo_names_the_missing_key() -> None:
    from takaro_maint.exit_codes import USAGE, MaintError

    with pytest.raises(MaintError) as raised:
        release_provider().observe(source_for("https://api.github.com", {"component": "dummy", "kind": "game"}))

    assert raised.value.code == USAGE
    assert "repo" in raised.value.message


# -- T-C10/T-C11 a game that exists only in the catalog -------------------------
DUMMY_GAME = "dummy"


def dummy_game(root: Path, source: dict[str, Any]) -> Path:
    """A whole game that exists only as catalog data: no adapter, no targets, no code."""
    record = {
        "schemaVersion": 1,
        "id": DUMMY_GAME,
        "name": "Dummy Release Game",
        "connector": DUMMY_GAME,
        "platforms": ["linux"],
        "sources": {source["id"]: {k: v for k, v in source.items() if k != "id"}},
        "build": {"system": "script", "projectDir": "games/dummy"},
        "componentRoles": {
            "server-mod": {"description": "Server mod folder", "artifactPattern": "takaro-dummy-{target}-{version}.zip"}
        },
        "legacyAssetAliases": {},
        "devServers": {"composeFile": "dummy.yml"},
    }
    path = root / "catalog" / DUMMY_GAME / "game.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def test_a_dummy_github_release_watch_scans_with_no_game_specific_code(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brand-new game reaches the tracker by adding catalog data and nothing else."""
    from fake_github import FakeGitHub
    from fake_upstream import FakeUpstream

    with FakeUpstream() as upstream, FakeGitHub() as fake:
        upstream.add_json(LISTING_PATH, carbon_listing())
        watch = {**watch_block(tag="production_build", mutable=True), "component": DUMMY_GAME}
        dummy_game(catalog_copy, source_for(upstream.base_url, watch) | {"id": "carbon-api"})
        monkeypatch.setenv("GH_TOKEN", "token-for-tests")

        code, payload, _ = run(
            "scan",
            "--game",
            DUMMY_GAME,
            "--bootstrap",
            "--repo",
            "gettakaro/connectors",
            "--api-url",
            fake.api_url,
            repo=catalog_copy,
        )

    assert code == 0, payload
    assert payload["mode"] == "read-only"
    assert payload["sources"]["dummy/carbon-api"]["status"] == "ok"
    assert payload["sources"]["dummy/carbon-api"]["history"] == "heads-only"
    assert payload["sources"]["dummy/carbon-api"]["heads"] == {"release": "production_build.bfc3cf3d"}
    assert [observation["rev"] for observation in payload["observations"]] == ["production_build.bfc3cf3d"]
    assert [entry["action"] for entry in payload["plan"]].count("create-issue") == 1
    assert payload["applied"] == [], "read-only writes nothing"
    assert fake.writes == 0


def test_an_ambiguous_asset_pattern_fails_only_its_own_source(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fake_github import FakeGitHub
    from fake_upstream import FakeUpstream

    with FakeUpstream() as upstream, FakeGitHub() as fake:
        upstream.add_json(LISTING_PATH, carbon_listing())
        watch = {
            **watch_block(tag="production_build", asset="^Carbon\\.Linux\\..*$"),
            "component": DUMMY_GAME,
        }
        dummy_game(catalog_copy, source_for(upstream.base_url, watch) | {"id": "carbon-api"})
        monkeypatch.setenv("GH_TOKEN", "token-for-tests")

        code, payload, _ = run(
            "scan",
            "--game",
            DUMMY_GAME,
            "--bootstrap",
            "--repo",
            "gettakaro/connectors",
            "--api-url",
            fake.api_url,
            repo=catalog_copy,
        )

    assert code == 4, payload
    assert payload["sources"]["dummy/carbon-api"]["status"] == "failed"
    assert "assets matching" in payload["sources"]["dummy/carbon-api"]["error"]
    assert payload["observations"] == []


def test_the_dummy_game_needs_no_adapter_and_no_mention_in_the_source() -> None:
    """The executable form of "catalog data only": grep the package for the game's name."""
    from takaro_maint import games

    assert DUMMY_GAME not in games.adapters()

    source_root = Path(games.__file__).resolve().parents[1]
    named = [
        path.relative_to(source_root).as_posix()
        for path in sorted(source_root.rglob("*.py"))
        if DUMMY_GAME in path.read_text(encoding="utf-8")
    ]
    assert named == [], f"the provider contract is generic, but these modules name the game: {named}"


# -- T-C9 a Steam game that exists only in the catalog --------------------------
DUMMY_APP = 999
DUMMY_DEPOT = "9991"


def dummy_app_info() -> dict[str, Any]:
    """One branch, one depot: the smallest app a Steam watch block can describe."""
    return {
        "common": {"name": "Dummy Steam Dedicated Server", "type": "Tool", "oslist": "linux"},
        "depots": {
            DUMMY_DEPOT: {
                "config": {"oslist": "linux"},
                "manifests": {"public": {"gid": "5500000000000000001", "size": "2048", "download": "1024"}},
            },
            "branches": {"public": {"buildid": "1", "timeupdated": "1788200000"}},
            "privatebranches": "0",
        },
    }


def test_a_dummy_steam_game_scans_with_no_game_specific_code(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Steam game reaches the tracker by adding catalog data and nothing else."""
    import fake_steamcmd
    from fake_github import FakeGitHub

    root = tmp_path / "steam-root"
    fake_steamcmd.serve(root, DUMMY_APP, dummy_app_info())
    for name, value in fake_steamcmd.environment(root, tmp_path / "steam-argv.jsonl").items():
        monkeypatch.setenv(name, value)

    with FakeGitHub() as fake:
        dummy_game(
            catalog_copy,
            {
                "id": "steam",
                "provider": "steam",
                "baseUrl": "https://store.steampowered.com",
                "watch": {
                    "kind": "game",
                    "component": DUMMY_GAME,
                    "app": DUMMY_APP,
                    "os": "linux",
                    "depots": [DUMMY_DEPOT],
                    "channels": {"public": {"branch": "public"}},
                },
            },
        )
        monkeypatch.setenv("GH_TOKEN", "token-for-tests")

        code, payload, err = run(
            "scan",
            "--game",
            DUMMY_GAME,
            "--bootstrap",
            "--publish",
            "--repo",
            "gettakaro/connectors",
            "--api-url",
            fake.api_url,
            repo=catalog_copy,
        )

        assert code == 0, err
        assert payload["sources"]["dummy/steam"]["history"] == "heads-only"
        assert [observation["component"] for observation in payload["observations"]] == [DUMMY_GAME]
        filed = [issue for issue in fake.issues if "Dummy" in str(issue["title"])]
        assert [issue["title"] for issue in filed] == ["Dummy Release Game public: build 1 needs a target"]
        assert f"component={DUMMY_GAME}" in str(filed[0]["body"]).splitlines()[0]
        assert "| Depot 9991 manifest | `5500000000000000001` (2048 bytes) |" in str(filed[0]["body"])
