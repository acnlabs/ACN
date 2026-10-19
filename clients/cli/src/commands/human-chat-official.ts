/**
 * Human chat on listen: official first when the person picked it,
 * otherwise the runtime, then one official try if that provider gave up.
 */

import {
  tryOfficialHumanReply,
  type ChatWritebackDeps,
} from './chat-writeback.js';
import { fileOfficialSpent, providerGaveUp, type OfficialSpentStore } from './provider-failure.js';
import type { NormalizedEvent } from './normalize-event.js';
import { wakeRuntime, type RuntimeWakeOptions, type WakeDeps, type WakeResult } from './runtime-adapter.js';

export async function deliverHumanChat(
  event: NormalizedEvent,
  opts: RuntimeWakeOptions,
  deps: WakeDeps & ChatWritebackDeps & { spent?: OfficialSpentStore } = {}
): Promise<WakeResult> {
  const chat = event.chat;
  const reply = opts.officialReply;
  if (!chat || event.invoke || !reply) {
    return wakeRuntime(event, opts, deps);
  }
  const id = (chat.gateway_message_id || event.message_id || '').trim();
  const store = deps.spent ?? fileOfficialSpent(reply.agentId);
  if (id && store.has(id)) return { ok: false, reason: 'official_spent' };

  if (chat.inference_path === 'official') {
    const written = await tryOfficialHumanReply(event, reply, deps, store);
    return written.ok ? { ok: true } : { ok: false, reason: written.reason };
  }

  const wake = await wakeRuntime(event, opts, deps);
  if (wake.ok || !providerGaveUp(wake.reason)) return wake;
  const written = await tryOfficialHumanReply(event, reply, deps, store);
  return written.ok ? { ok: true } : { ok: false, reason: written.reason };
}
