"""Steam: the provider behind every Steam-delivered dedicated server.

Acquisition is the whole-depot install (``takaro_maint.steam.install``); what this module
adds on top is discovery — asking Steam what each watched branch currently points at and
turning a move into one maintenance issue.

Two things make a Steam head different from a version string, and both shape everything
below:

*A build is an identity, not a number.* What names the bytes is an app, a branch, a build
id, an operating system and one content manifest per depot. A publisher can replace a
depot's content under the same build id, so the revision folds the watched depots'
manifest ids into itself (:func:`takaro_maint.steam.steamcmd.manifest_digest`), and the
branch name is part of it so the same build appearing on ``public`` after ``experimental``
is a new revision rather than a silent no-op.

*Steam publishes no history.* ``app_info_print`` answers with the current head of every
branch and nothing else. A build that came and went between two scans was never observed,
so every observation carries :data:`OBSERVATION_LIMIT` and the result reports
``heads-only`` history — the tracker says what it saw, and does not claim the rest.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import channels, observations, paths
from ..exit_codes import UpstreamUnavailable, UsageError
from ..steam import depotdownloader as dd
from ..steam import steamcmd
from ..tracker import identity
from .base import Observation, Provider, ProviderResult

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from ..catalog.loader import Target

#: Said in every observation, and rendered in every issue this provider files.
OBSERVATION_LIMIT = (
    "heads-only: Steam app metadata exposes only the current head of each branch; builds "
    "published between two scans are not observed and are not claimed."
)

#: A marker branch ends up in an issue identity forever, and the marker grammar
#: (``tracker.identity``) has no underscore in it. Steam labels do — ``latest_experimental``
#: — so the catalog maps the label to a branch name and this is what that name may be.
BRANCH_RE = re.compile(r"^[A-Za-z0-9.+-]+$")

#: The default variable a protected branch's password is read from. Only ever the NAME of
#: it is printed, recorded or rendered; the value is read straight out of the environment
#: and handed to DepotDownloader, which hides it in its own log.
PASSWORD_ENV_PREFIX = "TAKARO_MAINT_STEAM_BRANCH_PASSWORD"


def password_env(app: int, label: str) -> str:
    """The variable a branch's password is read from, when the channel names no other."""
    return f"{PASSWORD_ENV_PREFIX}__{app}__{re.sub(r'[^A-Z0-9]', '_', label.upper())}"


def branch_name(label: str) -> str:
    """A marker branch name for an upstream label nobody declared a channel for.

    ``channels.branch_name`` would pass an underscore straight through, and a marker with
    an underscore in its revision is rejected by the observation schema — so a Steam label
    is folded into the marker alphabet here instead: ``beta_test`` becomes ``beta-test``,
    and a label with nothing usable left in it is ``unknown``.
    """
    candidate = re.sub(r"[^a-z0-9.+-]", "-", str(label or "").strip().lower()).strip("-")
    return candidate if candidate and BRANCH_RE.match(candidate) else channels.UNKNOWN_BRANCH


def _selector(pattern: str, where: str) -> re.Pattern[str]:
    """``regex:<pattern>`` or an exact string — the grammar ``build.references`` uses.

    A pattern comes out of the catalog, so a broken one is a misconfigured source rather
    than a crash: ``re.error`` would escape the scan's per-source handling and take the
    whole run — and every other game's sources — down with it.
    """
    try:
        if pattern.startswith("regex:"):
            return re.compile(pattern[len("regex:") :])
        return re.compile(f"^{re.escape(pattern)}$")
    except re.error as exc:
        raise UsageError(f"{where} is not a valid pattern: {pattern!r} ({exc})") from exc


class _Watch:
    """A validated ``sources.<id>.watch`` block. Every failure names the key it is about."""

    def __init__(self, source: dict[str, Any]) -> None:
        watch = source.get("watch") or {}
        where = f"source '{source.get('id') or 'steam'}'"
        self.kind = str(watch.get("kind") or "game")
        self.component = str(watch.get("component") or source.get("game") or "")
        if not self.component:
            raise UsageError(f"the Steam watch block of {where} declares no 'watch.component'")
        app = watch.get("app")
        try:
            self.app = int(str(app))
        except (TypeError, ValueError) as exc:
            raise UsageError(f"the Steam watch block of {where} declares no integer 'watch.app'") from exc
        self.os = str(watch.get("os") or "")
        if self.os not in ("linux", "windows", "macos"):
            raise UsageError(
                f"the Steam watch block of {where} declares 'watch.os' = {watch.get('os')!r}; "
                "it must be linux, windows or macos"
            )
        self.depots = [str(depot) for depot in watch.get("depots") or []]
        if not self.depots:
            raise UsageError(f"the Steam watch block of {where} declares no 'watch.depots'; a build is its depots")
        self.enabled = channels.enabled_channels(watch)
        self.declared = channels.declared_labels(watch)
        for key, channel in self.enabled.items():
            if not BRANCH_RE.match(str(channel["branch"])):
                raise UsageError(
                    f"the Steam watch block of {where} maps 'watch.channels.{key}' to branch "
                    f"{channel['branch']!r}; a branch name is {BRANCH_RE.pattern} (an upstream label "
                    "with an underscore needs an explicit 'branch')"
                )
        self.known = [
            _selector(str(entry), f"the Steam watch block of {where} entry 'watch.knownBranches[{index}]'")
            for index, entry in enumerate(watch.get("knownBranches") or [])
        ]

    def is_known(self, label: str) -> bool:
        return any(selector.match(label) for selector in self.known)


class SteamProvider(Provider):
    id = "steam"

    def fetch_input(
        self,
        input_spec: dict[str, Any],
        source: dict[str, Any],
        dest: Path,
        cache: Path,
    ) -> Path:
        del input_spec, source, dest, cache
        raise UsageError(
            "steam-depots inputs are installed as a whole by takaro-maint install; "
            "the game adapter drives takaro_maint.steam.install"
        )

    def input_url(self, input_spec: dict[str, Any], source: dict[str, Any]) -> str | None:
        """The pseudo-URL that names exactly the depot manifests an input pins.

        Steam serves no download link: a build is an app on a branch plus one content
        manifest per depot. Spelling that out as a URL gives reports, compat records and
        ledgers one string that identifies the bytes, and it is stable because the
        manifest ids are.
        """
        del source
        if input_spec.get("kind") != "steam-depots":
            return None
        depots = input_spec.get("depots") or {}
        if not depots:
            return None
        pinned = ";".join(f"{depot}/manifest/{depots[depot]['manifest']}" for depot in sorted(depots))
        return (
            f"steam://app/{input_spec['app']}/branch/{input_spec['branch']}"
            f"/build/{input_spec['buildid']}/depot/{pinned}"
        )

    # -- discovery -------------------------------------------------------------
    def _depot(self, info: steamcmd.AppInfo, watch: _Watch, depot_id: str) -> steamcmd.Depot:
        depot = info.depots.get(depot_id)
        if depot is None:
            listed = ", ".join(sorted(info.depots)) or "<none>"
            raise UpstreamUnavailable(
                f"app {watch.app} publishes no depot {depot_id} named by watch.depots. Listed: {listed}"
            )
        return depot

    def _published(
        self,
        info: steamcmd.AppInfo,
        watch: _Watch,
        label: str,
        branch: steamcmd.Branch,
        channel: dict[str, Any],
    ) -> tuple[dict[str, dict[str, Any]], str | None]:
        """What the watched depots point at on ``label``, and the variable that unlocked it.

        A branch behind a password publishes its manifest ids encrypted, so they cannot be
        read out of the metadata at all; DepotDownloader resolves them with the password
        instead. The value is read from the environment here and never leaves it: what is
        recorded, rendered and returned is the variable's NAME.
        """
        depots = [self._depot(info, watch, depot_id) for depot_id in watch.depots]
        plain = {depot.id: depot.manifests.get(label) for depot in depots}
        protected = branch.pwdrequired or all(plain[depot.id] is None and label in depot.encrypted for depot in depots)
        if not protected:
            resolved: dict[str, dict[str, Any]] = {}
            for depot in depots:
                manifest = plain[depot.id]
                if manifest is None:
                    raise UpstreamUnavailable(
                        f"depot {depot.id} has no manifest on branch '{label}' of app {watch.app}"
                    )
                resolved[depot.id] = {
                    "manifest": manifest.gid,
                    "size": manifest.size,
                    "download": manifest.download,
                }
            return resolved, None

        name = str(channel.get("passwordEnv") or password_env(watch.app, label))
        if not os.environ.get(name):
            raise UpstreamUnavailable(
                f"branch '{label}' of app {watch.app} requires a password; set {name} in the "
                "environment (the value is never written anywhere)"
            )
        cache = paths.cache_dir()
        log = steamcmd.log_path(cache, watch.app)
        resolved = {}
        with tempfile.TemporaryDirectory(prefix="takaro-maint-branch-") as tmp:
            for depot in depots:
                found = dd.manifest_only(
                    watch.app,
                    depot.id,
                    label,
                    manifest=None,
                    os_=watch.os,
                    arch="64",
                    out=Path(tmp) / depot.id,
                    cache=cache,
                    log=log,
                    credentials={"branchPasswordEnv": name},
                )
                resolved[depot.id] = {"manifest": found.manifest, "size": found.bytes_on_disk, "download": None}
        return resolved, name

    def _rev(self, buildid: int, depots: dict[str, dict[str, Any]] | None, branch: str) -> str:
        """``<buildid>[.<manifest digest>]+<branch>`` — the identity, short enough to read."""
        if not depots:
            return f"{buildid}+{branch}"
        digest = steamcmd.manifest_digest({depot: str(entry["manifest"]) for depot, entry in depots.items()})
        return f"{buildid}.{digest}+{branch}"

    def _promoted_from(self, checkpoint: dict[str, Any] | None, rev: str, branch: str) -> str | None:
        """The branch this exact build was seen on before, if it was seen on another.

        The build and its manifests are what make the comparison: the same
        ``<buildid>.<digest>`` under a different branch name is the build being promoted,
        and nothing about the two numbers is compared.
        """
        prefix = rev.rpartition("+")[0]
        if not prefix:
            return None
        for seen, _ in _seen(checkpoint):
            head = channels.split_rollback_rev(seen)[0]
            other = head.rpartition("+")
            if other[0] == prefix and other[2] and other[2] != branch:
                return other[2]
        return None

    def _facts(
        self,
        info: steamcmd.AppInfo,
        watch: _Watch,
        label: str,
        branch: steamcmd.Branch,
        depots: dict[str, dict[str, Any]] | None,
        *,
        credentials_env: str | None,
        release_time: str,
    ) -> dict[str, Any]:
        return {
            "app": watch.app,
            "appName": info.name,
            "label": label,
            "buildid": branch.buildid,
            "os": watch.os,
            "depots": depots or {},
            "timeupdated": branch.timeupdated,
            "timebuildupdated": branch.timebuildupdated,
            "description": branch.description,
            "pwdrequired": branch.pwdrequired,
            "credentialsEnv": credentials_env,
            "changeNumber": info.change_number,
            "lastChange": info.last_change,
            "releaseTime": release_time,
            "history": "heads-only",
            "observationLimit": OBSERVATION_LIMIT,
            "listing": {"command": (f"steamcmd +login anonymous +app_info_update 1 +app_info_print {watch.app} +quit")},
        }

    def observe(self, source: dict[str, Any]) -> ProviderResult:
        """One observation per watched branch head, plus one per branch nobody declared.

        ``source`` is the game record's source mapping enriched by the scan with ``id``,
        ``game`` and ``checkpoint`` (see ``maintenance/docs/discovery.md``). Every failure
        here — a missing key, a branch the app does not list, a password that is not in the
        environment — is a failed source rather than an empty success, because "Steam
        published nothing new" and "we could not look" must never read the same.
        """
        watch = _Watch(source)
        info = steamcmd.app_info(watch.app, log=steamcmd.log_path(paths.cache_dir(), watch.app))
        checkpoint = source.get("checkpoint")
        now = observations.utcnow()

        seen: list[Observation] = []
        heads: dict[str, str] = {}
        for label, channel in sorted(watch.enabled.items()):
            name = str(channel["branch"])
            branch = info.branches.get(label)
            if branch is None:
                listed = ", ".join(sorted(info.branches)) or "<none>"
                hint = (
                    "; privatebranches=1 means it may be password-protected and hidden from an anonymous login"
                    if info.private_branches
                    else ""
                )
                raise UpstreamUnavailable(
                    f"branch '{label}' of app {watch.app} is not listed by app_info{hint}. Listed: {listed}"
                )
            depots, credentials_env = self._published(info, watch, label, branch, channel)
            rev = self._rev(branch.buildid, depots, name)
            published = branch.published_at
            facts = self._facts(
                info,
                watch,
                label,
                branch,
                depots,
                credentials_env=credentials_env,
                release_time=(
                    steamcmd.iso(published) if published is not None else channels.first_seen(checkpoint, rev, now)
                ),
            )
            promoted_from = self._promoted_from(checkpoint, rev, name)
            if promoted_from:
                facts["promotedFrom"] = promoted_from
            rolled_back_from = channels.head_event(checkpoint, (watch.component, name), rev, _revs_of(name))
            if rolled_back_from:
                facts["rollbackFrom"] = rolled_back_from
                rev = channels.rollback_rev(rev, rolled_back_from)
            seen.append(self._observation(watch, name, rev, watch.kind, facts, now))
            heads[name] = rev

        for label in sorted(info.branches):
            if label in watch.declared or watch.is_known(label):
                continue
            seen.append(self._review(info, watch, label, checkpoint, now, heads))

        return ProviderResult(
            source_id=str(source.get("id") or self.id),
            status="ok",
            heads=heads,
            observations=seen,
            history="heads-only",
        )

    def _review(
        self,
        info: steamcmd.AppInfo,
        watch: _Watch,
        label: str,
        checkpoint: dict[str, Any] | None,
        now: str,
        heads: dict[str, str],
    ) -> Observation:
        """A branch Steam publishes that the catalog has never decided about.

        No credential is looked up for it: nobody declared it, so nothing about it is
        worth a password. An encrypted branch is still reported — its build id is public —
        and its revision simply carries no manifest digest.
        """
        branch = info.branches[label]
        name = branch_name(label)
        depots = {
            depot_id: {
                "manifest": info.depots[depot_id].manifests[label].gid,
                "size": info.depots[depot_id].manifests[label].size,
                "download": info.depots[depot_id].manifests[label].download,
            }
            for depot_id in watch.depots
            if depot_id in info.depots and label in info.depots[depot_id].manifests
        }
        rev = self._rev(branch.buildid, depots, name)
        published = branch.published_at
        facts = self._facts(
            info,
            watch,
            label,
            branch,
            depots,
            credentials_env=None,
            release_time=(
                steamcmd.iso(published) if published is not None else channels.first_seen(checkpoint, rev, now)
            ),
        )
        facts["channel"] = label
        facts["reason"] = "listed by Steam but declared by no channel and matched by no knownBranches entry"
        # In ``heads`` so a bootstrap files it rather than swallowing it, but never over a
        # watched branch: a label that folds onto a watched name is the watched head's.
        heads.setdefault(name, rev)
        return self._observation(watch, name, rev, "branch-review", facts, now)

    def _observation(
        self,
        watch: _Watch,
        branch: str,
        rev: str,
        kind: str,
        facts: dict[str, Any],
        now: str,
    ) -> Observation:
        return Observation(
            provider=self.id,
            component=watch.component,
            branch=branch,
            rev=rev,
            kind=kind,
            identity=identity.canonical(self.id, watch.component, branch, rev),
            facts=facts,
            observed_at=now,
        )

    # -- how the tracker renders and reconciles what was seen --------------------
    def covers(self, observation: Observation, targets: list[Target]) -> bool | None:
        """Whether a target already pins exactly this app, branch, build and manifest set.

        The generic answer — the observation's revision equals a target's ``revision`` —
        is wrong here twice over: a Steam target is named by the game's own version
        (``3.2.0.b10``), and what a revision has to match is five facts rather than a
        string. So the inputs are read instead, and a target that does not pin every
        watched depot does not cover the head.
        """
        if observation.kind != "game":
            return False
        facts = observation.facts
        wanted = {str(depot): str(entry["manifest"]) for depot, entry in (facts.get("depots") or {}).items()}
        if not wanted:
            return False
        for target in targets:
            for spec in (target.record.get("inputs") or {}).values():
                if not isinstance(spec, dict) or spec.get("kind") != "steam-depots":
                    continue
                if str(spec.get("app")) != str(facts.get("app")) or str(spec.get("branch")) != str(facts.get("label")):
                    continue
                if str(spec.get("buildid")) != str(facts.get("buildid")) or str(spec.get("os")) != str(facts.get("os")):
                    continue
                pinned = {str(depot): str(entry.get("manifest")) for depot, entry in (spec.get("depots") or {}).items()}
                if all(pinned.get(depot) == manifest for depot, manifest in wanted.items()):
                    return True
        return False

    def presentation(self, observation: Observation, game_name: str) -> dict[str, Any] | None:
        """The Steam-shaped issue: which branch moved, to what, and how to pin it.

        The generic rendering names a revision and a release time, which for Steam is a
        digest and a timestamp and says nothing a maintainer can act on. What this adds is
        the identity itself — app, branch label, build id, depot manifests — the limit of
        what was observed, and the one command that turns it into a target record.
        """
        if observation.kind != "game":
            return None
        from ..tracker import issues

        facts = observation.facts
        dash = issues.DASH
        label = str(facts.get("label", dash))
        buildid = facts.get("buildid", dash)
        rolled_back_from = facts.get("rollbackFrom")
        promoted_from = facts.get("promotedFrom")
        if rolled_back_from:
            title = f"{game_name} {label}: rolled back to build {buildid}"
        elif promoted_from:
            title = f"{game_name} {label}: build {buildid} promoted from {promoted_from}"
        else:
            title = f"{game_name} {label}: build {buildid} needs a target"

        depot_rows = [
            f"| Depot {depot} manifest | `{entry.get('manifest', dash)}` ({entry.get('size', dash)} bytes) |"
            for depot, entry in sorted((facts.get("depots") or {}).items())
        ]
        moved = []
        if rolled_back_from:
            moved.append(f"| Rolled back from | build `{rolled_back_from}` |")
        if promoted_from:
            moved.append(f"| Promoted from | `{promoted_from}` |")
        credentials = facts.get("credentialsEnv")
        description = f" ({facts['description']})" if facts.get("description") else ""

        steps = issues.next_steps(observation)
        steps[1] = (
            f"2. `maintenance/bin/takaro-maint steam pin --game {observation.component} --branch {label} "
            f"--metadata --record-files <declared file>… --write`, or add "
            f"`catalog/{observation.component}/targets/<platform>-<revision>.json` following "
            f"`catalog/README.md` with build id `{buildid}` and the depot manifests above."
        )
        return {
            "title": title,
            "intro": (
                f"Steam moved the `{label}` branch of app {facts.get('app', dash)} "
                f"({facts.get('appName') or 'the dedicated server'}). Everything between the owned markers "
                "is rewritten by `takaro-maint scan`; edit anything else freely — but leave the first "
                "line where it is, because that marker is how this issue is recognised."
            ),
            "observationRows": issues.observation_rows(
                observation,
                [
                    f"| App | `{facts.get('app', dash)}` ({facts.get('appName') or dash}) |",
                    f"| Branch label | `{label}`{description} |",
                    f"| Build id | {buildid} |",
                    f"| OS | {facts.get('os', dash)} |",
                    *depot_rows,
                    *moved,
                    f"| Observation limit | {facts.get('observationLimit', dash)} |",
                    f"| Credentials | {f'env `{credentials}`' if credentials else 'anonymous'} |",
                    f"| Steam change number | {facts.get('changeNumber', dash)} |",
                ],
            ),
            "readinessLines": [
                "This game has no framework layer: the connector compiles against the server assemblies "
                "fetched by `takaro-maint steam references`. Nothing upstream is waited for."
            ],
            "nextSteps": steps,
        }


def _seen(checkpoint: dict[str, Any] | None) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for item in (checkpoint or {}).get("seen") or []:
        if isinstance(item, list | tuple) and len(item) == 2:
            entries.append((str(item[0]), str(item[1])))
        else:
            entries.append((str(item), ""))
    return entries


def _revs_of(branch: str) -> Callable[[str], bool]:
    """Which checkpoint revisions belong to one branch: the ones whose head ends in it."""

    def predicate(rev: str) -> bool:
        return channels.split_rollback_rev(rev)[0].endswith(f"+{branch}")

    return predicate


PROVIDER = SteamProvider()
