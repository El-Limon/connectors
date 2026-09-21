import fs from 'node:fs';
import type { GameEventType } from '../takaro/protocol.js';
import { mapPlayer } from './mapping.js';
import type { TakaroPlayer } from './types.js';

export type ConnectionEventType = Extract<GameEventType, 'player-connected' | 'player-disconnected'>;

/** A parsed log line. `gameId` is a SteamID64 when the line carried one. */
export interface LogLineEvent {
  type: ConnectionEventType;
  gameId?: string;
  /** Player name as the game logs it. */
  name?: string;
}

export interface LogEvent {
  type: ConnectionEventType;
  data: { player: TakaroPlayer };
}

export interface LogChat {
  name?: string;
  gameId?: string;
  msg: string;
  channel: string;
}

/**
 * The log grammar is a small pluggable table so the observed Vein lines can replace the defaults without touching
 * the tailer. Each regex may use the named groups `gameId` (SteamID64), `name`, `msg` and `channel`; unnamed
 * capture groups are used as a fallback in the order (gameId, name) / (name, msg).
 *
 * VERIFICATION STATUS (2026-09-17, research/2026-09-17-log-grammar.md)
 *  - `chatLine`    OBSERVED (client log, lane L0b): `LogVeinChat: [<SteamID64>] <SteamPersona> (aka <Character>): <msg>`.
 *                  The SteamID64 is on the line, so chat needs no lookup. The channel (Local/Global/Radio) is NOT
 *                  logged -> every log-sourced chat is reported as `global`. The plugin hook stays the real source.
 *  - `loginLine`   OBSERVED (client log): `LogVein: PlayerState ID changed to <SteamID64>` (id only, no name; an
 *                  empty variant is printed first and is ignored). The id is held as `pendingId` for the next join.
 *  - `joinLine`    OBSERVED (client log): `LogVein: [] Player <SteamPersona> selected character <32hex> (aka <Char>)`
 *                  = the moment the player enters the world. Carries no SteamID64 -> attributed via loginLine.
 *  - `readyLine`   VERIFIED: `Created session GameSession.` (pterodactyl egg startup marker for VeinServer).
 *  - `leaveLine`   UNVERIFIED: UE-standard `LogNet: UNetConnection::Close` / `UChannel::CleanUp` with
 *                  `UniqueId: Steam:<id64>`. NOTE: a half-open connection logs `UniqueId: NULL:gamer-<32hex>`, so
 *                  only a `Steam:<id64>` UniqueId is ever a join/leave key. The dedicated-server leave line is
 *                  still TODO for lane L0b.
 * The OBSERVED lines come from the vanilla client's own log during a local session: same game code and therefore
 * the same categories/shapes, but NOT yet confirmed on the dedicated server. Everything is overridable per
 * deployment via VEIN_LOG_*_RE (config.ts) without touching this file.
 */
export interface LogGrammar {
  /** Ties a player name to a SteamID64 before the join line (optional). */
  loginLine?: RegExp;
  joinLine: RegExp;
  leaveLine: RegExp[];
  chatLine?: RegExp;
  /** Server finished starting; used by /health and the L6 rig scripts. */
  readyLine: RegExp;
}

/** SteamID64 embedded in a UE `UniqueId: Steam:<id>` / `userId: Steam:<id>` field. */
const STEAM_ID = '(?<gameId>7656\\d{13})';

export const DEFAULT_GRAMMAR: LogGrammar = {
  // Either shape works: the UE `Login request:` line (name + id) or Vein's own `PlayerState ID changed to` (id only).
  loginLine: new RegExp(`(?:LogNet:\\s*Login request:.*?\\?Name=(?<name>[^?\\s]+).*?userId:\\s*(?:Steam:)?${STEAM_ID}|LogVein:\\s*PlayerState ID changed to\\s*(?<gameId2>7656\\d{13}))`, 'i'),
  joinLine: /(?:LogVein:\s*(?:\[[^\]]*\]\s*)?Player (?<name>.+?) selected character (?<characterId>[0-9A-Fa-f]{32})|LogNet:\s*Join succeeded:\s*(?<name2>.+?)\s*$)/,
  leaveLine: [
    new RegExp(`LogNet:.*?UNetConnection::Close.*?UniqueId:\\s*(?:Steam:)?${STEAM_ID}`, 'i'),
    new RegExp(`LogNet:.*?UChannel::CleanUp.*?UniqueId:\\s*(?:Steam:)?${STEAM_ID}`, 'i'),
    // Close/CleanUp variants that carry a player name instead of a UniqueId. `Name:` alone is NOT accepted:
    // on a UE `UNetConnection::Close` line that field is the connection object name (SteamNetConnection_0).
    /LogNet:.*?(?:UNetConnection::Close|Connection closed).*?PlayerName:\s*(?<name>[^\s,]+)/i,
  ],
  chatLine: new RegExp(`LogVeinChat:\\s*\\[${STEAM_ID}\\]\\s*(?<name>.+?)(?:\\s*\\(aka (?<character>[^)]*)\\))?:\\s?(?<msg>.*)$`),
  readyLine: /Created session GameSession\./,
};

/** Builds a grammar from the defaults plus optional per-deployment overrides (config.logGrammarOverrides). */
export function buildGrammar(overrides: Partial<Record<'loginLine' | 'joinLine' | 'leaveLine' | 'chatLine' | 'readyLine', string>> = {}): LogGrammar {
  const re = (value: string | undefined): RegExp | undefined => (value ? new RegExp(value, 'i') : undefined);
  return {
    loginLine: re(overrides.loginLine) ?? DEFAULT_GRAMMAR.loginLine,
    joinLine: re(overrides.joinLine) ?? DEFAULT_GRAMMAR.joinLine,
    leaveLine: overrides.leaveLine ? [new RegExp(overrides.leaveLine, 'i')] : DEFAULT_GRAMMAR.leaveLine,
    chatLine: re(overrides.chatLine) ?? DEFAULT_GRAMMAR.chatLine,
    readyLine: re(overrides.readyLine) ?? DEFAULT_GRAMMAR.readyLine,
  };
}

function groups(m: RegExpExecArray): { gameId?: string; name?: string; msg?: string; channel?: string } {
  const g = (m.groups ?? {}) as Record<string, string | undefined>;
  // A grammar may declare the same field twice (alternation); `gameId2`/`name2` are the second branch.
  const gameId = clean(g.gameId) ?? clean(g.gameId2) ?? (m[1] && /^7656\d{13}$/.test(m[1]) ? m[1] : undefined);
  const named = clean(g.name) ?? clean(g.name2);
  const name = named ?? (gameId ? clean(m[2]) : clean(m[1]));
  return { gameId, name, msg: clean(g.msg) ?? (named ? undefined : clean(m[2])), channel: clean(g.channel) };
}

function clean(value: string | undefined): string | undefined {
  const t = value?.trim();
  return t ? t : undefined;
}

export class VeinLogParser {
  /** SteamID64 -> name for players currently considered in-world (a leave without a join is not an event). */
  private readonly present = new Map<string, string | undefined>();
  /** name -> SteamID64, learned from the login line, so a name-only join can be attributed. */
  private readonly loginIds = new Map<string, string>();
  /** SteamID64 seen on an id-only login line, waiting for the join line that names the player. */
  private pendingId: string | undefined;
  private ready = false;
  private lastChat: LogChat | null = null;

  constructor(private readonly grammar: LogGrammar = DEFAULT_GRAMMAR) {}

  /** True once the ready line (`Created session GameSession.`) has been seen. */
  serverReady(): boolean {
    return this.ready;
  }

  /** The last chat line parsed, when the grammar has a (verified) chatLine; the plugin hook is the real source. */
  takeChat(): LogChat | null {
    const chat = this.lastChat;
    this.lastChat = null;
    return chat;
  }

  gameIdForName(name: string): string | undefined {
    return this.loginIds.get(name);
  }

  /** SteamID64 from an id-only login line that has not been claimed by a join line yet. */
  pendingGameId(): string | undefined {
    return this.pendingId;
  }

  feed(rawLine: string): LogLineEvent[] {
    const line = rawLine.replace(/\r$/, '');
    let m: RegExpExecArray | null;

    if (this.grammar.readyLine.test(line)) {
      this.ready = true;
      // A fresh session means nobody is in world any more; stale presence would swallow the next join.
      this.present.clear();
      this.pendingId = undefined;
      return [];
    }

    if (this.grammar.loginLine && (m = this.grammar.loginLine.exec(line))) {
      const { gameId, name } = groups(m);
      if (gameId && name) this.loginIds.set(name, gameId);
      else if (gameId) this.pendingId = gameId; // id-only line (Vein's `PlayerState ID changed to <id>`)
      return [];
    }

    if (this.grammar.chatLine && (m = this.grammar.chatLine.exec(line))) {
      const { gameId, name, msg, channel } = groups(m);
      if (msg) this.lastChat = { ...(gameId ? { gameId } : {}), ...(name ? { name } : {}), msg, channel: channel ?? 'global' };
      return [];
    }

    if ((m = this.grammar.joinLine.exec(line))) {
      const g = groups(m);
      const gameId = g.gameId ?? (g.name ? this.loginIds.get(g.name) : undefined) ?? this.pendingId;
      const name = g.name;
      if (gameId && name) this.loginIds.set(name, gameId);
      this.pendingId = undefined;
      const key = gameId ?? name;
      if (!key) return [];
      if (this.present.has(key)) return []; // duplicate/late line: already connected
      this.present.set(key, name);
      return [{ type: 'player-connected', ...(gameId ? { gameId } : {}), ...(name ? { name } : {}) }];
    }

    for (const re of this.grammar.leaveLine) {
      if (!(m = re.exec(line))) continue;
      const g = groups(m);
      const gameId = g.gameId ?? (g.name ? this.loginIds.get(g.name) : undefined);
      const key = gameId ?? g.name;
      if (!key) return [];
      const name = g.name ?? this.present.get(key);
      // UE prints several close/cleanup lines per disconnect; only the first one is an event.
      if (!this.present.has(key)) return [];
      this.present.delete(key);
      return [{ type: 'player-disconnected', ...(gameId ? { gameId } : {}), ...(name ? { name } : {}) }];
    }
    return [];
  }
}

/** Looks a player up in the plugin's /players, by SteamID64 or by name; null when unknown. */
export type PlayerResolver = (ref: { gameId?: string; name?: string }) => Promise<TakaroPlayer | null>;

export interface LogTailerOptions {
  file: string;
  /** Enriches a log line with the plugin's view of the player (name, ping, ip). */
  resolve: PlayerResolver;
  onEvent: (event: LogEvent) => void;
  onError?: (err: Error) => void;
  onReady?: () => void;
  intervalMs?: number;
  grammar?: LogGrammar;
  /** Start at end of file (default) so a restart does not replay old joins. */
  fromStart?: boolean;
}

/** Polling tailer that survives the server rotating the log on restart (file shrinks or inode changes). */
export class LogTailer {
  private timer: NodeJS.Timeout | null = null;
  private offset = -1;
  private inode = -1;
  private partial = '';
  private parser: VeinLogParser;
  /** Last identity seen per name, so a name-only leave line still carries a gameId. */
  private readonly known = new Map<string, TakaroPlayer>();
  private chain: Promise<void> = Promise.resolve();
  private readyFired = false;

  constructor(private readonly options: LogTailerOptions) {
    this.parser = new VeinLogParser(options.grammar ?? DEFAULT_GRAMMAR);
  }

  running(): boolean {
    return this.timer !== null;
  }

  serverReady(): boolean {
    return this.parser.serverReady();
  }

  /** Resolves once every line read so far has been resolved and emitted (tests). */
  flush(): Promise<void> {
    return this.chain;
  }

  start(): void {
    if (this.timer) return;
    this.offset = -1;
    this.timer = setInterval(() => this.poll(), this.options.intervalMs ?? 1000);
    this.poll();
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  poll(): void {
    let stat: fs.Stats;
    try {
      stat = fs.statSync(this.options.file);
    } catch (err) {
      this.options.onError?.(err as Error);
      return;
    }
    if (this.offset < 0) {
      this.offset = this.options.fromStart ? 0 : stat.size;
      this.inode = stat.ino;
    } else if (stat.ino !== this.inode || stat.size < this.offset) {
      this.offset = 0;
      this.inode = stat.ino;
      this.partial = '';
      this.parser = new VeinLogParser(this.options.grammar ?? DEFAULT_GRAMMAR);
      this.readyFired = false;
    }
    if (stat.size <= this.offset) return;

    const fd = fs.openSync(this.options.file, 'r');
    let lines: string[];
    try {
      const length = stat.size - this.offset;
      const buf = Buffer.alloc(length);
      fs.readSync(fd, buf, 0, length, this.offset);
      this.offset = stat.size;
      // A partial trailing line is carried over: the container wrapper can also cut a UE line in half on stdout.
      const text = this.partial + buf.toString('utf8');
      lines = text.split('\n');
      this.partial = lines.pop() ?? '';
    } finally {
      fs.closeSync(fd);
    }

    for (const line of lines) {
      for (const event of this.parser.feed(line)) this.enqueue(event);
      if (this.parser.serverReady() && !this.readyFired) {
        this.readyFired = true;
        this.options.onReady?.();
      }
    }
  }

  /** Plugin lookups are async; keep log order by chaining. */
  private enqueue(event: LogLineEvent): void {
    this.chain = this.chain.then(async () => {
      const cached = event.name ? this.known.get(event.name) : undefined;
      let player: TakaroPlayer | null = null;
      try {
        player = await this.options.resolve({ gameId: event.gameId, name: event.name });
      } catch (err) {
        this.options.onError?.(err as Error);
      }
      // The log line itself is authoritative for identity; the plugin only enriches it.
      if (event.gameId) {
        const base = player && player.gameId === event.gameId ? player : mapPlayer({ gameId: event.gameId });
        player = { ...base, ...(event.name ? { name: event.name } : {}) };
      } else {
        // A line with a name but no id still yields a usable record (gameId = the name); Takaro's IGamePlayer
        // validation rejects anything less, and the plugin corrects the identity on the next /players poll.
        player = player ?? cached ?? (event.name ? mapPlayer({ name: event.name }) : null);
      }
      if (!player) {
        this.options.onError?.(new Error(`Log tail: no gameId for player '${event.name ?? '?'}' (dropping ${event.type})`));
        return;
      }
      if (event.name) this.known.set(event.name, player);
      if (event.type === 'player-disconnected' && event.name) this.known.delete(event.name);
      this.options.onEvent({ type: event.type, data: { player } });
    });
  }
}
