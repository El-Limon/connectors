import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DragonwildsAdapter } from '../dragonwilds/adapter.js';
import { FileBanStore, MemoryBanStore } from '../dragonwilds/banStore.js';
import { DragonwildsPluginClient } from '../dragonwilds/pluginClient.js';
import { MockPlugin, MOCK_PUID } from '../testing/mockPlugin.js';

/**
 * Takaro never sends `unbanPlayer` for a timed ban: the connector must lift it itself when the expiry passes
 * (see context/takaro/protocol-notes.md). The game's ban list has no expiry field, so we persist one.
 */
let mock: MockPlugin;
let store: MemoryBanStore;
let adapter: DragonwildsAdapter;

const ban = (args: Record<string, unknown>) => adapter.handleAction('banPlayer', args);
const listBans = () => adapter.handleAction('listBans', {}) as Promise<any[]>;

beforeEach(async () => {
  mock = new MockPlugin();
  await mock.start();
  store = new MemoryBanStore();
  adapter = new DragonwildsAdapter(new DragonwildsPluginClient({ baseUrl: mock.url(), token: mock.token }), {
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
    await ban({ gameId: MOCK_PUID, reason: 'timeout', expiresAt: '2026-09-16T12:10:00.000Z' });
    expect(store.bans).toEqual([{ gameId: MOCK_PUID, expiresAt: '2026-09-16T12:10:00.000Z', reason: 'timeout' }]);
    expect(mock.bans).toHaveLength(1);

    const listed = await listBans();
    expect(listed).toHaveLength(1);
    expect(listed[0]).toMatchObject({ reason: 'timeout', expiresAt: '2026-09-16T12:10:00.000Z' });
    expect(listed[0].player.gameId).toBe(MOCK_PUID);

    // not due yet
    expect(await adapter.sweepExpiredBans()).toEqual([]);
    expect(mock.bans).toHaveLength(1);

    vi.setSystemTime(new Date('2026-09-16T12:10:01.000Z'));
    expect(await adapter.sweepExpiredBans()).toEqual([MOCK_PUID]);
    expect(mock.lastRequest('POST', '/unban')?.body).toEqual({ gameId: MOCK_PUID });
    expect(mock.bans).toEqual([]);
    expect(store.bans).toEqual([]);
    expect(await listBans()).toEqual([]);
  });

  it('the sweep timer lifts the ban without anyone calling listBans', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-16T12:00:00.000Z'));
    await ban({ gameId: MOCK_PUID, expiresAt: '2026-09-16T12:00:00.500Z' });
    adapter.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(mock.bans).toHaveLength(1);
    vi.setSystemTime(new Date('2026-09-16T12:00:02.000Z'));
    for (let i = 0; i < 50 && adapter.pendingBans().length; i++) await vi.advanceTimersByTimeAsync(1000);
    expect(mock.bans).toEqual([]);
    expect(adapter.pendingBans()).toEqual([]);
  });

  it('keeps the ban pending and retries when the plugin is unreachable at expiry', async () => {
    await ban({ gameId: MOCK_PUID, expiresAt: new Date(Date.now() - 1000).toISOString() });
    await mock.stop();
    expect(await adapter.sweepExpiredBans()).toEqual([]);
    expect(adapter.pendingBans()).toHaveLength(1);
    await mock.start();
    // a new client is needed because the mock got a new port
    const retry = new DragonwildsAdapter(new DragonwildsPluginClient({ baseUrl: mock.url(), token: mock.token }), { banStore: store });
    expect(await retry.sweepExpiredBans()).toEqual([MOCK_PUID]);
    expect(store.bans).toEqual([]);
  });

  it('survives a sidecar restart through the on-disk store and merges with the plugin ban list', async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dw-bans-'));
    try {
      const file = path.join(dir, 'timed-bans.json');
      const a1 = new DragonwildsAdapter(new DragonwildsPluginClient({ baseUrl: mock.url(), token: mock.token }), { banStore: new FileBanStore(file) });
      await a1.handleAction('banPlayer', { gameId: MOCK_PUID, expiresAt: '2030-01-01T00:00:00.000Z' });
      expect(new FileBanStore(file).load()).toEqual([{ gameId: MOCK_PUID, expiresAt: '2030-01-01T00:00:00.000Z' }]);

      const a2 = new DragonwildsAdapter(new DragonwildsPluginClient({ baseUrl: mock.url(), token: mock.token }), { banStore: new FileBanStore(file) });
      const listed = (await a2.handleAction('listBans', {})) as any[];
      expect(listed).toHaveLength(1);
      expect(listed[0].expiresAt).toBe('2030-01-01T00:00:00.000Z');

      // an explicit unbanPlayer also clears the pending expiry
      await a2.handleAction('unbanPlayer', { gameId: MOCK_PUID });
      expect(new FileBanStore(file).load()).toEqual([]);
      expect(await a2.handleAction('listBans', {})).toEqual([]);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it('reports a pending timed ban the plugin no longer lists', async () => {
    await ban({ gameId: MOCK_PUID, reason: 'cooldown', expiresAt: '2030-01-01T00:00:00.000Z' });
    mock.bans = []; // the game dropped it (ini rewrite, restart)
    expect(await listBans()).toEqual([
      { player: { gameId: MOCK_PUID, name: MOCK_PUID, epicOnlineServicesId: MOCK_PUID, platformId: `epic:${MOCK_PUID}` }, reason: 'cooldown', expiresAt: '2030-01-01T00:00:00.000Z' },
    ]);
  });

  it('a permanent ban has no pending entry and lists expiresAt null', async () => {
    await ban({ gameId: MOCK_PUID, reason: 'cheating' });
    expect(adapter.pendingBans()).toEqual([]);
    expect(await listBans()).toEqual([
      expect.objectContaining({ reason: 'cheating', expiresAt: null }),
    ]);
  });
});
