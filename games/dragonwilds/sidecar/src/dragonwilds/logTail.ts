import fs from 'node:fs';
import type { GameEventType } from '../takaro/protocol.js';
import { mapPlayer } from './mapping.js';
import type { TakaroPlayer } from './types.js';

export type ConnectionEventType = Extract<GameEventType, 'player-connected' | 'player-disconnected'>;

/** A join/leave line. `gameId` (EOS ProductUserId) is present on every verified line except the bare join hint. */
export interface LogLineEvent {
  type: ConnectionEventType;
  gameId?: string;
  /** Character name, as the game logs it. */
  name?: string;
  /** Per-character guid (`Guid[DCG:...]`), stable per character across servers; 8 per account. */
  characterGuid?: string;
}

export interface LogEvent {
  type: ConnectionEventType;
  data: { player: TakaroPlayer };
}

/**
 * RSDragonwilds.log grammar, captured from the real server + client (research/log-grammar.md).
 *
 *  - `LogNet: Login request: ?p=<pw>?pf=PC?cpx=1?Name=<PlatformName> userId: RedpointEOS:<puid> platform: RedpointEOS`
 *    is the first line that ties a connection to a game id (platform display name + PUID).
 *  - `LogNet: Join succeeded: <Name>` carries the PLATFORM display name, not the character name: it is a hint only,
 *    never an identity, and never an event (character select happens ~5 s later).
 *  - `LogDominionPlayerControllerBase: PlayerChar entered world [Account[XP:<puid>] Character Name[<name>]
 *    Guid[DCG:<guid>] Type[0]]` is the authoritative join line and the one we emit player-connected from.
 *  - `LogDominionPlayerController: ClientRequestDisconnect : DisconnectMe : PlayerStateSave result[true] -
 *    state saved for Account[XP:<puid>] Character Name[<name>] ...` is the clean-exit leave line (puid + name).
 *  - `LogNet: UChannel::CleanUp: ... UniqueId: RedpointEOS:<puid>` is the fallback for a drop with no leave line.
 *
 * Chat is NOT logged server-side at all (verified negative with a nonce): chat-message can only come from the
 * in-process plugin hook, never from this tail.
 */
const RE_LOGIN_REQUEST = /LogNet:\s*Login request:.*userId:\s*RedpointEOS:([0-9a-f]{32})/i;
const RE_URL_NAME = /\?Name=([^?\s]+)/i;
const RE_JOIN_SUCCEEDED = /LogNet:\s*(?:\w+:\s*)?Join succeeded:\s*(.+?)\s*$/;
const RE_ENTERED_WORLD = /LogDominionPlayerControllerBase:.*PlayerChar entered world\s*\[Account\[XP:([0-9a-f]{32})\][^\]]*\s*Character Name\[(.*?)\](?:\s*Guid\[DCG:([0-9A-Fa-f]+)\])?/i;
const RE_DISCONNECT = /LogDominionPlayerController:.*ClientRequestDisconnect.*?Account\[XP:([0-9a-f]{32})\][^\]]*\s*Character Name\[(.*?)\]/i;
/** Leave line shape without the Account[] prefix (older/partial builds): name only. */
const RE_DISCONNECT_NAME_ONLY = /LogDominionPlayerController:.*ClientRequestDisconnect(?!.*Account\[XP:).*Character Name\[(.*?)\]/i;
const RE_CHANNEL_CLEANUP = /UChannel::CleanUp:.*UniqueId:\s*RedpointEOS:([0-9a-f]{32})/i;

export class DragonwildsLogParser {
  /** PUIDs currently considered in-world, so a hard drop can be told apart from a duplicate/late line. */
  private readonly present = new Map<string, string | undefined>();
  /** Platform display name -> PUID, learned from `Login request:`; lets a bare join hint be attributed. */
  private readonly loginIds = new Map<string, string>();

  feed(rawLine: string): LogLineEvent[] {
    const line = rawLine.replace(/\r$/, '');
    let m: RegExpExecArray | null;

    if ((m = RE_LOGIN_REQUEST.exec(line))) {
      const platformName = RE_URL_NAME.exec(line)?.[1];
      if (platformName) this.loginIds.set(platformName, m[1].toLowerCase());
      return [];
    }
    if ((m = RE_ENTERED_WORLD.exec(line))) {
      const gameId = m[1].toLowerCase();
      const name = m[2].trim() || undefined;
      if (this.present.has(gameId)) return []; // character swap / duplicate line: already connected
      this.present.set(gameId, name);
      return [{ type: 'player-connected', gameId, ...(name ? { name } : {}), ...(m[3] ? { characterGuid: m[3].toUpperCase() } : {}) }];
    }
    if ((m = RE_DISCONNECT.exec(line))) {
      const gameId = m[1].toLowerCase();
      const name = m[2].trim() || this.present.get(gameId);
      this.present.delete(gameId);
      return [{ type: 'player-disconnected', gameId, ...(name ? { name } : {}) }];
    }
    if ((m = RE_DISCONNECT_NAME_ONLY.exec(line))) {
      const name = m[1].trim();
      if (!name) return [];
      const gameId = [...this.present.entries()].find(([, n]) => n === name)?.[0];
      if (gameId) this.present.delete(gameId);
      return [{ type: 'player-disconnected', ...(gameId ? { gameId } : {}), name }];
    }
    if ((m = RE_CHANNEL_CLEANUP.exec(line))) {
      // Hard drop (crash/timeout): only an event when no ClientRequestDisconnect was seen for this player.
      const gameId = m[1].toLowerCase();
      if (!this.present.has(gameId)) return [];
      const name = this.present.get(gameId);
      this.present.delete(gameId);
      return [{ type: 'player-disconnected', gameId, ...(name ? { name } : {}) }];
    }
    if ((m = RE_JOIN_SUCCEEDED.exec(line))) {
      // Platform display name only, and the character is not in the world yet: never an event.
      return [];
    }
    return [];
  }

  /** PUID last seen for a platform display name (`Login request:`), for callers that only have the name. */
  gameIdForPlatformName(name: string): string | undefined {
    return this.loginIds.get(name);
  }
}

/** Looks a player up in the plugin's /players, by EOS ProductUserId or by name; null when unknown. */
export type PlayerResolver = (ref: { gameId?: string; name?: string }) => Promise<TakaroPlayer | null>;

export interface LogTailerOptions {
  file: string;
  /** Enriches a log line with the plugin's view of the player (steamId, ping, platform name). */
  resolve: PlayerResolver;
  onEvent: (event: LogEvent) => void;
  onError?: (err: Error) => void;
  intervalMs?: number;
  /** Start at end of file (default) so a restart does not replay old joins. */
  fromStart?: boolean;
}

/** Polling tailer that survives the server rotating the log on restart (file shrinks or inode changes). */
export class LogTailer {
  private timer: NodeJS.Timeout | null = null;
  private offset = -1;
  private inode = -1;
  private partial = '';
  private parser = new DragonwildsLogParser();
  /** Last identity seen per character name, so a name-only leave line still carries a gameId. */
  private readonly known = new Map<string, TakaroPlayer>();
  private chain: Promise<void> = Promise.resolve();

  constructor(private readonly options: LogTailerOptions) {}

  running(): boolean {
    return this.timer !== null;
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
      this.parser = new DragonwildsLogParser();
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
        player = player ?? cached ?? null;
      }
      if (!player) {
        this.options.onError?.(new Error(`Log tail: no gameId for character '${event.name ?? '?'}' (dropping ${event.type})`));
        return;
      }
      if (event.name) this.known.set(event.name, player);
      if (event.type === 'player-disconnected' && event.name) this.known.delete(event.name);
      this.options.onEvent({ type: event.type, data: { player } });
    });
  }
}
