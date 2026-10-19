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

export type ReportedModelPrice = {
  model_id: string;
  input_price_per_million: number;
  output_price_per_million: number;
};

function finitePrice(value: unknown): number | null {
  const n = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(n) || n < 0) return null;
  return n;
}

function withProvider(modelId: string, provider: string): string {
  const clean = modelId.trim();
  if (!clean || !provider) return clean;
  if (clean.toLowerCase().startsWith(`${provider.toLowerCase()}/`)) return clean;
  return `${provider}/${clean}`;
}

/** Prices this agent can report for the ids the price book missed. */
export function pricesForModels(
  modelIds: string[],
  known: ReportedModelPrice[]
): ReportedModelPrice[] {
  const out: ReportedModelPrice[] = [];
  const seen = new Set<string>();
  for (const id of modelIds) {
    const key = id.trim().toLowerCase();
    if (!key || seen.has(key)) continue;
    const row = known.find((item) => item.model_id.trim().toLowerCase() === key);
    if (!row) continue;
    seen.add(key);
    out.push({
      model_id: id.trim(),
      input_price_per_million: row.input_price_per_million,
      output_price_per_million: row.output_price_per_million,
    });
  }
  return out;
}

/** ``ACN_MODEL_PRICES`` is a JSON list of model_id plus input and output per million. */
export function pricesFromEnv(env?: NodeJS.ProcessEnv): ReportedModelPrice[] {
  const raw = (env ?? process.env).ACN_MODEL_PRICES?.trim();
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  const out: ReportedModelPrice[] = [];
  for (const row of parsed) {
    if (!row || typeof row !== 'object') continue;
    const rec = row as {
      model_id?: unknown;
      input_price_per_million?: unknown;
      output_price_per_million?: unknown;
    };
    const model_id = String(rec.model_id || '').trim().slice(0, 200);
    const input = finitePrice(rec.input_price_per_million);
    const output = finitePrice(rec.output_price_per_million);
    if (!model_id || input == null || output == null) continue;
    out.push({
      model_id,
      input_price_per_million: input,
      output_price_per_million: output,
    });
  }
  return out;
}

/** Provider model-list rows that already carry a per-million price. */
export function pricesFromProviderCatalog(
  body: unknown,
  provider: string
): ReportedModelPrice[] {
  const rows =
    body && typeof body === 'object' && Array.isArray((body as { data?: unknown }).data)
      ? (body as { data: unknown[] }).data
      : [];
  const out: ReportedModelPrice[] = [];
  for (const row of rows) {
    if (!row || typeof row !== 'object') continue;
    const rec = row as {
      id?: unknown;
      input_price_per_million?: unknown;
      output_price_per_million?: unknown;
    };
    const bare = String(rec.id || '').trim();
    const input = finitePrice(rec.input_price_per_million);
    const output = finitePrice(rec.output_price_per_million);
    if (!bare || input == null || output == null) continue;
    out.push({
      model_id: withProvider(bare, provider),
      input_price_per_million: input,
      output_price_per_million: output,
    });
  }
  return out;
}

/** POST /agents/{id}/heartbeat with optional preferred_model / supported_models. */
export async function postAgentHeartbeat(opts: {
  baseUrl: string;
  agentId: string;
  apiKey: string;
  preferredModel?: string;
  /** Present (incl. ``[]``) → send field; omit → leave server value unchanged. */
  supportedModels?: string[];
  modelPrices?: ReportedModelPrice[];
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
  if (opts.modelPrices != null) {
    payload.model_prices = opts.modelPrices;
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
