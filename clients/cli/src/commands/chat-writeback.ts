/**
 * Unified Interfaze / Chat Gateway writeback for Mode B listen.
 *
 * When a relayed A2A message carries metadata.agentplanet (chat_id + reply_path),
 * the CLI:
 *   1) asks the host for reply text (--chat-complete-url | --chat-complete-exec)
 *   2) mints a short-lived ACN agent JWT via POST /oauth/token (acn_* API key)
 *   3) POSTs { content, reply_to_id?, usage? } to Chat Gateway agent-messages
 *      with Bearer JWT
 *
 * Hosts return {"content":"..."} and optionally
 * {"usage":{"input_tokens":N,"output_tokens":M}} for chat billing settle.
 * They do not call Gateway themselves.
 */

import { spawn } from 'child_process';
import type { ChildProcess } from 'child_process';
import type { NormalizedEvent } from './normalize-event.js';
import { isAllowedChatReplyPath } from './normalize-event.js';

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
  if (hasUrl === hasExec) {
    return (
      '--chat-writeback requires exactly one of --chat-complete-url or --chat-complete-exec ' +
      '(host returns JSON {"content":"..."}).'
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
};

export type ChatCompleteResult = {
  content: string;
  usage?: ChatTokenUsage;
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

/**
 * Optional token usage from host complete JSON (chat billing settle).
 * Accepts usage.input_tokens/output_tokens or prompt_tokens/completion_tokens.
 */
export function extractUsage(payload: unknown): ChatTokenUsage | undefined {
  const rec = asRecord(payload);
  if (!rec) return undefined;
  const usageRec = asRecord(rec.usage) ?? rec;
  const input =
    asNonNegInt(usageRec.input_tokens) ?? asNonNegInt(usageRec.prompt_tokens);
  const output =
    asNonNegInt(usageRec.output_tokens) ??
    asNonNegInt(usageRec.completion_tokens);
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
  return out;
}

function parseCompletePayload(
  payload: unknown
): { ok: true; result: ChatCompleteResult } | { ok: false; reason: string } {
  const content = extractContent(payload);
  if (!content) return { ok: false, reason: 'complete_missing_content' };
  const usage = extractUsage(payload);
  return { ok: true, result: usage ? { content, usage } : { content } };
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
      return { ok: false, reason: `oauth_http_${res.status}` };
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      return { ok: false, reason: 'oauth_invalid_json' };
    }
    const rec = asRecord(parsed);
    const token =
      typeof rec?.access_token === 'string' ? rec.access_token.trim() : '';
    if (!token) return { ok: false, reason: 'oauth_missing_access_token' };
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
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return { ok: false, reason: msg.slice(0, 200) };
  }
}

/** Test helper: clear process-local JWT cache. */
export function clearAgentJwtCache(): void {
  cachedJwt = null;
}

async function completeViaHttp(
  event: NormalizedEvent,
  opts: ChatWritebackOptions,
  deps: ChatWritebackDeps
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
      headers: { 'content-type': 'application/json' },
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

function completeViaExec(
  event: NormalizedEvent,
  opts: ChatWritebackOptions,
  deps: ChatWritebackDeps
): Promise<
  { ok: true; result: ChatCompleteResult } | { ok: false; reason: string }
> {
  const spawnFn = deps.spawnFn ?? spawn;
  const timeoutMs = opts.completeTimeoutMs ?? DEFAULT_COMPLETE_TIMEOUT_MS;
  const body = Buffer.from(JSON.stringify(event), 'utf-8');

  return new Promise((resolve) => {
    let settled = false;
    const child: ChildProcess = spawnFn(opts.completeExec!, { shell: true });
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
    body.usage = {
      input_tokens: complete.usage.input_tokens,
      output_tokens: complete.usage.output_tokens,
      meter_source: complete.usage.meter_source ?? 'peer_self',
    };
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

  const completed = opts.completeUrl
    ? await completeViaHttp(event, opts, deps)
    : await completeViaExec(event, opts, deps);

  if (!completed.ok) {
    logFn(
      `[acn listen] chat_complete_failed chat_id=${event.chat.chat_id} ` +
        `message_id=${event.message_id} reason=${completed.reason}`
    );
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

export { DEFAULT_COMPLETE_TIMEOUT_MS, DEFAULT_WRITEBACK_TIMEOUT_MS };
