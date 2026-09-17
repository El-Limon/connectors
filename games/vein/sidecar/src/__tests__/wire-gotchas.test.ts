import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Bridge } from '../bridge.js';
import { MemoryBanStore } from '../vein/banStore.js';
import { MemoryCursorStore } from '../vein/cursorStore.js';
import { MemoryKnownPlayerStore } from '../vein/knownStore.js';
import { buildGrammar, VeinLogParser } from '../vein/logTail.js';
import { VeinHttpApi, parsePlayers } from '../vein/httpApi.js';
import { VeinPluginClient } from '../vein/pluginClient.js';
import { TakaroWsClient } from '../takaro/client.js';
import { MockPlugin, MOCK_STEAMID } from '../testing/mockPlugin.js';
import type { WsMessage } from '../takaro/protocol.js';

/**
 * The banked Takaro wire gotchas, in one matrix (context/takaro/*.md + the Dragonwilds findings F7/F10).
 * Every row here has cost a live debugging session at least once.
 */
let mock: MockPlugin;
let bridge: Bridge;
let sent: WsMessage[];
let events: Array<[string, unknown]>;
let identified: boolean;
let n = 0;

const call = async (action: string, args: unknown = {}): Promise<WsMessage> =>
  (await bridge.handleRequest({ type: 'request', requestId: `wire-${++n}`, payload: { action, args } }))!;

beforeEach(async () => {
  mock = new MockPlugin();
  await mock.start();
  sent = [];
  events = [];
  identified = true;
  bridge = new Bridge({
    plugin: new VeinPluginClient({ baseUrl: mock.url(), token: mock.token, timeoutMs: 2000 }),
    takaro: {
      send: (m) => (sent.push(m), true),
      // A gameEvent only reaches Takaro on an OPEN *and* identified socket (F10).
      sendGameEvent: (t, d) => (identified ? (events.push([t, d]), true) : false),
    },
    cursorStore: new MemoryCursorStore(),
    logFile: '/nonexistent',
    logTailMode: 'never',
    adapter: { banStore: new MemoryBanStore(), knownStore: new MemoryKnownPlayerStore() },
  });
});
afterEach(async () => {
  bridge.stopEvents();
  await mock.stop();
  vi.restoreAllMocks();
});

describe('wire gotchas: request frames', () => {
  it('echoes the requestId on both success and error, and never answers without one', async () => {
    expect(await call('getPlayers')).toMatchObject({ type: 'response', requestId: 'wire-1' });
    const bad = await call('teleportToTheMoon');
    expect(bad).toMatchObject({ requestId: 'wire-2' });
    expect(bad.error).toMatch(/Unknown Takaro action/);
    expect(await bridge.handleRequest({ type: 'request', payload: { action: 'getPlayers', args: {} } })).toBeNull();
  });

  it('accepts args as a JSON string (Takaro sometimes sends the payload pre-serialised)', async () => {
    const reply = await call('getPlayer', JSON.stringify({ gameId: MOCK_STEAMID }));
    expect(reply.error).toBeUndefined();
    expect(reply.payload).toMatchObject({ gameId: MOCK_STEAMID, platformId: `steam:${MOCK_STEAMID}` });
    expect((await call('getPlayers', '[]')).error).toBeUndefined();
  });

  it('explicit JSON null optional args never throw (dimension, reason, expiresAt, quality, opts)', async () => {
    for (const [action, args] of [
      ['teleportPlayer', { gameId: MOCK_STEAMID, x: 1, y: 2, z: 3, dimension: null }],
      ['kickPlayer', { gameId: MOCK_STEAMID, reason: null }],
      ['banPlayer', { gameId: MOCK_STEAMID, reason: null, expiresAt: null }],
      ['giveItem', { gameId: MOCK_STEAMID, item: 'Item_Wood_Plank', amount: 1, quality: null }],
      ['sendMessage', { message: 'hi', opts: null }],
    ] as Array<[string, unknown]>) {
      const reply = await call(action, args);
      expect(reply.error, `${action} must tolerate explicit nulls`).toBeUndefined();
    }
  });

  it('getPlayer for an offline/unknown id returns a real IGamePlayer, never {}, null or an error frame (F7)', async () => {
    const online = await call('getPlayer', { gameId: MOCK_STEAMID });
    mock.players = [];
    const offline = await call('getPlayer', { player: { gameId: MOCK_STEAMID } });
    expect(offline.error).toBeUndefined();
    expect(offline.payload).toEqual({ ...(online.payload as object), online: false });

    const never = await call('getPlayer', { gameId: '76561198009999999' });
    expect(never.error).toBeUndefined();
    expect(never.payload).toEqual({
      gameId: '76561198009999999',
      name: '76561198009999999',
      steamId: '76561198009999999',
      platformId: 'steam:76561198009999999',
      online: false,
    });
  });

  it('an unknown console command answers {success:false}, not an error frame', async () => {
    mock.commandStatus = 400;
    const reply = await call('executeConsoleCommand', { command: 'nonsense' });
    expect(reply.error).toBeUndefined();
    expect(reply.payload).toMatchObject({ success: false });
    expect((reply.payload as { errorMessage: string }).errorMessage).toMatch(/unknown command/);

    // A command the plugin runs but the game rejects is also {success:false}, never an error frame.
    mock.commandStatus = null;
    mock.commandHandler = () => ({ success: false, output: 'No such command' });
    expect((await call('executeConsoleCommand', { command: 'bogus' })).payload).toMatchObject({ success: false, rawResult: 'No such command' });
  });
});

describe('wire gotchas: events and delivery', () => {
  it('no gameEvent leaves the sidecar before identify; the queue is flushed in order afterwards', async () => {
    identified = false;
    mock.pushEvent('chat-message', { player: mock.players[0], msg: 'first' });
    mock.pushEvent('chat-message', { player: mock.players[0], msg: 'second' });
    await bridge.poller.pollOnce();
    expect(events).toEqual([]);
    expect(bridge.pending().map((e) => (e.data as { msg: string }).msg)).toEqual(['first', 'second']);
    // The PERSISTED cursor must not advance on a queued (unsent) event.
    expect(bridge.poller.cursor()).toBe(0);
    expect(bridge.poller.scanCursor()).toBe(2);

    identified = true;
    expect(bridge.flushPending()).toBe(2);
    expect(events.map((e) => (e[1] as { msg: string }).msg)).toEqual(['first', 'second']);
    expect(bridge.poller.cursor()).toBe(2);
  });

  it('a bootId change resets the cursor and reconciles online players', async () => {
    mock.bootId = 'boot-a';
    mock.pushEvent('player-connected', { player: mock.players[0] });
    await bridge.poller.pollOnce();
    expect(bridge.onlinePlayers().map((p) => p.gameId)).toEqual([MOCK_STEAMID]);
    expect(bridge.poller.cursor()).toBe(1);

    // New game-server process: seq restarts at 1 with a different bootId, and the player is gone.
    mock.bootId = 'boot-b';
    mock.events = [];
    mock.players = [];
    mock.pushEvent('log', { msg: 'LogInit: fresh boot' });
    await bridge.poller.pollOnce();
    await bridge.reconcileOnline();
    expect(events.map((e) => e[0])).toContain('player-disconnected');
    expect(bridge.onlinePlayers()).toEqual([]);
  });

  it('a timed ban is lifted by the sidecar via the plugin /unban', async () => {
    await call('banPlayer', { gameId: MOCK_STEAMID, reason: 'cooldown', expiresAt: new Date(Date.now() + 1000).toISOString() });
    expect(bridge.adapter.pendingBans()).toHaveLength(1);
    expect(await bridge.adapter.sweepExpiredBans(Date.now())).toEqual([]);
    expect(await bridge.adapter.sweepExpiredBans(Date.now() + 5000)).toEqual([MOCK_STEAMID]);
    expect(mock.lastRequest('POST', '/unban')?.body).toEqual({ gameId: MOCK_STEAMID });
    expect(bridge.adapter.pendingBans()).toEqual([]);
  });

  // F17 (L6a/L6d): `docker compose restart vein` gives the game container a NEW network namespace; `network_mode:
  // service:vein` pins the sidecar to the old, dead one. The health probe must keep running after the Takaro socket
  // drops (DNS dies with the namespace) and self-exit, so `restart: unless-stopped` re-attaches us.
  it('F17: the health probe survives a Takaro disconnect and still self-exits on a dead namespace', async () => {
    const exit = vi.spyOn(process, 'exit').mockImplementation(((): never => undefined as never));
    const dead = new Bridge({
      plugin: new VeinPluginClient({ baseUrl: 'http://127.0.0.1:1', token: 't', timeoutMs: 200 }),
      takaro: { send: () => true, sendGameEvent: () => true },
      cursorStore: new MemoryCursorStore(),
      logFile: '/nonexistent',
      logTailMode: 'never',
      exitAfterUnreachableMs: 1,
      gameReachable: async () => false, // the game's own :8080 is gone too -> our netns is dead
      healthCheckIntervalMs: 60000,
      pollIntervalMs: 60000,
    });
    dead.startHealthWatch();
    // Takaro drops (this is what used to kill the health timer, so the sidecar never noticed the dead netns).
    dead.stopEvents();
    await dead.refreshHealth();
    await new Promise((r) => setTimeout(r, 5));
    await dead.refreshHealth();
    expect(exit).toHaveBeenCalledWith(1);
    dead.stopHealthWatch();
    exit.mockRestore();
  });

  it('F17: does NOT take the fast netns exit while the game HTTP API still answers', async () => {
    const exit = vi.spyOn(process, 'exit').mockImplementation(((): never => undefined as never));
    const pluginOnlyDown = new Bridge({
      plugin: new VeinPluginClient({ baseUrl: 'http://127.0.0.1:1', token: 't', timeoutMs: 200 }),
      takaro: { send: () => true, sendGameEvent: () => true },
      cursorStore: new MemoryCursorStore(),
      logFile: '/nonexistent',
      logTailMode: 'never',
      exitAfterUnreachableMs: 1,
      exitAfterPluginLossMs: 3_600_000,
      gameReachable: async () => true,
      healthCheckIntervalMs: 60000,
      pollIntervalMs: 60000,
    });
    await pluginOnlyDown.refreshHealth();
    await new Promise((r) => setTimeout(r, 5));
    await pluginOnlyDown.refreshHealth();
    pluginOnlyDown.stopEvents();
    expect(exit).not.toHaveBeenCalled();
    exit.mockRestore();
  });

  it('exits when the plugin has been unreachable past the limit (container re-attach)', async () => {
    const exit = vi.spyOn(process, 'exit').mockImplementation(((): never => undefined as never));
    const shortLived = new Bridge({
      plugin: new VeinPluginClient({ baseUrl: 'http://127.0.0.1:1', token: 't', timeoutMs: 200 }),
      takaro: { send: () => true, sendGameEvent: () => true },
      cursorStore: new MemoryCursorStore(),
      logFile: '/nonexistent',
      logTailMode: 'never',
      exitAfterUnreachableMs: 1,
      healthCheckIntervalMs: 60000,
      pollIntervalMs: 60000,
    });
    await shortLived.refreshHealth();
    await new Promise((r) => setTimeout(r, 5));
    await shortLived.refreshHealth();
    shortLived.stopEvents();
    expect(exit).toHaveBeenCalledWith(1);
  });
});

describe('wire gotchas: reconnect backoff', () => {
  it('backs off 2 s → 60 s and no further', () => {
    const client = new TakaroWsClient('ws://127.0.0.1:1', { identityToken: 'vein', registrationToken: '', serverName: 'x' });
    const delays: number[] = [];
    for (let i = 0; i < 8; i += 1) {
      delays.push(client.nextReconnectDelay());
      (client as unknown as { reconnectAttempts: number }).reconnectAttempts = i + 1;
    }
    expect(delays).toEqual([2000, 4000, 8000, 16000, 32000, 60000, 60000, 60000]);
  });

  it('refuses to send a gameEvent while not identified', () => {
    const client = new TakaroWsClient('ws://127.0.0.1:1', { identityToken: 'vein', registrationToken: '', serverName: 'x' });
    expect(client.identified()).toBe(false);
    expect(client.sendGameEvent('log', { msg: 'x' })).toBe(false);
  });
});

describe('wire gotchas: log grammar table and the game HTTP API', () => {
  it('the ready line is the verified marker; join/leave defaults are UE-standard and overridable', () => {
    const p = new VeinLogParser();
    expect(p.feed('[2026.09.17-10.00.00:000][  0]LogGameMode: Created session GameSession.')).toEqual([]);
    expect(p.serverReady()).toBe(true);

    const custom = new VeinLogParser(buildGrammar({ joinLine: 'VEINJOIN (?<gameId>7656\\d{13}) (?<name>\\S+)' }));
    expect(custom.feed(`VEINJOIN ${MOCK_STEAMID} Tester`)).toEqual([{ type: 'player-connected', gameId: MOCK_STEAMID, name: 'Tester' }]);
  });

  it("Vein's own HTTP API is tolerated when absent and parsed tolerantly when present", async () => {
    expect(new VeinHttpApi({ baseUrl: '' }).enabled()).toBe(false);
    expect(await new VeinHttpApi({ baseUrl: '' }).getPlayers()).toBeNull();
    expect(await new VeinHttpApi({ baseUrl: 'http://127.0.0.1:1', timeoutMs: 100 }).status()).toMatchObject({ reachable: false });

    expect(parsePlayers(JSON.stringify([{ steamId: MOCK_STEAMID, name: 'Tester', ping: 12 }]))).toEqual([
      { gameId: MOCK_STEAMID, name: 'Tester', steamId: MOCK_STEAMID, platformId: `steam:${MOCK_STEAMID}`, ping: 12, online: true },
    ]);
    expect(parsePlayers(JSON.stringify({ players: [{ id: MOCK_STEAMID, playerName: 'Tester' }] }))?.[0].gameId).toBe(MOCK_STEAMID);
    expect(parsePlayers('<html>not json</html>')).toBeNull();
    expect(parsePlayers('{"unexpected":1}')).toBeNull();
  });

  it('getPlayers falls back to the game HTTP API when the plugin /players fails', async () => {
    const api = new VeinHttpApi({
      baseUrl: 'http://127.0.0.1:9',
      fetchImpl: async () => new Response(JSON.stringify([{ steamId: MOCK_STEAMID, name: 'FromGameApi' }]), { status: 200 }),
    });
    const fallback = new Bridge({
      plugin: new VeinPluginClient({ baseUrl: 'http://127.0.0.1:1', token: 't', timeoutMs: 200 }),
      takaro: { send: (m) => (sent.push(m), true), sendGameEvent: () => true },
      cursorStore: new MemoryCursorStore(),
      logFile: '/nonexistent',
      logTailMode: 'never',
      adapter: { httpPlayers: () => api.getPlayers() },
    });
    try {
      const reply = await fallback.handleRequest({ type: 'request', requestId: 'fb-1', payload: { action: 'getPlayers', args: {} } });
      expect(reply?.error).toBeUndefined();
      expect(reply?.payload).toEqual([
        { gameId: MOCK_STEAMID, name: 'FromGameApi', steamId: MOCK_STEAMID, platformId: `steam:${MOCK_STEAMID}` },
      ]);
    } finally {
      fallback.stopEvents();
    }
  });
});
