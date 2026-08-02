/**
 * Unified Interfaze / Chat Gateway writeback for Mode B listen.
 *
 * When a relayed A2A message carries metadata.agentplanet (chat_id + reply_path),
 * the CLI:
 *   1) asks the host for reply text (--chat-complete-url | --chat-complete-exec)
 *   2) POSTs { content } to Chat Gateway agent-messages
 *
 * Hosts only need to return {"content":"..."} — they do not call Gateway themselves.
 */

import { spawn } from 'child_process';
import type { ChildProcess } from 'child_process';
import type { NormalizedEvent } from './normalize-event.js';
import { isAllowedChatReplyPath } from './normalize-event.js';

export interface ChatWritebackOptions {
  enabled: boolean;
  /** Chat Gateway origin, e.g. https://api.example.com */
  apiBase: string;
  /** X-Internal-Token for AgentPlanet INTERNAL_API_TOKEN */
  token: string;
  /** This listener's ACN agent_id (query param on writeback). */
  agentId: string;
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

function asRecord(v: unknown): Record<string, unknown> | null {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : null;
}

/** Validate chat-writeback flags when --chat-writeback is set. */
export function validateChatWritebackOptions(opts: {
  chatWriteback?: boolean;
  chatApiBase?: string;
  chatToken?: string;
  chatCompleteUrl?: string;
  chatCompleteExec?: string;
  agentId?: string;
}): string | null {
  if (!opts.chatWriteback) return null;
  if (!opts.agentId) {
    return '--chat-writeback requires a known agent id (join / --agent-id).';
  }
  if (!opts.chatApiBase?.trim()) {
    return '--chat-writeback requires --chat-api-base (or ACN_CHAT_API_BASE / AGENTPLANET_API_BASE).';
  }
  if (!opts.chatToken?.trim()) {
    return (
      '--chat-writeback requires --chat-token ' +
      '(or ACN_CHAT_WRITEBACK_TOKEN / AGENTPLANET_INTERNAL_TOKEN / AGENTPLANET_INTERNAL_API_TOKEN).'
    );
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
  chatToken?: string;
  chatCompleteUrl?: string;
  chatCompleteExec?: string;
  chatCompleteTimeoutMs?: number;
  chatWritebackTimeoutMs?: number;
  agentId: string;
}): ChatWritebackOptions | undefined {
  if (!opts.chatWriteback) return undefined;
  return {
    enabled: true,
    apiBase: opts.chatApiBase!.replace(/\/+$/, ''),
    token: opts.chatToken!,
    agentId: opts.agentId,
    completeUrl: opts.chatCompleteUrl?.trim() || undefined,
    completeExec: opts.chatCompleteExec?.trim() || undefined,
    completeTimeoutMs: opts.chatCompleteTimeoutMs,
    writebackTimeoutMs: opts.chatWritebackTimeoutMs,
  };
}

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

async function completeViaHttp(
  event: NormalizedEvent,
  opts: ChatWritebackOptions,
  deps: ChatWritebackDeps
): Promise<{ ok: true; content: string } | { ok: false; reason: string }> {
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
    const content = extractContent(parsed);
    if (!content) return { ok: false, reason: 'complete_missing_content' };
    return { ok: true, content };
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
): Promise<{ ok: true; content: string } | { ok: false; reason: string }> {
  const spawnFn = deps.spawnFn ?? spawn;
  const timeoutMs = opts.completeTimeoutMs ?? DEFAULT_COMPLETE_TIMEOUT_MS;
  const body = Buffer.from(JSON.stringify(event), 'utf-8');

  return new Promise((resolve) => {
    let settled = false;
    const child: ChildProcess = spawnFn(opts.completeExec!, { shell: true });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];

    const finish = (result: { ok: true; content: string } | { ok: false; reason: string }) => {
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
        const content = extractContent(parsed);
        if (!content) {
          finish({ ok: false, reason: 'complete_missing_content' });
          return;
        }
        finish({ ok: true, content });
      } catch {
        finish({ ok: false, reason: 'complete_invalid_json' });
      }
    });

    child.stdin?.end(body);
  });
}

async function postWriteback(
  event: NormalizedEvent,
  content: string,
  opts: ChatWritebackOptions,
  deps: ChatWritebackDeps
): Promise<ChatWritebackResult> {
  const chat = event.chat;
  if (!chat) return { ok: false, reason: 'no_chat_envelope' };

  // Defense in depth: never trust reply_path without allowlist (INTERNAL token).
  if (!isAllowedChatReplyPath(chat.chat_id, chat.reply_path)) {
    return { ok: false, reason: 'reply_path_rejected' };
  }
  if (chat.reply_channel !== 'agentplanet.chat') {
    return { ok: false, reason: 'reply_channel_rejected' };
  }

  const fetchFn = deps.fetchFn ?? fetch;
  const path = chat.reply_path; // already allowlisted absolute path
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
  url.searchParams.set('agent_id', opts.agentId);

  const timeoutMs = opts.writebackTimeoutMs ?? DEFAULT_WRITEBACK_TIMEOUT_MS;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetchFn(url.toString(), {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'X-Internal-Token': opts.token,
      },
      body: JSON.stringify({ content }),
      signal: controller.signal,
    });
    if (res.status === 200 || res.status === 201) {
      return { ok: true, httpStatus: res.status };
    }
    try {
      await res.arrayBuffer();
    } catch {
      /* ignore */
    }
    return { ok: false, reason: `writeback_http_${res.status}` };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (controller.signal.aborted || /abort|timeout/i.test(msg)) {
      return { ok: false, reason: 'writeback_timeout' };
    }
    return { ok: false, reason: msg.slice(0, 200) };
  } finally {
    clearTimeout(timer);
  }
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

  const written = await postWriteback(event, completed.content, opts, deps);
  if (!written.ok) {
    logFn(
      `[acn listen] chat_writeback_failed chat_id=${event.chat.chat_id} ` +
        `message_id=${event.message_id} reason=${written.reason}`
    );
    return written;
  }

  logFn(
    `[acn listen] chat_writeback_ok chat_id=${event.chat.chat_id} ` +
      `message_id=${event.message_id} http=${written.httpStatus}`
  );
  return written;
}

export { DEFAULT_COMPLETE_TIMEOUT_MS, DEFAULT_WRITEBACK_TIMEOUT_MS };
