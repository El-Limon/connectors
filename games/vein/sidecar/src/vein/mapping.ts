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

/** SteamID64: 17 digits starting with 7656119 (Vein is a Steam-only title; identity = SteamID64). */
export const STEAMID64_RE = /^7656\d{13}$/;

export function isSteamId64(value: unknown): boolean {
  return typeof value === 'string' && STEAMID64_RE.test(value.trim());
}

function steamOf(value: unknown): string | null {
  const s = str(value);
  return s && STEAMID64_RE.test(s) ? s : null;
}

/** Strips a `steam:` platform prefix (Takaro sends `platformId` in that shape). */
export function fromPlatform(platformId: unknown): string | null {
  const s = str(platformId);
  if (!s) return null;
  const m = /^steam:(.+)$/i.exec(s);
  return m ? m[1].trim() : null;
}

/**
 * Takaro identity for Vein (campaign plan, Decision 3): `gameId` IS the SteamID64 string, `steamId` is the same value
 * and `platformId` is `steam:<id64>`. Vein has no EOS/Epic identity at all, so no EOS fields are ever set.
 * A row without a usable SteamID64 (e.g. a log-tail line that only had a name) still yields a record: the raw
 * identifier is echoed as `gameId` so Takaro's IGamePlayer validation passes (F7).
 */
export function mapPlayer(raw: unknown): TakaroPlayer {
  const p = asRecord(raw);
  const steamId = steamOf(p.steamId) ?? steamOf(p.gameId) ?? steamOf(fromPlatform(p.platformId)) ?? steamOf(fromPlatform(p.steamId));

  const gameId = steamId ?? str(p.gameId) ?? str(p.name) ?? str(p.characterName);
  if (!gameId) throw new Error(`Plugin player has no identifier: ${JSON.stringify(raw)}`);

  const player: TakaroPlayer = { gameId, name: str(p.name) ?? str(p.characterName) ?? gameId };
  if (steamId) {
    player.steamId = steamId;
    player.platformId = `steam:${steamId}`;
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
  if (['hostile', 'enemy', 'monster', 'aggressive', 'zombie', 'infected', 'undead', 'ghoul', 'raider'].some((k) => t.includes(k))) return 'hostile';
  if (['friendly', 'ally', 'npc', 'villager', 'survivor', 'companion', 'pet', 'merchant', 'trader'].some((k) => t.includes(k))) return 'friendly';
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

/**
 * VEIN chat segments -> Takaro's chat channel enum (global | team | friends | whisper).
 *
 * Lane L3e: the plugin now reports the real `EChatSegment` the chat RPC carried
 * (`all` | `local` | `global` | `radio`) instead of a hard-coded `"global"` (L6b finding 3).
 * Takaro has no proximity channel, so the two proximity-limited segments are mapped to `team`:
 * it is the closest "not everyone hears this" channel Takaro has, and - the point of the whole
 * fix - it is what makes a module's `onlyGlobalChat` setting exclude local/radio chat instead of
 * relaying it as if it were server-wide.
 *
 * Honest limit measured on VEIN 0.024h8 (2026-09-17): a message typed with the client's send
 * selector on **Local** still arrives with `EChatSegment::All`, so on this build the mapping
 * below yields `global` either way. The plugin reports what the wire carried; when VEIN starts
 * transmitting the selected segment this mapping already does the right thing.
 */
function mapChannel(raw: unknown): string {
  const c = String(raw ?? '').toLowerCase();
  if (c === 'team' || c === 'friends' || c === 'whisper') return c;
  if (c === 'local' || c === 'radio') return 'team';
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

/**
 * VEIN has pseudo-stackable items (`bStackable=false`, `bPseudoStackable=true`: corn, MRE, ...):
 * every unit is its own `FVirtualItemInstance`, so the plugin's per-instance `/inventory` returns
 * N entries of `amount: 1`. The game's own UI groups identical rows ("MRE  x 4"), and Takaro renders
 * one row per IItemDTO, so forwarding them 1:1 shows four "MRE x1" rows. Aggregate by code (and
 * quality, which is a distinct item for Takaro), summing `amount`, keeping first-appearance order
 * and the first entry's `name`. The plugin endpoint stays the detailed, per-instance source.
 */
export function aggregateInventory(items: TakaroItem[]): TakaroItem[] {
  const out: TakaroItem[] = [];
  const index = new Map<string, TakaroItem>();
  for (const item of items) {
    const key = JSON.stringify([item.code, item.quality ?? null]);
    const existing = index.get(key);
    if (existing) {
      existing.amount = (existing.amount ?? 1) + (item.amount ?? 1);
      continue;
    }
    const copy: TakaroItem = { ...item, amount: item.amount ?? 1 };
    index.set(key, copy);
    out.push(copy);
  }
  return out;
}
