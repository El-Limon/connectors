import { describe, expect, it } from 'vitest';
import { DEFAULT_LOG_FILE, loadConfig } from '../config.js';

describe('config', () => {
  it('applies defaults', () => {
    const c = loadConfig({ TAKARO_PLUGIN_TOKEN: 'p', TAKARO_REGISTRATION_TOKEN: 'r' });
    expect(c.identityToken).toBe('vein');
    expect(c.registrationToken).toBe('r');
    expect(c.pluginBaseUrl).toBe('http://127.0.0.1:18890');
    expect(c.takaroWsUrl).toBe('wss://connect.takaro.io/');
    expect(c.logTailMode).toBe('auto');
    expect(c.logEvents).toBe('filtered');
    expect(c.logFile).toBe(DEFAULT_LOG_FILE);
    expect(DEFAULT_LOG_FILE).toBe('/home/steam/vein/Vein/Saved/Logs/Vein.log');
    expect(c.httpApiUrl).toBe('http://127.0.0.1:8080');
    expect(c.logGrammarOverrides).toEqual({});
    expect(c.serverName).toBe('Takaro Dev Vein');
    expect(c.senderName).toBe('');
    expect(c.healthPort).toBe(18891);
  });

  it('requires plugin token and validates the VEIN_LOG_* enums', () => {
    expect(() => loadConfig({})).toThrow(/TAKARO_PLUGIN_TOKEN/);
    expect(() => loadConfig({ TAKARO_PLUGIN_TOKEN: 'p', VEIN_LOG_TAIL: 'maybe' })).toThrow(/auto\|always\|never/);
    expect(() => loadConfig({ TAKARO_PLUGIN_TOKEN: 'p', VEIN_LOG_EVENTS: 'loud' })).toThrow(/all\|filtered\|none/);
  });

  it('honours overrides and derives the data-dir files from the cursor file', () => {
    const c = loadConfig({
      TAKARO_PLUGIN_TOKEN: 'p',
      TAKARO_IDENTITY_TOKEN: 'x',
      TAKARO_PLUGIN_URL: 'http://h:1/',
      TAKARO_POLL_INTERVAL_MS: '250',
      TAKARO_CURSOR_FILE: '/data/event-cursor.json',
      VEIN_LOG_FILE: '/logs/Vein.log',
      VEIN_HTTP_API: 'http://127.0.0.1:9090/',
      VEIN_LOG_READY_RE: 'Created session GameSession\\.',
      VEIN_LOG_TAIL: 'always',
      VEIN_LOG_EVENTS: 'none',
      SIDECAR_HEALTH_PORT: '9999',
    });
    expect(c.identityToken).toBe('x');
    expect(c.pluginBaseUrl).toBe('http://h:1');
    expect(c.pollIntervalMs).toBe(250);
    expect(c.logFile).toBe('/logs/Vein.log');
    expect(c.httpApiUrl).toBe('http://127.0.0.1:9090');
    expect(c.logGrammarOverrides).toEqual({ readyLine: 'Created session GameSession\\.' });
    expect(c.logTailMode).toBe('always');
    expect(c.logEvents).toBe('none');
    expect(c.healthPort).toBe(9999);
    expect(c.onlineFile).toBe('/data/online-players.json');
    expect(c.banFile).toBe('/data/timed-bans.json');
  });

  it('VEIN_HTTP_API can be disabled with an empty value, and a bad grammar regex is rejected', () => {
    expect(loadConfig({ TAKARO_PLUGIN_TOKEN: 'p', VEIN_HTTP_API: '' }).httpApiUrl).toBe('');
    expect(() => loadConfig({ TAKARO_PLUGIN_TOKEN: 'p', VEIN_LOG_LEAVE_RE: '(?<' })).toThrow(/VEIN_LOG_LEAVE_RE/);
  });

  it('sender name comes from TAKARO_SENDER_NAME, then TAKARO_SERVER_CHAT_NAME', () => {
    expect(loadConfig({ TAKARO_PLUGIN_TOKEN: 'p', TAKARO_SENDER_NAME: 'Takaro' }).senderName).toBe('Takaro');
    expect(loadConfig({ TAKARO_PLUGIN_TOKEN: 'p', TAKARO_SERVER_CHAT_NAME: 'Chat' }).senderName).toBe('Chat');
    expect(loadConfig({ TAKARO_PLUGIN_TOKEN: 'p', TAKARO_SENDER_NAME: 'A', TAKARO_SERVER_CHAT_NAME: 'B' }).senderName).toBe('A');
  });
});
