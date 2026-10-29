/**
 * BYO quota-exhaust degrade: snapshot last good providers, clear declaration,
 * flip preferred to official default, restore after exit debounce.
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import type { ReportedProvider } from './model-heartbeat.js';
import { OFFICIAL_FALLBACK_MODEL } from './provider-failure.js';

export const BYO_DEGRADE_ENTER_STREAK = 2;
export const BYO_DEGRADE_EXIT_STREAK = 3;

export type ByoDegradeSnapshot = {
  providers: ReportedProvider[];
  preferredModel?: string;
};

export type ByoDegradeFile = {
  agentId: string;
  degraded: boolean;
  enterStreak: number;
  exitStreak: number;
  /** Catalog/auth rejected the provider API key (distinct from quota). */
  keyInvalid?: boolean;
  snapshot?: ByoDegradeSnapshot;
  updatedAt: string;
};

export function byoDegradePath(agentId: string, env?: NodeJS.ProcessEnv): string {
  const root = (env ?? process.env).ACN_HOME?.trim() || join(homedir(), '.acn');
  const safe = agentId.replace(/[^a-zA-Z0-9._-]+/g, '_').slice(0, 80) || 'agent';
  return join(root, 'byo-degrade', `${safe}.json`);
}

export function loadByoDegradeState(
  agentId: string,
  env?: NodeJS.ProcessEnv
): ByoDegradeFile {
  const path = byoDegradePath(agentId, env);
  try {
    if (!existsSync(path)) {
      return {
        agentId,
        degraded: false,
        enterStreak: 0,
        exitStreak: 0,
        updatedAt: new Date(0).toISOString(),
      };
    }
    const raw = JSON.parse(readFileSync(path, 'utf8')) as ByoDegradeFile;
    return {
      agentId,
      degraded: !!raw.degraded,
      enterStreak: Math.max(0, Number(raw.enterStreak) || 0),
      exitStreak: Math.max(0, Number(raw.exitStreak) || 0),
      keyInvalid: raw.keyInvalid === true,
      snapshot: raw.snapshot,
      updatedAt: raw.updatedAt || new Date(0).toISOString(),
    };
  } catch {
    return {
      agentId,
      degraded: false,
      enterStreak: 0,
      exitStreak: 0,
      updatedAt: new Date(0).toISOString(),
    };
  }
}

export function saveByoDegradeState(
  state: ByoDegradeFile,
  env?: NodeJS.ProcessEnv
): void {
  const path = byoDegradePath(state.agentId, env);
  mkdirSync(dirname(path), { recursive: true });
  const next: ByoDegradeFile = {
    ...state,
    updatedAt: new Date().toISOString(),
  };
  writeFileSync(path, `${JSON.stringify(next, null, 2)}\n`, 'utf8');
}

/**
 * Official preferred for degrade heartbeat.
 * Env ``ACN_OFFICIAL_DEFAULT_MODEL`` first (Comiclaw/TokenPlus:
 * ``deepseek/deepseek-v4-flash``), else dual-shelf hardcoded fallback.
 */
export function resolveOfficialDefaultModel(env?: NodeJS.ProcessEnv): string {
  const fromEnv = (env ?? process.env).ACN_OFFICIAL_DEFAULT_MODEL?.trim();
  if (fromEnv) return fromEnv.slice(0, 200);
  return OFFICIAL_FALLBACK_MODEL.slice(0, 200);
}

export type DegradeTickInput = {
  agentId: string;
  /** True when this provider-round was all quota-exhausted (no callable models). */
  quotaExhausted: boolean;
  /** True when at least one BYO model survived probes this round. */
  hasByoModels: boolean;
  /**
   * Provider catalog rejected the API key (401/403). Enter degrade immediately
   * so Host can fall back to official; clear when probes succeed again.
   */
  keyInvalid?: boolean;
  /** Current declaration before degrade wipe (for snapshot). */
  providers?: ReportedProvider[];
  preferredModel?: string;
  env?: NodeJS.ProcessEnv;
};

export type DegradeTickResult = {
  state: ByoDegradeFile;
  /** Heartbeat should send byo_degraded + empty providers + official preferred. */
  enterOrStayDegraded: boolean;
  /** Heartbeat should restore snapshot and byo_degraded=false. */
  justRestored: boolean;
};

/**
 * Enter after ENTER_STREAK quota rounds (or immediately on keyInvalid);
 * exit after EXIT_STREAK healthy rounds.
 */
export function tickByoDegrade(input: DegradeTickInput): DegradeTickResult {
  const state = loadByoDegradeState(input.agentId, input.env);
  let enterOrStayDegraded = state.degraded;
  let justRestored = false;

  if (input.keyInvalid && !input.hasByoModels) {
    state.exitStreak = 0;
    state.enterStreak = 0;
    state.keyInvalid = true;
    // Auth failure is decisive — do not wait for quota enter streak.
    state.degraded = true;
    enterOrStayDegraded = true;
  } else if (input.quotaExhausted && !input.hasByoModels) {
    state.exitStreak = 0;
    state.keyInvalid = false;
    state.enterStreak += 1;
    if (!state.degraded && state.enterStreak >= BYO_DEGRADE_ENTER_STREAK) {
      // Keep prior healthy snapshot — this round's providers are empty models.
      state.degraded = true;
    }
    enterOrStayDegraded = state.degraded;
  } else if (input.hasByoModels) {
    state.enterStreak = 0;
    state.keyInvalid = false;
    // Refresh last-good declaration while healthy (or during exit debounce
    // probes that still see callable models).
    if (input.providers?.some((row) => row.models?.length)) {
      state.snapshot = {
        providers: input.providers,
        preferredModel: input.preferredModel,
      };
    }
    if (state.degraded) {
      state.exitStreak += 1;
      if (state.exitStreak >= BYO_DEGRADE_EXIT_STREAK) {
        state.degraded = false;
        state.exitStreak = 0;
        justRestored = true;
        enterOrStayDegraded = false;
      } else {
        enterOrStayDegraded = true;
      }
    } else {
      state.exitStreak = 0;
      enterOrStayDegraded = false;
    }
  } else {
    // Ambiguous empty (denied/network): do not advance enter; if already
    // degraded, stay degraded until a clear healthy round.
    state.enterStreak = 0;
    enterOrStayDegraded = state.degraded;
  }

  saveByoDegradeState(state, input.env);
  return { state, enterOrStayDegraded, justRestored };
}

/** True when a model id looks like a BYO vendor prefix (not Host book path). */
export function looksLikeByoModelId(modelId: string | null | undefined): boolean {
  const raw = (modelId || '').trim().toLowerCase();
  if (!raw || !raw.includes('/')) return false;
  // Official Host book ids are author/slug (openai/, deepseek/, moonshotai/, …).
  // BYO declarations use the provider id as the first segment (tencenttokenplan/, …).
  const vendor = raw.slice(0, raw.indexOf('/'));
  const officialish = new Set([
    'openai',
    'anthropic',
    'google',
    'deepseek',
    'deepseek-ai',
    'moonshotai',
    'moonshot-ai',
    'qwen',
    'meta-llama',
    'mistralai',
    'minimax',
    'z-ai',
    'xiaomi',
    'cohere',
    'perplexity',
    'x-ai',
    'nvidia',
    'microsoft',
    'amazon',
  ]);
  if (officialish.has(vendor)) return false;
  // Provider-style ids often contain "token", "plan", "hub", or are long compound names.
  if (/(token|plan|hub|proxy|gateway|byo)/.test(vendor)) return true;
  // Heuristic: vendor with no hyphen and longer than typical author slugs → BYO.
  return vendor.length > 24;
}
