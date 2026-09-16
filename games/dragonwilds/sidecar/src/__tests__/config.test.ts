import { describe, expect, it } from 'vitest';
import { DEFAULT_LOG_FILE, loadConfig } from '../config.js';

describe('config', () => {
  it('applies defaults', () => {
    const c = loadConfig({ TAKARO_PLUGIN_TOKEN: 'p', TAKARO_REGISTRATION_TOKEN: 'r' });
    expect(c.identityToken).toBe('dragonwilds');
    expect(c.registrationToken).toBe('r');
    expect(c.pluginBaseUrl).toBe('http://127.0.0.1:18890');
    expect(c.takaroWsUrl).toBe('wss://connect.takaro.io/');
    expect(c.logTailMode).toBe('auto');
    expect(c.logEvents).toBe('filtered');
    expect(c.logFile).toBe(DEFAULT_LOG_FILE);
    expect(DEFAULT_LOG_FILE).toBe('/home/ubuntu/Steam/RSDragonwilds/Saved/Logs/RSDragonwilds.log');
    expect(c.serverName).toBe('Takaro Dev Dragonwilds');
    expect(c.senderName).toBe('');
    expect(c.healthPort).toBe(18891);
  });

  it('requires plugin token and validates the DRAGONWILDS_LOG_* enums', () => {
    expect(() => loadConfig({})).toThrow(/TAKARO_PLUGIN_TOKEN/);
    expect(() => loadConfig({ TAKARO_PLUGIN_TOKEN: 'p', DRAGONWILDS_LOG_TAIL: 'maybe' })).toThrow(/auto\|always\|never/);
    expect(() => loadConfig({ TAKARO_PLUGIN_TOKEN: 'p', DRAGONWILDS_LOG_EVENTS: 'loud' })).toThrow(/all\|filtered\|none/);
  });

  it('honours overrides and derives the data-dir files from the cursor file', () => {
    const c = loadConfig({
      TAKARO_PLUGIN_TOKEN: 'p',
      TAKARO_IDENTITY_TOKEN: 'x',
      TAKARO_PLUGIN_URL: 'http://h:1/',
      TAKARO_POLL_INTERVAL_MS: '250',
      TAKARO_CURSOR_FILE: '/data/event-cursor.json',
      DRAGONWILDS_LOG_FILE: '/logs/RSDragonwilds.log',
      DRAGONWILDS_LOG_TAIL: 'always',
      DRAGONWILDS_LOG_EVENTS: 'none',
      SIDECAR_HEALTH_PORT: '9999',
    });
    expect(c.identityToken).toBe('x');
    expect(c.pluginBaseUrl).toBe('http://h:1');
    expect(c.pollIntervalMs).toBe(250);
    expect(c.logFile).toBe('/logs/RSDragonwilds.log');
    expect(c.logTailMode).toBe('always');
    expect(c.logEvents).toBe('none');
    expect(c.healthPort).toBe(9999);
    expect(c.onlineFile).toBe('/data/online-players.json');
    expect(c.banFile).toBe('/data/timed-bans.json');
  });

  it('sender name comes from TAKARO_SENDER_NAME, then TAKARO_SERVER_CHAT_NAME', () => {
    expect(loadConfig({ TAKARO_PLUGIN_TOKEN: 'p', TAKARO_SENDER_NAME: 'Takaro' }).senderName).toBe('Takaro');
    expect(loadConfig({ TAKARO_PLUGIN_TOKEN: 'p', TAKARO_SERVER_CHAT_NAME: 'Chat' }).senderName).toBe('Chat');
    expect(loadConfig({ TAKARO_PLUGIN_TOKEN: 'p', TAKARO_SENDER_NAME: 'A', TAKARO_SERVER_CHAT_NAME: 'B' }).senderName).toBe('A');
  });
});
