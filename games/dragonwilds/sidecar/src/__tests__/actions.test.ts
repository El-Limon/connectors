import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { Bridge, EVENT_LOCATION_FALLBACK_MS } from '../bridge.js';
import { MemoryBanStore } from '../dragonwilds/banStore.js';
import { MemoryKnownPlayerStore } from '../dragonwilds/knownStore.js';
import { MemoryCursorStore } from '../dragonwilds/cursorStore.js';
import { DragonwildsPluginClient } from '../dragonwilds/pluginClient.js';
import { GAME_SERVER_ACTIONS, type WsMessage } from '../takaro/protocol.js';
import { MockPlugin, MOCK_PUID, MOCK_PUID_2 } from '../testing/mockPlugin.js';

const LIMON = { gameId: MOCK_PUID, name: 'Limon', epicOnlineServicesId: MOCK_PUID, steamId: '76561198000000001', platformId: `epic:${MOCK_PUID}`, ping: 24 };
const GUEST = { gameId: MOCK_PUID_2, name: 'Guest', epicOnlineServicesId: MOCK_PUID_2, platformId: `epic:${MOCK_PUID_2}`, ping: 40 };

let mock: MockPlugin;
let bridge: Bridge;
let sent: WsMessage[];
let n = 0;

async function call(action: string, args: unknown = {}): Promise<WsMessage> {
  const reply = await bridge.handleRequest({ type: 'request', requestId: `req-${++n}`, payload: { action, args } });
  expect(reply?.requestId).toBe(`req-${n}`);
  expect(sent[sent.length - 1]).toEqual(reply);
  return reply!;
}

async function ok(action: string, args: unknown = {}): Promise<any> {
  const reply = await call(action, args);
  expect(reply.error, `unexpected error for ${action}: ${reply.error}`).toBeUndefined();
  expect(reply.type).toBe('response');
  return reply.payload;
}

let knownStore: MemoryKnownPlayerStore;

beforeEach(async () => {
  knownStore = new MemoryKnownPlayerStore();
  mock = new MockPlugin();
  await mock.start();
  sent = [];
  bridge = new Bridge({
    plugin: new DragonwildsPluginClient({ baseUrl: mock.url(), token: mock.token, timeoutMs: 2000 }),
    takaro: { send: (m) => (sent.push(m), true), sendGameEvent: () => true },
    cursorStore: new MemoryCursorStore(),
    logFile: '/nonexistent',
    logTailMode: 'never',
    adapter: { banStore: new MemoryBanStore(), knownStore },
  });
});
afterEach(async () => {
  bridge.stopEvents();
  await mock.stop();
});

describe('all 17 Takaro actions', () => {
  it('covers the full action list', () => {
    expect(GAME_SERVER_ACTIONS).toHaveLength(17);
  });

  it('sends Authorization: Bearer token to the plugin', async () => {
    await ok('getPlayers');
    expect(mock.requests.every((r) => r.auth === `Bearer ${mock.token}`)).toBe(true);
  });

  it('testReachability reflects plugin /health', async () => {
    expect(await ok('testReachability')).toEqual({ connectable: true, reason: null });
    mock.health = { status: 'ok', capabilities: { players: 'ok', kick: 'degraded', chatEvents: 'unimplemented' } };
    const degraded = await ok('testReachability');
    expect(degraded.connectable).toBe(true);
    expect(degraded.reason).toMatch(/kick=degraded/);
    expect(degraded.reason).not.toMatch(/chatEvents/);
    mock.health = { status: 'starting' };
    expect((await ok('testReachability')).connectable).toBe(false);
    mock.token = 'rotated';
    const unauth = await ok('testReachability');
    expect(unauth).toMatchObject({ connectable: false });
    expect(unauth.reason).toMatch(/401/);
    await mock.stop();
    const down = await ok('testReachability');
    expect(down.connectable).toBe(false);
    expect(down.reason).toMatch(/unreachable/);
  });

  it('getPlayers maps to IGamePlayer with gameId = EOS ProductUserId', async () => {
    mock.players.push({ gameId: 'ffffffffffffffffffffffffffffffff', name: 'Offline', online: false });
    expect(await ok('getPlayers', [])).toEqual([LIMON, GUEST]);
  });

  it('getPlayer accepts flat, nested and JSON-string args, and errors for an unknown player', async () => {
    expect(await ok('getPlayer', { gameId: MOCK_PUID })).toEqual(LIMON);
    expect(await ok('getPlayer', { player: { gameId: MOCK_PUID } })).toEqual(LIMON);
    expect(await ok('getPlayer', JSON.stringify({ gameId: MOCK_PUID }))).toEqual(LIMON);
    expect(await ok('getPlayer', { gameId: `epic:${MOCK_PUID}` })).toEqual(LIMON);
    expect(await ok('getPlayer', { steamId: '76561198000000001' })).toEqual(LIMON);
    // Never-seen player: a minimal offline record built from the identifier. Takaro rejects `{}`, `null` AND an
    // error frame for getPlayer with a user-visible 400 (F7, all three proven on the wire).
    expect(await ok('getPlayer', { gameId: 'deadbeefdeadbeefdeadbeefdeadbeef' })).toEqual({
      gameId: 'deadbeefdeadbeefdeadbeefdeadbeef',
      name: 'deadbeefdeadbeefdeadbeefdeadbeef',
      epicOnlineServicesId: 'deadbeefdeadbeefdeadbeefdeadbeef',
      platformId: 'epic:deadbeefdeadbeefdeadbeefdeadbeef',
      online: false,
    });
    expect((await call('getPlayer', [])).error).toMatch(/player identifier/);
  });

  it('getPlayer answers the last-known record (online:false) for a player who has left the server', async () => {
    // Takaro calls getPlayer for offline players while running commandTrigger (finding F7).
    expect(await ok('getPlayer', { gameId: MOCK_PUID })).toEqual(LIMON);
    mock.players = [];
    expect(await ok('getPlayers', [])).toEqual([]);
    expect(await ok('getPlayer', { gameId: MOCK_PUID })).toEqual({ ...LIMON, online: false });
    // Any identifier form resolves to the same record.
    expect(await ok('getPlayer', { player: { steamId: '76561198000000001' } })).toEqual({ ...LIMON, online: false });
    expect(await ok('getPlayer', { gameId: 'deadbeefdeadbeefdeadbeefdeadbeef' })).toMatchObject({ gameId: 'deadbeefdeadbeefdeadbeefdeadbeef', online: false });
  });

  it('remembers players across a sidecar restart so an offline getPlayer still works', async () => {
    expect(await ok('getPlayers', [])).toEqual([LIMON, GUEST]);
    expect(knownStore.players.map((p) => p.gameId)).toContain(MOCK_PUID);

    // A fresh sidecar process with an empty plugin player list, but the persisted store.
    mock.players = [];
    const restarted = new Bridge({
      plugin: new DragonwildsPluginClient({ baseUrl: mock.url(), token: mock.token, timeoutMs: 2000 }),
      takaro: { send: () => true, sendGameEvent: () => true },
      cursorStore: new MemoryCursorStore(),
      logFile: '/nonexistent',
      logTailMode: 'never',
      adapter: { banStore: new MemoryBanStore(), knownStore },
    });
    try {
      const reply = await restarted.handleRequest({ type: 'request', requestId: 'r', payload: { action: 'getPlayer', args: { gameId: MOCK_PUID } } });
      expect(reply?.error).toBeUndefined();
      expect(reply?.payload).toEqual({ ...LIMON, online: false });
    } finally {
      restarted.stopEvents();
    }
  });

  it('getPlayerLocation returns IPosition and resolves any identifier to the plugin gameId', async () => {
    expect(await ok('getPlayerLocation', { player: { gameId: MOCK_PUID } })).toEqual({ x: 10.5, y: 20, z: -3 });
    expect(mock.lastRequest('GET', `/players/${MOCK_PUID}/location`)).toBeTruthy();
    expect(await ok('getPlayerLocation', { steamId: '76561198000000001' })).toEqual({ x: 10.5, y: 20, z: -3 });
    expect((await call('getPlayerLocation', { gameId: 'nobody' })).error).toMatch(/404/);
  });

  describe('getPlayerLocation during a connect/disconnect event window', () => {
    const player = LIMON;

    it('without a forwarded event a failing lookup is still an error', async () => {
      mock.unimplemented.add('GET /players/:id/location');
      expect((await call('getPlayerLocation', { gameId: MOCK_PUID })).error).toMatch(/501/);
    });

    it('answers the origin when the plugin cannot locate (501) a player whose event was just forwarded', async () => {
      mock.unimplemented.add('GET /players/:id/location');
      bridge.noteConnectionEvent('player-connected', { player });
      expect(await ok('getPlayerLocation', { gameId: MOCK_PUID })).toEqual({ x: 0, y: 0, z: 0 });
      expect(await ok('getPlayerLocation', { player: { gameId: MOCK_PUID } })).toEqual({ x: 0, y: 0, z: 0 });
    });

    it('answers the last real position after the player left', async () => {
      expect(await ok('getPlayerLocation', { gameId: MOCK_PUID })).toEqual({ x: 10.5, y: 20, z: -3 });
      mock.players = mock.players.filter((p) => p.gameId !== MOCK_PUID);
      bridge.noteConnectionEvent('player-disconnected', { player });
      expect(await ok('getPlayerLocation', { gameId: MOCK_PUID })).toEqual({ x: 10.5, y: 20, z: -3 });
    });

    it('window expires and ignores non-connection events and other players', async () => {
      mock.unimplemented.add('GET /players/:id/location');
      bridge.noteConnectionEvent('chat-message', { player });
      expect(bridge.eventLocationFallback({ gameId: MOCK_PUID })).toBeNull();
      bridge.noteConnectionEvent('player-disconnected', { player }, 1_000);
      expect(bridge.eventLocationFallback({ gameId: MOCK_PUID }, 1_000 + EVENT_LOCATION_FALLBACK_MS)).toEqual({ x: 0, y: 0, z: 0 });
      expect(bridge.eventLocationFallback({ gameId: MOCK_PUID }, 2_000 + EVENT_LOCATION_FALLBACK_MS)).toBeNull();
      expect(bridge.eventLocationFallback({ gameId: 'someone-else' })).toBeNull();
    });
  });

  it('getPlayerInventory returns IItemDTO[]', async () => {
    expect(await ok('getPlayerInventory', { gameId: MOCK_PUID })).toEqual([
      { code: 'Item_Log_Oak', name: 'Oak Logs', amount: 25 },
      { code: 'Item_Sword_Bronze', name: 'Bronze Sword', amount: 1 },
    ]);
    expect(await ok('getPlayerInventory', { gameId: MOCK_PUID_2 })).toEqual([]);
  });

  it('giveItem uses key `item` and forwards amount (Dragonwilds has no quality tier)', async () => {
    expect(await ok('giveItem', { player: { gameId: MOCK_PUID }, item: 'Item_Sword_Bronze', amount: 5, quality: '2' })).toEqual({});
    expect(mock.lastRequest('POST', '/give')?.body).toEqual({ gameId: MOCK_PUID, code: 'Item_Sword_Bronze', amount: 5 });
    await ok('giveItem', JSON.stringify({ gameId: MOCK_PUID, item: 'Item_Log_Oak', amount: 1 }));
    expect(mock.lastRequest('POST', '/give')?.body).toEqual({ gameId: MOCK_PUID, code: 'Item_Log_Oak', amount: 1 });
    expect((await call('giveItem', { gameId: MOCK_PUID })).error).toMatch(/'item'/);
    expect((await call('giveItem', { gameId: MOCK_PUID, item: 'x', amount: 0 })).error).toMatch(/positive/);
  });

  it('listItems / listEntities / listLocations', async () => {
    expect(await ok('listItems')).toEqual([
      { code: 'Item_Log_Oak', name: 'Oak Logs', description: 'Basic material' },
      { code: 'Item_Sword_Bronze', name: 'Bronze Sword' },
    ]);
    expect(await ok('listItems', { search: 'sword' })).toEqual([{ code: 'Item_Sword_Bronze', name: 'Bronze Sword' }]);
    expect(await ok('listEntities')).toEqual([
      { code: 'AI_Goblin_Melee', name: 'Goblin', type: 'hostile' },
      { code: 'AI_Chicken', name: 'Chicken', type: 'neutral' },
      { code: 'AI_Merchant', name: 'Merchant', type: 'friendly' },
    ]);
    expect(await ok('listLocations', '{}')).toEqual([
      { code: 'lodestone_ashenfall', name: 'Ashenfall Lodestone', position: { x: 0, y: 100, z: 0 }, radius: 50 },
    ]);
  });

  it('executeConsoleCommand returns CommandOutput', async () => {
    expect(await ok('executeConsoleCommand', { command: 'players' })).toEqual({ success: true, rawResult: 'ran players', errorMessage: null });
    mock.commandHandler = () => ({ success: false, output: 'unknown command' });
    expect(await ok('executeConsoleCommand', { command: 'nope' })).toEqual({ success: false, rawResult: 'unknown command', errorMessage: 'unknown command' });
    expect((await call('executeConsoleCommand', {})).error).toMatch(/'command'/);
  });

  it('sendMessage: global, opts.recipient, senderNameOverride', async () => {
    await ok('sendMessage', { message: 'hello all' });
    expect(mock.lastRequest('POST', '/message')?.body).toEqual({ text: 'hello all', senderName: 'Server' });
    await ok('sendMessage', { message: 'Welcome', opts: { recipient: { gameId: MOCK_PUID }, senderNameOverride: 'Takaro' } });
    expect(mock.lastRequest('POST', '/message')?.body).toEqual({ text: 'Welcome', recipientGameId: MOCK_PUID, senderName: 'Takaro' });
    expect((await call('sendMessage', {})).error).toMatch(/'message'/);
  });

  it('teleportPlayer sends x/y/z (and optional yaw / named target)', async () => {
    await ok('teleportPlayer', { player: { gameId: MOCK_PUID }, x: 1, y: '2', z: 3.5 });
    expect(mock.lastRequest('POST', '/teleport')?.body).toEqual({ gameId: MOCK_PUID, x: 1, y: 2, z: 3.5 });
    await ok('teleportPlayer', { gameId: MOCK_PUID, x: 1, y: 2, z: 3, yaw: 90 });
    expect(mock.lastRequest('POST', '/teleport')?.body).toEqual({ gameId: MOCK_PUID, x: 1, y: 2, z: 3, yaw: 90 });
    await ok('teleportPlayer', { gameId: MOCK_PUID, target: 'lodestone_ashenfall' });
    expect(mock.lastRequest('POST', '/teleport')?.body).toEqual({ gameId: MOCK_PUID, x: 0, y: 0, z: 0, target: 'lodestone_ashenfall' });
    expect((await call('teleportPlayer', { gameId: MOCK_PUID, x: 1 })).error).toMatch(/numeric/);
  });

  it('kickPlayer', async () => {
    await ok('kickPlayer', { player: { gameId: MOCK_PUID }, reason: 'afk' });
    expect(mock.lastRequest('POST', '/kick')?.body).toEqual({ gameId: MOCK_PUID, reason: 'afk' });
  });

  it('banPlayer / listBans / unbanPlayer round-trip (incl. an offline id)', async () => {
    await ok('banPlayer', { player: { gameId: MOCK_PUID }, reason: 'grief', expiresAt: '2030-01-01T00:00:00.000Z' });
    expect(mock.lastRequest('POST', '/ban')?.body).toEqual({ gameId: MOCK_PUID, reason: 'grief' });
    await ok('banPlayer', { gameId: 'deadbeefdeadbeefdeadbeefdeadbeef' });
    expect(mock.lastRequest('POST', '/ban')?.body).toEqual({ gameId: 'deadbeefdeadbeefdeadbeefdeadbeef' });
    expect(await ok('listBans')).toEqual([
      { player: LIMON_BAN, reason: 'grief', expiresAt: '2030-01-01T00:00:00.000Z' },
      {
        player: { gameId: 'deadbeefdeadbeefdeadbeefdeadbeef', name: 'deadbeefdeadbeefdeadbeefdeadbeef', epicOnlineServicesId: 'deadbeefdeadbeefdeadbeefdeadbeef', platformId: 'epic:deadbeefdeadbeefdeadbeefdeadbeef' },
        reason: '',
        expiresAt: null,
      },
    ]);
    await ok('unbanPlayer', { gameId: 'deadbeefdeadbeefdeadbeefdeadbeef' });
    expect(await ok('listBans')).toHaveLength(1);
  });

  it('shutdown', async () => {
    expect(await ok('shutdown')).toEqual({});
    expect(mock.lastRequest('POST', '/shutdown')).toBeTruthy();
  });

  it('plugin 501 becomes a clear error response, not a crash', async () => {
    mock.unimplemented.add('POST /teleport');
    mock.unimplemented.add('GET /players/:id/inventory');
    mock.unimplemented.add('GET /entities');
    const t = await call('teleportPlayer', { gameId: MOCK_PUID, x: 1, y: 2, z: 3 });
    expect(t.type).toBe('response');
    expect(t.error).toMatch(/cannot perform 'teleportPlayer'.*not implemented \/teleport \(HTTP 501\)/);
    expect((await call('getPlayerInventory', { gameId: MOCK_PUID })).error).toMatch(/HTTP 501/);
    expect((await call('listEntities')).error).toMatch(/HTTP 501/);
    expect(await ok('getPlayers')).toHaveLength(2); // still serving
  });

  it('unknown action and malformed request get error responses with the same requestId', async () => {
    const unknown = await call('flyToMoon');
    expect(unknown.error).toMatch(/Unknown Takaro action/);
    expect(unknown.requestId).toBe(`req-${n}`);
    const reply = await bridge.handleRequest({ type: 'request', requestId: 'bad', payload: {} });
    expect(reply).toEqual({ type: 'response', requestId: 'bad', error: 'Takaro request missing action' });
    expect(await bridge.handleRequest({ type: 'request', payload: { action: 'getPlayers' } })).toBeNull();
  });
});

const LIMON_BAN = { gameId: MOCK_PUID, name: 'Limon', epicOnlineServicesId: MOCK_PUID, steamId: '76561198000000001', platformId: `epic:${MOCK_PUID}` };
