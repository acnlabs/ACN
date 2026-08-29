/**
 * Mode B invoke usage writeback (AgentRouter D40–D44).
 *
 * Same host complete JSON as chat. Posts usage to ACN /invoke/complete
 * with the listener's acn_* key. Does not call Chat Gateway.
 */

import type { NormalizedEvent } from './normalize-event.js';
import {
  completeHostReply,
  type ChatWritebackDeps,
  type ChatWritebackOptions,
  type ChatWritebackResult,
} from './chat-writeback.js';

export async function handleInvokeWriteback(
  event: NormalizedEvent,
  opts: ChatWritebackOptions,
  deps: ChatWritebackDeps = {}
): Promise<ChatWritebackResult> {
  const logFn = deps.logFn ?? ((line: string) => console.error(line));
  if (!event.invoke) return { ok: false, reason: 'no_invoke_envelope' };

  const completed = await completeHostReply(event, opts, deps);
  if (!completed.ok) {
    logFn(
      `[acn listen] invoke_complete_failed hop_id=${event.invoke.hop_id} ` +
        `reason=${completed.reason}`
    );
    return { ok: false, reason: completed.reason };
  }

  const fetchFn = deps.fetchFn ?? fetch;
  const url = `${opts.acnBaseUrl.replace(/\/+$/, '')}/api/v1/invoke/complete`;
  const usage = completed.result.usage
    ? { ...completed.result.usage, meter_source: 'peer_self' }
    : undefined;
  try {
    const res = await fetchFn(url, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        authorization: `Bearer ${opts.apiKey}`,
      },
      body: JSON.stringify({
        request_id: event.invoke.request_id,
        hop_id: event.invoke.hop_id,
        usage,
      }),
    });
    if (res.status < 200 || res.status >= 300) {
      const reason = `invoke_complete_http_${res.status}`;
      logFn(
        `[acn listen] invoke_writeback_failed hop_id=${event.invoke.hop_id} reason=${reason}`
      );
      return { ok: false, reason };
    }
    const usageNote = usage
      ? ` usage_in=${usage.input_tokens} usage_out=${usage.output_tokens}`
      : '';
    logFn(
      `[acn listen] invoke_writeback_ok hop_id=${event.invoke.hop_id} ` +
        `http=${res.status}${usageNote}`
    );
    return { ok: true, httpStatus: res.status };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    logFn(
      `[acn listen] invoke_writeback_failed hop_id=${event.invoke.hop_id} ` +
        `reason=${msg.slice(0, 200)}`
    );
    return { ok: false, reason: msg.slice(0, 200) };
  }
}
