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

Set the same long random `ARK_NATIVE_TOKEN` for the game and sidecar. Start the game through the shipped `/srv/ark/TakaroArk/TakaroArkNative/launch.sh /srv/ark 'TheIsland?listen?SessionName=Takaro-ARK?Port=7787?QueryPort=27025' -server -log -NoBattlEye`. The launcher checks the exact executable hash and sets `LD_PRELOAD` only for `ShooterGameServer`. Do not preload SteamCMD. The sidecar needs `TAKARO_REGISTRATION_TOKEN`, `TAKARO_IDENTITY_TOKEN`, `TAKARO_WS_URL`, `ARK_NATIVE_TOKEN`, `ARK_NATIVE_URL=http://127.0.0.1:18891`, and a persistent `TAKARO_CURSOR_FILE`; run it in the server's network namespace so the native bearer API stays on loopback. The shipped `TakaroArkSidecar/Dockerfile` builds from the compiled release files. `dev-servers/compose/ark.yml` supplies a concrete two-container rig.

### 3. Check the connection

For an isolated verification run, use `maintenance/bin/takaro-maint verify --help` and the ARK-specific checks in `maintenance/src/takaro_maint/games/ark/verify.py`. A bare run cannot prove PC chat, player location/inventory, or unsupported actions without real clients; those checks fail openly rather than inventing success. Do not run the verifier against an active production server; it manages its own container and shutdown.

### 4. Uninstall

To uninstall, stop the sidecar and game, then remove only the two paths named by their `uninstall-manifest.json` records: `/srv/ark/TakaroArk/TakaroArkNative` and `/srv/ark/TakaroArk/TakaroArkSidecar`. Keep `/srv/ark/ShooterGame/Saved` and `/srv/ark/.takaro`; the Steam server files and world are outside connector ownership. The launcher need not remain after uninstall, and starting the ordinary `ShooterGameServer` without it leaves the server unmodified.

## What works, what doesn't

As of 2026-09-24, this candidate targets ARK public build **21241282**. No capability below has been verified with a live server and real PC clients. ✅ means verified live, ⚠️ means implemented or checked only in source or isolated tests, and ❌ means unavailable.

| Capability | Status | Evidence and limit |
| --- | --- | --- |
| Exact executable guard and deployment layout | ⚠️ | Implemented and covered by isolated tests; live server verification is pending. |
| Native health and sidecar connection | ⚠️ | Implemented; live server verification is pending. |
| Player roster, chat, location, and inventory | ⚠️ | Require real PC-client evidence; a bare verifier run cannot establish them. |
| Native actions, gameplay events, and recovery | ⚠️ | Candidate work remains unfinished or unverified. |
