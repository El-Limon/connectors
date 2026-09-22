// What `check-exact-source.mjs` does when the lockfile and the catalog disagree.
//
// The lockfile is pinned whole by digest, and each direct dependency is additionally
// checked by URL and tarball hash. Each case drives the real script as a subprocess
// against a temporary lockfile and a local server that serves the chosen bytes.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { after, test } from 'node:test';
import { fileURLToPath } from 'node:url';

const SCRIPT = join(dirname(fileURLToPath(import.meta.url)), 'check-exact-source.mjs');
const BODY = Buffer.from('a tarball, for the purposes of this test');
const DIGEST = createHash('sha256').update(BODY).digest('hex');

/** One server for the whole file, answering `/<name>.tgz` with the bytes, 404 otherwise. */
const served = new Map([['express', BODY]]);
const server = createServer((request, response) => {
  const name = decodeURIComponent(request.url ?? '').replace(/^\/|\.tgz$/g, '');
  const body = served.get(name);
  if (!body) {
    response.writeHead(404).end('no such tarball');
    return;
  }
  response.writeHead(200, { 'content-type': 'application/octet-stream' }).end(body);
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;
after(() => server.close());

function workspace(lock) {
  const dir = mkdtempSync(join(tmpdir(), 'exact-source-'));
  writeFileSync(join(dir, 'package-lock.json'), JSON.stringify(lock), 'utf8');
  return dir;
}

function lockfile(resolved) {
  return { lockfileVersion: 3, packages: { '': { name: 'bridge' }, 'node_modules/express': { resolved } } };
}

function run(cwd, env) {
  return new Promise((resolve) => {
    const path = join(cwd, 'package-lock.json');
    const lockfileEnv = {
      CONAN_EXILES_LOCKFILE_PATH: path,
      CONAN_EXILES_LOCKFILE_SHA256: createHash('sha256').update(readFileSync(path)).digest('hex'),
    };
    const child = spawn(process.execPath, [SCRIPT], {
      cwd,
      // A clean environment: a real CONAN_EXILES_DEP_* in the caller's shell must not
      // decide what these cases check.
      env: { PATH: process.env.PATH, ...lockfileEnv, ...env },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => (stdout += chunk));
    child.stderr.on('data', (chunk) => (stderr += chunk));
    child.on('close', (code) => resolve({ code, stdout, stderr }));
  });
}

test('a lockfile and a tarball that both match the catalog pass', async () => {
  const url = `${base}/express.tgz`;
  const result = await run(workspace(lockfile(url)), {
    CONAN_EXILES_DEP_EXPRESS_URL: url,
    CONAN_EXILES_DEP_EXPRESS_SHA256: DIGEST,
  });

  assert.equal(result.code, 0, result.stderr);
  assert.match(result.stdout, new RegExp(`exact source: express ${DIGEST}`));
});

test('an added lockfile package is a re-pin even when direct dependencies still match', async () => {
  const url = `${base}/express.tgz`;
  const original = lockfile(url);
  const dir = workspace(original);
  const expected = createHash('sha256').update(readFileSync(join(dir, 'package-lock.json'))).digest('hex');
  original.packages['node_modules/unpinned'] = { resolved: `${base}/unpinned.tgz` };
  writeFileSync(join(dir, 'package-lock.json'), JSON.stringify(original), 'utf8');

  const result = await run(dir, {
    CONAN_EXILES_LOCKFILE_SHA256: expected,
    CONAN_EXILES_DEP_EXPRESS_URL: url,
    CONAN_EXILES_DEP_EXPRESS_SHA256: DIGEST,
  });

  assert.equal(result.code, 7);
  assert.match(result.stderr, /re-pin, not a build/);
  assert.match(result.stderr, /expected:/);
  assert.match(result.stderr, /actual:/);
});

test('a missing lockfile digest means the target was not resolved', async () => {
  const url = `${base}/express.tgz`;
  const result = await run(workspace(lockfile(url)), {
    CONAN_EXILES_LOCKFILE_SHA256: '',
    CONAN_EXILES_DEP_EXPRESS_URL: url,
    CONAN_EXILES_DEP_EXPRESS_SHA256: DIGEST,
  });

  assert.equal(result.code, 7);
  assert.match(result.stderr, /resolve the target first/);
});

test('a tarball whose hash disagrees is refused rather than installed', async () => {
  const url = `${base}/express.tgz`;
  const result = await run(workspace(lockfile(url)), {
    CONAN_EXILES_DEP_EXPRESS_URL: url,
    CONAN_EXILES_DEP_EXPRESS_SHA256: 'f'.repeat(64),
  });

  assert.equal(result.code, 5);
  assert.match(result.stderr, /not the recorded tarball/);
  assert.match(result.stderr, new RegExp(DIGEST));
});

test('a URL the host does not serve is a mismatch, not a silent skip', async () => {
  const url = `${base}/not-published.tgz`;
  const result = await run(workspace(lockfile(url)), {
    CONAN_EXILES_DEP_EXPRESS_URL: url,
    CONAN_EXILES_DEP_EXPRESS_SHA256: DIGEST,
  });

  assert.equal(result.code, 5);
  assert.match(result.stderr, /HTTP 404/);
});

test('a lockfile resolving the dependency somewhere else is drift', async () => {
  const result = await run(workspace(lockfile(`${base}/express.tgz?rewritten=1`)), {
    CONAN_EXILES_DEP_EXPRESS_URL: `${base}/express.tgz`,
    CONAN_EXILES_DEP_EXPRESS_SHA256: DIGEST,
  });

  assert.equal(result.code, 7);
  assert.match(result.stderr, /drifted for express/);
});

test('a dependency the lockfile does not hold at all is drift', async () => {
  const lock = { lockfileVersion: 3, packages: { '': { name: 'bridge' } } };
  const result = await run(workspace(lock), {
    CONAN_EXILES_DEP_EXPRESS_URL: `${base}/express.tgz`,
    CONAN_EXILES_DEP_EXPRESS_SHA256: DIGEST,
  });

  assert.equal(result.code, 7);
  assert.match(result.stderr, /holds no node_modules\/express/);
});

test('a URL without its sha256 names the key that is missing', async () => {
  const result = await run(workspace(lockfile(`${base}/express.tgz`)), {
    CONAN_EXILES_DEP_EXPRESS_URL: `${base}/express.tgz`,
  });

  assert.equal(result.code, 7);
  assert.match(result.stderr, /CONAN_EXILES_DEP_EXPRESS_SHA256 is not/);
});

test('no pinned dependencies at all is an unresolved target, never an empty pass', async () => {
  const result = await run(workspace(lockfile(`${base}/express.tgz`)), {});

  assert.equal(result.code, 7);
  assert.match(result.stderr, /resolve the target first/);
});
