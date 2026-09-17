import { asRecord } from '../takaro/protocol.js';
import { logger } from '../logger.js';
import type { BanStore, PendingBan } from './banStore.js';
import type { KnownPlayerStore } from './knownStore.js';
import {
  mapBan,
  mapEntity,
  mapInventoryItem,
  aggregateInventory,
  mapItemDefinition,
  mapLocation,
  mapPlayer,
  mapPosition,
  num,
  str,
} from './mapping.js';
import { VeinPluginClient, PluginHttpError, PluginUnimplementedError } from './pluginClient.js';
import type { PluginPlayer, TakaroBan, TakaroPlayer } from './types.js';

export class ActionError extends Error {}

type Args = Record<string, unknown>;

export interface AdapterOptions {
  /** Persisted timed bans; when absent, timed bans are not lifted automatically. */
  banStore?: BanStore;
  /** Persisted last-known players, so getPlayer can answer for offline players after a sidecar restart (F7). */
  knownStore?: KnownPlayerStore;
  /** How many last-known player records to keep (default 500). */
  maxKnownPlayers?: number;
  /** TAKARO_SENDER_NAME (preferred chat sender). */
  senderName?: string;
  /** TAKARO_SERVER_NAME (fallback chat sender). */
  serverName?: string;
  /** How often to sweep expired bans while running (default 30s). */
  banSweepIntervalMs?: number;
  /**
   * Fallback online-player source: Vein's own read-only HTTP API (VEIN_HTTP_API). Consulted ONLY when our plugin's
   * /players fails, so getPlayers still answers a real list during a plugin restart. Returns null when unavailable.
   */
  httpPlayers?: () => Promise<PluginPlayer[] | null>;
}

/** Strips a `steam:` platform prefix from a Takaro identifier. */
export function stripPlatform(id: string): string {
  return id.replace(/^steam:/i, '');
}

/**
 * Maps the 17 Takaro generic-connector actions onto the Vein plugin HTTP API.
 * Returns the Takaro response payload, or throws (ActionError / plugin errors) so the caller answers with an error frame.
 */
export class VeinAdapter {
  private pending: PendingBan[];
  private sweepTimer: NodeJS.Timeout | null = null;
  private banStoreCold = false;
  /**
   * Last-known record per player, keyed by every identifier we saw them under. Takaro calls `getPlayer` for players who
   * are NOT in world (e.g. while running a `commandTrigger`), and an empty object fails its IGamePlayer validation
   * ("property gameId ... isString") with a misleading "mod is out of date" 400 (finding F7).
   */
  private readonly known = new Map<string, TakaroPlayer>();

  constructor(
    private readonly plugin: VeinPluginClient,
    private readonly options: AdapterOptions = {},
  ) {
    this.pending = [];
    this.loadPending();
    for (const player of options.knownStore?.load() ?? []) this.index(player);
  }

  /** Chat sender: per-message override, then TAKARO_SENDER_NAME, then TAKARO_SERVER_NAME, then "Server". */
  senderName(override?: string | null): string {
    return override || this.options.senderName || this.options.serverName || 'Server';
  }

  start(): void {
    if (this.sweepTimer) return;
    this.sweepTimer = setInterval(() => void this.sweepExpiredBans(), this.options.banSweepIntervalMs ?? 30_000);
    void this.sweepExpiredBans();
  }

  stop(): void {
    if (this.sweepTimer) clearInterval(this.sweepTimer);
    this.sweepTimer = null;
  }

  /**
   * (Re-)reads the persisted timed bans. Called in the constructor AND at the top of every `listBans`, so the
   * on-disk store — not our in-memory copy — is the source of truth for `expiresAt` even on the very first answer
   * after a restart (F16). A store that exists but cannot be read marks the adapter "cold" and `listBans` fails
   * rather than reporting a timed ban as permanent.
   */
  private loadPending(): void {
    const store = this.options.banStore;
    if (!store) {
      this.banStoreCold = false;
      return;
    }
    try {
      this.pending = store.load();
      this.banStoreCold = false;
    } catch (err) {
      this.banStoreCold = true;
      logger.warn(`Timed-ban store unavailable: ${(err as Error).message}`);
    }
  }

  /** True when the persisted timed-ban store could not be read; `listBans` then refuses to answer. */
  banStoreUnavailable(): boolean {
    return this.banStoreCold;
  }

  pendingBans(): PendingBan[] {
    return this.pending.map((b) => ({ ...b }));
  }

  /** Lifts every timed ban whose expiry has passed by calling the plugin's /unban. Returns the gameIds lifted. */
  async sweepExpiredBans(now = Date.now()): Promise<string[]> {
    const due = this.pending.filter((b) => Date.parse(b.expiresAt) <= now);
    const lifted: string[] = [];
    for (const ban of due) {
      try {
        await this.plugin.unban(ban.gameId);
        lifted.push(ban.gameId);
        logger.info(`Timed ban expired for ${ban.gameId}; called plugin /unban`);
      } catch (err) {
        // Keep it pending and retry on the next sweep (plugin down / game restarting).
        logger.warn(`Could not lift expired ban for ${ban.gameId}: ${(err as Error).message}`);
      }
    }
    if (lifted.length) this.setPending(this.pending.filter((b) => !lifted.includes(b.gameId)));
    return lifted;
  }

  async handleAction(action: string, args: Args): Promise<unknown> {
    try {
      return await this.dispatch(action, args);
    } catch (err) {
      if (err instanceof PluginUnimplementedError) {
        throw new ActionError(`Vein connector cannot perform '${action}': ${err.message}`);
      }
      throw err;
    }
  }

  private async dispatch(action: string, args: Args): Promise<unknown> {
    switch (action) {
      case 'testReachability':
        return this.testReachability();
      case 'getPlayers': {
        const players = await this.onlinePlayers();
        // getPlayers is the ONLINE set only; offline players are answered by getPlayer from the last-known cache.
        return (Array.isArray(players) ? players : []).filter((p) => p.online !== false).map((p) => this.remember(mapPlayer(p)));
      }
      case 'getPlayer': {
        const id = playerId(args);
        const found = await this.findPlayer(id);
        if (found) return this.remember(mapPlayer(found), true);
        try {
          return this.remember(mapPlayer(await this.plugin.getPlayer(stripPlatform(id))));
        } catch (err) {
          if (!(err instanceof PluginHttpError) || err.status !== 404) throw err;
        }
        // Offline but seen before: answer the last-known record so Takaro's validation passes.
        const last = this.lastKnown(id);
        if (last) return { ...last, online: false };
        // Never seen by this connector. Takaro accepts nothing but a valid IGamePlayer here: `{}` fails with
        // "property gameId ... isString", and `null` AND an error frame both fail with "No payload provided but
        // expected DTO: IGamePlayer" - all three surface as "the gameserver responded with bad data, please verify
        // that the mod is up to date" (F7, all three proven on the wire). Takaro only asks about players it already
        // knows, so echo the identifier back as a minimal offline record instead of breaking the caller.
        logger.warn(`getPlayer: no record for '${id}'; answering a minimal offline record built from the identifier`);
        return { ...mapPlayer({ gameId: stripPlatform(id) }), online: false };
      }
      case 'getPlayerLocation': {
        const pluginId = await this.resolvePluginId(playerId(args));
        return mapPosition(await this.plugin.getPlayerLocation(pluginId));
      }
      case 'getPlayerInventory': {
        const pluginId = await this.resolvePluginId(playerId(args));
        const items = await this.plugin.getPlayerInventory(pluginId);
        return aggregateInventory((Array.isArray(items) ? items : []).map(mapInventoryItem));
      }
      case 'giveItem': {
        const pluginId = await this.resolvePluginId(playerId(args));
        const code = str(args.item) ?? str(args.itemCode) ?? str(args.code) ?? str(asRecord(args.item).code) ?? fail("giveItem requires 'item'");
        const amount = num(args.amount) ?? num(args.quantity) ?? 1;
        if (amount <= 0) fail('giveItem amount must be positive');
        // Vein items have no quality tier: an explicit `quality` (or explicit JSON null) from Takaro is accepted and ignored.
        await this.plugin.give(pluginId, code, amount);
        return {};
      }
      case 'listItems':
        return listOf(await this.plugin.getItems(str(args.search) ?? undefined)).map(mapItemDefinition);
      case 'listEntities':
        return listOf(await this.plugin.getEntities()).map(mapEntity);
      case 'listLocations':
        return listOf(await this.plugin.getLocations()).map(mapLocation);
      case 'executeConsoleCommand': {
        const command = str(args.command) ?? fail("executeConsoleCommand requires 'command'");
        let result;
        try {
          result = await this.plugin.command(command);
        } catch (err) {
          // A command the plugin rejects (unknown command, bad arguments) is a *command* failure,
          // not a transport failure: Takaro expects a CommandOutput payload, and an error frame
          // makes it answer 400 "the gameserver responded with bad data".
          if (err instanceof PluginHttpError && err.status >= 400 && err.status < 500) {
            return { success: false, rawResult: '', errorMessage: err.body || `Command failed (HTTP ${err.status})` };
          }
          throw err;
        }
        const success = result.success !== false;
        const output = typeof result.output === 'string' ? result.output : '';
        return { success, rawResult: output, errorMessage: success ? null : output || 'Command failed' };
      }
      case 'sendMessage': {
        const message = str(args.message) ?? fail("sendMessage requires 'message'");
        const opts = asRecord(args.opts);
        const sender = this.senderName(str(opts.senderNameOverride));
        const recipient = asRecord(opts.recipient);
        const recipientId =
          str(recipient.gameId) ?? str(recipient.steamId) ?? str(recipient.platformId) ?? str(args.recipientGameId);
        await this.plugin.sendMessage(message, recipientId ? await this.resolvePluginId(recipientId) : undefined, sender);
        return {};
      }
      case 'teleportPlayer': {
        const pluginId = await this.resolvePluginId(playerId(args));
        const target = str(args.target);
        const x = num(args.x);
        const y = num(args.y);
        const z = num(args.z);
        // `dimension` is always null/absent for Vein (single world) and is ignored.
        if (x === null || y === null || z === null) {
          if (!target) fail('teleportPlayer requires numeric x, y, z');
          await this.plugin.teleport(pluginId, 0, 0, 0, { target });
          return {};
        }
        const yaw = num(args.yaw);
        await this.plugin.teleport(pluginId, x, y, z, { yaw: yaw ?? undefined, target: target ?? undefined });
        return {};
      }
      case 'kickPlayer': {
        const pluginId = await this.resolvePluginId(playerId(args));
        await this.plugin.kick(pluginId, str(args.reason) ?? undefined);
        return {};
      }
      case 'banPlayer': {
        const pluginId = await this.resolvePluginId(playerId(args));
        const reason = str(args.reason) ?? undefined;
        const expiresAt = expiryOf(args.expiresAt);
        await this.plugin.ban(pluginId, reason);
        const rest = this.pending.filter((b) => b.gameId !== pluginId);
        // A permanent ban replaces any pending expiry for that player.
        this.setPending(expiresAt ? [...rest, { gameId: pluginId, expiresAt, ...(reason ? { reason } : {}) }] : rest);
        return {};
      }
      case 'unbanPlayer': {
        const pluginId = await this.resolvePluginId(playerId(args));
        await this.plugin.unban(pluginId);
        this.setPending(this.pending.filter((b) => b.gameId !== pluginId));
        return {};
      }
      case 'listBans':
        return this.listBans();
      case 'shutdown':
        await this.plugin.shutdown();
        return {};
      default:
        throw new ActionError(`Unknown Takaro action '${action}'`);
    }
  }

  /** The game's own ban list, with `expiresAt` filled in from our pending timed bans. */
  private async listBans(): Promise<TakaroBan[]> {
    // F16: the store is re-read here so a `listBans` served immediately after a restart (or after another process
    // wrote the file) carries the real expiry. Never answer from a store we could not read.
    this.loadPending();
    if (this.banStoreCold) {
      throw new ActionError(
        'Vein connector cannot list bans: the timed-ban store could not be read, and reporting a timed ban as permanent would leave an unmanaged ban in Takaro',
      );
    }
    const bans = listOf(await this.plugin.getBans()).map(mapBan);
    if (!this.pending.length) return bans;
    const byId = new Map(this.pending.map((b) => [b.gameId, b] as const));
    for (const ban of bans) {
      const pending = byId.get(ban.player.gameId) ?? byId.get(ban.player.steamId ?? '');
      if (pending) {
        ban.expiresAt = pending.expiresAt;
        if (!ban.reason && pending.reason) ban.reason = pending.reason;
        byId.delete(pending.gameId);
      }
    }
    // Timed bans the plugin no longer lists (e.g. the game dropped it) are still reported until they expire.
    for (const pending of byId.values()) {
      bans.push({ player: mapPlayer({ gameId: pending.gameId }), reason: pending.reason ?? '', expiresAt: pending.expiresAt });
    }
    return bans;
  }

  private setPending(bans: PendingBan[]): void {
    this.pending = bans;
    try {
      this.options.banStore?.save(bans);
    } catch (err) {
      logger.warn(`Could not persist timed bans: ${(err as Error).message}`);
    }
  }

  private async testReachability(): Promise<{ connectable: boolean; reason: string | null }> {
    try {
      const health = await this.plugin.health();
      const status = String(health.status ?? '').toLowerCase();
      // "unimplemented" is a known, static gap (the action returns a clear error); only degraded capabilities are news.
      const caps = Object.entries(health.capabilities ?? {}).filter(([, state]) => state !== 'ok' && state !== 'unimplemented');
      const capText = caps.length ? `capabilities not ok: ${caps.map(([n, s]) => `${n}=${s}`).join(', ')}` : '';
      if (status === 'ok') return { connectable: true, reason: capText || null };
      if (status === 'degraded') return { connectable: true, reason: `Vein plugin degraded${capText ? `; ${capText}` : ''}` };
      return { connectable: false, reason: `Vein plugin status '${health.status}'${capText ? `; ${capText}` : ''}` };
    } catch (err) {
      return { connectable: false, reason: err instanceof Error ? err.message : String(err) };
    }
  }

  /** Online players from the plugin, falling back to Vein's own HTTP API when the plugin call fails. */
  private async onlinePlayers(): Promise<PluginPlayer[]> {
    try {
      const players = await this.plugin.getPlayers();
      if (Array.isArray(players)) return players;
    } catch (err) {
      const fallback = await this.options.httpPlayers?.().catch(() => null);
      if (fallback) {
        logger.warn(`Plugin /players failed (${(err as Error).message}); answered from Vein's built-in HTTP API`);
        return fallback;
      }
      throw err;
    }
    return (await this.options.httpPlayers?.().catch(() => null)) ?? [];
  }

  /** Records a player under every identifier Takaro might ask for later. Returns the player unchanged. */
  remember(player: TakaroPlayer, online = true): TakaroPlayer {
    if (this.index(player)) this.saveKnown();
    return online ? player : { ...player, online: false };
  }

  /** Indexes one record under every identifier. Returns true when anything changed. */
  private index(player: TakaroPlayer): boolean {
    if (!player?.gameId) return false;
    const copy = { ...player };
    delete copy.online;
    const before = this.known.get(player.gameId.toLowerCase());
    for (const key of [player.gameId, player.steamId, player.platformId, player.name]) {
      if (typeof key === 'string' && key) this.known.set(key.toLowerCase(), copy);
    }
    return JSON.stringify(before) !== JSON.stringify(copy);
  }

  /** The distinct last-known records (one per gameId), newest last. */
  knownPlayers(): TakaroPlayer[] {
    const byGameId = new Map<string, TakaroPlayer>();
    for (const p of this.known.values()) byGameId.set(p.gameId.toLowerCase(), p);
    return [...byGameId.values()];
  }

  private saveKnown(): void {
    if (!this.options.knownStore) return;
    const max = this.options.maxKnownPlayers ?? 500;
    const players = this.knownPlayers();
    try {
      this.options.knownStore.save(players.length > max ? players.slice(players.length - max) : players);
    } catch (err) {
      logger.warn(`Could not persist known players: ${(err as Error).message}`);
    }
  }

  /** The last-known record for an identifier, if this sidecar has ever seen that player. */
  lastKnown(id: string): TakaroPlayer | undefined {
    return this.known.get(id.toLowerCase()) ?? this.known.get(stripPlatform(id).toLowerCase());
  }

  /** Finds a plugin player by SteamID64, platformId, character name or Steam persona name. */
  async findPlayer(id: string): Promise<PluginPlayer | undefined> {
    const needle = stripPlatform(id).toLowerCase();
    let players: PluginPlayer[];
    try {
      players = await this.onlinePlayers();
    } catch {
      return undefined;
    }
    if (!Array.isArray(players)) return undefined;
    for (const p of players) {
      try {
        this.remember(mapPlayer(p));
      } catch {
        /* unidentifiable plugin row: nothing to cache */
      }
    }
    return (
      players.find((p) => p.gameId?.toLowerCase() === needle) ??
      players.find((p) => p.steamId?.toLowerCase() === needle) ??
      players.find((p) => p.platformId?.toLowerCase() === id.toLowerCase()) ??
      players.find((p) => p.characterName?.toLowerCase() === needle) ??
      players.find((p) => p.name?.toLowerCase() === needle)
    );
  }

  /** Plugin endpoints take the plugin's gameId (SteamID64); offline players (ban/unban) fall back to the id as given. */
  private async resolvePluginId(id: string): Promise<string> {
    const found = await this.findPlayer(id);
    return found?.gameId ?? stripPlatform(id);
  }
}

/** Accepts flat { gameId } / { steamId } / { platformId } or nested { player: {...} } / { playerRef: {...} }. */
export function playerId(args: Args): string {
  for (const source of [args, asRecord(args.player), asRecord(args.playerRef)]) {
    const id = str(source.gameId) ?? str(source.steamId) ?? str(source.platformId);
    if (id) return id;
  }
  return fail('Expected player identifier (gameId, or player.gameId)');
}

/** Takaro sends `expiresAt` as an ISO string, a number, or an explicit JSON null (= permanent). */
export function expiryOf(value: unknown): string | null {
  // 0 / negative epochs are Takaro's "no expiry", not 1970.
  if (typeof value === 'number') return Number.isFinite(value) && value > 0 ? new Date(value).toISOString() : null;
  const s = str(value);
  if (!s) return null;
  const parsed = Date.parse(s);
  return Number.isFinite(parsed) ? new Date(parsed).toISOString() : null;
}

function listOf(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  const rec = asRecord(value);
  for (const key of ['items', 'entities', 'locations', 'bans', 'players', 'data']) {
    if (Array.isArray(rec[key])) return rec[key] as unknown[];
  }
  return [];
}

function fail(message: string): never {
  throw new ActionError(message);
}
