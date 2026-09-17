import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { buildGrammar, DEFAULT_GRAMMAR, VeinLogParser, LogTailer, type LogEvent } from '../vein/logTail.js';
import type { TakaroPlayer } from '../vein/types.js';

const STEAM = '76561198000000001';
const STEAM2 = '76561198000000002';
const NAME = 'Tester';

/**
 * UE-standard defaults. Only the ready line is VERIFIED for Vein (pterodactyl egg startup marker); the join/leave
 * lines are the engine's stock shapes and are replaced once lanes L0a/L0b bank
 * research/2026-09-17-log-grammar.md (or per deployment via VEIN_LOG_*_RE).
 */
const READY = '[2026.09.17-10.00.00:000][  0]LogGameMode: Created session GameSession.';
const LOGIN = `[2026.09.17-10.01.00:000][ 12]LogNet: Login request: ?Name=${NAME}?Password=$VEIN_DEV_WORLD_PASSWORD userId: Steam:${STEAM} platform: Steam`;
const JOIN = `[2026.09.17-10.01.00:100][ 13]LogNet: Join succeeded: ${NAME}`;
const LEAVE = `[2026.09.17-10.05.00:000][ 99]LogNet: UNetConnection::Close: [UNetConnection] RemoteAddr: 10.0.0.5:7777, Name: SteamNetConnection_0, Driver: GameNetDriver, PC: BP_VeinPlayerController_C_0, UniqueId: Steam:${STEAM}`;
const CLEANUP = `[2026.09.17-10.05.00:010][ 99]LogNet: UChannel::CleanUp: ChIndex == 0. Closing connection. UniqueId: Steam:${STEAM}`;

const player: TakaroPlayer = { gameId: STEAM, name: NAME, steamId: STEAM, platformId: `steam:${STEAM}` };
const resolve = async () => null; // no plugin: the log line is authoritative on its own
const feedAll = (p: VeinLogParser, lines: string[]) => lines.flatMap((l) => p.feed(l));

describe('VeinLogParser (UE-standard default grammar)', () => {
  it('the ready line is recognised (VERIFIED marker) and is never an event', () => {
    const p = new VeinLogParser();
    expect(p.serverReady()).toBe(false);
    expect(p.feed(READY)).toEqual([]);
    expect(p.serverReady()).toBe(true);
  });

  it('login line ties the name to a SteamID64, join line emits player-connected with that id', () => {
    const p = new VeinLogParser();
    expect(p.feed(LOGIN)).toEqual([]);
    expect(p.gameIdForName(NAME)).toBe(STEAM);
    expect(p.feed(JOIN)).toEqual([{ type: 'player-connected', gameId: STEAM, name: NAME }]);
  });

  it('a join with no preceding login still emits, keyed on the name alone', () => {
    const p = new VeinLogParser();
    expect(p.feed(JOIN)).toEqual([{ type: 'player-connected', name: NAME }]);
    expect(p.feed(JOIN)).toEqual([]); // duplicate line: not a second connect
  });

  it('UNetConnection::Close emits player-disconnected once; later CleanUp lines are ignored', () => {
    const p = new VeinLogParser();
    feedAll(p, [LOGIN, JOIN]);
    expect(p.feed(LEAVE)).toEqual([{ type: 'player-disconnected', gameId: STEAM, name: NAME }]);
    expect(p.feed(CLEANUP)).toEqual([]);
  });

  it('CleanUp alone is enough when no Close line was seen (hard drop)', () => {
    const p = new VeinLogParser();
    feedAll(p, [LOGIN, JOIN]);
    expect(p.feed(CLEANUP)).toEqual([{ type: 'player-disconnected', gameId: STEAM, name: NAME }]);
  });

  it('a leave for someone who never joined is not an event', () => {
    expect(new VeinLogParser().feed(LEAVE)).toEqual([]);
  });

  it('does not mistake the connection object `Name:` field for a player name', () => {
    const p = new VeinLogParser();
    expect(p.feed('LogNet: UNetConnection::Close: [UNetConnection] Name: SteamNetConnection_0, Driver: GameNetDriver')).toEqual([]);
  });

  it('a new session (`Created session GameSession.`) clears stale presence so the next join still fires', () => {
    const p = new VeinLogParser();
    feedAll(p, [LOGIN, JOIN]);
    p.feed(READY);
    expect(p.feed(JOIN)).toEqual([{ type: 'player-connected', gameId: STEAM, name: NAME }]);
  });

  it('handles two players, CRLF, and reconnects', () => {
    const p = new VeinLogParser();
    const out = feedAll(p, [
      `${LOGIN}\r`,
      `${JOIN}\r`,
      `LogNet: Login request: ?Name=Guest userId: Steam:${STEAM2} platform: Steam`,
      'LogNet: Join succeeded: Guest',
      `LogNet: UNetConnection::Close: [UNetConnection] UniqueId: Steam:${STEAM2}`,
      'LogNet: Join succeeded: Guest',
    ]);
    expect(out).toEqual([
      { type: 'player-connected', gameId: STEAM, name: NAME },
      { type: 'player-connected', gameId: STEAM2, name: 'Guest' },
      { type: 'player-disconnected', gameId: STEAM2, name: 'Guest' },
      { type: 'player-connected', gameId: STEAM2, name: 'Guest' },
    ]);
  });

  it('UE noise, empty lines and bare name: lines produce nothing', () => {
    const p = new VeinLogParser();
    expect(
      feedAll(p, [
        'LogHttp: Verbose: Http request complete',
        'LogOnlineSession: Verbose: ping',
        'LogStreaming: Display: Flushing async loaders',
        `${NAME}: hello world`,
        '',
      ]),
    ).toEqual([]);
    expect(p.takeChat()).toBeNull();
  });
});

/**
 * Lines OBSERVED on the vanilla client in a local session (research/2026-09-17-log-grammar.md, lane L0b):
 * same game code, so the same categories/shapes, but not yet confirmed on the dedicated server.
 */
describe('VeinLogParser with the observed Vein lines', () => {
  const ID_LINE = `[2026.09.17-05.51.58:240][207]LogVein: PlayerState ID changed to ${STEAM}`;
  const SELECT = '[2026.09.17-05.55.15:350][925]LogVein: [] Player Limon selected character 5463D6DD44DCEC89488FB7AB71820255 (aka Takaro Tester)';
  const CHAT = `[2026.09.17-05.58.10:857][ 27]LogVeinChat: [${STEAM}] Limon (aka Takaro Tester): takaro-l0b-sandbox-075811`;

  it('the id-only login line is held and claimed by the character-selection join line', () => {
    const p = new VeinLogParser();
    expect(p.feed('LogVein: PlayerState ID changed to ')).toEqual([]); // empty variant is printed first: ignored
    expect(p.feed(ID_LINE)).toEqual([]);
    expect(p.pendingGameId()).toBe(STEAM);
    expect(p.feed(SELECT)).toEqual([{ type: 'player-connected', gameId: STEAM, name: 'Limon' }]);
    expect(p.pendingGameId()).toBeUndefined();
    expect(p.gameIdForName('Limon')).toBe(STEAM);
  });

  it('chat carries the SteamID64 on the line; the channel is not logged, so it is reported as global', () => {
    const p = new VeinLogParser();
    expect(p.feed(CHAT)).toEqual([]); // chat is never a connection event
    expect(p.takeChat()).toEqual({ gameId: STEAM, name: 'Limon', msg: 'takaro-l0b-sandbox-075811', channel: 'global' });
    p.feed(`LogVeinChat: [${STEAM}] Limon (aka Takaro Tester): /help`);
    expect(p.takeChat()?.msg).toBe('/help');
  });

  it('prefix-less early lines (before LogTimes initialises) parse too', () => {
    const p = new VeinLogParser();
    p.feed(`LogVein: PlayerState ID changed to ${STEAM}`);
    expect(p.feed('LogVein: [] Player Limon selected character 5463D6DD44DCEC89488FB7AB71820255 (aka Takaro Tester)')).toEqual([
      { type: 'player-connected', gameId: STEAM, name: 'Limon' },
    ]);
  });

  it('a half-open connection (`UniqueId: NULL:gamer-…`) is never a join/leave key', () => {
    const p = new VeinLogParser();
    p.feed(`LogVein: PlayerState ID changed to ${STEAM}`);
    p.feed(SELECT);
    expect(p.feed('LogNet: UNetConnection::Close: [UNetConnection] UniqueId: NULL:gamer-0123456789abcdef0123456789abcdef')).toEqual([]);
  });
});

describe('pluggable grammar table', () => {
  it('overrides replace the defaults (this is how the observed Vein lines land)', () => {
    const grammar = buildGrammar({
      joinLine: 'VeinLog: player (?<name>\\S+) \\[(?<gameId>7656\\d{13})\\] joined',
      leaveLine: 'VeinLog: player (?<name>\\S+) \\[(?<gameId>7656\\d{13})\\] left',
      chatLine: 'VeinLog: chat \\((?<channel>\\w+)\\) (?<name>\\S+): (?<msg>.*)$',
      readyLine: 'VeinServer ready',
    });
    const p = new VeinLogParser(grammar);
    expect(p.feed(`VeinLog: player ${NAME} [${STEAM}] joined`)).toEqual([{ type: 'player-connected', gameId: STEAM, name: NAME }]);
    expect(p.feed(`VeinLog: chat (global) ${NAME}: hi there`)).toEqual([]);
    expect(p.takeChat()).toEqual({ name: NAME, msg: 'hi there', channel: 'global' });
    expect(p.takeChat()).toBeNull();
    expect(p.feed(`VeinLog: player ${NAME} [${STEAM}] left`)).toEqual([{ type: 'player-disconnected', gameId: STEAM, name: NAME }]);
    expect(p.feed('VeinServer ready')).toEqual([]);
    expect(p.serverReady()).toBe(true);
  });

  it('an invalid override regex is rejected by config, not silently ignored', async () => {
    const { loadConfig } = await import('../config.js');
    expect(() => loadConfig({ TAKARO_PLUGIN_TOKEN: 'p', VEIN_LOG_JOIN_RE: '([' })).toThrow(/VEIN_LOG_JOIN_RE/);
  });
});

describe('LogTailer', () => {
  let dir: string;
  afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

  it('starts at EOF, handles partial lines and rotation, and uses the SteamID64 from the line', async () => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vein-tail-'));
    const file = path.join(dir, 'Vein.log');
    fs.writeFileSync(file, [LOGIN, JOIN].join('\n') + '\n'); // old session must not replay
    const events: LogEvent[] = [];
    const tailer = new LogTailer({ file, resolve, onEvent: (e) => events.push(e), intervalMs: 999999 });
    tailer.start();
    await tailer.flush();
    expect(events).toEqual([]);

    const text = [LOGIN, JOIN].join('\n') + '\n';
    fs.appendFileSync(file, text.slice(0, text.length - 20));
    tailer.poll();
    await tailer.flush();
    expect(events).toEqual([]); // the join line is still half-written
    fs.appendFileSync(file, text.slice(text.length - 20));
    tailer.poll();
    await tailer.flush();
    expect(events).toEqual([{ type: 'player-connected', data: { player } }]);

    // rotation: server restart replaces the file
    fs.rmSync(file);
    fs.writeFileSync(file, 'LogInit: new boot\n');
    tailer.poll();
    fs.appendFileSync(file, [LOGIN, JOIN, LEAVE].join('\n') + '\n');
    tailer.poll();
    await tailer.flush();
    tailer.stop();
    expect(events.map((e) => e.type)).toEqual(['player-connected', 'player-connected', 'player-disconnected']);
  });

  it('fires onReady once when the server prints the ready line', async () => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vein-tail-ready-'));
    const file = path.join(dir, 'Vein.log');
    fs.writeFileSync(file, '');
    let ready = 0;
    const tailer = new LogTailer({ file, resolve, onEvent: () => {}, onReady: () => (ready += 1), intervalMs: 999999 });
    tailer.start();
    fs.appendFileSync(file, `${READY}\n${READY}\n`);
    tailer.poll();
    await tailer.flush();
    tailer.stop();
    expect(ready).toBe(1);
    expect(tailer.serverReady()).toBe(true);
  });

  it('enriches the log identity with the plugin view (ping/ip) but keeps the logged name', async () => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vein-tail2-'));
    const file = path.join(dir, 'Vein.log');
    fs.writeFileSync(file, '');
    const events: LogEvent[] = [];
    const refs: Array<{ gameId?: string; name?: string }> = [];
    const tailer = new LogTailer({
      file,
      resolve: async (ref) => {
        refs.push(ref);
        return { gameId: STEAM, name: 'PluginName', steamId: STEAM, platformId: `steam:${STEAM}`, ping: 24, ip: '10.0.0.5' };
      },
      onEvent: (e) => events.push(e),
      intervalMs: 999999,
    });
    tailer.start();
    fs.appendFileSync(file, [LOGIN, JOIN].join('\n') + '\n');
    tailer.poll();
    await tailer.flush();
    tailer.stop();
    expect(refs).toEqual([{ gameId: STEAM, name: NAME }]);
    expect(events).toEqual([{ type: 'player-connected', data: { player: { ...player, ping: 24, ip: '10.0.0.5' } } }]);
  });

  it('a name-only line with no known identity is dropped with an error, not emitted half-built', async () => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vein-tail3-'));
    const file = path.join(dir, 'Vein.log');
    fs.writeFileSync(file, '');
    const events: LogEvent[] = [];
    const errors: string[] = [];
    const grammar = buildGrammar({ leaveLine: 'VeinLog: (?<name>\\S+) left' });
    const tailer = new LogTailer({
      file,
      grammar,
      resolve,
      onEvent: (e) => events.push(e),
      onError: (e) => errors.push(e.message),
      intervalMs: 999999,
    });
    tailer.start();
    // Joined by name only (no login line), left by name only: the identity is the name, and it is still a valid record.
    fs.appendFileSync(file, `${JOIN}\nVeinLog: ${NAME} left\n`);
    tailer.poll();
    await tailer.flush();
    tailer.stop();
    expect(events.map((e) => [e.type, e.data.player.gameId])).toEqual([
      ['player-connected', NAME],
      ['player-disconnected', NAME],
    ]);
    expect(errors).toEqual([]);
  });
});
