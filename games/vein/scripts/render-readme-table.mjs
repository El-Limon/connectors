#!/usr/bin/env node
// Regenerates the "What works, what doesn't" table of games/vein/README.md from a
// capabilities.json (the campaign's evidence matrix).
//
//   node scripts/render-readme-table.mjs <capabilities.json> [--write <README.md>]
//
// Without --write the table is printed to stdout. With --write the block between the
// REGENERATE markers in the README is replaced in place.
//
// The row set is fixed (49 rows, identical to the Dragonwilds connector README) so the
// two connectors stay comparable. Only the symbol comes from capabilities.json; the note
// is authored here, optionally per status.
//
// Symbols: proven -> OK; partial, plugin-proven and any "awaiting-*" status -> CAVEAT;
//          blocked, pending and anything unknown -> NO.

import { readFileSync, writeFileSync } from 'node:fs';

const OK = '✅';      // white heavy check mark
const CAVEAT = '⚠️'; // warning sign
const NO = '❌';      // cross mark

const START = '<!-- REGENERATE FROM capabilities.json AT L7b -->';
const END = '<!-- END REGENERATE -->';

/** @type {Array<{label:string, keys?:string[], fixed?:string, note:string, notes?:Record<string,string>}>} */
const ROWS = [
  // ---- actions (17 capabilities, 18 rows: sendMessage covers broadcast + whisper) ----
  { label: 'Connection & heartbeat', keys: ['actions.testReachability'],
    note: 'The sidecar keeps an outbound WebSocket to Takaro and the server shows as reachable while it is up.' },
  { label: 'Player list', keys: ['actions.getPlayers'],
    note: 'Name, ping and whether the player has spawned. `gameId` is the player’s SteamID64 and `platformId` is `steam:<that id>`.' },
  { label: 'Single player lookup', keys: ['actions.getPlayer'],
    note: 'Same data as the player list, for one player. An offline player still answers with a last-known record instead of an error.' },
  { label: 'Player location', keys: ['actions.getPlayerLocation'],
    note: 'The player’s position in the world, read straight from the pawn and following walking and teleports.' },
  { label: 'Player inventory', keys: ['actions.getPlayerInventory'],
    note: 'What the player is carrying, read from the character’s inventory. Items VEIN keeps as one object per unit (corn, MREs, insulin…) are reported as a single row with the summed count, the way the game’s own bag shows them. While the player is dead the inventory is empty and no corpse loot leaks in.',
    notes: { partial: 'What the player is carrying, read by reflection from the inventory component. Matches the in-game bag, but it was not re-checked against Takaro after every item type.' } },
  { label: 'Give an item', keys: ['actions.giveItem'],
    note: 'The item appears in the player’s inventory without a relog. For an item VEIN keeps as one object per unit, asking for N gives N separate objects, which the bag then groups.',
    notes: { failed: 'Does not work yet. The game’s own give-item path is wired up but the item does not reach the player, so Takaro’s give and the shop’s delivery both fail.' } },
  { label: 'Item catalogue', keys: ['actions.listItems'],
    note: 'Every item class the server has loaded (1350 on the tested build), synced into Takaro’s item list. Names are the internal class names unless `TAKARO_ITEM_NAMES=1` is set, which asks the game for the readable name at start-up.',
    notes: { partial: 'Every item class the server has loaded. Names are the internal class names unless `TAKARO_ITEM_NAMES=1` is set, which asks the game for the readable name at start-up.' } },
  { label: 'Entity catalogue', keys: ['actions.listEntities'],
    note: 'The zombie and animal types the server knows about.',
    notes: { partial: 'The zombie and animal types the server has loaded are synced into Takaro, but Takaro’s catalogue also keeps a few rows from earlier game builds, because its sync only adds and updates and never deletes.' } },
  { label: 'Locations / points of interest', keys: ['actions.listLocations'],
    note: 'The connector serves the location markers the world has streamed in.',
    notes: { partial: 'The connector serves the location markers currently streamed in, but Takaro never asks for them, so it cannot be checked end to end.' } },
  { label: 'Run a console command', keys: ['actions.executeConsoleCommand'],
    note: 'VEIN has no operator console, so the connector provides its own set (`help`, `players`, `say`, `whisper`, `give`, `tp`, `kick`, `ban`, `unban`, `bans`, `save`, `shutdown`). An unknown command comes back as a failure with the reason.' },
  { label: 'Broadcast a message', keys: ['actions.sendMessage'],
    note: 'Everyone sees the message. It renders as a chat line prefixed with the server name, because VEIN has no separate "server" chat sender.' },
  { label: 'Whisper a player', keys: ['actions.sendMessage'],
    note: 'Reaches the one player, but as an on-screen notification rather than a chat line — VEIN cannot target a chat message at a single client.' },
  { label: 'Teleport a player', keys: ['actions.teleportPlayer'],
    note: 'The player is moved to the requested position.',
    notes: { failed: 'Does not work yet. The request is accepted but the player does not move, so the teleports module and Takaro’s teleport both fail.' } },
  { label: 'Kick a player', keys: ['actions.kickPlayer'],
    note: 'The player is dropped from the server and can rejoin afterwards.',
    notes: { failed: 'Does not work yet. The request reports success but the player stays connected. Use a ban followed by an unban to remove someone.' } },
  { label: 'Ban a player (timed and permanent)', keys: ['actions.banPlayer'],
    note: 'The player is disconnected and refused on rejoin. Bans go into VEIN’s own ban list and into the connector’s, so an offline player can be banned too. Timed bans are lifted by the connector when they expire; the game has no expiry of its own.' },
  { label: 'Unban a player', keys: ['actions.unbanPlayer'],
    note: 'Clears the ban in the game and in the connector, and the player can rejoin at once.' },
  { label: 'Ban list', keys: ['actions.listBans'],
    note: 'The game’s own bans unioned with the connector’s. Reason and expiry are kept by the connector, because the game stores neither.' },
  { label: 'Shut the server down', keys: ['actions.shutdown'],
    note: 'Saves the world first, then quits cleanly; your restart policy brings it back.' },

  // ---- events (6) ----
  { label: 'Player joined event', keys: ['events.player-connected'],
    note: 'Arrives in Takaro on every join, with the player’s SteamID64.' },
  { label: 'Player left event', keys: ['events.player-disconnected'],
    note: 'Arrives on a clean quit and after a server crash by reconciliation.' },
  { label: 'Player chat event', keys: ['events.chat-message'],
    note: 'Real player chat reaches Takaro; messages the connector itself sent are not echoed back.' },
  { label: 'Player death event', keys: ['events.player-death'],
    note: 'Reaches Takaro when a player dies, with the position and what killed them. A death with no killer — a fall or drowning — is reported with no attacker rather than blaming the victim.',
    notes: { failed: 'Not working yet — player deaths are not reported to Takaro.' } },
  { label: 'Entity kill event', keys: ['events.entity-killed'],
    note: 'A creature killed by a player reaches Takaro with the creature’s name, the player and the item they were holding. Kills the game’s own AI makes among itself are not reported, because no player was involved.',
    notes: { failed: 'Not working yet — creature kills are not reported to Takaro.' } },
  { label: 'Log events', keys: ['events.log'],
    note: 'The connector forwards server log lines with passwords and Steam tickets redacted.',
    notes: { partial: 'The connector forwards server log lines (passwords and Steam tickets redacted, checked live), but Takaro does not store log lines as events, so they cannot be searched or used in modules.',
             failed: 'The connector forwards server log lines (redacted), but Takaro does not store log lines as events, so they cannot be searched or used in modules.' } },

  // ---- not applicable to this connector type ----
  { label: 'Map info', fixed: NO, note: 'Takaro does not support map info for Generic game servers, so there is nothing for the connector to serve.' },
  { label: 'Map tiles', fixed: NO, note: 'Takaro does not support map tiles for Generic game servers. There is no map view for a VEIN server.' },

  // ---- modules (7 capabilities, 5 rows) ----
  { label: 'Modules: chat commands', keys: ['modules.command'],
    note: 'In-game chat commands with the domain’s prefix reach the module and answer in chat.',
    notes: { failed: 'Not verified in game yet — it needs a human to type a command at the keyboard.' } },
  { label: 'Modules: hooks', keys: ['modules.hook'],
    note: 'Chat and join hooks fire and run their module code.' },
  { label: 'Modules: cronjobs', keys: ['modules.cronjob'],
    note: 'Scheduled module runs fire and can message the server.',
    notes: { partial: 'Scheduled module runs fire on their own schedule and run their code; the in-game side effect was not checked for every run.' } },
  { label: 'Modules: teleports (`@settp`, `@tp`, …)', keys: ['modules.teleports'],
    note: 'The teleports module’s in-game commands move the player.',
    notes: { failed: 'Blocked by the teleport action above — the commands are accepted but the player does not move.' } },
  { label: 'Modules: server messages / onboarding', keys: ['modules.serverMessages', 'modules.playerOnboarding'],
    note: 'Timed server messages and the welcome message on join are delivered in game.',
    notes: { partial: 'The welcome message on join is delivered in game; timed server messages fire on schedule and were checked on the wire rather than for every in-game line.' } },

  // ---- shop + economy (7) ----
  { label: 'Shop: buy in game', keys: ['shop.buyInGame'],
    note: 'Buying from the shop with the in-game chat command.',
    notes: { failed: 'Not verified in game yet — it needs a human to run the in-game shop commands, and item delivery depends on the give action above.' } },
  { label: 'Shop: order in Takaro and claim in game', keys: ['shop.orderViaMcp'],
    note: 'An order placed in Takaro delivers the items to the player.',
    notes: { partial: 'The order is paid and claimed and the currency moves, but the items do not actually arrive while the give action is broken.' } },
  { label: 'Shop: bundle of several items', keys: ['shop.multiItemBundle'],
    note: 'One claim delivers every item in the listing.',
    notes: { partial: 'One claim does issue a delivery for every item in the listing; the items themselves do not arrive while the give action is broken.' } },
  { label: 'Shop: order while offline, claim later', keys: ['shop.orderWhileOffline'],
    note: 'The claim is refused while the player is offline and succeeds after rejoining.',
    notes: { partial: 'The claim is correctly refused while the player is offline and succeeds after the player rejoins. Not yet followed as one single order from offline all the way to the items counted in the world.' } },
  { label: 'Shop: not enough currency', keys: ['shop.insufficientFunds'],
    note: 'The purchase is refused and the balance is unchanged.' },
  { label: 'Economy: currency', keys: ['shop.economyEnabled'],
    note: 'Balances are set, read and debited by Takaro.' },
  { label: 'Economy: balance / top list in game', keys: ['shop.balance'],
    note: 'The in-game economy commands answer in chat.',
    notes: { failed: 'Not verified in game yet — it needs a human to type the balance command.' } },

  // ---- Discord (6 capabilities, 5 rows; noEcho shares a row with chatBridgeNoEcho) ----
  { label: 'Discord: game chat → Discord', keys: ['discord.gameToDiscord'],
    note: 'In-game chat is relayed to the linked Discord channel.',
    notes: { failed: 'Not verified with a real in-game chat line yet; the bridge itself accepts and posts player-authored messages.' } },
  { label: 'Discord: Discord → game chat', keys: ['discord.discordToGame'],
    note: 'A message posted in the linked Discord channel appears in the game chat.',
    notes: { partial: 'The path is wired and every other direction works, but the chat-bridge module ignores messages from bots, so this direction can only be confirmed by a real human post in the linked channel — that one check is still outstanding.' } },
  { label: 'Discord: module hook / cronjob posts', keys: ['discord.customHookToDiscord', 'discord.customCronToDiscord'],
    note: 'Module hooks and cronjobs can post to Discord and edit their own messages.' },
  { label: 'Discord: join/leave notices', keys: ['discord.joinLeaveNotices'],
    note: 'Join and leave notices posted to Discord by the chat-bridge module.' },
  { label: 'Discord: no echo of server messages', keys: ['discord.noEcho', 'modules.chatBridgeNoEcho'],
    note: 'The stock `chatBridge` module re-posts Takaro’s own server messages to Discord (a Takaro-core echo affecting every game); the `chatBridgeNoEcho` fork does not.' },

  // ---- resilience (9 capabilities, 6 rows) ----
  { label: 'Events while the Takaro connection is down', keys: ['resilience.outageReconnect'],
    note: 'Events that happen while Takaro is unreachable are kept and delivered in order once the connection is back. An event counts as delivered only when Takaro’s heartbeat confirms it, so nothing is lost at the moment the connection dies.',
    notes: { failed: 'Designed for and implemented (queued events, ordered delivery after reconnect) but not yet exercised against a real outage.' } },
  { label: 'Reconnects after a server or container restart',
    keys: ['resilience.containerRestartReconnect', 'resilience.serverRestartOnlineReconcile', 'resilience.pluginRestartDetection'],
    note: 'The connector comes back and re-identifies on its own, and players who were online are reported as disconnected. When it shares the game container’s network it restarts itself after a game-container restart, so keep its restart policy on.',
    notes: { failed: 'Implemented (restart detection, online-set reconciliation, a fresh plugin boot id resets the cursor) but not yet exercised end to end.' } },
  { label: 'No duplicate events after a connector restart', keys: ['resilience.sidecarRestartNoReplay'],
    note: 'The event cursor is persisted, so a sidecar restart replays nothing.',
    notes: { failed: 'The event cursor is persisted so a sidecar restart should replay nothing; not yet verified against Takaro’s event totals.' } },
  { label: 'Survives a network drop to Takaro', keys: ['resilience.wsDropReconnect'],
    note: 'The WebSocket reconnects by itself with a backoff of 2 to 60 seconds and re-identifies as the same server.',
    notes: { failed: 'The WebSocket is built to reconnect by itself with a backoff of 2 to 60 seconds; not yet verified against a forced socket drop.' } },
  { label: 'Timed bans expire on their own', keys: ['resilience.timedBanLiftedByConnector'],
    note: 'The connector lifts a timed ban when it runs out, including when it was restarted in between.',
    notes: { failed: 'Implemented — the connector lifts a timed ban at expiry because Takaro sends no unban — but not yet verified across a restart.' } },
  { label: 'Keeps running after a game update breaks a feature', keys: ['resilience.symbolSelfCheckDegrade'],
    note: 'The plugin self-checks at load and a feature it can no longer find reports `degraded` in `/health` and in Takaro’s reachability reason while the server and everything else keep running.',
    notes: { failed: 'The plugin self-checks at load and marks a feature it can no longer find as `degraded` in `/health` while the server keeps running. Built and unit-tested, not yet exercised with a deliberately broken build on the live rig.' } },
];

const CLASS = {
  proven: 'proven',
  partial: 'partial',
  'plugin-proven': 'partial',
  blocked: 'failed',
  pending: 'failed',
};
// A status of the form "awaiting-<someone>" means one human action is outstanding,
// not that the feature failed.
const classify = (status) => CLASS[status] ?? (String(status).startsWith('awaiting-') ? 'partial' : 'failed');
const SYMBOL = { proven: OK, partial: CAVEAT, failed: NO };
const RANK = { proven: 0, partial: 1, failed: 2 };

function lookup(caps, dotted) {
  const [section, ...rest] = dotted.split('.');
  const key = rest.join('.');
  const row = caps?.[section]?.[key];
  if (!row) throw new Error(`capabilities.json has no ${dotted}`);
  return row.status;
}

function render(caps) {
  const lines = ['| What | | Notes |', '|---|---|---|'];
  for (const row of ROWS) {
    let cls;
    if (row.fixed) {
      cls = row.fixed === OK ? 'proven' : row.fixed === CAVEAT ? 'partial' : 'failed';
    } else {
      cls = row.keys
        .map((k) => classify(lookup(caps, k)))
        .reduce((a, b) => (RANK[b] > RANK[a] ? b : a), 'proven');
    }
    const note = row.notes?.[cls] ?? row.note;
    lines.push(`| ${row.label} | ${SYMBOL[cls]} | ${note} |`);
  }
  return lines.join('\n');
}

const args = process.argv.slice(2);
const capsPath = args[0];
if (!capsPath) {
  console.error('usage: render-readme-table.mjs <capabilities.json> [--write <README.md>]');
  process.exit(2);
}
const caps = JSON.parse(readFileSync(capsPath, 'utf8'));
const table = render(caps);

const writeIdx = args.indexOf('--write');
if (writeIdx === -1) {
  console.log(table);
} else {
  const readmePath = args[writeIdx + 1];
  if (!readmePath) { console.error('--write needs a README path'); process.exit(2); }
  const readme = readFileSync(readmePath, 'utf8');
  const a = readme.indexOf(START);
  const b = readme.indexOf(END);
  if (a === -1 || b === -1) { console.error(`markers ${START} / ${END} not found in ${readmePath}`); process.exit(1); }
  const out = readme.slice(0, a + START.length) + '\n\n' + table + '\n\n' + readme.slice(b);
  writeFileSync(readmePath, out);
  console.error(`rewrote ${ROWS.length} rows in ${readmePath}`);
}
