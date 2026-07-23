/**
 * Built-in Mode B A2A receiver: answer JSON-RPC immediately, then wake runtime.
 * Does not bind a local HTTP port — replies go out over the relay WS.
 */

import { randomUUID } from 'crypto';
import {
  DedupeStore,
  dedupeKey,
  normalizeEvent,
  parseJsonRpcBody,
  type NormalizedEvent,
} from './normalize-event.js';
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

export interface LocalReceiverOptions extends RuntimeWakeOptions {
  dedupe: boolean;
  dedupeTtlSec: number;
}

export interface LocalReceiverDeps extends WakeDeps {
  generateId?: () => string;
  now?: () => Date;
  logFn?: (line: string) => void;
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

function formatWakeFailed(event: NormalizedEvent, reason: string): string {
  const task = event.task_id ?? '-';
  return (
    `[acn listen] wake_failed message_id=${event.message_id} ` +
    `task_id=${task} reason=${reason}`
  );
}

function formatDeduped(event: NormalizedEvent): string {
  return `[acn listen] deduped key=${dedupeKey(event)}`;
}

/**
 * Answer A2A first via ``send``, then fire-and-forget wake.
 * Returns as soon as the A2A frame is emitted (wake is not awaited).
 */
export function dispatchLocalReceiver(
  correlationId: string,
  bodyText: string,
  opts: LocalReceiverOptions,
  dedupeStore: DedupeStore,
  send: (frame: ReceiverResponseFrame) => void,
  deps: LocalReceiverDeps = {}
): void {
  const logFn = deps.logFn ?? ((line: string) => console.error(line));
  const result = processIncomingRequest(
    correlationId,
    bodyText,
    opts,
    dedupeStore,
    deps
  );

  send(result.response);

  if (result.dedupeHit && result.event) {
    logFn(formatDeduped(result.event));
    return;
  }

  if (!result.shouldWake || !result.event) return;

  const event = result.event;
  const key = opts.dedupe ? dedupeKey(event) : null;
  void wakeRuntime(event, opts, deps)
    .then((wake) => {
      if (!wake.ok) {
        // Release the slot so ACN at-least-once retries can wake again.
        if (key) dedupeStore.forget(key);
        logFn(formatWakeFailed(event, wake.reason));
      }
    })
    .catch((err: unknown) => {
      if (key) dedupeStore.forget(key);
      const msg = err instanceof Error ? err.message : String(err);
      logFn(formatWakeFailed(event, msg.slice(0, 200)));
    });
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
  send(result.response);

  if (result.dedupeHit && result.event) {
    logFn(formatDeduped(result.event));
    return { dedupeHit: true, woke: false };
  }
  if (!result.shouldWake || !result.event) {
    return { dedupeHit: false, woke: false };
  }

  const key = opts.dedupe ? dedupeKey(result.event) : null;
  try {
    const wake = await wakeRuntime(result.event, opts, deps);
    if (!wake.ok) {
      if (key) dedupeStore.forget(key);
      logFn(formatWakeFailed(result.event, wake.reason));
    }
    return { dedupeHit: false, woke: wake.ok };
  } catch (err) {
    if (key) dedupeStore.forget(key);
    const msg = err instanceof Error ? err.message : String(err);
    logFn(formatWakeFailed(result.event, msg.slice(0, 200)));
    return { dedupeHit: false, woke: false };
  }
}
