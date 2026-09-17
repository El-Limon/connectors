import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { WebSocketServer } from 'ws';
import { Bridge, type TakaroSink } from '../bridge.js';
import { MemoryCursorStore } from '../vein/cursorStore.js';
import { VeinPluginClient } from '../vein/pluginClient.js';
import { TakaroWsClient } from '../takaro/client.js';
import type { GameEventType } from '../takaro/protocol.js';
import { MockPlugin, MOCK_STEAMID } from '../testing/mockPlugin.js';
import { noTs } from './helpers.js';

function waitFor<T>(fn: () => T | undefined | false, timeoutMs = 4000): Promise<T> {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const tick = (): void => {
      const v = fn();
      if (v !== undefined && v !== false) return resolve(v as T);
      if (Date.now() - start > timeoutMs) return reject(new Error('waitFor timeout'));
      setTimeout(tick, 10);
    };
    tick();
  });
}

/**
 * Finding F20: `ws.send()` succeeding proves only that the LOCAL kernel took the bytes. During an egress outage the
 * sidecar kept writing gameEvents into a black hole, advanced the persisted cursor past them, and never re-sent them —
 * ~30 s of events silently lost at the start of every outage. The fix keeps an unconfirmed-send window that is only
 * released by a pong for a ping written after the event.
 */
describe('F20 — unconfirmed-send window (heartbeat-confirmed cursor)', () => {
  let mock: MockPlugin;
  let store: MemoryCursorStore;
  let bridge: Bridge;
  let wire: Array<[GameEventType, unknown]>;
  let sendId: number;
  let up: boolean;
  let confirm: (id: number) => void;

  beforeEach(async () => {
    mock = new MockPlugin();
    await mock.start();
    wire = [];
    sendId = 0;
    up = true;
    store = new MemoryCursorStore();
    const listeners: Array<(id: number) => void> = [];
    confirm = (id) => listeners.forEach((l) => l(id));
    const sink: TakaroSink = {
      send: () => up,
      sendGameEvent: (t, d) => (up ? (sendId += 1, wire.push([t, noTs(d)]), true) : false),
      lastSendId: () => sendId,
      onConfirmed: (cb) => listeners.push(cb),
    };
    bridge = new Bridge({
      plugin: new VeinPluginClient({ baseUrl: mock.url(), token: mock.token }),
      takaro: sink,
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

  const player = { gameId: MOCK_STEAMID, name: 'Tester', epicOnlineServicesId: MOCK_STEAMID };

  it('(a)+(c) events written but unconfirmed when the socket dies are re-sent in order, once, and the cursor never passes them', async () => {
    mock.pushEvent('chat-message', { player, msg: 'l4c-a' });
    mock.pushEvent('chat-message', { player, msg: 'l4c-b' });
    await bridge.poller.pollOnce();

    // Both are on the wire, but nothing is PROVEN: the persisted cursor must still be 0 (c).
    expect(wire.map(([, d]) => (d as { msg: string }).msg)).toEqual(['l4c-a', 'l4c-b']);
    expect(bridge.unconfirmed()).toHaveLength(2);
    expect(store.state.seq).toBe(0);
    expect(bridge.poller.cursor()).toBe(0);

    // The socket dies (heartbeat timeout / 1006) before any pong.
    bridge.stopEvents();
    expect(bridge.unconfirmed()).toHaveLength(0);
    expect(bridge.pending().map((e) => (e.data as { msg: string }).msg)).toEqual(['l4c-a', 'l4c-b']);
    expect(store.state.seq).toBe(0);

    // Reconnect + identifyResponse: everything after the confirmed cursor goes out again, in order.
    wire.length = 0;
    expect(bridge.flushPending()).toBe(2);
    expect(wire.map(([, d]) => (d as { msg: string }).msg)).toEqual(['l4c-a', 'l4c-b']);
    expect(store.state.seq).toBe(0); // still unproven

    confirm(sendId);
    expect(bridge.poller.cursor()).toBe(2);
    expect(store.state.seq).toBe(2);

    // And they are not sent a third time.
    wire.length = 0;
    await bridge.poller.pollOnce();
    expect(wire).toEqual([]);
  });

  it('(b) events a pong confirmed are NOT re-sent after a reconnect', async () => {
    mock.pushEvent('chat-message', { player, msg: 'l4c-confirmed' });
    await bridge.poller.pollOnce();
    confirm(sendId); // heartbeat proves it arrived
    expect(store.state.seq).toBe(1);
    expect(bridge.unconfirmed()).toHaveLength(0);

    mock.pushEvent('chat-message', { player, msg: 'l4c-unconfirmed' });
    await bridge.poller.pollOnce();
    expect(store.state.seq).toBe(1);

    bridge.stopEvents();
    wire.length = 0;
    bridge.flushPending();
    expect(wire.map(([, d]) => (d as { msg: string }).msg)).toEqual(['l4c-unconfirmed']);
  });

  it('(c) a partial confirmation advances the cursor only to the confirmed seq', async () => {
    mock.pushEvent('chat-message', { player, msg: 'one' });
    await bridge.poller.pollOnce();
    const afterFirst = sendId;
    mock.pushEvent('chat-message', { player, msg: 'two' });
    mock.pushEvent('chat-message', { player, msg: 'three' });
    await bridge.poller.pollOnce();

    confirm(afterFirst);
    expect(store.state.seq).toBe(1);
    expect(bridge.unconfirmed().map((e) => e.seq)).toEqual([2, 3]);

    bridge.stopEvents();
    expect(store.state.seq).toBe(1);
    expect(bridge.pending().map((e) => (e.data as { msg: string }).msg)).toEqual(['two', 'three']);
  });
});

describe('F20 — client heartbeat', () => {
  const cleanups: Array<() => unknown> = [];
  afterEach(async () => {
    for (const c of cleanups.splice(0).reverse()) await c();
  });

  it('(d) a silent peer (no pongs) is torn down and reconnected inside the heartbeat budget', async () => {
    // autoPong:false makes the server behave exactly like the rig outage: frames leave us, nothing comes back.
    const wss = new WebSocketServer({ port: 0, host: '127.0.0.1', autoPong: false });
    await new Promise((r) => wss.once('listening', r));
    cleanups.push(() => new Promise((r) => wss.close(r)));
    const port = (wss.address() as { port: number }).port;
    let connections = 0;
    wss.on('connection', (ws) => {
      connections += 1;
      ws.on('message', (raw) => {
        const msg = JSON.parse(raw.toString()) as { type: string };
        if (msg.type === 'identify') ws.send(JSON.stringify({ type: 'identifyResponse', payload: { server: { id: 'gs-1' } } }));
      });
    });

    const takaro = new TakaroWsClient(
      `ws://127.0.0.1:${port}`,
      { identityToken: 'vein', registrationToken: 'reg-123' },
      { baseReconnectMs: 20, maxReconnectMs: 40, pingIntervalMs: 50, maxMissedPongs: 2, idleTimeoutMs: 60_000 },
    );
    cleanups.push(() => takaro.shutdown());
    const started = Date.now();
    let disconnects = 0;
    takaro.on('disconnected', () => (disconnects += 1));
    takaro.connect();
    await waitFor(() => takaro.identified() || undefined);

    await waitFor(() => disconnects >= 1 || undefined, 2000);
    // 2 missed pongs at a 50 ms interval: the teardown must land in a small multiple of the interval, not 30 s.
    expect(Date.now() - started).toBeLessThan(1000);
    await waitFor(() => connections >= 2 || undefined, 2000);
  });

  it('(b-wire) a pong confirms every gameEvent written before the ping, and nothing after it', async () => {
    const wss = new WebSocketServer({ port: 0, host: '127.0.0.1' });
    await new Promise((r) => wss.once('listening', r));
    cleanups.push(() => new Promise((r) => wss.close(r)));
    const port = (wss.address() as { port: number }).port;
    wss.on('connection', (ws) => {
      ws.on('message', (raw) => {
        const msg = JSON.parse(raw.toString()) as { type: string };
        if (msg.type === 'identify') ws.send(JSON.stringify({ type: 'identifyResponse', payload: { server: { id: 'gs-1' } } }));
      });
    });
    const takaro = new TakaroWsClient(
      `ws://127.0.0.1:${port}`,
      { identityToken: 'vein', registrationToken: 'reg-123' },
      { baseReconnectMs: 20, pingIntervalMs: 60 },
    );
    cleanups.push(() => takaro.shutdown());
    const confirmed: number[] = [];
    takaro.onConfirmed((id) => confirmed.push(id));
    takaro.connect();
    await waitFor(() => takaro.identified() || undefined);

    expect(takaro.sendGameEvent('chat-message', { msg: 'l4c-wire-1' })).toBe(true);
    expect(takaro.lastSendId()).toBe(1);
    expect(takaro.lastConfirmedId()).toBe(0);
    await waitFor(() => takaro.lastConfirmedId() >= 1 || undefined, 2000);
    expect(confirmed.at(-1)).toBe(1);

    // An inbound frame alone must NOT confirm anything (it proves only that THEY are alive).
    expect(takaro.sendGameEvent('chat-message', { msg: 'l4c-wire-2' })).toBe(true);
    expect(takaro.lastConfirmedId()).toBe(1);
    await waitFor(() => takaro.lastConfirmedId() >= 2 || undefined, 2000);
  });
});
