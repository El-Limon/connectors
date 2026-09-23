import { describe, expect, it } from 'vitest';
import { BanManager, expiryOf, isExpired } from '../dune/bans.js';
import { MemoryBanStore, type BanStore, type PendingBan } from '../dune/banStore.js';
import type { TakaroBan, TakaroPlayer } from '../dune/types.js';
import { mockPlayer } from '../testing/mockBattlegroup.js';
import { harness } from './helpers.js';

const FLS = '6FF6498F4074E3DE';

function manager(options: { store?: BanStore; now?: () => number; kick?: (id: string) => Promise<boolean> } = {}) {
  const kicked: string[] = [];
  const store = options.store ?? new MemoryBanStore();
  const bans = new BanManager({
    store,
    kickCooldownMs: 1000,
    now: options.now,
    kick: async (gameId) => {
      kicked.push(gameId);
      return options.kick ? options.kick(gameId) : true;
    },
  });
  return { bans, kicked, store };
}

describe('connector-owned bans', () => {
  it('a permanent ban has expiresAt null on the wire, a timed one carries its ISO expiry', () => {
    const { bans } = manager();
    bans.add(FLS, 'griefing', null, { gameId: FLS, name: 'Tester' });
    expect(bans.list()).toEqual<TakaroBan[]>([{ player: { gameId: FLS, name: 'Tester' }, reason: 'griefing', expiresAt: null }]);

    bans.add(FLS, null, '2030-01-01T00:00:00.000Z');
    expect(bans.list()[0]).toMatchObject({ reason: '', expiresAt: '2030-01-01T00:00:00.000Z' });
  });

  it('listBans excludes an expired ban — a stale row makes Takaro\'s next banCreate 409', () => {
    let now = Date.parse('2026-01-01T00:00:00Z');
    const { bans } = manager({ now: () => now });
    bans.add(FLS, 'timeout', '2026-01-01T00:10:00.000Z');
    expect(bans.list()).toHaveLength(1);

    now = Date.parse('2026-01-01T00:11:00Z');
    expect(bans.list()).toEqual([]);
    // And the lift is persisted, not just filtered on read.
    expect(bans.all()).toEqual([]);
  });

  it('a timed ban is lifted by the connector — Takaro never sends unbanPlayer for one', () => {
    let now = 1000;
    const { bans } = manager({ now: () => now });
    bans.add(FLS, null, new Date(2000).toISOString());
    expect(bans.sweep()).toEqual([]);
    now = 2001;
    expect(bans.sweep()).toEqual([FLS]);
    expect(bans.isBanned(FLS)).toBeUndefined();
  });

  it('enforcement kicks a banned player on sight, honours a cooldown, and stops at the expiry', async () => {
    let now = 0;
    const { bans, kicked } = manager({ now: () => now });
    bans.add(FLS, 'banned', new Date(10_000).toISOString());
    const online: TakaroPlayer[] = [{ gameId: FLS, name: 'Tester' }];

    await bans.enforce(online);
    expect(kicked).toEqual([FLS]);
    // Inside the cooldown: no kick storm while the first one is still in flight.
    now = 500;
    await bans.enforce(online);
    expect(kicked).toEqual([FLS]);
    now = 2000;
    await bans.enforce(online);
    expect(kicked).toEqual([FLS, FLS]);
    // Expired: enforcement stops.
    now = 11_000;
    await bans.enforce(online);
    expect(kicked).toHaveLength(2);
  });

  it('a failing kick is retried rather than swallowed', async () => {
    let now = 0;
    const { bans, kicked } = manager({ now: () => now, kick: async () => false });
    bans.add(FLS, null, null);
    await bans.enforce([{ gameId: FLS, name: 'H' }]);
    now = 2000;
    await bans.enforce([{ gameId: FLS, name: 'H' }]);
    expect(kicked).toEqual([FLS, FLS]);
  });

  it('an unreadable ban store makes listBans FAIL rather than report a timed ban as permanent', () => {
    const broken: BanStore = {
      load(): PendingBan[] {
        throw new Error('bans.json is corrupt');
      },
      save() {},
    };
    const { bans } = manager({ store: broken });
    expect(bans.unavailable()).toBe(true);
    expect(() => bans.list()).toThrow(/could not be read/);
  });

  it('expiryOf accepts an ISO string, an epoch, and Takaro\'s explicit null / zero', () => {
    expect(expiryOf('2030-01-01T00:00:00Z')).toBe('2030-01-01T00:00:00.000Z');
    expect(expiryOf(1_700_000_000_000)).toBe('2023-11-14T22:13:20.000Z');
    expect(expiryOf(null)).toBeNull();
    expect(expiryOf(undefined)).toBeNull();
    expect(expiryOf(0)).toBeNull();
    expect(expiryOf('')).toBeNull();
    expect(expiryOf('not a date')).toBeNull();
    expect(isExpired({ expiresAt: '' }, Date.now())).toBe(false);
  });
});

describe('ban actions end to end', () => {
  it('banPlayer stores the ban AND kicks immediately; unban lifts it', async () => {
    const h = await harness();
    setTimeout(() => h.battlegroup.setOnline(FLS, false), 10);
    const result = (await h.adapter.handleAction('banPlayer', { gameId: FLS, reason: 'testing', expiresAt: null })) as Record<string, unknown>;
    expect(result).toMatchObject({ verified: true, enforcement: 'kick-on-sight', kicked: true, expiresAt: null });
    expect(h.gmSent().at(-1)).toEqual({ ServerCommand: 'KickPlayer', PlayerId: FLS });

    const listed = (await h.adapter.handleAction('listBans', {})) as TakaroBan[];
    expect(listed[0]).toMatchObject({ player: { gameId: FLS, name: 'Tester' }, reason: 'testing', expiresAt: null });

    expect(await h.adapter.handleAction('unbanPlayer', { gameId: FLS })).toEqual({ verified: true, removed: true });
    expect(await h.adapter.handleAction('listBans', {})).toEqual([]);
  });

  it('banning an offline player still records a usable IGamePlayer', async () => {
    const h = await harness({ players: [mockPlayer({ onlineStatus: 'Offline' })] });
    await h.adapter.handleAction('banPlayer', { gameId: FLS, reason: null, expiresAt: null });
    const listed = (await h.adapter.handleAction('listBans', {})) as TakaroBan[];
    expect(listed[0].player).toMatchObject({ gameId: FLS, name: 'Tester' });
    expect(listed[0].reason).toBe('');
  });

  it('banning a player the connector has never seen does not lose the ban', async () => {
    const h = await harness({ players: [] });
    await h.adapter.handleAction('banPlayer', { gameId: 'steam:76561198000000009', reason: 'preemptive', expiresAt: null });
    const listed = (await h.adapter.handleAction('listBans', {})) as TakaroBan[];
    expect(listed[0].player.gameId).toBe('76561198000000009');
  });
});
