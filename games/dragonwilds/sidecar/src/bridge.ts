import { DragonwildsAdapter, playerId, type AdapterOptions } from './dragonwilds/adapter.js';
import type { CursorStore } from './dragonwilds/cursorStore.js';
import { EventPoller } from './dragonwilds/eventPoller.js';
import { LogTailer } from './dragonwilds/logTail.js';
import { mapPlayer } from './dragonwilds/mapping.js';
import type { OnlineStore } from './dragonwilds/onlineStore.js';
import { DragonwildsPluginClient } from './dragonwilds/pluginClient.js';
import type { PluginHealth, Position, TakaroPlayer } from './dragonwilds/types.js';
import { logger } from './logger.js';
import { createErrorResponse, createResponse, parseTakaroRequest, type GameEventType, type WsMessage } from './takaro/protocol.js';

/**
 * Plugin /health capability names (plugin docs/API.md) that, when present and not "ok", mean the plugin cannot be
 * trusted for join/leave events, so the RSDragonwilds.log tail takes them over.
 */
export const CONNECTION_CAPABILITIES = ['players'];
const TAILED_TYPES: ReadonlySet<string> = new Set(['player-connected', 'player-disconnected']);

/**
 * Takaro (hosted) calls getPlayerLocation while processing a player-connected / player-disconnected gameEvent and only
 * stores the event record if that request succeeds. Inside this window after forwarding such an event we answer a
 * failed location lookup with the last position the plugin reported for that player, or the origin when none is known.
 */
export const EVENT_LOCATION_FALLBACK_MS = 60_000;

/**
 * How many undelivered gameEvents the sidecar keeps while Takaro is unreachable. At ~1 event/s of real traffic this
 * covers well over an hour of outage; beyond it the OLDEST events are dropped (and their cursor released) so the
 * sidecar never grows without bound.
 */
export const MAX_PENDING_EVENTS = 5000;
const ORIGIN: Position = { x: 0, y: 0, z: 0 };

/** A gameEvent that has not reached Takaro yet. `seq` is the plugin ring-buffer seq (absent for log-tail events). */
export interface PendingEvent {
  type: GameEventType;
  data: unknown;
  seq?: number;
}

export interface TakaroSink {
  send(message: WsMessage): boolean;
  sendGameEvent(type: GameEventType, data: unknown): boolean;
}

export interface BridgeOptions {
  plugin: DragonwildsPluginClient;
  takaro: TakaroSink;
  cursorStore: CursorStore;
  /** Persisted set of players Takaro was told are online; enables disconnect reconciliation after restarts. */
  onlineStore?: OnlineStore;
  logFile: string;
  logTailMode: 'auto' | 'always' | 'never';
  /** Forward `log` events: all lines, filtered (drop UE/EOS noise, default), or none. */
  logEvents?: 'all' | 'filtered' | 'none';
  pollIntervalMs?: number;
  healthCheckIntervalMs?: number;
  /** Exit the process after the plugin has been unreachable this long (0 = never). */
  exitAfterUnreachableMs?: number;
  adapter?: AdapterOptions;
}

/**
 * Unreal/EOS chatter that is worthless to a server admin. Takaro rate-limits `log` per server (sustained 50 per 30 s)
 * and RSDragonwilds.log prints thousands of Redpoint EOS verbose lines; forwarding them burns the budget.
 */
const NOISY_LOG = [
  /LogRedpointEOS:\s*Verbose/,
  /LogRedpointEOSCore:\s*Verbose/,
  /LogEOSHTTP/,
  /LogEOSAnalytics/,
  /SendBackendEvent/,
  /^\s*$/,
];

/**
 * The Dragonwilds server prints secrets in cleartext: `WorldPassword`/`AdminPassword` settings, and — verified on the
 * real server (research/log-grammar.md) — the base64 world password inside the URL options of
 * `LogNet: Login request: ?p=<base64>?pf=PC...` and `LogNet: Join request: /Game/Maps/...?p=<base64>...`.
 * Never forward those to Takaro. Redaction is unconditional: it applies in every logEvents mode.
 */
const PASSWORD_KEYS = /(WorldPassword|AdminPassword|Password)/i;
const PASSWORD_ASSIGNMENT = '((?:\\w*)Password)(\\s*[=:]\\s*)("?)([^\\s",;]*)\\3';
/** `?p=<base64 world password>` in a Login/Join request URL, up to the next `?` or whitespace. */
const URL_PASSWORD = /(\?p=)[^?\s]*/gi;

export function redactLog(msg: string): string {
  let out = msg.replace(URL_PASSWORD, '$1[redacted]');
  if (!PASSWORD_KEYS.test(out)) return out;
  let replaced = false;
  out = out.replace(new RegExp(PASSWORD_ASSIGNMENT, 'gi'), (_m, key: string, sep: string) => {
    replaced = true;
    return `${key}${sep}[redacted]`;
  });
  // A password mentioned without a `key=value` shape (e.g. free text) is dropped wholesale rather than leaked.
  return replaced ? out : '[redacted: line mentions a password]';
}

export function shouldForwardLog(mode: BridgeOptions['logEvents'], data: unknown): boolean {
  if (mode === 'none') return false;
  if (mode === 'all') return true;
  const msg = typeof (data as { msg?: unknown })?.msg === 'string' ? (data as { msg: string }).msg : '';
  return !NOISY_LOG.some((re) => re.test(msg));
}

export function shouldTailLog(mode: BridgeOptions['logTailMode'], health: PluginHealth | null): boolean {
  if (mode === 'always') return true;
  if (mode === 'never') return false;
  if (!health) return true;
  const status = String(health.status ?? '').toLowerCase();
  if (status !== 'ok' && status !== 'degraded') return true;
  const caps = health.capabilities ?? {};
  return CONNECTION_CAPABILITIES.some((name) => name in caps && caps[name] !== 'ok');
}

export class Bridge {
  readonly adapter: DragonwildsAdapter;
  readonly poller: EventPoller;
  readonly tailer: LogTailer;
  private healthTimer: NodeJS.Timeout | null = null;
  private lastHealth: PluginHealth | null = null;
  private tailActive = false;
  private unreachableSince: number | null = null;
  /** gameId/steamId -> expiry of the location fallback window opened by a forwarded connect/disconnect event. */
  private readonly eventWindows = new Map<string, number>();
  private readonly lastPositions = new Map<string, Position>();
  private readonly online = new Map<string, TakaroPlayer>();
  /** Reconcile online players at the next healthy plugin check (sidecar start, plugin restart). */
  private reconcilePending = true;
  /** gameEvents produced while Takaro was unreachable, oldest first; flushed in order after the next identify (F10). */
  private readonly pendingEvents: PendingEvent[] = [];
  private droppedEvents = 0;

  constructor(private readonly options: BridgeOptions) {
    this.adapter = new DragonwildsAdapter(options.plugin, options.adapter ?? {});
    for (const p of options.onlineStore?.load() ?? []) this.online.set(p.gameId, p);
    this.poller = new EventPoller({
      getEvents: (since) => options.plugin.getEvents(since),
      emit: (type, data, seq) => {
        const payload = type === 'log' ? redactEventData(data) : data;
        if (type === 'log' && !shouldForwardLog(options.logEvents ?? 'filtered', payload)) return true;
        const sent = options.takaro.sendGameEvent(type, payload);
        if (sent) this.noteConnectionEvent(type, payload);
        else this.queueEvent({ type, data: payload, seq });
        if (type !== 'log') logger.info(`Forwarded plugin ${type} (sent=${sent}): ${JSON.stringify(payload)}`);
        return sent ? true : 'queued';
      },
      store: options.cursorStore,
      onRestart: () => {
        logger.info('Plugin reports a new server process (bootId/seq reset); reconciling online players');
        void this.reconcileOnline();
      },
      suppress: () => (this.tailActive ? TAILED_TYPES : new Set()),
      onError: (err) => logger.warn(`Event poll failed: ${err.message}`),
      intervalMs: options.pollIntervalMs,
    });
    this.tailer = new LogTailer({
      file: options.logFile,
      intervalMs: options.pollIntervalMs,
      resolve: async ({ gameId, name }) => {
        const found = await this.adapter.findPlayer(gameId ?? name ?? '');
        return found ? mapPlayer(found) : null;
      },
      onEvent: (event) => {
        logger.info(`Log tail ${event.type}: ${event.data.player.name} (${event.data.player.gameId})`);
        if (options.takaro.sendGameEvent(event.type, event.data)) this.noteConnectionEvent(event.type, event.data);
        else this.queueEvent({ type: event.type, data: event.data });
      },
      onError: (err) => logger.debug(`Log tail: ${err.message}`),
    });
  }

  async handleRequest(message: WsMessage): Promise<WsMessage | null> {
    if (!message.requestId) {
      logger.warn(`Ignoring Takaro request without requestId: ${JSON.stringify(message)}`);
      return null;
    }
    let reply: WsMessage;
    try {
      const request = parseTakaroRequest(message);
      logger.debug(`Takaro request ${request.action} ${JSON.stringify(request.args)}`);
      let payload: unknown;
      try {
        payload = await this.adapter.handleAction(request.action, request.args);
        if (request.action === 'getPlayerLocation') this.rememberPosition(request.args, payload);
      } catch (err) {
        const fallback = request.action === 'getPlayerLocation' ? this.eventLocationFallback(request.args) : null;
        if (!fallback) throw err;
        logger.warn(
          `getPlayerLocation failed during a connect/disconnect event window (${(err as Error).message}); ` +
            `answering ${JSON.stringify(fallback)} so Takaro stores the event`,
        );
        payload = fallback;
      }
      reply = createResponse(request.requestId, payload);
    } catch (err) {
      const text = err instanceof Error ? err.message : String(err);
      logger.warn(`Takaro request ${message.requestId} failed: ${text}`);
      reply = createErrorResponse(message.requestId, text);
    }
    this.options.takaro.send(reply);
    return reply;
  }

  /** Opens the location fallback window for the player of a forwarded player-connected/-disconnected event. */
  noteConnectionEvent(type: string, data: unknown, now = Date.now()): void {
    if (type !== 'player-connected' && type !== 'player-disconnected') return;
    const player = (data as { player?: Record<string, unknown> } | null)?.player ?? {};
    if (typeof player.gameId === 'string' && player.gameId) {
      // Keep the record so a getPlayer for this player after they leave still answers a valid IGamePlayer (F7).
      this.adapter.remember(player as unknown as TakaroPlayer);
      if (type === 'player-connected') this.online.set(player.gameId, player as unknown as TakaroPlayer);
      else this.online.delete(player.gameId);
      this.saveOnline();
    }
    for (const key of [player.gameId, player.epicOnlineServicesId, player.steamId]) {
      if (typeof key === 'string' && key) this.eventWindows.set(key, now + EVENT_LOCATION_FALLBACK_MS);
    }
  }

  eventLocationFallback(args: Record<string, unknown>, now = Date.now()): Position | null {
    let id: string;
    try {
      id = playerId(args);
    } catch {
      return null;
    }
    const expires = this.eventWindows.get(id);
    if (expires === undefined) return null;
    if (expires < now) {
      this.eventWindows.delete(id);
      return null;
    }
    return this.lastPositions.get(id) ?? ORIGIN;
  }

  private rememberPosition(args: Record<string, unknown>, payload: unknown): void {
    const pos = payload as Position | null;
    if (!pos || typeof pos.x !== 'number' || typeof pos.y !== 'number' || typeof pos.z !== 'number') return;
    try {
      this.lastPositions.set(playerId(args), { x: pos.x, y: pos.y, z: pos.z });
    } catch {
      /* no identifier: nothing to remember */
    }
  }

  /** Players Takaro was told are online and not yet told have left. */
  onlinePlayers(): TakaroPlayer[] {
    return [...this.online.values()];
  }

  /**
   * Sends player-disconnected for every player Takaro was told is online but the plugin no longer lists (e.g. the
   * game server restarted while they were connected, so no leave line was ever logged). Returns the players sent.
   */
  async reconcileOnline(): Promise<TakaroPlayer[]> {
    if (this.online.size === 0) return [];
    let current: Set<string>;
    try {
      current = new Set((await this.options.plugin.getPlayers()).map((p) => mapPlayer(p).gameId));
    } catch (err) {
      logger.warn(`Online reconciliation postponed, plugin getPlayers failed: ${(err as Error).message}`);
      this.reconcilePending = true;
      return [];
    }
    const gone: TakaroPlayer[] = [];
    for (const player of [...this.online.values()]) {
      if (current.has(player.gameId)) continue;
      const sent = this.options.takaro.sendGameEvent('player-disconnected', { player });
      logger.info(`Reconcile: ${player.name} (${player.gameId}) is no longer on the server; sent player-disconnected (sent=${sent})`);
      if (!sent) {
        this.reconcilePending = true;
        continue;
      }
      this.noteConnectionEvent('player-disconnected', { player });
      gone.push(player);
    }
    return gone;
  }

  private saveOnline(): void {
    try {
      this.options.onlineStore?.save([...this.online.values()]);
    } catch (err) {
      logger.warn(`Could not persist online players: ${(err as Error).message}`);
    }
  }

  /** Events waiting for Takaro to come back (oldest first). */
  pending(): PendingEvent[] {
    return this.pendingEvents.map((e) => ({ ...e }));
  }

  /** Number of pending events dropped because the queue was full (never silently zero in the logs). */
  dropped(): number {
    return this.droppedEvents;
  }

  private queueEvent(event: PendingEvent): void {
    this.pendingEvents.push(event);
    while (this.pendingEvents.length > MAX_PENDING_EVENTS) {
      const lost = this.pendingEvents.shift();
      this.droppedEvents += 1;
      logger.error(`Pending event queue full (${MAX_PENDING_EVENTS}); dropping ${lost?.type} seq=${lost?.seq ?? '-'}`);
      // The event is gone for good; release its cursor so a restart does not replay everything behind it forever.
      if (lost?.seq !== undefined) this.poller.markDelivered(lost.seq);
    }
    if (this.pendingEvents.length === 1) logger.warn('Takaro is not reachable; queueing game events until it is');
  }

  /**
   * Sends every queued event, in order, to the (now open and identified) Takaro socket. Stops at the first failure and
   * keeps the rest. Returns how many were delivered.
   */
  flushPending(): number {
    if (!this.pendingEvents.length) return 0;
    logger.info(`Flushing ${this.pendingEvents.length} game event(s) buffered while Takaro was unreachable`);
    let flushed = 0;
    while (this.pendingEvents.length) {
      const event = this.pendingEvents[0];
      if (!this.options.takaro.sendGameEvent(event.type, event.data)) {
        logger.warn(`Takaro went away again after ${flushed} buffered event(s); keeping ${this.pendingEvents.length} queued`);
        break;
      }
      this.pendingEvents.shift();
      flushed += 1;
      this.noteConnectionEvent(event.type, event.data);
      if (event.seq !== undefined) this.poller.markDelivered(event.seq);
    }
    if (flushed) logger.info(`Flushed ${flushed} buffered game event(s); cursor now ${this.poller.cursor()}`);
    return flushed;
  }

  isLogTailActive(): boolean {
    return this.tailActive;
  }

  pluginHealth(): PluginHealth | null {
    return this.lastHealth;
  }

  async refreshHealth(): Promise<void> {
    try {
      this.lastHealth = await this.options.plugin.health();
      this.unreachableSince = null;
      if (this.reconcilePending) {
        this.reconcilePending = false;
        await this.reconcileOnline();
      }
    } catch (err) {
      this.unreachableSince ??= Date.now();
      const limit = this.options.exitAfterUnreachableMs ?? 0;
      if (limit > 0 && Date.now() - this.unreachableSince >= limit) {
        // network_mode "service:dragonwilds": when the game container restarts, this container keeps the old (dead)
        // network namespace. Exiting lets the restart policy re-attach us to the new one.
        logger.error(`Plugin unreachable for ${Math.round(limit / 1000)}s; exiting so the container restarts into the game's network namespace`);
        process.exit(1);
      }
      if (this.lastHealth !== null) logger.warn(`Plugin health failed: ${(err as Error).message}`);
      this.lastHealth = null;
    }
    const want = shouldTailLog(this.options.logTailMode, this.lastHealth);
    if (want && !this.tailActive) {
      logger.info(`Enabling log-tail fallback for connect/disconnect (${this.options.logFile})`);
      this.tailer.start();
    } else if (!want && this.tailActive) {
      logger.info('Plugin connection events healthy; disabling log-tail fallback');
      this.tailer.stop();
    }
    this.tailActive = want;
  }

  async startEvents(): Promise<void> {
    // Anything buffered during the outage goes out first, in order, before new polling resumes.
    this.flushPending();
    await this.refreshHealth();
    this.poller.start();
    this.adapter.start();
    if (!this.healthTimer) {
      this.healthTimer = setInterval(() => void this.refreshHealth(), this.options.healthCheckIntervalMs ?? 15000);
    }
  }

  stopEvents(): void {
    this.poller.stop();
    this.tailer.stop();
    this.adapter.stop();
    this.tailActive = false;
    if (this.healthTimer) clearInterval(this.healthTimer);
    this.healthTimer = null;
  }
}

function redactEventData(data: unknown): unknown {
  const msg = (data as { msg?: unknown })?.msg;
  if (typeof msg !== 'string') return data;
  const clean = redactLog(msg);
  return clean === msg ? data : { ...(data as Record<string, unknown>), msg: clean };
}
