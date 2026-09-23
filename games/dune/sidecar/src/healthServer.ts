import http from 'node:http';
import { logger } from './logger.js';

/** A local admin route. `handle` gets the parsed JSON body (`{}` when there was none). */
export interface AdminRoute {
  method: 'GET' | 'POST';
  path: string;
  /** One line for `GET /admin`, so the endpoint documents itself instead of living only in a report. */
  doc: string;
  handle: (body: Record<string, unknown>) => Promise<unknown> | unknown;
}

export interface HealthServerOptions {
  /**
   * Bearer token for `/admin/*`. Empty disables every admin route (they answer 404, as if they did not exist).
   *
   * Why this exists: Takaro is the connector's only control plane, so when Takaro is unreachable there is no way to
   * put a ban into the store or send a GM broadcast — which is exactly the situation in which an operator most needs
   * to. These routes are that escape hatch. They are reachable only on `SIDECAR_HEALTH_HOST` (the compose network /
   * loopback; the rig publishes no port for it) AND require this token, and they are never mentioned in a response
   * body unless the caller already authenticated.
   */
  adminToken?: string;
  routes?: AdminRoute[];
  /** Largest admin request body accepted, to keep an unauthenticated caller from buffering memory. */
  maxBodyBytes?: number;
}

export class HealthServer {
  private server: http.Server | null = null;
  private readonly options: HealthServerOptions;

  constructor(
    private readonly port: number,
    private readonly host: string,
    private readonly snapshot: () => Promise<Record<string, unknown>> | Record<string, unknown>,
    options: HealthServerOptions = {},
  ) {
    this.options = options;
  }

  address(): number | null {
    const addr = this.server?.address();
    return addr && typeof addr === 'object' ? addr.port : null;
  }

  private adminEnabled(): boolean {
    return Boolean(this.options.adminToken && (this.options.routes?.length ?? 0) > 0);
  }

  private authorized(req: http.IncomingMessage): boolean {
    const token = this.options.adminToken ?? '';
    if (!token) return false;
    const header = req.headers.authorization ?? '';
    const offered = header.startsWith('Bearer ') ? header.slice(7) : String(req.headers['x-takaro-token'] ?? '');
    // Length-first comparison: Node has no constant-time string compare, and this is a loopback-only route, but
    // there is no reason to leak the length either.
    return offered.length === token.length && offered === token;
  }

  start(): Promise<void> {
    if (this.server) return Promise.resolve();
    this.server = http.createServer((req, res) => {
      const path = req.url?.split('?')[0] ?? '';
      if (req.method === 'GET' && path === '/health') {
        void this.respondHealth(res);
        return;
      }
      if (path.startsWith('/admin') && this.adminEnabled()) {
        void this.respondAdmin(req, res, path);
        return;
      }
      res.statusCode = 404;
      res.end('not found');
    });
    return new Promise((resolve, reject) => {
      this.server?.once('error', reject);
      this.server?.listen(this.port, this.host, () => resolve());
    });
  }

  private async respondHealth(res: http.ServerResponse): Promise<void> {
    try {
      const body = await this.snapshot();
      res.statusCode = body.ok === false ? 503 : 200;
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify(body));
    } catch (err) {
      res.statusCode = 500;
      res.end(JSON.stringify({ ok: false, error: (err as Error).message }));
    }
  }

  private async respondAdmin(req: http.IncomingMessage, res: http.ServerResponse, path: string): Promise<void> {
    const json = (code: number, body: unknown): void => {
      res.statusCode = code;
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify(body));
    };
    if (!this.authorized(req)) {
      // No hint about which routes exist, and one log line so a probe is visible in the evidence.
      logger.warn(`Rejected an unauthenticated admin request ${req.method} ${path}`);
      json(401, { ok: false, error: 'unauthorized' });
      return;
    }
    if (req.method === 'GET' && path === '/admin') {
      json(200, { ok: true, routes: (this.options.routes ?? []).map((r) => ({ method: r.method, path: r.path, doc: r.doc })) });
      return;
    }
    const route = (this.options.routes ?? []).find((r) => r.path === path);
    if (!route) return json(404, { ok: false, error: `no admin route ${path}` });
    if (route.method !== req.method) return json(405, { ok: false, error: `${path} is ${route.method}` });

    let body: Record<string, unknown> = {};
    try {
      body = await readJsonBody(req, this.options.maxBodyBytes ?? 64 * 1024);
    } catch (err) {
      return json(400, { ok: false, error: (err as Error).message });
    }
    try {
      logger.info(`Local admin request ${route.method} ${route.path}`);
      json(200, { ok: true, result: await route.handle(body) });
    } catch (err) {
      logger.warn(`Local admin request ${route.path} failed: ${(err as Error).message}`);
      json(400, { ok: false, error: (err as Error).message });
    }
  }

  stop(): Promise<void> {
    const active = this.server;
    this.server = null;
    if (!active) return Promise.resolve();
    return new Promise((resolve, reject) => active.close((err) => (err ? reject(err) : resolve())));
  }
}

export function readJsonBody(req: http.IncomingMessage, maxBytes: number): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    req.on('data', (chunk: Buffer) => {
      size += chunk.length;
      if (size > maxBytes) {
        reject(new Error(`request body larger than ${maxBytes} bytes`));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8').trim();
      if (!raw) return resolve({});
      try {
        const parsed = JSON.parse(raw) as unknown;
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return reject(new Error('body must be a JSON object'));
        resolve(parsed as Record<string, unknown>);
      } catch (err) {
        reject(new Error(`body is not valid JSON: ${(err as Error).message}`));
      }
    });
    req.on('error', (err) => reject(err));
  });
}
