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
 * A player as the Dragonwilds plugin reports it (plugin docs/API.md).
 * Identity is the EOS ProductUserId (32 lowercase hex, shown at the bottom of the in-game Settings screen).
 * `steamId` is only present when the account is linked to Steam and the plugin could read the SteamID64.
 */
export interface PluginPlayer {
  /** EOS ProductUserId, 32 lowercase hex. */
  gameId: string;
  /** Platform display name (Steam/Epic persona). */
  name?: string;
  /** In-game character name (what the join/leave log lines carry). */
  characterName?: string;
  epicOnlineServicesId?: string;
  steamId?: string;
  platformId?: string;
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

/** Takaro IGamePlayer. gameId is the EOS ProductUserId for Dragonwilds. */
export interface TakaroPlayer {
  gameId: string;
  name: string;
  /** Only set on a last-known record answered for a player who is not in world (see adapter.getPlayer). */
  online?: boolean;
  epicOnlineServicesId?: string;
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
