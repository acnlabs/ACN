/**
 * Org Harness Work Port — TypeScript SDK surface tests.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ACNClient, ACNError } from './client';
import { orgSubnetId } from './types';

interface CapturedRequest {
  url: URL;
  init: RequestInit;
}

function setupFetchStub(
  handler: (url: URL, init: RequestInit) => { status: number; body: unknown },
): { client: ACNClient; calls: CapturedRequest[] } {
  const calls: CapturedRequest[] = [];
  const fetchStub = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(input instanceof URL ? input.toString() : String(input));
    const reqInit = init ?? {};
    calls.push({ url, init: reqInit });
    const { status, body } = handler(url, reqInit);
    return new Response(status === 204 ? null : JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fetchStub);
  const client = new ACNClient({
    baseUrl: 'http://acn.test',
    apiKey: 'acn_test',
  });
  return { client, calls };
}

describe('Org Harness SDK', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('createOrg / getOrg hit the right paths', async () => {
    const org = {
      org_id: 'org_abc',
      display_name: 'Acme',
      subnet_id: 'sub-1',
      fencing: { subnet_id: 'sub-1' },
    };
    const { client, calls } = setupFetchStub(() => ({ status: 200, body: org }));

    const created = await client.createOrg({
      display_name: 'Acme',
      subnet_id: 'sub-1',
      join_policy: 'open',
    });
    expect(created.org_id).toBe('org_abc');
    expect(calls[0]!.url.pathname).toBe('/api/v1/orgs');
    expect(calls[0]!.init.method).toBe('POST');
    expect(JSON.parse(String(calls[0]!.init.body))).toMatchObject({
      display_name: 'Acme',
      subnet_id: 'sub-1',
      join_policy: 'open',
    });

    await client.getOrg('org_abc');
    expect(calls[1]!.url.pathname).toBe('/api/v1/orgs/org_abc');
    expect(calls[1]!.init.method).toBe('GET');
  });

  it('createWork / updateWork / listWork / tickOrgLoop', async () => {
    const work = {
      work_id: 'work_1',
      org_id: 'org_abc',
      title: 'Ship SDK',
      status: 'todo',
    };
    const { client, calls } = setupFetchStub((url) => {
      if (url.pathname.endsWith('/loop/tick')) {
        return { status: 200, body: { open_count: 1 } };
      }
      if (url.pathname.endsWith('/work') && (!url.search || url.search.includes('open_only'))) {
        // GET list vs POST create distinguished by method below — body differs
      }
      if (url.pathname.endsWith('/work') || url.pathname.includes('/work/')) {
        if (url.searchParams.has('open_only')) {
          return { status: 200, body: { work: [work] } };
        }
        return { status: 200, body: work };
      }
      return { status: 500, body: { message: 'unexpected' } };
    });

    await client.createWork('org_abc', { title: 'Ship SDK' });
    expect(calls[0]!.url.pathname).toBe('/api/v1/orgs/org_abc/work');
    expect(calls[0]!.init.method).toBe('POST');
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({ title: 'Ship SDK' });

    await client.updateWork('org_abc', 'work_1', { status: 'done' });
    expect(calls[1]!.url.pathname).toBe('/api/v1/orgs/org_abc/work/work_1');
    expect(calls[1]!.init.method).toBe('PATCH');
    expect(JSON.parse(String(calls[1]!.init.body))).toEqual({ status: 'done' });

    const listed = await client.listWork('org_abc', { openOnly: true });
    expect(listed.work).toHaveLength(1);
    expect(calls[2]!.url.searchParams.get('open_only')).toBe('true');

    const tick = await client.tickOrgLoop('org_abc');
    expect(tick.open_count).toBe(1);
    expect(calls[3]!.url.pathname).toBe('/api/v1/orgs/org_abc/loop/tick');
    expect(calls[3]!.init.method).toBe('POST');

    await client.createWork('org_abc', {
      title: 'Root',
      metadata: { wave: { role: 'root', wave_id: 'wv_1' } },
    });
    expect(JSON.parse(String(calls[4]!.init.body))).toEqual({
      title: 'Root',
      metadata: { wave: { role: 'root', wave_id: 'wv_1' } },
    });

    await client.updateWork('org_abc', 'work_1', {
      status: 'todo',
      metadata: null,
    });
    expect(JSON.parse(String(calls[5]!.init.body))).toEqual({
      status: 'todo',
      metadata: null,
    });
  });

  it('orgSubnetId prefers fencing', () => {
    expect(
      orgSubnetId({
        org_id: 'org_x',
        display_name: 'X',
        subnet_id: 'top',
        fencing: { subnet_id: 'fence' },
      }),
    ).toBe('fence');
    expect(
      orgSubnetId({ org_id: 'org_x', display_name: 'X', subnet_id: 'top' }),
    ).toBe('top');
  });

  it('ACNError exposes reason and boundOrgIdHint', async () => {
    const { client } = setupFetchStub(() => ({
      status: 409,
      body: {
        error_code: 'conflict',
        message: 'subnet already bound to org_deadbeef01234567',
        details: {
          reason: 'subnet_bound',
          bound_org_id: 'org_deadbeef01234567',
        },
      },
    }));

    await expect(client.createOrg({ display_name: 'Dup' })).rejects.toMatchObject({
      status: 409,
      reason: 'subnet_bound',
      boundOrgIdHint: 'org_deadbeef01234567',
    });

    const err = new ACNError(409, 'conflict prose mentions org_aabbccddeeff0011', {
      body: { message: 'x' },
    });
    expect(err.boundOrgIdHint).toBe('org_aabbccddeeff0011');
  });
});
