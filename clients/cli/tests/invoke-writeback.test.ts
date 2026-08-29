import { describe, expect, it, vi } from 'vitest';

import { handleInvokeWriteback } from '../src/commands/invoke-writeback.js';
import { extractInvokeEnvelope } from '../src/commands/normalize-event.js';
import type { NormalizedEvent } from '../src/commands/normalize-event.js';

vi.mock('../src/commands/chat-writeback.js', () => ({
  completeHostReply: vi.fn(async () => ({
    ok: true,
    result: {
      content: 'done',
      usage: { input_tokens: 12, output_tokens: 3 },
    },
  })),
}));

function invokeEvent(): NormalizedEvent {
  return {
    event_type: 'a2a_message',
    task_id: null,
    message_id: 'm1',
    context_id: null,
    from_agent: 'system:agent-router',
    chat: null,
    invoke: {
      request_id: 'req-1',
      hop_id: 'hop:invoke:req-1:agent-b',
      slot: null,
    },
    received_at: new Date().toISOString(),
    raw: {},
  };
}

describe('invoke writeback', () => {
  it('extracts metadata.agentplanet.invoke', () => {
    const env = extractInvokeEnvelope({
      metadata: {
        agentplanet: {
          invoke: {
            request_id: 'req-1',
            hop_id: 'hop:invoke:req-1:agent-b',
            slot: 'text.reply',
          },
        },
      },
    });
    expect(env).toEqual({
      request_id: 'req-1',
      hop_id: 'hop:invoke:req-1:agent-b',
      slot: 'text.reply',
    });
  });

  it('POSTs /invoke/complete with acn_* and does not call chat', async () => {
    const fetchFn = vi.fn(async (url: string, _init?: RequestInit) => {
      expect(String(url)).toContain('/api/v1/invoke/complete');
      expect(String(url)).not.toContain('/api/chats/');
      return { status: 200, text: async () => '{}' } as Response;
    });
    const result = await handleInvokeWriteback(
      invokeEvent(),
      {
        enabled: true,
        apiBase: 'https://api.example',
        acnBaseUrl: 'https://acn.example',
        apiKey: 'acn_TEST',
        agentId: 'agent-b',
        audience: 'https://api.agentplanet.org',
        completeUrl: 'http://127.0.0.1:9/complete',
      },
      { fetchFn: fetchFn as never, logFn: () => undefined }
    );
    expect(result.ok).toBe(true);
    expect(fetchFn).toHaveBeenCalledTimes(1);
    const init = fetchFn.mock.calls[0]?.[1];
    if (!init) {
      throw new Error('expected fetch init');
    }
    const headers = init.headers as Record<string, string>;
    expect(headers.authorization).toBe('Bearer acn_TEST');
    const body = JSON.parse(String(init.body));
    expect(body.request_id).toBe('req-1');
    expect(body.usage.input_tokens).toBe(12);
    expect(body.usage.meter_source).toBe('peer_self');
  });

  it('times out /invoke/complete and does not hang', async () => {
    const fetchFn = vi.fn(
      (_url: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => {
            reject(new DOMException('The operation was aborted.', 'AbortError'));
          });
        })
    );
    const result = await handleInvokeWriteback(
      invokeEvent(),
      {
        enabled: true,
        apiBase: 'https://api.example',
        acnBaseUrl: 'https://acn.example',
        apiKey: 'acn_TEST',
        agentId: 'agent-b',
        audience: 'https://api.agentplanet.org',
        completeUrl: 'http://127.0.0.1:9/complete',
        writebackTimeoutMs: 20,
      },
      { fetchFn: fetchFn as never, logFn: () => undefined }
    );
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.reason).toBe('writeback_timeout');
  });
});
