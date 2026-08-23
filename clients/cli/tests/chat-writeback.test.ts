/**
 * Tests for Chat Gateway writeback path on Mode B listen.
 */

import { describe, expect, it, vi } from 'vitest';

import {
  buildChatWritebackOptions,
  clearAgentJwtCache,
  completeInferenceEnv,
  DEFAULT_CHAT_JWT_AUDIENCE,
  extractChatCompletionContent,
  extractContent,
  extractModelId,
  extractUsage,
  handleChatWriteback,
  officialCompleteFailureContent,
  officialV0SupportsModel,
  validateChatWritebackOptions,
} from '../src/commands/chat-writeback.js';
import {
  dispatchLocalReceiverAndWaitWake,
} from '../src/commands/local-receiver.js';
import {
  asHostInferenceUrl,
  DedupeStore,
  dedupeKey,
  extractChatEnvelope,
  isAllowedChatReplyPath,
  normalizeEvent,
  parseJsonRpcBody,
} from '../src/commands/normalize-event.js';

function chatMessageBody(
  agentplanet?: Record<string, unknown> | null,
  messageExtra: Record<string, unknown> = {}
): string {
  const baseMeta = {
    chat_id: 'chat-uuid',
    message_id: 'user-msg-1',
    from_user: 'auth0|x',
    reply_channel: 'agentplanet.chat',
    reply_path: '/api/chats/chat-uuid/agent-messages',
  };
  const ap =
    agentplanet === null
      ? undefined
      : { ...baseMeta, ...(agentplanet ?? {}) };
  const message = {
    role: 'user',
    messageId: 'msg-chat-1',
    kind: 'message',
    parts: [{ kind: 'text', text: 'hello from interfaze' }],
    metadata: ap ? { agentplanet: ap } : {},
    ...messageExtra,
  };
  return JSON.stringify({
    jsonrpc: '2.0',
    id: 'rpc-chat',
    method: 'message/send',
    params: { message },
  });
}

function mockOkResponse(body: string, status = 200) {
  return {
    status,
    headers: { get: () => null },
    text: async () => body,
    arrayBuffer: async () => new ArrayBuffer(0),
  };
}

describe('isAllowedChatReplyPath', () => {
  it('allows exact Gateway path only', () => {
    expect(
      isAllowedChatReplyPath('chat-uuid', '/api/chats/chat-uuid/agent-messages')
    ).toBe(true);
    expect(
      isAllowedChatReplyPath('chat-uuid', '/api/chats/other/agent-messages')
    ).toBe(false);
    expect(
      isAllowedChatReplyPath('chat-uuid', '/api/chats/chat-uuid/../admin')
    ).toBe(false);
    expect(
      isAllowedChatReplyPath('chat-uuid', '//evil.com/steal')
    ).toBe(false);
    expect(
      isAllowedChatReplyPath(
        'chat-uuid',
        '/api/chats/chat-uuid/agent-messages?x=1'
      )
    ).toBe(false);
  });
});

describe('extractChatEnvelope / normalizeEvent.chat', () => {
  it('extracts chat envelope from metadata.agentplanet', () => {
    const parsed = parseJsonRpcBody(chatMessageBody());
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    const event = normalizeEvent(parsed.body);
    expect(event.chat).toEqual({
      chat_id: 'chat-uuid',
      reply_path: '/api/chats/chat-uuid/agent-messages',
      reply_channel: 'agentplanet.chat',
      gateway_message_id: 'user-msg-1',
      user_text: 'hello from interfaze',
      requested_model: null,
      max_output_tokens: null,
      hop_id: null,
      inference_path: null,
      host_inference_url: null,
    });
    const params = parsed.body.params as { message: Record<string, unknown> };
    expect(extractChatEnvelope(params.message)?.chat_id).toBe('chat-uuid');
  });

  it('extracts requested_model and max_output_tokens for runtime wake', () => {
    const parsed = parseJsonRpcBody(
      chatMessageBody({
        requested_model: 'openai/gpt-4o-mini',
        max_output_tokens: 2048,
      })
    );
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    expect(normalizeEvent(parsed.body).chat).toMatchObject({
      requested_model: 'openai/gpt-4o-mini',
      max_output_tokens: 2048,
    });
  });

  it('extracts official hop wake fields and rejects arbitrary host URLs', () => {
    expect(asHostInferenceUrl('https://evil.example/api/inference/v1')).toBeNull();
    expect(asHostInferenceUrl('https://evil.example/steal')).toBeNull();
    expect(asHostInferenceUrl('http://127.0.0.1:8000/api/inference/v1')).toBe(
      'http://127.0.0.1:8000/api/inference/v1'
    );
    expect(asHostInferenceUrl('http://evil.example/api/inference/v1')).toBeNull();

    const parsed = parseJsonRpcBody(
      chatMessageBody({
        hop_id: 'hop:dialog:chat-uuid:user-msg-1:agent-1',
        inference_path: 'official',
        host_inference_url: 'https://api.agentplanet.org/api/inference/v1',
      })
    );
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    expect(normalizeEvent(parsed.body).chat).toMatchObject({
      hop_id: 'hop:dialog:chat-uuid:user-msg-1:agent-1',
      inference_path: 'official',
      host_inference_url: 'https://api.agentplanet.org/api/inference/v1',
    });
  });

  it('returns null when chat_id or reply_path missing', () => {
    const parsed = parseJsonRpcBody(
      chatMessageBody({ chat_id: 'only-id', reply_path: undefined as unknown as string })
    );
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    const body = parsed.body;
    const params = body.params as {
      message: { metadata: { agentplanet: Record<string, unknown> } };
    };
    delete params.message.metadata.agentplanet.reply_path;
    expect(normalizeEvent(body).chat).toBeNull();
  });

  it('returns null when reply_channel is wrong', () => {
    const parsed = parseJsonRpcBody(
      chatMessageBody({ reply_channel: 'other.channel' })
    );
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    expect(normalizeEvent(parsed.body).chat).toBeNull();
  });

  it('returns null when reply_path is not allowlisted', () => {
    const parsed = parseJsonRpcBody(
      chatMessageBody({ reply_path: '/api/internal/admin' })
    );
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    expect(normalizeEvent(parsed.body).chat).toBeNull();
  });

  it('dedupeKey prefers gateway message id for chat', () => {
    const parsed = parseJsonRpcBody(chatMessageBody());
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    const event = normalizeEvent(parsed.body);
    expect(dedupeKey(event)).toBe('chat:chat-uuid:user-msg-1');
  });
});

describe('official v0 model support', () => {
  it('rejects thinking and reasoning SKUs', () => {
    expect(officialV0SupportsModel('moonshotai/kimi-k2.5')).toBe(true);
    expect(officialV0SupportsModel('amazon/nova-lite-v1')).toBe(true);
    expect(officialV0SupportsModel('allenai/olmo-3-32b-think')).toBe(false);
    expect(officialV0SupportsModel('openai/o1-reasoning')).toBe(false);
    expect(officialCompleteFailureContent('official_complete_unsupported_model', 'allenai/olmo-3-32b-think')).toContain(
      'thinking/reasoning'
    );
  });
});

describe('extractContent', () => {
  it('reads content/reply/text only', () => {
    expect(extractChatCompletionContent({
      choices: [{ message: { content: 'official hi' } }],
    })).toBe('official hi');
    expect(extractChatCompletionContent({ content: 'plain' })).toBe('plain');
    expect(extractContent({ content: 'hi' })).toBe('hi');
    expect(extractContent({ reply: 'r' })).toBe('r');
    expect(extractContent({ text: 't' })).toBe('t');
    expect(extractContent({ message: 'should-ignore' })).toBeNull();
    expect(extractContent({ content: 'accepted' })).toBeNull();
  });
});

describe('extractUsage', () => {
  it('reads nested usage and OpenAI-style aliases', () => {
    expect(
      extractUsage({ usage: { input_tokens: 1, output_tokens: 2 } })
    ).toEqual({ input_tokens: 1, output_tokens: 2 });
    expect(
      extractUsage({
        usage: { prompt_tokens: 3, completion_tokens: 4, meter_source: 'peer_self' },
      })
    ).toEqual({
      input_tokens: 3,
      output_tokens: 4,
      meter_source: 'peer_self',
    });
    expect(
      extractUsage({
        usage: { input_tokens: 1, output_tokens: 0, model_id: 'openai/gpt-4o-mini' },
      })
    ).toEqual({
      input_tokens: 1,
      output_tokens: 0,
      model_id: 'openai/gpt-4o-mini',
    });
    expect(
      extractUsage({
        content: 'hi',
        model: 'anthropic/claude-sonnet-4',
        usage: { input_tokens: 2, output_tokens: 1 },
      })
    ).toEqual({
      input_tokens: 2,
      output_tokens: 1,
      model_id: 'anthropic/claude-sonnet-4',
    });
    // model_id alone must not invent zero-token usage
    expect(extractUsage({ content: 'hi', model_id: 'openai/gpt-4o-mini' })).toBeUndefined();
    expect(extractModelId({ content: 'hi', model_id: 'openai/gpt-4o-mini' })).toBe(
      'openai/gpt-4o-mini'
    );
    expect(extractUsage({ content: 'hi' })).toBeUndefined();
    expect(
      extractUsage({
        usage: {
          input: 10,
          output: 2,
          reasoningTokens: 3,
          cacheRead: 4,
          cacheWrite: 1,
          total: 16,
          duration_ms: 800,
          provider: 'tencenttokenplan',
          model_id: 'tencenttokenplan/kimi-k2.5',
        },
      })
    ).toEqual({
      input_tokens: 10,
      output_tokens: 2,
      model_id: 'tencenttokenplan/kimi-k2.5',
      reasoning_tokens: 3,
      cache_read_tokens: 4,
      cache_write_tokens: 1,
      total_tokens: 16,
      duration_ms: 800,
      provider: 'tencenttokenplan',
    });
  });
});

describe('validateChatWritebackOptions', () => {
  it('allows disabled', () => {
    expect(validateChatWritebackOptions({})).toBeNull();
  });

  it('requires base and api key; complete is optional and mutually exclusive', () => {
    expect(
      validateChatWritebackOptions({
        chatWriteback: true,
        agentId: 'a1',
        apiKey: 'acn_key',
        chatApiBase: 'http://gw',
      })
    ).toBeNull();

    expect(
      validateChatWritebackOptions({
        chatWriteback: true,
        agentId: 'a1',
        apiKey: 'acn_key',
        chatApiBase: 'http://gw',
        chatCompleteUrl: 'http://host/complete',
        chatCompleteExec: 'echo',
      })
    ).toMatch(/mutually exclusive/);

    expect(
      validateChatWritebackOptions({
        chatWriteback: true,
        agentId: 'a1',
        apiKey: 'acn_key',
        chatApiBase: 'http://gw',
        chatCompleteUrl: 'http://host/complete',
      })
    ).toBeNull();

    expect(
      validateChatWritebackOptions({
        chatWriteback: true,
        agentId: 'a1',
        chatApiBase: 'http://gw',
        chatCompleteUrl: 'http://host/complete',
      })
    ).toMatch(/API key/);
  });
});

describe('handleChatWriteback', () => {
  it('completes via HTTP then POSTs agent-messages with ACN JWT', async () => {
    clearAgentJwtCache();
    const calls: Array<{ url: string; body: string; headers: unknown }> = [];
    const fetchFn = vi.fn(async (url: string | URL, init?: RequestInit) => {
      const u = String(url);
      calls.push({
        url: u,
        body: String(init?.body ?? ''),
        headers: init?.headers,
      });
      if (u.includes('/complete')) {
        return mockOkResponse(JSON.stringify({ content: 'agent says hi' }));
      }
      if (u.includes('/oauth/token')) {
        return mockOkResponse(
          JSON.stringify({ access_token: 'jwt-from-acn', expires_in: 1800 })
        );
      }
      return mockOkResponse(JSON.stringify({ id: 'm1' }), 201);
    });

    const event = normalizeEvent(
      (parseJsonRpcBody(chatMessageBody()) as { ok: true; body: Record<string, unknown> })
        .body
    );
    const opts = buildChatWritebackOptions({
      chatWriteback: true,
      chatApiBase: 'http://gw:8000',
      acnBaseUrl: 'https://api.acnlabs.dev',
      apiKey: 'acn_secret',
      chatCompleteUrl: 'http://127.0.0.1:9/complete',
      agentId: 'agent-1',
    })!;

    const result = await handleChatWriteback(event, opts, {
      fetchFn: fetchFn as unknown as typeof fetch,
      logFn: () => {},
    });
    expect(result).toEqual({ ok: true, httpStatus: 201 });
    expect(calls).toHaveLength(3);
    expect(calls[0].url).toContain('/complete');
    expect(JSON.parse(calls[0].body).chat.chat_id).toBe('chat-uuid');
    expect(calls[0].headers).toMatchObject({ 'X-ACN-Agent-Id': 'agent-1' });
    expect(calls[1].url).toBe('https://api.acnlabs.dev/oauth/token');
    expect(JSON.parse(calls[1].body)).toMatchObject({
      grant_type: 'client_credentials',
      client_id: 'agent-1',
      client_secret: 'acn_secret',
      audience: DEFAULT_CHAT_JWT_AUDIENCE,
    });
    expect(calls[2].url).toBe(
      'http://gw:8000/api/chats/chat-uuid/agent-messages'
    );
    expect(JSON.parse(calls[2].body)).toEqual({
      content: 'agent says hi',
      reply_to_id: 'user-msg-1',
    });
    const hdrs = calls[2].headers as Record<string, string>;
    expect(hdrs['Authorization']).toBe('Bearer jwt-from-acn');
    expect(hdrs['X-Internal-Token']).toBeUndefined();
  });

  it('injects official hop env for complete-exec and headers for complete-url', async () => {
    clearAgentJwtCache();
    const event = normalizeEvent(
      (
        parseJsonRpcBody(
          chatMessageBody({
            hop_id: 'hop:dialog:chat-uuid:user-msg-1:agent-1',
            inference_path: 'official',
            host_inference_url: 'https://api.agentplanet.org/api/inference/v1',
            requested_model: 'moonshotai/kimi-k2.5',
            max_output_tokens: 2048,
          })
        ) as { ok: true; body: Record<string, unknown> }
      ).body
    );
    const opts = buildChatWritebackOptions({
      chatWriteback: true,
      chatApiBase: 'http://gw:8000',
      acnBaseUrl: 'https://api.acnlabs.dev',
      apiKey: 'acn_secret',
      chatCompleteUrl: 'http://127.0.0.1:9/complete',
      agentId: 'agent-1',
    })!;
    const env = completeInferenceEnv(event, opts, 'jwt-official');
    expect(env.ACN_AGENT_ID).toBe('agent-1');
    expect(env.ACN_CHAT_HOP_ID).toBe('hop:dialog:chat-uuid:user-msg-1:agent-1');
    expect(env.ACN_INFERENCE_PATH).toBe('official');
    expect(env.ACN_HOST_INFERENCE_URL).toBe(
      'https://api.agentplanet.org/api/inference/v1'
    );
    expect(env.ACN_AGENT_JWT).toBe('jwt-official');

    const fetchFn = vi.fn(async (url: string | URL, init?: RequestInit) => {
      const u = String(url);
      if (u.includes('/oauth/token')) {
        return mockOkResponse(
          JSON.stringify({ access_token: 'jwt-from-acn', expires_in: 1800 })
        );
      }
      if (u.includes('/chat/completions')) {
        return mockOkResponse(
          JSON.stringify({
            choices: [{ message: { content: 'official reply' } }],
          })
        );
      }
      return mockOkResponse(JSON.stringify({ id: 'm1' }), 201);
    });
    const result = await handleChatWriteback(event, opts, {
      fetchFn: fetchFn as unknown as typeof fetch,
      logFn: () => {},
    });
    expect(result).toEqual({ ok: true, httpStatus: 201 });
    const completeCall = fetchFn.mock.calls.find((c) =>
      String(c[0]).includes('/chat/completions')
    );
    expect(completeCall?.[0]).toBe(
      'https://api.agentplanet.org/api/inference/v1/chat/completions'
    );
    expect(completeCall?.[1]?.headers).toMatchObject({
      authorization: 'Bearer jwt-from-acn',
      'X-Hop-Id': 'hop:dialog:chat-uuid:user-msg-1:agent-1',
      'X-Agent-Id': 'agent-1',
    });
    expect(JSON.parse(String(completeCall?.[1]?.body ?? '{}'))).toEqual({
      model: 'moonshotai/kimi-k2.5',
      messages: [{ role: 'user', content: 'hello from interfaze' }],
      hop_id: 'hop:dialog:chat-uuid:user-msg-1:agent-1',
      max_tokens: 2048,
    });
    expect(
      fetchFn.mock.calls.some((c) => String(c[0]).includes('/complete'))
    ).toBe(false);
  });

  it('completes official hops without --chat-complete-*', async () => {
    clearAgentJwtCache();
    const event = normalizeEvent(
      (
        parseJsonRpcBody(
          chatMessageBody({
            hop_id: 'hop:dialog:chat-uuid:user-msg-1:agent-1',
            inference_path: 'official',
            host_inference_url: 'https://api.agentplanet.org/api/inference/v1',
            requested_model: 'moonshotai/kimi-k2.5',
          })
        ) as { ok: true; body: Record<string, unknown> }
      ).body
    );
    const opts = buildChatWritebackOptions({
      chatWriteback: true,
      chatApiBase: 'http://gw:8000',
      acnBaseUrl: 'https://api.acnlabs.dev',
      apiKey: 'acn_secret',
      agentId: 'agent-1',
    })!;
    const fetchFn = vi.fn(async (url: string | URL) => {
      const u = String(url);
      if (u.includes('/oauth/token')) {
        return mockOkResponse(
          JSON.stringify({ access_token: 'jwt-from-acn', expires_in: 1800 })
        );
      }
      if (u.includes('/chat/completions')) {
        return mockOkResponse(
          JSON.stringify({
            choices: [{ message: { content: 'official only' } }],
          })
        );
      }
      return mockOkResponse(JSON.stringify({ id: 'm1' }), 201);
    });
    const result = await handleChatWriteback(event, opts, {
      fetchFn: fetchFn as unknown as typeof fetch,
      logFn: () => {},
    });
    expect(result).toEqual({ ok: true, httpStatus: 201 });
  });

  it('skips thinking models on official v0 and still writebacks', async () => {
    clearAgentJwtCache();
    const event = normalizeEvent(
      (
        parseJsonRpcBody(
          chatMessageBody({
            hop_id: 'hop:dialog:chat-uuid:user-msg-1:agent-1',
            inference_path: 'official',
            host_inference_url: 'https://api.agentplanet.org/api/inference/v1',
            requested_model: 'allenai/olmo-3-32b-think',
          })
        ) as { ok: true; body: Record<string, unknown> }
      ).body
    );
    const opts = buildChatWritebackOptions({
      chatWriteback: true,
      chatApiBase: 'http://gw:8000',
      acnBaseUrl: 'https://api.acnlabs.dev',
      apiKey: 'acn_secret',
      agentId: 'agent-1',
    })!;
    const fetchFn = vi.fn(async (url: string | URL, init?: RequestInit) => {
      const u = String(url);
      if (u.includes('/oauth/token')) {
        return mockOkResponse(
          JSON.stringify({ access_token: 'jwt-from-acn', expires_in: 1800 })
        );
      }
      if (u.includes('/chat/completions')) {
        return mockOkResponse(JSON.stringify({ error: 'should not call host' }), 500);
      }
      return mockOkResponse(JSON.stringify({ id: 'm1' }), 201);
    });
    const result = await handleChatWriteback(event, opts, {
      fetchFn: fetchFn as unknown as typeof fetch,
      logFn: () => {},
    });
    expect(result).toEqual({ ok: true, httpStatus: 201 });
    expect(
      fetchFn.mock.calls.some((c) => String(c[0]).includes('/chat/completions'))
    ).toBe(false);
    const writeback = fetchFn.mock.calls.find((c) =>
      String(c[0]).includes('/agent-messages')
    );
    expect(JSON.parse(String(writeback?.[1]?.body ?? '{}')).content).toContain(
      'thinking/reasoning'
    );
  });

  it('writebacks an error when official Host complete fails', async () => {
    clearAgentJwtCache();
    const event = normalizeEvent(
      (
        parseJsonRpcBody(
          chatMessageBody({
            hop_id: 'hop:dialog:chat-uuid:user-msg-1:agent-1',
            inference_path: 'official',
            host_inference_url: 'https://api.agentplanet.org/api/inference/v1',
            requested_model: 'moonshotai/kimi-k2.5',
          })
        ) as { ok: true; body: Record<string, unknown> }
      ).body
    );
    const opts = buildChatWritebackOptions({
      chatWriteback: true,
      chatApiBase: 'http://gw:8000',
      acnBaseUrl: 'https://api.acnlabs.dev',
      apiKey: 'acn_secret',
      agentId: 'agent-1',
    })!;
    const fetchFn = vi.fn(async (url: string | URL) => {
      const u = String(url);
      if (u.includes('/oauth/token')) {
        return mockOkResponse(
          JSON.stringify({ access_token: 'jwt-from-acn', expires_in: 1800 })
        );
      }
      if (u.includes('/chat/completions')) {
        return mockOkResponse(JSON.stringify({ error: 'upstream down' }), 503);
      }
      return mockOkResponse(JSON.stringify({ id: 'm1' }), 201);
    });
    const result = await handleChatWriteback(event, opts, {
      fetchFn: fetchFn as unknown as typeof fetch,
      logFn: () => {},
    });
    expect(result).toEqual({ ok: true, httpStatus: 201 });
    const writeback = fetchFn.mock.calls.find((c) =>
      String(c[0]).includes('/agent-messages')
    );
    expect(JSON.parse(String(writeback?.[1]?.body ?? '{}')).content).toContain(
      'Official hop failed'
    );
  });

  it('fails BYO hops when no complete source is configured', async () => {
    const event = normalizeEvent(
      (parseJsonRpcBody(chatMessageBody()) as { ok: true; body: Record<string, unknown> })
        .body
    );
    const opts = buildChatWritebackOptions({
      chatWriteback: true,
      chatApiBase: 'http://gw:8000',
      acnBaseUrl: 'https://api.acnlabs.dev',
      apiKey: 'acn_secret',
      agentId: 'agent-1',
    })!;
    const result = await handleChatWriteback(event, opts, { logFn: () => {} });
    expect(result).toEqual({ ok: false, reason: 'byo_complete_missing' });
  });

  it('forwards host usage + reply_to_id for billing settle', async () => {
    clearAgentJwtCache();
    const calls: Array<{ url: string; body: string }> = [];
    const fetchFn = vi.fn(async (url: string | URL, init?: RequestInit) => {
      const u = String(url);
      calls.push({ url: u, body: String(init?.body ?? '') });
      if (u.includes('/complete')) {
        return mockOkResponse(
          JSON.stringify({
            content: 'billed reply',
            usage: {
              input_tokens: 12,
              output_tokens: 34,
              reasoningTokens: 3,
              cacheRead: 4,
              cacheWrite: 1,
              total: 50,
              durationMs: 800,
              provider: 'tencenttokenplan',
              model_id: 'tencenttokenplan/kimi-k2.5',
            },
          })
        );
      }
      if (u.includes('/oauth/token')) {
        return mockOkResponse(
          JSON.stringify({ access_token: 'jwt-usage', expires_in: 1800 })
        );
      }
      return mockOkResponse(JSON.stringify({ id: 'm1' }), 201);
    });

    const event = normalizeEvent(
      (parseJsonRpcBody(chatMessageBody()) as { ok: true; body: Record<string, unknown> })
        .body
    );
    const opts = buildChatWritebackOptions({
      chatWriteback: true,
      chatApiBase: 'http://gw:8000',
      acnBaseUrl: 'https://api.acnlabs.dev',
      apiKey: 'acn_secret',
      chatCompleteUrl: 'http://127.0.0.1:9/complete',
      agentId: 'agent-1',
    })!;

    const result = await handleChatWriteback(event, opts, {
      fetchFn: fetchFn as unknown as typeof fetch,
      logFn: () => {},
    });
    expect(result).toEqual({ ok: true, httpStatus: 201 });
    const writeback = JSON.parse(calls[2].body);
    expect(writeback).toEqual({
      content: 'billed reply',
      reply_to_id: 'user-msg-1',
      usage: {
        input_tokens: 12,
        output_tokens: 34,
        meter_source: 'peer_self',
        model_id: 'tencenttokenplan/kimi-k2.5',
        reasoning_tokens: 3,
        cache_read_tokens: 4,
        cache_write_tokens: 1,
        total_tokens: 50,
        duration_ms: 800,
        provider: 'tencenttokenplan',
      },
    });
    expect(event.chat?.gateway_message_id).toBe('user-msg-1');
  });

  it('defaults JWT audience to AgentPlanet canonical, not chat-api-base origin', () => {
    const opts = buildChatWritebackOptions({
      chatWriteback: true,
      chatApiBase: 'http://127.0.0.1:8000',
      acnBaseUrl: 'https://api.acnlabs.dev',
      apiKey: 'acn_key',
      chatCompleteUrl: 'http://host/c',
      agentId: 'a',
    })!;
    expect(opts.audience).toBe(DEFAULT_CHAT_JWT_AUDIENCE);
    expect(opts.audience).not.toBe('http://127.0.0.1:8000');
  });

  it('remints JWT once after Gateway 401', async () => {
    clearAgentJwtCache();
    let oauthCalls = 0;
    let writeCalls = 0;
    const fetchFn = vi.fn(async (url: string | URL) => {
      const u = String(url);
      if (u.includes('/complete')) {
        return mockOkResponse(JSON.stringify({ content: 'retry me' }));
      }
      if (u.includes('/oauth/token')) {
        oauthCalls += 1;
        return mockOkResponse(
          JSON.stringify({
            access_token: `jwt-${oauthCalls}`,
            expires_in: 1800,
          })
        );
      }
      writeCalls += 1;
      if (writeCalls === 1) {
        return mockOkResponse('{"detail":"invalid"}', 401);
      }
      return mockOkResponse(JSON.stringify({ id: 'm1' }), 201);
    });

    const event = normalizeEvent(
      (parseJsonRpcBody(chatMessageBody()) as { ok: true; body: Record<string, unknown> })
        .body
    );
    const result = await handleChatWriteback(
      event,
      buildChatWritebackOptions({
        chatWriteback: true,
        chatApiBase: 'http://gw:8000',
        acnBaseUrl: 'https://api.acnlabs.dev',
        apiKey: 'acn_secret',
        chatCompleteUrl: 'http://127.0.0.1:9/complete',
        agentId: 'agent-1',
      })!,
      { fetchFn: fetchFn as unknown as typeof fetch, logFn: () => {} }
    );
    expect(result).toEqual({ ok: true, httpStatus: 201 });
    expect(oauthCalls).toBe(2);
    expect(writeCalls).toBe(2);
  });

  it('fails when complete JSON lacks content', async () => {
    const fetchFn = vi.fn(async () => mockOkResponse('{}'));
    const event = normalizeEvent(
      (parseJsonRpcBody(chatMessageBody()) as { ok: true; body: Record<string, unknown> })
        .body
    );
    const result = await handleChatWriteback(
      event,
      buildChatWritebackOptions({
        chatWriteback: true,
        chatApiBase: 'http://gw',
        acnBaseUrl: 'https://api.acnlabs.dev',
        apiKey: 'acn_key',
        chatCompleteUrl: 'http://host/c',
        agentId: 'a',
      })!,
      { fetchFn: fetchFn as unknown as typeof fetch, logFn: () => {} }
    );
    expect(result).toEqual({ ok: false, reason: 'complete_missing_content' });
  });

  it('rejects writeback when envelope reply_path was tampered after normalize', async () => {
    const fetchFn = vi.fn(async (url: string | URL) => {
      if (String(url).includes('/complete')) {
        return mockOkResponse(JSON.stringify({ content: 'x' }));
      }
      return mockOkResponse('{}', 201);
    });
    const event = normalizeEvent(
      (parseJsonRpcBody(chatMessageBody()) as { ok: true; body: Record<string, unknown> })
        .body
    );
    event.chat!.reply_path = '/api/internal/admin';
    const result = await handleChatWriteback(
      event,
      buildChatWritebackOptions({
        chatWriteback: true,
        chatApiBase: 'http://gw',
        acnBaseUrl: 'https://api.acnlabs.dev',
        apiKey: 'acn_key',
        chatCompleteUrl: 'http://host/complete',
        agentId: 'a',
      })!,
      { fetchFn: fetchFn as unknown as typeof fetch, logFn: () => {} }
    );
    expect(result).toEqual({ ok: false, reason: 'reply_path_rejected' });
    expect(fetchFn.mock.calls.every((c) => !String(c[0]).includes('/admin'))).toBe(
      true
    );
  });
});

describe('dispatchLocalReceiver chat vs task wake', () => {
  it('routes chat envelope to writeback, not wake-url', async () => {
    clearAgentJwtCache();
    const wakeFetch = vi.fn(async () => mockOkResponse('ok'));
    const completeFetch = vi.fn(async (url: string | URL) => {
      const u = String(url);
      if (u.includes('wake')) {
        return mockOkResponse('should-not', 500);
      }
      if (u.includes('complete')) {
        return mockOkResponse(JSON.stringify({ content: 'done' }));
      }
      if (u.includes('/oauth/token')) {
        return mockOkResponse(
          JSON.stringify({ access_token: 'jwt-from-acn', expires_in: 1800 })
        );
      }
      return mockOkResponse('{}', 201);
    });

    const frames: unknown[] = [];
    const store = new DedupeStore(3600);
    const out = await dispatchLocalReceiverAndWaitWake(
      'corr-c',
      chatMessageBody(),
      {
        runtime: 'http',
        wakeUrl: 'http://host/wake',
        dedupe: true,
        dedupeTtlSec: 3600,
        chatWriteback: buildChatWritebackOptions({
          chatWriteback: true,
          chatApiBase: 'http://gw',
          acnBaseUrl: 'https://api.acnlabs.dev',
          apiKey: 'acn_key',
          chatCompleteUrl: 'http://host/complete',
          agentId: 'agent-1',
        }),
      },
      store,
      (f) => frames.push(f),
      {
        fetchFn: completeFetch as unknown as typeof fetch,
        logFn: () => {},
      }
    );

    expect(out.woke).toBe(true);
    expect(wakeFetch).not.toHaveBeenCalled();
    expect(completeFetch.mock.calls.some((c) => String(c[0]).includes('wake'))).toBe(
      false
    );
    expect(
      completeFetch.mock.calls.some((c) => String(c[0]).includes('complete'))
    ).toBe(true);
    expect(frames[0]).toMatchObject({ type: 'a2a_response', status: 200 });
  });

  it('falls through to task wake when forged chat channel is rejected', async () => {
    const body = chatMessageBody(
      { reply_channel: 'forged' },
      { metadata: { task_id: 'task-9', agentplanet: {
        chat_id: 'chat-uuid',
        message_id: 'user-msg-1',
        reply_channel: 'forged',
        reply_path: '/api/chats/chat-uuid/agent-messages',
      } } }
    );
    const fetchFn = vi.fn().mockResolvedValue(mockOkResponse('', 204));
    const store = new DedupeStore(3600);
    const out = await dispatchLocalReceiverAndWaitWake(
      'corr-forge',
      body,
      {
        runtime: 'http',
        wakeUrl: 'http://host/wake',
        dedupe: true,
        dedupeTtlSec: 3600,
        chatWriteback: buildChatWritebackOptions({
          chatWriteback: true,
          chatApiBase: 'http://gw',
          acnBaseUrl: 'https://api.acnlabs.dev',
          apiKey: 'acn_key',
          chatCompleteUrl: 'http://host/complete',
          agentId: 'agent-1',
        }),
      },
      store,
      () => {},
      { fetchFn: fetchFn as unknown as typeof fetch, logFn: () => {} }
    );
    expect(out.woke).toBe(true);
    expect(String(fetchFn.mock.calls[0][0])).toContain('/wake');
  });

  it('keeps task wake when no chat envelope', async () => {
    const body = JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'message/send',
      params: {
        message: {
          role: 'user',
          messageId: 'm-task',
          parts: [{ kind: 'text', text: 'do task' }],
          metadata: { task_id: 'task-9' },
        },
      },
    });
    const fetchFn = vi.fn().mockResolvedValue(mockOkResponse('', 204));
    const store = new DedupeStore(3600);
    const out = await dispatchLocalReceiverAndWaitWake(
      'corr-t',
      body,
      {
        runtime: 'http',
        wakeUrl: 'http://host/wake',
        dedupe: true,
        dedupeTtlSec: 3600,
        chatWriteback: buildChatWritebackOptions({
          chatWriteback: true,
          chatApiBase: 'http://gw',
          acnBaseUrl: 'https://api.acnlabs.dev',
          apiKey: 'acn_key',
          chatCompleteUrl: 'http://host/complete',
          agentId: 'agent-1',
        }),
      },
      store,
      () => {},
      { fetchFn: fetchFn as unknown as typeof fetch, logFn: () => {} }
    );
    expect(out.woke).toBe(true);
    expect(String(fetchFn.mock.calls[0][0])).toContain('/wake');
  });
});
