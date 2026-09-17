import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { VeinAdapter } from '../vein/adapter.js';
import { FileBanStore, MemoryBanStore } from '../vein/banStore.js';
import { VeinPluginClient } from '../vein/pluginClient.js';
import { MockPlugin, MOCK_STEAMID } from '../testing/mockPlugin.js';

/**
 * Takaro never sends `unbanPlayer` for a timed ban: the connector must lift it itself when the expiry passes
 * (see context/takaro/protocol-notes.md). The game's ban list has no expiry field, so we persist one.
 */
let mock: MockPlugin;
let store: MemoryBanStore;
let adapter: VeinAdapter;

const ban = (args: Record<string, unknown>) => adapter.handleAction('banPlayer', args);
const listBans = () => adapter.handleAction('listBans', {}) as Promise<any[]>;

beforeEach(async () => {
  mock = new MockPlugin();
  await mock.start();
  store = new MemoryBanStore();
  adapter = new VeinAdapter(new VeinPluginClient({ baseUrl: mock.url(), token: mock.token }), {
    banStore: store,
    banSweepIntervalMs: 1000,
  });
});
afterEach(async () => {
  adapter.stop();
  vi.useRealTimers();
  await mock.stop();
});

describe('timed bans', () => {
  it('persists the expiry, reports it in listBans, and lifts it via /unban when it passes', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-16T12:00:00.000Z'));
    await ban({ gameId: MOCK_STEAMID, reason: 'timeout', expiresAt: '2026-09-16T12:10:00.000Z' });
    expect(store.bans).toEqual([{ gameId: MOCK_STEAMID, expiresAt: '2026-09-16T12:10:00.000Z', reason: 'timeout' }]);
    expect(mock.bans).toHaveLength(1);

    const listed = await listBans();
    expect(listed).toHaveLength(1);
    expect(listed[0]).toMatchObject({ reason: 'timeout', expiresAt: '2026-09-16T12:10:00.000Z' });
    expect(listed[0].player.gameId).toBe(MOCK_STEAMID);

    // not due yet
    expect(await adapter.sweepExpiredBans()).toEqual([]);
    expect(mock.bans).toHaveLength(1);

    vi.setSystemTime(new Date('2026-09-16T12:10:01.000Z'));
    expect(await adapter.sweepExpiredBans()).toEqual([MOCK_STEAMID]);
    expect(mock.lastRequest('POST', '/unban')?.body).toEqual({ gameId: MOCK_STEAMID });
    expect(mock.bans).toEqual([]);
    expect(store.bans).toEqual([]);
    expect(await listBans()).toEqual([]);
  });

  it('the sweep timer lifts the ban without anyone calling listBans', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-16T12:00:00.000Z'));
    await ban({ gameId: MOCK_STEAMID, expiresAt: '2026-09-16T12:00:00.500Z' });
    adapter.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(mock.bans).toHaveLength(1);
    vi.setSystemTime(new Date('2026-09-16T12:00:02.000Z'));
    for (let i = 0; i < 50 && adapter.pendingBans().length; i++) await vi.advanceTimersByTimeAsync(1000);
    expect(mock.bans).toEqual([]);
    expect(adapter.pendingBans()).toEqual([]);
  });

  it('keeps the ban pending and retries when the plugin is unreachable at expiry', async () => {
    await ban({ gameId: MOCK_STEAMID, expiresAt: new Date(Date.now() - 1000).toISOString() });
    await mock.stop();
    expect(await adapter.sweepExpiredBans()).toEqual([]);
    expect(adapter.pendingBans()).toHaveLength(1);
    await mock.start();
    // a new client is needed because the mock got a new port
    const retry = new VeinAdapter(new VeinPluginClient({ baseUrl: mock.url(), token: mock.token }), { banStore: store });
    expect(await retry.sweepExpiredBans()).toEqual([MOCK_STEAMID]);
    expect(store.bans).toEqual([]);
  });

  it('survives a sidecar restart through the on-disk store and merges with the plugin ban list', async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dw-bans-'));
    try {
      const file = path.join(dir, 'timed-bans.json');
      const a1 = new VeinAdapter(new VeinPluginClient({ baseUrl: mock.url(), token: mock.token }), { banStore: new FileBanStore(file) });
      await a1.handleAction('banPlayer', { gameId: MOCK_STEAMID, expiresAt: '2030-01-01T00:00:00.000Z' });
      expect(new FileBanStore(file).load()).toEqual([{ gameId: MOCK_STEAMID, expiresAt: '2030-01-01T00:00:00.000Z' }]);

      const a2 = new VeinAdapter(new VeinPluginClient({ baseUrl: mock.url(), token: mock.token }), { banStore: new FileBanStore(file) });
      const listed = (await a2.handleAction('listBans', {})) as any[];
      expect(listed).toHaveLength(1);
      expect(listed[0].expiresAt).toBe('2030-01-01T00:00:00.000Z');

      // an explicit unbanPlayer also clears the pending expiry
      await a2.handleAction('unbanPlayer', { gameId: MOCK_STEAMID });
      expect(new FileBanStore(file).load()).toEqual([]);
      expect(await a2.handleAction('listBans', {})).toEqual([]);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  // F16 (L6d): a `listBans` served right after a sidecar restart reported a live timed ban as PERMANENT, and Takaro
  // imported it as an unmanaged `until: null` ban row that survived the connector's own lift.
  it('F16: the very first listBans after a restart reports the real expiry, read from disk', async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vein-bans-f16-'));
    try {
      const file = path.join(dir, 'timed-bans.json');
      const client = () => new VeinPluginClient({ baseUrl: mock.url(), token: mock.token });
      const before = new VeinAdapter(client(), { banStore: new FileBanStore(file) });
      await before.handleAction('banPlayer', { gameId: MOCK_STEAMID, reason: 'cooldown', expiresAt: '2030-01-01T00:00:00.000Z' });
      // The game's own ban list has no expiry field at all — exactly what the plugin returns on the rig.
      expect(mock.bans[0]).toMatchObject({ gameId: MOCK_STEAMID });
      expect((mock.bans[0] as any).expiresAt ?? null).toBeNull();

      // "restart": a brand-new adapter, and listBans is the FIRST thing it is asked.
      const after = new VeinAdapter(client(), { banStore: new FileBanStore(file) });
      const listed = (await after.handleAction('listBans', {})) as any[];
      expect(listed).toHaveLength(1);
      expect(listed[0].expiresAt).toBe('2030-01-01T00:00:00.000Z');
      expect(listed[0].expiresAt).not.toBeNull();

      // A ban written to the store by another process after we loaded it is still reported with its expiry:
      // listBans re-reads the store, it does not trust its in-memory copy.
      new FileBanStore(file).save([{ gameId: '76561198000000001', expiresAt: '2031-02-03T04:05:06.000Z', reason: 'other' }]);
      mock.bans = [{ gameId: '76561198000000001', name: '', reason: 'other', expiresAt: null } as any];
      const relisted = (await after.handleAction('listBans', {})) as any[];
      expect(relisted).toHaveLength(1);
      expect(relisted[0].expiresAt).toBe('2031-02-03T04:05:06.000Z');

      // After the expiry the sidecar lifts the ban and listBans omits it entirely: nothing more the connector can do.
      new FileBanStore(file).save([{ gameId: '76561198000000001', expiresAt: '2000-01-01T00:00:00.000Z' }]);
      const lifted = new VeinAdapter(client(), { banStore: new FileBanStore(file) });
      expect(await lifted.sweepExpiredBans()).toEqual(['76561198000000001']);
      expect(mock.bans).toEqual([]);
      expect(await lifted.handleAction('listBans', {})).toEqual([]);
      expect(new FileBanStore(file).load()).toEqual([]);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it('F16: listBans FAILS rather than reporting a timed ban as permanent when the store cannot be read', async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vein-bans-cold-'));
    try {
      const file = path.join(dir, 'timed-bans.json');
      fs.writeFileSync(file, '{ this is not json');
      const cold = new VeinAdapter(new VeinPluginClient({ baseUrl: mock.url(), token: mock.token }), { banStore: new FileBanStore(file) });
      expect(cold.banStoreUnavailable()).toBe(true);
      await ban({ gameId: MOCK_STEAMID, reason: 'perm' }); // a real ban exists game-side
      await expect(cold.handleAction('listBans', {})).rejects.toThrow(/timed-ban store/i);
      // A missing store file is the normal first-boot state and is NOT an error.
      fs.rmSync(file);
      const fresh = new VeinAdapter(new VeinPluginClient({ baseUrl: mock.url(), token: mock.token }), { banStore: new FileBanStore(file) });
      expect(fresh.banStoreUnavailable()).toBe(false);
      expect(await fresh.handleAction('listBans', {})).toHaveLength(1);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it('reports a pending timed ban the plugin no longer lists', async () => {
    await ban({ gameId: MOCK_STEAMID, reason: 'cooldown', expiresAt: '2030-01-01T00:00:00.000Z' });
    mock.bans = []; // the game dropped it (ini rewrite, restart)
    expect(await listBans()).toEqual([
      { player: { gameId: MOCK_STEAMID, name: MOCK_STEAMID, steamId: MOCK_STEAMID, platformId: `steam:${MOCK_STEAMID}` }, reason: 'cooldown', expiresAt: '2030-01-01T00:00:00.000Z' },
    ]);
  });

  it('a permanent ban has no pending entry and lists expiresAt null', async () => {
    await ban({ gameId: MOCK_STEAMID, reason: 'cheating' });
    expect(adapter.pendingBans()).toEqual([]);
    expect(await listBans()).toEqual([
      expect.objectContaining({ reason: 'cheating', expiresAt: null }),
    ]);
  });
});
