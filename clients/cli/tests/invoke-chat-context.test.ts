import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';

import {
  injectChatIdIntoInvokeMessage,
  mergeOrchestrationWithPending,
  recordPendingCallee,
} from '../src/commands/invoke-chat-context.js';

const chatId = 'chat-test-654927f2';

afterEach(() => {
  const root = process.env.ACN_PENDING_CALLEES_DIR || path.join(os.homedir(), '.acn', 'pending-callees');
  const file = path.join(root, `${chatId.replace(/[^a-zA-Z0-9._-]+/g, '_')}.json`);
  try {
    fs.unlinkSync(file);
  } catch {
    /* ok */
  }
});

describe('invoke-chat-context', () => {
  it('injects metadata.agentplanet.chat_id on invoke message', () => {
    const message: Record<string, unknown> = { text: 'hi' };
    injectChatIdIntoInvokeMessage(message, chatId);
    const ap = (message.metadata as { agentplanet: { chat_id: string } }).agentplanet;
    expect(ap.chat_id).toBe(chatId);
  });

  it('merges pending callees into writeback orchestration', () => {
    recordPendingCallee(chatId, {
      to: '61c3783b-a45e-42c3-908a-b80b1b9c6e43',
      hopId: 'hop:invoke:req-a:61c3783b-a45e-42c3-908a-b80b1b9c6e43',
      status: 'accepted',
    });
    const merged = mergeOrchestrationWithPending(chatId, undefined);
    expect(merged?.callees).toEqual([
      {
        agent_id: '61c3783b-a45e-42c3-908a-b80b1b9c6e43',
        hop_id: 'hop:invoke:req-a:61c3783b-a45e-42c3-908a-b80b1b9c6e43',
        status: 'accepted',
      },
    ]);
    expect(mergeOrchestrationWithPending(chatId, undefined)).toBeUndefined();
  });
});
