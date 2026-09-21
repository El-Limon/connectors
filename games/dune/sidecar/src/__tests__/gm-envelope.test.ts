import { describe, expect, it } from 'vitest';
import {
  AmqpGmPublisher,
  EXEC_MARKER,
  ExecGmPublisher,
  GmClient,
  GmCommandError,
  addItemToInventory,
  assertErlangSafe,
  buildEnvelope,
  buildErlangPublish,
  dockerExecArgv,
  encodeEnvelope,
  kickPlayer,
  kubectlExecArgv,
  messageId,
  passthrough,
  safeLabel,
  serverShutdownBroadcast,
  serverShutdownCancel,
  serviceBroadcast,
  spawnVehicleAt,
  teleport,
} from '../dune/gm.js';
import { MockBattlegroup } from '../testing/mockBattlegroup.js';

const TOKEN = 'super-secret-server-command-token';

describe('GM envelope (golden)', () => {
  it('is version 2 with the inner command as a JSON STRING, not a nested object', () => {
    const envelope = buildEnvelope({ ServerCommand: 'KickPlayer', PlayerId: '6FF6498F4074E3DE' }, TOKEN);
    expect(envelope).toEqual({
      Version: 2,
      AuthToken: TOKEN,
      MessageContent: '{"ServerCommand":"KickPlayer","PlayerId":"6FF6498F4074E3DE"}',
    });
    // The whole point of the string: the server deserialises MessageContent separately.
    expect(typeof envelope.MessageContent).toBe('string');
    expect(JSON.parse(encodeEnvelope({ ServerCommand: 'X' }, TOKEN).toString('utf8')).Version).toBe(2);
  });
});

describe('GM command builders', () => {
  it('AddItemToInventory defaults Quantity and Durability', () => {
    expect(addItemToInventory({ playerId: 'FLS1', itemName: 'PowerPack2' })).toEqual({
      ServerCommand: 'AddItemToInventory',
      PlayerId: 'FLS1',
      ItemName: 'PowerPack2',
      Quantity: 1,
      Durability: 1.0,
    });
    expect(addItemToInventory({ playerId: '*', itemName: 'x', quantity: 5.7 }).Quantity).toBe(5);
  });

  it('TeleportToExact requires coordinates and omits absent/null camera args', () => {
    expect(teleport({ playerId: 'FLS1', x: 1, y: 2, z: 3, yaw: null, camPitch: undefined })).toEqual({
      ServerCommand: 'TeleportToExact',
      PlayerId: 'FLS1',
      X: 1,
      Y: 2,
      Z: 3,
    });
    expect(teleport({ playerId: 'FLS1', x: 1, y: 2, z: 3, yaw: 90 }, false)).toMatchObject({ ServerCommand: 'TeleportTo', Yaw: 90 });
    expect(() => teleport({ playerId: 'FLS1', x: Number.NaN, y: 2, z: 3 })).toThrow(/numeric 'X'/);
    expect(() => teleport({ playerId: '', x: 1, y: 2, z: 3 })).toThrow(/PlayerId/);
  });

  it('KickPlayer rejects an empty or newline-bearing id', () => {
    expect(kickPlayer('FLS1')).toEqual({ ServerCommand: 'KickPlayer', PlayerId: 'FLS1' });
    expect(() => kickPlayer('')).toThrow(GmCommandError);
    expect(() => kickPlayer('a\nb')).toThrow(/newlines/);
  });

  it('ServiceBroadcast Generic ships both en and en-US so an en-US client renders it', () => {
    const inner = serviceBroadcast({ title: 'Server', body: 'hello', durationSeconds: 12 });
    expect(inner.BroadcastType).toBe('Generic');
    const payload = inner.BroadcastPayload as { BroadcastDuration: number; LocalizedText: { Key: string }[] };
    expect(payload.BroadcastDuration).toBe(12);
    expect(payload.LocalizedText.map((t) => t.Key)).toEqual(['en', 'en-US']);
    expect(() => serviceBroadcast({ title: '', body: 'x' })).toThrow(/Title/);
    expect(() => serviceBroadcast({ title: 'x', body: '' })).toThrow(/Body/);
  });

  it('ServerShutdown carries a consistent timestamp pair, and cancel short-circuits it', () => {
    const inner = serverShutdownBroadcast({ leadSeconds: 300, now: new Date(1_700_000_000_000) });
    const payload = inner.BroadcastPayload as Record<string, number | string>;
    expect(payload.DateTimestamp).toBe(1_700_000_000);
    expect(payload.ShutdownTimestamp).toBe(1_700_000_300);
    expect(payload.ShutdownDuration).toBe(300);
    expect(serverShutdownCancel().BroadcastPayload).toEqual({ ShouldCancel: true });
  });

  it('SpawnVehicleAt needs a class AND a template variant', () => {
    expect(spawnVehicleAt({ playerId: 'F', className: 'Sandbike', templateName: 'T6_Combat', x: 1, y: 2, z: 3 })).toMatchObject({
      ServerCommand: 'SpawnVehicleAt',
      ClassName: 'Sandbike',
      TemplateName: 'T6_Combat',
      Persistent: 1.0,
    });
    expect(() => spawnVehicleAt({ playerId: 'F', className: 'Sandbike', templateName: '', x: 1, y: 2, z: 3 })).toThrow(/TemplateName/);
  });

  it('the passthrough accepts any well-named command and rejects an injected one', () => {
    expect(passthrough('AwardXP', { PlayerId: 'F', Experience: 10 })).toEqual({ ServerCommand: 'AwardXP', PlayerId: 'F', Experience: 10 });
    expect(() => passthrough('Award XP; DROP', {})).toThrow(/not a valid ServerCommand/);
    expect(() => passthrough('', {})).toThrow(GmCommandError);
  });
});

describe('GmClient', () => {
  it('refuses to publish without an auth token instead of silently no-opping', async () => {
    const battlegroup = new MockBattlegroup();
    const client = new GmClient({
      authToken: '',
      publisher: new AmqpGmPublisher(async () => battlegroup.channel),
      exchange: 'heartbeats',
      routingKey: 'notifications',
      userId: 'fls',
      appId: 'fls_backend',
    });
    await expect(client.send(kickPlayer('F'))).rejects.toThrow(/DUNE_GM_AUTH_TOKEN/);
    expect(battlegroup.publishes).toHaveLength(0);
  });

  it('publishes to heartbeats/notifications with the documented AMQP properties', async () => {
    const battlegroup = new MockBattlegroup();
    const client = new GmClient({
      authToken: TOKEN,
      publisher: new AmqpGmPublisher(async () => battlegroup.channel),
      exchange: 'heartbeats',
      routingKey: 'notifications',
      userId: 'fls',
      appId: 'fls_backend',
    });
    await client.send(kickPlayer('6FF6498F4074E3DE'), 1_700_000_000_000);
    expect(battlegroup.publishes).toHaveLength(1);
    const published = battlegroup.publishes[0];
    expect(published.exchange).toBe('heartbeats');
    expect(published.routingKey).toBe('notifications');
    expect(published.options).toMatchObject({
      contentType: 'Content',
      userId: 'fls',
      appId: 'fls_backend',
      messageId: 'takaro-KickPlayer-1700000000000',
    });
    expect(JSON.parse(published.body)).toMatchObject({ Version: 2, AuthToken: TOKEN });
  });

  it('message ids and exec labels are sanitised', () => {
    expect(messageId('KickPlayer', 5)).toBe('takaro-KickPlayer-5');
    expect(safeLabel('rm -rf /')).toBe('takaro');
    expect(safeLabel('KickPlayer')).toBe('KickPlayer');
  });
});

describe('exec publisher argv safety', () => {
  const injectionAttempt = '"; rm -rf / #\n$(touch /tmp/pwned)`id`';

  it('never puts caller text on the command line — everything travels base64 inside one argv element', async () => {
    const battlegroup = new MockBattlegroup();
    const publisher = new ExecGmPublisher({
      kind: 'docker-exec',
      execArgv: dockerExecArgv('docker', 'game-rmq'),
      runner: battlegroup.execRunner,
    });
    const result = await publisher.publish({
      exchange: `chat.map${injectionAttempt}`,
      routingKey: injectionAttempt,
      body: Buffer.from(JSON.stringify({ msg: injectionAttempt })),
      properties: { contentType: 'Content', userId: 'fls', appId: 'fls_backend', messageId: 'id', deliveryMode: 1 },
      label: injectionAttempt,
    });
    expect(result.ok).toBe(true);

    const call = battlegroup.execCalls[0];
    // 1. The command prefix is fixed. `eval_file /dev/stdin` was the original design and does NOT work on the
    //    shipped broker (RabbitMQ 3.13.7 cannot read a pipe there), so the expression is the LAST argv element,
    //    handed to execve as one argument. There is still no shell anywhere.
    expect(call.argv.slice(0, -1)).toEqual(['docker', 'exec', '-i', 'game-rmq', 'rabbitmqctl', 'eval']);
    expect(call.argv.some((a) => a === 'sh' || a === 'bash' || a === '-c')).toBe(false);
    expect(call.argv).not.toContain('/dev/stdin');
    expect(call.stdin).toBe('');

    // 2. The expression carries the payload only as inert base64 — no shell metacharacter survives anywhere,
    //    which is what makes a single argv element safe.
    const expr = call.argv[call.argv.length - 1];
    expect(expr).not.toContain('rm -rf');
    expect(expr).not.toContain('$(touch');
    expect(/[\\`$]/.test(expr)).toBe(false);
    expect(expr).toContain(Buffer.from(injectionAttempt, 'utf8').toString('base64'));
    // 3. the label is sanitised before it reaches the marker line.
    expect(expr).toContain('label=takaro');
  });

  it('refuses to exec an expression that is not pure base64 plus fixed literals', () => {
    // A regression guard for the argv path: if a future builder ever interpolates raw text into the Erlang, the
    // publish must fail loudly rather than ship an injection.
    expect(() => assertErlangSafe('io:format("ok").')).not.toThrow();
    expect(() => assertErlangSafe('os:cmd("rm -rf /`id`").')).toThrow(/shell metacharacters/);
    expect(() => assertErlangSafe('io:format("$(id)").')).toThrow(/shell metacharacters/);
    expect(() => assertErlangSafe('io:format("héllo").')).toThrow(/unexpected characters/);
  });

  it('decides success on the marker, not the exit code', async () => {
    const battlegroup = new MockBattlegroup();
    const publisher = new ExecGmPublisher({ kind: 'docker-exec', execArgv: ['docker', 'exec', '-i', 'c'], runner: battlegroup.execRunner });
    const request = {
      exchange: 'heartbeats',
      routingKey: 'notifications',
      body: Buffer.from('{}'),
      properties: {},
      label: 'test',
    };
    // rabbitmqctl eval exits 0 after printing an Erlang exception: that is NOT a publish.
    battlegroup.execResult = { code: 0, stdout: 'Error: {badmatch, undefined}', stderr: '' };
    expect((await publisher.publish(request)).ok).toBe(false);
    battlegroup.execResult = { code: 0, stdout: `${EXEC_MARKER} result=ok exchange=heartbeats routing=notifications label=test`, stderr: '' };
    expect((await publisher.publish(request)).ok).toBe(true);
  });

  it('builds a property tuple in AMQP basic-properties order', () => {
    const expr = buildErlangPublish({
      exchange: 'heartbeats',
      routingKey: 'notifications',
      body: Buffer.from('x'),
      properties: { contentType: 'Content', messageId: 'mid', userId: 'fls', appId: 'fls_backend', type: 'text_chat', deliveryMode: 1 },
      label: 'test',
    });
    const props = /Props = \{(.+)\},/s.exec(expr)?.[1] ?? '';
    const fields = splitTop(props);
    expect(fields).toHaveLength(15); // 'P_basic' + the 14 basic-properties fields
    expect(fields[0]).toBe('list_to_atom("P_basic")');
    expect(fields[4]).toBe('1'); // delivery_mode
    expect(fields[9]).toContain(Buffer.from('mid').toString('base64')); // message_id
    expect(fields[11]).toContain(Buffer.from('text_chat').toString('base64')); // type
    expect(fields[12]).toContain(Buffer.from('fls').toString('base64')); // user_id
    expect(fields[13]).toContain(Buffer.from('fls_backend').toString('base64')); // app_id
  });

  it('kubectl argv refuses to run without a pod, docker without a container', () => {
    expect(kubectlExecArgv('kubectl', 'dune', 'mq-0')).toEqual(['kubectl', 'exec', '-i', '-n', 'dune', 'mq-0', '--']);
    expect(() => kubectlExecArgv('kubectl', 'dune', '')).toThrow(/DUNE_K8S_POD/);
    expect(() => dockerExecArgv('docker', '')).toThrow(/DUNE_RMQ_CONTAINER/);
  });
});

/** Splits a flat Erlang tuple body on top-level commas. */
function splitTop(text: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let current = '';
  for (const ch of text) {
    if (ch === '(' || ch === '{' || ch === '[' || ch === '<') depth += 1;
    else if (ch === ')' || ch === '}' || ch === ']' || ch === '>') depth -= 1;
    if (ch === ',' && depth === 0) {
      out.push(current.trim());
      current = '';
      continue;
    }
    current += ch;
  }
  if (current.trim()) out.push(current.trim());
  return out;
}
