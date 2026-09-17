import { describe, expect, it } from 'vitest';
import { aggregateInventory, isSteamId64, mapBan, mapEntityType, mapInventoryItem, mapPlayer, mapPluginEvent, num, str } from '../vein/mapping.js';

const STEAM = '76561198000000001';

describe('Vein identity mapping (Decision 3: gameId === SteamID64)', () => {
  it('gameId is the SteamID64, steamId is the same value and platformId is steam:<id64>', () => {
    expect(mapPlayer({ gameId: STEAM, name: 'Tester' })).toEqual({
      gameId: STEAM,
      name: 'Tester',
      steamId: STEAM,
      platformId: `steam:${STEAM}`,
    });
  });

  it('never emits EOS/Epic fields', () => {
    const p = mapPlayer({ gameId: STEAM, name: 'Tester', epicOnlineServicesId: 'deadbeef'.repeat(4) });
    expect(p).not.toHaveProperty('epicOnlineServicesId');
    expect(p.platformId).toBe(`steam:${STEAM}`);
  });

  it('accepts the id from steamId or from a steam: platformId', () => {
    expect(mapPlayer({ steamId: STEAM, name: 'A' }).gameId).toBe(STEAM);
    expect(mapPlayer({ platformId: `steam:${STEAM}`, name: 'A' }).gameId).toBe(STEAM);
    expect(mapPlayer({ platformId: `STEAM:${STEAM}`, name: 'A' }).platformId).toBe(`steam:${STEAM}`);
  });

  it('name comes from the plugin, falling back to characterName then the id', () => {
    expect(mapPlayer({ gameId: STEAM, name: 'Tester', characterName: 'Other' }).name).toBe('Tester');
    expect(mapPlayer({ gameId: STEAM, characterName: 'Other' }).name).toBe('Other');
    expect(mapPlayer({ gameId: STEAM }).name).toBe(STEAM);
  });

  it('carries ping and ip, and rejects a player with no identifier at all', () => {
    expect(mapPlayer({ gameId: STEAM, ping: 42, ip: '10.0.0.2' })).toMatchObject({ ping: 42, ip: '10.0.0.2' });
    expect(mapPlayer({ gameId: STEAM, ping: 'nope' as unknown as number })).not.toHaveProperty('ping');
    expect(() => mapPlayer({})).toThrow(/no identifier/);
  });

  it('a non-Steam id is still usable as a gameId (log line with a name only)', () => {
    expect(mapPlayer({ gameId: 'Tester' })).toEqual({ gameId: 'Tester', name: 'Tester' });
    expect(mapPlayer({ name: 'Tester' })).toEqual({ gameId: 'Tester', name: 'Tester' });
  });

  it('isSteamId64 recognises 17-digit 7656… ids only', () => {
    expect(isSteamId64(STEAM)).toBe(true);
    expect(isSteamId64('0123456789abcdef0123456789abcdef')).toBe(false);
    expect(isSteamId64('7656119')).toBe(false);
  });

  it('bans map the plugin ban record onto a Takaro player (flat and nested)', () => {
    expect(mapBan({ gameId: STEAM, name: 'Tester', reason: 'grief', expiresAt: null })).toEqual({
      player: { gameId: STEAM, name: 'Tester', steamId: STEAM, platformId: `steam:${STEAM}` },
      reason: 'grief',
      expiresAt: null,
    });
    expect(mapBan({ player: { gameId: STEAM }, expiresAt: 1893456000000 }).expiresAt).toBe(new Date(1893456000000).toISOString());
    expect(mapBan({ gameId: STEAM, reason: null, expiresAt: null }).reason).toBe('');
  });

  it('entity type heuristics cover the Vein bestiary', () => {
    expect(mapEntityType('hostile')).toBe('hostile');
    expect(mapEntityType('BP_Zombie_Base')).toBe('hostile');
    expect(mapEntityType('npc')).toBe('friendly');
    expect(mapEntityType('BP_Trader')).toBe('friendly');
    expect(mapEntityType('animal')).toBe('neutral');
    expect(mapEntityType(null)).toBe('neutral');
  });

  it('str/num coercion helpers', () => {
    expect(str('  x ')).toBe('x');
    expect(str('')).toBeNull();
    expect(str(null)).toBeNull();
    expect(str(7)).toBe('7');
    expect(num('2.5')).toBe(2.5);
    expect(num(null)).toBeNull();
    expect(num('abc')).toBeNull();
  });
});

// ---- lane L3e: the chat channel is the EChatSegment the RPC carried, not a constant -------------
describe('chat channel mapping (lane L3e, L6b finding 3)', () => {
  const chat = (channel: unknown) =>
    (mapPluginEvent({ type: 'chat-message', data: { msg: 'x', channel } })?.data as { channel: string }).channel;

  it('keeps global as global', () => {
    expect(chat('global')).toBe('global');
    expect(chat('all')).toBe('global');
  });

  it('maps the proximity segments to a non-global channel so onlyGlobalChat excludes them', () => {
    // The whole point of the fix: local chat must not be relayed as if it were server-wide.
    expect(chat('local')).toBe('team');
    expect(chat('radio')).toBe('team');
    expect(chat('local')).not.toBe('global');
  });

  it('passes the Takaro channels through and defaults anything unknown to global', () => {
    expect(chat('whisper')).toBe('whisper');
    expect(chat('team')).toBe('team');
    expect(chat('friends')).toBe('friends');
    expect(chat(undefined)).toBe('global');
    expect(chat('nonsense')).toBe('global');
  });
});

describe('pseudo-stackable inventory aggregation (F22)', () => {
  const agg = (raw: unknown[]) => aggregateInventory(raw.map(mapInventoryItem));

  it('collapses four one-unit MRE instances into a single row of amount 4', () => {
    // VEIN's BP_MRE_C is bStackable=false / bPseudoStackable=true: one FVirtualItemInstance per
    // unit, so the plugin reports four entries of amount 1. The game UI shows "MRE x 4".
    expect(agg([
      { code: 'BP_MRE_C', name: 'MRE', amount: 1 },
      { code: 'BP_MRE_C', name: 'MRE', amount: 1 },
      { code: 'BP_MRE_C', name: 'MRE', amount: 1 },
      { code: 'BP_MRE_C', name: 'MRE', amount: 1 },
    ])).toEqual([{ code: 'BP_MRE_C', name: 'MRE', amount: 4 }]);
  });

  it('mixes real stackables with pseudo-stackables and keeps first-appearance order', () => {
    expect(agg([
      { code: 'BP_MRE_C', name: 'MRE', amount: 1 },
      { code: 'BP_Ammo_9mm_C', name: '9mm Ammo', amount: 30 },
      { code: 'BP_MRE_C', name: 'MRE', amount: 1 },
      { code: 'BP_Corn_C', name: 'Corn', amount: 1 },
      { code: 'BP_Ammo_9mm_C', name: '9mm Ammo', amount: 12 },
      { code: 'BP_Corn_C', name: 'Corn', amount: 1 },
      { code: 'BP_Corn_C', name: 'Corn', amount: 1 },
    ])).toEqual([
      { code: 'BP_MRE_C', name: 'MRE', amount: 2 },
      { code: 'BP_Ammo_9mm_C', name: '9mm Ammo', amount: 42 },
      { code: 'BP_Corn_C', name: 'Corn', amount: 3 },
    ]);
  });

  it('keeps different qualities of the same code apart and defaults a missing amount to 1', () => {
    expect(agg([
      { code: 'BP_Axe_C', name: 'Axe', quality: '2' },
      { code: 'BP_Axe_C', name: 'Axe' },
      { code: 'BP_Axe_C', name: 'Axe', quality: '2' },
    ])).toEqual([
      { code: 'BP_Axe_C', name: 'Axe', amount: 2, quality: '2' },
      { code: 'BP_Axe_C', name: 'Axe', amount: 1 },
    ]);
  });

  it('is a no-op for an empty inventory and for one already-unique row', () => {
    expect(agg([])).toEqual([]);
    expect(agg([{ code: 'BP_MRE_C', name: 'MRE', amount: 1 }])).toEqual([{ code: 'BP_MRE_C', name: 'MRE', amount: 1 }]);
  });

  it('does not mutate the mapped input rows', () => {
    const rows = [mapInventoryItem({ code: 'BP_MRE_C', name: 'MRE', amount: 1 }), mapInventoryItem({ code: 'BP_MRE_C', name: 'MRE', amount: 1 })];
    aggregateInventory(rows);
    expect(rows.map((r) => r.amount)).toEqual([1, 1]);
  });
});
