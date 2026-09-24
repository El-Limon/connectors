# Takaro ARK: Survival Evolved Connector

This server-side native Linux connector targets ARK: Survival Evolved. Players do not install anything. It is pinned to Steam app 376030, public build 21241282, depot 376031 manifest 6366771435093287465, and `ShooterGameServer` SHA-256 `7e7ded49e0c658e74199801d79630bd33da407d3e468e039837bf61a9de7c520`. The native library checks the executable again at launch. A different executable is refused. The target remains **candidate** until the remaining native actions, gameplay events, and recovery behavior are verified.

## Install

### 1. Build and deploy the exact target

These candidate instructions require a checkout of [the connectors repository](https://github.com/gettakaro/connectors), Docker, and an ARK Linux server at the pinned build. Run the commands from the checkout root. They use the catalog and produce exact-target artifacts:

```sh
maintenance/bin/takaro-maint catalog validate > /tmp/ark-catalog-validation.json
maintenance/bin/takaro-maint targets resolve --game ark --target linux-21241282 --format json > /tmp/ark-target.json
maintenance/bin/takaro-maint install --game ark --target linux-21241282 --dest /srv/ark --dry-run
maintenance/bin/takaro-maint install --game ark --target linux-21241282 --dest /srv/ark --reuse-world
maintenance/bin/takaro-maint build --game ark --target linux-21241282 --version 0.1.0 --out /tmp/ark-artifacts
maintenance/bin/takaro-maint deploy --game ark --target linux-21241282 --dest /srv/ark --from /tmp/ark-artifacts/build-manifest.json
```

`build` uses the target's immutable Node Bookworm toolchain image and runs the native and sidecar tests. The deployment writes only `/srv/ark/TakaroArk/TakaroArkNative` and `/srv/ark/TakaroArk/TakaroArkSidecar`, atomically replacing each owned folder. It preserves `ShooterGame/Saved`, `.takaro`, and the other component folder. The `takaro-target.json` and `uninstall-manifest.json` shipped in each zip record its exact ownership and target. Keep the build manifest with the artifacts; do not substitute an older native library into a newer zip.

### 2. Configure and start

Set the same long random `ARK_NATIVE_TOKEN` for the game and sidecar. Start the game through the shipped `/srv/ark/TakaroArk/TakaroArkNative/launch.sh /srv/ark 'TheIsland?listen?SessionName=Takaro-ARK?Port=7787?QueryPort=27025' -server -log -NoBattlEye`. The launcher checks the exact executable hash and sets `LD_PRELOAD` only for `ShooterGameServer`. Do not preload SteamCMD. When containerized, run the game with Docker `--init` or Compose `init: true`, so the game is not namespace PID 1. ARK's normal acknowledged shutdown saves the world and intentionally exits with signal 6 (status 134); a PID 1 process can instead hit glibc's abort fallback and exit 139. The sidecar needs `TAKARO_REGISTRATION_TOKEN`, `TAKARO_IDENTITY_TOKEN`, `TAKARO_WS_URL`, `ARK_NATIVE_TOKEN`, `ARK_NATIVE_URL=http://127.0.0.1:18891`, and a persistent `TAKARO_CURSOR_FILE`; run it in the server's network namespace so the native bearer API stays on loopback. The shipped `TakaroArkSidecar/Dockerfile` builds from the compiled release files. `dev-servers/compose/ark.yml` supplies a concrete two-container rig.

### 3. Check the connection

For an isolated verification run, use `maintenance/bin/takaro-maint verify --help` and the ARK-specific checks in `maintenance/src/takaro_maint/games/ark/verify.py`. A bare run cannot prove PC chat, player location/inventory, or unsupported actions without real clients; those checks fail openly rather than inventing success. The `--ark-readonly-base` mode passed the pinned v20 isolated protocol, including two acknowledged saves and status-134 exits plus a distinct-boot reload that read the unchanged owned world save. That protocol run does not cover real PC gameplay. Do not run the verifier against an active production server; it manages its own container and shutdown.

### 4. Uninstall

To uninstall, stop the sidecar and game, then remove only the two paths named by their `uninstall-manifest.json` records: `/srv/ark/TakaroArk/TakaroArkNative` and `/srv/ark/TakaroArk/TakaroArkSidecar`. Keep `/srv/ark/ShooterGame/Saved` and `/srv/ark/.takaro`; the Steam server files and world are outside connector ownership. The launcher need not remain after uninstall, and starting the ordinary `ShooterGameServer` without it leaves the server unmodified.

## What works, what doesn't

As of 2026-09-24, this remains a candidate for ARK public build **21241282**. The current v20 deployment uses source revision `a25e3a30892b7dc6751d28e7c907f3a1f99a5585`, native library SHA-256 `002faf257402e59aa2f3e5ef30dcffeffc7dd8d4e0de2e887af6b8c5f981b90c`, and sidecar ZIP SHA-256 `1484545f4141239c729d20214a72306ef2ef898495bc162f83466880f31d8e8b`. The [target issue #289](https://github.com/gettakaro/connectors/issues/289) and [draft PR #291](https://github.com/gettakaro/connectors/pull/291) track remaining verification. Earlier v17 gameplay evidence remains historical and does not count toward v20 acceptance.

| Capability | Status | Evidence and limit |
| --- | --- | --- |
| Exact executable guard and deployed identity | Observed on v20 | The pinned native library and sidecar booted against the exact server build; isolated wrong-target tests reject a different executable. |
| Native health and sidecar connection | Observed on v20 | Authenticated native health and sidecar identification reported the same live boot ID. |
| Real client chat and server broadcast | Observed on v20 | PC-typed `9904` persisted as a Takaro chat event; a Takaro `sendMessage` with marker `9905` appeared as yellow SERVER chat in the ARK client. Full same-boot native attribution is under review. |
| Read-only engine console command and native ban list | Pending on v20 | Both had narrow v17 proof, which remains historical. The isolated v20 protocol checked a handled console dispatch and empty native ban list; real live behavior still needs current evidence. |
| Location, inventory, grant, teleport, death, catalog, modules, and recovery | Pending on v20 | These need their own current-artifact acceptance evidence. A sidecar outage longer than two minutes retained native chat and leave events but delayed Takaro hydration failed after the player left; recovery needs a fix and retest. `listLocations` has no available Takaro SDK/MCP operation despite native support. |
