import { mkdtempSync, rmSync, utimesSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, describe, expect, it } from 'vitest';

import {
  clearProviderEnvReloadCache,
  reloadProviderEnvFiles,
} from '../src/commands/provider-env-reload.js';

const dirs: string[] = [];

afterEach(() => {
  clearProviderEnvReloadCache();
  delete process.env.ACN_PROVIDER_ENV_FILE;
  delete process.env.ACN_PROVIDER_API_KEY;
  while (dirs.length) {
    const root = dirs.pop();
    if (root) rmSync(root, { recursive: true, force: true });
  }
});

describe('reloadProviderEnvFiles', () => {
  it('picks up a rotated ACN_PROVIDER_API_KEY without restart', () => {
    const root = mkdtempSync(join(tmpdir(), 'acn-provider-env-'));
    dirs.push(root);
    const file = join(root, 'provider.env');
    writeFileSync(file, 'ACN_PROVIDER_API_KEY=old-key\n', 'utf8');
    process.env.ACN_PROVIDER_ENV_FILE = file;
    process.env.ACN_PROVIDER_API_KEY = 'stale-from-systemd';

    expect(reloadProviderEnvFiles()).toBe(true);
    expect(process.env.ACN_PROVIDER_API_KEY).toBe('old-key');

    // Same mtime → no change.
    expect(reloadProviderEnvFiles()).toBe(false);

    writeFileSync(file, 'ACN_PROVIDER_API_KEY=new-key\n', 'utf8');
    // Ensure mtime advances on coarse filesystems.
    const now = Date.now() / 1000 + 2;
    utimesSync(file, now, now);

    expect(reloadProviderEnvFiles()).toBe(true);
    expect(process.env.ACN_PROVIDER_API_KEY).toBe('new-key');
  });
});