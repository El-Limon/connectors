import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { DragonwildsLogParser, LogTailer, type LogEvent } from '../dragonwilds/logTail.js';
import type { TakaroPlayer } from '../dragonwilds/types.js';

const PUID = '0123456789abcdef0123456789abcdef';
const DCG = '41C4B04F4C9C038FEB767888BADD003F';
const CHAR = 'takarotester';

/** Verbatim lines from research/log-grammar.md (captured on the real server + client, 2026-09-16). */
const JOIN = [
  'LogNet: NotifyAcceptingConnection accepted from: 192.168.129.15:58308',
  'LogNet: Login request: ?p=cGFzc3dvcmQ=?pf=PC?cpx=1?Name=Limon userId: RedpointEOS:0123456789abcdef0123456789abcdef platform: RedpointEOS',
  'LogNet: Join request: /Game/Maps/Server/L_ServerStartup?p=cGFzc3dvcmQ=?pf=PC?cpx=1?Name=Limon?SplitscreenCount=1',
  'LogNet: Join succeeded: Limon',
  'DominionLog: [DedicatedServer] Server_SetPlayerGuid_Implementation() : Setting PlayerGuid to [Limon]',
  `LogDominionPlayerControllerBase: PlayerChar entered world [Account[XP:${PUID}] Character Name[${CHAR}] Guid[DCG:${DCG}] Type[0]]`,
  'LogDominionPlayerControllerBase: Remote Character load starting for handle[3] slot[takarotester]',
];
const LEAVE = [
  `LogDominionPlayerController: RequestGameExit : Server saving World and Player state for Account[XP:${PUID}] Character Name[${CHAR}] Guid[DCG:${DCG}] Type[0]`,
  `LogDominionPlayerController: ClientRequestDisconnect : DisconnectMe : PlayerStateSave result[true] - state saved for Account[XP:${PUID}] Character Name[${CHAR}] Guid[DCG:${DCG}] Type[0]`,
  `LogNet: UChannel::CleanUp: ChIndex == 0. Closing connection. [UChannel] ... UniqueId: RedpointEOS:${PUID}`,
];

const player: TakaroPlayer = { gameId: PUID, name: CHAR, epicOnlineServicesId: PUID, platformId: `epic:${PUID}` };
const resolve = async () => null; // no plugin: the log line is authoritative on its own

const feedAll = (p: DragonwildsLogParser, lines: string[]) => lines.flatMap((l) => p.feed(l));

describe('DragonwildsLogParser (verbatim lines from the real server)', () => {
  it('emits player-connected only from `PlayerChar entered world`, with puid + character name + DCG guid', () => {
    const p = new DragonwildsLogParser();
    expect(feedAll(p, JOIN)).toEqual([{ type: 'player-connected', gameId: PUID, name: CHAR, characterGuid: DCG }]);
  });

  it('`Join succeeded: <Name>` is the platform name and never an event', () => {
    const p = new DragonwildsLogParser();
    expect(p.feed('LogNet: Join succeeded: Limon')).toEqual([]);
    // but the Login request line does record the platform-name -> puid link
    p.feed(JOIN[1]);
    expect(p.gameIdForPlatformName('Limon')).toBe(PUID);
  });

  it('emits player-disconnected from ClientRequestDisconnect, gameId straight off the line', () => {
    const p = new DragonwildsLogParser();
    feedAll(p, JOIN);
    expect(feedAll(p, LEAVE)).toEqual([{ type: 'player-disconnected', gameId: PUID, name: CHAR }]);
  });

  it('UChannel::CleanUp is a hard-drop fallback only when no leave line was seen', () => {
    const p = new DragonwildsLogParser();
    feedAll(p, JOIN);
    expect(p.feed(LEAVE[2])).toEqual([{ type: 'player-disconnected', gameId: PUID, name: CHAR }]);
    expect(p.feed(LEAVE[2])).toEqual([]); // already gone: no duplicate
  });

  it('does not duplicate a connect for a repeated entered-world line, but reconnect works', () => {
    const p = new DragonwildsLogParser();
    expect(feedAll(p, JOIN)).toHaveLength(1);
    expect(p.feed(JOIN[5])).toEqual([]);
    expect(feedAll(p, LEAVE)).toHaveLength(1);
    expect(p.feed(JOIN[5])).toHaveLength(1);
  });

  it('tolerates a leave line without the Account[XP:] prefix by matching the character name', () => {
    const p = new DragonwildsLogParser();
    feedAll(p, JOIN);
    expect(p.feed(`LogDominionPlayerController: ClientRequestDisconnect : DisconnectMe : Character Name[${CHAR}]`)).toEqual([
      { type: 'player-disconnected', gameId: PUID, name: CHAR },
    ]);
  });

  it('handles two players, CRLF and names with spaces', () => {
    const p = new DragonwildsLogParser();
    const other = 'aa11bb22cc33dd44ee55ff6600112233';
    const out = feedAll(p, [
      `LogDominionPlayerControllerBase: PlayerChar entered world [Account[XP:${PUID}] Character Name[Sir Reginald III] Guid[DCG:${DCG}] Type[0]]\r`,
      `LogDominionPlayerControllerBase: PlayerChar entered world [Account[XP:${other}] Character Name[Second] Guid[DCG:BEEF] Type[0]]`,
      `LogDominionPlayerController: ClientRequestDisconnect : DisconnectMe : state saved for Account[XP:${other}] Character Name[Second] Guid[DCG:BEEF] Type[0]`,
    ]);
    expect(out).toEqual([
      { type: 'player-connected', gameId: PUID, name: 'Sir Reginald III', characterGuid: DCG },
      { type: 'player-connected', gameId: other, name: 'Second', characterGuid: 'BEEF' },
      { type: 'player-disconnected', gameId: other, name: 'Second' },
    ]);
  });

  it('chat is never sourced from the log (it is not logged server-side at all)', () => {
    const p = new DragonwildsLogParser();
    expect(
      feedAll(p, [
        'LogRedpointEOS: Verbose: EOS_Platform_Tick',
        `DominionLog: [DedicatedServer] Server_SetPlayerCharacterInfo_Implementation() : Setting player info to Account[XP:${PUID}] Character Name[${CHAR}] Guid[DCG:${DCG}] Type[0]`,
        'LogSpudSubsystem: Save to slot TakaroDev: Success',
        `${CHAR}: takaro-l0b-175345`,
        '',
      ]),
    ).toEqual([]);
  });
});

describe('LogTailer', () => {
  let dir: string;
  afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

  it('starts at EOF, handles partial lines and rotation, and uses the puid from the line', async () => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dw-tail-'));
    const file = path.join(dir, 'RSDragonwilds.log');
    fs.writeFileSync(file, JOIN.join('\n') + '\n'); // old session must not replay
    const events: LogEvent[] = [];
    const tailer = new LogTailer({ file, resolve, onEvent: (e) => events.push(e), intervalMs: 999999 });
    tailer.start();
    await tailer.flush();
    expect(events).toEqual([]);

    const joinText = JOIN.join('\n') + '\n';
    fs.appendFileSync(file, joinText.slice(0, 260));
    tailer.poll();
    await tailer.flush();
    expect(events).toEqual([]); // the entered-world line is still half-written
    fs.appendFileSync(file, joinText.slice(260));
    tailer.poll();
    await tailer.flush();
    expect(events).toEqual([{ type: 'player-connected', data: { player } }]);

    // rotation: server restart replaces the file
    fs.rmSync(file);
    fs.writeFileSync(file, 'LogInit: new boot\n');
    tailer.poll();
    fs.appendFileSync(file, JOIN.join('\n') + '\n' + LEAVE.join('\n') + '\n');
    tailer.poll();
    await tailer.flush();
    tailer.stop();
    expect(events.map((e) => e.type)).toEqual(['player-connected', 'player-connected', 'player-disconnected']);
  });

  it('enriches the log identity with the plugin view (steamId/ping) but keeps the character name', async () => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dw-tail2-'));
    const file = path.join(dir, 'RSDragonwilds.log');
    fs.writeFileSync(file, '');
    const events: LogEvent[] = [];
    const refs: Array<{ gameId?: string; name?: string }> = [];
    const tailer = new LogTailer({
      file,
      resolve: async (ref) => {
        refs.push(ref);
        return { gameId: PUID, name: 'Limon', epicOnlineServicesId: PUID, steamId: '76561198000000001', platformId: `epic:${PUID}`, ping: 24 };
      },
      onEvent: (e) => events.push(e),
      intervalMs: 999999,
    });
    tailer.start();
    fs.appendFileSync(file, JOIN.join('\n') + '\n');
    tailer.poll();
    await tailer.flush();
    tailer.stop();
    expect(refs).toEqual([{ gameId: PUID, name: CHAR }]);
    expect(events).toEqual([
      { type: 'player-connected', data: { player: { ...player, steamId: '76561198000000001', ping: 24 } } },
    ]);
  });

  it('a name-only leave line falls back to the identity learned at join', async () => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dw-tail3-'));
    const file = path.join(dir, 'RSDragonwilds.log');
    fs.writeFileSync(file, '');
    const events: LogEvent[] = [];
    const errors: string[] = [];
    const tailer = new LogTailer({ file, resolve, onEvent: (e) => events.push(e), onError: (e) => errors.push(e.message), intervalMs: 999999 });
    tailer.start();
    fs.appendFileSync(file, `LogDominionPlayerController: ClientRequestDisconnect : DisconnectMe : Character Name[Ghost]\n`);
    tailer.poll();
    await tailer.flush();
    expect(events).toEqual([]);
    expect(errors.join('\n')).toMatch(/no gameId for character 'Ghost'/);

    fs.appendFileSync(file, JOIN.join('\n') + `\nLogDominionPlayerController: ClientRequestDisconnect : DisconnectMe : Character Name[${CHAR}]\n`);
    tailer.poll();
    await tailer.flush();
    tailer.stop();
    expect(events.map((e) => [e.type, e.data.player.gameId])).toEqual([
      ['player-connected', PUID],
      ['player-disconnected', PUID],
    ]);
  });
});
