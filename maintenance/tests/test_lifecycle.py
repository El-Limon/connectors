"""The maintenance issue lifecycle, end to end through ``cli.main``.

The rig composes the two fakes this needs — ``FakeGitHub`` serves issues, pulls and repository
contents, ``FakeReleases`` serves releases with real asset bytes — behind one front server that
routes by path, because neither fake can answer for the other and the command talks to both in
one run. Everything else is the ``scan`` rig: a fake Mojang and the three framework listings on
one in-process upstream, a pinned clock, and a token in the environment.

Nothing is stubbed inside the tool. Every scenario runs the real command over real HTTP and
asserts on the exit code, the stdout document, and the bytes that end up in an issue body.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import re
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

import test_readiness_transitions as transitions
import test_scan_support as support
from conftest import read_target
from fake_github import FakeGitHub
from fake_github_releases import FakeReleases
from fake_upstream import FakeUpstream
from takaro_maint import fingerprint as fp
from takaro_maint import readiness
from takaro_maint.catalog import ids
from takaro_maint.publish import compat_record
from takaro_maint.tracker import issues, lifecycle

FIXTURES = Path(__file__).parent / "fixtures" / "lifecycle"

#: Paths the release fake owns. Everything else belongs to the issue fake.
RELEASE_PATHS = re.compile(r"^/(repos/[^/]+/[^/]+/(releases|git)(/|$)|uploads/)")

CONNECTOR = "minecraft"


# -- the composite tracker -----------------------------------------------------
@dataclass
class Tracker:
    """One API endpoint in front of the two fakes, routed by path.

    ``fake_github`` cannot serve asset bytes and ``fake_github_releases`` cannot serve issues,
    and a lifecycle run needs both from the same base URL. Rather than teach either fake the
    other's half, this forwards each request to whichever one owns that path and relays the
    status, body and ``Content-Type`` back unchanged. ``Link`` is rewritten to this server's
    own base, as any proxy must: the client refuses a pagination link off the API host it was
    given, and the backend writes its own address into the header.
    """

    github: FakeGitHub
    releases: FakeReleases
    _server: http.server.ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

    @property
    def api_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def writes(self) -> int:
        return self.github.writes

    @property
    def requests(self) -> list[tuple[str, str]]:
        return [*self.github.requests, *self.releases.requests]

    def __enter__(self) -> Tracker:
        front = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _forward(self, method: str) -> None:
                path = urlparse(self.path).path
                base = front.releases.api_url if RELEASE_PATHS.match(path) else front.github.api_url
                length = int(self.headers.get("Content-Length") or 0)
                request = urllib.request.Request(
                    base + self.path, data=self.rfile.read(length) if length else None, method=method
                )
                for header in ("Authorization", "Accept", "Content-Type"):
                    value = self.headers.get(header)
                    if value:
                        request.add_header(header, value)
                try:
                    with urllib.request.urlopen(request) as response:  # noqa: S310 - an in-process fake
                        status, payload, headers = response.status, response.read(), response.headers
                except urllib.error.HTTPError as exc:
                    status, payload, headers = exc.code, exc.read(), exc.headers
                self.send_response(status)
                self.send_header("Content-Type", headers.get("Content-Type") or "application/json")
                self.send_header("Content-Length", str(len(payload)))
                link = headers.get("Link")
                if link:
                    self.send_header("Link", link.replace(base, front.api_url))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802
                self._forward("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._forward("POST")

            def do_PATCH(self) -> None:  # noqa: N802
                self._forward("PATCH")

            def do_DELETE(self) -> None:  # noqa: N802
                self._forward("DELETE")

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


@dataclass
class Rig(transitions.Rig):
    """The readiness rig, talking to the composite tracker instead of a bare issue fake."""

    tracker: Tracker | None = None

    @property
    def front(self) -> Tracker:
        assert self.tracker is not None
        return self.tracker

    def scan(self, run: Any, *flags: str) -> tuple[int, Any, str]:
        readiness.reset_registry()
        return run("scan", "--repo", support.REPO, "--api-url", self.front.api_url, *flags, repo=self.root)

    def reconcile(self, run: Any, *flags: str) -> tuple[int, Any, str]:
        return run("reconcile", "--repo", support.REPO, "--api-url", self.front.api_url, *flags, repo=self.root)

    def run_command(self, run: Any, *flags: str) -> tuple[int, Any, str]:
        readiness.reset_registry()
        return run("run", "--repo", support.REPO, "--api-url", self.front.api_url, *flags, repo=self.root)

    # -- staging the facts ----------------------------------------------------
    def open_pr(
        self,
        number: int,
        body: str,
        *,
        state: str = "open",
        merged_at: str | None = None,
        base: str = "main",
        title: str = "implementation",
    ) -> dict[str, Any]:
        pull = {
            "number": number,
            "state": state,
            "merged_at": merged_at,
            "title": title,
            "body": body,
            "html_url": f"https://github.com/{support.REPO}/pull/{number}",
            "base": {"ref": base},
            "draft": False,
        }
        self.front.github.pulls = [p for p in self.front.github.pulls if p["number"] != number] + [pull]
        return pull

    def close_pr(self, number: int, *, merged_at: str | None = None, base: str = "main") -> None:
        for pull in self.front.github.pulls:
            if pull["number"] == number:
                pull.update({"state": "closed", "merged_at": merged_at, "base": {"ref": base}})

    def put_target_on_main(self, record: dict[str, Any], ref: str = "main") -> None:
        payload = json.dumps(record, indent=2).encode("utf-8")
        key = f"{ref}:catalog/{record['game']}/targets/{record['id']}.json"
        self.front.github.contents[key] = base64.b64encode(payload).decode("ascii")

    # -- reading the tracker back ---------------------------------------------
    def body_of(self, rev: str = "26.3") -> str:
        return str(self.issue_with(kind="support", rev=rev)["body"])

    def lifecycle_json(self, rev: str = "26.3") -> dict[str, Any] | None:
        return lifecycle.parse_lifecycle(self.body_of(rev))

    def state_of(self, rev: str = "26.3") -> str | None:
        return issues.existing_state(self.body_of(rev))

    def work_state(self, rev: str = "26.3") -> str | None:
        key = f"provider=mojang component=minecraft branch=release rev={rev}"
        entry = (self.dashboard_state().get("work") or {}).get(key) or {}
        return entry.get("state")


_PLATFORM_SOURCE = {"fabric": "fabric-26.2", "paper": "paper-1.21.11", "neoforge": "neoforge-1.21.11"}


def target_for(root: Path, revision: str, *, platform: str = "fabric", status: str = "candidate") -> dict[str, Any]:
    """A catalog record for ``revision`` on ``platform``, cloned from the shipped record of that platform.

    Both the revision and the identity input's game version move, because the identity join reads
    both and a record that disagrees with itself would match nothing. A Paper or NeoForge clone
    carries no ``mojang-version`` input, exactly like the shipped records.
    """
    record = read_target(root, _PLATFORM_SOURCE[platform])
    was, now = str(record["id"]), f"{platform}-{revision}"
    record["id"] = now
    record["platform"] = platform
    record["revision"] = revision
    record["default"] = False
    spec = next(spec for spec in record["inputs"].values() if spec["kind"] in lifecycle.MOJANG_VERSION_FIELDS)
    spec[lifecycle.MOJANG_VERSION_FIELDS[spec["kind"]]] = revision
    record["support"] = {"status": status, "since": "2026-09-17", "evidence": [], "notes": "test fixture"}
    for component in record.get("components", []):
        component["artifact"] = str(component["artifact"]).replace(was, now)
    return record


# -- building a release --------------------------------------------------------
def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _artifact_names(record: dict[str, Any], version: str) -> list[tuple[str, str]]:
    return [
        (str(component["role"]), ids.artifact_file_name(record, str(component["role"]), version))
        for component in record.get("components", [])
    ]


TAMPERS = (
    "drop-target",
    "wrong-sum",
    "missing-asset",
    "underlevel",
    "candidate",
    "no-record",
    "stale-fingerprint",
)


def stable_release(
    rig: Rig,
    tag: str,
    version: str,
    targets: dict[str, dict[str, Any]],
    *,
    executed: str = "protocol",
    status: str = "maintained",
    published_at: str = "2026-09-20T10:00:00Z",
    tamper: str | None = None,
    connector: str = CONNECTOR,
) -> dict[str, Any]:
    """Publish a complete, checkable release set for ``targets``, optionally damaged.

    Every asset is real bytes with a real digest and a ``SHA256SUMS`` that covers the record
    itself, so a scenario that says "the release is complete" is saying something the command
    actually verified rather than something the fixture asserted.
    """
    assert tamper is None or tamper in TAMPERS, tamper
    files: dict[str, bytes] = {}
    record_targets: dict[str, dict[str, Any]] = {}
    assets: list[dict[str, Any]] = []

    def asset(name: str, kind: str, payload: bytes) -> dict[str, Any]:
        files[name] = payload
        entry = {
            "name": name,
            "kind": kind,
            "sha256": _sha256(payload),
            "size": len(payload),
            "url": compat_record.download_url(support.REPO, tag, name),
        }
        assets.append(entry)
        return entry

    for target_id, record in sorted(targets.items()):
        artifacts = []
        for role, name in _artifact_names(record, version):
            entry = asset(name, "artifact", f"jar for {target_id} {role} {version}\n".encode())
            artifacts.append(
                {"role": role, "name": name, "sha256": entry["sha256"], "size": entry["size"], "url": entry["url"]}
            )
        report = compat_record.report_name(connector, version, target_id)
        asset(report, "verify-report", json.dumps({"target": target_id, "level": executed}).encode("utf-8"))
        fingerprint = fp.fingerprint(record)
        if tamper == "stale-fingerprint":
            fingerprint = "0" * 64
        container = record["runtime"]["container"]
        record_targets[target_id] = {
            "platform": str(record["platform"]),
            "revision": str(record["revision"]),
            "status": "candidate" if tamper == "candidate" else status,
            "fingerprint": fingerprint,
            "inputs": {
                "game.manifest": {
                    "kind": "mojang-version",
                    "url": "https://piston-meta.example/manifest.json",
                    "version": str(record["revision"]),
                }
            },
            "runtime": {"image": container["image"], "tag": container["tag"], "digest": container["digest"]},
            "verification": {
                "required": str(record["verification"]["required"]),
                "executed": "startup" if tamper == "underlevel" else executed,
                "report": report,
                "outcome": "pass",
                "takaro": "local",
            },
            "artifacts": artifacts,
        }

    if tamper == "drop-target":
        record_targets.pop(sorted(record_targets)[0])

    record = compat_record.build(
        connector=connector,
        version=version,
        channel="stable",
        tag=tag,
        mode="catalog",
        repo=support.REPO,
        source_commit="0" * 40,
        source_tag=tag,
        dirty=False,
        stamp="2026-09-20T09:00:00Z",
        catalog={"game": CONNECTOR, "targetIds": sorted(targets)},
        catalog_hash=None,
        targets=record_targets,
        aliases={},
        assets=sorted(assets, key=lambda item: str(item["name"])),
    )
    compat_record.validate(record)
    record_name = str(record["self"])
    if tamper != "no-record":
        files[record_name] = (json.dumps(record, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    sums = {name: _sha256(payload) for name, payload in files.items()}
    if tamper == "wrong-sum":
        sums[sorted(n for n in sums if n.endswith(".jar"))[0]] = "f" * 64
    files["SHA256SUMS"] = "".join(f"{sums[name]}  {name}\n" for name in sorted(sums)).encode("utf-8")

    if tamper == "missing-asset":
        files.pop(sorted(n for n in files if n.endswith(".jar"))[0])

    release = rig.front.releases.add_release(tag, name=f"{connector} {version}")
    release["published_at"] = published_at
    for name in sorted(files):
        rig.front.releases.add_asset(release, name, files[name])
    return release


# -- the rig -------------------------------------------------------------------
@contextmanager
def lifecycle_rig(
    catalog_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    releases: tuple[str, ...] = support.RECORDED,
) -> Iterator[Rig]:
    """Mojang and the three frameworks on one upstream; issues and releases behind one URL."""
    readiness.reset_registry()
    support.frozen_clock(monkeypatch)
    with FakeUpstream() as upstream, FakeGitHub() as fake, FakeReleases(repo=support.REPO) as release_fake:
        with Tracker(github=fake, releases=release_fake) as tracker:
            served = support.mojang_upstream(upstream, releases)
            transitions.frameworks_upstream(upstream)
            support.scan_repo(catalog_copy, upstream)
            monkeypatch.setenv("GH_TOKEN", support.TOKEN)
            try:
                yield Rig(root=catalog_copy, upstream=upstream, fake=fake, served=served, tracker=tracker)
            finally:
                readiness.reset_registry()


def golden(name: str) -> str:
    """One pinned rendering, compared byte for byte."""
    return (FIXTURES / name).read_text(encoding="utf-8").strip("\n")


def block_of(body: str) -> str:
    """The Lifecycle block exactly as it sits in ``body``."""
    _, begin, rest = body.partition(lifecycle.LIFECYCLE_BEGIN)
    assert begin, "no lifecycle block in this body"
    return begin + rest.partition(lifecycle.LIFECYCLE_END)[0] + lifecycle.LIFECYCLE_END


def _set_state(body: str, state: str) -> str:
    """Seed a body at one lifecycle state, whatever the scan happened to leave it at."""
    return re.sub(r"<!-- takaro-maint:state=[a-z-]+ -->", f"<!-- takaro-maint:state={state} -->", body, count=1)


def reasons_of(payload: Any, issue: int | None = None) -> list[str]:
    entries = payload["issues"] if issue is None else [e for e in payload["issues"] if e["issue"] == issue]
    return [reason for entry in entries for reason in entry["reasons"]]


def blocked_upstream(harness: Rig) -> None:
    """Withdraw every framework's 26.3 support, so nothing can build it yet."""
    transitions.withdraw_fabric_game(harness.upstream, "26.3")
    transitions.withdraw_paper_version(harness.upstream, "26.3", "26.3-rc-3")
    transitions.withdraw_neoforge_version(
        harness.upstream, "26.3.0.0-beta", "26.3.0.1-beta", "26.3.0.2-beta", "26.3.0.3-beta"
    )


# =============================================================================
# pull requests
# =============================================================================
def _pull(number: int, title: str = "", body: str = "") -> dict[str, Any]:
    return {
        "number": number,
        "state": "open",
        "merged_at": None,
        "title": title,
        "body": body,
        "html_url": f"https://github.com/{support.REPO}/pull/{number}",
        "base": {"ref": "main"},
        "draft": False,
    }


def test_prs_reference_by_refs_and_closing_keywords() -> None:
    """Every form a maintainer actually types, and nothing a bare number could claim."""
    from takaro_maint.tracker import prs

    found = prs.references(
        [
            _pull(1, title="feat: something", body="Refs #12"),
            _pull(2, body="refs: #12"),
            _pull(3, title="Closes #12"),
            _pull(4, body="FIXES #12 and resolves #13"),
            _pull(5, body="see #120 for background"),
            _pull(6, body="Refs #12\nCloses #12"),
        ]
    )

    assert sorted(found) == [12, 13]
    assert [ref.number for ref in found[12]] == [1, 2, 3, 4, 6]
    assert [ref.number for ref in found[13]] == [4]
    assert found[12][0].closes_on_merge is False
    assert found[12][2].closes_on_merge is True
    # One pull request that says it twice is one reference, and the closing keyword is the
    # one recorded, because that is the one GitHub will act on.
    assert found[12][4].closes_on_merge is True
    assert 120 not in found


def test_prs_come_from_the_paginated_listing_not_search(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Five pull requests over three pages, with the search API made to explode."""
    from takaro_maint.github import GitHub

    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        number = int(harness.issue_with(kind="support", rev="26.3")["number"])
        for index in range(1, 6):
            harness.open_pr(index, f"Refs #{number}")

        def explode(self: GitHub, query: str) -> Any:
            raise AssertionError("reconcile must not use the search API for pull requests")

        monkeypatch.setattr(GitHub, "pulls_search", explode)
        code, payload, stderr = harness.reconcile(run)

        assert code == 0, stderr
        assert payload["pulls"] == {"listed": 5, "referencing": 1}
        assert payload["issues"][0]["prs"] == [1, 2, 3, 4, 5]
        pull_requests = [path for method, path in harness.front.github.requests if "/pulls" in path]
        assert pull_requests and all("state=all" in path for path in pull_requests)
        assert not [
            path
            for _, path in harness.front.github.requests
            if path.startswith("/search/issues?q=repo%3A") and "is%3Apr" in path
        ]


# =============================================================================
# identity
# =============================================================================
def _record(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "fabric-26.3",
        "game": "minecraft",
        "platform": "fabric",
        "revision": "26.3",
        "support": {"status": "candidate"},
        "inputs": {"game": {"kind": "mojang-version", "version": "26.3"}},
    }
    base.update(fields)
    return base


MOJANG_MARKER = {"kind": "support", "provider": "mojang", "component": "minecraft", "branch": "release", "rev": "26.3"}
STEAM_MARKER = {
    "kind": "support",
    "provider": "steam",
    "component": "enshrouded",
    "branch": "public",
    "rev": "19783520",
    "app": "2278520",
}


def test_identity_mojang_matches_revision_and_version() -> None:
    matched = lifecycle.matching_targets(MOJANG_MARKER, [_record()], game_id="minecraft", ref="main")
    assert [target.id for target in matched] == ["fabric-26.3"]

    # The record has to agree with itself: a revision bumped without the input is not 26.3.
    disagreeing = _record(inputs={"game": {"kind": "mojang-version", "version": "26.2"}})
    assert lifecycle.matching_targets(MOJANG_MARKER, [disagreeing], game_id="minecraft", ref="main") == []
    assert lifecycle.matching_targets(MOJANG_MARKER, [_record()], game_id="enshrouded", ref="main") == []


def test_identity_steam_matches_app_branch_buildid() -> None:
    record = _record(
        id="win-19783520",
        game="enshrouded",
        platform="win",
        revision="19783520",
        inputs={"server": {"kind": "steam-depots", "app": "2278520", "branch": "public", "buildid": "19783520"}},
    )
    assert [t.id for t in lifecycle.matching_targets(STEAM_MARKER, [record], game_id="enshrouded", ref="main")] == [
        "win-19783520"
    ]

    other_branch = json.loads(json.dumps(record))
    other_branch["inputs"]["server"]["branch"] = "beta"
    assert lifecycle.matching_targets(STEAM_MARKER, [other_branch], game_id="enshrouded", ref="main") == []


def test_identity_steam_matches_compound_revision_and_legacy_marker() -> None:
    record = _record(
        id="win-19783520",
        game="enshrouded",
        platform="win",
        revision="1.2.3",
        inputs={"server": {"kind": "steam-depots", "app": "2278520", "branch": "public", "buildid": "19783520"}},
    )
    current = {
        **STEAM_MARKER,
        "buildid": "19783520",
        "rev": "19783520.0123456789abcdef+public",
    }
    legacy = {key: value for key, value in current.items() if key not in {"app", "buildid"}}

    assert [t.id for t in lifecycle.matching_targets(current, [record], game_id="enshrouded", ref="main")] == [
        "win-19783520"
    ]
    assert [t.id for t in lifecycle.matching_targets(legacy, [record], game_id="enshrouded", ref="main")] == [
        "win-19783520"
    ]


def _paper(revision: str = "26.3", *, game_version: str | None = None) -> dict[str, Any]:
    return _record(
        id=f"paper-{revision}",
        platform="paper",
        revision=revision,
        inputs={
            "loader": {
                "kind": "paper-build",
                "project": "paper",
                "gameVersion": game_version or revision,
                "loaderVersion": "7",
            }
        },
    )


def _neoforge(revision: str = "26.3") -> dict[str, Any]:
    return _record(
        id=f"neoforge-{revision}",
        platform="neoforge",
        revision=revision,
        inputs={
            "universal": {"kind": "http-file", "path": "/x/neoforge-universal.jar"},
            "loader": {"kind": "neoforge-installer", "gameVersion": revision, "loaderVersion": "26.3.1"},
        },
    )


def test_identity_paper_and_neoforge_match_a_mojang_marker_by_game_version() -> None:
    """A Paper or NeoForge record has no Mojang input; its loader names the game version instead."""
    records = [_record(), _paper(), _neoforge()]
    matched = lifecycle.matching_targets(MOJANG_MARKER, records, game_id="minecraft", ref="main")
    assert [target.id for target in matched] == ["fabric-26.3", "neoforge-26.3", "paper-26.3"]

    # The record has to agree with itself, exactly like the Mojang rule.
    paper_26_2 = [_paper(game_version="26.2")]
    assert lifecycle.matching_targets(MOJANG_MARKER, paper_26_2, game_id="minecraft", ref="main") == []
    # A different game version is a different issue.
    assert lifecycle.matching_targets(MOJANG_MARKER, [_paper("26.2")], game_id="minecraft", ref="main") == []
    # A Steam marker never matches a Mojang-side kind.
    assert lifecycle.matching_targets(STEAM_MARKER, [_paper(), _neoforge()], game_id="minecraft", ref="main") == []


def test_identity_the_shipped_paper_and_neoforge_records_identify_their_game_version(repo_root: Path) -> None:
    """The regression the fixture-based tests missed: the catalog as shipped, not a clone of the Fabric record."""
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((repo_root / "catalog" / "minecraft" / "targets").glob("*.json"))
    ]
    for_1_21_11 = {**MOJANG_MARKER, "rev": "1.21.11"}
    assert [t.id for t in lifecycle.matching_targets(for_1_21_11, records, game_id="minecraft", ref="main")] == [
        "neoforge-1.21.11",
        "paper-1.21.11",
    ]
    for_26_2 = {**MOJANG_MARKER, "rev": "26.2"}
    assert [t.id for t in lifecycle.matching_targets(for_26_2, records, game_id="minecraft", ref="main")] == [
        "fabric-26.2"
    ]


def test_identity_ignores_unknown_input_kinds() -> None:
    """A loader jar is a build detail. A record with no identity input identifies nothing."""
    record = _record(inputs={"loader": {"kind": "maven-artifact", "version": "26.3"}})
    assert lifecycle.matching_targets(MOJANG_MARKER, [record], game_id="minecraft", ref="main") == []
    # A Fabric launcher is a build detail too: a Fabric record is identified by its Mojang input.
    assert (
        lifecycle.matching_targets(
            MOJANG_MARKER,
            [_record(inputs={"loader": {"kind": "fabric-launcher", "gameVersion": "26.3"}})],
            game_id="minecraft",
            ref="main",
        )
        == []
    )


def test_identity_skips_retired_targets() -> None:
    record = _record(support={"status": "retired"})
    live, retired = lifecycle.identity_matches(MOJANG_MARKER, [record], game_id="minecraft", ref="main")
    assert live == []
    assert retired == ["fabric-26.3"]


def test_remote_targets_read_the_catalog_at_the_ref_and_decode_base64(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from takaro_maint import github as github_module

    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        record = target_for(catalog_copy, "26.3")
        harness.put_target_on_main(record)
        harness.put_target_on_main(target_for(catalog_copy, "26.4"), ref="other")

        client = github_module.GitHub(support.REPO, support.TOKEN, harness.front.api_url)
        records, note = lifecycle.remote_targets(client, "minecraft", "main")

        assert note is None
        assert [str(item["id"]) for item in records] == ["fabric-26.3"]
        assert records[0]["inputs"]["game"]["version"] == "26.3"


def test_remote_targets_missing_on_ref_is_a_note_not_an_error(
    catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from takaro_maint import github as github_module

    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        client = github_module.GitHub(support.REPO, support.TOKEN, harness.front.api_url)
        records, note = lifecycle.remote_targets(client, "minecraft", "main")

        assert records == []
        assert note == "catalog/minecraft/targets is not on main"


# =============================================================================
# writing (and not writing) to the tracker
# =============================================================================
def test_read_only_reconcile_writes_nothing(run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        harness.put_target_on_main(target_for(catalog_copy, "26.3"))
        writes_before = harness.front.writes
        mark = len(harness.front.github.requests)

        code, payload, stderr = harness.reconcile(run)

        assert code == 0, stderr
        assert payload["mode"] == "read-only"
        assert [entry["action"] for entry in payload["plan"]] == ["update-issue", "update-dashboard"]
        assert payload["applied"] == []
        assert harness.front.writes == writes_before
        assert {method for method, _ in harness.front.github.requests[mark:]} == {"GET"}


def test_a_fresh_issue_gets_a_lifecycle_block_and_keeps_its_readiness_state(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing has happened yet, so the state is readiness's and the block simply says so."""
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        blocked_upstream(harness)
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        before = harness.state_of()

        code, payload, stderr = harness.reconcile(run, "--publish")

        assert code == 0, stderr
        body = harness.body_of()
        assert harness.state_of() == before == "blocked-upstream"
        assert body.count(lifecycle.LIFECYCLE_BEGIN) == 1
        assert body.index(issues.OWNED_END) < body.index(lifecycle.LIFECYCLE_BEGIN)
        assert payload["issues"][0]["to"] == "blocked-upstream"
        assert block_of(body) == golden("block-detected.md")


def test_the_controlled_lifecycle_from_discovery_to_release(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery to release, in the order it happens, with every gate on the way."""
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        # (a) nothing can build 26.3 yet.
        blocked_upstream(harness)
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        number = int(harness.issue_with(kind="support", rev="26.3")["number"])
        assert harness.state_of() == "blocked-upstream"
        code, payload, stderr = harness.reconcile(run, "--publish")
        assert code == 0, stderr
        assert harness.state_of() == "blocked-upstream"
        # The first reconcile appends the Lifecycle block; nothing about the state moved, so
        # the very next one has nothing left to write.
        code, payload, stderr = harness.reconcile(run, "--publish")
        assert code == 0, stderr
        assert [entry["reason"] for entry in payload["issues"]] == ["unchanged"]

        # (b) Fabric ships for 26.3; the scan moves the issue on and reconcile follows.
        transitions.add_fabric_game(harness.upstream, "26.3")
        transitions.add_fabric_api(harness.upstream, "0.161.0+26.3")
        assert harness.scan(run, "--publish")[0] == 0
        assert harness.state_of() == "ready-for-agent"
        code, payload, stderr = harness.reconcile(run, "--publish")
        assert code == 0, stderr
        assert harness.state_of() == "ready-for-agent"
        assert [entry["reason"] for entry in harness.reconcile(run, "--publish")[1]["issues"]] == ["unchanged"]

        # (c) somebody opens the implementation.
        harness.open_pr(12, f"Refs #{number}")
        code, payload, stderr = harness.reconcile(run, "--publish")
        assert code == 0, stderr
        assert harness.state_of() == "implementation-pr"
        assert "| Implementation | #12 (open) |" in harness.body_of()
        assert harness.work_state() == "implementation-pr"
        assert payload["warnings"] == []

        # (d) it merges and the target lands on main. A merge does not close the issue.
        harness.close_pr(12, merged_at="2026-09-18T11:00:00Z")
        record = target_for(catalog_copy, "26.3", status="maintained")
        harness.put_target_on_main(record)
        code, payload, stderr = harness.reconcile(run, "--publish")
        assert code == 0, stderr
        assert harness.state_of() == "awaiting-release"
        assert str(harness.issue_with(kind="support", rev="26.3")["state"]) == "open"
        assert "no stable release for minecraft yet" in reasons_of(payload, number)
        assert "catalog on main has fabric-26.3 (maintained)" in reasons_of(payload, number)

        # (e) three damaged releases, three named reasons, and no close.
        for tamper, expected in (
            ("drop-target", "minecraft-v0.2.0 does not list fabric-26.3"),
            ("wrong-sum", "SHA256SUMS disagrees for"),
            ("underlevel", "fabric-26.3 verified only to startup, requires protocol"),
        ):
            harness.front.releases.releases.clear()
            stable_release(harness, "minecraft-v0.2.0", "0.2.0", {"fabric-26.3": record}, tamper=tamper)
            code, payload, stderr = harness.reconcile(run, "--publish")
            assert code == 0, stderr
            assert harness.state_of() == "awaiting-release", tamper
            assert any(expected in reason for reason in reasons_of(payload, number)), (tamper, payload["issues"])
            assert str(harness.issue_with(kind="support", rev="26.3")["state"]) == "open"

        # (f) the complete release: one PATCH that writes the body and closes the issue.
        harness.front.releases.releases.clear()
        stable_release(harness, "minecraft-v0.2.0", "0.2.0", {"fabric-26.3": record})
        patches_before = harness.patches(number)
        code, payload, stderr = harness.reconcile(run, "--publish")

        assert code == 0, stderr
        assert harness.patches(number) == patches_before + 1
        issue = harness.issue_with(kind="support", rev="26.3")
        assert (issue["state"], issue["state_reason"]) == ("closed", "completed")
        assert harness.state_of() == "released"
        body = harness.body_of()
        assert "[minecraft-v0.2.0](" in body
        jar = _artifact_names(record, "0.2.0")[0][1]
        assert jar in body
        assert harness.lifecycle_json()["release"]["tag"] == "minecraft-v0.2.0"
        assert harness.work_state() == "released"
        assert payload["applied"][0]["action"] == "close-issue"
        assert block_of(body) == golden("block-released.md")

        # (g) an identical rerun changes nothing at all.
        code, payload, stderr = harness.reconcile(run, "--publish")
        assert code == 0, stderr
        assert payload["applied"] == []
        assert harness.patches(number) == patches_before + 1
        assert [entry["reason"] for entry in payload["issues"]] == ["closed-completed"]

        # (h) and a later scan leaves the closed issue exactly as it is.
        body_before = harness.body_of()
        harness.forget("26.3")
        assert harness.scan(run, "--publish")[0] == 0
        assert harness.body_of() == body_before
        assert str(harness.issue_with(kind="support", rev="26.3")["state"]) == "closed"


def test_a_scan_after_reconcile_keeps_the_lifecycle_state_and_block(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan owns its block; the state token and the Lifecycle block survive it intact."""
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        number = int(harness.issue_with(kind="support", rev="26.3")["number"])
        harness.open_pr(12, f"Refs #{number}")
        assert harness.reconcile(run, "--publish")[0] == 0
        assert harness.state_of() == "implementation-pr"
        block_before = block_of(harness.body_of())

        # A new catalog target changes the scan's affected-targets table, so the owned block
        # really is rewritten by this run rather than left alone for being identical.
        support.add_candidate_target(catalog_copy, "26.3")
        harness.forget("26.3")
        assert harness.scan(run, "--publish")[0] == 0

        body = harness.body_of()
        assert "`fabric-26.3`" in body.partition(issues.OWNED_END)[0]
        assert issues.existing_state(body) == "implementation-pr"
        assert block_of(body) == block_before


def test_a_closed_unmerged_pr_walks_the_issue_back_to_readiness(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        number = int(harness.issue_with(kind="support", rev="26.3")["number"])
        harness.open_pr(12, f"Refs #{number}")
        assert harness.reconcile(run, "--publish")[0] == 0
        assert harness.state_of() == "implementation-pr"
        since_before = harness.lifecycle_json()["since"]

        harness.close_pr(12)
        support.frozen_clock(monkeypatch, "2026-09-19T08:00:00Z")
        code, payload, stderr = harness.reconcile(run, "--publish")

        assert code == 0, stderr
        assert harness.state_of() == "ready-for-agent"
        assert harness.lifecycle_json()["since"] == "2026-09-19T08:00:00Z" != since_before
        assert harness.lifecycle_json()["prs"] == []


def test_a_merged_pr_without_the_target_on_main_stays_implementation_pr(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Epic branches merge before main does; a merge into one is not a published target."""
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        number = int(harness.issue_with(kind="support", rev="26.3")["number"])
        harness.open_pr(12, f"Refs #{number}", state="closed", merged_at="2026-09-18T10:00:00Z", base="epic/x")

        code, payload, stderr = harness.reconcile(run, "--publish")

        assert code == 0, stderr
        assert harness.state_of() == "implementation-pr"
        assert "merged, not on main yet" in reasons_of(payload, number)
        assert "#12 (merged into epic/x at 2026-09-18T10:00:00Z)" in reasons_of(payload, number)


def test_a_candidate_on_main_is_awaiting_release_and_a_candidate_in_the_release_is_not_released(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        number = int(harness.issue_with(kind="support", rev="26.3")["number"])
        record = target_for(catalog_copy, "26.3", status="candidate")
        harness.put_target_on_main(record)

        code, payload, stderr = harness.reconcile(run, "--publish")
        assert code == 0, stderr
        assert harness.state_of() == "awaiting-release"
        assert "catalog on main has fabric-26.3 (candidate)" in reasons_of(payload, number)

        stable_release(harness, "minecraft-v0.2.0", "0.2.0", {"fabric-26.3": record}, tamper="candidate")
        code, payload, stderr = harness.reconcile(run, "--publish")

        assert code == 0, stderr
        assert harness.state_of() == "awaiting-release"
        assert "minecraft-v0.2.0 ships fabric-26.3 as candidate; promote it to maintained" in reasons_of(
            payload, number
        )


def test_a_candidate_on_the_ref_is_never_released_however_the_release_labels_it(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Promotion on the ref is part of what the issue asks for, and is not fingerprinted."""
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        number = int(harness.issue_with(kind="support", rev="26.3")["number"])
        record = target_for(catalog_copy, "26.3", status="candidate")
        harness.put_target_on_main(record)
        # The release says maintained; the catalog has not been promoted. A target's support
        # status is deliberately outside its fingerprint, so this matches on identity.
        stable_release(harness, "minecraft-v0.2.0", "0.2.0", {"fabric-26.3": record})

        code, payload, stderr = harness.reconcile(run, "--publish")

        assert code == 0, stderr
        assert harness.state_of() == "awaiting-release"
        assert str(harness.issue_with(kind="support", rev="26.3")["state"]) == "open"
        assert "catalog on main has fabric-26.3 (candidate)" in reasons_of(payload, number)


def test_a_release_missing_a_component_role_is_not_released(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``components`` are outside the fingerprint, so a new role has to be checked by name."""
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        number = int(harness.issue_with(kind="support", rev="26.3")["number"])
        shipped = target_for(catalog_copy, "26.3", status="maintained")
        stable_release(harness, "minecraft-v0.2.0", "0.2.0", {"fabric-26.3": shipped})

        # The ref gains a second component. The fingerprint does not move, so the release
        # still matches on identity while shipping only half of what the target now asks for.
        on_ref = json.loads(json.dumps(shipped))
        on_ref["components"].append(
            {"role": "companion", "artifact": "takaro-minecraft-companion-26.3-{version}.jar", "installDir": "mods"}
        )
        assert fp.fingerprint(on_ref) == fp.fingerprint(shipped)
        harness.put_target_on_main(on_ref)

        code, payload, stderr = harness.reconcile(run, "--publish")

        assert code == 0, stderr
        assert harness.state_of() == "awaiting-release"
        assert "minecraft-v0.2.0 ships no companion artifact for fabric-26.3" in reasons_of(payload, number)


def test_a_lifecycle_block_with_no_end_marker_is_left_alone(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Half a block is unrecognisable. Rewriting to end-of-body would delete what follows."""
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        number = int(harness.issue_with(kind="support", rev="26.3")["number"])
        assert harness.reconcile(run, "--publish")[0] == 0
        issue = harness.issue_with(kind="support", rev="26.3")
        issue["body"] = str(issue["body"]).replace(lifecycle.LIFECYCLE_END, "") + "\nmy notes live here\n"
        body_before = str(issue["body"])
        patches_before = harness.patches(number)

        harness.open_pr(12, f"Refs #{number}")
        code, payload, stderr = harness.reconcile(run, "--publish")

        assert code == 0, stderr
        assert [entry["reason"] for entry in payload["issues"]] == ["unrecognised-body"]
        assert str(issue["body"]) == body_before
        assert "my notes live here" in str(issue["body"])
        assert harness.patches(number) == patches_before


def test_a_stale_fingerprint_on_main_keeps_awaiting_release(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        number = int(harness.issue_with(kind="support", rev="26.3")["number"])
        record = target_for(catalog_copy, "26.3", status="maintained")
        harness.put_target_on_main(record)
        stable_release(harness, "minecraft-v0.2.0", "0.2.0", {"fabric-26.3": record}, tamper="stale-fingerprint")

        code, payload, stderr = harness.reconcile(run, "--publish")

        assert code == 0, stderr
        assert harness.state_of() == "awaiting-release"
        expected = f"minecraft-v0.2.0 ships fabric-26.3 with fingerprint {'0' * 16}, main has {fp.fp16(record)}"
        assert expected in reasons_of(payload, number)


def test_two_platforms_release_only_when_both_ship(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release that ships one of two matched targets has not released the issue's work."""
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        number = int(harness.issue_with(kind="support", rev="26.3")["number"])
        fabric = target_for(catalog_copy, "26.3", status="maintained")
        paper = target_for(catalog_copy, "26.3", platform="paper", status="maintained")
        harness.put_target_on_main(fabric)
        harness.put_target_on_main(paper)
        stable_release(harness, "minecraft-v0.2.0", "0.2.0", {"fabric-26.3": fabric})

        code, payload, stderr = harness.reconcile(run, "--publish")

        assert code == 0, stderr
        assert harness.state_of() == "awaiting-release"
        assert payload["issues"][0]["targets"] == ["fabric-26.3", "paper-26.3"]
        assert "minecraft-v0.2.0 does not list paper-26.3" in reasons_of(payload, number)

        # Once both ship, the Release row names both platforms' artifacts, not just the first.
        harness.front.releases.releases.clear()
        stable_release(harness, "minecraft-v0.2.0", "0.2.0", {"fabric-26.3": fabric, "paper-26.3": paper})
        code, payload, stderr = harness.reconcile(run, "--publish")

        assert code == 0, stderr
        assert harness.state_of() == "released"
        shipped = [item["name"] for item in harness.lifecycle_json()["release"]["artifacts"]]
        assert shipped == sorted(_artifact_names(fabric, "0.2.0")[0][1:] + _artifact_names(paper, "0.2.0")[0][1:])


def test_an_interrupted_close_is_completed_on_the_next_run(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The close is one PATCH, so an interrupted run leaves nothing half-written to repair."""
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        record = target_for(catalog_copy, "26.3", status="maintained")
        harness.put_target_on_main(record)
        stable_release(harness, "minecraft-v0.2.0", "0.2.0", {"fabric-26.3": record})
        body_before = harness.body_of()

        harness.front.github.fail_after(harness.front.writes)
        code, payload, stderr = harness.reconcile(run, "--publish")

        assert code == 9, stderr
        issue = harness.issue_with(kind="support", rev="26.3")
        assert issue["state"] == "open"
        assert str(issue["body"]) == body_before

        harness.front.github.fail_after(10**6)
        code, payload, stderr = harness.reconcile(run, "--publish")

        assert code == 0, stderr
        issue = harness.issue_with(kind="support", rev="26.3")
        assert (issue["state"], issue["state_reason"]) == ("closed", "completed")
        body = str(issue["body"])
        assert body.count(lifecycle.LIFECYCLE_BEGIN) == 1
        assert body.count("<!-- takaro-maint: kind=support") == 1
        assert not [path for _, path in harness.front.github.requests if path.endswith("/comments")]


def test_a_dashboard_conflict_after_issue_writes_exits_nine_and_the_rerun_is_a_noop(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from takaro_maint.github import GitHub

    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        harness.put_target_on_main(target_for(catalog_copy, "26.3"))
        dashboard_number = int(harness.dashboard_issue()["number"])  # type: ignore[index]

        original = GitHub.issue_get

        def mutating(client: GitHub, number: int) -> Any:
            """Someone edits the dashboard between the reconcile's read and its write."""
            if number == dashboard_number:
                for issue in harness.front.github.issues:
                    if issue["number"] == number:
                        issue["body"] = str(issue["body"]) + "\nsomeone else typed here\n"
            return original(client, number)

        monkeypatch.setattr(GitHub, "issue_get", mutating)
        code, payload, stderr = harness.reconcile(run, "--publish")

        assert code == 9, stderr
        assert "changed during the scan" in str(payload["error"])
        assert harness.state_of() == "awaiting-release"

        monkeypatch.setattr(GitHub, "issue_get", original)
        code, payload, stderr = harness.reconcile(run, "--publish")

        assert code == 0, stderr
        assert [entry["reason"] for entry in payload["issues"]] == ["unchanged"]
        assert payload["dashboard"]["updated"] is True
        # The body was already right, so nothing was PATCHed — but the dashboard the failed
        # run never managed to save is repaired all the same.
        assert harness.work_state() == "awaiting-release"


def test_an_issue_that_moved_during_the_run_is_skipped(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from takaro_maint.github import GitHub

    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        number = int(harness.issue_with(kind="support", rev="26.3")["number"])
        harness.put_target_on_main(target_for(catalog_copy, "26.3"))
        patches_before = harness.patches(number)

        original = GitHub.issue_get

        def mutating(client: GitHub, wanted: int) -> Any:
            if wanted == number:
                for issue in harness.front.github.issues:
                    if issue["number"] == wanted:
                        issue["body"] = "<!-- takaro-maint: kind=support -->\n" + str(issue["body"])
            return original(client, wanted)

        monkeypatch.setattr(GitHub, "issue_get", mutating)
        code, payload, stderr = harness.reconcile(run, "--publish")

        assert code == 0, stderr
        entry = next(item for item in payload["issues"] if item["issue"] == number)
        assert (entry["action"], entry["reason"]) == ("noop", "moved-during-run")
        assert harness.patches(number) == patches_before
        assert payload["plan"] == [{"action": "update-dashboard", "issue": payload["dashboard"]["issue"]}]


def test_declined_is_terminal_even_when_released(run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        issue = harness.issue_with(kind="support", rev="26.3")
        issue.update({"state": "closed", "state_reason": "not_planned"})
        record = target_for(catalog_copy, "26.3", status="maintained")
        harness.put_target_on_main(record)
        stable_release(harness, "minecraft-v0.2.0", "0.2.0", {"fabric-26.3": record})
        number = int(issue["number"])
        patches_before = harness.patches(number)

        code, payload, stderr = harness.reconcile(run, "--publish")

        assert code == 0, stderr
        assert [entry["reason"] for entry in payload["issues"]] == ["declined"]
        assert harness.patches(number) == patches_before

        # A human reopens it: from then on it is reconciled like any other open issue.
        issue.update({"state": "open", "state_reason": None})
        code, payload, stderr = harness.reconcile(run, "--publish")
        assert code == 0, stderr
        assert harness.state_of() == "released"


def test_closed_completed_without_release_is_reported_not_reopened(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        number = int(harness.issue_with(kind="support", rev="26.3")["number"])
        harness.open_pr(12, f"Refs #{number}")
        assert harness.reconcile(run, "--publish")[0] == 0
        assert harness.state_of() == "implementation-pr"
        harness.issue_with(kind="support", rev="26.3").update({"state": "closed", "state_reason": "completed"})
        patches_before = harness.patches(number)

        code, payload, stderr = harness.reconcile(run, "--publish")

        assert code == 0, stderr
        assert [entry["reason"] for entry in payload["issues"]] == ["closed-before-release"]
        assert harness.patches(number) == patches_before
        assert harness.issue_with(kind="support", rev="26.3")["state"] == "closed"


def test_superseded_and_review_issues_are_left_alone(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        issue = harness.issue_with(kind="support", rev="26.3")
        issue["body"] = _set_state(str(issue["body"]), "superseded")
        harness.put_target_on_main(target_for(catalog_copy, "26.3"))
        number = int(issue["number"])
        patches_before = harness.patches(number)

        code, payload, stderr = harness.reconcile(run, "--publish")

        assert code == 0, stderr
        assert [entry["reason"] for entry in payload["issues"]] == ["superseded"]
        assert harness.patches(number) == patches_before

        issue["body"] = _set_state(str(issue["body"]), "review")
        code, payload, stderr = harness.reconcile(run, "--publish")
        assert code == 0, stderr
        assert [entry["reason"] for entry in payload["issues"]] == ["review"]
        assert harness.patches(number) == patches_before


def test_an_unrecognised_body_is_reported_and_untouched(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        issue = harness.issue_with(kind="support", rev="26.3")
        issue["body"] = re.sub(r"<!-- takaro-maint:state=[a-z-]+ -->", "", str(issue["body"]))
        body_before = str(issue["body"])
        number = int(issue["number"])
        patches_before = harness.patches(number)

        code, payload, stderr = harness.reconcile(run, "--publish")

        assert code == 0, stderr
        assert [entry["reason"] for entry in payload["issues"]] == ["unrecognised-body"]
        assert str(issue["body"]) == body_before
        assert harness.patches(number) == patches_before


def test_a_human_edit_outside_both_blocks_survives(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        number = int(harness.issue_with(kind="support", rev="26.3")["number"])
        assert harness.reconcile(run, "--publish")[0] == 0
        issue = harness.issue_with(kind="support", rev="26.3")
        issue["body"] = str(issue["body"]).replace(
            issues.OWNED_BEGIN, "I looked at this on Tuesday. — a human\n\n" + issues.OWNED_BEGIN
        )
        issue["body"] = str(issue["body"]).rstrip("\n") + "\n\nand a note at the very bottom.\n"

        harness.open_pr(12, f"Refs #{number}")
        code, _, stderr = harness.reconcile(run, "--publish")

        assert code == 0, stderr
        body = harness.body_of()
        assert harness.state_of() == "implementation-pr"
        assert "I looked at this on Tuesday. — a human" in body
        assert body.endswith("and a note at the very bottom.\n")


def test_a_closing_keyword_on_an_open_pr_is_a_warning(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tool cannot stop GitHub auto-closing on merge. It can say so, loudly, every run."""
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        number = int(harness.issue_with(kind="support", rev="26.3")["number"])
        harness.open_pr(12, f"Closes #{number}")

        code, payload, stderr = harness.reconcile(run)

        assert code == 0, stderr
        assert payload["warnings"] == [
            f"PR #12 will close #{number} on merge; maintenance PRs should use Refs #{number}"
        ]


def test_no_dashboard_means_no_dashboard_write(run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bootstrapping the tracker is ``scan``'s job; reconcile never invents a dashboard."""
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        harness.front.github.issues = [
            issue for issue in harness.front.github.issues if issue is not harness.dashboard_issue()
        ]
        mark = len(harness.front.github.requests)

        code, payload, stderr = harness.reconcile(run, "--publish")

        assert code == 0, stderr
        assert payload["dashboard"] == {"issue": None, "updated": False}
        assert [entry["action"] for entry in payload["plan"]] == ["update-issue"]
        assert [entry["action"] for entry in payload["applied"]] == ["update-issue"]
        assert not [method for method, _ in harness.front.github.requests[mark:] if method == "POST"]


def test_issue_filter_and_unknown_game(run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.scan(run, "--bootstrap", "--publish")[0] == 0
        number = int(harness.issue_with(kind="support", rev="26.3")["number"])

        code, payload, stderr = harness.reconcile(run, "--issue", str(number))
        assert code == 0, stderr
        assert [entry["issue"] for entry in payload["issues"]] == [number]

        code, payload, stderr = harness.reconcile(run, "--issue", "9999")
        assert code == 0, stderr
        assert payload["issues"] == [
            {
                "issue": 9999,
                "identity": None,
                "game": None,
                "from": None,
                "to": None,
                "action": "noop",
                "reason": "not-a-support-issue",
                "reasons": [],
                "prs": [],
                "targets": [],
                "release": None,
            }
        ]

        code, payload, stderr = harness.reconcile(run, "--game", "not-a-game")
        assert code == 2, stderr


# =============================================================================
# run = scan + reconcile
# =============================================================================
def test_run_is_scan_then_reconcile_in_one_document(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        code, payload, stderr = harness.run_command(run, "--bootstrap", "--publish")

        assert code == 0, stderr
        assert payload["op"] == "run"
        assert payload["exitCodes"] == {"scan": 0, "reconcile": 0}
        assert [entry["action"] for entry in payload["scan"]["applied"] if entry["action"] == "create-issue"] == [
            "create-issue"
        ]
        assert payload["reconcile"]["issues"][0]["to"] == "ready-for-agent"
        # The reconcile reads the dashboard the scan just saved, in the same process.
        assert payload["reconcile"]["dashboard"]["issue"] == payload["scan"]["dashboard"]["issue"]


def test_run_exit_code_is_the_scans_when_it_fails(
    run: Any, catalog_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.run_command(run, "--bootstrap", "--publish")[0] == 0
        harness.upstream.status_overrides[support.MANIFEST_PATH] = 503

        code, payload, stderr = harness.run_command(run, "--publish")

        assert code == 4, stderr
        assert payload["exitCodes"]["scan"] == 4
        assert payload["exitCodes"]["reconcile"] == 0

    with lifecycle_rig(catalog_copy, monkeypatch) as harness:
        assert harness.run_command(run, "--bootstrap", "--publish")[0] == 0
        dashboard_number = int(harness.dashboard_issue()["number"])  # type: ignore[index]
        harness.front.github.fail_on(rf"/issues/{dashboard_number}$")

        code, payload, stderr = harness.run_command(run, "--publish")

        assert code == 9, stderr
        assert payload["exitCodes"] == {"scan": 9, "reconcile": None}
        assert payload["reconcile"] is None
