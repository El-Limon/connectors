import path from 'node:path';

/** UE dedicated-server log inside the rig container (compose mounts `_data/vein-dev` at `/home/steam/vein`). */
export const DEFAULT_LOG_FILE = '/home/steam/vein/Vein/Saved/Logs/Vein.log';

/** Vein's own built-in read-only HTTP API (Game.ini `HTTPPort`, default 8080); used as a cross-check only. */
export const DEFAULT_HTTP_API = 'http://127.0.0.1:8080';

/** Per-deployment log-grammar overrides (VEIN_LOG_*_RE), applied on top of logTail.DEFAULT_GRAMMAR. */
export interface LogGrammarOverrides {
  loginLine?: string;
  joinLine?: string;
  leaveLine?: string;
  chatLine?: string;
  readyLine?: string;
}

export interface SidecarConfig {
  takaroWsUrl: string;
  identityToken: string;
  registrationToken: string;
  serverName: string;
  /** Name shown as the chat sender for sendMessage (TAKARO_SENDER_NAME / TAKARO_SERVER_CHAT_NAME). */
  senderName: string;
  pluginBaseUrl: string;
  pluginToken: string;
  pluginTimeoutMs: number;
  pollIntervalMs: number;
  cursorFile: string;
  onlineFile: string;
  banFile: string;
  knownPlayersFile: string;
  logFile: string;
  /**
   * Vein's built-in HTTP API (`GET /players`), read-only. Optional: '' disables it. It is a cross-check for /health
   * and a players fallback when the plugin is unreachable; it is never the identity source of truth.
   */
  httpApiUrl: string;
  logGrammarOverrides: LogGrammarOverrides;
  logTailMode: 'auto' | 'always' | 'never';
  logEvents: 'all' | 'filtered' | 'none';
  healthPort: number;
  healthHost: string;
  reconnectBaseMs: number;
  reconnectMaxMs: number;
}

type Env = Record<string, string | undefined>;

export function loadConfig(env: Env = process.env): SidecarConfig {
  const registrationToken = env.TAKARO_REGISTRATION_TOKEN?.trim() ?? '';
  const pluginToken = env.TAKARO_PLUGIN_TOKEN?.trim() ?? '';
  if (!pluginToken) throw new Error('Missing required env TAKARO_PLUGIN_TOKEN');

  const mode = (env.VEIN_LOG_TAIL || 'auto').toLowerCase();
  if (mode !== 'auto' && mode !== 'always' && mode !== 'never') {
    throw new Error(`VEIN_LOG_TAIL must be auto|always|never, got '${mode}'`);
  }

  const logEvents = (env.VEIN_LOG_EVENTS || 'filtered').toLowerCase();
  if (logEvents !== 'all' && logEvents !== 'filtered' && logEvents !== 'none') {
    throw new Error(`VEIN_LOG_EVENTS must be all|filtered|none, got '${logEvents}'`);
  }

  const cursorFile = env.TAKARO_CURSOR_FILE || './data/event-cursor.json';
  const dataDir = path.dirname(cursorFile);

  return {
    takaroWsUrl: env.TAKARO_WS_URL || 'wss://connect.takaro.io/',
    identityToken: env.TAKARO_IDENTITY_TOKEN || 'vein',
    registrationToken,
    serverName: env.TAKARO_SERVER_NAME || 'Takaro Dev Vein',
    senderName: (env.TAKARO_SENDER_NAME || env.TAKARO_SERVER_CHAT_NAME || '').trim(),
    pluginBaseUrl: (env.TAKARO_PLUGIN_URL || 'http://127.0.0.1:18890').replace(/\/+$/, ''),
    pluginToken,
    pluginTimeoutMs: int(env.TAKARO_PLUGIN_TIMEOUT_MS, 10000),
    pollIntervalMs: int(env.TAKARO_POLL_INTERVAL_MS, 1000),
    cursorFile,
    onlineFile: env.TAKARO_ONLINE_FILE || path.join(dataDir, 'online-players.json'),
    banFile: env.TAKARO_BAN_FILE || path.join(dataDir, 'timed-bans.json'),
    knownPlayersFile: env.TAKARO_KNOWN_PLAYERS_FILE || path.join(dataDir, 'known-players.json'),
    logFile: env.VEIN_LOG_FILE || DEFAULT_LOG_FILE,
    httpApiUrl: (env.VEIN_HTTP_API ?? DEFAULT_HTTP_API).trim().replace(/\/+$/, ''),
    logGrammarOverrides: grammarOverrides(env),
    logTailMode: mode,
    logEvents,
    healthPort: int(env.SIDECAR_HEALTH_PORT, 18891),
    healthHost: env.SIDECAR_HEALTH_HOST || '127.0.0.1',
    reconnectBaseMs: int(env.TAKARO_RECONNECT_BASE_MS, 2000),
    reconnectMaxMs: int(env.TAKARO_RECONNECT_MAX_MS, 60000),
  };
}

function grammarOverrides(env: Env): LogGrammarOverrides {
  const keys = { loginLine: 'VEIN_LOG_LOGIN_RE', joinLine: 'VEIN_LOG_JOIN_RE', leaveLine: 'VEIN_LOG_LEAVE_RE', chatLine: 'VEIN_LOG_CHAT_RE', readyLine: 'VEIN_LOG_READY_RE' } as const;
  const out: LogGrammarOverrides = {};
  for (const [field, key] of Object.entries(keys) as [keyof LogGrammarOverrides, string][]) {
    const value = env[key]?.trim();
    if (!value) continue;
    try {
      new RegExp(value);
    } catch (err) {
      throw new Error(`${key} is not a valid regular expression: ${(err as Error).message}`);
    }
    out[field] = value;
  }
  return out;
}

function int(value: string | undefined, fallback: number): number {
  if (value == null || value.trim() === '') return fallback;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
}
