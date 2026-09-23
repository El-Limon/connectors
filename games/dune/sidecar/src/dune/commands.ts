import type { TakaroBan, TakaroPlayer } from './types.js';

/**
 * `executeConsoleCommand` for a game with no console.
 *
 * Dune's `ServerExec` and `CheatScript` GM commands are live-tested no-ops, so there is no raw command line to
 * forward to. Instead the connector exposes its own small, documented command set over the same action, plus a
 * `gm <ServerCommand> <json>` passthrough for anything the server understands that we have not wrapped.
 *
 * An unknown command answers `{success:false}` — never an error frame: Takaro expects a CommandOutput payload here
 * and turns an error frame into a user-visible 400 "the gameserver responded with bad data".
 */
export interface CommandResult {
  success: boolean;
  rawResult: string;
  errorMessage: string | null;
}

export interface CommandContext {
  players(): Promise<TakaroPlayer[]>;
  say(msg: string): Promise<string>;
  whisper(playerId: string, msg: string): Promise<string>;
  broadcast(title: string, body: string): Promise<string>;
  give(playerId: string, item: string, quantity: number): Promise<string>;
  teleport(playerId: string, x: number, y: number, z: number): Promise<string>;
  kick(playerId: string, reason?: string): Promise<string>;
  ban(playerId: string, reason: string | null, expiresAt: string | null): Promise<string>;
  unban(playerId: string): Promise<string>;
  listBans(): Promise<TakaroBan[]>;
  spawnVehicle(playerId: string, className: string, templateName: string, x: number, y: number, z: number): Promise<string>;
  shutdown(): Promise<string>;
  gm(command: string, payload: unknown): Promise<string>;
}

export const COMMANDS = [
  'help',
  'players',
  'say',
  'whisper',
  'broadcast',
  'give',
  'tp',
  'kick',
  'ban',
  'unban',
  'bans',
  'spawnvehicle',
  'shutdown',
  'gm',
] as const;

export const HELP_TEXT = [
  'Takaro Dune connector commands:',
  '  help                                   this list',
  '  players                                online players (gameId = FLS id)',
  '  say <message>                          server-wide chat message',
  '  whisper <playerId> <message>           private message to one player',
  '  broadcast <title> | <body>             on-screen ServiceBroadcast',
  '  give <playerId> <item> [quantity]      AddItemToInventory, read back from the DB',
  '  tp <playerId> <x> <y> <z>              TeleportToExact',
  '  kick <playerId> [reason]               KickPlayer',
  '  ban <playerId> [reason] [expiresAt]    connector-enforced ban (kick-on-sight)',
  '  unban <playerId>                       lift a connector ban',
  '  bans                                   list connector bans',
  '  spawnvehicle <playerId> <class> <template> <x> <y> <z>',
  '  shutdown                               shutdown notice, then the configured stop hook',
  '  gm <ServerCommand> <json>              raw server-command passthrough',
].join('\n');

export interface Token {
  value: string;
  /** Offset just past this token in the original line, so a trailing free-text/JSON argument can be sliced exactly. */
  end: number;
}

/**
 * Splits a command line, honouring double quotes so a message can contain spaces. The spans matter: slicing a JSON
 * payload by `token.length` breaks the moment a token was quoted, because the quotes are not in the value.
 */
export function tokenizeSpans(line: string): Token[] {
  const out: Token[] = [];
  const re = /"([^"]*)"|(\S+)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(line)) !== null) out.push({ value: m[1] ?? m[2], end: re.lastIndex });
  return out;
}

export function tokenize(line: string): string[] {
  return tokenizeSpans(line).map((t) => t.value);
}

function fail(message: string): CommandResult {
  return { success: false, rawResult: '', errorMessage: message };
}

function ok(output: string): CommandResult {
  return { success: true, rawResult: output, errorMessage: null };
}

function number(token: string | undefined, name: string): number {
  const n = Number(token);
  if (!Number.isFinite(n)) throw new Error(`'${name}' must be a number`);
  return n;
}

export async function runConsoleCommand(raw: string, ctx: CommandContext): Promise<CommandResult> {
  const line = (raw ?? '').trim();
  if (!line) return fail('Empty command. Try `help`.');
  const spans = tokenizeSpans(line);
  const tokens = spans.map((t) => t.value);
  const name = tokens[0].toLowerCase();
  const rest = line.slice(spans[0].end).trim();
  /** Everything after the Nth token, verbatim (used for free text and JSON payloads). */
  const after = (index: number): string => line.slice(spans[index]?.end ?? line.length).trim();

  try {
    switch (name) {
      case 'help':
        return ok(HELP_TEXT);
      case 'players': {
        const players = await ctx.players();
        if (!players.length) return ok('No players online.');
        return ok(players.map((p) => `${p.name} (${p.gameId})${p.steamId ? ` steam:${p.steamId}` : ''}`).join('\n'));
      }
      case 'say': {
        if (!rest) return fail('Usage: say <message>');
        return ok(await ctx.say(rest));
      }
      case 'whisper': {
        if (tokens.length < 3) return fail('Usage: whisper <playerId> <message>');
        return ok(await ctx.whisper(tokens[1], after(1).replace(/^"|"$/g, '')));
      }
      case 'broadcast': {
        if (!rest) return fail('Usage: broadcast <title> | <body>');
        const [title, ...body] = rest.split('|');
        const bodyText = body.join('|').trim();
        // A broadcast with no body would be rejected by the server's own parser (Generic needs Title AND Body).
        return ok(await ctx.broadcast(title.trim() || 'Server', bodyText || title.trim()));
      }
      case 'give': {
        if (tokens.length < 3) return fail('Usage: give <playerId> <item> [quantity]');
        const quantity = tokens[3] === undefined ? 1 : number(tokens[3], 'quantity');
        return ok(await ctx.give(tokens[1], tokens[2], quantity));
      }
      case 'tp': {
        if (tokens.length < 5) return fail('Usage: tp <playerId> <x> <y> <z>');
        return ok(await ctx.teleport(tokens[1], number(tokens[2], 'x'), number(tokens[3], 'y'), number(tokens[4], 'z')));
      }
      case 'kick': {
        if (tokens.length < 2) return fail('Usage: kick <playerId> [reason]');
        return ok(await ctx.kick(tokens[1], tokens.slice(2).join(' ') || undefined));
      }
      case 'ban': {
        if (tokens.length < 2) return fail('Usage: ban <playerId> [reason] [expiresAt]');
        const last = tokens[tokens.length - 1];
        const hasExpiry = tokens.length > 2 && Number.isFinite(Date.parse(last));
        const reason = tokens.slice(2, hasExpiry ? -1 : undefined).join(' ') || null;
        return ok(await ctx.ban(tokens[1], reason, hasExpiry ? new Date(last).toISOString() : null));
      }
      case 'unban': {
        if (tokens.length < 2) return fail('Usage: unban <playerId>');
        return ok(await ctx.unban(tokens[1]));
      }
      case 'bans': {
        const bans = await ctx.listBans();
        if (!bans.length) return ok('No bans.');
        return ok(bans.map((b) => `${b.player.name} (${b.player.gameId}) ${b.expiresAt ?? 'permanent'}${b.reason ? ` — ${b.reason}` : ''}`).join('\n'));
      }
      case 'spawnvehicle': {
        if (tokens.length < 7) return fail('Usage: spawnvehicle <playerId> <class> <template> <x> <y> <z>');
        return ok(
          await ctx.spawnVehicle(tokens[1], tokens[2], tokens[3], number(tokens[4], 'x'), number(tokens[5], 'y'), number(tokens[6], 'z')),
        );
      }
      case 'shutdown':
        return ok(await ctx.shutdown());
      case 'gm': {
        if (tokens.length < 2) return fail('Usage: gm <ServerCommand> <json>');
        const jsonText = after(1);
        let payload: unknown = {};
        if (jsonText) {
          try {
            payload = JSON.parse(jsonText);
          } catch (err) {
            return fail(`gm payload is not valid JSON: ${(err as Error).message}`);
          }
        }
        return ok(await ctx.gm(tokens[1], payload));
      }
      default:
        return fail(`Unknown command '${tokens[0]}'. Known commands: ${COMMANDS.join(' ')}`);
    }
  } catch (err) {
    // A command that fails in the game is a COMMAND failure, not a transport failure.
    return fail(err instanceof Error ? err.message : String(err));
  }
}
