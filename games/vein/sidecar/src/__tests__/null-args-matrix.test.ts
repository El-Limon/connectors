import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { MemoryBanStore } from '../vein/banStore.js';
import { VeinAdapter, expiryOf, playerId } from '../vein/adapter.js';
import { VeinPluginClient } from '../vein/pluginClient.js';
import { MockPlugin, MOCK_STEAMID } from '../testing/mockPlugin.js';

/**
 * Takaro modules send explicit JSON `null` for optional arguments (see context/takaro/protocol-notes.md):
 * `dimension`, `reason`, `expiresAt`, `quality`, `opts`, `opts.recipient` all arrive as null, and sometimes with a
 * wrong type entirely. None of these may throw.
 */
let mock: MockPlugin;
let adapter: VeinAdapter;

beforeEach(async () => {
  mock = new MockPlugin();
  await mock.start();
  adapter = new VeinAdapter(new VeinPluginClient({ baseUrl: mock.url(), token: mock.token }), {
    banStore: new MemoryBanStore(),
  });
});
afterEach(async () => {
  adapter.stop();
  await mock.stop();
});

const NULLISH = [undefined, null, '', 0, false, [], {}] as const;
/** What `str()` makes of a nullish/wrong-typed value: numbers stringify, everything else disappears. */
const optional = (v: unknown): string | undefined =>
  typeof v === 'number' && Number.isFinite(v) ? String(v) : typeof v === 'string' && v.trim() ? v.trim() : undefined;

describe('explicit-null / wrong-type argument matrix', () => {
  it('teleportPlayer tolerates dimension absent, null or wrong-typed', async () => {
    for (const dimension of NULLISH) {
      await adapter.handleAction('teleportPlayer', { gameId: MOCK_STEAMID, x: 1, y: 2, z: 3, dimension });
      expect(mock.lastRequest('POST', '/teleport')?.body).toEqual({ gameId: MOCK_STEAMID, x: 1, y: 2, z: 3 });
    }
    // yaw / target nullish are simply omitted
    await adapter.handleAction('teleportPlayer', { gameId: MOCK_STEAMID, x: 1, y: 2, z: 3, yaw: null, target: null });
    expect(mock.lastRequest('POST', '/teleport')?.body).toEqual({ gameId: MOCK_STEAMID, x: 1, y: 2, z: 3 });
    // coordinates as numeric strings still work; a missing coordinate is a clean error
    await adapter.handleAction('teleportPlayer', { gameId: MOCK_STEAMID, x: '1', y: '2', z: '3' });
    expect(mock.lastRequest('POST', '/teleport')?.body).toEqual({ gameId: MOCK_STEAMID, x: 1, y: 2, z: 3 });
    await expect(adapter.handleAction('teleportPlayer', { gameId: MOCK_STEAMID, x: null, y: null, z: null })).rejects.toThrow(/numeric/);
  });

  it('banPlayer tolerates reason and expiresAt absent, null or wrong-typed', async () => {
    for (const reason of NULLISH) {
      for (const expiresAt of NULLISH) {
        await adapter.handleAction('banPlayer', { gameId: MOCK_STEAMID, reason, expiresAt });
        const body = mock.lastRequest('POST', '/ban')?.body;
        expect(body.gameId).toBe(MOCK_STEAMID);
        expect(body).not.toHaveProperty('expiresAt'); // the plugin's ban list has no expiry field
        const want = optional(reason);
        if (want === undefined) expect(body).not.toHaveProperty('reason');
        else expect(body.reason).toBe(want);
        expect(adapter.pendingBans()).toEqual([]); // nothing nullish is a timed ban
      }
    }
    await adapter.handleAction('banPlayer', { gameId: MOCK_STEAMID, reason: 'grief', expiresAt: '2030-01-01T00:00:00.000Z' });
    expect(adapter.pendingBans()).toEqual([{ gameId: MOCK_STEAMID, expiresAt: '2030-01-01T00:00:00.000Z', reason: 'grief' }]);
    // a later permanent ban clears the pending expiry
    await adapter.handleAction('banPlayer', { gameId: MOCK_STEAMID, reason: null, expiresAt: null });
    expect(adapter.pendingBans()).toEqual([]);
  });

  it('kickPlayer tolerates a null / wrong-typed reason', async () => {
    for (const reason of NULLISH) {
      await adapter.handleAction('kickPlayer', { gameId: MOCK_STEAMID, reason });
      const body = mock.lastRequest('POST', '/kick')?.body;
      const want = optional(reason);
      if (want === undefined) expect(body).not.toHaveProperty('reason');
      else expect(body.reason).toBe(want);
      mock.players[0].online = true;
    }
  });

  it('giveItem tolerates null quality/amount and nested item objects', async () => {
    for (const quality of NULLISH) {
      await adapter.handleAction('giveItem', { gameId: MOCK_STEAMID, item: 'Item_Wood_Plank', amount: 2, quality });
      expect(mock.lastRequest('POST', '/give')?.body).toEqual({ gameId: MOCK_STEAMID, code: 'Item_Wood_Plank', amount: 2 });
    }
    await adapter.handleAction('giveItem', { gameId: MOCK_STEAMID, item: 'Item_Wood_Plank', amount: null });
    expect(mock.lastRequest('POST', '/give')?.body).toEqual({ gameId: MOCK_STEAMID, code: 'Item_Wood_Plank', amount: 1 });
    await adapter.handleAction('giveItem', { gameId: MOCK_STEAMID, item: { code: 'Item_Wood_Plank' }, amount: 3 });
    expect(mock.lastRequest('POST', '/give')?.body).toEqual({ gameId: MOCK_STEAMID, code: 'Item_Wood_Plank', amount: 3 });
  });

  it('sendMessage tolerates opts and opts.recipient absent, null or wrong-typed', async () => {
    for (const opts of NULLISH) {
      await adapter.handleAction('sendMessage', { message: 'hi', opts });
      expect(mock.lastRequest('POST', '/message')?.body).toEqual({ text: 'hi', senderName: 'Server' });
    }
    for (const recipient of NULLISH) {
      await adapter.handleAction('sendMessage', { message: 'hi', opts: { recipient, senderNameOverride: null } });
      expect(mock.lastRequest('POST', '/message')?.body).toEqual({ text: 'hi', senderName: 'Server' });
    }
    await adapter.handleAction('sendMessage', { message: 'hi', opts: { recipient: { gameId: MOCK_STEAMID } } });
    expect(mock.lastRequest('POST', '/message')?.body).toEqual({ text: 'hi', recipientGameId: MOCK_STEAMID, senderName: 'Server' });
  });

  it('player identifiers: flat, nested player, nested playerRef, platform-prefixed', () => {
    expect(playerId({ gameId: MOCK_STEAMID })).toBe(MOCK_STEAMID);
    expect(playerId({ gameId: null, player: { gameId: MOCK_STEAMID } })).toBe(MOCK_STEAMID);
    expect(playerId({ player: null, playerRef: { steamId: '76561198000000001' } })).toBe('76561198000000001');
    expect(playerId({ platformId: `steam:${MOCK_STEAMID}` })).toBe(`steam:${MOCK_STEAMID}`);
    expect(playerId({ player: { steamId: MOCK_STEAMID } })).toBe(MOCK_STEAMID);
    expect(() => playerId({ gameId: null, player: null, playerRef: null })).toThrow(/player identifier/);
  });

  it('expiryOf normalises ISO strings, epoch millis and null', () => {
    expect(expiryOf(null)).toBeNull();
    expect(expiryOf(undefined)).toBeNull();
    expect(expiryOf('')).toBeNull();
    expect(expiryOf('not a date')).toBeNull();
    expect(expiryOf('2030-01-01T00:00:00.000Z')).toBe('2030-01-01T00:00:00.000Z');
    expect(expiryOf(1893456000000)).toBe(new Date(1893456000000).toISOString());
    expect(expiryOf(0)).toBeNull();
  });
});
