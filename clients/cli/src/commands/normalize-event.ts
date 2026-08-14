/**
 * Normalize a relayed A2A JSON-RPC body into a stable wake event, and
 * provide an in-process TTL dedupe window (restart clears the window).
 */

/** Required reply_channel on Chat Gateway writeback envelopes. */
export const CHAT_REPLY_CHANNEL = 'agentplanet.chat';

/** Chat Gateway / Interfaze writeback envelope (metadata.agentplanet). */
export interface ChatEnvelope {
  chat_id: string;
  reply_path: string;
  reply_channel: typeof CHAT_REPLY_CHANNEL;
  /** Stable user-message id from Gateway (preferred for dedupe). */
  gateway_message_id: string | null;
  user_text: string | null;
  /**
   * User-selected model for this hop (Interfaze composer S1).
   * Runtime should honor when generating the reply.
   */
  requested_model: string | null;
  /** Soft Host output cap when present. */
  max_output_tokens: number | null;
}

export interface NormalizedEvent {
  event_type: 'a2a_message';
  task_id: string | null;
  message_id: string;
  context_id: string | null;
  from_agent: string | null;
  /** Present when Chat Gateway attached a writeback contract. */
  chat: ChatEnvelope | null;
  received_at: string;
  raw: Record<string, unknown>;
}

export type JsonRpcParseResult =
  | { ok: true; body: Record<string, unknown> }
  | { ok: false; code: -32700 | -32600; message: string };

function asRecord(v: unknown): Record<string, unknown> | null {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : null;
}

function asNonEmptyString(v: unknown): string | null {
  return typeof v === 'string' && v.length > 0 ? v : null;
}

/** Parse UTF-8 request body into a JSON-RPC object (or a typed parse error). */
export function parseJsonRpcBody(bodyText: string): JsonRpcParseResult {
  let parsed: unknown;
  try {
    parsed = JSON.parse(bodyText);
  } catch {
    return { ok: false, code: -32700, message: 'Parse error' };
  }
  const body = asRecord(parsed);
  if (!body) {
    return { ok: false, code: -32600, message: 'Invalid Request' };
  }
  if (body.jsonrpc !== '2.0' || typeof body.method !== 'string') {
    return { ok: false, code: -32600, message: 'Invalid Request' };
  }
  return { ok: true, body };
}

function extractTaskId(message: Record<string, unknown>): string | null {
  const metadata = asRecord(message.metadata);
  if (metadata) {
    const fromMeta =
      asNonEmptyString(metadata.task_id) ?? asNonEmptyString(metadata.acn_task_id);
    if (fromMeta) return fromMeta;
  }
  const parts = message.parts;
  if (!Array.isArray(parts)) return null;
  for (const part of parts) {
    const p = asRecord(part);
    if (!p || p.kind !== 'data') continue;
    const data = asRecord(p.data);
    if (!data) continue;
    const fromData =
      asNonEmptyString(data.task_id) ?? asNonEmptyString(data.acn_task_id);
    if (fromData) return fromData;
  }
  return null;
}

function extractMessageId(
  message: Record<string, unknown>,
  generateId: () => string
): string {
  return (
    asNonEmptyString(message.messageId) ??
    asNonEmptyString(message.message_id) ??
    generateId()
  );
}

function extractContextId(message: Record<string, unknown>): string | null {
  return (
    asNonEmptyString(message.contextId) ?? asNonEmptyString(message.context_id)
  );
}

function extractFromAgent(message: Record<string, unknown>): string | null {
  const metadata = asRecord(message.metadata);
  if (!metadata) return null;
  return (
    asNonEmptyString(metadata.from_agent) ??
    asNonEmptyString(metadata.fromAgent)
  );
}

function extractUserText(message: Record<string, unknown>): string | null {
  const parts = message.parts;
  if (!Array.isArray(parts)) return null;
  const chunks: string[] = [];
  for (const part of parts) {
    const p = asRecord(part);
    if (!p || p.kind !== 'text') continue;
    const t = asNonEmptyString(p.text);
    if (t) chunks.push(t);
  }
  return chunks.length > 0 ? chunks.join('\n') : null;
}

/**
 * Allowlist Chat Gateway writeback path.
 * Exact match only — blocks `..`, `//`, query, fragment, and other hosts/paths
 * that could exfiltrate X-Internal-Token on the same apiBase origin.
 */
export function isAllowedChatReplyPath(
  chatId: string,
  replyPath: string
): boolean {
  if (!chatId || !replyPath) return false;
  if (
    replyPath.includes('..') ||
    replyPath.includes('?') ||
    replyPath.includes('#') ||
    replyPath.includes('//') ||
    replyPath.includes('\\') ||
    !replyPath.startsWith('/')
  ) {
    return false;
  }
  return replyPath === `/api/chats/${chatId}/agent-messages`;
}

/**
 * Extract Chat Gateway writeback envelope from message.metadata.agentplanet.
 * Requires chat_id, reply_channel=agentplanet.chat, and allowlisted reply_path.
 */
export function extractChatEnvelope(
  message: Record<string, unknown>
): ChatEnvelope | null {
  const metadata = asRecord(message.metadata);
  if (!metadata) return null;
  const ap = asRecord(metadata.agentplanet);
  if (!ap) return null;
  const chatId = asNonEmptyString(ap.chat_id);
  const replyPath = asNonEmptyString(ap.reply_path);
  const replyChannel = asNonEmptyString(ap.reply_channel);
  if (!chatId || !replyPath) return null;
  if (replyChannel !== CHAT_REPLY_CHANNEL) return null;
  if (!isAllowedChatReplyPath(chatId, replyPath)) return null;
  const requested = asNonEmptyString(ap.requested_model);
  let maxOut: number | null = null;
  const rawMax = ap.max_output_tokens;
  if (typeof rawMax === 'number' && Number.isFinite(rawMax) && rawMax > 0) {
    maxOut = Math.floor(rawMax);
  } else if (typeof rawMax === 'string' && rawMax.trim()) {
    const n = Number.parseInt(rawMax.trim(), 10);
    if (Number.isFinite(n) && n > 0) maxOut = n;
  }
  return {
    chat_id: chatId,
    reply_path: replyPath,
    reply_channel: CHAT_REPLY_CHANNEL,
    gateway_message_id:
      asNonEmptyString(ap.message_id) ?? asNonEmptyString(ap.messageId),
    user_text: extractUserText(message),
    requested_model: requested ? requested.slice(0, 200) : null,
    max_output_tokens: maxOut,
  };
}

/**
 * Build a normalized wake event from a valid JSON-RPC body.
 * Call only after parseJsonRpcBody succeeds for message/send|stream.
 */
export function normalizeEvent(
  body: Record<string, unknown>,
  opts: { generateId?: () => string; now?: () => Date } = {}
): NormalizedEvent {
  const generateId = opts.generateId ?? (() => crypto.randomUUID());
  const now = opts.now ?? (() => new Date());
  const params = asRecord(body.params);
  const message = asRecord(params?.message) ?? {};

  return {
    event_type: 'a2a_message',
    task_id: extractTaskId(message),
    message_id: extractMessageId(message, generateId),
    context_id: extractContextId(message),
    from_agent: extractFromAgent(message),
    chat: extractChatEnvelope(message),
    received_at: now().toISOString(),
    raw: body,
  };
}

export function dedupeKey(event: NormalizedEvent): string {
  if (event.chat) {
    const mid = event.chat.gateway_message_id ?? event.message_id;
    return `chat:${event.chat.chat_id}:${mid}`;
  }
  return event.task_id ?? event.message_id;
}

/** In-process TTL dedupe. Restart clears the window. */
export class DedupeStore {
  private readonly map = new Map<string, number>();

  constructor(private readonly ttlSec: number) {}

  /** Returns true if key was already seen within TTL; otherwise marks and returns false. */
  isDuplicate(key: string, nowMs: number = Date.now()): boolean {
    this.gc(nowMs);
    const exp = this.map.get(key);
    if (exp !== undefined && exp > nowMs) return true;
    this.map.set(key, nowMs + this.ttlSec * 1000);
    return false;
  }

  /**
   * Drop a key so a later retry can wake again.
   * Used when wake fails after we reserved the slot on accept.
   */
  forget(key: string): void {
    this.map.delete(key);
  }

  /** Test helper — current window size after GC. */
  size(nowMs: number = Date.now()): number {
    this.gc(nowMs);
    return this.map.size;
  }

  private gc(nowMs: number): void {
    for (const [k, exp] of this.map) {
      if (exp <= nowMs) this.map.delete(k);
    }
  }
}
