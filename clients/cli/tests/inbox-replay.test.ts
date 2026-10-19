import { describe, expect, it, vi } from 'vitest';

import { inboxMessageToEvent, replayInbox } from '../src/commands/inbox-replay.js';
import { DedupeStore } from '../src/commands/normalize-event.js';
import { MemoryOfficialSpent } from '../src/commands/provider-failure.js';

describe('inboxMessageToEvent', () => {
  it('wraps a stored A2A message', () => {
    const event = inboxMessageToEvent({
      role: 'user',
      parts: [{ kind: 'text', text: 'hi' }],
    });
    expect(event?.parts).toEqual([{ kind: 'text', text: 'hi' }]);
  });

  it('reads a stored JSON-RPC body', () => {
    const event = inboxMessageToEvent({
      jsonrpc: '2.0',
      id: '1',
      method: 'message/send',
      params: {
        message: {
          messageId: 'm1',
          role: 'user',
          parts: [{ kind: 'text', text: 'yo' }],
        },
      },
    });
    expect(event?.message_id).toBe('m1');
  });
});

describe('replayInbox', () => {
  it('acks a letter the host accepted and leaves the rest when the host is down', async () => {
    const calls: string[] = [];
    const fetchFn = vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? 'GET'} ${url}`);
      if (String(url).includes('/ack')) {
        return { ok: true, status: 200, json: async () => ({ acked: ['r1'] }) };
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({
          messages: [
            {
              route_id: 'r1',
              message: { role: 'user', parts: [{ kind: 'text', text: 'one' }] },
            },
            {
              route_id: 'r2',
              message: { role: 'user', parts: [{ kind: 'text', text: 'two' }] },
            },
          ],
        }),
      };
    });
    let wakes = 0;
    const result = await replayInbox({
      baseUrl: 'https://api.acnlabs.dev',
      agentId: 'agent-1',
      apiKey: 'acn_k',
      runtime: { runtime: 'http', wakeUrl: 'http://127.0.0.1/wake' },
      fetchFn: fetchFn as unknown as typeof fetch,
      logFn: () => undefined,
      wakeDeps: {
        fetchFn: vi.fn(async () => {
          wakes += 1;
          return {
            ok: wakes === 1,
            status: wakes === 1 ? 200 : 502,
            headers: { get: () => null },
            text: async () => '',
          } as unknown as Response;
        }) as unknown as typeof fetch,
      },
    });

    expect(result).toEqual({ delivered: 1, waiting: 1 });
    expect(calls.some((c) => c.startsWith('POST') && c.includes('/ack'))).toBe(true);
    const ackBody = JSON.parse(String(fetchFn.mock.calls[1]?.[1]?.body));
    expect(ackBody).toEqual({ route_ids: ['r1'] });
  });

  it('keeps going after one refused letter and stops when the door is down', async () => {
    const statuses = [200, 400, 200, 200];
    const wakeFetch = vi.fn(async () => {
      const status = statuses[wakeFetch.mock.calls.length - 1] ?? 500;
      return {
        ok: status >= 200 && status < 300,
        status,
        headers: { get: () => null },
        text: async () => '',
      } as unknown as Response;
    });
    const fetchFn = vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).includes('/ack')) {
        return { ok: true, status: 200, json: async () => ({}) };
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({
          messages: ['r1', 'r2', 'r3'].map((id) => ({
            route_id: id,
            message: { role: 'user', parts: [{ kind: 'text', text: id }] },
          })),
        }),
      };
    });
    const refused = await replayInbox({
      baseUrl: 'https://api.acnlabs.dev',
      agentId: 'agent-1',
      apiKey: 'acn_k',
      runtime: { runtime: 'http', wakeUrl: 'http://127.0.0.1/wake' },
      fetchFn: fetchFn as unknown as typeof fetch,
      logFn: () => undefined,
      wakeDeps: { fetchFn: wakeFetch as unknown as typeof fetch },
    });
    expect(refused).toEqual({ delivered: 2, waiting: 1 });
    expect(wakeFetch).toHaveBeenCalledTimes(3);
    expect(JSON.parse(String(fetchFn.mock.calls[1]?.[1]?.body))).toEqual({
      route_ids: ['r1', 'r3'],
    });

    let wakes = 0;
    const downFetch = vi.fn(async () => {
      wakes += 1;
      if (wakes === 1) {
        return { ok: true, status: 200, headers: { get: () => null }, text: async () => '' } as unknown as Response;
      }
      throw new Error('timeout');
    });
    const downList = vi.fn(async (url: string, _init?: RequestInit) => {
      if (String(url).includes('/ack')) {
        return { ok: true, status: 200, json: async () => ({}) };
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({
          messages: ['a', 'b', 'c'].map((id) => ({
            route_id: id,
            message: { role: 'user', parts: [{ kind: 'text', text: id }] },
          })),
        }),
      };
    });
    const down = await replayInbox({
      baseUrl: 'https://api.acnlabs.dev',
      agentId: 'agent-1',
      apiKey: 'acn_k',
      runtime: { runtime: 'http', wakeUrl: 'http://127.0.0.1/wake' },
      fetchFn: downList as unknown as typeof fetch,
      logFn: () => undefined,
      wakeDeps: { fetchFn: downFetch as unknown as typeof fetch },
    });
    expect(down).toEqual({ delivered: 1, waiting: 1 });
    expect(downFetch).toHaveBeenCalledTimes(2);
  });

  it('acks a letter already delivered live without knocking again', async () => {
    const store = new DedupeStore(3600);
    store.isDuplicate('t-live');
    const wakeFetch = vi.fn();
    const fetchFn = vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).includes('/ack')) {
        return { ok: true, status: 200, json: async () => ({}) };
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({
          messages: [
            {
              route_id: 'r-live',
              message: {
                role: 'user',
                parts: [{ kind: 'text', text: 'hi' }],
                metadata: { task_id: 't-live' },
              },
            },
          ],
        }),
      };
    });
    const result = await replayInbox({
      baseUrl: 'https://api.acnlabs.dev',
      agentId: 'agent-1',
      apiKey: 'acn_k',
      runtime: { runtime: 'http', wakeUrl: 'http://127.0.0.1/wake' },
      dedupeStore: store,
      fetchFn: fetchFn as unknown as typeof fetch,
      logFn: () => undefined,
      wakeDeps: { fetchFn: wakeFetch as unknown as typeof fetch },
    });
    expect(result).toEqual({ delivered: 1, waiting: 0 });
    expect(wakeFetch).not.toHaveBeenCalled();
    const ackBody = JSON.parse(String(fetchFn.mock.calls[1]?.[1]?.body));
    expect(ackBody).toEqual({ route_ids: ['r-live'] });
  });

  it('skips a human chat official v0 cannot complete and still delivers the next letter', async () => {
    const chatId = '11111111-1111-1111-1111-111111111111';
    const chat = (messageId: string, extra: Record<string, unknown>) => ({
      role: 'user',
      messageId,
      parts: [{ kind: 'text', text: '在吗' }],
      metadata: {
        agentplanet: {
          chat_id: chatId,
          message_id: messageId,
          reply_channel: 'agentplanet.chat',
          reply_path: `/api/chats/${chatId}/agent-messages`,
          inference_path: 'official',
          host_inference_url: 'https://api.agentplanet.org/api/inference/v1',
          ...extra,
        },
      },
    });
    const spent = new MemoryOfficialSpent();
    const wakeFetch = vi.fn(async () => {
      return {
        ok: true,
        status: 200,
        headers: { get: () => null },
        text: async () => '',
      } as unknown as Response;
    });
    const fetchFn = vi.fn(async (url: string, _init?: RequestInit) => {
      if (String(url).includes('/ack')) {
        return { ok: true, status: 200, json: async () => ({}) };
      }
      if (String(url).includes('/chat/completions') || String(url).includes('/oauth/token')) {
        throw new Error(`model called: ${url}`);
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({
          messages: [
            {
              route_id: 'r-think',
              message: chat('m-think', {
                hop_id: `hop:dialog:${chatId}:m-think:agent-1`,
                requested_model: 'deepseek/deepseek-r1',
              }),
            },
            {
              route_id: 'r-nohop',
              message: chat('m-nohop', { requested_model: 'moonshotai/kimi-k2.5' }),
            },
            {
              route_id: 'r-ok',
              message: { role: 'user', parts: [{ kind: 'text', text: 'plain' }] },
            },
          ],
        }),
      };
    });
    const opts = {
      baseUrl: 'https://api.acnlabs.dev',
      agentId: 'agent-1',
      apiKey: 'acn_k',
      runtime: {
        runtime: 'http' as const,
        wakeUrl: 'http://127.0.0.1/wake',
        officialReply: {
          enabled: true,
          apiBase: 'https://api.agentplanet.org',
          acnBaseUrl: 'https://api.acnlabs.dev',
          apiKey: 'acn_k',
          agentId: 'agent-1',
          audience: 'https://api.agentplanet.org',
        },
      },
      fetchFn: fetchFn as unknown as typeof fetch,
      logFn: () => undefined,
      wakeDeps: { fetchFn: wakeFetch as unknown as typeof fetch, spent },
    };
    const first = await replayInbox(opts);
    expect(first).toEqual({ delivered: 1, waiting: 2 });
    expect(wakeFetch).toHaveBeenCalledTimes(1);
    expect(JSON.parse(String(fetchFn.mock.calls[1]?.[1]?.body))).toEqual({
      route_ids: ['r-ok'],
    });

    const second = await replayInbox(opts);
    expect(second).toEqual({ delivered: 1, waiting: 2 });
    expect(wakeFetch).toHaveBeenCalledTimes(2);
    expect(spent.failed('m-think')).toBe(false);
    expect(spent.failed('m-nohop')).toBe(false);
    expect(spent.pending('m-think')).toBeNull();
    expect(spent.delivered('m-think')).toBe(false);
  });
});
