/**
 * ADR-0004 Slice 2.3 — TypeScript SDK subnet-admission surface tests.
 *
 * First vitest test file for `acn-client` (TypeScript). Pin:
 *
 * - All 13 admission methods issue the right verb + path + params
 *   + body shape on the wire.
 * - Optional `note` is only included in the JSON body when set,
 *   matching the server contract (bodyless POST/DELETE).
 * - `subnetInvitationSend` returns the discriminated-union
 *   payload verbatim — branch dispatch is the caller's
 *   responsibility, matching the Python SDK.
 * - `subnetJoinRequestList` defaults `kind='join_request'` to
 *   match the server default.
 * - `SubnetCreateRequest.join_policy` round-trips through
 *   `createSubnet` and is omitted when not set (back-compat for
 *   legacy callers).
 *
 * Implementation strategy: stub `globalThis.fetch` per test and
 * assert on the (url, init) tuple. We avoid mocking the
 * private `request<T>` method directly so the test surface is
 * the same one users see in production — query string assembly,
 * `Content-Type` header presence, body serialisation, and
 * 204-no-content handling all flow through the real code path.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ACNClient } from './client';

interface CapturedRequest {
  url: URL;
  init: RequestInit;
}

function setupFetchStub(
  status: number,
  body: unknown,
): { client: ACNClient; calls: CapturedRequest[] } {
  const calls: CapturedRequest[] = [];
  const fetchStub = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const urlStr = input instanceof URL ? input.toString() : String(input);
    calls.push({ url: new URL(urlStr), init: init ?? {} });
    return new Response(status === 204 ? null : JSON.stringify(body), {
      status,
      headers:
        status === 204 ? {} : { 'Content-Type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fetchStub);
  const client = new ACNClient({ baseUrl: 'http://acn.test', apiKey: 'k' });
  return { client, calls };
}

function readBody(init: RequestInit): unknown {
  if (init.body === undefined || init.body === null) return undefined;
  if (typeof init.body === 'string') return JSON.parse(init.body);
  return init.body;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// SubnetCreateRequest.join_policy round-trip
// ---------------------------------------------------------------------------

describe('SubnetCreateRequest.join_policy', () => {
  it('serialises join_policy when set', async () => {
    const { client, calls } = setupFetchStub(201, {
      success: true,
      subnet_id: 'gated',
      message: 'ok',
    });
    await client.createSubnet({ name: 'Gated', join_policy: 'approval' });
    expect(calls).toHaveLength(1);
    const body = readBody(calls[0].init) as Record<string, unknown>;
    expect(body.join_policy).toBe('approval');
  });

  it('omits join_policy from body when not set (back-compat)', async () => {
    const { client, calls } = setupFetchStub(201, {
      success: true,
      subnet_id: 'open',
      message: 'ok',
    });
    await client.createSubnet({ name: 'Open' });
    const body = readBody(calls[0].init) as Record<string, unknown>;
    expect(body).toStrictEqual({ name: 'Open' });
    expect(body).not.toHaveProperty('join_policy');
  });
});

// ---------------------------------------------------------------------------
// Allowlist (3 verbs)
// ---------------------------------------------------------------------------

describe('subnet allowlist', () => {
  it('subnetAllowlistAdd POSTs canonical path with agent_id body', async () => {
    const { client, calls } = setupFetchStub(201, {
      agent_id: 'alice',
      added_by: 'owner',
      added_at: '2026-05-19T00:00:00Z',
    });
    const result = await client.subnetAllowlistAdd('squad-1', 'alice');
    expect(result.agent_id).toBe('alice');
    expect(calls).toHaveLength(1);
    expect(calls[0].init.method).toBe('POST');
    expect(calls[0].url.pathname).toBe('/api/v1/subnets/squad-1/allowlist');
    expect(readBody(calls[0].init)).toStrictEqual({ agent_id: 'alice' });
  });

  it('subnetAllowlistRemove uses DELETE and resolves to undefined on 204', async () => {
    const { client, calls } = setupFetchStub(204, null);
    const result = await client.subnetAllowlistRemove('squad-1', 'alice');
    expect(result).toBeUndefined();
    expect(calls[0].init.method).toBe('DELETE');
    expect(calls[0].url.pathname).toBe(
      '/api/v1/subnets/squad-1/allowlist/alice',
    );
  });

  it('subnetAllowlistList passes pagination params', async () => {
    const { client, calls } = setupFetchStub(200, {
      subnet_id: 'squad-1',
      entries: [],
    });
    await client.subnetAllowlistList('squad-1', { limit: 50, offset: 10 });
    expect(calls[0].init.method).toBe('GET');
    expect(calls[0].url.pathname).toBe('/api/v1/subnets/squad-1/allowlist');
    expect(calls[0].url.searchParams.get('limit')).toBe('50');
    expect(calls[0].url.searchParams.get('offset')).toBe('10');
  });
});

// ---------------------------------------------------------------------------
// Join requests (4 verbs)
// ---------------------------------------------------------------------------

describe('subnet join requests', () => {
  it('subnetJoinRequestApprove omits body when no note', async () => {
    const { client, calls } = setupFetchStub(200, { status: 'approved' });
    await client.subnetJoinRequestApprove('squad-1', 'req-42');
    expect(calls[0].init.method).toBe('POST');
    expect(calls[0].url.pathname).toBe(
      '/api/v1/subnets/squad-1/join-requests/req-42/approve',
    );
    expect(readBody(calls[0].init)).toBeUndefined();
  });

  it('subnetJoinRequestApprove includes note when set', async () => {
    const { client, calls } = setupFetchStub(200, { status: 'approved' });
    await client.subnetJoinRequestApprove('squad-1', 'req-42', {
      note: 'welcome',
    });
    expect(readBody(calls[0].init)).toStrictEqual({ note: 'welcome' });
  });

  it('subnetJoinRequestReject uses reject path', async () => {
    const { client, calls } = setupFetchStub(200, { status: 'rejected' });
    await client.subnetJoinRequestReject('squad-1', 'req-42', {
      note: 'not a fit',
    });
    expect(calls[0].url.pathname).toBe(
      '/api/v1/subnets/squad-1/join-requests/req-42/reject',
    );
    expect(readBody(calls[0].init)).toStrictEqual({ note: 'not a fit' });
  });

  it('subnetJoinRequestWithdraw uses DELETE on join-requests path', async () => {
    const { client, calls } = setupFetchStub(200, { status: 'withdrawn' });
    await client.subnetJoinRequestWithdraw('squad-1', 'req-42');
    expect(calls[0].init.method).toBe('DELETE');
    expect(calls[0].url.pathname).toBe(
      '/api/v1/subnets/squad-1/join-requests/req-42',
    );
    expect(readBody(calls[0].init)).toBeUndefined();
  });

  it('subnetJoinRequestList defaults kind to join_request', async () => {
    const { client, calls } = setupFetchStub(200, {
      subnet_id: 'squad-1',
      items: [],
    });
    await client.subnetJoinRequestList('squad-1');
    expect(calls[0].url.pathname).toBe(
      '/api/v1/subnets/squad-1/join-requests',
    );
    expect(calls[0].url.searchParams.get('kind')).toBe('join_request');
    expect(calls[0].url.searchParams.get('limit')).toBe('100');
    expect(calls[0].url.searchParams.get('offset')).toBe('0');
    expect(calls[0].url.searchParams.has('status')).toBe(false);
  });

  it('subnetJoinRequestList passes status + allowlist_auto kind', async () => {
    const { client, calls } = setupFetchStub(200, {
      subnet_id: 'squad-1',
      items: [],
    });
    await client.subnetJoinRequestList('squad-1', {
      kind: 'allowlist_auto',
      status: 'approved',
      limit: 25,
      offset: 5,
    });
    expect(calls[0].url.searchParams.get('kind')).toBe('allowlist_auto');
    expect(calls[0].url.searchParams.get('status')).toBe('approved');
    expect(calls[0].url.searchParams.get('limit')).toBe('25');
    expect(calls[0].url.searchParams.get('offset')).toBe('5');
  });
});

// ---------------------------------------------------------------------------
// Invitations (5 + 1 verbs)
// ---------------------------------------------------------------------------

describe('subnet invitations', () => {
  it('subnetInvitationSend forwards normal-path 202 payload', async () => {
    const { client, calls } = setupFetchStub(202, {
      invitation_id: 'inv-42',
      status: 'pending',
    });
    const result = await client.subnetInvitationSend('squad-1', 'bob');
    expect(result).toStrictEqual({
      invitation_id: 'inv-42',
      status: 'pending',
    });
    expect(calls[0].init.method).toBe('POST');
    expect(calls[0].url.pathname).toBe('/api/v1/subnets/squad-1/invitations');
    expect(readBody(calls[0].init)).toStrictEqual({ agent_id: 'bob' });
  });

  it('subnetInvitationSend forwards merge-path 200 payload verbatim', async () => {
    // Server detected a pending join_request from the same target
    // and auto-resolved instead of creating a new invitation row.
    const mergePayload = {
      auto_resolved: true as const,
      resolved_kind: 'join_request' as const,
      request_id: 'req-7',
    };
    const { client, calls } = setupFetchStub(200, mergePayload);
    const result = await client.subnetInvitationSend('squad-1', 'bob', {
      note: 'merging',
    });
    expect(result).toStrictEqual(mergePayload);
    expect(readBody(calls[0].init)).toStrictEqual({
      agent_id: 'bob',
      note: 'merging',
    });
    // Discriminated union narrowing — must be tractable for callers.
    if ('auto_resolved' in result && result.auto_resolved) {
      expect(result.resolved_kind).toBe('join_request');
      expect(result.request_id).toBe('req-7');
    } else {
      throw new Error('expected merge-path discriminator');
    }
  });

  it('subnetInvitationAccept uses accept path with no body', async () => {
    const { client, calls } = setupFetchStub(200, { status: 'approved' });
    await client.subnetInvitationAccept('squad-1', 'inv-42');
    expect(calls[0].init.method).toBe('POST');
    expect(calls[0].url.pathname).toBe(
      '/api/v1/subnets/squad-1/invitations/inv-42/accept',
    );
    expect(readBody(calls[0].init)).toBeUndefined();
  });

  it('subnetInvitationReject uses reject path with note', async () => {
    const { client, calls } = setupFetchStub(200, { status: 'rejected' });
    await client.subnetInvitationReject('squad-1', 'inv-42', {
      note: 'too busy',
    });
    expect(calls[0].url.pathname).toBe(
      '/api/v1/subnets/squad-1/invitations/inv-42/reject',
    );
    expect(readBody(calls[0].init)).toStrictEqual({ note: 'too busy' });
  });

  it('subnetInvitationCancel uses DELETE on invitations path', async () => {
    const { client, calls } = setupFetchStub(200, { status: 'withdrawn' });
    await client.subnetInvitationCancel('squad-1', 'inv-42');
    expect(calls[0].init.method).toBe('DELETE');
    expect(calls[0].url.pathname).toBe(
      '/api/v1/subnets/squad-1/invitations/inv-42',
    );
    expect(readBody(calls[0].init)).toBeUndefined();
  });

  it('subnetInvitationList omits status when none and uses default pagination', async () => {
    const { client, calls } = setupFetchStub(200, {
      subnet_id: 'squad-1',
      items: [],
    });
    await client.subnetInvitationList('squad-1');
    expect(calls[0].url.pathname).toBe('/api/v1/subnets/squad-1/invitations');
    expect(calls[0].url.searchParams.get('limit')).toBe('100');
    expect(calls[0].url.searchParams.get('offset')).toBe('0');
    expect(calls[0].url.searchParams.has('status')).toBe(false);
  });

  it('subnetInvitationList includes status filter', async () => {
    const { client, calls } = setupFetchStub(200, {
      subnet_id: 'squad-1',
      items: [],
    });
    await client.subnetInvitationList('squad-1', { status: 'pending' });
    expect(calls[0].url.searchParams.get('status')).toBe('pending');
  });

  it('agentSubnetInvitations uses agent path', async () => {
    const { client, calls } = setupFetchStub(200, {
      agent_id: 'bob',
      items: [],
    });
    const result = await client.agentSubnetInvitations('bob');
    expect(result).toStrictEqual({ agent_id: 'bob', items: [] });
    expect(calls[0].init.method).toBe('GET');
    expect(calls[0].url.pathname).toBe('/api/v1/agents/bob/subnet-invitations');
  });
});
