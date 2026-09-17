import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { Bridge } from '../bridge.js';
import { FileCursorStore, MemoryCursorStore } from '../dragonwilds/cursorStore.js';
import { EventPoller } from '../dragonwilds/eventPoller.js';
import { mapPluginEvent } from '../dragonwilds/mapping.js';
import { DragonwildsPluginClient } from '../dragonwilds/pluginClient.js';
import { MockPlugin, MOCK_PUID } from '../testing/mockPlugin.js';
import { noTs } from './helpers.js';

const player = { gameId: MOCK_PUID, name: 'Hendrik', characterName: 'Limon', epicOnlineServicesId: MOCK_PUID };
const takaroPlayer = { gameId: MOCK_PUID, name: 'Limon', epicOnlineServicesId: MOCK_PUID, platformId: `epic:${MOCK_PUID}` };

describe('event mapping', () => {
  it('player-connected / player-disconnected (nested or flat player)', () => {
    expect(mapPluginEvent({ type: 'player-connected', data: { player } })).toEqual({ type: 'player-connected', data: { player: takaroPlayer } });
    expect(mapPluginEvent({ type: 'player-disconnected', data: player })).toEqual({ type: 'player-disconnected', data: { player: takaroPlayer } });
  });
  it('chat-message', () => {
    expect(mapPluginEvent({ type: 'chat-message', data: { player, message: 'hi', channel: 'Global' } })).toEqual({
      type: 'chat-message',
      data: { player: takaroPlayer, msg: 'hi', channel: 'global' },
    });
    expect(mapPluginEvent({ type: 'chat-message', data: { msg: 'sys', channel: 'team' } })).toEqual({ type: 'chat-message', data: { msg: 'sys', channel: 'team' } });
    expect(mapPluginEvent({ type: 'chat-message', data: { player, text: 'psst', channel: 'whisper' } })?.data).toMatchObject({ msg: 'psst', channel: 'whisper' });
  });
  it('player-death with position and attacker', () => {
    const attacker = { gameId: 'aa11bb22cc33dd44ee55ff6600112233', characterName: 'Bob' };
    expect(mapPluginEvent({ type: 'player-death', data: { player, position: { x: 1, y: 2, z: 3 }, attacker } })).toEqual({
      type: 'player-death',
      data: {
        player: takaroPlayer,
        position: { x: 1, y: 2, z: 3 },
        attacker: { gameId: attacker.gameId, name: 'Bob', epicOnlineServicesId: attacker.gameId, platformId: `epic:${attacker.gameId}` },
      },
    });
  });
  it('player-death by a creature puts the killer in msg', () => {
    expect(mapPluginEvent({ type: 'player-death', data: { player, killerEntity: 'AI_Goblin_Melee' } })?.data).toMatchObject({
      msg: 'Limon was killed by AI_Goblin_Melee',
    });
    expect(mapPluginEvent({ type: 'player-death', data: { player } })?.data).not.toHaveProperty('msg');
  });
  it('entity-killed', () => {
    expect(mapPluginEvent({ type: 'entity-killed', data: { player, entity: 'AI_Goblin_Melee', weapon: 'Item_Sword_Bronze' } })).toEqual({
      type: 'entity-killed',
      data: { player: takaroPlayer, entity: 'AI_Goblin_Melee', weapon: 'Item_Sword_Bronze' },
    });
    expect(mapPluginEvent({ type: 'entity-killed', data: { player, entity: { code: 'AI_Chicken' } } })?.data).toMatchObject({ entity: 'AI_Chicken', weapon: '' });
  });
  it('log', () => {
    expect(mapPluginEvent({ type: 'log', data: { msg: 'line' } })).toEqual({ type: 'log', data: { msg: 'line' } });
    expect(mapPluginEvent({ type: 'log', data: { line: 'LogDominion: saved', level: 'info' } })).toEqual({ type: 'log', data: { msg: 'LogDominion: saved' } });
    expect(mapPluginEvent({ type: 'log', data: 'raw' })).toEqual({ type: 'log', data: { msg: 'raw' } });
  });
  it('unknown types are dropped', () => {
    expect(mapPluginEvent({ type: 'player-sync', data: {} })).toBeNull();
  });
});

describe('EventPoller + cursor persistence', () => {
  let mock: MockPlugin;
  let dir: string;
  beforeEach(async () => {
    mock = new MockPlugin();
    await mock.start();
    dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dw-cursor-'));
  });
  afterEach(async () => {
    await mock.stop();
    fs.rmSync(dir, { recursive: true, force: true });
  });

  const client = () => new DragonwildsPluginClient({ baseUrl: mock.url(), token: mock.token });

  it('forwards events in order, persists seq, and does not replay after restart', async () => {
    const file = path.join(dir, 'sub', 'cursor.json');
    const emitted: Array<[string, unknown]> = [];
    mock.pushEvent('player-connected', { player });
    mock.pushEvent('chat-message', { player, msg: 'hello' });
    mock.pushEvent('bogus', {});

    const p1 = new EventPoller({ getEvents: (s) => client().getEvents(s), emit: (t, d) => void emitted.push([t, noTs(d)]), store: new FileCursorStore(file) });
    expect(await p1.pollOnce()).toBe(3);
    expect(emitted.map((e) => e[0])).toEqual(['player-connected', 'chat-message']);
    expect(JSON.parse(fs.readFileSync(file, 'utf8'))).toEqual({ seq: 3 });
    expect(mock.lastRequest('GET', '/events')?.query.since).toBe('0');

    mock.pushEvent('player-death', { player, position: { x: 0, y: 0, z: 0 } });
    const p2 = new EventPoller({ getEvents: (s) => client().getEvents(s), emit: (t, d) => void emitted.push([t, noTs(d)]), store: new FileCursorStore(file) });
    expect(p2.cursor()).toBe(3);
    await p2.pollOnce();
    expect(mock.lastRequest('GET', '/events')?.query.since).toBe('3');
    expect(emitted.map((e) => e[0])).toEqual(['player-connected', 'chat-message', 'player-death']);
    expect(new FileCursorStore(file).load()).toEqual({ seq: 4 });
  });

  it('does not advance past events Takaro could not accept', async () => {
    mock.pushEvent('log', { msg: 'a' });
    mock.pushEvent('log', { msg: 'b' });
    const store = new MemoryCursorStore();
    let accept = false;
    const got: unknown[] = [];
    const poller = new EventPoller({ getEvents: (s) => client().getEvents(s), emit: (_t, d) => (accept ? (got.push(noTs(d)), true) : false), store });
    expect(await poller.pollOnce()).toBe(0);
    accept = true;
    expect(await poller.pollOnce()).toBe(2);
    expect(got).toEqual([{ msg: 'a' }, { msg: 'b' }]);
  });

  it('resets cursor when plugin seq goes backwards (plugin restart)', async () => {
    const store = new MemoryCursorStore({ seq: 50 });
    mock.pushEvent('log', { msg: 'after restart' });
    const got: unknown[] = [];
    const poller = new EventPoller({ getEvents: (s) => client().getEvents(s), emit: (_t, d) => void got.push(noTs(d)), store });
    expect(await poller.pollOnce()).toBe(1);
    expect(got).toEqual([{ msg: 'after restart' }]);
  });

  it('drops a malformed event without stalling the cursor', async () => {
    mock.pushEvent('player-connected', {}); // no identifier at all
    mock.pushEvent('log', { msg: 'still flowing' });
    const errors: string[] = [];
    const got: unknown[] = [];
    const poller = new EventPoller({
      getEvents: (s) => client().getEvents(s),
      emit: (_t, d) => void got.push(noTs(d)),
      store: new MemoryCursorStore(),
      onError: (e) => errors.push(e.message),
    });
    expect(await poller.pollOnce()).toBe(2);
    expect(got).toEqual([{ msg: 'still flowing' }]);
    expect(errors.join()).toMatch(/malformed plugin event seq=1/);
  });

  it('suppresses tailed types while log tail is active', async () => {
    mock.pushEvent('player-connected', { player });
    mock.pushEvent('chat-message', { player, msg: 'x' });
    const types: string[] = [];
    const poller = new EventPoller({
      getEvents: (s) => client().getEvents(s),
      emit: (t) => void types.push(t),
      store: new MemoryCursorStore(),
      suppress: () => new Set(['player-connected']),
    });
    expect(await poller.pollOnce()).toBe(2);
    expect(types).toEqual(['chat-message']);
  });
});

describe('log tail takeover', () => {
  it('bridge enables the tail when the plugin players capability is degraded, and forwards log joins', async () => {
    const mock = new MockPlugin();
    await mock.start();
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dw-log-'));
    const logFile = path.join(dir, 'RSDragonwilds.log');
    fs.writeFileSync(logFile, '[2026.09.16-15.00.00:000][  0]LogInit: boot\n');
    const events: Array<[string, any]> = [];
    const bridge = new Bridge({
      plugin: new DragonwildsPluginClient({ baseUrl: mock.url(), token: mock.token }),
      takaro: { send: () => true, sendGameEvent: (t, d) => (events.push([t, d]), true) },
      cursorStore: new MemoryCursorStore(),
      logFile,
      logTailMode: 'auto',
      pollIntervalMs: 60000,
      healthCheckIntervalMs: 60000,
    });
    try {
      mock.health = { status: 'ok', capabilities: { players: 'degraded' } };
      mock.pushEvent('player-connected', { player }); // must be suppressed (the tail owns joins)
      await bridge.startEvents();
      expect(bridge.isLogTailActive()).toBe(true);
      // `Join succeeded` is the platform name and never an event; the authoritative line is `PlayerChar entered world`
      fs.appendFileSync(logFile, 'LogNet: Join succeeded: Limon\n');
      bridge.tailer.poll();
      await bridge.tailer.flush();
      expect(events).toEqual([]);
      fs.appendFileSync(
        logFile,
        `LogDominionPlayerControllerBase: PlayerChar entered world [Account[XP:${MOCK_PUID}] Character Name[takarotester] Guid[DCG:41C4B04F] Type[0]]\n`,
      );
      bridge.tailer.poll();
      await bridge.tailer.flush();
      await bridge.poller.pollOnce();
      expect(events).toEqual([
        ['player-connected', { player: { ...takaroPlayer, name: 'takarotester', steamId: '76561198000000001', ping: 24 } }],
      ]);

      mock.health = { status: 'ok', capabilities: { players: 'ok' } };
      await bridge.refreshHealth();
      expect(bridge.isLogTailActive()).toBe(false);
    } finally {
      bridge.stopEvents();
      await mock.stop();
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});
