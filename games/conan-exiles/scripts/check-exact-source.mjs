#!/usr/bin/env node
// Does this lockfile still describe the dependencies the catalog recorded?
//
// A release is only as exact as the bytes it was built from, and `npm ci` will happily
// install whatever the lockfile points at. So before anything is installed, every
// lockfile is pinned whole by digest, then each direct dependency is checked twice: the
// lockfile must resolve it to exactly the recorded URL, and the tarball at that URL must
// hash to exactly the recorded sha256.
// Neither failure falls back to "install it anyway" -- a build that cannot reproduce the
// recorded inputs is not this target's build.
//
// Reads CONAN_EXILES_DEP_<NAME>_URL / _SHA256 pairs from the environment, which
// `takaro-maint targets resolve` writes from `build.deps`.
//
// Exit codes: 0 fine, 5 a hash disagrees, 7 the lockfile drifted.

import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';

const DRIFT = 7;
const MISMATCH = 5;

function pinnedDeps() {
  const deps = [];
  for (const [key, value] of Object.entries(process.env)) {
    const found = /^CONAN_EXILES_DEP_(.+)_URL$/.exec(key);
    if (!found) continue;
    const sha256 = process.env[`CONAN_EXILES_DEP_${found[1]}_SHA256`];
    if (!sha256) {
      console.error(`${key} is set but CONAN_EXILES_DEP_${found[1]}_SHA256 is not`);
      process.exit(DRIFT);
    }
    // The catalog keys `build.deps` by package name; the env key is that name upper-cased.
    deps.push({ name: found[1].toLowerCase(), url: value, sha256 });
  }
  return deps.sort((a, b) => a.name.localeCompare(b.name));
}

async function main() {
  const lockfile = process.env.CONAN_EXILES_LOCKFILE_PATH;
  const expectedLockfile = process.env.CONAN_EXILES_LOCKFILE_SHA256;
  if (!lockfile || !expectedLockfile) {
    console.error('CONAN_EXILES_LOCKFILE_PATH and CONAN_EXILES_LOCKFILE_SHA256 are required; resolve the target first');
    process.exit(DRIFT);
  }
  const lockBytes = readFileSync(lockfile);
  const actualLockfile = createHash('sha256').update(lockBytes).digest('hex');
  if (actualLockfile !== expectedLockfile) {
    console.error(
      `${lockfile} does not match the catalog pin:\n  expected: ${expectedLockfile}\n  actual:   ${actualLockfile}\n` +
        'a dependency added, removed or moved in the lockfile is a re-pin, not a build',
    );
    process.exit(DRIFT);
  }

  const deps = pinnedDeps();
  if (deps.length === 0) {
    console.error('no CONAN_EXILES_DEP_*_URL in the environment; resolve the target first');
    process.exit(DRIFT);
  }

  const lock = JSON.parse(lockBytes.toString('utf8'));
  const packages = lock.packages ?? {};

  for (const dep of deps) {
    const entry = packages[`node_modules/${dep.name}`];
    if (!entry) {
      console.error(`${lockfile} holds no node_modules/${dep.name}; the catalog pins it`);
      process.exit(DRIFT);
    }
    if (entry.resolved !== dep.url) {
      console.error(
        `${lockfile} drifted for ${dep.name}:\n  lockfile: ${entry.resolved}\n  catalog:  ${dep.url}`,
      );
      process.exit(DRIFT);
    }
  }

  for (const dep of deps) {
    const response = await fetch(dep.url);
    if (!response.ok) {
      console.error(`could not read ${dep.url}: HTTP ${response.status}`);
      process.exit(MISMATCH);
    }
    const digest = createHash('sha256').update(Buffer.from(await response.arrayBuffer())).digest('hex');
    if (digest !== dep.sha256) {
      console.error(
        `${dep.name} is not the recorded tarball, and this build is not falling back to it:\n` +
          `  expected: ${dep.sha256}\n  actual:   ${digest}\n  url:      ${dep.url}`,
      );
      process.exit(MISMATCH);
    }
    console.log(`exact source: ${dep.name} ${digest}`);
  }
}

await main();
