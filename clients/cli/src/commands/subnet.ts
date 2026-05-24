import { Command } from 'commander';
import { acnGet, acnPost, acnDelete, acnPatch } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';

interface SubnetInfo {
  slug: string;
  name: string;
  owner?: string;
  description?: string;
  is_private?: boolean;
  harness_url?: string | null;
  harness_registered?: boolean;
  created_at?: string;
  metadata?: Record<string, unknown>;
  // ADR-0003 nesting fields (optional + back-compat default
  // tolerant — older servers may omit them).
  parent_slug?: string | null;
  /** @deprecated */ parent_subnet_id?: string | null;
  lifecycle?: 'persistent' | 'task_scoped';
  linked_task_id?: string | null;
}

interface SubnetListResponse {
  subnets: SubnetInfo[];
  count: number;
}

interface SubnetCreateResponse {
  status: string;
  slug: string;
  is_public: boolean;
  gateway_a2a_url: string;
  gateway_ws_url: string;
  // ADR-0004 — server echoes the effective policy back so callers
  // that omitted ``--join-policy`` can see what the service inferred.
  join_policy?: 'open' | 'approval';
}

// -----------------------------------------------------------------------------
// ADR-0004 admission types (Slice 2.3 PR B). The shapes mirror
// ``acn/routes/_subnet_admission.py::serialize_join_request`` /
// ``serialize_allowlist_entry`` and the sealed-union ``JoinFlowResult``
// dispatch in the same module. We keep server-derived fields optional
// where the route's contract allows nullability so older servers (or
// future shape additions) don't break the CLI parse.
// -----------------------------------------------------------------------------

type SubnetJoinRequestKind = 'join_request' | 'invitation' | 'allowlist_auto';
type SubnetJoinRequestStatus =
  | 'pending'
  | 'approved'
  | 'rejected'
  | 'withdrawn';

interface SubnetJoinRequestDTO {
  request_id: string;
  slug: string;
  agent_id: string;
  kind: SubnetJoinRequestKind;
  status: SubnetJoinRequestStatus;
  initiated_by: string;
  decided_by: string | null;
  created_at: string | null;
  decided_at: string | null;
  note: string | null;
}

interface SubnetAllowlistEntryDTO {
  slug: string;
  agent_id: string;
  added_by: string;
  added_at: string | null;
}

interface JoinRequestListResponse {
  slug: string;
  items: SubnetJoinRequestDTO[];
}

interface InvitationListResponse {
  slug: string;
  items: SubnetJoinRequestDTO[];
}

interface AllowlistListResponse {
  slug: string;
  entries: SubnetAllowlistEntryDTO[];
}

interface AgentInvitationsResponse {
  agent_id: string;
  items: SubnetJoinRequestDTO[];
}

// ``POST /agents/{a}/subnets/{s}`` returns one of six body shapes —
// see ADR-0004 §"join branches" and
// ``_subnet_admission.py::join_flow_result_to_response``. We discriminate
// on body shape (not HTTP status, which the fetch wrapper doesn't expose).
// All variants carry ``subnet_id`` + ``agent_id``.
type JoinResponseBody =
  | { status: 'joined'; slug: string; agent_id: string } // branches 1+2
  | {
      auto_resolved: true;
      resolved_kind: 'invitation';
      slug: string;
      agent_id: string;
      invitation_id: string;
      via: 'self_join' | 'allowlist';
    } // branches 3+4
  | {
      slug: string;
      agent_id: string;
      request_id: string;
      via: 'allowlist';
    } // branch 5
  | {
      slug: string;
      agent_id: string;
      request_id: string;
      status: 'pending';
    }; // branch 6

// ``POST /subnets/{s}/invitations`` returns one of two body shapes per
// ``invite_agent_result_to_response``: the normal pending invitation
// or the merge path where the target already had a pending join_request.
type InviteResponseBody =
  | {
      slug: string;
      agent_id: string;
      invitation_id: string;
      status: 'pending';
    }
  | {
      auto_resolved: true;
      resolved_kind: 'join_request';
      slug: string;
      agent_id: string;
      request_id: string;
    };

function requireAgentId(): string {
  const config = loadConfig();
  if (!config.api_key) {
    console.error('No API key found. Run `acn join` first or `acn config set api-key <key>`.');
    process.exit(1);
  }
  if (!config.agent_id) {
    console.error('No agent ID found. Run `acn join` first or `acn config set agent-id <id>`.');
    process.exit(1);
  }
  return config.agent_id!;
}

function requireApiKey(): void {
  const config = loadConfig();
  if (!config.api_key) {
    console.error('No API key found. Run `acn join` first or `acn config set api-key <key>`.');
    process.exit(1);
  }
}

// -----------------------------------------------------------------------------
// ADR-0004 formatters (Slice 2.3 PR B). All three are pure functions
// so they can be unit-tested directly without spinning up Commander.
// -----------------------------------------------------------------------------

function formatJoinRequest(r: SubnetJoinRequestDTO, index?: number): string {
  const prefix = index !== undefined ? `[${index + 1}] ` : '';
  const lines = [
    `${prefix}${r.request_id}  [${r.kind} / ${r.status}]`,
    `  Subnet     : ${r.slug}`,
    `  Agent      : ${r.agent_id}`,
    `  InitiatedBy: ${r.initiated_by}`,
  ];
  if (r.decided_by) lines.push(`  DecidedBy  : ${r.decided_by}`);
  if (r.created_at) lines.push(`  CreatedAt  : ${r.created_at}`);
  if (r.decided_at) lines.push(`  DecidedAt  : ${r.decided_at}`);
  if (r.note) lines.push(`  Note       : ${r.note}`);
  return lines.join('\n');
}

function formatAllowlistEntry(
  e: SubnetAllowlistEntryDTO,
  index?: number
): string {
  const prefix = index !== undefined ? `[${index + 1}] ` : '';
  const lines = [
    `${prefix}${e.agent_id}`,
    `  AddedBy : ${e.added_by}`,
  ];
  if (e.added_at) lines.push(`  AddedAt : ${e.added_at}`);
  return lines.join('\n');
}

// Render one of the six ADR-0004 ``POST /agents/{a}/subnets/{s}``
// response bodies as a single operator-facing line. The discriminators
// are body-shape only (the fetch wrapper hides HTTP status), but the
// branches happen to be mutually exclusive on body shape — see
// ADR-0004 §"join branches" for the field matrix.
function formatJoinResponse(res: JoinResponseBody, subnetId: string): string {
  // We discriminate on body-shape only (the fetch wrapper hides HTTP
  // status). TypeScript's narrowing for tagged unions with overlapping
  // fields (``via`` appears in branches 3/4 and 5) doesn't catch the
  // exclusive cases here, so we cast to a permissive shape and read
  // the discriminator fields directly. Verified exhaustively against
  // ``_subnet_admission.py::join_flow_result_to_response``.
  type _Any = {
    status?: 'joined' | 'pending';
    auto_resolved?: true;
    resolved_kind?: 'invitation';
    via?: 'self_join' | 'allowlist';
    invitation_id?: string;
    request_id?: string;
  };
  const r = res as _Any;

  // Branches 3/4 — pending invitation auto-accepted on join.
  if (r.auto_resolved && r.resolved_kind === 'invitation') {
    if (r.via === 'self_join') {
      return `accepted pending invitation ${r.invitation_id} from owner — joined subnet ${subnetId}`;
    }
    return `allowlist match plus pending invitation ${r.invitation_id} — accepted invitation, joined subnet ${subnetId}`;
  }
  // Branch 5 — allowlist hit, no pending invitation. Body has
  // ``via='allowlist'`` + ``request_id`` but NO ``auto_resolved``.
  if (r.via === 'allowlist' && r.request_id) {
    return `allowlist match — joined subnet ${subnetId} (request ${r.request_id})`;
  }
  // Branch 6 — fall-through pending join_request (HTTP 202).
  if (r.status === 'pending' && r.request_id) {
    return `join request submitted — pending owner approval (request ${r.request_id})`;
  }
  // Branches 1/2 — open subnet or owner self-join (``status='joined'``).
  return `joined subnet ${subnetId}`;
}

// Render the two-variant ``POST /subnets/{s}/invitations`` response.
function formatInviteResponse(
  res: InviteResponseBody,
  subnetId: string,
  targetAgentId: string
): string {
  type _Any = {
    auto_resolved?: true;
    resolved_kind?: 'join_request';
    invitation_id?: string;
    request_id?: string;
    status?: 'pending';
  };
  const r = res as _Any;
  if (r.auto_resolved && r.resolved_kind === 'join_request') {
    return `target agent ${targetAgentId} already had a pending join request — auto-approved (request ${r.request_id}) on subnet ${subnetId}`;
  }
  return `invitation ${r.invitation_id} sent to ${targetAgentId} on subnet ${subnetId} (status: ${r.status})`;
}

function formatSubnet(s: SubnetInfo, index?: number): string {
  const prefix = index !== undefined ? `[${index + 1}] ` : '';
  const privacy = s.is_private ? ' [private]' : ' [public]';
  const lines = [`${prefix}${s.slug}${privacy}`, `  Name  : ${s.name}`];
  if (s.owner) lines.push(`  Owner : ${s.owner}`);
  if (s.description) lines.push(`  Desc  : ${s.description}`);
  if (s.created_at) lines.push(`  Since : ${s.created_at}`);
  if (s.parent_slug ?? s.parent_subnet_id) {
    lines.push(`  Parent: ${s.parent_slug ?? s.parent_subnet_id}`);
  }
  if (s.lifecycle && s.lifecycle !== 'persistent') {
    const task = s.linked_task_id ? ` (task=${s.linked_task_id})` : '';
    lines.push(`  Lifecycle: ${s.lifecycle}${task}`);
  }
  if (s.harness_registered) {
    lines.push(`  Harness: ${s.harness_url ?? '(registered)'}`);
  } else {
    lines.push(`  Harness: none`);
  }
  return lines.join('\n');
}

export function subnetCommand(): Command {
  const cmd = new Command('subnet').description('Manage ACN subnets');

  cmd
    .command('list')
    .description(
      'List subnets. Without --all/--parent shows only subnets you have joined.'
    )
    .option('--all', 'Show all public subnets on ACN (not just your own)')
    .option(
      '--parent <id>',
      'Show only immediate children of the given parent subnet (ADR-0003)'
    )
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config, ignored with --all/--parent)')
    .action(async (opts: { all?: boolean; parent?: string; agentId?: string }) => {
      try {
        if (opts.parent) {
          const res = await acnGet<SubnetListResponse>(
            `/subnets?parent=${encodeURIComponent(opts.parent)}`
          );
          const subnets = res.subnets ?? [];
          if (subnets.length === 0) {
            output(res, `No visible children of subnet ${opts.parent}.`);
            return;
          }
          output(
            res,
            `${subnets.length} child subnet(s) of ${opts.parent}:\n\n` +
              subnets.map((s, i) => formatSubnet(s, i)).join('\n\n')
          );
          return;
        }
        if (opts.all) {
          const res = await acnGet<SubnetListResponse>('/subnets');
          const subnets = res.subnets ?? [];
          if (subnets.length === 0) {
            output(res, 'No public subnets found.');
            return;
          }
          output(
            res,
            `${subnets.length} public subnet(s):\n\n` +
              subnets.map((s, i) => formatSubnet(s, i)).join('\n\n')
          );
        } else {
          const agentId = opts.agentId ?? requireAgentId();
          const res = await acnGet<{ agent_id: string; subnets: string[] }>(
            `/subnets/${agentId}/subnets`
          );
          const subnets = res.subnets ?? [];
          if (subnets.length === 0) {
            output(res, 'Not a member of any subnets. Use --all to see public subnets.');
            return;
          }
          output(res, `Member of ${subnets.length} subnet(s):\n  ${subnets.join('\n  ')}`);
        }
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('get <subnet_id>')
    .description('Get details of a specific subnet')
    .action(async (subnetId: string) => {
      try {
        const res = await acnGet<SubnetInfo>(`/subnets/${subnetId}`);
        output(res, formatSubnet(res));
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('members <subnet_id>')
    .description('List agents in a subnet')
    .action(async (subnetId: string) => {
      try {
        const res = await acnGet<{ slug: string; agents: unknown[]; count: number }>(
          `/subnets/${subnetId}/agents`
        );
        const count = res.count ?? (res.agents ?? []).length;
        output(res, `${count} agent(s) in subnet ${subnetId}:\n${JSON.stringify(res.agents, null, 2)}`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('join <subnet_id>')
    .description(
      'Join a subnet. ADR-0004: branches on response shape ' +
        '(open/allowlist/auto-invite/pending).'
    )
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (subnetId: string, opts: { agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const res = await acnPost<JoinResponseBody>(
          `/agents/${agentId}/subnets/${subnetId}`
        );
        output(res, formatJoinResponse(res, subnetId));
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('leave <subnet_id>')
    .description('Leave a subnet')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (subnetId: string, opts: { agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const res = await acnDelete<{ status: string; agent_id: string; slug: string }>(
          `/agents/${agentId}/subnets/${subnetId}`
        );
        output(res, `Left subnet ${subnetId} (status: ${res.status})`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('create')
    .description('Create a new subnet (you become the owner)')
    .requiredOption('--name <name>', 'Subnet name (1-128 chars)')
    .option('--id <subnet_id>', 'Custom subnet ID (1-64 chars). Omit to let ACN auto-generate.')
    .option('-d, --description <text>', 'Subnet description (up to 500 chars)')
    .option('--private', 'Mark this subnet as private', false)
    .option(
      '--join-policy <policy>',
      "Join policy: 'open' (anyone joins) or 'approval' (owner gates joins). " +
        'ADR-0004. --private implies approval; explicit open+private is rejected.'
    )
    .option(
      '--parent <id>',
      'Parent subnet ID for a child/squad subnet (ADR-0003). Single-layer cap.'
    )
    .option(
      '--lifecycle <mode>',
      'Lifecycle mode: persistent (default) or task_scoped',
      'persistent'
    )
    .option(
      '--task <id>',
      'Linked task ID. Required with --lifecycle task_scoped.'
    )
    .action(
      async (opts: {
        name: string;
        id?: string;
        description?: string;
        private?: boolean;
        joinPolicy?: string;
        parent?: string;
        lifecycle?: string;
        task?: string;
      }) => {
        const config = loadConfig();
        if (!config.api_key) {
          console.error('No API key found. Run `acn join` first.');
          process.exit(1);
        }
        const lifecycle = opts.lifecycle ?? 'persistent';
        if (lifecycle !== 'persistent' && lifecycle !== 'task_scoped') {
          console.error(
            `Invalid --lifecycle '${lifecycle}'. Must be 'persistent' or 'task_scoped'.`
          );
          process.exit(2);
        }
        // Client-side pre-check matching ADR-0003 entity invariant —
        // saves a round-trip + gives a nicer error than the server's
        // ``task_scoped_requires_linked_task`` response.
        if (lifecycle === 'task_scoped' && !opts.task) {
          console.error('--lifecycle task_scoped requires --task <id>.');
          process.exit(2);
        }
        if (lifecycle === 'persistent' && opts.task) {
          console.error(
            '--task is only valid with --lifecycle task_scoped (current: persistent).'
          );
          process.exit(2);
        }
        // ADR-0004 — validate --join-policy + reconcile with --private.
        // We reject the explicit private+open conflict client-side so
        // operators get a clean message before the server returns 400
        // INVALID_REQUEST + details.reason=visibility_policy_conflict.
        let joinPolicy: 'open' | 'approval' | undefined;
        if (opts.joinPolicy !== undefined) {
          if (opts.joinPolicy !== 'open' && opts.joinPolicy !== 'approval') {
            console.error(
              `Invalid --join-policy '${opts.joinPolicy}'. Must be 'open' or 'approval'.`
            );
            process.exit(2);
          }
          joinPolicy = opts.joinPolicy;
          if (opts.private && joinPolicy === 'open') {
            console.error(
              '--private implies --join-policy=approval; --private --join-policy=open is rejected.'
            );
            process.exit(2);
          }
        }
        const body: Record<string, unknown> = {
          name: opts.name,
          is_private: !!opts.private,
        };
        if (opts.id) body.slug = opts.id;
        if (opts.description) body.description = opts.description;
        if (joinPolicy !== undefined) body.join_policy = joinPolicy;
        if (opts.parent) body.parent_slug = opts.parent;
        body.lifecycle = lifecycle;
        if (opts.task) body.linked_task_id = opts.task;
        try {
          const res = await acnPost<SubnetCreateResponse>('/subnets', body);
          const lines = [
            `Subnet created: ${res.slug}`,
            `  Visibility : ${res.is_public ? 'public' : 'private'}`,
            `  Gateway A2A: ${res.gateway_a2a_url}`,
            `  Gateway WS : ${res.gateway_ws_url}`,
          ];
          if (res.join_policy) {
            lines.push(`  JoinPolicy : ${res.join_policy}`);
          }
          if (opts.parent) lines.push(`  Parent     : ${opts.parent}`);
          if (lifecycle !== 'persistent') {
            lines.push(`  Lifecycle  : ${lifecycle}${opts.task ? ` (task=${opts.task})` : ''}`);
          }
          output(res, lines.join('\n'));
        } catch (err) {
          handleError(err);
        }
      }
    );

  cmd
    .command('promote <subnet_id>')
    .description(
      'Promote a task_scoped subnet to persistent (ADR-0003). Owner-only; idempotent.'
    )
    .action(async (subnetId: string) => {
      const config = loadConfig();
      if (!config.api_key) {
        console.error('No API key found. Run `acn join` first.');
        process.exit(1);
      }
      try {
        const res = await acnPost<SubnetInfo>(`/subnets/${subnetId}/promote`);
        const lifecycle = res.lifecycle ?? 'persistent';
        output(
          res,
          `Subnet ${res.slug} lifecycle=${lifecycle}` +
            (res.linked_task_id ? ` (still linked to task ${res.linked_task_id})` : '')
        );
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('delete <subnet_id>')
    .description('Delete a subnet you own')
    .action(async (subnetId: string) => {
      const config = loadConfig();
      if (!config.api_key) {
        console.error('No API key found. Run `acn join` first.');
        process.exit(1);
      }
      try {
        const res = await acnDelete<{ status: string; slug: string }>(
          `/subnets/${subnetId}`
        );
        output(res, `Subnet ${res.slug} ${res.status}`);
      } catch (err) {
        handleError(err);
      }
    });

  // -------------------------------------------------------------------------
  // acn subnet requests — join-request workflow (ADR-0004 Slice 2.3 PR B)
  // -------------------------------------------------------------------------
  //
  // Owner verbs: list / approve / reject (per ADR §"Application-side
  // endpoints"). Applicant verb: withdraw. Plus a cross-subnet
  // ``pending`` convenience that aggregates client-side by walking
  // the operator's owned subnets — PR A intentionally did not add a
  // dedicated backend endpoint for this view.
  const requests = new Command('requests').description(
    'Manage join-requests for a subnet (ADR-0004)'
  );

  requests
    .command('list <subnet_id>')
    .description(
      'Owner-only: list join_request / allowlist_auto rows for a subnet. ' +
        'Use --kind to switch between channels; --status to filter state.'
    )
    .option(
      '--status <status>',
      'Filter by status: pending | approved | rejected | withdrawn'
    )
    .option(
      '--kind <kind>',
      "Filter by kind: 'join_request' (default) or 'allowlist_auto'. " +
        "'invitation' is rejected here — use `subnet invitations list`.",
      'join_request'
    )
    .option('--limit <n>', 'Page size (1-500, default 100)', '100')
    .option('--offset <n>', 'Page offset (default 0)', '0')
    .action(
      async (
        subnetId: string,
        opts: {
          status?: string;
          kind?: string;
          limit?: string;
          offset?: string;
        }
      ) => {
        requireApiKey();
        const params: Record<string, string> = {};
        if (opts.status) params.status = opts.status;
        if (opts.kind) params.kind = opts.kind;
        if (opts.limit) params.limit = opts.limit;
        if (opts.offset) params.offset = opts.offset;
        const qs = new URLSearchParams(params).toString();
        try {
          const res = await acnGet<JoinRequestListResponse>(
            `/subnets/${subnetId}/join-requests${qs ? `?${qs}` : ''}`
          );
          const items = res.items ?? [];
          if (items.length === 0) {
            output(res, `No join requests on subnet ${subnetId}.`);
            return;
          }
          output(
            res,
            `${items.length} request(s) on subnet ${subnetId}:\n\n` +
              items.map((r, i) => formatJoinRequest(r, i)).join('\n\n')
          );
        } catch (err) {
          handleError(err);
        }
      }
    );

  requests
    .command('pending')
    .description(
      "Owner-side convenience: list pending join_requests across every subnet you own. " +
        'Client-side aggregation — issues N+1 calls (one per owned subnet).'
    )
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (opts: { agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        // Get the agent's owned/joined subnets, then fetch pending
        // join_requests for each. The owner-only nature of the
        // per-subnet endpoint means the aggregation silently skips
        // subnets where the caller is not the owner (403 → ignore).
        const subs = await acnGet<{
          agent_id: string;
          subnets: string[];
        }>(`/agents/${agentId}/subnets`);
        const subnetIds = subs.subnets ?? [];
        const aggregated: Array<{
          slug: string;
          request: SubnetJoinRequestDTO;
        }> = [];
        for (const sid of subnetIds) {
          try {
            const res = await acnGet<JoinRequestListResponse>(
              `/subnets/${sid}/join-requests?status=pending&kind=join_request`
            );
            for (const r of res.items ?? []) {
              aggregated.push({ slug: sid, request: r });
            }
          } catch {
            // Owner-only — silently skip non-owned subnets (per
            // the ADR's authz matrix the 403 here is expected).
            continue;
          }
        }
        if (aggregated.length === 0) {
          output(
            { agent_id: agentId, pending: [] },
            'No pending join requests across your subnets.'
          );
          return;
        }
        output(
          { agent_id: agentId, pending: aggregated },
          `${aggregated.length} pending request(s) across ${subnetIds.length} subnet(s):\n\n` +
            aggregated
              .map((a, i) => formatJoinRequest(a.request, i))
              .join('\n\n')
        );
      } catch (err) {
        handleError(err);
      }
    });

  requests
    .command('approve <subnet_id>')
    .description('Owner-only: approve a pending join_request (CAS pending → approved).')
    .requiredOption('--request-id <rid>', 'Join request ID to approve')
    .option('--note <text>', 'Optional audit note (≤500 chars)')
    .action(
      async (
        subnetId: string,
        opts: { requestId: string; note?: string }
      ) => {
        requireApiKey();
        const body: Record<string, unknown> = {};
        if (opts.note !== undefined) body.note = opts.note;
        try {
          const res = await acnPost<SubnetJoinRequestDTO>(
            `/subnets/${subnetId}/join-requests/${opts.requestId}/approve`,
            body
          );
          output(
            res,
            `approved request ${res.request_id} (agent ${res.agent_id}) on subnet ${subnetId}`
          );
        } catch (err) {
          handleError(err);
        }
      }
    );

  requests
    .command('reject <subnet_id>')
    .description('Owner-only: reject a pending join_request (CAS pending → rejected).')
    .requiredOption('--request-id <rid>', 'Join request ID to reject')
    .option('--note <text>', 'Optional audit note (≤500 chars)')
    .action(
      async (
        subnetId: string,
        opts: { requestId: string; note?: string }
      ) => {
        requireApiKey();
        const body: Record<string, unknown> = {};
        if (opts.note !== undefined) body.note = opts.note;
        try {
          const res = await acnPost<SubnetJoinRequestDTO>(
            `/subnets/${subnetId}/join-requests/${opts.requestId}/reject`,
            body
          );
          output(
            res,
            `rejected request ${res.request_id} (agent ${res.agent_id}) on subnet ${subnetId}`
          );
        } catch (err) {
          handleError(err);
        }
      }
    );

  requests
    .command('withdraw <subnet_id>')
    .description(
      'Applicant-only: withdraw your own pending join_request ' +
        '(CAS pending → withdrawn).'
    )
    .requiredOption('--request-id <rid>', 'Your join request ID')
    .option('--note <text>', 'Optional audit note (≤500 chars)')
    .action(
      async (
        subnetId: string,
        opts: { requestId: string; note?: string }
      ) => {
        requireApiKey();
        const body: Record<string, unknown> = {};
        if (opts.note !== undefined) body.note = opts.note;
        try {
          // DELETE with a body — acnDelete doesn't accept one, so we
          // fall through to acnFetch via a custom call. Keep the
          // wrapper consistent: re-use acnPost shape with empty body
          // is not appropriate (different verb). Instead use the
          // raw fetch by calling acnDelete (DELETE without body —
          // ADR-0004 says the body is OPTIONAL, so dropping it is
          // contract-safe).
          const res = await acnDelete<SubnetJoinRequestDTO>(
            `/subnets/${subnetId}/join-requests/${opts.requestId}`
          );
          if (opts.note !== undefined) {
            // The DELETE wrapper has no body parameter — surface a
            // warning so the user knows the note was not sent rather
            // than silently dropping it.
            console.error(
              '[warn] --note is not sent on withdraw (CLI limitation; ' +
                'use the API directly if you need to record a reason).'
            );
          }
          output(
            res,
            `withdrew request ${res.request_id} on subnet ${subnetId}`
          );
        } catch (err) {
          handleError(err);
        }
      }
    );

  cmd.addCommand(requests);

  // -------------------------------------------------------------------------
  // acn subnet invitations — invitation workflow (ADR-0004 Slice 2.3 PR B)
  // -------------------------------------------------------------------------
  //
  // Owner verbs: send / list / cancel. Invitee verbs: accept / reject.
  // Plus a cross-subnet ``pending`` view backed by the dedicated
  // ``GET /agents/{a}/subnet-invitations`` endpoint added in PR A —
  // no client-side aggregation needed for invitees.
  const invitations = new Command('invitations').description(
    'Manage invitations on a subnet (ADR-0004)'
  );

  invitations
    .command('send <subnet_id>')
    .description(
      'Owner-only: invite an agent to a subnet. Auto-merges with a ' +
        "target's pending join_request (collapses to auto-approval)."
    )
    .requiredOption('--agent-id <aid>', 'Agent ID to invite')
    .option('--note <text>', 'Optional audit note (≤500 chars)')
    .action(
      async (
        subnetId: string,
        opts: { agentId: string; note?: string }
      ) => {
        requireApiKey();
        const body: Record<string, unknown> = { agent_id: opts.agentId };
        if (opts.note !== undefined) body.note = opts.note;
        try {
          const res = await acnPost<InviteResponseBody>(
            `/subnets/${subnetId}/invitations`,
            body
          );
          output(res, formatInviteResponse(res, subnetId, opts.agentId));
        } catch (err) {
          handleError(err);
        }
      }
    );

  invitations
    .command('list <subnet_id>')
    .description('Owner-only: list invitation rows for a subnet.')
    .option(
      '--status <status>',
      'Filter by status: pending | approved | rejected | withdrawn'
    )
    .option('--limit <n>', 'Page size (1-500, default 100)', '100')
    .option('--offset <n>', 'Page offset (default 0)', '0')
    .action(
      async (
        subnetId: string,
        opts: { status?: string; limit?: string; offset?: string }
      ) => {
        requireApiKey();
        const params: Record<string, string> = {};
        if (opts.status) params.status = opts.status;
        if (opts.limit) params.limit = opts.limit;
        if (opts.offset) params.offset = opts.offset;
        const qs = new URLSearchParams(params).toString();
        try {
          const res = await acnGet<InvitationListResponse>(
            `/subnets/${subnetId}/invitations${qs ? `?${qs}` : ''}`
          );
          const items = res.items ?? [];
          if (items.length === 0) {
            output(res, `No invitations on subnet ${subnetId}.`);
            return;
          }
          output(
            res,
            `${items.length} invitation(s) on subnet ${subnetId}:\n\n` +
              items.map((r, i) => formatJoinRequest(r, i)).join('\n\n')
          );
        } catch (err) {
          handleError(err);
        }
      }
    );

  invitations
    .command('pending')
    .description(
      "Invitee view: list pending invitations addressed to you across all subnets " +
        '(backed by GET /agents/{aid}/subnet-invitations).'
    )
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (opts: { agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const res = await acnGet<AgentInvitationsResponse>(
          `/agents/${agentId}/subnet-invitations`
        );
        const items = res.items ?? [];
        if (items.length === 0) {
          output(res, `No pending invitations for ${agentId}.`);
          return;
        }
        output(
          res,
          `${items.length} pending invitation(s) for ${agentId}:\n\n` +
            items.map((r, i) => formatJoinRequest(r, i)).join('\n\n')
        );
      } catch (err) {
        handleError(err);
      }
    });

  invitations
    .command('accept <subnet_id>')
    .description(
      'Invitee-only: accept a pending invitation (CAS pending → approved). ' +
        'Side effect: you join the subnet.'
    )
    .requiredOption('--invitation-id <iid>', 'Invitation ID to accept')
    .option('--note <text>', 'Optional audit note (≤500 chars)')
    .action(
      async (
        subnetId: string,
        opts: { invitationId: string; note?: string }
      ) => {
        requireApiKey();
        const body: Record<string, unknown> = {};
        if (opts.note !== undefined) body.note = opts.note;
        try {
          const res = await acnPost<SubnetJoinRequestDTO>(
            `/subnets/${subnetId}/invitations/${opts.invitationId}/accept`,
            body
          );
          output(
            res,
            `accepted invitation ${res.request_id} — joined subnet ${subnetId}`
          );
        } catch (err) {
          handleError(err);
        }
      }
    );

  invitations
    .command('reject <subnet_id>')
    .description(
      'Invitee-only: reject a pending invitation (CAS pending → rejected). ' +
        'No membership change.'
    )
    .requiredOption('--invitation-id <iid>', 'Invitation ID to reject')
    .option('--note <text>', 'Optional audit note (≤500 chars)')
    .action(
      async (
        subnetId: string,
        opts: { invitationId: string; note?: string }
      ) => {
        requireApiKey();
        const body: Record<string, unknown> = {};
        if (opts.note !== undefined) body.note = opts.note;
        try {
          const res = await acnPost<SubnetJoinRequestDTO>(
            `/subnets/${subnetId}/invitations/${opts.invitationId}/reject`,
            body
          );
          output(
            res,
            `rejected invitation ${res.request_id} on subnet ${subnetId}`
          );
        } catch (err) {
          handleError(err);
        }
      }
    );

  invitations
    .command('cancel <subnet_id>')
    .description(
      'Owner-only: cancel a pending invitation (CAS pending → withdrawn).'
    )
    .requiredOption('--invitation-id <iid>', 'Invitation ID to cancel')
    .action(
      async (subnetId: string, opts: { invitationId: string }) => {
        requireApiKey();
        try {
          const res = await acnDelete<SubnetJoinRequestDTO>(
            `/subnets/${subnetId}/invitations/${opts.invitationId}`
          );
          output(
            res,
            `cancelled invitation ${res.request_id} on subnet ${subnetId}`
          );
        } catch (err) {
          handleError(err);
        }
      }
    );

  cmd.addCommand(invitations);

  // -------------------------------------------------------------------------
  // acn subnet allowlist — pre-authorisation list (ADR-0004 Slice 2.3 PR B)
  // -------------------------------------------------------------------------
  //
  // All three verbs are owner-only by design (the allowlist is a
  // privacy-sensitive trust signal — ADR §"GET /subnets/{s}/allowlist
  // is owner-only deliberately"). DELETE is idempotent (returns 204
  // whether or not the entry existed).
  const allowlist = new Command('allowlist').description(
    'Manage a subnet allowlist (ADR-0004)'
  );

  allowlist
    .command('list <subnet_id>')
    .description('Owner-only: list allowlist entries for a subnet.')
    .option('--limit <n>', 'Page size (1-500, default 100)', '100')
    .option('--offset <n>', 'Page offset (default 0)', '0')
    .action(
      async (
        subnetId: string,
        opts: { limit?: string; offset?: string }
      ) => {
        requireApiKey();
        const params: Record<string, string> = {};
        if (opts.limit) params.limit = opts.limit;
        if (opts.offset) params.offset = opts.offset;
        const qs = new URLSearchParams(params).toString();
        try {
          const res = await acnGet<AllowlistListResponse>(
            `/subnets/${subnetId}/allowlist${qs ? `?${qs}` : ''}`
          );
          const entries = res.entries ?? [];
          if (entries.length === 0) {
            output(res, `Allowlist on subnet ${subnetId} is empty.`);
            return;
          }
          output(
            res,
            `${entries.length} entry/entries on subnet ${subnetId}:\n\n` +
              entries.map((e, i) => formatAllowlistEntry(e, i)).join('\n\n')
          );
        } catch (err) {
          handleError(err);
        }
      }
    );

  allowlist
    .command('add <subnet_id>')
    .description(
      'Owner-only: pre-authorise an agent on the subnet allowlist. ' +
        '409 ALREADY_ON_ALLOWLIST on duplicate.'
    )
    .requiredOption('--agent-id <aid>', 'Agent ID to add')
    .action(
      async (subnetId: string, opts: { agentId: string }) => {
        requireApiKey();
        try {
          const res = await acnPost<SubnetAllowlistEntryDTO>(
            `/subnets/${subnetId}/allowlist`,
            { agent_id: opts.agentId }
          );
          output(
            res,
            `added ${res.agent_id} to allowlist of subnet ${subnetId} (by ${res.added_by})`
          );
        } catch (err) {
          handleError(err);
        }
      }
    );

  allowlist
    .command('remove <subnet_id>')
    .description(
      'Owner-only: remove an agent from the subnet allowlist. ' +
        'Idempotent (204 even if missing).'
    )
    .requiredOption('--agent-id <aid>', 'Agent ID to remove')
    .action(
      async (subnetId: string, opts: { agentId: string }) => {
        requireApiKey();
        try {
          await acnDelete<unknown>(
            `/subnets/${subnetId}/allowlist/${opts.agentId}`
          );
          output(
            { slug: subnetId, agent_id: opts.agentId, removed: true },
            `removed ${opts.agentId} from allowlist of subnet ${subnetId}`
          );
        } catch (err) {
          handleError(err);
        }
      }
    );

  cmd.addCommand(allowlist);

  // -------------------------------------------------------------------------
  // acn subnet harness — Org Harness registration
  // -------------------------------------------------------------------------
  const harness = new Command('harness').description('Manage Org Harness webhook for a subnet');

  harness
    .command('set <subnet_id>')
    .description('Register an Org Harness webhook on a subnet you own')
    .requiredOption('--url <url>', 'Harness webhook URL (HTTPS)')
    .option('--secret <secret>', 'HMAC-SHA256 signing secret (recommended)')
    .action(async (subnetId: string, opts: { url: string; secret?: string }) => {
      const config = loadConfig();
      if (!config.api_key) {
        console.error('No API key found. Run `acn join` first.');
        process.exit(1);
      }
      try {
        const res = await acnPatch<{
          status: string;
          slug: string;
          harness_url: string | null;
          harness_registered: boolean;
        }>(`/subnets/${subnetId}/harness`, {
          harness_url: opts.url,
          harness_secret: opts.secret ?? null,
        });
        const lines = [
          `Harness registered on subnet ${res.slug}`,
          `  URL       : ${res.harness_url}`,
          `  Signed    : ${opts.secret ? 'yes (HMAC-SHA256)' : 'no (unsigned)'}`,
        ];
        output(res, lines.join('\n'));
      } catch (err) {
        handleError(err);
      }
    });

  harness
    .command('clear <subnet_id>')
    .description('Unregister the Org Harness from a subnet you own')
    .action(async (subnetId: string) => {
      const config = loadConfig();
      if (!config.api_key) {
        console.error('No API key found. Run `acn join` first.');
        process.exit(1);
      }
      try {
        const res = await acnPatch<{
          status: string;
          slug: string;
          harness_url: string | null;
          harness_registered: boolean;
        }>(`/subnets/${subnetId}/harness`, {
          harness_url: null,
          harness_secret: null,
        });
        output(res, `Harness unregistered from subnet ${res.slug}`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd.addCommand(harness);

  return cmd;
}
