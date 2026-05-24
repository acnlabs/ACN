/**
 * CLI regression tests for ADR-0004 Slice 2.3 PR B — the new
 * ``subnet`` admission verbs (allowlist / requests / invitations)
 * plus the ``create`` ``--join-policy`` flag and the ``join`` 6-branch
 * output dispatcher.
 *
 * Mocks src/api.ts; no live ACN backend. Assertions cover:
 * - URL + body shapes match the routes pinned by
 *   ``acn/routes/subnet_admission.py``.
 * - Output strings match ADR §"CLI changes" and §"join branches"
 *   verbatim where the ADR pins specific phrasing.
 * - Client-side guards (--private + --join-policy=open conflict,
 *   invalid --join-policy / --kind values) surface as exit 2 BEFORE
 *   any HTTP call fires.
 */

import { Command } from 'commander';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { acnGet, acnPost, acnDelete } from '../src/api.js';
import { loadConfig } from '../src/config.js';
import { output } from '../src/output.js';
import { subnetCommand } from '../src/commands/subnet.js';

vi.mock('../src/api.js', () => ({
  acnGet: vi.fn(),
  acnPost: vi.fn(),
  acnDelete: vi.fn(),
  acnPatch: vi.fn(),
}));

vi.mock('../src/config.js', () => ({
  loadConfig: vi.fn(() => ({
    api_key: 'sk-test',
    agent_id: 'agent-1',
    base_url: 'https://api.test',
  })),
}));

vi.mock('../src/output.js', () => ({
  output: vi.fn(),
  handleError: vi.fn((err: unknown) => {
    const msg = err instanceof Error ? err.message : String(err);
    throw new Error(`handleError:${msg}`);
  }),
}));

async function runSubnet(args: string[]): Promise<void> {
  const root = new Command();
  root.addCommand(subnetCommand());
  await root.parseAsync(['node', 'acn', 'subnet', ...args]);
}

beforeEach(() => {
  vi.mocked(loadConfig).mockReturnValue({
    api_key: 'sk-test',
    agent_id: 'agent-1',
    base_url: 'https://api.test',
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// subnet create — ADR-0004 --join-policy flag
// ---------------------------------------------------------------------------

describe('subnet create --join-policy (ADR-0004)', () => {
  const createResponse = {
    status: 'created',
    slug: 'subnet-x',
    is_public: true,
    gateway_a2a_url: 'https://gw/a2a/subnet-x',
    gateway_ws_url: 'https://gw/ws/subnet-x',
    join_policy: 'approval',
  };

  it('passes join_policy=approval when flag is set', async () => {
    vi.mocked(acnPost).mockResolvedValue(createResponse as never);
    await runSubnet([
      'create',
      '--name',
      'N',
      '--join-policy',
      'approval',
    ]);
    expect(acnPost).toHaveBeenCalledWith(
      '/subnets',
      expect.objectContaining({ name: 'N', join_policy: 'approval' })
    );
  });

  it('passes join_policy=open when flag is set explicitly', async () => {
    vi.mocked(acnPost).mockResolvedValue({
      ...createResponse,
      join_policy: 'open',
    } as never);
    await runSubnet(['create', '--name', 'N', '--join-policy', 'open']);
    expect(acnPost).toHaveBeenCalledWith(
      '/subnets',
      expect.objectContaining({ join_policy: 'open' })
    );
  });

  it('omits join_policy from the body when flag is absent (server defaults apply)', async () => {
    vi.mocked(acnPost).mockResolvedValue(createResponse as never);
    await runSubnet(['create', '--name', 'N']);
    const [, body] = vi.mocked(acnPost).mock.calls[0]!;
    expect(body).not.toHaveProperty('join_policy');
  });

  it('echoes join_policy in the human output line', async () => {
    vi.mocked(acnPost).mockResolvedValue(createResponse as never);
    await runSubnet(['create', '--name', 'N', '--join-policy', 'approval']);
    const [, text] = vi.mocked(output).mock.calls[0]!;
    expect(text).toContain('JoinPolicy : approval');
  });

  it('exits 2 on invalid --join-policy value', async () => {
    const exitSpy = vi
      .spyOn(process, 'exit')
      .mockImplementation(((code?: number) => {
        throw new Error(`EXIT:${code}`);
      }) as never);
    await expect(
      runSubnet(['create', '--name', 'N', '--join-policy', 'weird'])
    ).rejects.toThrow('EXIT:2');
    exitSpy.mockRestore();
    expect(acnPost).not.toHaveBeenCalled();
  });

  it('exits 2 on --private --join-policy=open conflict (client-side gate)', async () => {
    const exitSpy = vi
      .spyOn(process, 'exit')
      .mockImplementation(((code?: number) => {
        throw new Error(`EXIT:${code}`);
      }) as never);
    await expect(
      runSubnet([
        'create',
        '--name',
        'N',
        '--private',
        '--join-policy',
        'open',
      ])
    ).rejects.toThrow('EXIT:2');
    exitSpy.mockRestore();
    expect(acnPost).not.toHaveBeenCalled();
  });

  it('allows --private --join-policy=approval (consistent combo)', async () => {
    vi.mocked(acnPost).mockResolvedValue(createResponse as never);
    await runSubnet([
      'create',
      '--name',
      'N',
      '--private',
      '--join-policy',
      'approval',
    ]);
    expect(acnPost).toHaveBeenCalledWith(
      '/subnets',
      expect.objectContaining({
        is_private: true,
        join_policy: 'approval',
      })
    );
  });
});

// ---------------------------------------------------------------------------
// subnet join — ADR-0004 6-branch output dispatcher
// ---------------------------------------------------------------------------

describe('subnet join (ADR-0004 6 branches)', () => {
  it('hits the canonical /agents/{aid}/subnets/{sid} URL', async () => {
    vi.mocked(acnPost).mockResolvedValue({
      status: 'joined',
      slug: 's1',
      agent_id: 'agent-1',
    } as never);
    await runSubnet(['join', 's1']);
    expect(acnPost).toHaveBeenCalledWith('/agents/agent-1/subnets/s1');
  });

  it('branch 1/2 (joined): plain "joined subnet" line', async () => {
    vi.mocked(acnPost).mockResolvedValue({
      status: 'joined',
      slug: 's1',
      agent_id: 'agent-1',
    } as never);
    await runSubnet(['join', 's1']);
    const [, text] = vi.mocked(output).mock.calls[0]!;
    expect(text).toBe('joined subnet s1');
  });

  it('branch 3 (self_join auto-invite): mentions accepted invitation', async () => {
    vi.mocked(acnPost).mockResolvedValue({
      auto_resolved: true,
      resolved_kind: 'invitation',
      slug: 's2',
      agent_id: 'agent-1',
      invitation_id: 'inv-3',
      via: 'self_join',
    } as never);
    await runSubnet(['join', 's2']);
    const [, text] = vi.mocked(output).mock.calls[0]!;
    expect(text).toBe(
      'accepted pending invitation inv-3 from owner — joined subnet s2'
    );
  });

  it('branch 4 (allowlist auto-invite merge): explains the merge path', async () => {
    vi.mocked(acnPost).mockResolvedValue({
      auto_resolved: true,
      resolved_kind: 'invitation',
      slug: 's3',
      agent_id: 'agent-1',
      invitation_id: 'inv-4',
      via: 'allowlist',
    } as never);
    await runSubnet(['join', 's3']);
    const [, text] = vi.mocked(output).mock.calls[0]!;
    expect(text).toBe(
      'allowlist match plus pending invitation inv-4 — accepted invitation, joined subnet s3'
    );
  });

  it('branch 5 (allowlist, no invitation): mentions allowlist match', async () => {
    vi.mocked(acnPost).mockResolvedValue({
      slug: 's4',
      agent_id: 'agent-1',
      request_id: 'req-5',
      via: 'allowlist',
    } as never);
    await runSubnet(['join', 's4']);
    const [, text] = vi.mocked(output).mock.calls[0]!;
    expect(text).toBe('allowlist match — joined subnet s4 (request req-5)');
  });

  it('branch 6 (pending): mentions pending owner approval', async () => {
    vi.mocked(acnPost).mockResolvedValue({
      slug: 's5',
      agent_id: 'agent-1',
      request_id: 'req-6',
      status: 'pending',
    } as never);
    await runSubnet(['join', 's5']);
    const [, text] = vi.mocked(output).mock.calls[0]!;
    expect(text).toBe(
      'join request submitted — pending owner approval (request req-6)'
    );
  });
});

// ---------------------------------------------------------------------------
// subnet allowlist — 3 verbs
// ---------------------------------------------------------------------------

describe('subnet allowlist (ADR-0004)', () => {
  it('list hits GET /subnets/{sid}/allowlist', async () => {
    vi.mocked(acnGet).mockResolvedValue({
      slug: 's1',
      entries: [
        {
          slug: 's1',
          agent_id: 'a-1',
          added_by: 'owner-1',
          added_at: '2026-05-19T00:00:00Z',
        },
      ],
    } as never);
    await runSubnet(['allowlist', 'list', 's1']);
    expect(acnGet).toHaveBeenCalledWith(
      '/subnets/s1/allowlist?limit=100&offset=0'
    );
    const [, text] = vi.mocked(output).mock.calls[0]!;
    expect(text).toContain('1 entry');
    expect(text).toContain('a-1');
  });

  it('list with empty result prints empty message', async () => {
    vi.mocked(acnGet).mockResolvedValue({
      slug: 's1',
      entries: [],
    } as never);
    await runSubnet(['allowlist', 'list', 's1']);
    const [, text] = vi.mocked(output).mock.calls[0]!;
    expect(text).toBe('Allowlist on subnet s1 is empty.');
  });

  it('add posts {agent_id}', async () => {
    vi.mocked(acnPost).mockResolvedValue({
      slug: 's1',
      agent_id: 'a-2',
      added_by: 'agent-1',
      added_at: '2026-05-19T00:00:00Z',
    } as never);
    await runSubnet(['allowlist', 'add', 's1', '--agent-id', 'a-2']);
    expect(acnPost).toHaveBeenCalledWith('/subnets/s1/allowlist', {
      agent_id: 'a-2',
    });
  });

  it('remove deletes /subnets/{sid}/allowlist/{aid}', async () => {
    vi.mocked(acnDelete).mockResolvedValue({} as never);
    await runSubnet(['allowlist', 'remove', 's1', '--agent-id', 'a-2']);
    expect(acnDelete).toHaveBeenCalledWith('/subnets/s1/allowlist/a-2');
    const [, text] = vi.mocked(output).mock.calls[0]!;
    expect(text).toBe('removed a-2 from allowlist of subnet s1');
  });
});

// ---------------------------------------------------------------------------
// subnet requests — 4 per-subnet verbs + cross-subnet pending
// ---------------------------------------------------------------------------

describe('subnet requests (ADR-0004)', () => {
  it('list hits GET /subnets/{sid}/join-requests with default kind', async () => {
    vi.mocked(acnGet).mockResolvedValue({
      slug: 's1',
      items: [
        {
          request_id: 'r-1',
          slug: 's1',
          agent_id: 'a-1',
          kind: 'join_request',
          status: 'pending',
          initiated_by: 'a-1',
          decided_by: null,
          created_at: '2026-05-19T00:00:00Z',
          decided_at: null,
          note: null,
        },
      ],
    } as never);
    await runSubnet(['requests', 'list', 's1']);
    expect(acnGet).toHaveBeenCalledWith(
      '/subnets/s1/join-requests?kind=join_request&limit=100&offset=0'
    );
  });

  it('list propagates --status + --kind filters', async () => {
    vi.mocked(acnGet).mockResolvedValue({
      slug: 's1',
      items: [],
    } as never);
    await runSubnet([
      'requests',
      'list',
      's1',
      '--status',
      'pending',
      '--kind',
      'allowlist_auto',
    ]);
    expect(acnGet).toHaveBeenCalledWith(
      '/subnets/s1/join-requests?status=pending&kind=allowlist_auto&limit=100&offset=0'
    );
  });

  it('approve posts to /approve endpoint with optional note', async () => {
    vi.mocked(acnPost).mockResolvedValue({
      request_id: 'r-1',
      slug: 's1',
      agent_id: 'a-1',
      kind: 'join_request',
      status: 'approved',
      initiated_by: 'a-1',
      decided_by: 'agent-1',
      created_at: null,
      decided_at: null,
      note: 'lgtm',
    } as never);
    await runSubnet([
      'requests',
      'approve',
      's1',
      '--request-id',
      'r-1',
      '--note',
      'lgtm',
    ]);
    expect(acnPost).toHaveBeenCalledWith(
      '/subnets/s1/join-requests/r-1/approve',
      { note: 'lgtm' }
    );
  });

  it('reject posts to /reject endpoint, omits body when no --note', async () => {
    vi.mocked(acnPost).mockResolvedValue({
      request_id: 'r-2',
      slug: 's1',
      agent_id: 'a-1',
      kind: 'join_request',
      status: 'rejected',
      initiated_by: 'a-1',
      decided_by: 'agent-1',
      created_at: null,
      decided_at: null,
      note: null,
    } as never);
    await runSubnet(['requests', 'reject', 's1', '--request-id', 'r-2']);
    expect(acnPost).toHaveBeenCalledWith(
      '/subnets/s1/join-requests/r-2/reject',
      {}
    );
  });

  it('withdraw deletes /subnets/{sid}/join-requests/{rid}', async () => {
    vi.mocked(acnDelete).mockResolvedValue({
      request_id: 'r-3',
      slug: 's1',
      agent_id: 'agent-1',
      kind: 'join_request',
      status: 'withdrawn',
      initiated_by: 'agent-1',
      decided_by: 'agent-1',
      created_at: null,
      decided_at: null,
      note: null,
    } as never);
    await runSubnet(['requests', 'withdraw', 's1', '--request-id', 'r-3']);
    expect(acnDelete).toHaveBeenCalledWith(
      '/subnets/s1/join-requests/r-3'
    );
  });

  it('pending aggregates across owned subnets, silently skipping 403s', async () => {
    vi.mocked(acnGet).mockImplementation(((path: string) => {
      if (path === '/agents/agent-1/subnets') {
        return Promise.resolve({
          agent_id: 'agent-1',
          subnets: ['s1', 's2'],
        });
      }
      if (path.startsWith('/subnets/s1/')) {
        return Promise.resolve({
          slug: 's1',
          items: [
            {
              request_id: 'r-a',
              slug: 's1',
              agent_id: 'a-x',
              kind: 'join_request',
              status: 'pending',
              initiated_by: 'a-x',
              decided_by: null,
              created_at: null,
              decided_at: null,
              note: null,
            },
          ],
        });
      }
      if (path.startsWith('/subnets/s2/')) {
        // Owner-only — non-owner sees 403 → CLI silently skips.
        return Promise.reject(new Error('HTTP 403: subnet_not_owner'));
      }
      return Promise.reject(new Error(`unexpected GET ${path}`));
    }) as never);

    await runSubnet(['requests', 'pending']);

    const [, text] = vi.mocked(output).mock.calls[0]!;
    expect(text).toContain('1 pending request');
    expect(text).toContain('r-a');
  });

  it('pending prints empty message when no requests found', async () => {
    vi.mocked(acnGet).mockImplementation(((path: string) => {
      if (path === '/agents/agent-1/subnets') {
        return Promise.resolve({ agent_id: 'agent-1', subnets: [] });
      }
      return Promise.reject(new Error('unexpected'));
    }) as never);
    await runSubnet(['requests', 'pending']);
    const [, text] = vi.mocked(output).mock.calls[0]!;
    expect(text).toBe('No pending join requests across your subnets.');
  });
});

// ---------------------------------------------------------------------------
// subnet invitations — 5 per-subnet verbs + cross-subnet pending
// ---------------------------------------------------------------------------

describe('subnet invitations (ADR-0004)', () => {
  it('send posts {agent_id, note?}', async () => {
    vi.mocked(acnPost).mockResolvedValue({
      slug: 's1',
      agent_id: 'a-2',
      invitation_id: 'inv-1',
      status: 'pending',
    } as never);
    await runSubnet([
      'invitations',
      'send',
      's1',
      '--agent-id',
      'a-2',
      '--note',
      'come join us',
    ]);
    expect(acnPost).toHaveBeenCalledWith('/subnets/s1/invitations', {
      agent_id: 'a-2',
      note: 'come join us',
    });
    const [, text] = vi.mocked(output).mock.calls[0]!;
    expect(text).toBe(
      'invitation inv-1 sent to a-2 on subnet s1 (status: pending)'
    );
  });

  it('send merge path: auto-approved join_request output', async () => {
    vi.mocked(acnPost).mockResolvedValue({
      auto_resolved: true,
      resolved_kind: 'join_request',
      slug: 's1',
      agent_id: 'a-3',
      request_id: 'r-merge',
    } as never);
    await runSubnet(['invitations', 'send', 's1', '--agent-id', 'a-3']);
    const [, text] = vi.mocked(output).mock.calls[0]!;
    expect(text).toBe(
      'target agent a-3 already had a pending join request — auto-approved (request r-merge) on subnet s1'
    );
  });

  it('list hits GET /subnets/{sid}/invitations', async () => {
    vi.mocked(acnGet).mockResolvedValue({
      slug: 's1',
      items: [],
    } as never);
    await runSubnet(['invitations', 'list', 's1']);
    expect(acnGet).toHaveBeenCalledWith(
      '/subnets/s1/invitations?limit=100&offset=0'
    );
  });

  it('pending hits GET /agents/{aid}/subnet-invitations', async () => {
    vi.mocked(acnGet).mockResolvedValue({
      agent_id: 'agent-1',
      items: [
        {
          request_id: 'inv-9',
          slug: 's1',
          agent_id: 'agent-1',
          kind: 'invitation',
          status: 'pending',
          initiated_by: 'owner-1',
          decided_by: null,
          created_at: null,
          decided_at: null,
          note: null,
        },
      ],
    } as never);
    await runSubnet(['invitations', 'pending']);
    expect(acnGet).toHaveBeenCalledWith('/agents/agent-1/subnet-invitations');
    const [, text] = vi.mocked(output).mock.calls[0]!;
    expect(text).toContain('1 pending invitation');
    expect(text).toContain('inv-9');
  });

  it('accept posts to /invitations/{iid}/accept', async () => {
    vi.mocked(acnPost).mockResolvedValue({
      request_id: 'inv-1',
      slug: 's1',
      agent_id: 'agent-1',
      kind: 'invitation',
      status: 'approved',
      initiated_by: 'owner-1',
      decided_by: 'agent-1',
      created_at: null,
      decided_at: null,
      note: null,
    } as never);
    await runSubnet([
      'invitations',
      'accept',
      's1',
      '--invitation-id',
      'inv-1',
    ]);
    expect(acnPost).toHaveBeenCalledWith(
      '/subnets/s1/invitations/inv-1/accept',
      {}
    );
    const [, text] = vi.mocked(output).mock.calls[0]!;
    expect(text).toBe('accepted invitation inv-1 — joined subnet s1');
  });

  it('reject posts to /invitations/{iid}/reject with optional note', async () => {
    vi.mocked(acnPost).mockResolvedValue({
      request_id: 'inv-2',
      slug: 's1',
      agent_id: 'agent-1',
      kind: 'invitation',
      status: 'rejected',
      initiated_by: 'owner-1',
      decided_by: 'agent-1',
      created_at: null,
      decided_at: null,
      note: 'no thanks',
    } as never);
    await runSubnet([
      'invitations',
      'reject',
      's1',
      '--invitation-id',
      'inv-2',
      '--note',
      'no thanks',
    ]);
    expect(acnPost).toHaveBeenCalledWith(
      '/subnets/s1/invitations/inv-2/reject',
      { note: 'no thanks' }
    );
  });

  it('cancel deletes /invitations/{iid}', async () => {
    vi.mocked(acnDelete).mockResolvedValue({
      request_id: 'inv-3',
      slug: 's1',
      agent_id: 'a-target',
      kind: 'invitation',
      status: 'withdrawn',
      initiated_by: 'agent-1',
      decided_by: 'agent-1',
      created_at: null,
      decided_at: null,
      note: null,
    } as never);
    await runSubnet([
      'invitations',
      'cancel',
      's1',
      '--invitation-id',
      'inv-3',
    ]);
    expect(acnDelete).toHaveBeenCalledWith(
      '/subnets/s1/invitations/inv-3'
    );
    const [, text] = vi.mocked(output).mock.calls[0]!;
    expect(text).toBe('cancelled invitation inv-3 on subnet s1');
  });
});
