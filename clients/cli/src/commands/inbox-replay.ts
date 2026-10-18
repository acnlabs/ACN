/**
 * Deliver letters that were parked in the inbox while the host door was down.
 * A successful wake is acknowledged; a failed wake leaves the letter in place.
 */

import { wakeInFlight } from './local-receiver.js';
import {
  dedupeKey,
  normalizeEvent,
  parseJsonRpcBody,
  type DedupeStore,
  type NormalizedEvent,
} from './normalize-event.js';
import { wakeRuntime, type RuntimeWakeOptions, type WakeDeps } from './runtime-adapter.js';

interface InboxMessage {
  route_id?: string;
  message?: unknown;
}

interface InboxList {
  messages?: InboxMessage[];
}

export function inboxMessageToEvent(message: unknown): NormalizedEvent | null {
  if (!message || typeof message !== 'object') return null;
  const record = message as Record<string, unknown>;
  const rpc = looksLikeJsonRpc(record)
    ? record
    : {
        jsonrpc: '2.0',
        id: null,
        method: 'message/send',
        params: { message: record },
      };
  const parsed = parseJsonRpcBody(JSON.stringify(rpc));
  if (!parsed.ok) return null;
  return normalizeEvent(parsed.body);
}

function looksLikeJsonRpc(record: Record<string, unknown>): boolean {
  return record.method === 'message/send' || record.method === 'message/stream';
}

/** The door answered and refused this letter. A timeout or a dead door stops the batch. */
function hostRefusedOneLetter(reason: string): boolean {
  return /^http_\d+$/.test(reason);
}

export async function replayInbox(opts: {
  baseUrl: string;
  agentId: string;
  apiKey: string;
  runtime: RuntimeWakeOptions;
  fetchFn?: typeof fetch;
  logFn?: (line: string) => void;
  wakeDeps?: WakeDeps;
  dedupeStore?: DedupeStore;
}): Promise<{ delivered: number; waiting: number }> {
  const fetchFn = opts.fetchFn ?? fetch;
  const logFn = opts.logFn ?? ((line: string) => console.error(line));
  const origin = opts.baseUrl.replace(/\/+$/, '');
  const headers = {
    Authorization: `Bearer ${opts.apiKey}`,
    'Content-Type': 'application/json',
  };
  let list: InboxList;
  try {
    const res = await fetchFn(
      `${origin}/api/v1/communication/history/${opts.agentId}?limit=100`,
      { headers }
    );
    if (!res.ok) {
      logFn(`[acn listen] inbox replay skipped: http_${res.status}`);
      return { delivered: 0, waiting: 0 };
    }
    list = (await res.json()) as InboxList;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    logFn(`[acn listen] inbox replay skipped: ${msg}`);
    return { delivered: 0, waiting: 0 };
  }

  const acked: string[] = [];
  let waiting = 0;
  for (const item of list.messages ?? []) {
    const routeId = item.route_id?.trim();
    const event = inboxMessageToEvent(item.message);
    if (!routeId || !event) {
      waiting += 1;
      continue;
    }
    const key = dedupeKey(event);
    if (opts.dedupeStore && wakeInFlight(opts.dedupeStore, key)) {
      waiting += 1;
      continue;
    }
    if (opts.dedupeStore?.has(key)) {
      acked.push(routeId);
      continue;
    }
    const wake = await wakeRuntime(event, opts.runtime, opts.wakeDeps ?? {});
    if (!wake.ok) {
      logFn(`[acn listen] inbox replay waiting route_id=${routeId} reason=${wake.reason}`);
      waiting += 1;
      if (!hostRefusedOneLetter(wake.reason)) break;
      continue;
    }
    opts.dedupeStore?.isDuplicate(key);
    acked.push(routeId);
  }

  if (acked.length > 0) {
    try {
      const res = await fetchFn(
        `${origin}/api/v1/communication/history/${opts.agentId}/ack`,
        { method: 'POST', headers, body: JSON.stringify({ route_ids: acked }) }
      );
      if (!res.ok) {
        logFn(`[acn listen] inbox ack failed: http_${res.status}`);
      } else {
        logFn(`[acn listen] inbox replay delivered ${acked.length}`);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      logFn(`[acn listen] inbox ack failed: ${msg}`);
    }
  }
  return { delivered: acked.length, waiting };
}
