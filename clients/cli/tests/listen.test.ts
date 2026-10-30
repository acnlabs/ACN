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
import {
  bodyLooksLikeModelRefusal,
  chatCompletionsUrlFromModelsUrl,
  classifyKeyCallProbe,
  clearProviderProbeDenialCache,
  collapseModelSpellings,
  declarationHeartbeatFields,
  formatModelHeartbeatLog,
  fetchRuntimeModelReport,
  isKeyPermissionDenied,
  providerModelsFromListBody,
  providerProbeDenialTtlMs,
  rowKeyAuthorized,
  supportedModelsToReport,
  trustProviderCatalogWithoutKeyScope,
} from '../src/commands/model-heartbeat.js';

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

  it('does not invent a model list when the server omitted one', () => {
    expect(formatModelHeartbeatLog({ preferred: 'openai/gpt-4o-mini' })).toBe(
      ' preferred_model=openai/gpt-4o-mini'
    );
  });

  it('names a cleared declaration instead of falling back to a handwritten list', () => {
    expect(formatModelHeartbeatLog({ supported: [] })).toBe(
      ' supported_models=(cleared)'
    );
  });
});

describe('declarationHeartbeatFields', () => {
  it('clears providers and the derived union without reading a handwritten list', () => {
    expect(
      declarationHeartbeatFields({
        clear: true,
        report: {
          supportedModels: ['tencenttokenplan/glm-5'],
          providers: [{ id: 'tencenttokenplan', models: ['tencenttokenplan/glm-5'], current: true }],
        },
      })
    ).toEqual({
      supportedModels: [],
      providers: [],
      modelPrices: undefined,
    });
  });

  it('sends nothing when a provider list could not be read', () => {
    expect(declarationHeartbeatFields({ clear: false, report: {} })).toEqual({});
  });

  it('sends the provider rows when every list was read', () => {
    const report = {
      supportedModels: ['tencenttokenplan/glm-5'],
      providers: [{ id: 'tencenttokenplan', models: ['tencenttokenplan/glm-5'], current: true }],
      modelPrices: [{ model_id: 'tencenttokenplan/glm-5', input_price_per_million: 1, output_price_per_million: 2 }],
    };
    expect(declarationHeartbeatFields({ clear: false, report })).toEqual({
      providers: report.providers,
      modelPrices: report.modelPrices,
    });
  });
});

describe('preferred-model apply intercept', () => {
  it('applies before --forward and heartbeats the new default', async () => {
    const prev = process.env.ACN_PREFERRED_MODEL;
    const prevHub = process.env.ACN_PROVIDER_API_KEY;
    const prevUrl = process.env.ACN_PROVIDER_MODELS_URL;
    const prevProvider = process.env.ACN_PROVIDER;
    process.env.ACN_PROVIDER = 'minimax';
    process.env.ACN_PROVIDER_MODELS_URL = 'https://provider.example/v1/models';
    process.env.ACN_PROVIDER_API_KEY = 'k';
    const fetchFn = vi.fn(async (url: string) => {
      if (String(url).includes('provider.example')) {
        return {
          ok: true,
          status: 200,
          json: () => Promise.resolve({ data: [{ id: 'minimax-m2.5' }] }),
        } as Response;
      }
      return {
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            status: 'ok',
            preferred_model: 'minimax/minimax-m2.5',
          }),
      } as Response;
    });
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
      { fetchFn: fetchFn as unknown as typeof fetch }
    );

    expect(opts.preferredModel).toBe('minimax/minimax-m2.5');
    expect(frames[0]).toMatchObject({ type: 'a2a_response', status: 200 });
    const urls = fetchFn.mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.includes('/heartbeat'))).toBe(true);
    expect(urls.every((u) => !u.includes('localhost:8080'))).toBe(true);
    if (prev === undefined) delete process.env.ACN_PREFERRED_MODEL;
    else process.env.ACN_PREFERRED_MODEL = prev;
    if (prevHub === undefined) delete process.env.ACN_PROVIDER_API_KEY;
    else process.env.ACN_PROVIDER_API_KEY = prevHub;
    if (prevUrl === undefined) delete process.env.ACN_PROVIDER_MODELS_URL;
    else process.env.ACN_PROVIDER_MODELS_URL = prevUrl;
    if (prevProvider === undefined) delete process.env.ACN_PROVIDER;
    else process.env.ACN_PROVIDER = prevProvider;
  });

  it('applies a catalog model the hand allowlist does not name', async () => {
    const prevHub = process.env.ACN_PROVIDER_API_KEY;
    const prevUrl = process.env.ACN_PROVIDER_MODELS_URL;
    const prevProvider = process.env.ACN_PROVIDER;
    const prevModels = process.env.ACN_SUPPORTED_MODELS;
    process.env.ACN_PROVIDER_API_KEY = 'catalog-key';
    process.env.ACN_PROVIDER_MODELS_URL = 'https://provider.example/v1/models';
    process.env.ACN_PROVIDER = 'tencenttokenplan';
    delete process.env.ACN_SUPPORTED_MODELS;
    const fetchFn = vi.fn(async (url: string) => {
      if (String(url).includes('provider.example')) {
        return {
          ok: true,
          status: 200,
          json: () => Promise.resolve({ data: [{ id: 'glm-5' }, { id: 'glm-5.1' }] }),
        } as Response;
      }
      return {
        ok: true,
        status: 200,
        json: () => Promise.resolve({ status: 'ok', preferred_model: 'tencenttokenplan/glm-5' }),
      } as Response;
    });
    const frames: OutboundFrame[] = [];
    await dispatchA2aRequest(
      ownerApplyRequest({
        body: JSON.stringify({ preferred_model: 'tencenttokenplan/glm-5' }),
      }),
      {
        forward: 'http://localhost:8080',
        agentId: 'ag1',
        apiKey: 'k',
        baseUrl: 'https://api.acnlabs.dev',
        preferredModel: 'tencenttokenplan/glm-5.1',
        supportedModels: ['tencenttokenplan/deepseek-v4-flash-202605'],
      },
      (f) => frames.push(f),
      { fetchFn: fetchFn as unknown as typeof fetch }
    );
    expect(frames[0]).toMatchObject({ type: 'a2a_response', status: 200 });
    const heartbeat = fetchFn.mock.calls.find((c) =>
      String(c[0]).includes('/heartbeat')
    ) as [string, RequestInit] | undefined;
    const sent = JSON.parse(String(heartbeat?.[1]?.body ?? '{}'));
    expect(sent.preferred_model).toBe('tencenttokenplan/glm-5');
    expect(sent.providers).toEqual([
      {
        id: 'tencenttokenplan',
        models: ['tencenttokenplan/glm-5', 'tencenttokenplan/glm-5.1'],
        current: true,
      },
    ]);
    expect(sent.supported_models).toBeUndefined();
    if (prevHub === undefined) delete process.env.ACN_PROVIDER_API_KEY;
    else process.env.ACN_PROVIDER_API_KEY = prevHub;
    if (prevUrl === undefined) delete process.env.ACN_PROVIDER_MODELS_URL;
    else process.env.ACN_PROVIDER_MODELS_URL = prevUrl;
    if (prevProvider === undefined) delete process.env.ACN_PROVIDER;
    else process.env.ACN_PROVIDER = prevProvider;
    if (prevModels === undefined) delete process.env.ACN_SUPPORTED_MODELS;
    else process.env.ACN_SUPPORTED_MODELS = prevModels;
  });

  it('heartbeats prices for the models the price book missed', async () => {
    const prev = process.env.ACN_MODEL_PRICES;
    process.env.ACN_MODEL_PRICES = JSON.stringify([
      {
        model_id: 'tencenttokenplan/kimi-k2.5',
        input_price_per_million: 0.4,
        output_price_per_million: 1.2,
      },
    ]);
    const fetchFn = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ status: 'ok' }),
    } as unknown as Response);
    const frames: OutboundFrame[] = [];
    await dispatchA2aRequest(
      ownerApplyRequest({
        path: '/acn/v1/runtime',
        body: JSON.stringify({
          quote_model_prices: ['tencenttokenplan/kimi-k2.5', 'tencenttokenplan/glm-5'],
          error: 'price_required',
        }),
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
      },
      (f) => frames.push(f),
      { fetchFn }
    );
    expect(frames[0]).toMatchObject({ type: 'a2a_response', status: 200 });
    const body = JSON.parse(String((frames[0] as { body?: string }).body));
    expect(body.model_prices).toEqual([
      {
        model_id: 'tencenttokenplan/kimi-k2.5',
        input_price_per_million: 0.4,
        output_price_per_million: 1.2,
      },
    ]);
    const heartbeat = fetchFn.mock.calls.find((c) => String(c[0]).includes('/heartbeat'));
    expect(heartbeat).toBeTruthy();
    const sent = JSON.parse(String((heartbeat?.[1] as { body?: string } | undefined)?.body));
    expect(sent.model_prices).toEqual(body.model_prices);
    if (prev === undefined) delete process.env.ACN_MODEL_PRICES;
    else process.env.ACN_MODEL_PRICES = prev;
  });

  it('invokes onQuoteModelPrices when catalog and env miss (incl. price_required retry)', async () => {
    const prev = process.env.ACN_MODEL_PRICES;
    delete process.env.ACN_MODEL_PRICES;
    const prevUrl = process.env.ACN_PROVIDER_MODELS_URL;
    delete process.env.ACN_PROVIDER_MODELS_URL;
    const logs: string[] = [];
    const fetchFn = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ status: 'ok' }),
    } as unknown as Response);
    const spawnFn = vi.fn((_cmd: string) => {
      const handlers: Record<string, Array<(...args: unknown[]) => void>> = {};
      return {
        stdout: {
          on: (ev: string, fn: (...args: unknown[]) => void) => {
            (handlers[`stdout:${ev}`] ??= []).push(fn);
          },
        },
        stderr: { on: () => {} },
        stdin: {
          end: (payload: string) => {
            const ask = JSON.parse(payload);
            expect(ask.retry).toBe(true);
            expect(ask.model_ids).toEqual(['tencenttokenplan/glm-5']);
            const out = JSON.stringify({
              model_prices: [
                {
                  model_id: 'tencenttokenplan/glm-5',
                  input_price_per_million: 0.5,
                  output_price_per_million: 1.5,
                },
              ],
            });
            for (const fn of handlers['stdout:data'] || []) fn(Buffer.from(out));
            for (const fn of handlers['close'] || []) fn(0);
          },
        },
        on: (ev: string, fn: (...args: unknown[]) => void) => {
          (handlers[ev] ??= []).push(fn);
        },
        kill: vi.fn(),
      };
    });
    const frames: OutboundFrame[] = [];
    await dispatchA2aRequest(
      ownerApplyRequest({
        path: '/acn/v1/runtime',
        body: JSON.stringify({
          quote_model_prices: ['tencenttokenplan/glm-5'],
          error: 'price_required',
        }),
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
        onQuoteModelPrices: 'tokenplan-quote',
      },
      (f) => frames.push(f),
      { fetchFn, spawnFn: spawnFn as never, logFn: (line) => logs.push(line) }
    );
    expect(spawnFn).toHaveBeenCalled();
    expect(frames[0]).toMatchObject({ type: 'a2a_response', status: 200 });
    const body = JSON.parse(String((frames[0] as { body?: string }).body));
    expect(body.model_prices).toEqual([
      {
        model_id: 'tencenttokenplan/glm-5',
        input_price_per_million: 0.5,
        output_price_per_million: 1.5,
      },
    ]);
    expect(logs.some((l) => l.startsWith('quote_hook'))).toBe(true);
    if (prev === undefined) delete process.env.ACN_MODEL_PRICES;
    else process.env.ACN_MODEL_PRICES = prev;
    if (prevUrl === undefined) delete process.env.ACN_PROVIDER_MODELS_URL;
    else process.env.ACN_PROVIDER_MODELS_URL = prevUrl;
  });

  it('heartbeats the current model list when asked to report again', async () => {
    const prev = process.env.ACN_SUPPORTED_MODELS;
    const prevHub = process.env.ACN_PROVIDER_API_KEY;
    const prevUrl = process.env.ACN_PROVIDER_MODELS_URL;
    delete process.env.ACN_SUPPORTED_MODELS;
    delete process.env.ACN_PROVIDER_API_KEY;
    delete process.env.ACN_PROVIDER_MODELS_URL;
    const fetchFn = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () =>
        Promise.resolve({
          status: 'ok',
          supported_models: ['moonshot/kimi-k2', 'moonshot/kimi-k2.6'],
        }),
    } as unknown as Response);
    const frames: OutboundFrame[] = [];
    await dispatchA2aRequest(
      ownerApplyRequest({
        path: '/acn/v1/runtime',
        body: JSON.stringify({ refresh_supported_models: true }),
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
        supportedModels: ['moonshot/kimi-k2', 'moonshot/kimi-k2.6'],
      },
      (f) => frames.push(f),
      { fetchFn }
    );
    expect(frames[0]).toMatchObject({
      type: 'a2a_response',
      status: 503,
      body: expect.stringContaining('provider_list_unavailable'),
    });
    const urls = fetchFn.mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.includes('/heartbeat'))).toBe(false);
    if (prev === undefined) delete process.env.ACN_SUPPORTED_MODELS;
    else process.env.ACN_SUPPORTED_MODELS = prev;
    if (prevHub === undefined) delete process.env.ACN_PROVIDER_API_KEY;
    else process.env.ACN_PROVIDER_API_KEY = prevHub;
    if (prevUrl === undefined) delete process.env.ACN_PROVIDER_MODELS_URL;
    else process.env.ACN_PROVIDER_MODELS_URL = prevUrl;
  });

  it('reports the model list from the provider the agent is using', async () => {
    const prevHub = process.env.ACN_PROVIDER_API_KEY;
    const prevUrl = process.env.ACN_PROVIDER_MODELS_URL;
    const prevProvider = process.env.ACN_PROVIDER;
    process.env.ACN_PROVIDER_API_KEY = 'catalog-key';
    process.env.ACN_PROVIDER_MODELS_URL = 'https://provider.example/v1/models';
    process.env.ACN_PROVIDER = 'tencenttokenplan';
    const fetchFn = vi.fn(async (url: string) => {
      if (String(url).includes('provider.example')) {
        return {
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve({
              data: [
                { id: 'deepseek-v4-flash-202605' },
                { id: 'deepseek/deepseek-flash' },
                { id: 'kimi-k2.5', status: 'offline' },
              ],
            }),
        } as unknown as Response;
      }
      return {
        ok: true,
        status: 200,
        json: () => Promise.resolve({ status: 'ok' }),
      } as unknown as Response;
    });
    const frames: OutboundFrame[] = [];
    const configured = [
      'tencenttokenplan/kimi-k2.5',
      'tencenttokenplan/deepseek-v4-flash-202605',
      'tencenttokenplan/deepseek/deepseek-flash',
      'moonshot/kimi-k2',
    ];
    await dispatchA2aRequest(
      ownerApplyRequest({
        path: '/acn/v1/runtime',
        body: JSON.stringify({ refresh_supported_models: true }),
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
        supportedModels: configured,
      },
      (f) => frames.push(f),
      { fetchFn: fetchFn as unknown as typeof fetch }
    );
    const body = JSON.parse(String((frames[0] as { body?: string }).body));
    expect(body.providers).toEqual([
      {
        id: 'tencenttokenplan',
        models: [
          'tencenttokenplan/deepseek-v4-flash-202605',
          'tencenttokenplan/deepseek/deepseek-flash',
        ],
        current: true,
      },
    ]);
    expect(body.supported_models).toBeUndefined();
    const heartbeat = fetchFn.mock.calls.find((c) =>
      String(c[0]).includes('/heartbeat')
    ) as [string, RequestInit] | undefined;
    const sent = JSON.parse(String(heartbeat?.[1]?.body ?? ''));
    expect(sent.providers).toEqual(body.providers);
    expect(sent.supported_models).toBeUndefined();
    if (prevHub === undefined) delete process.env.ACN_PROVIDER_API_KEY;
    else process.env.ACN_PROVIDER_API_KEY = prevHub;
    if (prevUrl === undefined) delete process.env.ACN_PROVIDER_MODELS_URL;
    else process.env.ACN_PROVIDER_MODELS_URL = prevUrl;
    if (prevProvider === undefined) delete process.env.ACN_PROVIDER;
    else process.env.ACN_PROVIDER = prevProvider;
  });

  it('keeps one id when a provider lists the same model twice', () => {
    expect(
      collapseModelSpellings([
        'glm-5.3',
        'glm-5-3',
        'minimax-m2.7',
        'minimax-m-2-7',
        'glm-5-0',
        'glm-5',
        'deepseek-v4-flash-202605',
        'deepseek/deepseek-v4-flash',
      ])
    ).toEqual([
      'glm-5.3',
      'minimax-m2.7',
      'glm-5',
      'deepseek-v4-flash-202605',
      'deepseek/deepseek-v4-flash',
    ]);
  });

  it('reports keyInvalid with empty models on catalog 401 (does not omit providers)', async () => {
    const report = await fetchRuntimeModelReport({
      env: {
        ACN_PROVIDER: 'tencenttokenplan',
        ACN_PROVIDER_API_KEY: 'catalog-key',
        ACN_PROVIDER_MODELS_URL: 'https://provider.example/v1/models',
      },
      fetchFn: vi.fn().mockResolvedValue({
        ok: false,
        status: 401,
        json: () => Promise.resolve({}),
      }),
    });
    expect(report.keyInvalid).toBe(true);
    expect(report.providers).toEqual([
      { id: 'tencenttokenplan', models: [], current: true },
    ]);
    expect(report.supportedModels).toEqual([]);
    const noKey = await supportedModelsToReport({
      env: {
        ACN_SUPPORTED_MODELS: 'tencenttokenplan/kimi-k2.5,tencenttokenplan/glm-5',
      },
      fetchFn: vi.fn(),
    });
    expect(noKey).toBeUndefined();
  });

  it('keeps healthy providers when a sibling catalog fails', async () => {
    const report = await fetchRuntimeModelReport({
      env: {
        ACN_PROVIDER: 'tencenttokenplan',
        ACN_PROVIDER_MODELS_URL: 'https://tencent.example/models',
        ACN_PROVIDER_API_KEY: 't-key',
        ACN_PROVIDERS: JSON.stringify([
          {
            id: 'openrouter',
            models_url: 'https://openrouter.example/models',
            api_key: 'o-key',
          },
        ]),
        // Avoid key-call probes so the test only checks catalog fan-out.
        ACN_PROVIDER_MODELS_TRUST_CATALOG: '1',
      },
      fetchFn: vi.fn(async (url: string) => {
        if (String(url).includes('tencent.example')) {
          return {
            ok: true,
            json: async () => ({ data: [{ id: 'glm-5' }] }),
          } as Response;
        }
        return { ok: false, status: 503, json: async () => ({}) } as Response;
      }) as unknown as typeof fetch,
    });
    expect(report.providers).toEqual([
      {
        id: 'tencenttokenplan',
        models: ['tencenttokenplan/glm-5'],
        current: true,
      },
      { id: 'openrouter', models: [], current: false },
    ]);
    expect(report.supportedModels).toEqual(['tencenttokenplan/glm-5']);
  });

  it('does not treat catalog available:true as Key scope without TRUST flag', async () => {
    const parsed = providerModelsFromListBody({
      data: [
        { id: 'kimi-k2.5', available: true },
        { id: 'kimi-k3', available: true },
      ],
    });
    expect(parsed.hadKeyScopeHints).toBe(false);
    expect(parsed.keyScopedIds).toEqual([]);
    expect(parsed.catalogOnlineIds).toEqual(['kimi-k2.5', 'kimi-k3']);
  });

  it('heartbeats only key-authorized SKUs when the provider exposes scope', async () => {
    expect(rowKeyAuthorized({ id: 'kimi-k2.5', authorized: false })).toBe(false);
    expect(rowKeyAuthorized({ id: 'kimi-k3', authorized: true })).toBe(true);
    const parsed = providerModelsFromListBody({
      data: [
        { id: 'kimi-k2.5', authorized: false },
        { id: 'kimi-k3', authorized: true },
        { id: 'deepseek-v4-flash-202605', authorized: true },
      ],
    });
    expect(parsed.hadKeyScopeHints).toBe(true);
    expect(parsed.keyScopedIds).toEqual(['kimi-k3', 'deepseek-v4-flash-202605']);
    const report = await fetchRuntimeModelReport({
      env: {
        ACN_PROVIDER_API_KEY: 'catalog-key',
        ACN_PROVIDER_MODELS_URL: 'https://provider.example/v1/models',
        ACN_PROVIDER: 'tencenttokenplan',
      },
      fetchFn: vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            data: [
              { id: 'kimi-k2.5', authorized: false },
              { id: 'kimi-k3', authorized: true },
            ],
          }),
      }),
    });
    expect(report.supportedModels).toEqual(['tencenttokenplan/kimi-k3']);
    expect(report.providers).toEqual([
      { id: 'tencenttokenplan', models: ['tencenttokenplan/kimi-k3'], current: true },
    ]);
  });

  it('keeps key-allowed SKUs and online rows that carry no scope field', async () => {
    const report = await fetchRuntimeModelReport({
      env: {
        ACN_PROVIDER_API_KEY: 'catalog-key',
        ACN_PROVIDER_MODELS_URL: 'https://provider.example/v1/models',
        ACN_PROVIDER: 'tencenttokenplan',
      },
      fetchFn: vi.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({
            data: [
              { id: 'kimi-k2.5', authorized: false },
              { id: 'kimi-k3', authorized: true },
              { id: 'glm-5.1' },
            ],
          }),
      }),
    });
    expect(report.supportedModels).toEqual([
      'tencenttokenplan/kimi-k3',
      'tencenttokenplan/glm-5.1',
    ]);
  });

  it('heartbeats every key-allowed SKU of this provider', async () => {
    const report = await fetchRuntimeModelReport({
      env: {
        ACN_PROVIDER_API_KEY: 'catalog-key',
        ACN_PROVIDER_MODELS_URL: 'https://provider.example/v1/models',
        ACN_PROVIDER: 'tencenttokenplan',
      },
      fetchFn: vi.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({
            data: [
              { id: 'kimi-k3', authorized: true },
              { id: 'glm-5.1', authorized: true },
            ],
          }),
      }),
    });
    expect(report.supportedModels).toEqual([
      'tencenttokenplan/kimi-k3',
      'tencenttokenplan/glm-5.1',
    ]);
  });

  it('omits models the provider key explicitly denied', async () => {
    const report = await fetchRuntimeModelReport({
      env: {
        ACN_PROVIDER_API_KEY: 'k',
        ACN_PROVIDER_MODELS_URL: 'https://provider.example/v1/models',
        ACN_PROVIDER: 'tencenttokenplan',
      },
      fetchFn: vi.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({
            data: [{ id: 'kimi-k2.5', authorized: false }],
          }),
      }),
    });
    expect(report.supportedModels).toEqual([]);
  });

  it('reports an empty provider row when every listed model is denied', async () => {
    const report = await fetchRuntimeModelReport({
      env: {
        ACN_PROVIDER_API_KEY: 'k',
        ACN_PROVIDER_MODELS_URL: 'https://provider.example/v1/models',
        ACN_PROVIDER: 'tencenttokenplan',
      },
      fetchFn: vi.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({
            data: [{ id: 'kimi-k2.5', authorized: false }],
          }),
      }),
    });
    expect(report.supportedModels).toEqual([]);
  });

  it('probes chat completions when the catalog has no key-scope fields', async () => {
    clearProviderProbeDenialCache();
    expect(chatCompletionsUrlFromModelsUrl('https://provider.example/v1/models')).toBe(
      'https://provider.example/v1/chat/completions',
    );
    expect(trustProviderCatalogWithoutKeyScope({})).toBe(false);
    expect(
      isKeyPermissionDenied(403, {
        error: { code: '403002', type: 'permission_error', message: 'not authorized' },
      }),
    ).toBe(true);

    const fetchFn = vi.fn(async (url: string, init?: RequestInit) => {
      const href = String(url);
      if (href.endsWith('/models')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            data: [
              { id: 'minimax-m2.7' },
              { id: 'kimi-k2.5' },
              { id: 'glm-5.1' },
            ],
          }),
        } as Response;
      }
      const body = JSON.parse(String(init?.body || '{}')) as { model?: string };
      if (body.model === 'kimi-k2.5') {
        return {
          ok: false,
          status: 403,
          json: async () => ({
            error: {
              code: '403002',
              type: 'permission_error',
              message: 'The current API Key is not authorized to access model kimi-k2.5',
            },
          }),
        } as Response;
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({
          choices: [{ message: { content: 'ok' } }],
        }),
      } as Response;
    }) as unknown as typeof fetch;

    const reported = await supportedModelsToReport({
      env: {
        ACN_PROVIDER_API_KEY: 'catalog-key',
        ACN_PROVIDER_MODELS_URL: 'https://provider.example/v1/models',
        ACN_PROVIDER: 'tencenttokenplan',
      },
      fetchFn,
    });
    expect(reported).toEqual([
      'tencenttokenplan/minimax-m2.7',
      'tencenttokenplan/glm-5.1',
    ]);
  });

  it('caches clear permission denials so the next heartbeat skips re-probe', async () => {
    clearProviderProbeDenialCache();
    expect(providerProbeDenialTtlMs({})).toBe(60 * 60 * 1000);
    expect(providerProbeDenialTtlMs({ ACN_PROVIDER_PROBE_DENIAL_TTL_SEC: '0' })).toBe(0);

    const chatBodies: string[] = [];
    const fetchFn = vi.fn(async (url: string, init?: RequestInit) => {
      const href = String(url);
      if (href.endsWith('/models')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            data: [{ id: 'minimax-m2.7' }, { id: 'kimi-k2.5' }],
          }),
        } as Response;
      }
      const body = JSON.parse(String(init?.body || '{}')) as { model?: string };
      chatBodies.push(String(body.model || ''));
      if (body.model === 'kimi-k2.5') {
        return {
          ok: false,
          status: 403,
          json: async () => ({ error: { code: '403002', type: 'permission_error' } }),
        } as Response;
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({ choices: [{ message: { content: 'ok' } }] }),
      } as Response;
    }) as unknown as typeof fetch;

    const env = {
      ACN_PROVIDER_API_KEY: 'catalog-key',
      ACN_PROVIDER_MODELS_URL: 'https://provider.example/v1/models',
      ACN_PROVIDER: 'tencenttokenplan',
    };
    const first = await supportedModelsToReport({ env, fetchFn });
    const second = await supportedModelsToReport({ env, fetchFn });
    expect(first).toEqual(['tencenttokenplan/minimax-m2.7']);
    expect(second).toEqual(['tencenttokenplan/minimax-m2.7']);
    expect(chatBodies.filter((id) => id === 'kimi-k2.5')).toHaveLength(1);
    // Allowed SKUs still re-probe each heartbeat (only denials are cached).
    expect(chatBodies.filter((id) => id === 'minimax-m2.7')).toHaveLength(2);

    clearProviderProbeDenialCache();
    const third = await supportedModelsToReport({ env, fetchFn });
    expect(third).toEqual(['tencenttokenplan/minimax-m2.7']);
    expect(chatBodies.filter((id) => id === 'kimi-k2.5')).toHaveLength(2);
  });

  it('ACN_PROVIDER_PROBE_DENIAL_TTL_SEC=0 disables denial cache', async () => {
    clearProviderProbeDenialCache();
    const chatBodies: string[] = [];
    const fetchFn = vi.fn(async (url: string, init?: RequestInit) => {
      const href = String(url);
      if (href.endsWith('/models')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ data: [{ id: 'kimi-k2.5' }] }),
        } as Response;
      }
      const body = JSON.parse(String(init?.body || '{}')) as { model?: string };
      chatBodies.push(String(body.model || ''));
      return {
        ok: false,
        status: 403,
        json: async () => ({ error: { type: 'permission_error' } }),
      } as Response;
    }) as unknown as typeof fetch;

    const env = {
      ACN_PROVIDER_API_KEY: 'catalog-key',
      ACN_PROVIDER_MODELS_URL: 'https://provider.example/v1/models',
      ACN_PROVIDER: 'tencenttokenplan',
      ACN_PROVIDER_PROBE_DENIAL_TTL_SEC: '0',
    };
    await supportedModelsToReport({ env, fetchFn });
    await supportedModelsToReport({ env, fetchFn });
    expect(chatBodies.filter((id) => id === 'kimi-k2.5')).toHaveLength(2);
  });

  it('drops discontinued / 4xx probes; keeps 429 and generic 5xx fail-open', async () => {
    clearProviderProbeDenialCache();
    expect(classifyKeyCallProbe(400, { error: { message: 'discontinued' } })).toBe(
      'denied',
    );
    expect(classifyKeyCallProbe(404, { error: { message: 'model_not_found' } })).toBe(
      'denied',
    );
    expect(classifyKeyCallProbe(429, {})).toBe('unknown');
    expect(classifyKeyCallProbe(503, {})).toBe('unknown');
    expect(
      classifyKeyCallProbe(500, {
        error: { message: 'token plan quota exhausted', code: '20098' },
      }),
    ).toBe('quota_exhausted');
    expect(
      bodyLooksLikeModelRefusal({
        error: {
          message:
            'kimi-k2.5 was officially discontinued and is no longer maintained',
        },
      }),
    ).toBe(true);
    expect(
      classifyKeyCallProbe(200, {
        error: { message: 'The model does not exist or is discontinued' },
      }),
    ).toBe('denied');
    expect(isKeyPermissionDenied(400, { error: { message: 'invalid model' } })).toBe(
      true,
    );

    const fetchFn = vi.fn(async (url: string, init?: RequestInit) => {
      const href = String(url);
      if (href.endsWith('/models')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            data: [
              { id: 'minimax-m2.7' },
              { id: 'kimi-k2.5' },
              { id: 'gone-model' },
              { id: 'busy-model' },
              { id: 'down-model' },
            ],
          }),
        } as Response;
      }
      const body = JSON.parse(String(init?.body || '{}')) as { model?: string };
      if (body.model === 'kimi-k2.5') {
        return {
          ok: false,
          status: 400,
          json: async () => ({
            error: {
              message:
                'kimi-k2.5 was officially discontinued on August 31, 2026',
            },
          }),
        } as Response;
      }
      if (body.model === 'gone-model') {
        return {
          ok: false,
          status: 404,
          json: async () => ({ error: { code: 'model_not_found' } }),
        } as Response;
      }
      if (body.model === 'busy-model') {
        return { ok: false, status: 429, json: async () => ({}) } as Response;
      }
      if (body.model === 'down-model') {
        return { ok: false, status: 503, json: async () => ({}) } as Response;
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({ choices: [{ message: { content: 'ok' } }] }),
      } as Response;
    }) as unknown as typeof fetch;

    const reported = await supportedModelsToReport({
      env: {
        ACN_PROVIDER_API_KEY: 'catalog-key',
        ACN_PROVIDER_MODELS_URL: 'https://provider.example/v1/models',
        ACN_PROVIDER: 'tencenttokenplan',
      },
      fetchFn,
    });
    expect(reported).toEqual([
      'tencenttokenplan/minimax-m2.7',
      'tencenttokenplan/busy-model',
      'tencenttokenplan/down-model',
    ]);
  });

  it('TRUST_CATALOG skips probes and heartbeats the full product shelf', async () => {
    const reported = await supportedModelsToReport({
      env: {
        ACN_PROVIDER_API_KEY: 'catalog-key',
        ACN_PROVIDER_MODELS_URL: 'https://provider.example/v1/models',
        ACN_PROVIDER: 'tencenttokenplan',
        ACN_PROVIDER_MODELS_TRUST_CATALOG: '1',
      },
      fetchFn: vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            data: [
              { id: 'minimax-m2.7' },
              { id: 'deepseek-v4-flash-202605' },
              { id: 'kimi-k2.5' },
              { id: 'glm-5.1' },
            ],
          }),
      }),
    });
    expect(reported).toEqual([
      'tencenttokenplan/minimax-m2.7',
      'tencenttokenplan/deepseek-v4-flash-202605',
      'tencenttokenplan/kimi-k2.5',
      'tencenttokenplan/glm-5.1',
    ]);
  });

  it('reads the provider list when no handwritten model list is set', async () => {
    const report = await fetchRuntimeModelReport({
      env: {
        ACN_PROVIDER_API_KEY: 'catalog-key',
        ACN_PROVIDER_MODELS_URL: 'https://provider.example/v1/models',
        ACN_PROVIDER: 'tencenttokenplan',
      },
      fetchFn: vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            data: [{ id: 'glm-5' }, { id: 'glm-5.1' }],
          }),
      }),
    });
    expect(report.supportedModels).toEqual([
      'tencenttokenplan/glm-5',
      'tencenttokenplan/glm-5.1',
    ]);
    expect(report.providers).toEqual([
      {
        id: 'tencenttokenplan',
        models: ['tencenttokenplan/glm-5', 'tencenttokenplan/glm-5.1'],
        current: true,
      },
    ]);
  });

  it('reports every configured provider and marks the one in use', async () => {
    const report = await fetchRuntimeModelReport({
      env: {
        ACN_PROVIDER: 'tencenttokenplan',
        ACN_PROVIDER_MODELS_URL: 'https://tencent.example/models',
        ACN_PROVIDER_API_KEY: 't-key',
        ACN_PROVIDERS: JSON.stringify([
          {
            id: 'openrouter',
            models_url: 'https://openrouter.example/models',
            api_key: 'o-key',
          },
        ]),
      },
      fetchFn: vi.fn(async (url: string, init?: RequestInit) => {
        const href = String(url);
        const headers = (init?.headers || {}) as Record<string, string>;
        const auth = String(headers.Authorization || '');
        if (href.includes('tencent.example')) {
          expect(auth).toBe('Bearer t-key');
          return {
            ok: true,
            json: async () => ({ data: [{ id: 'glm-5' }] }),
          } as Response;
        }
        expect(auth).toBe('Bearer o-key');
        return {
          ok: true,
          json: async () => ({ data: [{ id: 'gpt-4o-mini' }] }),
        } as Response;
      }) as unknown as typeof fetch,
    });
    expect(report.providers).toEqual([
      { id: 'tencenttokenplan', models: ['tencenttokenplan/glm-5'], current: true },
      { id: 'openrouter', models: ['openrouter/gpt-4o-mini'], current: false },
    ]);
    expect(report.supportedModels).toEqual([
      'tencenttokenplan/glm-5',
      'openrouter/gpt-4o-mini',
    ]);
  });

  it('intercepts POST /acn/v1/runtime with the runtime marker', async () => {
    const prevHub = process.env.ACN_PROVIDER_API_KEY;
    const prevUrl = process.env.ACN_PROVIDER_MODELS_URL;
    const prevProvider = process.env.ACN_PROVIDER;
    process.env.ACN_PROVIDER = 'minimax';
    process.env.ACN_PROVIDER_MODELS_URL = 'https://provider.example/v1/models';
    process.env.ACN_PROVIDER_API_KEY = 'k';
    const fetchFn = vi.fn(async (url: string) => {
      if (String(url).includes('provider.example')) {
        return {
          ok: true,
          status: 200,
          json: () => Promise.resolve({ data: [{ id: 'minimax-m2.5' }] }),
        } as Response;
      }
      return {
        ok: true,
        status: 200,
        json: () => Promise.resolve({ status: 'ok' }),
      } as Response;
    });
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
      { fetchFn: fetchFn as unknown as typeof fetch }
    );
    expect(frames[0]).toMatchObject({ type: 'a2a_response', status: 200 });
    expect(fetchFn.mock.calls.some((c) => String(c[0]).includes('/heartbeat'))).toBe(
      true
    );
    if (prevHub === undefined) delete process.env.ACN_PROVIDER_API_KEY;
    else process.env.ACN_PROVIDER_API_KEY = prevHub;
    if (prevUrl === undefined) delete process.env.ACN_PROVIDER_MODELS_URL;
    else process.env.ACN_PROVIDER_MODELS_URL = prevUrl;
    if (prevProvider === undefined) delete process.env.ACN_PROVIDER;
    else process.env.ACN_PROVIDER = prevProvider;
  });

  it('rejects a model the provider list does not include', async () => {
    const prevHub = process.env.ACN_PROVIDER_API_KEY;
    const prevUrl = process.env.ACN_PROVIDER_MODELS_URL;
    const prevProvider = process.env.ACN_PROVIDER;
    process.env.ACN_PROVIDER = 'minimax';
    process.env.ACN_PROVIDER_MODELS_URL = 'https://provider.example/v1/models';
    process.env.ACN_PROVIDER_API_KEY = 'k';
    const fetchFn = vi.fn(async (_url: string) => ({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ data: [{ id: 'minimax-m2.5' }] }),
    }) as Response);
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
        supportedModels: ['openai/gpt-4o'],
      },
      (f) => frames.push(f),
      { fetchFn: fetchFn as unknown as typeof fetch }
    );
    expect(frames[0]).toMatchObject({
      status: 400,
      body: expect.stringContaining('unsupported_model'),
    });
    expect(fetchFn.mock.calls.some((c) => String(c[0]).includes('/heartbeat'))).toBe(false);
    if (prevHub === undefined) delete process.env.ACN_PROVIDER_API_KEY;
    else process.env.ACN_PROVIDER_API_KEY = prevHub;
    if (prevUrl === undefined) delete process.env.ACN_PROVIDER_MODELS_URL;
    else process.env.ACN_PROVIDER_MODELS_URL = prevUrl;
    if (prevProvider === undefined) delete process.env.ACN_PROVIDER;
    else process.env.ACN_PROVIDER = prevProvider;
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
