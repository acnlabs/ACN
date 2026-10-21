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

function bareModelId(modelId: string): string {
  const slash = modelId.lastIndexOf("/");
  return (slash >= 0 ? modelId.slice(slash + 1) : modelId).trim().toLowerCase();
}

/** A catalog row matches a model this agent configured, including ids that contain a slash. */
export function reportedStillListed(reportedId: string, liveId: string): boolean {
  const reported = reportedId.trim().toLowerCase();
  const live = liveId.trim().toLowerCase();
  if (!reported || !live) return false;
  if (reported === live || reported.endsWith(`/${live}`)) return true;
  return !live.includes("/") && bareModelId(reported) === live;
}

/** Keep the agent's own order. Models the catalog does not list are dropped. */
export function intersectSupportedModels(reported: string[], liveIds: string[]): string[] {
  return reported.filter((id) => liveIds.some((live) => reportedStillListed(id, live)));
}

function providerListRows(body: unknown): Record<string, unknown>[] {
  if (!body || typeof body !== "object") return [];
  const data = (body as { data?: unknown }).data;
  if (!Array.isArray(data)) return [];
  return data.filter((row): row is Record<string, unknown> => !!row && typeof row === "object");
}

function boolishField(value: unknown): boolean | null {
  if (value === true || value === 1 || value === "true" || value === "1") return true;
  if (value === false || value === 0 || value === "false" || value === "0") return false;
  return null;
}

function trustLooseProviderAuthFields(env?: NodeJS.ProcessEnv): boolean {
  const raw = (env ?? process.env).ACN_PROVIDER_MODELS_TRUST_AVAILABLE?.trim().toLowerCase();
  return raw === "1" || raw === "true" || raw === "yes";
}

/** Per-row Key invoke permission when the provider exposes it on the model list. */
export function rowKeyAuthorized(
  rec: Record<string, unknown>,
  env?: NodeJS.ProcessEnv
): boolean | null {
  const keys = [
    "authorized",
    "key_authorized",
    "keyAuthorized",
    "api_key_authorized",
  ];
  if (trustLooseProviderAuthFields(env)) {
    keys.push("available", "available_for_inference", "inference_enabled", "enabled");
  }
  let sawHint = false;
  for (const key of keys) {
    if (!(key in rec)) continue;
    sawHint = true;
    const b = boolishField(rec[key]);
    if (b === false) return false;
    if (b === true) return true;
  }
  const perm = rec.permission;
  if (perm && typeof perm === "object") {
    sawHint = true;
    for (const key of ["inference", "chat", "invoke"]) {
      if (!(key in (perm as Record<string, unknown>))) continue;
      const b = boolishField((perm as Record<string, unknown>)[key]);
      if (b === false) return false;
      if (b === true) return true;
    }
  }
  return sawHint ? false : null;
}

export type ProviderListParse = {
  /** Models this API key may invoke (provider returned explicit allow hints). */
  keyScopedIds: string[];
  /** Online rows with no per-key hint (allowlist may still admit them). */
  noHintOnlineIds: string[];
  /** Online ids explicitly denied for this API key. */
  keyDeniedIds: string[];
  /** Online product-catalog ids (non-denied). */
  catalogOnlineIds: string[];
  /** True when at least one row carried key-scope hints. */
  hadKeyScopeHints: boolean;
};

export function providerModelsFromListBody(
  body: unknown,
  env?: NodeJS.ProcessEnv
): ProviderListParse {
  const keyScopedIds: string[] = [];
  const noHintOnlineIds: string[] = [];
  const keyDeniedIds: string[] = [];
  const catalogOnlineIds: string[] = [];
  let hadKeyScopeHints = false;
  for (const rec of providerListRows(body)) {
    if (String(rec.status || "online").toLowerCase() === "offline") continue;
    const id = String(rec.id || "").trim();
    if (!id) continue;
    const auth = rowKeyAuthorized(rec, env);
    if (auth !== null) hadKeyScopeHints = true;
    if (auth === false) {
      keyDeniedIds.push(id);
      continue;
    }
    catalogOnlineIds.push(id);
    if (auth === true) keyScopedIds.push(id);
    else noHintOnlineIds.push(id);
  }
  return {
    keyScopedIds: collapseModelSpellings(keyScopedIds),
    noHintOnlineIds: collapseModelSpellings(noHintOnlineIds),
    keyDeniedIds: collapseModelSpellings(keyDeniedIds),
    catalogOnlineIds: collapseModelSpellings(catalogOnlineIds),
    hadKeyScopeHints,
  };
}

/** Online model ids from a provider ``/models``-style list (legacy helper). */
export function catalogModelIds(body: unknown): string[] {
  return providerModelsFromListBody(body).catalogOnlineIds;
}

/** Drop configured ids the provider explicitly denied for this key. */
export function configuredWithoutKeyDenied(
  configured: string[],
  deniedBareIds: string[],
  provider: string
): string[] {
  if (!deniedBareIds.length) return configured;
  const deniedPrefixed = prefixProviderIds(deniedBareIds, provider);
  return configured.filter(
    (id) => !deniedPrefixed.some((denied) => reportedStillListed(id, denied))
  );
}

function uniqueModelIds(ids: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const id of ids) {
    const mark = id.trim().toLowerCase();
    if (!mark || seen.has(mark)) continue;
    seen.add(mark);
    out.push(id.trim());
  }
  return out;
}

function prefixProviderIds(ids: string[], provider: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const id of ids) {
    const named = withProvider(id, provider);
    const mark = named.toLowerCase();
    if (!named || seen.has(mark)) continue;
    seen.add(mark);
    out.push(named);
  }
  return out;
}

export type RuntimeModelReport = {
  supportedModels?: string[];
  modelPrices?: ReportedModelPrice[];
};

/**
 * Resolve what the runtime should heartbeat: provider Key scope when exposed,
 * otherwise runtime allowlist ∩ online catalog. Never publishes a full product
 * catalog when Key scope is unknown and no allowlist was declared.
 */
export function runtimeModelReportFromProviderBody(
  body: unknown,
  opts: { configured?: string[]; provider: string; env?: NodeJS.ProcessEnv }
): RuntimeModelReport {
  const configured = opts.configured ?? [];
  const parsed = providerModelsFromListBody(body, opts.env);
  const keyScoped = prefixProviderIds(parsed.keyScopedIds, opts.provider);
  const catalogOnline = prefixProviderIds(parsed.catalogOnlineIds, opts.provider);

  let supportedModels: string[] | undefined;
  if (parsed.hadKeyScopeHints) {
    const keyAllowed =
      configured.length > 0
        ? intersectSupportedModels(configured, keyScoped)
        : keyScoped;
    const fromNoHint =
      configured.length > 0
        ? intersectSupportedModels(
            configured,
            prefixProviderIds(parsed.noHintOnlineIds, opts.provider)
          )
        : [];
    const merged = uniqueModelIds([...keyAllowed, ...fromNoHint]);
    if (merged.length > 0) {
      supportedModels = merged;
    } else if (configured.length > 0) {
      const withoutDenied = configuredWithoutKeyDenied(
        configured,
        parsed.keyDeniedIds,
        opts.provider
      );
      supportedModels = withoutDenied.length > 0 ? withoutDenied : [];
    } else {
      supportedModels = undefined;
    }
  } else if (configured.length > 0) {
    const intersected = intersectSupportedModels(configured, catalogOnline);
    supportedModels = intersected.length > 0 ? intersected : configured;
  } else {
    supportedModels = undefined;
  }

  const priceRows = [
    ...pricesFromEnv(opts.env),
    ...pricesFromProviderCatalog(body, opts.provider),
  ];
  const modelPrices =
    supportedModels && supportedModels.length
      ? pricesForModels(supportedModels, priceRows)
      : undefined;

  return { supportedModels, modelPrices };
}

export async function fetchRuntimeModelReport(opts: {
  configured?: string[];
  env?: NodeJS.ProcessEnv;
  fetchFn?: typeof fetch;
}): Promise<RuntimeModelReport> {
  const configured = opts.configured;
  const env = opts.env ?? process.env;
  const url = (env.ACN_PROVIDER_MODELS_URL || "").trim();
  const key = (env.ACN_PROVIDER_API_KEY || "").trim();
  const provider = (env.ACN_PROVIDER || "").trim();
  if (!url || !key) {
    return { supportedModels: configured ?? undefined };
  }
  const fetchFn = opts.fetchFn ?? fetch;
  try {
    const res = await fetchFn(url, { headers: { Authorization: `Bearer ${key}` } });
    if (!res.ok) return { supportedModels: configured ?? undefined };
    const body = await res.json();
    const report = runtimeModelReportFromProviderBody(body, {
      configured: configured ?? [],
      provider,
      env,
    });
    if (report.supportedModels !== undefined) return report;
    return { supportedModels: configured ?? undefined };
  } catch {
    return { supportedModels: configured ?? undefined };
  }
}

/** Same model written with different punctuation, including a trailing ".0". */
export function spellingKey(modelId: string): string {
  let text = modelId.trim().toLowerCase();
  text = text.replace(/[._]/g, "-");
  text = text.replace(/([a-z])(\d)/g, "$1-$2");
  text = text.replace(/(\d)([a-z])/g, "$1-$2");
  text = text.replace(/-+/g, "-").replace(/^-|-$/g, "");
  return text.replace(/-0$/, "");
}

/** One id per spelling. The shorter id is the one a caller should send. */
export function collapseModelSpellings(ids: string[]): string[] {
  const groups = new Map<string, string[]>();
  const order: string[] = [];
  for (const raw of ids) {
    const id = raw.trim();
    if (!id) continue;
    const key = spellingKey(id);
    if (!groups.has(key)) {
      groups.set(key, []);
      order.push(key);
    }
    groups.get(key)!.push(id);
  }
  return order.map((key) => {
    const choices = groups.get(key) || [];
    return choices.slice().sort((left, right) => left.length - right.length)[0];
  });
}

function withProvider(modelId: string, provider: string): string {
  const clean = modelId.trim();
  if (!clean || !provider) return clean;
  if (clean.toLowerCase().startsWith(`${provider.toLowerCase()}/`)) return clean;
  return `${provider}/${clean}`;
}

/** Supported models only (see ``fetchRuntimeModelReport`` for prices). */
export async function supportedModelsToReport(opts: {
  configured?: string[];
  env?: NodeJS.ProcessEnv;
  fetchFn?: typeof fetch;
}): Promise<string[] | undefined> {
  const report = await fetchRuntimeModelReport(opts);
  return report.supportedModels;
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
    const row = known.find((item) => reportedStillListed(id, item.model_id));
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
