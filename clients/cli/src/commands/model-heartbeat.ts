/**
 * REST heartbeat helpers for Host Catalog model self-report.
 * Used by `acn listen` and `acn heartbeat` (keep free of WebSocket deps).
 */

import { createHash } from "node:crypto";

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

function envFlagTrue(env: NodeJS.ProcessEnv | undefined, name: string): boolean {
  const raw = (env ?? process.env)[name]?.trim().toLowerCase();
  return raw === "1" || raw === "true" || raw === "yes";
}

function trustLooseProviderAuthFields(env?: NodeJS.ProcessEnv): boolean {
  return envFlagTrue(env, "ACN_PROVIDER_MODELS_TRUST_AVAILABLE");
}

/**
 * Opt into treating a bare product catalog (no per-key scope fields) as
 * key-callable. Default is off: TokenPlan-style ``/models`` lists every SKU
 * the package sells, including ones this API key cannot invoke.
 */
export function trustProviderCatalogWithoutKeyScope(
  env?: NodeJS.ProcessEnv
): boolean {
  return envFlagTrue(env, "ACN_PROVIDER_MODELS_TRUST_CATALOG");
}

/** Derive OpenAI-compatible chat URL from a ``…/models`` list URL. */
export function chatCompletionsUrlFromModelsUrl(
  modelsUrl: string
): string | null {
  const cleaned = modelsUrl.trim().replace(/\/+$/, "");
  if (!cleaned.toLowerCase().endsWith("/models")) return null;
  return `${cleaned.slice(0, -"/models".length)}/chat/completions`;
}

function bareIdForProviderCall(modelId: string, provider: string): string {
  const clean = modelId.trim();
  const pref = provider.trim();
  if (!clean || !pref) return clean;
  const needle = `${pref.toLowerCase()}/`;
  if (clean.toLowerCase().startsWith(needle)) return clean.slice(needle.length);
  return clean;
}

function probeErrorText(body: unknown): string {
  if (!body || typeof body !== "object") return "";
  const err = (body as { error?: unknown }).error;
  if (typeof err === "string") return err;
  if (err && typeof err === "object") {
    const rec = err as { message?: unknown; code?: unknown; type?: unknown };
    return [rec.message, rec.code, rec.type].map((v) => String(v ?? "")).join(" ");
  }
  const msg = (body as { message?: unknown }).message;
  return typeof msg === "string" ? msg : "";
}

/** Body says the model/key cannot run (even on some 200 error envelopes). */
export function bodyLooksLikeModelRefusal(body: unknown): boolean {
  const text = probeErrorText(body).toLowerCase();
  if (!text.trim()) return false;
  return (
    text.includes("not authorized") ||
    text.includes("permission") ||
    text.includes("discontinued") ||
    text.includes("no longer") ||
    text.includes("not found") ||
    text.includes("does not exist") ||
    text.includes("invalid model") ||
    text.includes("unknown model") ||
    text.includes("model_not_found") ||
    text.includes("unsupported model")
  );
}

/** TokenPlan-style plan quota empty (business 5xx), not a generic outage. */
export function bodyLooksLikeQuotaExhausted(body: unknown): boolean {
  if (!body || typeof body !== "object") return false;
  const err = (body as { error?: unknown }).error;
  if (err && typeof err === "object") {
    const code = String((err as { code?: unknown }).code ?? "").trim();
    if (code === "20098") return true;
  }
  const text = probeErrorText(body).toLowerCase();
  return (
    text.includes("quota exhausted") ||
    text.includes("token plan quota") ||
    text.includes("20098")
  );
}

/**
 * Classify a chat/completions probe for menu declaration.
 * - ``callable``: keep on the heartbeat card
 * - ``denied``: drop + cache (permission, missing, discontinued, other 4xx)
 * - ``quota_exhausted``: plan/key quota empty — do not keep; may trigger BYO degrade
 * - ``unknown``: fail-open keep (429 / generic 5xx / weird) so a blip does not empty the menu
 */
export function classifyKeyCallProbe(
  status: number,
  body?: unknown
): "callable" | "denied" | "unknown" | "quota_exhausted" {
  if (status === 429) return "unknown";
  if (status >= 200 && status < 300) {
    if (bodyLooksLikeQuotaExhausted(body)) return "quota_exhausted";
    return bodyLooksLikeModelRefusal(body) ? "denied" : "callable";
  }
  if (status === 401 || status === 403) return "denied";
  if (status >= 400 && status < 500) return "denied";
  if (status >= 500) {
    return bodyLooksLikeQuotaExhausted(body) ? "quota_exhausted" : "unknown";
  }
  return "unknown";
}

/** True when the provider refused this key for this model (not a transient error). */
export function isKeyPermissionDenied(
  status: number,
  body?: unknown
): boolean {
  return classifyKeyCallProbe(status, body) === "denied";
}

/** Default how long a clear 401/403 probe result is remembered (1 hour). */
const DEFAULT_PROBE_DENIAL_TTL_MS = 60 * 60 * 1000;
const MAX_PROBE_DENIAL_TTL_MS = 7 * 24 * 60 * 60 * 1000;

type ProbeDenialEntry = { expiresAt: number };

/** In-process: key → model denials so listen heartbeats do not re-burn quota. */
const probeDenialCache = new Map<string, ProbeDenialEntry>();

/**
 * TTL for cached key/model permission denials.
 * ``ACN_PROVIDER_PROBE_DENIAL_TTL_SEC=0`` disables the cache.
 */
export function providerProbeDenialTtlMs(env?: NodeJS.ProcessEnv): number {
  const raw = (env ?? process.env).ACN_PROVIDER_PROBE_DENIAL_TTL_SEC?.trim();
  if (raw === "0") return 0;
  if (raw) {
    const sec = Number(raw);
    if (Number.isFinite(sec) && sec > 0) {
      return Math.min(sec * 1000, MAX_PROBE_DENIAL_TTL_MS);
    }
  }
  return DEFAULT_PROBE_DENIAL_TTL_MS;
}

function apiKeyFingerprint(apiKey: string): string {
  return createHash("sha256").update(apiKey).digest("hex").slice(0, 16);
}

function probeDenialCacheKey(
  chatUrl: string,
  apiKey: string,
  modelId: string
): string {
  return [
    chatUrl.trim().toLowerCase(),
    apiKeyFingerprint(apiKey),
    modelId.trim().toLowerCase(),
  ].join("|");
}

function readCachedDenial(key: string, nowMs: number): boolean {
  const hit = probeDenialCache.get(key);
  if (!hit) return false;
  if (hit.expiresAt <= nowMs) {
    probeDenialCache.delete(key);
    return false;
  }
  return true;
}

function writeCachedDenial(key: string, ttlMs: number, nowMs: number): void {
  if (ttlMs <= 0) return;
  probeDenialCache.set(key, { expiresAt: nowMs + ttlMs });
}

/** Test helper — drop remembered 401/403 probe results. */
export function clearProviderProbeDenialCache(): void {
  probeDenialCache.clear();
}

export type KeyCallProbeFilterResult = {
  models: string[];
  /** Every completed probe was plan quota empty (no keep). Triggers BYO degrade. */
  quotaExhausted: boolean;
};

/**
 * Drop models this API key cannot invoke. Used when ``/models`` has no
 * per-row key-scope hints. Keep clear successes and generic 429/5xx fail-open.
 * Quota-exhausted answers are dropped (not fail-open). Denied 4xx are cached.
 */
export async function filterModelsCallableByKey(opts: {
  chatUrl: string;
  apiKey: string;
  provider: string;
  modelIds: string[];
  fetchFn?: typeof fetch;
  concurrency?: number;
  env?: NodeJS.ProcessEnv;
  nowMs?: number;
}): Promise<KeyCallProbeFilterResult> {
  const fetchFn = opts.fetchFn ?? fetch;
  const concurrency = Math.max(1, Math.min(opts.concurrency ?? 4, 8));
  const ttlMs = providerProbeDenialTtlMs(opts.env);
  const nowMs = opts.nowMs ?? Date.now();
  const kept: string[] = [];
  let cursor = 0;
  let probed = 0;
  let quotaHits = 0;

  async function probeOne(modelId: string): Promise<void> {
    const bare = bareIdForProviderCall(modelId, opts.provider);
    if (!bare) return;
    const cacheKey = probeDenialCacheKey(opts.chatUrl, opts.apiKey, modelId);
    if (readCachedDenial(cacheKey, nowMs)) return;
    try {
      const res = await fetchFn(opts.chatUrl, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${opts.apiKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          model: bare,
          messages: [{ role: "user", content: "ping" }],
          max_tokens: 1,
        }),
      });
      let body: unknown = null;
      try {
        body = await res.json();
      } catch {
        body = null;
      }
      probed += 1;
      const kind = classifyKeyCallProbe(res.status, body);
      if (kind === "denied") {
        writeCachedDenial(cacheKey, ttlMs, nowMs);
        return;
      }
      if (kind === "quota_exhausted") {
        quotaHits += 1;
        return;
      }
      // callable + unknown (429 / generic 5xx): keep
      kept.push(modelId);
    } catch {
      // Network / parse errors: keep (fail open).
      probed += 1;
      kept.push(modelId);
    }
  }

  const workers = Array.from({ length: concurrency }, async () => {
    while (cursor < opts.modelIds.length) {
      const idx = cursor++;
      await probeOne(opts.modelIds[idx]);
    }
  });
  await Promise.all(workers);
  // Preserve caller order.
  const allowed = new Set(kept.map((id) => id.trim().toLowerCase()));
  const models = opts.modelIds.filter((id) =>
    allowed.has(id.trim().toLowerCase())
  );
  const quotaExhausted =
    probed > 0 && quotaHits === probed && models.length === 0;
  return { models, quotaExhausted };
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
  /** Online rows with no per-key hint. Included in the declaration; not filtered by a hand allowlist. */
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

export type ReportedProvider = {
  id: string;
  models: string[];
  current: boolean;
};

export type RuntimeModelReport = {
  /** Derived union. Present so older servers still store a flat list. */
  supportedModels?: string[];
  /** Declaration: every provider this runtime supports, and which is current. */
  providers?: ReportedProvider[];
  modelPrices?: ReportedModelPrice[];
  /**
   * True when every probed provider returned plan quota exhausted and no
   * models were kept. Listen uses this for BYO → official degrade.
   */
  quotaExhausted?: boolean;
  /**
   * True when a provider catalog call returned 401/403 (bad or rotated key).
   * Listen must still run the degrade tick — never treat this as “no report”.
   */
  keyInvalid?: boolean;
};

/** A provider `/models` body is a list only when `data` is an array. */
export function providerBodyHasModelList(body: unknown): boolean {
  return !!body && typeof body === "object" && Array.isArray((body as { data?: unknown }).data);
}

/**
 * Models this provider supports for the key that fetched the list.
 * Explicit key denies are omitted. An empty list means the key can run nothing.
 *
 * When the list has **no** per-key scope fields, the ids are only a product
 * catalog until probed (see ``fetchRuntimeModelReport``) or
 * ``ACN_PROVIDER_MODELS_TRUST_CATALOG=1``.
 */
export function runtimeModelReportFromProviderBody(
  body: unknown,
  opts: { provider: string; env?: NodeJS.ProcessEnv }
): RuntimeModelReport & { needsKeyCallProbe?: boolean } {
  const parsed = providerModelsFromListBody(body, opts.env);
  const keyScoped = prefixProviderIds(parsed.keyScopedIds, opts.provider);
  const catalogOnline = prefixProviderIds(parsed.catalogOnlineIds, opts.provider);

  let supportedModels: string[];
  let needsKeyCallProbe = false;
  if (parsed.hadKeyScopeHints) {
    supportedModels = uniqueModelIds([
      ...keyScoped,
      ...prefixProviderIds(parsed.noHintOnlineIds, opts.provider),
    ]);
  } else if (trustProviderCatalogWithoutKeyScope(opts.env)) {
    supportedModels = catalogOnline;
  } else {
    // Product shelf only — do not claim key access until probed.
    supportedModels = catalogOnline;
    needsKeyCallProbe = catalogOnline.length > 0;
  }

  const priceRows = [
    ...pricesFromEnv(opts.env),
    ...pricesFromProviderCatalog(body, opts.provider),
  ];
  const modelPrices =
    supportedModels && supportedModels.length
      ? pricesForModels(supportedModels, priceRows)
      : undefined;

  const providerId = opts.provider.trim();
  const providers: ReportedProvider[] | undefined =
    providerId && supportedModels
      ? [{ id: providerId, models: supportedModels, current: true }]
      : undefined;

  return { supportedModels, providers, modelPrices, needsKeyCallProbe };
}

export type ProviderEndpoint = {
  id: string;
  modelsUrl: string;
  apiKey: string;
  current: boolean;
};

function providerEndpointsFromEnv(env: NodeJS.ProcessEnv): ProviderEndpoint[] {
  const rows: ProviderEndpoint[] = [];
  const raw = (env.ACN_PROVIDERS || "").trim();
  if (raw) {
    try {
      const data = JSON.parse(raw) as unknown;
      if (Array.isArray(data)) {
        for (const item of data) {
          if (!item || typeof item !== "object") continue;
          const rec = item as Record<string, unknown>;
          const id = String(rec.id || "").trim().slice(0, 80);
          const modelsUrl = String(rec.models_url || rec.modelsUrl || "").trim();
          const inlineKey = String(rec.api_key || rec.apiKey || "").trim();
          const keyEnv = String(rec.api_key_env || rec.apiKeyEnv || "").trim();
          const apiKey = inlineKey || (keyEnv ? String(env[keyEnv] || "").trim() : "");
          if (!id || !modelsUrl || !apiKey) continue;
          if (rows.some((row) => row.id.toLowerCase() === id.toLowerCase())) continue;
          rows.push({ id, modelsUrl, apiKey, current: rec.current === true });
          if (rows.length >= 20) break;
        }
      }
    } catch {
      /* a bad extra list does not hide the current provider triple */
    }
  }
  const id = (env.ACN_PROVIDER || "").trim();
  const modelsUrl = (env.ACN_PROVIDER_MODELS_URL || "").trim();
  const apiKey = (env.ACN_PROVIDER_API_KEY || "").trim();
  if (id && modelsUrl && apiKey) {
    const existing = rows.find((row) => row.id.toLowerCase() === id.toLowerCase());
    if (existing) {
      if (!existing.modelsUrl) existing.modelsUrl = modelsUrl;
      if (!existing.apiKey) existing.apiKey = apiKey;
    } else {
      rows.unshift({ id, modelsUrl, apiKey, current: false });
    }
  }
  const usable = rows.filter((row) => row.id && row.modelsUrl && row.apiKey).slice(0, 20);
  if (usable.length && !usable.some((row) => row.current)) {
    const primary = usable.find((row) => row.id.toLowerCase() === id.toLowerCase());
    (primary ?? usable[0]).current = true;
  }
  let marked = false;
  for (const row of usable) {
    if (!row.current) continue;
    if (marked) row.current = false;
    else marked = true;
  }
  return usable;
}

export async function fetchRuntimeModelReport(opts: {
  env?: NodeJS.ProcessEnv;
  fetchFn?: typeof fetch;
}): Promise<RuntimeModelReport> {
  const env = opts.env ?? process.env;
  const endpoints = providerEndpointsFromEnv(env);
  if (!endpoints.length) return {};
  const fetchFn = opts.fetchFn ?? fetch;
  const providers: ReportedProvider[] = [];
  const priceRows: ReportedModelPrice[] = [];
  let anyProbe = false;
  let allQuota = true;
  let keyInvalid = false;
  for (const endpoint of endpoints) {
    try {
      const res = await fetchFn(endpoint.modelsUrl, {
        headers: { Authorization: `Bearer ${endpoint.apiKey}` },
      });
      if (!res.ok) {
        // Never return ``{}`` here — listen would skip the degrade tick and
        // leave a sticky ``byo_degraded`` on ACN after a rotated/invalid key.
        if (res.status === 401 || res.status === 403) keyInvalid = true;
        allQuota = false;
        providers.push({
          id: endpoint.id,
          models: [],
          current: endpoint.current,
        });
        continue;
      }
      const body = await res.json();
      if (!providerBodyHasModelList(body)) {
        allQuota = false;
        providers.push({
          id: endpoint.id,
          models: [],
          current: endpoint.current,
        });
        continue;
      }
      let report = runtimeModelReportFromProviderBody(body, {
        provider: endpoint.id,
        env,
      });
      if (report.needsKeyCallProbe) {
        const chatUrl = chatCompletionsUrlFromModelsUrl(endpoint.modelsUrl);
        if (!chatUrl) {
          // Cannot verify key access — do not advertise the product shelf.
          report = {
            supportedModels: [],
            providers: [{ id: endpoint.id, models: [], current: true }],
            modelPrices: undefined,
          };
          allQuota = false;
        } else {
          const filtered = await filterModelsCallableByKey({
            chatUrl,
            apiKey: endpoint.apiKey,
            provider: endpoint.id,
            modelIds: report.supportedModels ?? [],
            fetchFn,
            env,
          });
          anyProbe = true;
          if (!filtered.quotaExhausted) allQuota = false;
          const callable = filtered.models;
          const priceSource = [
            ...pricesFromEnv(env),
            ...pricesFromProviderCatalog(body, endpoint.id),
          ];
          report = {
            supportedModels: callable,
            providers: [
              { id: endpoint.id, models: callable, current: true },
            ],
            modelPrices: callable.length
              ? pricesForModels(callable, priceSource)
              : undefined,
          };
        }
      } else {
        allQuota = false;
      }
      providers.push({
        id: endpoint.id,
        models: report.supportedModels ?? [],
        current: endpoint.current,
      });
      if (report.modelPrices?.length) priceRows.push(...report.modelPrices);
    } catch {
      allQuota = false;
      providers.push({
        id: endpoint.id,
        models: [],
        current: endpoint.current,
      });
    }
  }
  if (!providers.length) return {};
  if (!providers.some((row) => row.current)) providers[0].current = true;
  const supportedModels = uniqueModelIds(providers.flatMap((row) => row.models));
  const modelPrices = supportedModels.length
    ? pricesForModels(supportedModels, priceRows)
    : undefined;
  const quotaExhausted =
    anyProbe && allQuota && supportedModels.length === 0;
  return {
    supportedModels,
    providers,
    modelPrices: modelPrices?.length ? modelPrices : undefined,
    quotaExhausted: quotaExhausted || undefined,
    keyInvalid: keyInvalid || undefined,
  };
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
  providers?: ReportedProvider[];
  modelPrices?: ReportedModelPrice[];
  /** Owner UI flag; omit leaves prior value. */
  byoDegraded?: boolean;
  /** Owner UI: provider catalog rejected this API key (401/403). */
  byoKeyInvalid?: boolean;
  fetchFn?: typeof fetch;
}): Promise<
  | {
      ok: true;
      preferred_model?: string | null;
      supported_models?: string[] | null;
      desired_preferred_model?: string | null;
      byo_degraded?: boolean | null;
      byo_key_invalid?: boolean | null;
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
  if (opts.providers != null) {
    payload.providers = opts.providers;
  }
  if (opts.supportedModels != null) {
    payload.supported_models = opts.supportedModels;
  }
  if (opts.modelPrices != null) {
    payload.model_prices = opts.modelPrices;
  }
  if (opts.byoDegraded !== undefined) {
    payload.byo_degraded = opts.byoDegraded;
  }
  if (opts.byoKeyInvalid !== undefined) {
    payload.byo_key_invalid = opts.byoKeyInvalid;
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
    let byoDegraded: boolean | null | undefined;
    let byoKeyInvalid: boolean | null | undefined;
    try {
      const json = (await res.json()) as {
        preferred_model?: string | null;
        supported_models?: string[] | null;
        desired_preferred_model?: string | null;
        byo_degraded?: boolean | null;
        byo_key_invalid?: boolean | null;
      };
      preferred = json.preferred_model;
      supported = json.supported_models;
      desired = json.desired_preferred_model;
      byoDegraded =
        typeof json.byo_degraded === 'boolean' ? json.byo_degraded : undefined;
      byoKeyInvalid =
        typeof json.byo_key_invalid === 'boolean'
          ? json.byo_key_invalid
          : undefined;
    } catch {
      preferred = opts.preferredModel ?? null;
      supported = opts.supportedModels ?? null;
      desired = undefined;
      byoDegraded = undefined;
      byoKeyInvalid = undefined;
    }
    return {
      ok: true,
      preferred_model: preferred,
      supported_models: supported,
      desired_preferred_model: desired,
      byo_degraded: byoDegraded,
      byo_key_invalid: byoKeyInvalid,
    };
  } catch (err) {
    return {
      ok: false,
      reason: err instanceof Error ? err.message : String(err),
    };
  }
}

/** Fields to send for the model declaration. A failed read sends nothing. */
export function declarationHeartbeatFields(opts: {
  clear: boolean;
  report: RuntimeModelReport;
}): Pick<RuntimeModelReport, 'supportedModels' | 'providers' | 'modelPrices'> {
  if (opts.clear) {
    // providers:[] clears the declaration; supported_models:[] clears leftovers.
    return { supportedModels: [], providers: [], modelPrices: undefined };
  }
  if (!opts.report.providers) return {};
  return {
    // Flat supported_models is retired — Host reads providers only.
    providers: opts.report.providers,
    modelPrices: opts.report.modelPrices,
  };
}

/** Format model heartbeat log bits (parentheses required for ?? vs ?:). */
export function formatModelHeartbeatLog(opts: {
  preferred?: string | null;
  supported?: string[] | null;
}): string {
  const preferred = (opts.preferred ?? '').trim();
  const supported = opts.supported;
  const supportedNote =
    supported == null
      ? ''
      : supported.length
        ? `supported_models=${supported.join(',')}`
        : 'supported_models=(cleared)';
  const bits = [
    preferred ? `preferred_model=${preferred}` : '',
    supportedNote,
  ].filter(Boolean);
  return bits.length ? ` ${bits.join(' ')}` : '';
}
