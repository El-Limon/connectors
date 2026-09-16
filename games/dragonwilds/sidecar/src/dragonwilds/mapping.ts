import { asRecord, isGameEventType, type GameEventType } from '../takaro/protocol.js';
import type {
  PluginInventoryItem,
  Position,
  TakaroBan,
  TakaroEntity,
  TakaroEntityType,
  TakaroItem,
  TakaroLocation,
  TakaroPlayer,
} from './types.js';

/** EOS ProductUserId: 32 hex characters (the plugin lower-cases them; we accept either case). */
export const PUID_RE = /^[0-9a-f]{32}$/i;
const STEAMID64_RE = /^7656\d{13}$/;

export function isPuid(value: unknown): boolean {
  return typeof value === 'string' && PUID_RE.test(value.trim());
}

function puidOf(value: unknown): string | null {
  const s = str(value);
  return s && PUID_RE.test(s) ? s.toLowerCase() : null;
}

function steamOf(value: unknown): string | null {
  const s = str(value);
  return s && STEAMID64_RE.test(s) ? s : null;
}

function fromPlatform(platformId: string | null, prefix: 'epic' | 'steam'): string | null {
  if (!platformId) return null;
  const m = new RegExp(`^${prefix}:(.+)$`, 'i').exec(platformId);
  return m ? m[1] : null;
}

/**
 * Takaro gameId for Dragonwilds is the EOS ProductUserId (stable across sessions and platforms).
 * `platformId` follows what the plugin reports as the account's primary platform: `epic:<puid>` normally,
 * `steam:<id64>` when the plugin says the Steam id is primary (no EOS id available).
 */
export function mapPlayer(raw: unknown): TakaroPlayer {
  const p = asRecord(raw);
  const platformRaw = str(p.platformId);
  const puid =
    puidOf(p.gameId) ??
    puidOf(p.epicOnlineServicesId) ??
    puidOf(fromPlatform(platformRaw, 'epic')) ??
    puidOf(p.productUserId) ??
    null;
  const steamId = steamOf(p.steamId) ?? steamOf(fromPlatform(platformRaw, 'steam')) ?? (puid ? null : steamOf(p.gameId));
  const steamPrimary = !puid && Boolean(steamId);

  const gameId = puid ?? steamId ?? str(p.gameId) ?? str(p.characterName) ?? str(p.name);
  if (!gameId) throw new Error(`Plugin player has no identifier: ${JSON.stringify(raw)}`);

  const player: TakaroPlayer = { gameId, name: str(p.characterName) ?? str(p.name) ?? gameId };
  if (puid) {
    player.epicOnlineServicesId = puid;
    player.platformId = `epic:${puid}`;
  }
  if (steamId) {
    player.steamId = steamId;
    if (steamPrimary || !puid) player.platformId = `steam:${steamId}`;
  }
  if (str(p.ip)) player.ip = str(p.ip)!;
  if (typeof p.ping === 'number' && Number.isFinite(p.ping)) player.ping = p.ping;
  return player;
}

export function mapPosition(raw: unknown): Position {
  const p = asRecord(raw);
  const x = num(p.x);
  const y = num(p.y);
  const z = num(p.z);
  if (x === null || y === null || z === null) {
    throw new Error(`Plugin returned an invalid position: ${JSON.stringify(raw)}`);
  }
  const pos: Position = { x, y, z };
  if (str(p.dimension)) pos.dimension = str(p.dimension)!;
  return pos;
}

export function mapInventoryItem(raw: PluginInventoryItem | unknown): TakaroItem {
  const i = asRecord(raw);
  const code = str(i.code) ?? str(i.name);
  if (!code) throw new Error(`Plugin inventory item has no code: ${JSON.stringify(raw)}`);
  const item: TakaroItem = { code, name: str(i.name) ?? code, amount: num(i.amount) ?? 1 };
  if (i.quality !== undefined && i.quality !== null && i.quality !== '') item.quality = String(i.quality);
  return item;
}

export function mapItemDefinition(raw: unknown): TakaroItem {
  const i = asRecord(raw);
  const code = str(i.code) ?? str(i.name);
  if (!code) throw new Error(`Plugin item has no code: ${JSON.stringify(raw)}`);
  const item: TakaroItem = { code, name: str(i.name) ?? code };
  if (str(i.description)) item.description = str(i.description)!;
  return item;
}

export function mapEntityType(raw: unknown): TakaroEntityType {
  const t = String(raw ?? '').toLowerCase();
  if (['hostile', 'enemy', 'monster', 'aggressive', 'goblin', 'troll', 'dragon', 'undead'].some((k) => t.includes(k))) return 'hostile';
  if (['friendly', 'ally', 'npc', 'villager', 'survivor', 'companion', 'pet', 'merchant'].some((k) => t.includes(k))) return 'friendly';
  return 'neutral';
}

export function mapEntity(raw: unknown): TakaroEntity {
  const e = asRecord(raw);
  const code = str(e.code) ?? str(e.name);
  if (!code) throw new Error(`Plugin entity has no code: ${JSON.stringify(raw)}`);
  const entity: TakaroEntity = { code, name: str(e.name) ?? code, type: mapEntityType(e.type) };
  if (str(e.description)) entity.description = str(e.description)!;
  return entity;
}

export function mapLocation(raw: unknown): TakaroLocation {
  const l = asRecord(raw);
  const code = str(l.code) ?? str(l.name);
  if (!code) throw new Error(`Plugin location has no code: ${JSON.stringify(raw)}`);
  const position = mapPosition(l.position ?? l);
  const loc: TakaroLocation = { code, name: str(l.name) ?? code, position };
  for (const key of ['radius', 'sizeX', 'sizeY', 'sizeZ'] as const) {
    const v = num(l[key]);
    if (v !== null) loc[key] = v;
  }
  return loc;
}

export function mapBan(raw: unknown): TakaroBan {
  const b = asRecord(raw);
  const playerSource = Object.keys(asRecord(b.player)).length ? b.player : b;
  const expires = b.expiresAt;
  return {
    player: mapPlayer(playerSource),
    reason: str(b.reason) ?? '',
    expiresAt: typeof expires === 'string' && expires ? expires : typeof expires === 'number' ? new Date(expires).toISOString() : null,
  };
}

export interface MappedEvent {
  type: GameEventType;
  data: Record<string, unknown>;
}

/**
 * ISO-8601 for a plugin event `ts` (ISO string or epoch ms). Takaro's BaseEvent carries a `timestamp`; stamping every
 * event with the moment the *game* produced it keeps replayed events (pending queue after a Takaro outage) in their
 * original order and at their original time instead of the reconnect time.
 */
export function eventTimestamp(ts: unknown): string | null {
  if (typeof ts === 'number' && Number.isFinite(ts) && ts > 0) return new Date(ts).toISOString();
  const s = str(ts);
  if (!s) return null;
  const parsed = Date.parse(s);
  return Number.isFinite(parsed) ? new Date(parsed).toISOString() : null;
}

/** Normalises a plugin /events entry into a Takaro gameEvent payload. Returns null for unknown types. */
export function mapPluginEvent(event: { type: string; data: unknown; ts?: unknown }): MappedEvent | null {
  const mapped = mapPluginEventBody(event);
  if (!mapped) return null;
  const timestamp = eventTimestamp(event.ts);
  if (timestamp) mapped.data.timestamp = timestamp;
  return mapped;
}

function mapPluginEventBody(event: { type: string; data: unknown; ts?: unknown }): MappedEvent | null {
  if (!isGameEventType(event.type)) return null;
  const d = asRecord(event.data);
  const withPlayer = (): Record<string, unknown> => {
    const source = Object.keys(asRecord(d.player)).length ? d.player : d;
    return { player: mapPlayer(source) };
  };

  switch (event.type) {
    case 'player-connected':
    case 'player-disconnected':
      return { type: event.type, data: withPlayer() };
    case 'chat-message': {
      const msg = str(d.msg) ?? str(d.message) ?? str(d.text) ?? '';
      const out: Record<string, unknown> = { msg, channel: mapChannel(d.channel) };
      if (Object.keys(asRecord(d.player)).length) out.player = mapPlayer(d.player);
      return { type: event.type, data: out };
    }
    case 'player-death': {
      const out = withPlayer();
      if (Object.keys(asRecord(d.attacker)).length) {
        try {
          out.attacker = mapPlayer(d.attacker);
        } catch {
          /* non-player attacker */
        }
      }
      if (d.position) out.position = mapPosition(d.position);
      // Takaro's EventPlayerDeath only has a player `attacker`; a creature/NPC killer goes into the base `msg` field.
      const killerEntity = str(d.killerEntity) ?? str(asRecord(d.killer).code);
      if (!out.attacker && killerEntity) {
        const who = (out.player as TakaroPlayer).name;
        out.msg = `${who} was killed by ${killerEntity}`;
      }
      return { type: event.type, data: out };
    }
    case 'entity-killed': {
      const out = withPlayer();
      out.entity = str(d.entity) ?? str(asRecord(d.entity).code) ?? 'unknown';
      out.weapon = str(d.weapon) ?? '';
      return { type: event.type, data: out };
    }
    case 'log':
      return {
        type: 'log',
        data: {
          msg: str(d.msg) ?? str(d.message) ?? str(d.line) ?? (typeof event.data === 'string' ? event.data : JSON.stringify(event.data)),
        },
      };
  }
}

function mapChannel(raw: unknown): string {
  const c = String(raw ?? '').toLowerCase();
  if (c === 'team' || c === 'friends' || c === 'whisper') return c;
  return 'global';
}

export function str(value: unknown): string | null {
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  if (typeof value !== 'string') return null;
  const t = value.trim();
  return t || null;
}

export function num(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value))) return Number(value);
  return null;
}
