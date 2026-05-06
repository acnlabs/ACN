import { homedir } from 'os';
import { join } from 'path';
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'fs';

const CONFIG_DIR = join(homedir(), '.acn');
const CONFIG_FILE = join(CONFIG_DIR, 'config.json');

const DEFAULT_BASE_URL = 'https://acn-production.up.railway.app';

export interface AcnConfig {
  api_key?: string;
  agent_id?: string;
  base_url: string;
}

export function loadConfig(): AcnConfig {
  if (!existsSync(CONFIG_FILE)) {
    return { base_url: DEFAULT_BASE_URL };
  }
  try {
    const raw = readFileSync(CONFIG_FILE, 'utf-8');
    const parsed = JSON.parse(raw) as Partial<AcnConfig>;
    return {
      base_url: parsed.base_url ?? DEFAULT_BASE_URL,
      api_key: parsed.api_key,
      agent_id: parsed.agent_id,
    };
  } catch {
    return { base_url: DEFAULT_BASE_URL };
  }
}

export function saveConfig(updates: Partial<AcnConfig>): void {
  if (!existsSync(CONFIG_DIR)) {
    mkdirSync(CONFIG_DIR, { recursive: true });
  }
  const current = loadConfig();
  const next = { ...current, ...updates };
  // Remove undefined values
  const clean = Object.fromEntries(
    Object.entries(next).filter(([, v]) => v !== undefined)
  ) as AcnConfig;
  writeFileSync(CONFIG_FILE, JSON.stringify(clean, null, 2), 'utf-8');
}

export function getConfigPath(): string {
  return CONFIG_FILE;
}
