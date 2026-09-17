import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { Bridge, EVENT_LOCATION_FALLBACK_MS } from '../bridge.js';
import { MemoryBanStore } from '../vein/banStore.js';
import { MemoryKnownPlayerStore } from '../vein/knownStore.js';
import { MemoryCursorStore } from '../vein/cursorStore.js';
import { VeinPluginClient } from '../vein/pluginClient.js';
import { GAME_SERVER_ACTIONS, type WsMessage } from '../takaro/protocol.js';
import { MockPlugin, MOCK_STEAMID, MOCK_STEAMID_2 } from '../testing/mockPlugin.js';

const HENDRIK = { gameId: MOCK_STEAMID, name: 'Tester', steamId: MOCK_STEAMID, platformId: `steam:${MOCK_STEAMID}`, ip: '10.0.0.5', ping: 24 };
const GUEST = { gameId: MOCK_STEAMID_2, name: 'Guest', steamId: MOCK_STEAMID_2, platformId: `steam:${MOCK_STEAMID_2}`, ping: 40 };

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
    plugin: new VeinPluginClient({ baseUrl: mock.url(), token: mock.token, timeoutMs: 2000 }),
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

  it('getPlayers maps to IGamePlayer with gameId = SteamID64', async () => {
    mock.players.push({ gameId: 'ffffffffffffffffffffffffffffffff', name: 'Offline', online: false });
    expect(await ok('getPlayers', [])).toEqual([HENDRIK, GUEST]);
  });

  it('getPlayer accepts flat, nested and JSON-string args, and errors for an unknown player', async () => {
    expect(await ok('getPlayer', { gameId: MOCK_STEAMID })).toEqual(HENDRIK);
    expect(await ok('getPlayer', { player: { gameId: MOCK_STEAMID } })).toEqual(HENDRIK);
    expect(await ok('getPlayer', JSON.stringify({ gameId: MOCK_STEAMID }))).toEqual(HENDRIK);
    expect(await ok('getPlayer', { gameId: `steam:${MOCK_STEAMID}` })).toEqual(HENDRIK);
    expect(await ok('getPlayer', { steamId: '76561198000000001' })).toEqual(HENDRIK);
    // Never-seen player: a minimal offline record built from the identifier. Takaro rejects `{}`, `null` AND an
    // error frame for getPlayer with a user-visible 400 (F7, all three proven on the wire).
    expect(await ok('getPlayer', { gameId: '76561198009999999' })).toEqual({
      gameId: '76561198009999999',
      name: '76561198009999999',
      steamId: '76561198009999999',
      platformId: 'steam:76561198009999999',
      online: false,
    });
    expect((await call('getPlayer', [])).error).toMatch(/player identifier/);
  });

  it('getPlayer answers the last-known record (online:false) for a player who has left the server', async () => {
    // Takaro calls getPlayer for offline players while running commandTrigger (finding F7).
    expect(await ok('getPlayer', { gameId: MOCK_STEAMID })).toEqual(HENDRIK);
    mock.players = [];
    expect(await ok('getPlayers', [])).toEqual([]);
    expect(await ok('getPlayer', { gameId: MOCK_STEAMID })).toEqual({ ...HENDRIK, online: false });
    // Any identifier form resolves to the same record.
    expect(await ok('getPlayer', { player: { steamId: '76561198000000001' } })).toEqual({ ...HENDRIK, online: false });
    expect(await ok('getPlayer', { gameId: '76561198009999999' })).toMatchObject({ gameId: '76561198009999999', online: false });
  });

  it('remembers players across a sidecar restart so an offline getPlayer still works', async () => {
    expect(await ok('getPlayers', [])).toEqual([HENDRIK, GUEST]);
    expect(knownStore.players.map((p) => p.gameId)).toContain(MOCK_STEAMID);

    // A fresh sidecar process with an empty plugin player list, but the persisted store.
    mock.players = [];
    const restarted = new Bridge({
      plugin: new VeinPluginClient({ baseUrl: mock.url(), token: mock.token, timeoutMs: 2000 }),
      takaro: { send: () => true, sendGameEvent: () => true },
      cursorStore: new MemoryCursorStore(),
      logFile: '/nonexistent',
      logTailMode: 'never',
      adapter: { banStore: new MemoryBanStore(), knownStore },
    });
    try {
      const reply = await restarted.handleRequest({ type: 'request', requestId: 'r', payload: { action: 'getPlayer', args: { gameId: MOCK_STEAMID } } });
      expect(reply?.error).toBeUndefined();
      expect(reply?.payload).toEqual({ ...HENDRIK, online: false });
    } finally {
      restarted.stopEvents();
    }
  });

  it('getPlayerLocation returns IPosition and resolves any identifier to the plugin gameId', async () => {
    expect(await ok('getPlayerLocation', { player: { gameId: MOCK_STEAMID } })).toEqual({ x: 10.5, y: 20, z: -3 });
    expect(mock.lastRequest('GET', `/players/${MOCK_STEAMID}/location`)).toBeTruthy();
    expect(await ok('getPlayerLocation', { steamId: '76561198000000001' })).toEqual({ x: 10.5, y: 20, z: -3 });
    expect((await call('getPlayerLocation', { gameId: 'nobody' })).error).toMatch(/404/);
  });

  describe('getPlayerLocation during a connect/disconnect event window', () => {
    const player = HENDRIK;

    it('without a forwarded event a failing lookup is still an error', async () => {
      mock.unimplemented.add('GET /players/:id/location');
      expect((await call('getPlayerLocation', { gameId: MOCK_STEAMID })).error).toMatch(/501/);
    });

    it('answers the origin when the plugin cannot locate (501) a player whose event was just forwarded', async () => {
      mock.unimplemented.add('GET /players/:id/location');
      bridge.noteConnectionEvent('player-connected', { player });
      expect(await ok('getPlayerLocation', { gameId: MOCK_STEAMID })).toEqual({ x: 0, y: 0, z: 0 });
      expect(await ok('getPlayerLocation', { player: { gameId: MOCK_STEAMID } })).toEqual({ x: 0, y: 0, z: 0 });
    });

    it('answers the last real position after the player left', async () => {
      expect(await ok('getPlayerLocation', { gameId: MOCK_STEAMID })).toEqual({ x: 10.5, y: 20, z: -3 });
      mock.players = mock.players.filter((p) => p.gameId !== MOCK_STEAMID);
      bridge.noteConnectionEvent('player-disconnected', { player });
      expect(await ok('getPlayerLocation', { gameId: MOCK_STEAMID })).toEqual({ x: 10.5, y: 20, z: -3 });
    });

    it('window expires and ignores non-connection events and other players', async () => {
      mock.unimplemented.add('GET /players/:id/location');
      bridge.noteConnectionEvent('chat-message', { player });
      expect(bridge.eventLocationFallback({ gameId: MOCK_STEAMID })).toBeNull();
      bridge.noteConnectionEvent('player-disconnected', { player }, 1_000);
      expect(bridge.eventLocationFallback({ gameId: MOCK_STEAMID }, 1_000 + EVENT_LOCATION_FALLBACK_MS)).toEqual({ x: 0, y: 0, z: 0 });
      expect(bridge.eventLocationFallback({ gameId: MOCK_STEAMID }, 2_000 + EVENT_LOCATION_FALLBACK_MS)).toBeNull();
      expect(bridge.eventLocationFallback({ gameId: 'someone-else' })).toBeNull();
    });
  });

  it('getPlayerInventory returns IItemDTO[]', async () => {
    expect(await ok('getPlayerInventory', { gameId: MOCK_STEAMID })).toEqual([
      { code: 'Item_Wood_Plank', name: 'Wooden Plank', amount: 25 },
      { code: 'Item_Weapon_Pistol', name: 'Pistol', amount: 1 },
    ]);
    expect(await ok('getPlayerInventory', { gameId: MOCK_STEAMID_2 })).toEqual([]);
  });

  it('giveItem uses key `item` and forwards amount (Vein has no quality tier)', async () => {
    expect(await ok('giveItem', { player: { gameId: MOCK_STEAMID }, item: 'Item_Weapon_Pistol', amount: 5, quality: '2' })).toEqual({});
    expect(mock.lastRequest('POST', '/give')?.body).toEqual({ gameId: MOCK_STEAMID, code: 'Item_Weapon_Pistol', amount: 5 });
    await ok('giveItem', JSON.stringify({ gameId: MOCK_STEAMID, item: 'Item_Wood_Plank', amount: 1 }));
    expect(mock.lastRequest('POST', '/give')?.body).toEqual({ gameId: MOCK_STEAMID, code: 'Item_Wood_Plank', amount: 1 });
    expect((await call('giveItem', { gameId: MOCK_STEAMID })).error).toMatch(/'item'/);
    expect((await call('giveItem', { gameId: MOCK_STEAMID, item: 'x', amount: 0 })).error).toMatch(/positive/);
  });

  it('listItems / listEntities / listLocations', async () => {
    expect(await ok('listItems')).toEqual([
      { code: 'Item_Wood_Plank', name: 'Wooden Plank', description: 'Basic material' },
      { code: 'Item_Weapon_Pistol', name: 'Pistol' },
    ]);
    expect(await ok('listItems', { search: 'pistol' })).toEqual([{ code: 'Item_Weapon_Pistol', name: 'Pistol' }]);
    expect(await ok('listEntities')).toEqual([
      { code: 'BP_Zombie_Base', name: 'Zombie', type: 'hostile' },
      { code: 'BP_Deer', name: 'Deer', type: 'neutral' },
      { code: 'BP_Trader', name: 'Trader', type: 'friendly' },
    ]);
    expect(await ok('listLocations', '{}')).toEqual([
      { code: 'vein_town_square', name: 'Town Square', position: { x: 0, y: 100, z: 0 }, radius: 50 },
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
    await ok('sendMessage', { message: 'Welcome', opts: { recipient: { gameId: MOCK_STEAMID }, senderNameOverride: 'Takaro' } });
    expect(mock.lastRequest('POST', '/message')?.body).toEqual({ text: 'Welcome', recipientGameId: MOCK_STEAMID, senderName: 'Takaro' });
    expect((await call('sendMessage', {})).error).toMatch(/'message'/);
  });

  it('teleportPlayer sends x/y/z (and optional yaw / named target)', async () => {
    await ok('teleportPlayer', { player: { gameId: MOCK_STEAMID }, x: 1, y: '2', z: 3.5 });
    expect(mock.lastRequest('POST', '/teleport')?.body).toEqual({ gameId: MOCK_STEAMID, x: 1, y: 2, z: 3.5 });
    await ok('teleportPlayer', { gameId: MOCK_STEAMID, x: 1, y: 2, z: 3, yaw: 90 });
    expect(mock.lastRequest('POST', '/teleport')?.body).toEqual({ gameId: MOCK_STEAMID, x: 1, y: 2, z: 3, yaw: 90 });
    await ok('teleportPlayer', { gameId: MOCK_STEAMID, target: 'vein_town_square' });
    expect(mock.lastRequest('POST', '/teleport')?.body).toEqual({ gameId: MOCK_STEAMID, x: 0, y: 0, z: 0, target: 'vein_town_square' });
    expect((await call('teleportPlayer', { gameId: MOCK_STEAMID, x: 1 })).error).toMatch(/numeric/);
  });

  it('kickPlayer', async () => {
    await ok('kickPlayer', { player: { gameId: MOCK_STEAMID }, reason: 'afk' });
    expect(mock.lastRequest('POST', '/kick')?.body).toEqual({ gameId: MOCK_STEAMID, reason: 'afk' });
  });

  it('banPlayer / listBans / unbanPlayer round-trip (incl. an offline id)', async () => {
    await ok('banPlayer', { player: { gameId: MOCK_STEAMID }, reason: 'grief', expiresAt: '2030-01-01T00:00:00.000Z' });
    expect(mock.lastRequest('POST', '/ban')?.body).toEqual({ gameId: MOCK_STEAMID, reason: 'grief' });
    await ok('banPlayer', { gameId: '76561198009999999' });
    expect(mock.lastRequest('POST', '/ban')?.body).toEqual({ gameId: '76561198009999999' });
    expect(await ok('listBans')).toEqual([
      { player: HENDRIK_BAN, reason: 'grief', expiresAt: '2030-01-01T00:00:00.000Z' },
      {
        player: { gameId: '76561198009999999', name: '76561198009999999', steamId: '76561198009999999', platformId: 'steam:76561198009999999' },
        reason: '',
        expiresAt: null,
      },
    ]);
    await ok('unbanPlayer', { gameId: '76561198009999999' });
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
    const t = await call('teleportPlayer', { gameId: MOCK_STEAMID, x: 1, y: 2, z: 3 });
    expect(t.type).toBe('response');
    expect(t.error).toMatch(/cannot perform 'teleportPlayer'.*not implemented \/teleport \(HTTP 501\)/);
    expect((await call('getPlayerInventory', { gameId: MOCK_STEAMID })).error).toMatch(/HTTP 501/);
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

const HENDRIK_BAN = { gameId: MOCK_STEAMID, name: 'Tester', steamId: MOCK_STEAMID, platformId: `steam:${MOCK_STEAMID}` };
