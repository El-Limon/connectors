export type CapabilityState = 'ok' | 'degraded' | 'unimplemented';

export interface PluginHealth {
  status: string;
  version?: string;
  gameBuild?: string;
  engineVersion?: string;
  capabilities?: Record<string, CapabilityState | string>;
  diagnostics?: Record<string, unknown>;
}

export interface Position {
  x: number;
  y: number;
  z: number;
  dimension?: string;
}

/**
 * A player as the Vein plugin reports it (plugin docs/API.md).
 * Identity is the SteamID64 (Vein is Steam-only; `APlayerState::UniqueId` is an FUniqueNetIdSteam).
 */
export interface PluginPlayer {
  /** SteamID64 as a decimal string. */
  gameId: string;
  /** Steam persona name as the game shows it. */
  name?: string;
  /** In-game character name, when the plugin can read one (usually identical to `name`). */
  characterName?: string;
  /** Same value as `gameId`; kept so a plugin row may state it explicitly. */
  steamId?: string;
  /** `steam:<id64>`. */
  platformId?: string;
  /** Client IP, when the plugin can read it from the net connection. */
  ip?: string;
  ping?: number;
  /** Pawn exists in the world (false while still loading in). */
  spawned?: boolean;
  online?: boolean;
  position?: Position;
}

export interface PluginInventoryItem {
  code: string;
  name: string;
  amount: number;
  quality?: string | number;
}

export interface PluginEvent {
  seq: number;
  type: string;
  data: unknown;
  ts?: string | number;
}

export interface PluginEventsResponse {
  /** Per-process id of the game server; changes when the server restarts. */
  bootId?: string;
  seq: number;
  truncated?: boolean;
  events: PluginEvent[];
}

export interface PluginCommandResult {
  success: boolean;
  output?: string;
}

/** Takaro IGamePlayer. For Vein `gameId` === `steamId` === SteamID64, and `platformId` === `steam:<id64>`. */
export interface TakaroPlayer {
  gameId: string;
  name: string;
  /** Only set on a last-known record answered for a player who is not in world (see adapter.getPlayer). */
  online?: boolean;
  steamId?: string;
  platformId?: string;
  ip?: string;
  ping?: number;
}

export interface TakaroItem {
  code: string;
  name: string;
  description?: string;
  amount?: number;
  quality?: string;
}

export type TakaroEntityType = 'hostile' | 'friendly' | 'neutral';

export interface TakaroEntity {
  code: string;
  name: string;
  description?: string;
  type: TakaroEntityType;
}

export interface TakaroLocation {
  code: string;
  name: string;
  position: Position;
  radius?: number;
  sizeX?: number;
  sizeY?: number;
  sizeZ?: number;
}

export interface TakaroBan {
  player: TakaroPlayer;
  reason: string;
  expiresAt: string | null;
}
