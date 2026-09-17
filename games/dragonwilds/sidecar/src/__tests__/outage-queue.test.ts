import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { Bridge, MAX_PENDING_EVENTS } from '../bridge.js';
import { MemoryCursorStore } from '../dragonwilds/cursorStore.js';
import { DragonwildsPluginClient } from '../dragonwilds/pluginClient.js';
import type { GameEventType } from '../takaro/protocol.js';
import { MockPlugin, MOCK_PUID } from '../testing/mockPlugin.js';
import { noTs } from './helpers.js';

/**
 * Finding F10: during a Takaro outage the sidecar wrote gameEvent frames onto a dead socket and advanced the persisted
 * cursor anyway, so every event produced while Takaro was unreachable was lost. These tests pin the fixed discipline.
 */
describe('Takaro outage: pending queue + cursor discipline', () => {
  let mock: MockPlugin;
  let store: MemoryCursorStore;
  let bridge: Bridge;
  let up = true;
  let delivered: Array<[GameEventType, unknown]>;

  beforeEach(async () => {
    mock = new MockPlugin();
    await mock.start();
    up = true;
    delivered = [];
    store = new MemoryCursorStore();
    bridge = new Bridge({
      plugin: new DragonwildsPluginClient({ baseUrl: mock.url(), token: mock.token }),
      takaro: {
        send: () => up,
        sendGameEvent: (t, d) => (up ? (delivered.push([t, noTs(d)]), true) : false),
      },
      cursorStore: store,
      logFile: '/nonexistent',
      logTailMode: 'never',
      logEvents: 'all',
      pollIntervalMs: 60000,
      healthCheckIntervalMs: 60000,
    });
  });
  afterEach(async () => {
    bridge.stopEvents();
    await mock.stop();
  });

  const player = { gameId: MOCK_PUID, name: 'Hendrik', characterName: 'Limon', epicOnlineServicesId: MOCK_PUID };

  it('queues events produced while Takaro is down, keeps the persisted cursor behind them, and flushes in order', async () => {
    mock.pushEvent('chat-message', { player, msg: 'before' });
    await bridge.poller.pollOnce();
    expect(store.state.seq).toBe(1);
    expect(delivered.map(([, d]) => (d as { msg: string }).msg)).toEqual(['before']);

    // Takaro goes away (half-dead socket: the close only arrives minutes later).
    up = false;
    mock.pushEvent('chat-message', { player, msg: 'nonce-1' });
    mock.pushEvent('chat-message', { player, msg: 'nonce-2' });
    mock.pushEvent('chat-message', { player, msg: 'nonce-3' });
    await bridge.poller.pollOnce();

    expect(bridge.pending().map((e) => (e.data as { msg: string }).msg)).toEqual(['nonce-1', 'nonce-2', 'nonce-3']);
    // Nothing was delivered, so the PERSISTED cursor must not have moved past them...
    expect(store.state.seq).toBe(1);
    // ...while the scan position did, so the same events are not read from the plugin ring twice.
    expect(bridge.poller.scanCursor()).toBe(4);
    expect(delivered).toHaveLength(1);

    // Reconnect + identify: everything is flushed in order and the cursor catches up.
    up = true;
    expect(bridge.flushPending()).toBe(3);
    expect(bridge.pending()).toEqual([]);
    expect(delivered.map(([, d]) => (d as { msg: string }).msg)).toEqual(['before', 'nonce-1', 'nonce-2', 'nonce-3']);
    expect(store.state.seq).toBe(4);
    expect(bridge.poller.cursor()).toBe(4);
  });

  it('keeps the original game timestamps on replayed events', async () => {
    const raw: Array<[GameEventType, unknown]> = [];
    (bridge as unknown as { options: { takaro: { sendGameEvent: (t: GameEventType, d: unknown) => boolean } } }).options.takaro.sendGameEvent = (t, d) => {
      if (!up) return false;
      raw.push([t, d]);
      return true;
    };
    const pushed = mock.pushEvent('chat-message', { player, msg: 'stamped' });
    up = false;
    await bridge.poller.pollOnce();
    await new Promise((r) => setTimeout(r, 25));
    up = true;
    expect(bridge.flushPending()).toBe(1);
    // The frame carries the game's own event time, not the reconnect time.
    expect((raw[0][1] as { timestamp?: string }).timestamp).toBe(new Date(String(pushed.ts)).toISOString());
  });

  it('a sidecar restart mid-outage replays the queued events (cursor never skipped them)', async () => {
    up = false;
    mock.pushEvent('chat-message', { player, msg: 'lost-if-broken' });
    await bridge.poller.pollOnce();
    expect(store.state.seq).toBe(0);

    // A fresh sidecar (new Bridge, same persisted cursor) re-reads the event from the plugin ring.
    const replayed: Array<[GameEventType, unknown]> = [];
    const restarted = new Bridge({
      plugin: new DragonwildsPluginClient({ baseUrl: mock.url(), token: mock.token }),
      takaro: { send: () => true, sendGameEvent: (t, d) => (replayed.push([t, noTs(d)]), true) },
      cursorStore: store,
      logFile: '/nonexistent',
      logTailMode: 'never',
      logEvents: 'all',
      pollIntervalMs: 60000,
      healthCheckIntervalMs: 60000,
    });
    try {
      await restarted.poller.pollOnce();
      expect(replayed.map(([, d]) => (d as { msg: string }).msg)).toEqual(['lost-if-broken']);
      expect(store.state.seq).toBe(1);
    } finally {
      restarted.stopEvents();
    }
  });

  it('bounds the queue and releases the cursor for events it had to drop', async () => {
    up = false;
    for (let i = 0; i < MAX_PENDING_EVENTS + 3; i += 1) mock.pushEvent('log', { msg: `spam-${i}` });
    await bridge.poller.pollOnce();
    expect(bridge.pending()).toHaveLength(MAX_PENDING_EVENTS);
    expect(bridge.dropped()).toBe(3);
    // The three dropped events are gone for good, so their cursor is released; everything still queued is not.
    expect(store.state.seq).toBe(3);
    expect(bridge.poller.scanCursor()).toBe(MAX_PENDING_EVENTS + 3);
  });

  it('a second outage during the flush keeps the rest of the queue', async () => {
    up = false;
    mock.pushEvent('chat-message', { player, msg: 'a' });
    mock.pushEvent('chat-message', { player, msg: 'b' });
    await bridge.poller.pollOnce();
    let allowed = 1;
    bridge = Object.assign(bridge, {});
    up = true;
    // Only the first send succeeds.
    (bridge as unknown as { options: { takaro: { sendGameEvent: (t: GameEventType, d: unknown) => boolean } } }).options.takaro.sendGameEvent = (t, d) => {
      if (allowed-- <= 0) return false;
      delivered.push([t, noTs(d)]);
      return true;
    };
    expect(bridge.flushPending()).toBe(1);
    expect(bridge.pending().map((e) => (e.data as { msg: string }).msg)).toEqual(['b']);
    expect(store.state.seq).toBe(1);
  });
});
