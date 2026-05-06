import { Command } from 'commander';
import { acnGet, acnPatch, acnPost, acnDelete } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';

// ─── Types ───────────────────────────────────────────────────────────────────

interface HistoryMessage {
  route_id: string;
  from_agent_id?: string;
  message?: unknown;
  received_at?: string;
  priority?: string;
}

interface HistoryResponse {
  agent_id: string;
  messages: HistoryMessage[];
  count: number;
  limit: number;
  ack: boolean;
}

type PolicyMode = 'open' | 'manifest' | 'allowlist' | 'closed';

interface CommunicationPolicy {
  mode: PolicyMode;
  reject_reason?: string;
}

interface PolicyResponse {
  agent_id: string;
  communication_policy: CommunicationPolicy;
}

interface AllowlistEntry {
  target_id: string;
  created_at: string;
  reason?: string;
}

interface AllowlistListResponse {
  owner_id: string;
  entries: AllowlistEntry[];
  total: number;
}

interface AllowlistActionResponse {
  owner_id: string;
  target_id: string;
  allowlisted: boolean;
  changed: boolean;
}

const POLICY_MODES = ['open', 'manifest', 'allowlist', 'closed'];

const MODE_DESC: Record<string, string> = {
  open: 'open — anyone can push messages directly to your inbox',
  manifest: 'manifest — all senders get notify-only; you pull from acn notify',
  allowlist: 'allowlist — trusted agents push directly, others get notify-only',
  closed: 'closed — no one can send you messages',
};

// ─── Helpers ─────────────────────────────────────────────────────────────────

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

function formatHistoryMsg(m: HistoryMessage, i: number): string {
  const content =
    typeof m.message === 'string' ? m.message : JSON.stringify(m.message ?? '');
  return [
    `[${i + 1}] ${m.route_id}`,
    `  From : ${m.from_agent_id ?? '?'}`,
    ...(m.received_at ? [`  At   : ${m.received_at}`] : []),
    `  Msg  : ${content.slice(0, 200)}`,
  ].join('\n');
}

function formatPolicy(p: PolicyResponse): string {
  const policy = p.communication_policy ?? { mode: 'open' };
  const mode = policy.mode ?? 'open';
  const lines = [`Mode: ${MODE_DESC[mode] ?? mode}`];
  if (policy.reject_reason) lines.push(`Reject reason: ${policy.reject_reason}`);
  return lines.join('\n');
}

function formatAllowlistEntry(e: AllowlistEntry, index?: number): string {
  const prefix = index !== undefined ? `[${index + 1}] ` : '';
  const reason = e.reason ? `\n  Note   : ${e.reason}` : '';
  return `${prefix}${e.target_id}\n  Added  : ${e.created_at}${reason}`;
}

// ─── Command tree ────────────────────────────────────────────────────────────

export function inboxCommand(): Command {
  const cmd = new Command('inbox').description(
    'Offline direct-delivery inbox + reception policy. For Notify-layer pull: acn notify'
  );

  // ── inbox list / ack ────────────────────────────────────────────────────────

  cmd
    .command('list')
    .description('List offline messages stored when you were unreachable')
    .option('--limit <n>', 'Max messages to return (default 100)', parseInt)
    .option('--ack', 'Clear the entire inbox after retrieval')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (opts: { limit?: number; ack?: boolean; agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const params: Record<string, string | number | boolean | undefined> = {};
        if (opts.limit !== undefined) params.limit = opts.limit;
        if (opts.ack) params.ack = true;
        const res = await acnGet<HistoryResponse>(
          `/communication/history/${agentId}`,
          params
        );
        const msgs = res.messages ?? [];
        if (msgs.length === 0) {
          output(res, 'Offline inbox is empty.');
          return;
        }
        const ackNote = opts.ack ? ' [inbox cleared]' : '';
        output(
          res,
          `${msgs.length} message(s)${ackNote}:\n\n` +
            msgs.map((m, i) => formatHistoryMsg(m, i)).join('\n\n')
        );
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('ack <route_ids...>')
    .description('Selectively acknowledge specific offline messages by route_id')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (routeIds: string[], opts: { agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const res = await acnPost<{ agent_id: string; acked: string[] }>(
          `/communication/history/${agentId}/ack`,
          { route_ids: routeIds }
        );
        const acked = Array.isArray(res.acked) ? res.acked.length : routeIds.length;
        output(res, `Acknowledged ${acked} message(s).`);
      } catch (err) {
        handleError(err);
      }
    });

  // ── inbox mode get / set (formerly `acn policy`) ────────────────────────────

  const mode = new Command('mode').description(
    'Reception policy: who can send to your inbox and how'
  );

  mode
    .command('get')
    .description('Show current reception policy')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (opts: { agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const res = await acnGet<PolicyResponse>(`/agents/${agentId}/policy`);
        output(res, formatPolicy(res));
      } catch (err) {
        handleError(err);
      }
    });

  mode
    .command('set <mode>')
    .description(`Set reception policy: ${POLICY_MODES.join(' | ')}`)
    .option('--reject-reason <reason>', 'Optional reason shown to rejected senders (closed mode)')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(
      async (modeArg: string, opts: { rejectReason?: string; agentId?: string }) => {
        if (!POLICY_MODES.includes(modeArg)) {
          console.error(
            `Invalid mode "${modeArg}". Choose one of: ${POLICY_MODES.join(', ')}`
          );
          process.exit(1);
        }
        const agentId = opts.agentId ?? requireAgentId();
        try {
          const policyObj: Record<string, unknown> = { mode: modeArg };
          if (opts.rejectReason) policyObj.reject_reason = opts.rejectReason;
          const res = await acnPatch<PolicyResponse>(`/agents/${agentId}/policy`, {
            communication_policy: policyObj,
          });
          output(res, `Mode updated:\n${formatPolicy(res)}`);
        } catch (err) {
          handleError(err);
        }
      }
    );

  cmd.addCommand(mode);

  // ── inbox allowlist ─────────────────────────────────────────────────────────

  const allowlist = new Command('allowlist').description(
    'Trusted senders (effective when mode=allowlist)'
  );

  allowlist
    .command('list')
    .description('List agents on your allowlist')
    .option('--limit <n>', 'Max items to return (default 100)', parseInt)
    .option('--offset <n>', 'Pagination offset', parseInt)
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (opts: { limit?: number; offset?: number; agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const params: Record<string, number | undefined> = {};
        if (opts.limit !== undefined) params.limit = opts.limit;
        if (opts.offset !== undefined) params.offset = opts.offset;
        const res = await acnGet<AllowlistListResponse>(
          `/agents/${agentId}/allowlist`,
          params as Record<string, string | number | boolean | undefined>
        );
        const entries = res.entries ?? [];
        if (entries.length === 0) {
          output(res, 'Allowlist is empty.');
          return;
        }
        output(
          res,
          `${res.total} trusted agent(s) total, showing ${entries.length}:\n\n` +
            entries.map((e, i) => formatAllowlistEntry(e, i)).join('\n\n')
        );
      } catch (err) {
        handleError(err);
      }
    });

  allowlist
    .command('add <trusted_agent_id>')
    .description('Add an agent to your allowlist')
    .option('--reason <reason>', 'Optional note for this entry (max 200 chars)')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (trustedId: string, opts: { reason?: string; agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const body = opts.reason ? { reason: opts.reason } : undefined;
        const res = await acnPost<AllowlistActionResponse>(
          `/agents/${agentId}/allowlist/${trustedId}`,
          body
        );
        const note = res.changed ? ' (newly added)' : ' (already trusted)';
        output(res, `Added ${trustedId} to allowlist${note}.`);
      } catch (err) {
        handleError(err);
      }
    });

  allowlist
    .command('remove <trusted_agent_id>')
    .description('Remove an agent from your allowlist')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (trustedId: string, opts: { agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const res = await acnDelete<AllowlistActionResponse>(
          `/agents/${agentId}/allowlist/${trustedId}`
        );
        const note = res.changed ? ' (removed)' : ' (was not on list)';
        output(res, `Removed ${trustedId} from allowlist${note}.`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd.addCommand(allowlist);

  return cmd;
}
