import fs from 'node:fs';
import path from 'node:path';
import type { TakaroPlayer } from './types.js';

/**
 * Last-known player records, persisted next to the event cursor.
 *
 * Takaro calls `getPlayer {gameId}` for players who are NOT in world (running a `commandTrigger`, for example) and
 * rejects both `{}` ("property gameId ... isString") and `null` ("No payload provided but expected DTO: IGamePlayer")
 * with a user-visible 400 "the gameserver responded with bad data" (finding F7). The connector therefore has to
 * answer a real record, which means remembering players across a sidecar restart.
 */
export interface KnownPlayerStore {
  load(): TakaroPlayer[];
  save(players: TakaroPlayer[]): void;
}

export class FileKnownPlayerStore implements KnownPlayerStore {
  constructor(private readonly file: string) {}

  load(): TakaroPlayer[] {
    try {
      const parsed = JSON.parse(fs.readFileSync(this.file, 'utf8')) as unknown;
      if (!Array.isArray(parsed)) return [];
      return parsed.filter((p): p is TakaroPlayer => typeof p?.gameId === 'string' && typeof p?.name === 'string');
    } catch {
      return [];
    }
  }

  save(players: TakaroPlayer[]): void {
    fs.mkdirSync(path.dirname(path.resolve(this.file)), { recursive: true });
    const tmp = `${this.file}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(players));
    fs.renameSync(tmp, this.file);
  }
}

export class MemoryKnownPlayerStore implements KnownPlayerStore {
  constructor(public players: TakaroPlayer[] = []) {}
  load(): TakaroPlayer[] {
    return this.players.map((p) => ({ ...p }));
  }
  save(players: TakaroPlayer[]): void {
    this.players = players.map((p) => ({ ...p }));
  }
}
