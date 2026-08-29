/**
 * Unified Interfaze / Chat Gateway writeback for Mode B listen.
 *
 * When a relayed A2A message carries metadata.agentplanet (chat_id + reply_path),
 * the CLI:
 *   1) completes the hop:
 *        official + exec/url → door + agent complete; Host must have seen the hop
 *        official, no exec   → POST Host /chat/completions (CLI-owned; omit usage)
 *        byo                 → --chat-complete-url | --chat-complete-exec
 *   2) mints a short-lived ACN agent JWT via POST /oauth/token (acn_* API key)
 *   3) POSTs { content, reply_to_id?, usage? } to Chat Gateway agent-messages
 *      with Bearer JWT
 *
 * Hosts return {"content":"..."} and optionally usage (in/out billed;
 * extras stored). See skills/acn/references/INTERFAZE.md.
 * They do not call Gateway themselves.
 */

import { spawn } from 'child_process';
import type { ChildProcess } from 'child_process';
import type { NormalizedEvent } from './normalize-event.js';
import {
  asHostInferenceUrl,
  isAllowedChatReplyPath,
} from './normalize-event.js';
import {
  canCompleteOfficialHop,
  startOfficialHopDoor,
  type OfficialHopDoor,
} from './official-hop-door.js';

export interface ChatWritebackOptions {
  enabled: boolean;
  /** Chat Gateway origin, e.g. https://api.agentplanet.org */
  apiBase: string;
  /** ACN origin for /oauth/token, e.g. https://api.acnlabs.dev */
  acnBaseUrl: string;
  /** Long-lived acn_* API key (client_secret for JWT mint). */
  apiKey: string;
  /** This listener's ACN agent_id (client_id for JWT mint; must match key). */
  agentId: string;
  /** JWT audience expected by Chat Gateway (default: apiBase origin). */
  audience: string;
  /** POST NormalizedEvent → JSON {"content":"..."} */
  completeUrl?: string;
  /** Shell: event JSON on stdin → stdout JSON {"content":"..."} */
  completeExec?: string;
  completeTimeoutMs?: number;
  /** Timeout for the Gateway agent-messages POST (default 30s). */
  writebackTimeoutMs?: number;
}

export type ChatWritebackResult =
  | { ok: true; httpStatus: number }
  | { ok: false; reason: string };

export interface ChatWritebackDeps {
  fetchFn?: typeof fetch;
  spawnFn?: typeof spawn;
  logFn?: (line: string) => void;
}

const DEFAULT_COMPLETE_TIMEOUT_MS = 120_000;
/** Beat Interfaze's 20×2s reply poll so an official hang writebacks before the spinner dies. */
const DEFAULT_OFFICIAL_COMPLETE_TIMEOUT_MS = 28_000;
const JWT_MINT_ATTEMPTS = 3;
const DEFAULT_WRITEBACK_TIMEOUT_MS = 30_000;
/** Must match AgentPlanet ``ACN_JWT_AUDIENCE`` (not the chat-api-base host). */
export const DEFAULT_CHAT_JWT_AUDIENCE = 'https://api.agentplanet.org';

/** Process-local JWT cache (restart clears). */
let cachedJwt: { token: string; expEpochSec: number; agentId: string } | null =
  null;

function asRecord(v: unknown): Record<string, unknown> | null {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : null;
}

/** Validate chat-writeback flags when --chat-writeback is set. */
export function validateChatWritebackOptions(opts: {
  chatWriteback?: boolean;
  chatApiBase?: string;
  chatCompleteUrl?: string;
  chatCompleteExec?: string;
  agentId?: string;
  apiKey?: string;
}): string | null {
  if (!opts.chatWriteback) return null;
  if (!opts.agentId) {
    return '--chat-writeback requires a known agent id (join / --agent-id).';
  }
  if (!opts.apiKey?.trim()) {
    return '--chat-writeback requires an ACN API key (acn join / config set api-key).';
  }
  if (!opts.chatApiBase?.trim()) {
    return '--chat-writeback requires --chat-api-base (or ACN_CHAT_API_BASE / AGENTPLANET_API_BASE).';
  }
  const hasUrl = Boolean(opts.chatCompleteUrl?.trim());
  const hasExec = Boolean(opts.chatCompleteExec?.trim());
  if (hasUrl && hasExec) {
    return (
      '--chat-writeback: --chat-complete-url and --chat-complete-exec are mutually exclusive ' +
      '(omit both for official-only; BYO hops need exactly one).'
    );
  }
  return null;
}

export function buildChatWritebackOptions(opts: {
  chatWriteback?: boolean;
  chatApiBase?: string;
  acnBaseUrl: string;
  apiKey: string;
  chatCompleteUrl?: string;
  chatCompleteExec?: string;
  chatCompleteTimeoutMs?: number;
  chatWritebackTimeoutMs?: number;
  agentId: string;
  audience?: string;
}): ChatWritebackOptions | undefined {
  if (!opts.chatWriteback) return undefined;
  const apiBase = opts.chatApiBase!.replace(/\/+$/, '');
  const audience = opts.audience?.trim() || DEFAULT_CHAT_JWT_AUDIENCE;
  return {
    enabled: true,
    apiBase,
    acnBaseUrl: opts.acnBaseUrl.replace(/\/+$/, ''),
    apiKey: opts.apiKey,
    agentId: opts.agentId,
    audience,
    completeUrl: opts.chatCompleteUrl?.trim() || undefined,
    completeExec: opts.chatCompleteExec?.trim() || undefined,
    completeTimeoutMs: opts.chatCompleteTimeoutMs,
    writebackTimeoutMs: opts.chatWritebackTimeoutMs,
  };
}

export type ChatTokenUsage = {
  input_tokens: number;
  output_tokens: number;
  meter_source?: 'peer_self' | 'gateway' | 'runtime_attested' | 'protocol';
  /** Host Catalog id used for this hop — self-reported; soft mismatch vs listing. */
  model_id?: string;
  reasoning_tokens?: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  total_tokens?: number;
  duration_ms?: number;
  provider?: string;
};

export type ChatCompleteResult = {
  content: string;
  usage?: ChatTokenUsage;
  /** Top-level complete.model_id when no token usage is present. */
  modelId?: string;
};

/**
 * Host complete response → reply text.
 * Prefer content/reply/text; never treat a nested "message" object or ACK as content.
 */
export function extractContent(payload: unknown): string | null {
  const rec = asRecord(payload);
  if (!rec) return null;
  for (const key of ['content', 'reply', 'text']) {
    const v = rec[key];
    if (typeof v === 'string' && v.trim()) {
      const t = v.trim();
      if (t.toLowerCase() === 'accepted') continue;
      return t;
    }
  }
  return null;
}

/**
 * OpenAI-compatible Host /chat/completions → reply text.
 * Official hops omit writeback usage; Host meters what it saw.
 */
export function extractChatCompletionContent(payload: unknown): string | null {
  const rec = asRecord(payload);
  if (!rec) return null;
  const choices = rec.choices;
  if (Array.isArray(choices) && choices.length > 0) {
    const first = asRecord(choices[0]);
    if (first) {
      const msg = asRecord(first.message);
      const fromMsg = msg?.content;
      if (typeof fromMsg === 'string' && fromMsg.trim()) {
        const t = fromMsg.trim();
        if (t.toLowerCase() !== 'accepted') return t;
      }
      const fromText = first.text;
      if (typeof fromText === 'string' && fromText.trim()) {
        const t = fromText.trim();
        if (t.toLowerCase() !== 'accepted') return t;
      }
    }
  }
  return extractContent(payload);
}

function asNonNegInt(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v) && v >= 0) {
    return Math.floor(v);
  }
  if (typeof v === 'string' && v.trim() !== '') {
    const n = Number(v);
    if (Number.isFinite(n) && n >= 0) return Math.floor(n);
  }
  return null;
}

/** Host Catalog model id from complete JSON (usage.* or top-level). */
export function extractModelId(payload: unknown): string | undefined {
  const rec = asRecord(payload);
  if (!rec) return undefined;
  const usageRec = asRecord(rec.usage);
  for (const raw of [
    usageRec?.model_id,
    usageRec?.model,
    rec.model_id,
    rec.model,
  ]) {
    if (typeof raw === 'string' && raw.trim()) return raw.trim().slice(0, 200);
  }
  return undefined;
}

/**
 * Optional token usage from host complete JSON (chat billing settle).
 * Accepts usage.input_tokens/output_tokens or prompt_tokens/completion_tokens.
 * model_id-only complete payloads do NOT invent zero-token usage — use
 * {@link extractModelId} / writeback `usage: { model_id }` instead.
 */
export function extractUsage(payload: unknown): ChatTokenUsage | undefined {
  const rec = asRecord(payload);
  if (!rec) return undefined;
  const usageRec = asRecord(rec.usage) ?? rec;
  const input =
    asNonNegInt(usageRec.input_tokens) ??
    asNonNegInt(usageRec.prompt_tokens) ??
    asNonNegInt(usageRec.input);
  const output =
    asNonNegInt(usageRec.output_tokens) ??
    asNonNegInt(usageRec.completion_tokens) ??
    asNonNegInt(usageRec.output);
  if (input === null && output === null) return undefined;
  const out: ChatTokenUsage = {
    input_tokens: input ?? 0,
    output_tokens: output ?? 0,
  };
  const ms = usageRec.meter_source;
  if (
    ms === 'peer_self' ||
    ms === 'gateway' ||
    ms === 'runtime_attested' ||
    ms === 'protocol'
  ) {
    out.meter_source = ms;
  }
  const modelId = extractModelId(payload);
  if (modelId) out.model_id = modelId;
  const reasoning =
    asNonNegInt(usageRec.reasoning_tokens) ?? asNonNegInt(usageRec.reasoningTokens);
  const cacheRead =
    asNonNegInt(usageRec.cache_read_tokens) ?? asNonNegInt(usageRec.cacheRead);
  const cacheWrite =
    asNonNegInt(usageRec.cache_write_tokens) ?? asNonNegInt(usageRec.cacheWrite);
  const total = asNonNegInt(usageRec.total_tokens) ?? asNonNegInt(usageRec.total);
  const duration =
    asNonNegInt(usageRec.duration_ms) ??
    asNonNegInt(usageRec.durationMs) ??
    asNonNegInt(rec.duration_ms) ??
    asNonNegInt(rec.durationMs);
  const provider =
    (typeof usageRec.provider === 'string' && usageRec.provider.trim()) ||
    (typeof rec.provider === 'string' && rec.provider.trim()) ||
    '';
  if (reasoning !== null) out.reasoning_tokens = reasoning;
  if (cacheRead !== null) out.cache_read_tokens = cacheRead;
  if (cacheWrite !== null) out.cache_write_tokens = cacheWrite;
  if (total !== null) out.total_tokens = total;
  if (duration !== null) out.duration_ms = duration;
  if (provider) out.provider = provider.slice(0, 80);
  return out;
}

function parseCompletePayload(
  payload: unknown
): { ok: true; result: ChatCompleteResult } | { ok: false; reason: string } {
  const content = extractContent(payload);
  if (!content) return { ok: false, reason: 'complete_missing_content' };
  const usage = extractUsage(payload);
  const modelId = extractModelId(payload);
  const result: ChatCompleteResult = { content };
  if (usage) result.usage = usage;
  else if (modelId) result.modelId = modelId;
  return { ok: true, result };
}

/** Mint (or reuse cached) ACN agent JWT for Chat Gateway. Exported for tests. */
export async function mintAgentJwt(
  opts: Pick<ChatWritebackOptions, 'acnBaseUrl' | 'apiKey' | 'agentId' | 'audience'>,
  fetchFn: typeof fetch = fetch
): Promise<{ ok: true; token: string } | { ok: false; reason: string }> {
  const now = Math.floor(Date.now() / 1000);
  if (
    cachedJwt &&
    cachedJwt.agentId === opts.agentId &&
    cachedJwt.expEpochSec > now + 60
  ) {
    return { ok: true, token: cachedJwt.token };
  }

  const url = `${opts.acnBaseUrl.replace(/\/+$/, '')}/oauth/token`;
  let lastReason = 'oauth_failed';
  for (let attempt = 1; attempt <= JWT_MINT_ATTEMPTS; attempt++) {
    try {
      const res = await fetchFn(url, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          grant_type: 'client_credentials',
          client_id: opts.agentId,
          client_secret: opts.apiKey,
          audience: opts.audience,
        }),
      });
      const text = await res.text();
      if (res.status < 200 || res.status >= 300) {
        lastReason = `oauth_http_${res.status}`;
      } else {
        let parsed: unknown;
        try {
          parsed = JSON.parse(text);
        } catch {
          lastReason = 'oauth_invalid_json';
          continue;
        }
        const rec = asRecord(parsed);
        const token =
          typeof rec?.access_token === 'string' ? rec.access_token.trim() : '';
        if (!token) {
          lastReason = 'oauth_missing_access_token';
          continue;
        }
        const expiresIn =
          typeof rec?.expires_in === 'number' && rec.expires_in > 0
            ? rec.expires_in
            : 1800;
        cachedJwt = {
          token,
          agentId: opts.agentId,
          expEpochSec: now + expiresIn,
        };
        return { ok: true, token };
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      lastReason = msg.slice(0, 200);
    }
  }
  return { ok: false, reason: lastReason };
}

/** Test helper: clear process-local JWT cache. */
export function clearAgentJwtCache(): void {
  cachedJwt = null;
}

/** Env injected into --chat-complete-exec for official / BYO hops. */
export function completeInferenceEnv(
  event: NormalizedEvent,
  opts: Pick<ChatWritebackOptions, 'agentId'>,
  jwt?: string | null,
  door?: Pick<OfficialHopDoor, 'baseUrl'> | null
): NodeJS.ProcessEnv {
  const extra: Record<string, string> = { ACN_AGENT_ID: opts.agentId };
  const chat = event.chat;
  if (chat?.hop_id) extra.ACN_CHAT_HOP_ID = chat.hop_id;
  if (chat?.inference_path) extra.ACN_INFERENCE_PATH = chat.inference_path;
  if (chat?.host_inference_url) {
    extra.ACN_HOST_INFERENCE_URL = chat.host_inference_url;
  }
  if (jwt) extra.ACN_AGENT_JWT = jwt;
  if (door?.baseUrl && jwt) {
    extra.OPENAI_BASE_URL = door.baseUrl;
    extra.OPENAI_API_KEY = jwt;
  }
  return { ...process.env, ...extra };
}

function completeInferenceHeaders(
  event: NormalizedEvent,
  opts: Pick<ChatWritebackOptions, 'agentId'>,
  jwt?: string | null,
  door?: Pick<OfficialHopDoor, 'baseUrl'> | null
): Record<string, string> {
  const headers: Record<string, string> = {
    'content-type': 'application/json',
  };
  if (opts.agentId) headers['X-ACN-Agent-Id'] = opts.agentId;
  const chat = event.chat;
  if (chat?.hop_id) headers['X-ACN-Hop-Id'] = chat.hop_id;
  if (chat?.inference_path) headers['X-ACN-Inference-Path'] = chat.inference_path;
  if (chat?.host_inference_url) {
    headers['X-ACN-Host-Inference-Url'] = chat.host_inference_url;
  }
  if (jwt) headers['X-ACN-Agent-Jwt'] = jwt;
  if (door?.baseUrl && jwt) {
    headers['X-ACN-OpenAI-Base-Url'] = door.baseUrl;
    headers['X-ACN-OpenAI-Api-Key'] = jwt;
  }
  return headers;
}

async function completeViaHttp(
  event: NormalizedEvent,
  opts: ChatWritebackOptions,
  deps: ChatWritebackDeps,
  jwt?: string | null,
  door?: OfficialHopDoor | null
): Promise<
  { ok: true; result: ChatCompleteResult } | { ok: false; reason: string }
> {
  const fetchFn = deps.fetchFn ?? fetch;
  const timeoutMs = opts.completeTimeoutMs ?? DEFAULT_COMPLETE_TIMEOUT_MS;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetchFn(opts.completeUrl!, {
      method: 'POST',
      headers: completeInferenceHeaders(event, opts, jwt, door),
      body: JSON.stringify(event),
      signal: controller.signal,
    });
    const text = await res.text();
    if (res.status < 200 || res.status >= 300) {
      return { ok: false, reason: `complete_http_${res.status}` };
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      return { ok: false, reason: 'complete_invalid_json' };
    }
    return parseCompletePayload(parsed);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (controller.signal.aborted || /abort|timeout/i.test(msg)) {
      return { ok: false, reason: 'complete_timeout' };
    }
    return { ok: false, reason: msg.slice(0, 200) };
  } finally {
    clearTimeout(timer);
  }
}

function spawnCompleteExec(
  event: NormalizedEvent,
  opts: ChatWritebackOptions,
  deps: ChatWritebackDeps,
  jwt?: string | null,
  door?: OfficialHopDoor | null
): Promise<
  { ok: true; result: ChatCompleteResult } | { ok: false; reason: string }
> {
  const spawnFn = deps.spawnFn ?? spawn;
  const timeoutMs = opts.completeTimeoutMs ?? DEFAULT_COMPLETE_TIMEOUT_MS;
  const body = Buffer.from(JSON.stringify(event), 'utf-8');

  return new Promise((resolve) => {
    let settled = false;
    const child: ChildProcess = spawnFn(opts.completeExec!, {
      shell: true,
      env: completeInferenceEnv(event, opts, jwt, door),
    });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];

    const finish = (
      result:
        | { ok: true; result: ChatCompleteResult }
        | { ok: false; reason: string }
    ) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };

    const timer = setTimeout(() => {
      child.kill('SIGTERM');
      finish({ ok: false, reason: 'complete_timeout' });
    }, timeoutMs);

    child.stdout?.on('data', (d: Buffer) => stdout.push(Buffer.from(d)));
    child.stderr?.on('data', (d: Buffer) => stderr.push(Buffer.from(d)));
    child.on('error', (e: Error) =>
      finish({ ok: false, reason: e.message.slice(0, 200) })
    );
    child.on('close', (code: number | null) => {
      if (code !== 0) {
        const detail = Buffer.concat(stderr).toString('utf-8').slice(0, 80);
        finish({
          ok: false,
          reason: detail ? `complete_exit_${code}:${detail}` : `complete_exit_${code}`,
        });
        return;
      }
      const text = Buffer.concat(stdout).toString('utf-8').trim();
      try {
        const parsed = JSON.parse(text) as unknown;
        finish(parseCompletePayload(parsed));
      } catch {
        finish({ ok: false, reason: 'complete_invalid_json' });
      }
    });

    child.stdin?.end(body);
  });
}

/** Keep in lockstep with backend official_v0_supports_model and Interfaze. */
const OFFICIAL_V0_O_SERIES = /(^|\/)o[134](?:$|[-/:])/;

export function officialV0SupportsModel(modelId?: string | null): boolean {
  const id = (modelId || '').trim().toLowerCase();
  if (!id) return true;
  if (id.includes('-think') || id.includes(':thinking') || id.includes('reasoning')) {
    return false;
  }
  if (id.includes('deepseek-r1')) return false;
  return !OFFICIAL_V0_O_SERIES.test(id);
}

export function officialCompleteFailureContent(
  reason: string,
  model?: string | null
): string {
  const mid = (model || '').trim();
  if (mid && !officialV0SupportsModel(mid)) {
    return (
      `Official v0 is a single completion and cannot run thinking/reasoning models (${mid}). ` +
      'Switch to a chat model such as kimi-k2.5.'
    );
  }
  if (reason === 'official_host_unseen') {
    return (
      'Official hop failed: Host did not see this hop. ' +
      'Complete must call OPENAI_BASE_URL (Host), not a BYO / TokenHub key.'
    );
  }
  return `Official hop failed (${reason}). Try kimi or another chat model.`;
}

async function completeOfficialViaHost(
  event: NormalizedEvent,
  opts: ChatWritebackOptions,
  deps: ChatWritebackDeps,
  jwt: string
): Promise<
  { ok: true; result: ChatCompleteResult } | { ok: false; reason: string }
> {
  const chat = event.chat;
  if (!chat) return { ok: false, reason: 'no_chat_envelope' };
  if (
    !canCompleteOfficialHop({
      inferencePath: chat.inference_path,
      hopId: chat.hop_id,
      hostInferenceUrl: chat.host_inference_url,
      jwt,
    })
  ) {
    return { ok: false, reason: 'official_complete_skipped' };
  }
  const model = chat.requested_model?.trim();
  const text = chat.user_text?.trim();
  if (!model) return { ok: false, reason: 'official_complete_missing_model' };
  if (!officialV0SupportsModel(model)) {
    return { ok: false, reason: 'official_complete_unsupported_model' };
  }
  if (!text) return { ok: false, reason: 'official_complete_missing_text' };

  const logFn = deps.logFn ?? ((line: string) => console.error(line));
  logFn(
    `[acn listen] official_complete chat_id=${chat.chat_id} model=${model}`
  );

  const fetchFn = deps.fetchFn ?? fetch;
  const timeoutMs = opts.completeTimeoutMs ?? DEFAULT_OFFICIAL_COMPLETE_TIMEOUT_MS;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetchFn(`${chat.host_inference_url}/chat/completions`, {
      method: 'POST',
      headers: {
        authorization: `Bearer ${jwt}`,
        'content-type': 'application/json',
        'X-Hop-Id': chat.hop_id!,
        'X-Agent-Id': opts.agentId,
      },
      body: JSON.stringify({
        model,
        messages: [{ role: 'user', content: text }],
        hop_id: chat.hop_id,
        ...(chat.max_output_tokens && chat.max_output_tokens > 0
          ? { max_tokens: chat.max_output_tokens }
          : {}),
      }),
      signal: controller.signal,
    });
    const raw = await res.text();
    if (res.status < 200 || res.status >= 300) {
      return { ok: false, reason: `official_complete_http_${res.status}` };
    }
    let payload: unknown;
    try {
      payload = JSON.parse(raw) as unknown;
    } catch {
      return { ok: false, reason: 'official_complete_invalid_json' };
    }
    const content = extractChatCompletionContent(payload);
    if (!content) return { ok: false, reason: 'official_complete_missing_content' };
    return { ok: true, result: { content } };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (controller.signal.aborted || /abort|timeout/i.test(msg)) {
      return { ok: false, reason: 'official_complete_timeout' };
    }
    return { ok: false, reason: msg.slice(0, 200) };
  } finally {
    clearTimeout(timer);
  }
}

async function loadOfficialHostMeter(
  event: NormalizedEvent,
  opts: ChatWritebackOptions,
  deps: ChatWritebackDeps,
  jwt: string
): Promise<{ ok: true; seen: boolean } | { ok: false; reason: string }> {
  const chat = event.chat;
  const base = asHostInferenceUrl(chat?.host_inference_url);
  const hopId = chat?.hop_id?.trim();
  if (!base || !hopId) return { ok: false, reason: 'official_complete_skipped' };
  const fetchFn = deps.fetchFn ?? fetch;
  try {
    const res = await fetchFn(`${base}/hops/${encodeURIComponent(hopId)}`, {
      headers: {
        authorization: `Bearer ${jwt}`,
        'X-Hop-Id': hopId,
        'X-Agent-Id': opts.agentId,
      },
    });
    if (res.status < 200 || res.status >= 300) {
      return { ok: false, reason: `official_meter_http_${res.status}` };
    }
    let payload: { seen?: unknown };
    try {
      payload = JSON.parse(await res.text()) as { seen?: unknown };
    } catch {
      return { ok: false, reason: 'official_meter_invalid_json' };
    }
    return { ok: true, seen: payload.seen === true };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return { ok: false, reason: `official_meter_unreachable:${msg.slice(0, 80)}` };
  }
}

async function completeOfficialViaAgent(
  event: NormalizedEvent,
  opts: ChatWritebackOptions,
  deps: ChatWritebackDeps,
  jwt: string
): Promise<
  { ok: true; result: ChatCompleteResult } | { ok: false; reason: string }
> {
  const chat = event.chat;
  if (!chat) return { ok: false, reason: 'no_chat_envelope' };
  if (
    !canCompleteOfficialHop({
      inferencePath: chat.inference_path,
      hopId: chat.hop_id,
      hostInferenceUrl: chat.host_inference_url,
      jwt,
    })
  ) {
    return { ok: false, reason: 'official_complete_skipped' };
  }
  const model = chat.requested_model?.trim();
  if (model && !officialV0SupportsModel(model)) {
    return { ok: false, reason: 'official_complete_unsupported_model' };
  }

  const logFn = deps.logFn ?? ((line: string) => console.error(line));
  logFn(
    `[acn listen] official_complete_via_agent chat_id=${chat.chat_id}` +
      (model ? ` model=${model}` : '')
  );

  const door = await startOfficialHopDoor({
    hostInferenceUrl: chat.host_inference_url!,
    hopId: chat.hop_id!,
    agentId: opts.agentId,
    jwt,
    fetchFn: deps.fetchFn,
  });
  if (!door) return { ok: false, reason: 'official_door_failed' };

  try {
    const completed = opts.completeUrl
      ? await completeViaHttp(event, opts, deps, jwt, door)
      : await spawnCompleteExec(event, opts, deps, jwt, door);
    if (!completed.ok) return completed;
    const meter = await loadOfficialHostMeter(event, opts, deps, jwt);
    if (!meter.ok) return meter;
    if (!meter.seen) return { ok: false, reason: 'official_host_unseen' };
    return { ok: true, result: { content: completed.result.content } };
  } finally {
    await door.close();
  }
}

async function completeViaExec(
  event: NormalizedEvent,
  opts: ChatWritebackOptions,
  deps: ChatWritebackDeps,
  jwt?: string | null
): Promise<
  { ok: true; result: ChatCompleteResult } | { ok: false; reason: string }
> {
  return spawnCompleteExec(event, opts, deps, jwt);
}

async function postWriteback(
  event: NormalizedEvent,
  complete: ChatCompleteResult,
  opts: ChatWritebackOptions,
  deps: ChatWritebackDeps
): Promise<ChatWritebackResult> {
  const chat = event.chat;
  if (!chat) return { ok: false, reason: 'no_chat_envelope' };

  if (!isAllowedChatReplyPath(chat.chat_id, chat.reply_path)) {
    return { ok: false, reason: 'reply_path_rejected' };
  }
  if (chat.reply_channel !== 'agentplanet.chat') {
    return { ok: false, reason: 'reply_channel_rejected' };
  }

  const fetchFn = deps.fetchFn ?? fetch;

  const path = chat.reply_path;
  let url: URL;
  let baseOrigin: string;
  try {
    baseOrigin = new URL(opts.apiBase).origin;
    url = new URL(`${opts.apiBase}${path}`);
  } catch {
    return { ok: false, reason: 'invalid_api_base' };
  }
  if (url.origin !== baseOrigin) {
    return { ok: false, reason: 'reply_url_origin_mismatch' };
  }

  const timeoutMs = opts.writebackTimeoutMs ?? DEFAULT_WRITEBACK_TIMEOUT_MS;
  const replyToId = chat.gateway_message_id ?? event.message_id;
  const body: Record<string, unknown> = {
    content: complete.content,
    reply_to_id: replyToId,
  };
  if (complete.usage) {
    const usageBody: Record<string, unknown> = {
      input_tokens: complete.usage.input_tokens,
      output_tokens: complete.usage.output_tokens,
      meter_source: complete.usage.meter_source ?? 'peer_self',
    };
    if (complete.usage.model_id) {
      usageBody.model_id = complete.usage.model_id;
    }
    if (complete.usage.reasoning_tokens != null) {
      usageBody.reasoning_tokens = complete.usage.reasoning_tokens;
    }
    if (complete.usage.cache_read_tokens != null) {
      usageBody.cache_read_tokens = complete.usage.cache_read_tokens;
    }
    if (complete.usage.cache_write_tokens != null) {
      usageBody.cache_write_tokens = complete.usage.cache_write_tokens;
    }
    if (complete.usage.total_tokens != null) {
      usageBody.total_tokens = complete.usage.total_tokens;
    }
    if (complete.usage.duration_ms != null) {
      usageBody.duration_ms = complete.usage.duration_ms;
    }
    if (complete.usage.provider) {
      usageBody.provider = complete.usage.provider;
    }
    body.usage = usageBody;
  } else if (complete.modelId) {
    // model_id only — do not invent zero token counts; Host defaults tokens to 0.
    body.usage = { model_id: complete.modelId };
  }

  const postOnce = async (
    token: string
  ): Promise<{ ok: true; status: number } | { ok: false; status: number; reason: string }> => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await fetchFn(url.toString(), {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (res.status === 200 || res.status === 201) {
        return { ok: true, status: res.status };
      }
      try {
        await res.arrayBuffer();
      } catch {
        /* ignore */
      }
      return {
        ok: false,
        status: res.status,
        reason: `writeback_http_${res.status}`,
      };
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (controller.signal.aborted || /abort|timeout/i.test(msg)) {
        return { ok: false, status: 0, reason: 'writeback_timeout' };
      }
      return { ok: false, status: 0, reason: msg.slice(0, 200) };
    } finally {
      clearTimeout(timer);
    }
  };

  const minted = await mintAgentJwt(opts, fetchFn);
  if (!minted.ok) {
    return { ok: false, reason: minted.reason };
  }

  let result = await postOnce(minted.token);
  if (result.ok) {
    return { ok: true, httpStatus: result.status };
  }

  // Cached / near-expired JWT rejected — drop cache and remint once.
  if (result.status === 401) {
    clearAgentJwtCache();
    const reminted = await mintAgentJwt(opts, fetchFn);
    if (!reminted.ok) {
      return { ok: false, reason: reminted.reason };
    }
    result = await postOnce(reminted.token);
    if (result.ok) {
      return { ok: true, httpStatus: result.status };
    }
  }

  return { ok: false, reason: result.reason };
}

/**
 * BYO host complete (url/exec). Invoke writeback uses this; chat official
 * hops keep their own door/Host path.
 */
export async function completeHostReply(
  event: NormalizedEvent,
  opts: ChatWritebackOptions,
  deps: ChatWritebackDeps = {}
): Promise<
  { ok: true; result: ChatCompleteResult } | { ok: false; reason: string }
> {
  if (opts.completeUrl) {
    return completeViaHttp(event, opts, deps);
  }
  if (opts.completeExec) {
    return completeViaExec(event, opts, deps);
  }
  return { ok: false, reason: 'byo_complete_missing' };
}

/**
 * Complete host reply + Gateway writeback. Never throws.
 * Only call when event.chat is present and opts.enabled.
 */
export async function handleChatWriteback(
  event: NormalizedEvent,
  opts: ChatWritebackOptions,
  deps: ChatWritebackDeps = {}
): Promise<ChatWritebackResult> {
  const logFn = deps.logFn ?? ((line: string) => console.error(line));
  if (!event.chat) return { ok: false, reason: 'no_chat_envelope' };

  let jwt: string | null = null;
  if (event.chat.inference_path === 'official') {
    const minted = await mintAgentJwt(opts, deps.fetchFn ?? fetch);
    if (!minted.ok) {
      logFn(
        `[acn listen] official_jwt_failed chat_id=${event.chat.chat_id} ` +
          `reason=${minted.reason}`
      );
      const written = await postWriteback(
        event,
        { content: officialCompleteFailureContent(minted.reason, event.chat.requested_model) },
        opts,
        deps
      );
      if (written.ok) {
        logFn(
          `[acn listen] official_fail_writeback_ok chat_id=${event.chat.chat_id} ` +
            `message_id=${event.message_id} reason=${minted.reason}`
        );
        return written;
      }
      return { ok: false, reason: minted.reason };
    }
    jwt = minted.token;
  }

  let completed:
    | { ok: true; result: ChatCompleteResult }
    | { ok: false; reason: string };
  if (event.chat.inference_path === 'official') {
    if (opts.completeUrl || opts.completeExec) {
      completed = await completeOfficialViaAgent(event, opts, deps, jwt!);
    } else {
      completed = await completeOfficialViaHost(event, opts, deps, jwt!);
    }
  } else if (opts.completeUrl) {
    completed = await completeViaHttp(event, opts, deps);
  } else if (opts.completeExec) {
    completed = await completeViaExec(event, opts, deps, jwt);
  } else {
    completed = { ok: false, reason: 'byo_complete_missing' };
  }

  if (!completed.ok) {
    logFn(
      `[acn listen] chat_complete_failed chat_id=${event.chat.chat_id} ` +
        `message_id=${event.message_id} reason=${completed.reason}`
    );
    if (event.chat.inference_path === 'official') {
      const written = await postWriteback(
        event,
        { content: officialCompleteFailureContent(completed.reason, event.chat.requested_model) },
        opts,
        deps
      );
      if (written.ok) {
        logFn(
          `[acn listen] official_fail_writeback_ok chat_id=${event.chat.chat_id} ` +
            `message_id=${event.message_id} reason=${completed.reason}`
        );
        return written;
      }
    }
    return { ok: false, reason: completed.reason };
  }

  const written = await postWriteback(event, completed.result, opts, deps);
  if (!written.ok) {
    logFn(
      `[acn listen] chat_writeback_failed chat_id=${event.chat.chat_id} ` +
        `message_id=${event.message_id} reason=${written.reason}`
    );
    return written;
  }

  const usageNote = completed.result.usage
    ? ` usage_in=${completed.result.usage.input_tokens}` +
      ` usage_out=${completed.result.usage.output_tokens}`
    : '';
  logFn(
    `[acn listen] chat_writeback_ok chat_id=${event.chat.chat_id} ` +
      `message_id=${event.message_id} http=${written.httpStatus}${usageNote}`
  );
  return written;
}

export {
  DEFAULT_COMPLETE_TIMEOUT_MS,
  DEFAULT_OFFICIAL_COMPLETE_TIMEOUT_MS,
  DEFAULT_WRITEBACK_TIMEOUT_MS,
};
