/**
 * Tests for `acn listen` (ADR-0012 Mode B agent-side listener).
 *
 * The WebSocket lifecycle (connect / reconnect / keepalive) is side-effecting
 * and exercised manually; here we pin the two pure, security-relevant pieces:
 *   - toWebsocketUrl: REST base URL → control-channel WS URL.
 *   - handleA2aRequest: relayed request → response frame, for both handler
 *     modes (--forward HTTP tunnel, --exec subprocess), incl. base64 bodies
 *     and failure mapping.
 */

import { EventEmitter } from 'events';
import { describe, expect, it, vi } from 'vitest';

import {
  dispatchA2aRequest,
  handleA2aRequest,
  postAgentHeartbeat,
  resolvePreferredModel,
  resolveSupportedModels,
  toWebsocketUrl,
  type A2aRequestFrame,
  type A2aStreamChunkFrame,
  type A2aStreamEndFrame,
  type OutboundFrame,
} from '../src/commands/listen.js';
import { formatModelHeartbeatLog } from '../src/commands/model-heartbeat.js';

function ownerApplyRequest(overrides: Partial<A2aRequestFrame> = {}): A2aRequestFrame {
  const { headers, ...rest } = overrides;
  return makeRequest({
    path: '/acn/v1/preferred-model',
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-acn-preferred-model-apply': '1',
      ...headers,
    },
    ...rest,
  });
}

function makeRequest(overrides: Partial<A2aRequestFrame> = {}): A2aRequestFrame {
  return {
    type: 'a2a_request',
    id: 'corr-1',
    method: 'POST',
    path: '/',
    headers: { 'content-type': 'application/json' },
    body: '{"jsonrpc":"2.0","method":"message/send"}',
    body_encoding: 'utf-8',
    ...overrides,
  };
}

function fakeResponse(status: number, body: string, contentType = 'application/json') {
  return {
    status,
    headers: { get: (k: string) => (k.toLowerCase() === 'content-type' ? contentType : null) },
    text: () => Promise.resolve(body),
  } as unknown as Response;
}

describe('toWebsocketUrl', () => {
  it('maps https → wss and points at /ws/{agentId}', () => {
    expect(toWebsocketUrl('https://api.acnlabs.dev', 'ag_123')).toBe(
      'wss://api.acnlabs.dev/ws/ag_123'
    );
  });

  it('maps http → ws and drops any base path / query', () => {
    expect(toWebsocketUrl('http://localhost:8000/?x=1', 'ag_local')).toBe(
      'ws://localhost:8000/ws/ag_local'
    );
  });
});

describe('handleA2aRequest --forward', () => {
  it('tunnels the request to the local server and relays the response', async () => {
    const fetchFn = vi.fn().mockResolvedValue(fakeResponse(200, '{"result":"ok"}'));
    const frame = makeRequest();

    const resp = await handleA2aRequest(frame, { forward: 'http://localhost:8080' }, { fetchFn });

    expect(fetchFn).toHaveBeenCalledTimes(1);
    const [url, init] = fetchFn.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('http://localhost:8080');
    expect(init.method).toBe('POST');
    expect((init.headers as Record<string, string>)['content-type']).toBe('application/json');
    expect(resp).toMatchObject({ type: 'a2a_response', id: 'corr-1', status: 200, body: '{"result":"ok"}' });
  });

  it('appends a non-root sub-path to the forward base', async () => {
    const fetchFn = vi.fn().mockResolvedValue(fakeResponse(200, 'ok', 'text/plain'));
    await handleA2aRequest(
      makeRequest({ path: 'tasks/get', method: 'GET', body: '' }),
      { forward: 'http://localhost:8080/' },
      { fetchFn }
    );
    const [url, init] = fetchFn.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('http://localhost:8080/tasks/get');
    expect(init.body).toBeUndefined(); // GET carries no body
  });

  it('decodes a base64 body before forwarding', async () => {
    const fetchFn = vi.fn().mockResolvedValue(fakeResponse(204, ''));
    const raw = Buffer.from([0xff, 0xfe, 0x00, 0x01]);
    await handleA2aRequest(
      makeRequest({ body: raw.toString('base64'), body_encoding: 'base64' }),
      { forward: 'http://localhost:8080' },
      { fetchFn }
    );
    const [, init] = fetchFn.mock.calls[0] as [string, RequestInit];
    expect(Buffer.compare(init.body as Buffer, raw)).toBe(0);
  });

  it('maps a handler exception to a 502 response frame', async () => {
    const fetchFn = vi.fn().mockRejectedValue(new Error('ECONNREFUSED'));
    const resp = await handleA2aRequest(makeRequest(), { forward: 'http://localhost:8080' }, { fetchFn });
    expect(resp.status).toBe(502);
    expect(resp.id).toBe('corr-1');
    expect(JSON.parse(resp.body).error).toContain('ECONNREFUSED');
  });
});

function sseResponse(chunks: string[], status = 200) {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(encoder.encode(c));
      controller.close();
    },
  });
  return {
    status,
    headers: {
      get: (k: string) =>
        k.toLowerCase() === 'content-type' ? 'text/event-stream' : null,
    },
    body,
  } as unknown as Response;
}

describe('dispatchA2aRequest --forward streaming (ADR-0012 P2d #171)', () => {
  it('streams an SSE response as chunk frames terminated by an end frame', async () => {
    const fetchFn = vi
      .fn()
      .mockResolvedValue(sseResponse(['data: a\n\n', 'data: b\n\n']));
    const frames: OutboundFrame[] = [];

    await dispatchA2aRequest(
      makeRequest(),
      { forward: 'http://localhost:8080' },
      (f) => frames.push(f),
      { fetchFn }
    );

    const chunks = frames.filter(
      (f): f is A2aStreamChunkFrame => f.type === 'a2a_stream_chunk'
    );
    expect(chunks.length).toBe(2);
    expect(chunks.map((c) => c.seq)).toEqual([0, 1]);
    expect(Buffer.from(chunks[0].data, 'base64').toString('utf-8')).toBe('data: a\n\n');
    expect(Buffer.from(chunks[1].data, 'base64').toString('utf-8')).toBe('data: b\n\n');

    const end = frames[frames.length - 1] as A2aStreamEndFrame;
    expect(end).toMatchObject({ type: 'a2a_stream_end', id: 'corr-1', status: 200 });
  });

  it('emits a single a2a_response for a non-SSE response', async () => {
    const fetchFn = vi.fn().mockResolvedValue(fakeResponse(200, '{"result":"ok"}'));
    const frames: OutboundFrame[] = [];

    await dispatchA2aRequest(
      makeRequest(),
      { forward: 'http://localhost:8080' },
      (f) => frames.push(f),
      { fetchFn }
    );

    expect(frames).toHaveLength(1);
    expect(frames[0]).toMatchObject({ type: 'a2a_response', status: 200 });
  });
});

describe('handleA2aRequest --exec', () => {
  function fakeSpawn(opts: { stdout?: string; stderr?: string; code: number }) {
    return vi.fn(() => {
      const child = new EventEmitter() as EventEmitter & {
        stdout: EventEmitter;
        stderr: EventEmitter;
        stdin: { end: (b: Buffer) => void };
      };
      child.stdout = new EventEmitter();
      child.stderr = new EventEmitter();
      child.stdin = { end: vi.fn() };
      // Emit data + close on next tick, after handlers are attached.
      queueMicrotask(() => {
        if (opts.stdout) child.stdout.emit('data', Buffer.from(opts.stdout));
        if (opts.stderr) child.stderr.emit('data', Buffer.from(opts.stderr));
        child.emit('close', opts.code);
      });
      return child;
    });
  }

  it('returns stdout with status 200 on exit code 0', async () => {
    const spawnFn = fakeSpawn({ stdout: '{"ok":true}', code: 0 }) as never;
    const resp = await handleA2aRequest(makeRequest(), { exec: 'cat' }, { spawnFn });
    expect(resp.status).toBe(200);
    expect(resp.body).toBe('{"ok":true}');
  });

  it('maps a non-zero exit to status 500 with stderr detail', async () => {
    const spawnFn = fakeSpawn({ stderr: 'boom', code: 1 }) as never;
    const resp = await handleA2aRequest(makeRequest(), { exec: 'false' }, { spawnFn });
    expect(resp.status).toBe(500);
    expect(JSON.parse(resp.body).error).toContain('boom');
  });
});

describe('resolvePreferredModel', () => {
  it('prefers --model flag over env', () => {
    expect(
      resolvePreferredModel({
        model: 'openai/gpt-4o',
        env: { ACN_PREFERRED_MODEL: 'openai/gpt-4o-mini' } as NodeJS.ProcessEnv,
      })
    ).toBe('openai/gpt-4o');
  });

  it('falls back to ACN_PREFERRED_MODEL', () => {
    expect(
      resolvePreferredModel({
        env: { ACN_PREFERRED_MODEL: ' anthropic/claude-sonnet-4 ' } as NodeJS.ProcessEnv,
      })
    ).toBe('anthropic/claude-sonnet-4');
  });
});

describe('postAgentHeartbeat', () => {
  it('POSTs preferred_model when provided', async () => {
    const fetchFn = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: 'ok', preferred_model: 'openai/gpt-4o-mini' }),
    });
    const r = await postAgentHeartbeat({
      baseUrl: 'https://api.acnlabs.dev',
      agentId: 'ag-1',
      apiKey: 'acn_key',
      preferredModel: 'openai/gpt-4o-mini',
      fetchFn: fetchFn as unknown as typeof fetch,
    });
    expect(r).toMatchObject({ ok: true, preferred_model: 'openai/gpt-4o-mini' });
    expect(fetchFn).toHaveBeenCalledTimes(1);
    const [url, init] = fetchFn.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('https://api.acnlabs.dev/api/v1/agents/ag-1/heartbeat');
    expect(init.method).toBe('POST');
    expect(init.body).toBe(JSON.stringify({ preferred_model: 'openai/gpt-4o-mini' }));
  });

  it('POSTs supported_models when provided', async () => {
    const fetchFn = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        status: 'ok',
        supported_models: ['openai/gpt-4o-mini', 'tencenttokenplan/kimi-k2.5'],
      }),
    });
    const r = await postAgentHeartbeat({
      baseUrl: 'https://api.acnlabs.dev',
      agentId: 'ag-1',
      apiKey: 'acn_key',
      supportedModels: ['openai/gpt-4o-mini', 'tencenttokenplan/kimi-k2.5'],
      fetchFn: fetchFn as unknown as typeof fetch,
    });
    expect(r).toMatchObject({
      ok: true,
      supported_models: ['openai/gpt-4o-mini', 'tencenttokenplan/kimi-k2.5'],
    });
    const [, init] = fetchFn.mock.calls[0] as [string, RequestInit];
    expect(init.body).toBe(
      JSON.stringify({
        supported_models: ['openai/gpt-4o-mini', 'tencenttokenplan/kimi-k2.5'],
      })
    );
  });
});

describe('resolveSupportedModels', () => {
  it('parses comma list from flag and env', () => {
    expect(
      resolveSupportedModels({
        models: 'openai/gpt-4o-mini, tencenttokenplan/kimi-k2.5',
      })
    ).toEqual(['openai/gpt-4o-mini', 'tencenttokenplan/kimi-k2.5']);
    expect(
      resolveSupportedModels({
        env: { ACN_SUPPORTED_MODELS: 'a/b;c/d' } as NodeJS.ProcessEnv,
      })
    ).toEqual(['a/b', 'c/d']);
  });

  it('clear returns empty list for server wipe', () => {
    expect(resolveSupportedModels({ clear: true })).toEqual([]);
  });
});

describe('formatModelHeartbeatLog', () => {
  it('parenthesizes preferred so ?? does not break ternary', () => {
    expect(
      formatModelHeartbeatLog({
        preferred: 'openai/gpt-4o-mini',
        supported: ['a/b'],
      })
    ).toBe(' preferred_model=openai/gpt-4o-mini supported_models=a/b');
  });
});

describe('preferred-model apply intercept', () => {
  it('applies before --forward and heartbeats the new default', async () => {
    const prev = process.env.ACN_PREFERRED_MODEL;
    const fetchFn = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () =>
        Promise.resolve({
          status: 'ok',
          preferred_model: 'minimax/minimax-m2.5',
        }),
    } as unknown as Response);
    const frames: OutboundFrame[] = [];
    const opts = {
      forward: 'http://localhost:8080',
      agentId: 'ag1',
      apiKey: 'k',
      baseUrl: 'https://api.acnlabs.dev',
      preferredModel: 'old/model',
      supportedModels: ['minimax/minimax-m2.5'],
    };

    await dispatchA2aRequest(
      ownerApplyRequest({
        body: JSON.stringify({ preferred_model: 'minimax/minimax-m2.5' }),
      }),
      opts,
      (f) => frames.push(f),
      { fetchFn }
    );

    expect(opts.preferredModel).toBe('minimax/minimax-m2.5');
    expect(frames[0]).toMatchObject({ type: 'a2a_response', status: 200 });
    const urls = fetchFn.mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.includes('/heartbeat'))).toBe(true);
    expect(urls.every((u) => !u.includes('localhost:8080'))).toBe(true);
    if (prev === undefined) delete process.env.ACN_PREFERRED_MODEL;
    else process.env.ACN_PREFERRED_MODEL = prev;
  });

  it('intercepts POST /acn/v1/runtime with the runtime marker', async () => {
    const fetchFn = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ status: 'ok' }),
    } as unknown as Response);
    const frames: OutboundFrame[] = [];
    await dispatchA2aRequest(
      ownerApplyRequest({
        path: '/acn/v1/runtime',
        body: JSON.stringify({ preferred_model: 'minimax/minimax-m2.5' }),
        headers: {
          'content-type': 'application/json',
          'x-acn-runtime-apply': '1',
        },
      }),
      {
        forward: 'http://localhost:8080',
        agentId: 'ag1',
        apiKey: 'k',
        baseUrl: 'https://api.acnlabs.dev',
        preferredModel: 'old/model',
      },
      (f) => frames.push(f),
      { fetchFn }
    );
    expect(frames[0]).toMatchObject({ type: 'a2a_response', status: 200 });
    expect(fetchFn.mock.calls.some((c) => String(c[0]).includes('/heartbeat'))).toBe(
      true
    );
  });

  it('rejects a model not on supported_models', async () => {
    const fetchFn = vi.fn();
    const frames: OutboundFrame[] = [];
    await dispatchA2aRequest(
      ownerApplyRequest({
        body: JSON.stringify({ preferred_model: 'openai/gpt-4o' }),
      }),
      {
        exec: 'true',
        agentId: 'ag1',
        apiKey: 'k',
        baseUrl: 'https://api.acnlabs.dev',
        supportedModels: ['minimax/minimax-m2.5'],
      },
      (f) => frames.push(f),
      { fetchFn }
    );
    expect(frames[0]).toMatchObject({
      status: 400,
      body: expect.stringContaining('unsupported_model'),
    });
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it('does not intercept a suffixed lookalike path', async () => {
    const fetchFn = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: { get: (k: string) => (k.toLowerCase() === 'content-type' ? 'application/json' : null) },
      text: () => Promise.resolve('{"result":"ok"}'),
    } as unknown as Response);
    const frames: OutboundFrame[] = [];
    const opts = {
      forward: 'http://localhost:8080',
      agentId: 'ag1',
      apiKey: 'k',
      baseUrl: 'https://api.acnlabs.dev',
      preferredModel: 'old/model',
    };
    await dispatchA2aRequest(
      makeRequest({
        path: '/foo/acn/v1/preferred-model',
        body: JSON.stringify({ preferred_model: 'minimax/minimax-m2.5' }),
      }),
      opts,
      (f) => frames.push(f),
      { fetchFn }
    );
    expect(opts.preferredModel).toBe('old/model');
    const urls = fetchFn.mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.includes('localhost:8080'))).toBe(true);
    expect(urls.every((u) => !u.includes('/heartbeat'))).toBe(true);
  });

  it('403s a preferred-model path without the owner marker and does not forward', async () => {
    const fetchFn = vi.fn();
    const frames: OutboundFrame[] = [];
    await dispatchA2aRequest(
      makeRequest({
        path: '/acn/v1/preferred-model',
        body: JSON.stringify({ preferred_model: 'minimax/minimax-m2.5' }),
      }),
      {
        forward: 'http://localhost:8080',
        agentId: 'ag1',
        apiKey: 'k',
        baseUrl: 'https://api.acnlabs.dev',
        preferredModel: 'old/model',
      },
      (f) => frames.push(f),
      { fetchFn }
    );
    expect(frames[0]).toMatchObject({
      status: 403,
      body: expect.stringContaining('runtime_owner_only'),
    });
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it('403s a preferred-model path that carries X-ACN-Caller-Agent', async () => {
    const fetchFn = vi.fn();
    const frames: OutboundFrame[] = [];
    await dispatchA2aRequest(
      ownerApplyRequest({
        body: JSON.stringify({ preferred_model: 'minimax/minimax-m2.5' }),
        headers: {
          'x-acn-preferred-model-apply': '1',
          'x-acn-caller-agent': 'attacker',
        },
      }),
      {
        forward: 'http://localhost:8080',
        agentId: 'ag1',
        apiKey: 'k',
        baseUrl: 'https://api.acnlabs.dev',
      },
      (f) => frames.push(f),
      { fetchFn }
    );
    expect(frames[0]).toMatchObject({ status: 403 });
    expect(fetchFn).not.toHaveBeenCalled();
  });
});
