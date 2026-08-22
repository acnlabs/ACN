import { describe, expect, it, vi } from 'vitest';

import {
  buildChatWritebackOptions,
  clearAgentJwtCache,
  completeInferenceEnv,
  handleChatWriteback,
} from '../src/commands/chat-writeback.js';
import {
  canCompleteOfficialHop,
  startOfficialHopDoor,
} from '../src/commands/official-hop-door.js';
import {
  normalizeEvent,
  parseJsonRpcBody,
} from '../src/commands/normalize-event.js';

function mockOkResponse(body: string, status = 200) {
  return {
    status,
    headers: { get: (name: string) => (name === 'content-type' ? 'application/json' : null) },
    text: async () => body,
    arrayBuffer: async () => Buffer.from(body),
  };
}

function chatOfficialBody(): string {
  return JSON.stringify({
    jsonrpc: '2.0',
    id: 'rpc-chat',
    method: 'message/send',
    params: {
      message: {
        role: 'user',
        messageId: 'msg-chat-1',
        kind: 'message',
        parts: [{ kind: 'text', text: 'hello' }],
        metadata: {
          agentplanet: {
            chat_id: 'chat-uuid',
            message_id: 'user-msg-1',
            from_user: 'auth0|x',
            reply_channel: 'agentplanet.chat',
            reply_path: '/api/chats/chat-uuid/agent-messages',
            hop_id: 'hop:dialog:chat-uuid:user-msg-1:agent-1',
            inference_path: 'official',
            host_inference_url: 'https://api.agentplanet.org/api/inference/v1',
            requested_model: 'moonshotai/kimi-k2.5',
          },
        },
      },
    },
  });
}

function officialEvent() {
  const parsed = parseJsonRpcBody(chatOfficialBody());
  if (!parsed.ok) throw new Error('fixture');
  return normalizeEvent(parsed.body);
}

describe('canCompleteOfficialHop', () => {
  it('allows Host complete only when official + hop + allowlisted URL + JWT are present', () => {
    expect(
      canCompleteOfficialHop({
        inferencePath: 'official',
        hopId: 'hop:dialog:c:m:a',
        hostInferenceUrl: 'https://api.agentplanet.org/api/inference/v1',
        jwt: 'jwt',
      })
    ).toBe(true);
    expect(
      canCompleteOfficialHop({
        inferencePath: 'byo',
        hopId: 'hop:dialog:c:m:a',
        hostInferenceUrl: 'https://api.agentplanet.org/api/inference/v1',
        jwt: 'jwt',
      })
    ).toBe(false);
    expect(
      canCompleteOfficialHop({
        inferencePath: 'official',
        hopId: 'hop:dialog:c:m:a',
        hostInferenceUrl: 'https://evil.example/api/inference/v1',
        jwt: 'jwt',
      })
    ).toBe(false);
    expect(
      canCompleteOfficialHop({
        inferencePath: 'official',
        hopId: 'hop:dialog:c:m:a',
        hostInferenceUrl: 'https://api.agentplanet.org/api/inference/v1',
        jwt: '',
      })
    ).toBe(false);
  });
});

describe('startOfficialHopDoor', () => {
  it('returns null for a non-allowlisted Host URL', async () => {
    await expect(
      startOfficialHopDoor({
        hostInferenceUrl: 'https://evil.example/api/inference/v1',
        hopId: 'hop:1',
        agentId: 'agent-1',
        jwt: 'jwt',
      })
    ).resolves.toBeNull();
  });

  it('forwards POST /v1/chat/completions with hop headers and body hop_id', async () => {
    const fetchFn = vi.fn(async () =>
      mockOkResponse(JSON.stringify({ id: 'cmpl', choices: [] }))
    );
    const door = await startOfficialHopDoor({
      hostInferenceUrl: 'https://api.agentplanet.org/api/inference/v1',
      hopId: 'hop:dialog:c:m:agent-1',
      agentId: 'agent-1',
      jwt: 'jwt-official',
      fetchFn: fetchFn as unknown as typeof fetch,
    });
    expect(door).not.toBeNull();
    try {
      const res = await fetch(`${door!.baseUrl}/chat/completions`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          model: 'tencenttokenplan/kimi-k2.5',
          messages: [{ role: 'user', content: 'hi' }],
          agent_id: 'steal',
        }),
      });
      expect(res.status).toBe(200);
      expect(await res.json()).toEqual({ id: 'cmpl', choices: [] });
      expect(fetchFn).toHaveBeenCalledTimes(1);
      const call = fetchFn.mock.calls[0] as unknown as [
        string,
        RequestInit | undefined,
      ];
      expect(call[0]).toBe(
        'https://api.agentplanet.org/api/inference/v1/chat/completions'
      );
      expect(call[1]?.headers).toMatchObject({
        authorization: 'Bearer jwt-official',
        'X-Hop-Id': 'hop:dialog:c:m:agent-1',
        'X-Agent-Id': 'agent-1',
      });
      const forwarded = JSON.parse(String(call[1]?.body ?? '{}'));
      expect(forwarded.hop_id).toBe('hop:dialog:c:m:agent-1');
      expect(forwarded.agent_id).toBeUndefined();
      expect(forwarded.model).toBe('tencenttokenplan/kimi-k2.5');
    } finally {
      await door!.close();
    }
  });

  it('404s unknown paths', async () => {
    const door = await startOfficialHopDoor({
      hostInferenceUrl: 'http://127.0.0.1:9/api/inference/v1',
      hopId: 'hop:1',
      agentId: 'agent-1',
      jwt: 'jwt',
      fetchFn: (async () => mockOkResponse('{}')) as unknown as typeof fetch,
    });
    expect(door).not.toBeNull();
    try {
      const res = await fetch(`${door!.baseUrl}/models`);
      expect(res.status).toBe(404);
    } finally {
      await door!.close();
    }
  });
});

describe('official hop CLI complete', () => {
  it('injects OPENAI_* when a door URL is passed', () => {
    const event = officialEvent();
    const env = completeInferenceEnv(event, { agentId: 'agent-1' }, 'jwt-official', {
      baseUrl: 'http://127.0.0.1:9/v1',
    });
    expect(env.OPENAI_BASE_URL).toBe('http://127.0.0.1:9/v1');
    expect(env.OPENAI_API_KEY).toBe('jwt-official');
    expect(env.ACN_AGENT_JWT).toBe('jwt-official');
  });

  it('completes official hops via Host and does not spawn complete-exec', async () => {
    clearAgentJwtCache();
    const upstream: Array<{ url: string; body: Record<string, unknown> }> = [];
    const writebacks: Array<Record<string, unknown>> = [];
    const fetchFn = vi.fn(async (url: string | URL, init?: RequestInit) => {
      const u = String(url);
      if (u.includes('/oauth/token')) {
        return mockOkResponse(
          JSON.stringify({ access_token: 'jwt-door', expires_in: 1800 })
        );
      }
      if (u.includes('/chat/completions')) {
        upstream.push({
          url: u,
          body: JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>,
        });
        return mockOkResponse(
          JSON.stringify({
            id: 'cmpl',
            choices: [{ message: { role: 'assistant', content: 'official hi' } }],
          })
        );
      }
      writebacks.push(JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>);
      return mockOkResponse(JSON.stringify({ id: 'm1' }), 201);
    });

    const spawnFn = vi.fn();

    const result = await handleChatWriteback(
      officialEvent(),
      buildChatWritebackOptions({
        chatWriteback: true,
        chatApiBase: 'http://gw:8000',
        acnBaseUrl: 'https://api.acnlabs.dev',
        apiKey: 'acn_secret',
        chatCompleteExec: 'complete.sh',
        agentId: 'agent-1',
      })!,
      {
        fetchFn: fetchFn as unknown as typeof fetch,
        spawnFn: spawnFn as unknown as typeof import('child_process').spawn,
        logFn: () => {},
      }
    );
    expect(result).toEqual({ ok: true, httpStatus: 201 });
    expect(spawnFn).not.toHaveBeenCalled();
    expect(upstream).toEqual([
      {
        url: 'https://api.agentplanet.org/api/inference/v1/chat/completions',
        body: {
          model: 'moonshotai/kimi-k2.5',
          messages: [{ role: 'user', content: 'hello' }],
          hop_id: 'hop:dialog:chat-uuid:user-msg-1:agent-1',
        },
      },
    ]);
    expect(writebacks).toEqual([
      { content: 'official hi', reply_to_id: 'user-msg-1' },
    ]);
  });
});
