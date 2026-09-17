import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { Bridge } from '../bridge.js';
import { MemoryCursorStore } from '../dragonwilds/cursorStore.js';
import { EventPoller } from '../dragonwilds/eventPoller.js';
import { FileOnlineStore, MemoryOnlineStore } from '../dragonwilds/onlineStore.js';
import { DragonwildsPluginClient } from '../dragonwilds/pluginClient.js';
import { MockPlugin, MOCK_PUID } from '../testing/mockPlugin.js';
import { noTs } from './helpers.js';

const takaroPlayer = { gameId: MOCK_PUID, name: 'Limon', epicOnlineServicesId: MOCK_PUID, platformId: `epic:${MOCK_PUID}` };

describe('plugin bootId restart detection', () => {
  let mock: MockPlugin;
  beforeEach(async () => {
    mock = new MockPlugin();
    await mock.start();
  });
  afterEach(async () => mock.stop());

  it('persists bootId with the cursor and resets on a new bootId even when the new seq is already higher', async () => {
    const client = new DragonwildsPluginClient({ baseUrl: mock.url(), token: mock.token });
    const store = new MemoryCursorStore();
    const emitted: string[] = [];
    let restarts = 0;
    const poller = () =>
      new EventPoller({ getEvents: (s) => client.getEvents(s), emit: (_t, d: any) => void emitted.push(d.msg as string), store, onRestart: () => restarts++ });

    mock.bootId = 'aaaa';
    mock.pushEvent('log', { msg: 'old1' });
    mock.pushEvent('log', { msg: 'old2' });
    expect(await poller().pollOnce()).toBe(2);
    expect(store.state).toEqual({ seq: 2, bootId: 'aaaa' });

    // server restarts; the new process already has 5 events before the sidecar polls again
    mock.events = [];
    mock.bootId = 'bbbb';
    for (const m of ['n1', 'n2', 'n3', 'n4', 'n5']) mock.pushEvent('log', { msg: m });
    expect(await poller().pollOnce()).toBe(5);
    expect(restarts).toBe(1);
    expect(emitted).toEqual(['old1', 'old2', 'n1', 'n2', 'n3', 'n4', 'n5']);
    expect(store.state).toEqual({ seq: 5, bootId: 'bbbb' });

    // same boot: no replay
    expect(await poller().pollOnce()).toBe(5);
    expect(emitted).toHaveLength(7);
  });

  it('adopts a bootId for a legacy cursor without replaying', async () => {
    const client = new DragonwildsPluginClient({ baseUrl: mock.url(), token: mock.token });
    mock.pushEvent('log', { msg: 'a' });
    mock.pushEvent('log', { msg: 'b' });
    mock.bootId = 'cccc';
    const store = new MemoryCursorStore({ seq: 2 });
    const emitted: unknown[] = [];
    const p = new EventPoller({ getEvents: (s) => client.getEvents(s), emit: (_t, d) => void emitted.push(noTs(d)), store });
    mock.pushEvent('log', { msg: 'c' });
    expect(await p.pollOnce()).toBe(3);
    expect(emitted).toEqual([{ msg: 'c' }]);
    expect(store.state).toEqual({ seq: 3, bootId: 'cccc' });
  });
});

describe('online reconciliation', () => {
  let mock: MockPlugin;
  let dir: string;
  beforeEach(async () => {
    mock = new MockPlugin();
    await mock.start();
    dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dw-online-'));
  });
  afterEach(async () => {
    await mock.stop();
    fs.rmSync(dir, { recursive: true, force: true });
  });

  const makeBridge = (onlineStore: FileOnlineStore | MemoryOnlineStore, events: Array<[string, any]>) =>
    new Bridge({
      plugin: new DragonwildsPluginClient({ baseUrl: mock.url(), token: mock.token }),
      takaro: { send: () => true, sendGameEvent: (t, d) => (events.push([t, d]), true) },
      cursorStore: new MemoryCursorStore(),
      onlineStore,
      logFile: '/nonexistent',
      logTailMode: 'never',
      pollIntervalMs: 60000,
      healthCheckIntervalMs: 60000,
    });

  it('tracks forwarded connects in the store and sends player-disconnected on startup for players no longer on the server', async () => {
    const file = path.join(dir, 'online-players.json');
    const b1 = makeBridge(new FileOnlineStore(file), []);
    b1.noteConnectionEvent('player-connected', { player: takaroPlayer });
    expect(new FileOnlineStore(file).load()).toEqual([takaroPlayer]);

    // server restarted without logging the leave: the plugin lists nobody
    mock.players = [];
    const events: Array<[string, any]> = [];
    const b2 = makeBridge(new FileOnlineStore(file), events);
    try {
      await b2.startEvents();
      expect(events).toContainEqual(['player-disconnected', { player: takaroPlayer }]);
      expect(new FileOnlineStore(file).load()).toEqual([]);
      expect(b2.onlinePlayers()).toEqual([]);
      // the location lookup for the just-disconnected player is answered (Takaro stores the event only then)
      expect(b2.eventLocationFallback({ gameId: MOCK_PUID })).toEqual({ x: 0, y: 0, z: 0 });
    } finally {
      b2.stopEvents();
      b1.stopEvents();
    }
  });

  it('a bootId change reconciles online players (vanished players get player-disconnected)', async () => {
    mock.bootId = 'boot-1';
    const store = new MemoryOnlineStore([takaroPlayer]);
    const events: Array<[string, any]> = [];
    const b = makeBridge(store, events);
    try {
      // drive the poller by hand so the bootId swap below cannot race a background tick
      await b.refreshHealth();
      await b.poller.pollOnce();
      expect(events.filter((e) => e[0] === 'player-disconnected')).toEqual([]); // still online
      expect(b.poller.cursor()).toBe(0);

      mock.bootId = 'boot-2';
      mock.events = [];
      mock.players = []; // the new process has nobody on it
      mock.pushEvent('log', { msg: 'LogInit: new boot' });
      await b.poller.pollOnce();
      for (let i = 0; i < 50 && !events.some((e) => e[0] === 'player-disconnected'); i++) await new Promise((r) => setTimeout(r, 10));
      expect(events).toContainEqual(['player-disconnected', { player: takaroPlayer }]);
      expect(store.players).toEqual([]);
    } finally {
      b.stopEvents();
    }
  });

  it('keeps players that are still online and disconnect events remove them', async () => {
    const store = new MemoryOnlineStore([takaroPlayer]);
    const events: Array<[string, any]> = [];
    const b = makeBridge(store, events);
    try {
      expect(await b.reconcileOnline()).toEqual([]);
      expect(events.filter((e) => e[0] === 'player-disconnected')).toEqual([]);
      b.noteConnectionEvent('player-disconnected', { player: { gameId: MOCK_PUID } });
      expect(store.players).toEqual([]);
    } finally {
      b.stopEvents();
    }
  });

  it('postpones when the plugin is unreachable', async () => {
    const store = new MemoryOnlineStore([takaroPlayer]);
    const events: Array<[string, any]> = [];
    const b = makeBridge(store, events);
    await mock.stop();
    expect(await b.reconcileOnline()).toEqual([]);
    expect(events).toEqual([]);
    expect(store.players).toEqual([takaroPlayer]);
    b.stopEvents();
    await mock.start();
  });
});
