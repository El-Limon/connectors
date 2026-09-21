import { spawn } from 'node:child_process';
import { logger } from '../logger.js';
import { asRecord } from '../takaro/protocol.js';
import { num, str } from './identity.js';

/**
 * Dune has no RCON. The admin seam is a "server command": a JSON envelope published to the GAME RabbitMQ, which every
 * map process consumes. The envelope is version 2 and carries the inner command as a *string*, not as a nested object:
 *
 *   {"Version":2,"AuthToken":"<ServerCommandsAuthToken>","MessageContent":"{\"ServerCommand\":\"KickPlayer\",…}"}
 *
 * Both gates must be set on the map process for it to act on one: `server.NotificationSystem.Enabled=true` and
 * `[FuncomLiveServices] ServerCommandsAuthToken`. The server logs `Server command received` → `Now running
 * ServerCommand`, or `Invalid Auth Token` / `Invalid Sender ID`, which is the independent oracle for every mutation.
 */
export const ENVELOPE_VERSION = 2;

export interface GmEnvelope {
  Version: number;
  AuthToken: string;
  MessageContent: string;
}

export function buildEnvelope(inner: Record<string, unknown>, authToken: string): GmEnvelope {
  return { Version: ENVELOPE_VERSION, AuthToken: authToken, MessageContent: JSON.stringify(inner) };
}

export function encodeEnvelope(inner: Record<string, unknown>, authToken: string): Buffer {
  return Buffer.from(JSON.stringify(buildEnvelope(inner, authToken)), 'utf8');
}

export class GmCommandError extends Error {}

function require<T>(value: T | null | undefined, field: string, command: string): T {
  if (value === null || value === undefined || value === '') throw new GmCommandError(`${command} requires '${field}'`);
  return value;
}

function finiteNumber(value: unknown, field: string, command: string): number {
  const n = num(value);
  if (n === null) throw new GmCommandError(`${command} requires a numeric '${field}'`);
  return n;
}

/** `PlayerId` accepts a single id or the literal `*` (all online players); anything else must be a non-empty string. */
export function validatePlayerId(value: unknown, command: string): string {
  const id = str(value);
  if (!id) throw new GmCommandError(`${command} requires 'PlayerId'`);
  if (id !== '*' && /[\r\n]/.test(id)) throw new GmCommandError(`${command}: 'PlayerId' must not contain newlines`);
  return id;
}

// ---------------------------------------------------------------------------
// Command builders. Each returns the INNER payload; the caller wraps it.
// ---------------------------------------------------------------------------

export function addItemToInventory(args: { playerId: string; itemName: string; quantity?: number; durability?: number }): Record<string, unknown> {
  return {
    ServerCommand: 'AddItemToInventory',
    PlayerId: validatePlayerId(args.playerId, 'AddItemToInventory'),
    ItemName: require(str(args.itemName), 'ItemName', 'AddItemToInventory'),
    Quantity: Math.max(1, Math.trunc(num(args.quantity) ?? 1)),
    Durability: num(args.durability) ?? 1.0,
  };
}

export interface TeleportArgs {
  playerId: string;
  x: number;
  y: number;
  z: number;
  yaw?: number | null;
  camPitch?: number | null;
  camYaw?: number | null;
  camRoll?: number | null;
}

/** `TeleportToExact` places the player at the literal coordinates; `TeleportTo` snaps to a safe spot first. */
export function teleport(args: TeleportArgs, exact = true): Record<string, unknown> {
  const command = exact ? 'TeleportToExact' : 'TeleportTo';
  const out: Record<string, unknown> = {
    ServerCommand: command,
    PlayerId: validatePlayerId(args.playerId, command),
    X: finiteNumber(args.x, 'X', command),
    Y: finiteNumber(args.y, 'Y', command),
    Z: finiteNumber(args.z, 'Z', command),
  };
  // Takaro modules send explicit JSON `null` for optional args; null and absent both mean "leave it out" here.
  for (const [key, value] of [
    ['Yaw', args.yaw],
    ['CamPitch', args.camPitch],
    ['CamYaw', args.camYaw],
    ['CamRoll', args.camRoll],
  ] as const) {
    const n = num(value);
    if (n !== null) out[key] = n;
  }
  return out;
}

export function kickPlayer(playerId: string): Record<string, unknown> {
  return { ServerCommand: 'KickPlayer', PlayerId: validatePlayerId(playerId, 'KickPlayer') };
}

export interface BroadcastArgs {
  title: string;
  body: string;
  durationSeconds?: number;
}

/**
 * A Generic `ServiceBroadcast` is the server-wide toast. Its text is a *localised* table: the client picks the entry
 * whose `Key` matches its locale, so an English-only payload has to ship both `en` and `en-US` or an en-US client
 * renders nothing.
 */
export function serviceBroadcast(args: BroadcastArgs): Record<string, unknown> {
  const title = require(str(args.title), 'Title', 'ServiceBroadcast');
  const body = require(str(args.body), 'Body', 'ServiceBroadcast');
  const duration = Math.max(1, Math.trunc(num(args.durationSeconds) ?? 30));
  return {
    ServerCommand: 'ServiceBroadcast',
    BroadcastType: 'Generic',
    BroadcastPayload: {
      BroadcastDuration: duration,
      LocalizedText: [
        { Key: 'en', Title: title, Body: body },
        { Key: 'en-US', Title: title, Body: body },
      ],
    },
  };
}

export type ShutdownKind = 'Restart' | 'Maintenance' | 'Update';

export interface ShutdownArgs {
  shutdownType?: ShutdownKind;
  /** Seconds until the shutdown fires. */
  leadSeconds?: number;
  /** How often the countdown re-broadcasts. */
  frequencySeconds?: number;
  /** How long each on-screen pulse stays up. */
  broadcastDurationSeconds?: number;
  now?: Date;
}

export function serverShutdownBroadcast(args: ShutdownArgs = {}): Record<string, unknown> {
  const lead = Math.max(1, Math.trunc(num(args.leadSeconds) ?? 600));
  const now = Math.floor((args.now ?? new Date()).getTime() / 1000);
  return {
    ServerCommand: 'ServiceBroadcast',
    BroadcastType: 'ServerShutdown',
    BroadcastPayload: {
      ShutdownType: args.shutdownType ?? 'Restart',
      DateTimestamp: now,
      ShutdownDuration: lead,
      ShutdownTimestamp: now + lead,
      BroadcastFrequency: Math.max(1, Math.trunc(num(args.frequencySeconds) ?? 60)),
      BroadcastDuration: Math.max(1, Math.trunc(num(args.broadcastDurationSeconds) ?? 30)),
    },
  };
}

/** `ShouldCancel` short-circuits the parser: no other shutdown metadata is required or read. */
export function serverShutdownCancel(): Record<string, unknown> {
  return { ServerCommand: 'ServiceBroadcast', BroadcastType: 'ServerShutdown', BroadcastPayload: { ShouldCancel: true } };
}

export interface SpawnVehicleArgs {
  playerId: string;
  className: string;
  templateName: string;
  x: number;
  y: number;
  z: number;
  rotation?: number | null;
  persistent?: number | null;
  faction?: string | null;
}

export function spawnVehicleAt(args: SpawnVehicleArgs): Record<string, unknown> {
  const out: Record<string, unknown> = {
    ServerCommand: 'SpawnVehicleAt',
    PlayerId: validatePlayerId(args.playerId, 'SpawnVehicleAt'),
    ClassName: require(str(args.className), 'ClassName', 'SpawnVehicleAt'),
    TemplateName: require(str(args.templateName), 'TemplateName', 'SpawnVehicleAt'),
    X: finiteNumber(args.x, 'X', 'SpawnVehicleAt'),
    Y: finiteNumber(args.y, 'Y', 'SpawnVehicleAt'),
    Z: finiteNumber(args.z, 'Z', 'SpawnVehicleAt'),
    Persistent: num(args.persistent) ?? 1.0,
  };
  const rotation = num(args.rotation);
  if (rotation !== null) out.Rotation = rotation;
  const faction = str(args.faction);
  if (faction) out.Faction = faction;
  return out;
}

/**
 * `gm <ServerCommand> <json>` passthrough for `executeConsoleCommand`. Anything the operator sends is validated only
 * as far as "it is a JSON object and names a command"; the server's own reply (in its log) is the oracle.
 */
export function passthrough(commandName: string, payload: unknown): Record<string, unknown> {
  const name = str(commandName);
  if (!name) throw new GmCommandError('A GM passthrough needs a ServerCommand name');
  if (!/^[A-Za-z][A-Za-z0-9_]*$/.test(name)) throw new GmCommandError(`'${name}' is not a valid ServerCommand name`);
  const body = asRecord(payload);
  if (payload !== undefined && payload !== null && !Object.keys(body).length && typeof payload !== 'object') {
    throw new GmCommandError('A GM passthrough payload must be a JSON object');
  }
  return { ServerCommand: name, ...body };
}

// ---------------------------------------------------------------------------
// Publishers
// ---------------------------------------------------------------------------

export interface PublishProperties {
  contentType?: string;
  messageId?: string;
  userId?: string;
  appId?: string;
  type?: string;
  deliveryMode?: 1 | 2;
}

export interface PublishRequest {
  exchange: string;
  routingKey: string;
  body: Buffer;
  properties: PublishProperties;
  /** Short, `[A-Za-z][A-Za-z0-9_-]{0,63}` label that shows up in the broker-side marker line. */
  label: string;
}

export interface PublishResult {
  ok: boolean;
  /** Broker/exec output, already redacted by the logger when logged. */
  output: string;
}

export interface GmPublisher {
  readonly kind: string;
  publish(request: PublishRequest): Promise<PublishResult>;
  close?(): Promise<void>;
}

const LABEL_RE = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;

export function safeLabel(label: string): string {
  return LABEL_RE.test(label) ? label : 'takaro';
}

export function messageId(command: string, now = Date.now()): string {
  return `takaro-${safeLabel(command)}-${now}`;
}

/** Minimal surface of an amqplib confirm/plain channel, so tests can inject a recorder. */
export interface PublishChannel {
  publish(exchange: string, routingKey: string, content: Buffer, options?: Record<string, unknown>): boolean;
}

/**
 * Publishes straight over AMQP. Cleanest option — no docker socket, no kubectl — but it only works if the sidecar can
 * authenticate to the game broker as the user named in `user_id`: RabbitMQ validates `user_id` against the
 * authenticated user and rejects the message otherwise, which is exactly why the community tools shell into the broker
 * instead. Whether an `fls` login can be provisioned on our rig is a live probe.
 */
export class AmqpGmPublisher implements GmPublisher {
  readonly kind = 'amqp';

  constructor(private readonly channel: () => Promise<PublishChannel>) {}

  async publish(request: PublishRequest): Promise<PublishResult> {
    const channel = await this.channel();
    const ok = channel.publish(request.exchange, request.routingKey, request.body, {
      contentType: request.properties.contentType,
      messageId: request.properties.messageId,
      userId: request.properties.userId,
      appId: request.properties.appId,
      type: request.properties.type,
      deliveryMode: request.properties.deliveryMode,
    });
    return { ok, output: ok ? `published to ${request.exchange}/${request.routingKey}` : 'channel refused the publish (write buffer full)' };
  }
}

/**
 * Builds the Erlang term `rabbitmqctl eval` runs inside the broker to publish AS the broker itself, bypassing the
 * `user_id` check. Everything variable is passed in base64 and decoded inside Erlang, so no caller-supplied byte ever
 * becomes Erlang syntax — the expression below contains only `[A-Za-z0-9+/=]` from us plus fixed literals.
 *
 * The property term is the `#'P_basic'{}` record written as a raw tuple, because `rabbitmqctl eval` has no record
 * definitions loaded. Field order is the AMQP basic-properties order:
 *   {'P_basic', content_type, content_encoding, headers, delivery_mode, priority, correlation_id, reply_to,
 *               expiration, message_id, timestamp, type, user_id, app_id, cluster_id}
 */
export const EXEC_MARKER = 'TAKARO_PUBLISH';

export function buildErlangPublish(request: PublishRequest): string {
  const b64 = (value: string | undefined): string => Buffer.from(value ?? '', 'utf8').toString('base64');
  const optional = (value: string | undefined): string => (value ? `base64:decode(<<"${b64(value)}">>)` : 'undefined');
  const deliveryMode = request.properties.deliveryMode === 2 ? '2' : request.properties.deliveryMode === 1 ? '1' : 'undefined';
  return [
    `Body = base64:decode(<<"${request.body.toString('base64')}">>),`,
    `Exchange = base64:decode(<<"${b64(request.exchange)}">>),`,
    `RoutingKey = base64:decode(<<"${b64(request.routingKey)}">>),`,
    `XName = rabbit_misc:r(<<"/">>, exchange, Exchange),`,
    `X = rabbit_exchange:lookup_or_die(XName),`,
    `Props = {list_to_atom("P_basic"), ${optional(request.properties.contentType)}, undefined, [], ${deliveryMode}, ` +
      `undefined, undefined, undefined, undefined, ${optional(request.properties.messageId)}, undefined, ` +
      `${optional(request.properties.type)}, ${optional(request.properties.userId)}, ${optional(request.properties.appId)}, undefined},`,
    `Content = rabbit_basic:build_content(Props, Body),`,
    `{ok, Msg} = rabbit_basic:message(XName, RoutingKey, Content),`,
    `Outcome = rabbit_queue_type:publish_at_most_once(X, Msg),`,
    `io:format("${EXEC_MARKER} result=~p exchange=~s routing=~s label=${safeLabel(request.label)}~n", [Outcome, Exchange, RoutingKey]).`,
  ].join('\n');
}

/**
 * Everything `buildErlangPublish` can emit: fixed Erlang literals plus base64. Nothing caller-supplied survives
 * outside a `base64:decode(<<"…">>)`, so the whole expression is safe to hand to `execve` as ONE argv element —
 * which is what the exec publisher now does (see `ExecGmPublisher.argvFor`). This is asserted, not assumed: if a
 * future builder ever interpolates raw text the publish fails loudly instead of shipping an injection.
 */
const ERLANG_SAFE_RE = /^[A-Za-z0-9_ \t\n,.:;<>"'{}\[\]()=~|+/@%-]*$/;

export function assertErlangSafe(expr: string): string {
  // The named check first, so the error says WHY: a base64 alphabet contains no backslash, backtick or dollar, and
  // neither do our fixed literals.
  if (/[\\`$]/.test(expr)) throw new GmCommandError('Refusing to exec an Erlang expression containing shell metacharacters');
  if (!ERLANG_SAFE_RE.test(expr)) {
    throw new GmCommandError('Refusing to exec an Erlang expression containing unexpected characters');
  }
  return expr;
}

export interface ExecRunner {
  (argv: string[], stdin: string, timeoutMs: number): Promise<{ code: number; stdout: string; stderr: string }>;
}

/**
 * Runs a command with an explicit argv and feeds the payload on STDIN. There is no shell anywhere in this path: the
 * Erlang expression (which embeds operator- and player-supplied text, base64-encoded) is never concatenated into a
 * command line, so a chat message containing `"; rm -rf /` cannot become anything but base64.
 */
export const defaultExecRunner: ExecRunner = (argv, stdin, timeoutMs) =>
  new Promise((resolve, reject) => {
    const child = spawn(argv[0], argv.slice(1), { stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      reject(new Error(`${argv[0]} timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    child.stdout.on('data', (c: Buffer) => (stdout += c.toString()));
    child.stderr.on('data', (c: Buffer) => (stderr += c.toString()));
    child.on('error', (err) => {
      clearTimeout(timer);
      reject(err);
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      resolve({ code: code ?? -1, stdout, stderr });
    });
    child.stdin.end(stdin);
  });

export interface ExecPublisherOptions {
  /** Prefix argv that lands us inside the broker container, e.g. `['docker','exec','-i','game-rmq']`. */
  execArgv: string[];
  kind: string;
  timeoutMs?: number;
  runner?: ExecRunner;
  /** Path to `rabbitmqctl` inside the container. */
  rabbitmqctl?: string;
}

/**
 * `docker exec` / `kubectl exec` publisher, for installs where the sidecar cannot authenticate to the game broker.
 *
 * **Live correction (rig probe, 2026-09-21):** the original design piped the Erlang expression to
 * `rabbitmqctl eval_file /dev/stdin`. That **does not work** on the shipped broker image (RabbitMQ 3.13.7):
 * `eval_file` cannot read a pipe and answers `Evaluation failed: syntax error before:`. `eval_file <real file>`
 * and `eval '<expr>'` both do work. So the expression now travels as a **single argv element** to
 * `rabbitmqctl eval`, which needs no writable path inside the container and no cleanup.
 *
 * That is still shell-free — `spawn` calls `execve` directly, there is no `sh -c` anywhere — and
 * `assertErlangSafe` proves before every publish that the expression is base64 plus fixed literals, so a chat
 * message containing `"; rm -rf /` or `$(id)` cannot become anything but base64.
 *
 * Success is decided by our own marker on stdout, never by the exit code alone: `rabbitmqctl eval` happily exits 0
 * after printing an Erlang exception.
 */
export class ExecGmPublisher implements GmPublisher {
  readonly kind: string;
  private readonly runner: ExecRunner;
  private readonly timeoutMs: number;

  constructor(private readonly options: ExecPublisherOptions) {
    this.kind = options.kind;
    this.runner = options.runner ?? defaultExecRunner;
    this.timeoutMs = options.timeoutMs ?? 20000;
  }

  /**
   * The exact argv this publisher would run. Exposed so a test can assert nothing is shell-interpolated and that the
   * expression is the LAST element — one `execve` argument, never a command line.
   */
  argvFor(expr: string): string[] {
    return [...this.options.execArgv, this.options.rabbitmqctl ?? 'rabbitmqctl', 'eval', assertErlangSafe(expr)];
  }

  async publish(request: PublishRequest): Promise<PublishResult> {
    const expr = buildErlangPublish(request);
    const argv = this.argvFor(expr);
    logger.debug(`GM publish via ${this.kind}: ${argv.slice(0, -1).join(' ')} <${expr.length} bytes of Erlang in argv>`);
    // stdin is closed empty: `eval` takes the expression from argv, and leaving a pipe open would reintroduce the
    // `eval_file /dev/stdin` failure mode this replaced.
    const result = await this.runner(argv, '', this.timeoutMs);
    const output = `${result.stdout}${result.stderr}`.trim();
    const ok = result.code === 0 && output.includes(`${EXEC_MARKER} result=ok`);
    return { ok, output };
  }
}

export function dockerExecArgv(dockerBin: string, container: string): string[] {
  if (!container) throw new Error('DUNE_RMQ_CONTAINER is required for the docker-exec GM publisher');
  return [dockerBin, 'exec', '-i', container];
}

export function kubectlExecArgv(kubectlBin: string, namespace: string, pod: string): string[] {
  if (!pod) throw new Error('DUNE_K8S_POD is required for the kubectl-exec GM publisher');
  return [kubectlBin, 'exec', '-i', '-n', namespace, pod, '--'];
}

export interface GmClientOptions {
  authToken: string;
  publisher: GmPublisher;
  exchange: string;
  routingKey: string;
  userId: string;
  appId: string;
  contentType?: string;
}

/** Wraps an inner command in the version-2 envelope and hands it to whichever publisher is configured. */
export class GmClient {
  constructor(private readonly options: GmClientOptions) {}

  publisherKind(): string {
    return this.options.publisher.kind;
  }

  hasToken(): boolean {
    return Boolean(this.options.authToken);
  }

  async send(inner: Record<string, unknown>, now = Date.now()): Promise<PublishResult> {
    if (!this.options.authToken) {
      // A missing token is not a transport failure: the server would answer `Invalid Auth Token` and do nothing, and
      // saying so here is far more useful than a silent no-op that looks like success.
      throw new GmCommandError('DUNE_GM_AUTH_TOKEN is not set; the server rejects every server command without it');
    }
    const command = str(inner.ServerCommand) ?? 'command';
    const result = await this.options.publisher.publish({
      exchange: this.options.exchange,
      routingKey: this.options.routingKey,
      body: encodeEnvelope(inner, this.options.authToken),
      properties: {
        contentType: this.options.contentType ?? 'Content',
        messageId: messageId(command, now),
        userId: this.options.userId,
        appId: this.options.appId,
      },
      label: safeLabel(command),
    });
    logger.debug(`GM ${command} → ${result.ok ? 'published' : 'FAILED'} (${this.options.publisher.kind}): ${result.output}`);
    if (!result.ok) throw new GmCommandError(`GM ${command} could not be published: ${result.output}`);
    return result;
  }
}
