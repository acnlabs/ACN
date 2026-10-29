/**
 * Hot-reload BYO provider credentials from disk without restarting listen.
 *
 * Agents rotate provider keys often. Systemd ``EnvironmentFile=`` only loads
 * once at process start; set ``ACN_PROVIDER_ENV_FILE`` (comma-separated paths)
 * so each model heartbeat re-reads ``ACN_PROVIDER*`` keys when the file mtimes.
 */
import { readFileSync, statSync } from 'node:fs';

import { clearProviderProbeDenialCache } from './model-heartbeat.js';

const mtimeByFile = new Map<string, number>();

/** Provider-related keys safe to refresh from an env file. */
const RELOAD_KEY =
  /^(ACN_PROVIDER|ACN_PROVIDERS|ACN_OFFICIAL_DEFAULT_MODEL)([A-Z0-9_]*)?$/;

function parseEnvLine(line: string): { key: string; value: string } | null {
  const trimmed = line.trim();
  if (!trimmed || trimmed.startsWith('#')) return null;
  const eq = trimmed.indexOf('=');
  if (eq <= 0) return null;
  const key = trimmed.slice(0, eq).trim();
  if (!key || /\s/.test(key)) return null;
  let value = trimmed.slice(eq + 1).trim();
  if (
    (value.startsWith('"') && value.endsWith('"')) ||
    (value.startsWith("'") && value.endsWith("'"))
  ) {
    value = value.slice(1, -1);
  }
  return { key, value };
}

export function providerEnvFilesFromEnv(env?: NodeJS.ProcessEnv): string[] {
  const raw = (env ?? process.env).ACN_PROVIDER_ENV_FILE?.trim() || '';
  if (!raw) return [];
  return raw
    .split(/[,:]/)
    .map((part) => part.trim())
    .filter(Boolean);
}

/**
 * Re-apply ``ACN_PROVIDER*`` from configured env files when mtime changes.
 * Returns true when at least one watched value changed in ``process.env``.
 */
export function reloadProviderEnvFiles(env?: NodeJS.ProcessEnv): boolean {
  const files = providerEnvFilesFromEnv(env);
  if (!files.length) return false;
  let changed = false;
  for (const file of files) {
    let mtimeMs = 0;
    let text = '';
    try {
      mtimeMs = statSync(file).mtimeMs;
      const prev = mtimeByFile.get(file);
      // First see: record mtime and still load once so a late-written key
      // after process start is picked up without requiring a second write.
      if (prev === mtimeMs && prev !== undefined) continue;
      text = readFileSync(file, 'utf8');
      mtimeByFile.set(file, mtimeMs);
    } catch {
      continue;
    }
    for (const line of text.split(/\n/)) {
      const parsed = parseEnvLine(line);
      if (!parsed || !RELOAD_KEY.test(parsed.key)) continue;
      if (process.env[parsed.key] === parsed.value) continue;
      process.env[parsed.key] = parsed.value;
      changed = true;
    }
  }
  if (changed) clearProviderProbeDenialCache();
  return changed;
}

/** Test helper — drop remembered file mtimes. */
export function clearProviderEnvReloadCache(): void {
  mtimeByFile.clear();
}
