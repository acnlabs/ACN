import { homedir } from 'os';
import { join } from 'path';
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'fs';

const CONFIG_DIR = join(homedir(), '.acn');
const CONFIG_FILE = join(CONFIG_DIR, 'config.json');

/** Hosted ACN origins (no trailing slash, no `/api/v1`). */
export const REGION_BASE_URLS = {
  global: 'https://api.acnlabs.dev',
  cn: 'https://acn.acnlabs.cn',
} as const;

export type AcnRegion = keyof typeof REGION_BASE_URLS;

export const DEFAULT_BASE_URL = REGION_BASE_URLS.global;

export interface AcnConfig {
  api_key?: string;
  agent_id?: string;
  /** ACN origin, e.g. https://api.acnlabs.dev or https://acn.acnlabs.cn */
  base_url: string;
  /** Set when joining via --region or `config set region`. */
  region?: AcnRegion;
}

/** Strip trailing slashes and a mistaken `/api/v1` suffix (acnFetch adds it). */
export function normalizeBaseUrl(url: string): string {
  let u = url.trim().replace(/\/+$/, '');
  u = u.replace(/\/api\/v1$/i, '');
  return u.replace(/\/+$/, '');
}

export function baseUrlForRegion(region: string): string {
  const key = region.trim().toLowerCase();
  if (key === 'global' || key === 'cn') {
    return REGION_BASE_URLS[key];
  }
  throw new Error(`Unknown region "${region}". Valid: global | cn`);
}

export function inferRegion(baseUrl: string): AcnRegion | undefined {
  const u = normalizeBaseUrl(baseUrl);
  if (u === REGION_BASE_URLS.global) return 'global';
  if (u === REGION_BASE_URLS.cn) return 'cn';
  return undefined;
}

function readConfigFile(): Partial<AcnConfig> {
  if (!existsSync(CONFIG_FILE)) return {};
  try {
    const raw = readFileSync(CONFIG_FILE, 'utf-8');
    return JSON.parse(raw) as Partial<AcnConfig>;
  } catch {
    return {};
  }
}

/**
 * Resolve which ACN instance to talk to.
 *
 * Precedence: explicit override → `ACN_BASE_URL` env → `~/.acn/config.json`
 * → hosted global default.
 *
 * Deployed agents should register where they run: CN infra → `cn`,
 * overseas infra → `global`. Credentials are not portable across regions.
 */
export function resolveBaseUrl(overrides?: {
  base_url?: string;
  region?: string;
}): string {
  if (overrides?.base_url) {
    return normalizeBaseUrl(overrides.base_url);
  }
  if (overrides?.region) {
    return baseUrlForRegion(overrides.region);
  }
  const env = process.env.ACN_BASE_URL?.trim();
  if (env) {
    return normalizeBaseUrl(env);
  }
  const file = readConfigFile();
  if (file.base_url) {
    return normalizeBaseUrl(file.base_url);
  }
  if (file.region) {
    return baseUrlForRegion(file.region);
  }
  return DEFAULT_BASE_URL;
}

export function loadConfig(): AcnConfig {
  const file = readConfigFile();
  const base_url = resolveBaseUrl();
  // Always derive region from the *effective* base_url so ACN_BASE_URL
  // cannot disagree with a stale file `region` field.
  return {
    base_url,
    api_key: file.api_key,
    agent_id: file.agent_id,
    region: inferRegion(base_url),
  };
}

/**
 * Persist config. Merges against the on-disk file (not loadConfig), so a
 * transient `ACN_BASE_URL` env override cannot rewrite the saved base_url.
 */
export function saveConfig(updates: Partial<AcnConfig>): void {
  if (!existsSync(CONFIG_DIR)) {
    mkdirSync(CONFIG_DIR, { recursive: true });
  }
  const file = readConfigFile();
  const current: AcnConfig = {
    base_url: file.base_url ? normalizeBaseUrl(file.base_url) : DEFAULT_BASE_URL,
    api_key: file.api_key,
    agent_id: file.agent_id,
    region:
      file.region === 'global' || file.region === 'cn' ? file.region : undefined,
  };
  const next: AcnConfig = { ...current, ...updates };
  if (updates.region && !updates.base_url) {
    next.base_url = baseUrlForRegion(updates.region);
  }
  if (updates.base_url && updates.region === undefined) {
    next.region = inferRegion(updates.base_url);
  }
  const clean: AcnConfig = { base_url: normalizeBaseUrl(next.base_url) };
  if (next.api_key !== undefined) clean.api_key = next.api_key;
  if (next.agent_id !== undefined) clean.agent_id = next.agent_id;
  // Persist inferred region when known; omit for custom origins.
  const region = next.region ?? inferRegion(clean.base_url);
  if (region !== undefined) clean.region = region;
  writeFileSync(CONFIG_FILE, JSON.stringify(clean, null, 2), 'utf-8');
}

export function getConfigPath(): string {
  return CONFIG_FILE;
}
