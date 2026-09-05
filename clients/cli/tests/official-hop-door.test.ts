import { EventEmitter } from 'node:events';

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

  it('pipes SSE and does not buffer with arrayBuffer', async () => {
    const arrayBuffer = vi.fn(async () => {
      throw new Error('stream must not call arrayBuffer');
    });
    const encoder = new TextEncoder();
    const chunks = [
      'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
      'data: [DONE]\n\n',
    ];
    let i = 0;
    const body = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (i < chunks.length) {
          controller.enqueue(encoder.encode(chunks[i++]));
        } else {
          controller.close();
        }
      },
    });
    const fetchFn = vi.fn(async () => ({
      status: 200,
      headers: {
        get: (name: string) =>
          name.toLowerCase() === 'content-type' ? 'text/event-stream' : null,
      },
      body,
      arrayBuffer,
    }));
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
          stream: true,
          messages: [{ role: 'user', content: 'hi' }],
        }),
      });
      expect(res.status).toBe(200);
      expect(res.headers.get('content-type')).toContain('text/event-stream');
      const text = await res.text();
      expect(text).toContain('data:');
      expect(text).toContain('[DONE]');
      expect(arrayBuffer).not.toHaveBeenCalled();
    } finally {
      await door!.close();
    }
  });

  it('keeps JSON errors when stream is requested', async () => {
    const arrayBuffer = vi.fn(async () =>
      Buffer.from(JSON.stringify({ error: 'official_model_unsupported' }))
    );
    const fetchFn = vi.fn(async () => ({
      status: 400,
      headers: {
        get: (name: string) =>
          name.toLowerCase() === 'content-type' ? 'application/json' : null,
      },
      body: new ReadableStream<Uint8Array>(),
      arrayBuffer,
    }));
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
          model: 'allenai/olmo-3-32b-think',
          stream: true,
          messages: [{ role: 'user', content: 'hi' }],
        }),
      });
      expect(res.status).toBe(400);
      expect(res.headers.get('content-type')).toContain('application/json');
      expect(await res.json()).toEqual({ error: 'official_model_unsupported' });
      expect(arrayBuffer).toHaveBeenCalled();
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
    expect(env.ACN_REQUESTED_MODEL).toBe('moonshotai/kimi-k2.5');
  });

  it('official + complete-exec opens a door, spawns exec, and requires a Host invoice', async () => {
    clearAgentJwtCache();
    const writebacks: Array<Record<string, unknown>> = [];
    let spawnedEnv: NodeJS.ProcessEnv | undefined;
    const fetchFn = vi.fn(async (url: string | URL, init?: RequestInit) => {
      const u = String(url);
      if (u.includes('/oauth/token')) {
        return mockOkResponse(
          JSON.stringify({ access_token: 'jwt-door', expires_in: 1800 })
        );
      }
      if (u.includes('/hops/')) {
        return mockOkResponse(JSON.stringify({ seen: true, call_count: 1 }));
      }
      if (u.includes('/chat/completions')) {
        return mockOkResponse(
          JSON.stringify({ error: 'cli must not complete official hops itself' }),
          500
        );
      }
      writebacks.push(JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>);
      return mockOkResponse(JSON.stringify({ id: 'm1' }), 201);
    });

    const spawnFn = vi.fn((_cmd: string, opts?: { env?: NodeJS.ProcessEnv }) => {
      spawnedEnv = opts?.env;
      const child = new EventEmitter() as EventEmitter & {
        stdout: EventEmitter;
        stderr: EventEmitter;
        stdin: { end: () => void };
        kill: () => void;
      };
      child.stdout = new EventEmitter();
      child.stderr = new EventEmitter();
      child.kill = () => {};
      child.stdin = {
        end: () => {
          queueMicrotask(() => {
            child.stdout.emit('data', Buffer.from('{"content":"from agent"}'));
            child.emit('close', 0);
          });
        },
      };
      return child;
    });

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
    expect(spawnFn).toHaveBeenCalledTimes(1);
    expect(spawnedEnv?.OPENAI_BASE_URL).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/v1$/);
    expect(spawnedEnv?.OPENAI_API_KEY).toBe('jwt-door');
    expect(spawnedEnv?.ACN_INFERENCE_PATH).toBe('official');
    expect(
      fetchFn.mock.calls.some((c) => String(c[0]).includes('/chat/completions'))
    ).toBe(false);
    expect(fetchFn.mock.calls.some((c) => String(c[0]).includes('/hops/'))).toBe(
      true
    );
    expect(writebacks).toEqual([
      { content: 'from agent', reply_to_id: 'user-msg-1' },
    ]);
  });
});
