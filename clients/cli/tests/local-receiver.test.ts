/**
 * Tests for built-in Mode B local receiver + runtime wake adapters.
 */

import { EventEmitter } from 'events';
import { describe, expect, it, vi } from 'vitest';

import {
  dispatchA2aRequest,
  handleA2aRequest,
  validateListenHandlerFlags,
  type A2aRequestFrame,
  type OutboundFrame,
} from '../src/commands/listen.js';
import {
  dispatchLocalReceiverAndWaitWake,
  processIncomingRequest,
} from '../src/commands/local-receiver.js';
import {
  DedupeStore,
  normalizeEvent,
  parseJsonRpcBody,
} from '../src/commands/normalize-event.js';
import {
  parseWakeHeaders,
  validateRuntimeOptions,
  wakeRuntime,
} from '../src/commands/runtime-adapter.js';

function messageSendBody(overrides: Record<string, unknown> = {}): string {
  const message = {
    role: 'user',
    messageId: 'msg-1',
    kind: 'message',
    parts: [{ kind: 'text', text: 'hi' }],
    ...overrides,
  };
  return JSON.stringify({
    jsonrpc: '2.0',
    id: 'rpc-1',
    method: 'message/send',
    params: { message },
  });
}

function makeRequest(body: string, id = 'corr-1'): A2aRequestFrame {
  return {
    type: 'a2a_request',
    id,
    method: 'POST',
    path: '/',
    headers: { 'content-type': 'application/json' },
    body,
    body_encoding: 'utf-8',
  };
}

describe('parseJsonRpcBody / normalizeEvent', () => {
  it('extracts task_id from metadata then data parts', () => {
    const withMeta = parseJsonRpcBody(
      messageSendBody({ metadata: { task_id: 'task-meta' } })
    );
    expect(withMeta.ok).toBe(true);
    if (!withMeta.ok) return;
    expect(normalizeEvent(withMeta.body).task_id).toBe('task-meta');

    const withData = parseJsonRpcBody(
      messageSendBody({
        parts: [{ kind: 'data', data: { acn_task_id: 'task-data' } }],
      })
    );
    expect(withData.ok).toBe(true);
    if (!withData.ok) return;
    expect(normalizeEvent(withData.body).task_id).toBe('task-data');
  });

  it('generates message_id when absent', () => {
    const parsed = parseJsonRpcBody(
      JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'message/send',
        params: { message: { role: 'user', parts: [] } },
      })
    );
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    const event = normalizeEvent(parsed.body, { generateId: () => 'gen-1' });
    expect(event.message_id).toBe('gen-1');
    expect(event.event_type).toBe('a2a_message');
  });
});

describe('processIncomingRequest', () => {
  it('returns accepted message with kind for message/send', () => {
    const store = new DedupeStore(3600);
    const result = processIncomingRequest(
      'corr-1',
      messageSendBody(),
      { dedupe: true },
      store,
      { generateId: () => 'reply-1' }
    );
    expect(result.shouldWake).toBe(true);
    const body = JSON.parse(result.response.body);
    expect(body.result.kind).toBe('message');
    expect(body.result.parts[0].text).toBe('accepted');
    expect(body.id).toBe('rpc-1');
  });

  it('handles message/stream the same way (single-shot accepted)', () => {
    const store = new DedupeStore(3600);
    const body = messageSendBody();
    const streamBody = body.replace('message/send', 'message/stream');
    const result = processIncomingRequest('c', streamBody, { dedupe: false }, store);
    expect(result.shouldWake).toBe(true);
    expect(JSON.parse(result.response.body).result.kind).toBe('message');
  });

  it('returns -32601 for unknown methods without waking', () => {
    const store = new DedupeStore(3600);
    const result = processIncomingRequest(
      'c',
      JSON.stringify({ jsonrpc: '2.0', id: 9, method: 'tasks/get', params: {} }),
      { dedupe: true },
      store
    );
    expect(result.shouldWake).toBe(false);
    expect(JSON.parse(result.response.body).error.code).toBe(-32601);
  });

  it('dedupes by task_id and skips second wake', () => {
    const store = new DedupeStore(3600);
    const body = messageSendBody({ metadata: { task_id: 't1' } });
    const first = processIncomingRequest('c1', body, { dedupe: true }, store);
    const second = processIncomingRequest('c2', body, { dedupe: true }, store);
    expect(first.shouldWake).toBe(true);
    expect(second.shouldWake).toBe(false);
    expect(second.dedupeHit).toBe(true);
    expect(JSON.parse(second.response.body).result.kind).toBe('message');
  });
});

describe('validateListenHandlerFlags', () => {
  it('requires exactly one handler mode', () => {
    expect(validateListenHandlerFlags({})).toMatch(/Provide a handler/);
    expect(
      validateListenHandlerFlags({ runtime: 'http', forward: 'http://x' })
    ).toMatch(/only one handler/);
  });

  it('requires wake-url / wake-exec for runtime modes', () => {
    expect(validateRuntimeOptions({ runtime: 'http' })).toMatch(/wake-url/);
    expect(validateRuntimeOptions({ runtime: 'command' })).toMatch(/wake-exec/);
    expect(validateRuntimeOptions({ runtime: 'log' })).toBeNull();
  });

  it('parses wake headers', () => {
    expect(parseWakeHeaders(['Authorization: Bearer tok'])).toEqual({
      Authorization: 'Bearer tok',
    });
    expect(() => parseWakeHeaders(['bad'])).toThrow(/Invalid/);
  });
});

describe('wakeRuntime + dispatch order', () => {
  it('answers A2A before a slow http wake completes', async () => {
    let resolveFetch!: (v: Response) => void;
    const fetchFn = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        })
    );
    const frames: OutboundFrame[] = [];
    const order: string[] = [];

    const dispatchPromise = dispatchA2aRequest(
      makeRequest(messageSendBody()),
      {
        runtime: {
          runtime: 'http',
          wakeUrl: 'http://127.0.0.1:9/wake',
          dedupe: false,
          dedupeTtlSec: 3600,
          wakeTimeoutMs: 5000,
        },
      },
      (f) => {
        frames.push(f);
        order.push('a2a');
      },
      {
        fetchFn: fetchFn as unknown as typeof fetch,
        logFn: () => undefined,
      }
    );

    // dispatch returns immediately after send (wake is fire-and-forget)
    await dispatchPromise;
    expect(order).toEqual(['a2a']);
    expect(frames).toHaveLength(1);
    expect(JSON.parse((frames[0] as { body: string }).body).result.kind).toBe(
      'message'
    );

    // wake still in flight
    expect(fetchFn).toHaveBeenCalled();
    resolveFetch({
      status: 200,
      headers: { get: () => null },
      text: async () => '',
      arrayBuffer: async () => new ArrayBuffer(0),
    } as unknown as Response);
  });

  function mockFetch(status: number) {
    return vi.fn().mockResolvedValue({
      status,
      headers: { get: () => null },
      text: async () => '',
      arrayBuffer: async () => new ArrayBuffer(0),
    });
  }

  it('logs wake_failed when http wake returns non-2xx but still accepted A2A', async () => {
    const logs: string[] = [];
    const store = new DedupeStore(3600);
    const frames: OutboundFrame[] = [];
    const fetchFn = mockFetch(502);

    await dispatchLocalReceiverAndWaitWake(
      'corr-1',
      messageSendBody({ messageId: 'm-fail' }),
      {
        runtime: 'http',
        wakeUrl: 'http://127.0.0.1:9/wake',
        dedupe: true,
        dedupeTtlSec: 3600,
        wakeTimeoutMs: 1000,
      },
      store,
      (f) => frames.push(f),
      { fetchFn: fetchFn as unknown as typeof fetch, logFn: (l) => logs.push(l) }
    );

    expect(JSON.parse((frames[0] as { body: string }).body).result.kind).toBe(
      'message'
    );
    expect(logs.some((l) => l.includes('wake_failed') && l.includes('http_502'))).toBe(
      true
    );
  });

  it('releases dedupe on wake failure so a retry can wake again', async () => {
    const logs: string[] = [];
    const store = new DedupeStore(3600);
    const opts = {
      runtime: 'http' as const,
      wakeUrl: 'http://127.0.0.1:9/wake',
      dedupe: true,
      dedupeTtlSec: 3600,
      wakeTimeoutMs: 1000,
    };
    const body = messageSendBody({ metadata: { task_id: 'retry-1' } });

    const failFetch = mockFetch(502);
    await dispatchLocalReceiverAndWaitWake(
      'c1',
      body,
      opts,
      store,
      () => undefined,
      { fetchFn: failFetch as unknown as typeof fetch, logFn: (l) => logs.push(l) }
    );
    expect(failFetch).toHaveBeenCalledTimes(1);
    expect(store.size()).toBe(0);

    const okFetch = mockFetch(204);
    const second = await dispatchLocalReceiverAndWaitWake(
      'c2',
      body,
      opts,
      store,
      () => undefined,
      { fetchFn: okFetch as unknown as typeof fetch, logFn: (l) => logs.push(l) }
    );
    expect(second.dedupeHit).toBe(false);
    expect(second.woke).toBe(true);
    expect(okFetch).toHaveBeenCalledTimes(1);
    expect(logs.some((l) => l.includes('deduped'))).toBe(false);
  });

  it('logs deduped on second delivery after successful wake', async () => {
    const logs: string[] = [];
    const store = new DedupeStore(3600);
    const fetchFn = mockFetch(204);
    const opts = {
      runtime: 'http' as const,
      wakeUrl: 'http://127.0.0.1:9/wake',
      dedupe: true,
      dedupeTtlSec: 3600,
      wakeTimeoutMs: 1000,
    };
    const body = messageSendBody({ metadata: { task_id: 'dup-1' } });

    await dispatchLocalReceiverAndWaitWake(
      'c1',
      body,
      opts,
      store,
      () => undefined,
      { fetchFn: fetchFn as unknown as typeof fetch, logFn: (l) => logs.push(l) }
    );
    await dispatchLocalReceiverAndWaitWake(
      'c2',
      body,
      opts,
      store,
      () => undefined,
      { fetchFn: fetchFn as unknown as typeof fetch, logFn: (l) => logs.push(l) }
    );

    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect(logs.some((l) => l.includes('deduped key=dup-1'))).toBe(true);
  });

  it('runtime command wakes with event JSON on stdin', async () => {
    const spawnFn = vi.fn(() => {
      const child = new EventEmitter() as EventEmitter & {
        stdout: EventEmitter;
        stderr: EventEmitter;
        stdin: { end: (b: Buffer) => void };
      };
      child.stdout = new EventEmitter();
      child.stderr = new EventEmitter();
      child.stdin = {
        end: (b: Buffer) => {
          const event = JSON.parse(b.toString('utf-8'));
          expect(event.event_type).toBe('a2a_message');
          expect(event.message_id).toBe('msg-cmd');
          queueMicrotask(() => child.emit('close', 0));
        },
      };
      return child;
    });

    const result = await wakeRuntime(
      {
        event_type: 'a2a_message',
        task_id: null,
        message_id: 'msg-cmd',
        context_id: null,
        from_agent: null,
        chat: null,
        received_at: new Date().toISOString(),
        raw: {},
      },
      { runtime: 'command', wakeExec: 'true', wakeTimeoutMs: 1000 },
      { spawnFn: spawnFn as never }
    );
    expect(result.ok).toBe(true);
  });
});

describe('handleA2aRequest --runtime vs legacy --exec', () => {
  it('runtime path returns accepted even without a local server', async () => {
    const resp = await handleA2aRequest(
      makeRequest(messageSendBody()),
      {
        runtime: {
          runtime: 'log',
          dedupe: false,
          dedupeTtlSec: 60,
        },
      },
      { logFn: () => undefined }
    );
    expect(resp.status).toBe(200);
    expect(JSON.parse(resp.body).result.kind).toBe('message');
  });

  it('legacy --exec still uses stdout as the A2A body', async () => {
    const spawnFn = vi.fn(() => {
      const child = new EventEmitter() as EventEmitter & {
        stdout: EventEmitter;
        stderr: EventEmitter;
        stdin: { end: (b: Buffer) => void };
      };
      child.stdout = new EventEmitter();
      child.stderr = new EventEmitter();
      child.stdin = { end: vi.fn() };
      queueMicrotask(() => {
        child.stdout.emit('data', Buffer.from('{"jsonrpc":"2.0","result":{"custom":true}}'));
        child.emit('close', 0);
      });
      return child;
    });
    const resp = await handleA2aRequest(
      makeRequest(messageSendBody()),
      { exec: 'cat' },
      { spawnFn: spawnFn as never }
    );
    expect(JSON.parse(resp.body).result.custom).toBe(true);
  });
});
