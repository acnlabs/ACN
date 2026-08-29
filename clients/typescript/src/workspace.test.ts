/**
 * Execution Workspace SDK — doorplate, not a sandbox.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ACNClient } from './client';

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

describe('Execution Workspace SDK', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('create / get / close hit the right paths', async () => {
    const ws = {
      workspace_id: 'ws_1',
      owner_agent_id: 'agt_a',
      display_name: 'yard',
      admit: 'org',
      status: 'active',
    };
    const { client, calls } = setupFetchStub(() => ({ status: 200, body: ws }));

    const created = await client.createWorkspace({
      display_name: 'yard',
      execution_env: { kind: 'git', uri: 'https://github.com/acme/squad.git' },
      admit: 'org',
      org_id: 'org_1',
    });
    expect(created.workspace_id).toBe('ws_1');
    expect(calls[0]!.url.pathname).toBe('/api/v1/workspaces');
    expect(calls[0]!.init.method).toBe('POST');

    await client.getWorkspace('ws_1');
    expect(calls[1]!.url.pathname).toBe('/api/v1/workspaces/ws_1');
    expect(calls[1]!.init.method).toBe('GET');

    await client.closeWorkspace('ws_1');
    expect(calls[2]!.url.pathname).toBe('/api/v1/workspaces/ws_1/close');
    expect(calls[2]!.init.method).toBe('POST');
  });

  it('attestation POST/GET', async () => {
    const att = {
      attestation_id: 'att_1',
      kind: 'workspace_owner',
      workspace_id: 'ws_1',
    };
    const { client, calls } = setupFetchStub(() => ({ status: 200, body: att }));

    const created = await client.createWorkspaceAttestation('ws_1', {
      agent_id: 'agt_b',
      run_id: 'run-9',
      task_id: 'task_1',
      artifact: { git_sha: 'deadbeef' },
    });
    expect(created.kind).toBe('workspace_owner');
    expect(calls[0]!.url.pathname).toBe('/api/v1/workspaces/ws_1/attestations');
    expect(JSON.parse(String(calls[0]!.init.body))).toMatchObject({
      agent_id: 'agt_b',
      run_id: 'run-9',
      artifact: { git_sha: 'deadbeef' },
    });

    await client.getWorkspaceAttestation('ws_1', 'att_1');
    expect(calls[1]!.url.pathname).toBe(
      '/api/v1/workspaces/ws_1/attestations/att_1',
    );
  });
});
