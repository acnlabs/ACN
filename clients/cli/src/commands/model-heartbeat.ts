/**
 * REST heartbeat helpers for Host Catalog model self-report.
 * Used by `acn listen` and `acn heartbeat` (keep free of WebSocket deps).
 */

/** Resolve runtime model: flag → ACN_PREFERRED_MODEL → empty. */
export function resolvePreferredModel(opts?: {
  model?: string | null;
  env?: NodeJS.ProcessEnv;
}): string | undefined {
  const env = opts?.env ?? process.env;
  const fromFlag = opts?.model?.trim();
  if (fromFlag) return fromFlag.slice(0, 200);
  const fromEnv = env.ACN_PREFERRED_MODEL?.trim();
  if (fromEnv) return fromEnv.slice(0, 200);
  return undefined;
}

/**
 * Resolve supported models: flag → ACN_SUPPORTED_MODELS.
 * ``clear: true`` → ``[]`` (server clears ``metadata.supported_models``).
 * Omit / empty string without clear → ``undefined`` (leave unchanged).
 */
export function resolveSupportedModels(opts?: {
  models?: string | null;
  clear?: boolean;
  env?: NodeJS.ProcessEnv;
}): string[] | undefined {
  if (opts?.clear) return [];
  const env = opts?.env ?? process.env;
  const raw = (opts?.models?.trim() || env.ACN_SUPPORTED_MODELS?.trim() || '').trim();
  if (!raw) return undefined;
  const out: string[] = [];
  const seen = new Set<string>();
  for (const part of raw.split(/[,;\s]+/)) {
    const mid = part.trim().slice(0, 200);
    if (!mid) continue;
    const key = mid.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(mid);
    if (out.length >= 50) break;
  }
  return out.length ? out : undefined;
}

/** POST /agents/{id}/heartbeat with optional preferred_model / supported_models. */
export async function postAgentHeartbeat(opts: {
  baseUrl: string;
  agentId: string;
  apiKey: string;
  preferredModel?: string;
  /** Present (incl. ``[]``) → send field; omit → leave server value unchanged. */
  supportedModels?: string[];
  fetchFn?: typeof fetch;
}): Promise<
  | {
      ok: true;
      preferred_model?: string | null;
      supported_models?: string[] | null;
      desired_preferred_model?: string | null;
    }
  | { ok: false; reason: string }
> {
  const fetchFn = opts.fetchFn ?? fetch;
  const origin = opts.baseUrl.replace(/\/+$/, '');
  const url = `${origin}/api/v1/agents/${opts.agentId}/heartbeat`;
  const headers: Record<string, string> = {
    Authorization: `Bearer ${opts.apiKey}`,
    'Content-Type': 'application/json',
  };
  const payload: Record<string, unknown> = {};
  if (opts.preferredModel && opts.preferredModel.trim()) {
    payload.preferred_model = opts.preferredModel.trim().slice(0, 200);
  }
  if (opts.supportedModels != null) {
    payload.supported_models = opts.supportedModels;
  }
  const body = Object.keys(payload).length ? JSON.stringify(payload) : undefined;
  try {
    const res = await fetchFn(url, { method: 'POST', headers, body });
    if (!res.ok) {
      return { ok: false, reason: `http_${res.status}` };
    }
    let preferred: string | null | undefined;
    let supported: string[] | null | undefined;
    let desired: string | null | undefined;
    try {
      const json = (await res.json()) as {
        preferred_model?: string | null;
        supported_models?: string[] | null;
        desired_preferred_model?: string | null;
      };
      preferred = json.preferred_model;
      supported = json.supported_models;
      desired = json.desired_preferred_model;
    } catch {
      preferred = opts.preferredModel ?? null;
      supported = opts.supportedModels ?? null;
      desired = undefined;
    }
    return {
      ok: true,
      preferred_model: preferred,
      supported_models: supported,
      desired_preferred_model: desired,
    };
  } catch (err) {
    return {
      ok: false,
      reason: err instanceof Error ? err.message : String(err),
    };
  }
}

/** Format model heartbeat log bits (parentheses required for ?? vs ?:). */
export function formatModelHeartbeatLog(opts: {
  preferred?: string | null;
  supported?: string[] | null;
}): string {
  const preferred = (opts.preferred ?? '').trim();
  const supported = opts.supported ?? [];
  const bits = [
    preferred ? `preferred_model=${preferred}` : '',
    supported.length ? `supported_models=${supported.join(',')}` : '',
  ].filter(Boolean);
  return bits.length ? ` ${bits.join(' ')}` : '';
}
