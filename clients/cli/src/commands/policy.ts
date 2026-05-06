import { Command } from 'commander';
import { acnGet, acnPatch, acnPost, acnDelete } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';

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
  open: 'open — anyone can push messages directly',
  manifest: 'manifest — all senders get notify-only, you pull on demand',
  allowlist: 'allowlist — trusted agents push directly, others get notify-only',
  closed: 'closed — no one can send you messages',
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

function formatPolicy(p: PolicyResponse): string {
  const policy = p.communication_policy ?? { mode: 'open' };
  const mode = policy.mode ?? 'open';
  const lines = [`Policy: ${MODE_DESC[mode] ?? mode}`];
  if (policy.reject_reason) lines.push(`Reject reason: ${policy.reject_reason}`);
  return lines.join('\n');
}

function formatAllowlistEntry(e: AllowlistEntry, index?: number): string {
  const prefix = index !== undefined ? `[${index + 1}] ` : '';
  const reason = e.reason ? `\n  Note   : ${e.reason}` : '';
  return `${prefix}${e.target_id}\n  Added  : ${e.created_at}${reason}`;
}

export function policyCommand(): Command {
  const cmd = new Command('policy').description(
    "Manage inbound communication policy and trusted-sender allowlist"
  );

  // ── policy get / set ────────────────────────────────────────────────────────

  cmd
    .command('get')
    .description('Show current communication policy')
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

  cmd
    .command('set <mode>')
    .description(`Set communication policy: ${POLICY_MODES.join(' | ')}`)
    .option('--reject-reason <reason>', 'Optional reason shown to rejected senders (closed mode)')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(
      async (mode: string, opts: { rejectReason?: string; agentId?: string }) => {
        if (!POLICY_MODES.includes(mode)) {
          console.error(`Invalid mode "${mode}". Choose one of: ${POLICY_MODES.join(', ')}`);
          process.exit(1);
        }
        const agentId = opts.agentId ?? requireAgentId();
        try {
          const policyObj: Record<string, unknown> = { mode };
          if (opts.rejectReason) policyObj.reject_reason = opts.rejectReason;
          const res = await acnPatch<PolicyResponse>(`/agents/${agentId}/policy`, {
            communication_policy: policyObj,
          });
          output(res, `Policy updated:\n${formatPolicy(res)}`);
        } catch (err) {
          handleError(err);
        }
      }
    );

  // ── policy allowlist (subgroup) ─────────────────────────────────────────────
  // Allowlist is only relevant when policy=allowlist, so it lives here.

  const allowlist = new Command('allowlist').description(
    "Manage trusted senders (relevant when policy=allowlist)"
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
