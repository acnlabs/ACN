/**
 * Built-in Mode B A2A receiver.
 * A host-door wake answers only after the door finishes: success is accepted,
 * failure asks ACN to keep the letter in the inbox. Chat writeback still
 * answers first. Does not bind a local HTTP port — replies go out over the relay WS.
 */

import { randomUUID } from 'crypto';
import {
  DedupeStore,
  dedupeKey,
  normalizeEvent,
  parseJsonRpcBody,
  type ContentPart,
  type NormalizedEvent,
} from './normalize-event.js';
import {
  handleChatWriteback,
  type ChatWritebackDeps,
  type ChatWritebackOptions,
} from './chat-writeback.js';
import { handleInvokeWriteback } from './invoke-writeback.js';
import { deliverHumanChat } from './human-chat-official.js';
import type { OfficialSpentStore } from './provider-failure.js';
import {
  wakeRuntime,
  type RuntimeWakeOptions,
  type WakeDeps,
} from './runtime-adapter.js';

const HANDLED_METHODS = new Set(['message/send', 'message/stream']);

/** Structurally matches listen.ts ``A2aResponseFrame`` (kept local to avoid cycles). */
export interface ReceiverResponseFrame {
  type: 'a2a_response';
  id: string;
  status: number;
  headers: Record<string, string>;
  body: string;
}

export interface ReplySender {
  agentId: string;
  apiKey: string;
  baseUrl: string;
}

export interface LocalReceiverOptions extends RuntimeWakeOptions {
  dedupe: boolean;
  dedupeTtlSec: number;
  /** When set, chat envelopes skip wakeRuntime and use Gateway writeback. */
  chatWriteback?: ChatWritebackOptions;
  /** Credentials for sending a later content message back to the caller. */
  sender?: ReplySender;
}

export interface LocalReceiverDeps extends WakeDeps, ChatWritebackDeps {
  generateId?: () => string;
  now?: () => Date;
  logFn?: (line: string) => void;
  spent?: OfficialSpentStore;
}

async function wakeHumanOrRuntime(
  event: NormalizedEvent,
  opts: LocalReceiverOptions,
  deps: LocalReceiverDeps
) {
  if (event.chat && !event.invoke) return deliverHumanChat(event, opts, deps);
  return wakeRuntime(event, opts, deps);
}

export interface ProcessIncomingResult {
  response: ReceiverResponseFrame;
  event: NormalizedEvent | null;
  shouldWake: boolean;
  dedupeHit: boolean;
}

function jsonRpcResponse(
  correlationId: string,
  jsonrpcId: unknown,
  payload: Record<string, unknown>
): ReceiverResponseFrame {
  return {
    type: 'a2a_response',
    id: correlationId,
    status: 200,
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: jsonrpcId ?? null,
      ...payload,
    }),
  };
}

function acceptedMessage(
  correlationId: string,
  jsonrpcId: unknown,
  messageId: string
): ReceiverResponseFrame {
  return jsonRpcResponse(correlationId, jsonrpcId, {
    result: {
      kind: 'message',
      messageId,
      role: 'agent',
      parts: [{ kind: 'text', text: 'accepted' }],
    },
  });
}

function jsonRpcError(
  correlationId: string,
  jsonrpcId: unknown,
  code: number,
  message: string
): ReceiverResponseFrame {
  return jsonRpcResponse(correlationId, jsonrpcId, {
    error: { code, message },
  });
}

/**
 * Pure: parse body → A2A response + whether to wake.
 * Dedupe store is mutated when a wake-eligible event is accepted.
 */
export function processIncomingRequest(
  correlationId: string,
  bodyText: string,
  opts: Pick<LocalReceiverOptions, 'dedupe'>,
  dedupeStore: DedupeStore,
  deps: Pick<LocalReceiverDeps, 'generateId' | 'now'> = {}
): ProcessIncomingResult {
  const generateId = deps.generateId ?? (() => randomUUID());
  const parsed = parseJsonRpcBody(bodyText);
  if (!parsed.ok) {
    return {
      response: jsonRpcError(correlationId, null, parsed.code, parsed.message),
      event: null,
      shouldWake: false,
      dedupeHit: false,
    };
  }

  const { body } = parsed;
  const jsonrpcId = body.id ?? null;
  const method = body.method as string;

  if (!HANDLED_METHODS.has(method)) {
    return {
      response: jsonRpcError(
        correlationId,
        jsonrpcId,
        -32601,
        `Method not found: ${method}`
      ),
      event: null,
      shouldWake: false,
      dedupeHit: false,
    };
  }

  const event = normalizeEvent(body, {
    generateId,
    now: deps.now,
  });
  const replyMessageId = generateId();
  const response = acceptedMessage(correlationId, jsonrpcId, replyMessageId);

  if (opts.dedupe) {
    const key = dedupeKey(event);
    if (dedupeStore.isDuplicate(key)) {
      return { response, event, shouldWake: false, dedupeHit: true };
    }
  }

  return { response, event, shouldWake: true, dedupeHit: false };
}

async function deliverContentReply(
  event: NormalizedEvent,
  parts: ContentPart[] | undefined,
  opts: LocalReceiverOptions,
  deps: LocalReceiverDeps
): Promise<void> {
  if (!parts || parts.length === 0) return;
  const logFn = deps.logFn ?? ((line: string) => console.error(line));
  if (!event.from_agent) {
    logFn(
      `[acn listen] content_reply_skipped message_id=${event.message_id} reason=no_from_agent`
    );
    return;
  }
  if (!opts.sender) {
    logFn(
      `[acn listen] content_reply_skipped message_id=${event.message_id} reason=no_sender`
    );
    return;
  }
  const fetchFn = deps.fetchFn ?? fetch;
  const url = `${opts.sender.baseUrl.replace(/\/+$/, '')}/api/v1/communication/send`;
  try {
    const res = await fetchFn(url, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        authorization: `Bearer ${opts.sender.apiKey}`,
      },
      body: JSON.stringify({
        from_agent: opts.sender.agentId,
        target_agent: event.from_agent,
        message: { role: 'agent', parts },
      }),
    });
    try {
      await res.text();
    } catch {
      /* ignore drain errors */
    }
    if (res.status < 200 || res.status >= 300) {
      logFn(
        `[acn listen] content_reply_failed to=${event.from_agent} http=${res.status}`
      );
      return;
    }
    logFn(`[acn listen] content_reply_sent to=${event.from_agent}`);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    logFn(
      `[acn listen] content_reply_failed to=${event.from_agent} reason=${msg.slice(0, 200)}`
    );
  }
}

function formatWakeFailed(event: NormalizedEvent, reason: string): string {
  const task = event.task_id ?? '-';
  return (
    `[acn listen] wake_failed message_id=${event.message_id} ` +
    `task_id=${task} reason=${reason}`
  );
}

function formatWakeWrote(event: NormalizedEvent): string {
  return `[acn listen] wake_wrote message_id=${event.message_id}`;
}

function formatDeduped(event: NormalizedEvent): string {
  return `[acn listen] deduped key=${dedupeKey(event)}`;
}

/** Ask ACN to park the original request in the inbox instead of treating it as delivered. */
export function parkInboxResponse(correlationId: string): ReceiverResponseFrame {
  return {
    type: 'a2a_response',
    id: correlationId,
    status: 503,
    headers: {
      'content-type': 'application/json',
      'x-acn-park-inbox': '1',
    },
    body: JSON.stringify({ error: 'host_unavailable' }),
  };
}

function usesHostDoor(event: NormalizedEvent, opts: LocalReceiverOptions): boolean {
  if (event.chat && opts.chatWriteback?.enabled) return false;
  if (event.invoke && opts.chatWriteback?.enabled) return false;
  return true;
}

/** Wake still in progress for a dedupe key. Same store the listener reuses. */
const inflightWakes = new WeakMap<DedupeStore, Map<string, Promise<boolean>>>();

function inflightMap(store: DedupeStore): Map<string, Promise<boolean>> {
  let map = inflightWakes.get(store);
  if (!map) {
    map = new Map();
    inflightWakes.set(store, map);
  }
  return map;
}

export function wakeInFlight(store: DedupeStore, key: string): boolean {
  return inflightMap(store).has(key);
}

function claimInflight(store: DedupeStore, key: string | null): (ok: boolean) => void {
  if (!key) return () => undefined;
  let finish: (ok: boolean) => void = () => undefined;
  const pending = new Promise<boolean>((resolve) => {
    finish = resolve;
  });
  inflightMap(store).set(key, pending);
  return (ok: boolean) => {
    inflightMap(store).delete(key);
    finish(ok);
  };
}

/**
 * Chat/invoke writeback still answers A2A first. A host-door wake
 * answers only after the wake: success → accepted, failure →
 * ``x-acn-park-inbox`` so ACN keeps the message in the inbox.
 */
export async function dispatchLocalReceiver(
  correlationId: string,
  bodyText: string,
  opts: LocalReceiverOptions,
  dedupeStore: DedupeStore,
  send: (frame: ReceiverResponseFrame) => void,
  deps: LocalReceiverDeps = {}
): Promise<void> {
  const logFn = deps.logFn ?? ((line: string) => console.error(line));
  const result = processIncomingRequest(
    correlationId,
    bodyText,
    opts,
    dedupeStore,
    deps
  );

  const event = result.event;
  if (result.dedupeHit && event && usesHostDoor(event, opts)) {
    const pending = inflightMap(dedupeStore).get(dedupeKey(event));
    if (pending) {
      const ok = await pending;
      send(ok ? result.response : parkInboxResponse(correlationId));
      logFn(formatDeduped(event));
      return;
    }
  }
  const hostDoor = Boolean(
    result.shouldWake && event && usesHostDoor(event, opts) && !result.dedupeHit
  );
  if (!hostDoor) send(result.response);

  if (result.dedupeHit && event) {
    logFn(formatDeduped(event));
    return;
  }

  if (!result.shouldWake || !event) return;

  const key = opts.dedupe ? dedupeKey(event) : null;

  // Chat Gateway envelopes: complete host reply + writeback (not task wake).
  if (event.chat && opts.chatWriteback?.enabled) {
    void handleChatWriteback(event, opts.chatWriteback, deps)
      .then((written) => {
        if (!written.ok && key) dedupeStore.forget(key);
      })
      .catch((err: unknown) => {
        if (key) dedupeStore.forget(key);
        const msg = err instanceof Error ? err.message : String(err);
        logFn(
          `[acn listen] chat_writeback_failed message_id=${event.message_id} ` +
            `reason=${msg.slice(0, 200)}`
        );
      });
    return;
  }

  if (event.invoke && opts.chatWriteback?.enabled) {
    void handleInvokeWriteback(event, opts.chatWriteback, deps)
      .then((written) => {
        if (!written.ok && key) dedupeStore.forget(key);
      })
      .catch((err: unknown) => {
        if (key) dedupeStore.forget(key);
        const msg = err instanceof Error ? err.message : String(err);
        logFn(
          `[acn listen] invoke_writeback_failed hop_id=${event.invoke?.hop_id} ` +
            `reason=${msg.slice(0, 200)}`
        );
      });
    return;
  }

  const finishInflight = claimInflight(dedupeStore, hostDoor ? key : null);
  try {
    const wake = await wakeHumanOrRuntime(event, opts, deps);
    if (!wake.ok) {
      if (key) dedupeStore.forget(key);
      finishInflight(false);
      logFn(formatWakeFailed(event, wake.reason));
      send(parkInboxResponse(correlationId));
      return;
    }
    finishInflight(true);
    if (event.chat) logFn(formatWakeWrote(event));
    send(result.response);
    await deliverContentReply(event, wake.replyParts, opts, deps);
  } catch (err: unknown) {
    if (key) dedupeStore.forget(key);
    finishInflight(false);
    const msg = err instanceof Error ? err.message : String(err);
    logFn(formatWakeFailed(event, msg.slice(0, 200)));
    send(parkInboxResponse(correlationId));
  }
}

/** Awaitable variant for tests that need to observe wake completion. */
export async function dispatchLocalReceiverAndWaitWake(
  correlationId: string,
  bodyText: string,
  opts: LocalReceiverOptions,
  dedupeStore: DedupeStore,
  send: (frame: ReceiverResponseFrame) => void,
  deps: LocalReceiverDeps = {}
): Promise<{ dedupeHit: boolean; woke: boolean }> {
  const logFn = deps.logFn ?? ((line: string) => console.error(line));
  const result = processIncomingRequest(
    correlationId,
    bodyText,
    opts,
    dedupeStore,
    deps
  );
  const event = result.event;
  if (result.dedupeHit && event && usesHostDoor(event, opts)) {
    const pending = inflightMap(dedupeStore).get(dedupeKey(event));
    if (pending) {
      const ok = await pending;
      send(ok ? result.response : parkInboxResponse(correlationId));
      logFn(formatDeduped(event));
      return { dedupeHit: true, woke: ok };
    }
  }
  const hostDoor = Boolean(
    result.shouldWake && event && usesHostDoor(event, opts) && !result.dedupeHit
  );
  if (!hostDoor) send(result.response);

  if (result.dedupeHit && event) {
    logFn(formatDeduped(event));
    return { dedupeHit: true, woke: false };
  }
  if (!result.shouldWake || !event) {
    return { dedupeHit: false, woke: false };
  }

  const key = opts.dedupe ? dedupeKey(event) : null;

  if (event.chat && opts.chatWriteback?.enabled) {
    try {
      const written = await handleChatWriteback(
        event,
        opts.chatWriteback,
        deps
      );
      if (!written.ok && key) dedupeStore.forget(key);
      return { dedupeHit: false, woke: written.ok };
    } catch (err) {
      if (key) dedupeStore.forget(key);
      const msg = err instanceof Error ? err.message : String(err);
      logFn(
        `[acn listen] chat_writeback_failed message_id=${event.message_id} ` +
          `reason=${msg.slice(0, 200)}`
      );
      return { dedupeHit: false, woke: false };
    }
  }

  if (event.invoke && opts.chatWriteback?.enabled) {
    try {
      const written = await handleInvokeWriteback(
        event,
        opts.chatWriteback,
        deps
      );
      if (!written.ok && key) dedupeStore.forget(key);
      return { dedupeHit: false, woke: written.ok };
    } catch (err) {
      if (key) dedupeStore.forget(key);
      const msg = err instanceof Error ? err.message : String(err);
      logFn(
        `[acn listen] invoke_writeback_failed hop_id=${event.invoke.hop_id} ` +
          `reason=${msg.slice(0, 200)}`
      );
      return { dedupeHit: false, woke: false };
    }
  }

  const finishInflight = claimInflight(dedupeStore, hostDoor ? key : null);
  try {
    const wake = await wakeHumanOrRuntime(event, opts, deps);
    if (!wake.ok) {
      if (key) dedupeStore.forget(key);
      finishInflight(false);
      logFn(formatWakeFailed(event, wake.reason));
      if (hostDoor) send(parkInboxResponse(correlationId));
      return { dedupeHit: false, woke: false };
    }
    finishInflight(true);
    if (event.chat) logFn(formatWakeWrote(event));
    if (hostDoor) send(result.response);
    await deliverContentReply(event, wake.replyParts, opts, deps);
    return { dedupeHit: false, woke: true };
  } catch (err) {
    if (key) dedupeStore.forget(key);
    finishInflight(false);
    const msg = err instanceof Error ? err.message : String(err);
    logFn(formatWakeFailed(event, msg.slice(0, 200)));
    if (hostDoor) send(parkInboxResponse(correlationId));
    return { dedupeHit: false, woke: false };
  }
}
