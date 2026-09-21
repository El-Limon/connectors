"""The seams an upstream that is not Mojang needs from the shared tracker core.

Two things decide whether a new provider can file honest issues without editing the
tracker: how its observation *reads* (title, intro, the Observation table, the Readiness
sentence, the next steps) and what "the catalog already ships this" *means* for it. Both
are asked of the provider here, and both are exercised through the real ``scan`` command
against providers discovered exactly like the real ones — their modules are dropped on
``takaro_maint.providers.__path__`` and their games written into a copy of the catalog.
Nothing in the tracker names them, and nothing in it names Mojang either.
"""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from fake_github import FakeGitHub
from takaro_maint import readiness
from takaro_maint.providers.base import Observation, ProviderResult
from takaro_maint.tracker import identity, issues

REPO = "gettakaro/connectors"
TOKEN = "token-for-tests"

STUB_SOURCE = '''
"""An observing provider that renders its own issues. Used by the seam tests only."""

from __future__ import annotations

from typing import Any

from takaro_maint import observations
from takaro_maint.providers.base import Observation, Provider, ProviderResult
from takaro_maint.tracker import identity, issues

INTRO = "The stub upstream promoted a build; leave the first line of this body where it is."


class StubProvider(Provider):
    """Heads only: what the watch block declares is what this upstream currently serves."""

    id = "stubprov"

    def observe(self, source: dict[str, Any]) -> ProviderResult:
        watch = source.get("watch") or {}
        component = str(watch.get("component") or source.get("game") or "")
        observed_at = observations.utcnow()
        heads = sorted((watch.get("heads") or {}).items())
        return ProviderResult(
            source_id=str(source.get("id") or self.id),
            status="ok",
            heads={str(branch): str(head["rev"]) for branch, head in heads},
            observations=[
                Observation(
                    provider=self.id,
                    component=component,
                    branch=str(branch),
                    rev=str(head["rev"]),
                    kind="game",
                    identity=identity.canonical(self.id, component, str(branch), str(head["rev"])),
                    facts={"build": str(head["build"]), "releaseTime": str(head["releaseTime"])},
                    observed_at=observed_at,
                )
                for branch, head in heads
            ],
            history="heads-only",
        )

    def presentation(self, observation: Observation, game_name: str) -> dict[str, Any]:
        build = observation.facts["build"]
        return {
            "title": f"{game_name}: build {build} on branch {observation.branch}",
            "intro": INTRO,
            "observationRows": issues.observation_rows(observation, [f"| Build | `{build}` |"]),
            "readinessLines": ["The stub upstream ships no frameworks."],
            "nextSteps": issues.next_steps(observation, pin=f"pin build {build}"),
        }

    def covers(self, observation: Observation, targets: list[Any]) -> bool | None:
        """A build id, not a version string: the revision comparison would file nonsense."""
        build = observation.facts["build"]
        return any(
            str(((target.record.get("inputs") or {}).get("server") or {}).get("buildid")) == build
            for target in targets
        )


PROVIDER = StubProvider()
'''

PLAIN_SOURCE = '''
"""An observing provider with no opinion about anything. Used by the seam tests only."""

from __future__ import annotations

from takaro_maint.providers.base import Provider
from takaro_maint.providers.stubprov import StubProvider


class PlainProvider(StubProvider):
    """Observes the same way, and answers neither ``presentation`` nor ``covers``."""

    id = "plainprov"
    presentation = Provider.presentation
    covers = Provider.covers


PROVIDER = PlainProvider()
'''


def _game_record(game_id: str, name: str, provider: str, heads: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "id": game_id,
        "name": name,
        "connector": game_id,
        "platforms": ["linux"],
        "sources": {
            "upstream": {
                "provider": provider,
                "baseUrl": "https://upstream.invalid",
                "watch": {"kind": "game", "component": game_id, "heads": heads},
            }
        },
    }


def _target_record(game_id: str, revision: str, buildid: int) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "id": f"linux-{revision}",
        "game": game_id,
        "platform": "linux",
        "revision": revision,
        "default": True,
        "support": {"status": "maintained", "since": "2026-09-21", "evidence": ["the seam tests"]},
        "inputs": {"server": {"kind": "steam-depots", "app": 42, "branch": "public", "buildid": buildid}},
    }


def _write(root: Path, game: dict[str, Any], target: dict[str, Any] | None = None) -> None:
    directory = root / "catalog" / str(game["id"])
    (directory / "targets").mkdir(parents=True, exist_ok=True)
    (directory / "game.json").write_text(json.dumps(game, indent=2) + "\n", encoding="utf-8")
    if target is not None:
        path = directory / "targets" / f"{target['id']}.json"
        path.write_text(json.dumps(target, indent=2) + "\n", encoding="utf-8")


def _head(rev: str, build: str) -> dict[str, Any]:
    return {"release": {"rev": rev, "build": build, "releaseTime": "2026-09-17T09:00:00Z"}}


@pytest.fixture
def stub_providers(tmp_path: Path) -> Iterator[None]:
    """Two discovered providers, added to and removed from the real registry."""
    from takaro_maint import providers

    extra = tmp_path / "providers-extra"
    extra.mkdir(parents=True)
    (extra / "stubprov.py").write_text(STUB_SOURCE, encoding="utf-8")
    (extra / "plainprov.py").write_text(PLAIN_SOURCE, encoding="utf-8")

    providers.__path__.append(str(extra))
    providers.providers.cache_clear()
    importlib.invalidate_caches()
    try:
        yield
    finally:
        providers.__path__.remove(str(extra))
        providers.providers.cache_clear()
        for name in [
            n
            for n in sys.modules
            if n.startswith(("takaro_maint.providers.stubprov", "takaro_maint.providers.plainprov"))
        ]:
            del sys.modules[name]


@contextmanager
def tracker(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHub]:
    with FakeGitHub() as fake:
        monkeypatch.setenv("GH_TOKEN", TOKEN)
        yield fake


def scan(run: Any, root: Path, fake: FakeGitHub, game: str, *flags: str) -> tuple[int, Any, str]:
    return run("scan", "--repo", REPO, "--api-url", fake.api_url, "--game", game, *flags, repo=root)


def support_issues(fake: FakeGitHub) -> list[dict[str, Any]]:
    return [
        issue
        for issue in fake.issues
        if (identity.parse_marker(str(issue.get("body") or "")) or {}).get("kind") == "support"
    ]


# -- how an observation reads -------------------------------------------------
def test_a_provider_renders_the_issue_its_own_upstream_needs(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch, stub_providers: None
) -> None:
    _write(catalog_copy, _game_record("stubgame", "Stub Game", "stubprov", _head("public-888", "888")))

    with tracker(monkeypatch) as fake:
        code, payload, stderr = scan(run, catalog_copy, fake, "stubgame", "--bootstrap", "--publish")

        assert code == 0, stderr
        assert [entry["action"] for entry in payload["applied"]] == ["create-issue", "create-dashboard"]
        filed = support_issues(fake)[0]
        body = str(filed["body"])

        assert filed["title"] == "Stub Game: build 888 on branch release"
        assert "The stub upstream promoted a build" in body
        assert "| Build | `888` |" in body
        assert "| Provider / component / branch | stubprov / stubgame / release |" in body
        assert "The stub upstream ships no frameworks." in body
        assert '→ "Adding a target" (pin build 888).' in body
        # None of the Mojang rendering leaks into an upstream that has nothing to do with it.
        assert "Version manifest" not in body
        assert "Mojang" not in body
        assert issues.READINESS_SENTENCE not in body


def test_a_provider_with_no_opinion_gets_the_generic_issue(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch, stub_providers: None
) -> None:
    _write(catalog_copy, _game_record("plaingame", "Plain Game", "plainprov", _head("2.0", "12")))

    with tracker(monkeypatch) as fake:
        code, _, stderr = scan(run, catalog_copy, fake, "plaingame", "--bootstrap", "--publish")

        assert code == 0, stderr
        filed = support_issues(fake)[0]
        body = str(filed["body"])

        assert filed["title"] == "Plain Game 2.0: new stable release needs a target"
        assert issues.INTRO in body
        assert issues.READINESS_SENTENCE in body
        assert "| Released | 2026-09-17T09:00:00Z |" in body
        assert '→ "Adding a target".' in body
        assert "Mojang" not in body
        assert "Version manifest" not in body


def test_an_observation_from_a_provider_this_build_does_not_carry_still_renders() -> None:
    """The lookup is by id: a body is never held hostage to a provider being installed."""
    observation = Observation(
        provider="nosuchprov",
        component="somegame",
        branch="release",
        rev="7",
        kind="game",
        identity=identity.canonical("nosuchprov", "somegame", "release", "7"),
        facts={"releaseTime": "2026-09-17T09:00:00Z"},
        observed_at="2026-09-17T12:00:00Z",
    )
    targets = issues.GameTargets(name="Some Game", platforms=["linux"])

    block = issues.render_owned_block(observation, targets)

    assert issues.title_for(observation, "Some Game") == "Some Game 7: new stable release needs a target"
    assert "| Provider / component / branch | nosuchprov / somegame / release |" in block
    assert issues.READINESS_SENTENCE in block


def test_the_issue_renderer_names_no_upstream() -> None:
    """What makes the next game a provider change and not a tracker change."""
    for module in (issues, __import__("takaro_maint.commands.scan", fromlist=["scan"])):
        source = Path(module.__file__ or "").read_text(encoding="utf-8").lower()
        assert "mojang" not in source
        assert "minecraft" not in source


# -- what "already shipped" means ---------------------------------------------
def test_bootstrap_files_nothing_for_a_build_the_catalog_already_ships(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch, stub_providers: None
) -> None:
    """The head's revision matches no target revision; the provider says it is shipped anyway."""
    _write(
        catalog_copy,
        _game_record("stubgame", "Stub Game", "stubprov", _head("public-777", "777")),
        _target_record("stubgame", "1.0", 777),
    )

    with tracker(monkeypatch) as fake:
        code, payload, stderr = scan(run, catalog_copy, fake, "stubgame", "--bootstrap", "--publish")

        assert code == 0, stderr
        assert support_issues(fake) == []
        assert [entry["action"] for entry in payload["applied"]] == ["create-dashboard"]
        seeded = [entry for entry in payload["plan"] if entry["action"] == "bootstrap-checkpoint"]
        assert seeded[0]["seeded"] == 1


def test_without_the_hook_bootstrap_still_compares_revisions(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch, stub_providers: None
) -> None:
    _write(
        catalog_copy,
        _game_record("plaingame", "Plain Game", "plainprov", _head("1.0", "12")),
        _target_record("plaingame", "1.0", 999),
    )

    with tracker(monkeypatch) as fake:
        code, payload, stderr = scan(run, catalog_copy, fake, "plaingame", "--bootstrap", "--publish")

        assert code == 0, stderr
        assert support_issues(fake) == []
        assert [entry["action"] for entry in payload["applied"]] == ["create-dashboard"]


# -- readiness belongs to the game that has those platforms -------------------
def test_readiness_rows_ignore_a_framework_another_game_observed() -> None:
    """A Fabric listing is no evidence at all about a game with no Fabric platform."""
    readiness.reset_registry()
    readiness.registry().record(
        watch={"component": "fabric", "game": {"provider": "mojang", "component": "minecraft"}},
        result=ProviderResult(source_id="minecraft/fabric-meta", status="ok"),
    )
    try:
        assert readiness.rows_from_registry("1.0", ["linux"], branch="release") is None

        observed = readiness.rows_from_registry("26.3", ["fabric"], branch="release")
        assert observed is not None
        assert observed["fabric"].status == readiness.MISSING
    finally:
        readiness.reset_registry()
