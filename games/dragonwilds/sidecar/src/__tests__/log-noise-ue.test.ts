import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { Bridge, redactLog, shouldForwardLog, shouldTailLog } from '../bridge.js';
import { MemoryCursorStore } from '../dragonwilds/cursorStore.js';
import { DragonwildsPluginClient } from '../dragonwilds/pluginClient.js';
import { MockPlugin } from '../testing/mockPlugin.js';
import { noTs } from './helpers.js';

const NOISE = [
  '[2026.09.16-15.38.52:100][  0]LogRedpointEOS: Verbose: EOS_Platform_Tick',
  '[2026.09.16-15.38.52:100][  0]LogRedpointEOSCore: Verbose: cached 12 entries',
  '[2026.09.16-15.38.52:100][  0]LogEOSHTTP: POST https://api.epicgames.dev/... 200',
  '[2026.09.16-15.38.52:100][  0]LogEOSAnalytics: flushing 3 events',
  '[2026.09.16-15.38.52:100][  0]LogPlayerReporting: SendBackendEvent(PlayerJoined)',
  '',
  '   ',
];
const SIGNAL = [
  '[2026.09.16-15.38.52:100][  0]LogNet: Join succeeded: Limon',
  '[2026.09.16-15.38.52:100][  0]LogDominionSaveGame: World saved',
  '[2026.09.16-15.38.52:100][  0]LogRedpointEOS: Warning: connection lost',
];

describe('UE/EOS log noise filter', () => {
  it('drops Unreal/EOS chatter in filtered mode', () => {
    for (const msg of NOISE) expect(shouldForwardLog('filtered', { msg }), msg).toBe(false);
  });
  it('keeps admin-relevant lines, including non-verbose EOS warnings', () => {
    for (const msg of SIGNAL) expect(shouldForwardLog('filtered', { msg }), msg).toBe(true);
  });
  it('mode all forwards everything and mode none forwards nothing', () => {
    expect(shouldForwardLog('all', { msg: NOISE[0] })).toBe(true);
    expect(shouldForwardLog('none', { msg: SIGNAL[0] })).toBe(false);
  });
  it('shouldTailLog follows the plugin players capability', () => {
    expect(shouldTailLog('never', null)).toBe(false);
    expect(shouldTailLog('always', { status: 'ok' })).toBe(true);
    expect(shouldTailLog('auto', null)).toBe(true);
    expect(shouldTailLog('auto', { status: 'starting', capabilities: { players: 'ok' } })).toBe(true);
    expect(shouldTailLog('auto', { status: 'ok', capabilities: { players: 'ok', chatEvents: 'unimplemented' } })).toBe(false);
    expect(shouldTailLog('auto', { status: 'degraded', capabilities: { players: 'degraded' } })).toBe(true);
  });
});

describe('password redaction (the Dragonwilds server prints them in cleartext)', () => {
  it('redacts the value of any *Password key', () => {
    expect(redactLog('LogDominion: WorldPassword=swordfish')).toBe('LogDominion: WorldPassword=[redacted]');
    expect(redactLog('AdminPassword = "s3cr3t!"')).toBe('AdminPassword = [redacted]');
    expect(redactLog('Settings: Password=abc, ServerName=Takaro')).toBe('Settings: Password=[redacted], ServerName=Takaro');
    expect(redactLog('[..]LogDom: ServerPassword: hunter2')).toBe('[..]LogDom: ServerPassword: [redacted]');
    expect(redactLog('WorldPassword=a AdminPassword=b')).toBe('WorldPassword=[redacted] AdminPassword=[redacted]');
  });
  it('redacts the base64 world password in Login/Join request URL options (verified on the real server)', () => {
    expect(redactLog('LogNet: Login request: ?p=cGFzc3dvcmQ=?pf=PC?cpx=1?Name=Limon userId: RedpointEOS:0123456789abcdef0123456789abcdef platform: RedpointEOS')).toBe(
      'LogNet: Login request: ?p=[redacted]?pf=PC?cpx=1?Name=Limon userId: RedpointEOS:0123456789abcdef0123456789abcdef platform: RedpointEOS',
    );
    expect(redactLog('LogNet: Join request: /Game/Maps/Server/L_ServerStartup?p=cGFzc3dvcmQ=?pf=PC?cpx=1?Name=Limon?SplitscreenCount=1')).toBe(
      'LogNet: Join request: /Game/Maps/Server/L_ServerStartup?p=[redacted]?pf=PC?cpx=1?Name=Limon?SplitscreenCount=1',
    );
    // trailing ?p= with nothing after it, and an empty password
    expect(redactLog('LogNet: Join request: /Game/Maps/World/L_World?p=c3dvcmRmaXNo')).toBe('LogNet: Join request: /Game/Maps/World/L_World?p=[redacted]');
    expect(redactLog('LogNet: Login request: ?p=?pf=PC')).toBe('LogNet: Login request: ?p=[redacted]?pf=PC');
    expect(redactLog('LogNet: Login request: ?p=one?pf=PC and again ?p=two')).toBe('LogNet: Login request: ?p=[redacted]?pf=PC and again ?p=[redacted]');
  });

  it('drops the whole line when a password appears without a key=value shape', () => {
    expect(redactLog('the WorldPassword for this server is swordfish')).toBe('[redacted: line mentions a password]');
  });
  it('leaves unrelated lines untouched', () => {
    expect(redactLog('LogNet: Join succeeded: Limon')).toBe('LogNet: Join succeeded: Limon');
    expect(redactLog('')).toBe('');
  });
});

describe('bridge forwards redacted log events', () => {
  let mock: MockPlugin;
  let events: Array<[string, any]>;
  let bridge: Bridge;

  beforeEach(async () => {
    mock = new MockPlugin();
    await mock.start();
    events = [];
    bridge = new Bridge({
      plugin: new DragonwildsPluginClient({ baseUrl: mock.url(), token: mock.token }),
      takaro: { send: () => true, sendGameEvent: (t, d) => (events.push([t, noTs(d)]), true) },
      cursorStore: new MemoryCursorStore(),
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

  it('redacts even in logEvents=all, and never forwards a cleartext password', async () => {
    mock.pushEvent('log', { msg: 'LogDominion: WorldPassword=swordfish AdminPassword=hunter2' });
    mock.pushEvent('log', { msg: 'LogNet: Login request: ?p=c3dvcmRmaXNo?pf=PC?cpx=1?Name=Limon userId: RedpointEOS:0123456789abcdef0123456789abcdef' });
    mock.pushEvent('log', { msg: 'LogNet: Join succeeded: Limon' });
    await bridge.poller.pollOnce();
    expect(events).toEqual([
      ['log', { msg: 'LogDominion: WorldPassword=[redacted] AdminPassword=[redacted]' }],
      ['log', { msg: 'LogNet: Login request: ?p=[redacted]?pf=PC?cpx=1?Name=Limon userId: RedpointEOS:0123456789abcdef0123456789abcdef' }],
      ['log', { msg: 'LogNet: Join succeeded: Limon' }],
    ]);
    expect(JSON.stringify(events)).not.toMatch(/swordfish|hunter2|c3dvcmRmaXNo/);
  });

  it('filtered mode drops EOS verbose spam but still advances the cursor', async () => {
    bridge.stopEvents();
    const filtered = new Bridge({
      plugin: new DragonwildsPluginClient({ baseUrl: mock.url(), token: mock.token }),
      takaro: { send: () => true, sendGameEvent: (t, d) => (events.push([t, noTs(d)]), true) },
      cursorStore: new MemoryCursorStore(),
      logFile: '/nonexistent',
      logTailMode: 'never',
      logEvents: 'filtered',
      pollIntervalMs: 60000,
      healthCheckIntervalMs: 60000,
    });
    try {
      mock.pushEvent('log', { msg: NOISE[0] });
      mock.pushEvent('log', { msg: SIGNAL[1] });
      expect(await filtered.poller.pollOnce()).toBe(2);
      expect(events).toEqual([['log', { msg: SIGNAL[1] }]]);
    } finally {
      filtered.stopEvents();
    }
  });
});
