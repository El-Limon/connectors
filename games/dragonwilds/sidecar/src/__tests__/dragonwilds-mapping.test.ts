import { describe, expect, it } from 'vitest';
import { isPuid, mapBan, mapEntityType, mapPlayer, num, str } from '../dragonwilds/mapping.js';

const PUID = '0123456789abcdef0123456789abcdef';
const STEAM = '76561198000000001';

describe('Dragonwilds identity mapping', () => {
  it('gameId is the EOS ProductUserId and platformId is epic:<puid>', () => {
    expect(mapPlayer({ gameId: PUID, name: 'Hendrik', characterName: 'Limon' })).toEqual({
      gameId: PUID,
      name: 'Limon',
      epicOnlineServicesId: PUID,
      platformId: `epic:${PUID}`,
    });
  });

  it('name falls back to the platform display name, then to the id', () => {
    expect(mapPlayer({ gameId: PUID, name: 'Hendrik' }).name).toBe('Hendrik');
    expect(mapPlayer({ gameId: PUID }).name).toBe(PUID);
  });

  it('keeps the SteamID64 alongside the EOS id without changing gameId', () => {
    const p = mapPlayer({ gameId: PUID, characterName: 'Limon', steamId: STEAM });
    expect(p).toMatchObject({ gameId: PUID, steamId: STEAM, epicOnlineServicesId: PUID, platformId: `epic:${PUID}` });
  });

  it('accepts the EOS id from epicOnlineServicesId, platformId or productUserId', () => {
    expect(mapPlayer({ epicOnlineServicesId: PUID, name: 'A' }).gameId).toBe(PUID);
    expect(mapPlayer({ platformId: `epic:${PUID}`, name: 'A' }).gameId).toBe(PUID);
    expect(mapPlayer({ productUserId: PUID, name: 'A' }).gameId).toBe(PUID);
    expect(mapPlayer({ gameId: PUID.toUpperCase(), name: 'A' }).gameId).toBe(PUID); // normalised to lower case
  });

  it('falls back to steam:<id64> when the plugin reports Steam as the primary platform', () => {
    expect(mapPlayer({ gameId: STEAM, name: 'SteamOnly' })).toEqual({
      gameId: STEAM,
      name: 'SteamOnly',
      steamId: STEAM,
      platformId: `steam:${STEAM}`,
    });
    expect(mapPlayer({ platformId: `steam:${STEAM}`, name: 'SteamOnly' }).platformId).toBe(`steam:${STEAM}`);
  });

  it('carries ping and ip, and rejects a player with no identifier at all', () => {
    expect(mapPlayer({ gameId: PUID, ping: 42, ip: '10.0.0.2' })).toMatchObject({ ping: 42, ip: '10.0.0.2' });
    expect(mapPlayer({ gameId: PUID, ping: 'nope' as unknown as number })).not.toHaveProperty('ping');
    expect(() => mapPlayer({})).toThrow(/no identifier/);
  });

  it('a non-PUID, non-Steam id is still usable as a gameId (degraded plugin build)', () => {
    expect(mapPlayer({ gameId: 'Player_0', name: 'X' })).toEqual({ gameId: 'Player_0', name: 'X' });
  });

  it('isPuid recognises 32-hex ids only', () => {
    expect(isPuid(PUID)).toBe(true);
    expect(isPuid(PUID.toUpperCase())).toBe(true);
    expect(isPuid(STEAM)).toBe(false);
    expect(isPuid('zz0204f6f3a444a99a44c995d80ff2a7')).toBe(false);
  });

  it('bans map the plugin ban record onto a Takaro player', () => {
    expect(mapBan({ gameId: PUID, characterName: 'Limon', steamId: STEAM, reason: 'grief', expiresAt: null })).toEqual({
      player: { gameId: PUID, name: 'Limon', epicOnlineServicesId: PUID, steamId: STEAM, platformId: `epic:${PUID}` },
      reason: 'grief',
      expiresAt: null,
    });
    expect(mapBan({ player: { gameId: PUID }, expiresAt: 1893456000000 }).expiresAt).toBe(new Date(1893456000000).toISOString());
  });

  it('entity type heuristics cover the Dragonwilds bestiary', () => {
    expect(mapEntityType('hostile')).toBe('hostile');
    expect(mapEntityType('AI_Goblin_Melee')).toBe('hostile');
    expect(mapEntityType('npc')).toBe('friendly');
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
