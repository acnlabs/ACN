/**
 * TypeScript SDK tests for getDelivery / setDelivery (ADR-0012 Mode A/B).
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { ACNClient } from './client';

interface CapturedRequest {
  url: URL;
  init: RequestInit;
  body: unknown;
}

function setupFetchStub(
  status: number,
  body: unknown,
): { client: ACNClient; calls: CapturedRequest[] } {
  const calls: CapturedRequest[] = [];
  const fetchStub = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const urlStr = input instanceof URL ? input.toString() : String(input);
    let parsed: unknown = undefined;
    if (init?.body && typeof init.body === 'string') {
      parsed = JSON.parse(init.body);
    }
    calls.push({ url: new URL(urlStr), init: init ?? {}, body: parsed });
    return new Response(status === 204 ? null : JSON.stringify(body), {
      status,
      headers: status === 204 ? {} : { 'Content-Type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fetchStub);
  const client = new ACNClient({ baseUrl: 'http://acn.test', apiKey: 'k' });
  return { client, calls };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('getDelivery', () => {
  it('returns derived transport from the server', async () => {
    const { client, calls } = setupFetchStub(200, {
      agent_id: 'agent-1',
      delivery: 'direct',
      endpoint: 'https://agent.example.com/a2a',
      communication_mode: 'open',
    });

    const result = await client.getDelivery('agent-1');

    expect(result.delivery).toBe('direct');
    expect(result.endpoint).toBe('https://agent.example.com/a2a');
    expect(calls[0].init.method).toBe('GET');
    expect(calls[0].url.pathname).toBe('/api/v1/agents/agent-1/delivery');
  });
});

describe('setDelivery', () => {
  it('PATCHes relay without endpoint', async () => {
    const { client, calls } = setupFetchStub(200, {
      agent_id: 'agent-1',
      delivery: 'relay',
      endpoint: null,
      communication_mode: 'open',
      next_step_hint: 'run acn listen',
    });

    const result = await client.setDelivery('agent-1', 'relay');

    expect(result.delivery).toBe('relay');
    expect(calls[0].init.method).toBe('PATCH');
    expect(calls[0].url.pathname).toBe('/api/v1/agents/agent-1/delivery');
    expect(calls[0].body).toEqual({ delivery: 'relay' });
  });

  it('PATCHes direct with endpoint', async () => {
    const { client, calls } = setupFetchStub(200, {
      agent_id: 'agent-1',
      delivery: 'direct',
      endpoint: 'https://agent.example.com/a2a',
      communication_mode: 'open',
      a2a_handshake_ok: true,
    });

    const result = await client.setDelivery(
      'agent-1',
      'direct',
      'https://agent.example.com/a2a',
    );

    expect(result.delivery).toBe('direct');
    expect(calls[0].body).toEqual({
      delivery: 'direct',
      endpoint: 'https://agent.example.com/a2a',
    });
  });

  it('rejects direct without endpoint before calling the API', async () => {
    const { client, calls } = setupFetchStub(200, {});
    await expect(client.setDelivery('agent-1', 'direct')).rejects.toThrow(
      /requires endpoint/,
    );
    expect(calls).toHaveLength(0);
  });

  it('rejects relay with endpoint before calling the API', async () => {
    const { client, calls } = setupFetchStub(200, {});
    await expect(
      client.setDelivery('agent-1', 'relay', 'https://agent.example.com/a2a'),
    ).rejects.toThrow(/mutually exclusive/);
    expect(calls).toHaveLength(0);
  });
});
