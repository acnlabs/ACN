import { Command } from 'commander';
import { acnGet, acnPatch } from '../api.js';
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

export function policyCommand(): Command {
  const cmd = new Command('policy').description(
    "Manage agent's inbound communication policy"
  );

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
      async (
        mode: string,
        opts: { rejectReason?: string; agentId?: string }
      ) => {
        if (!POLICY_MODES.includes(mode)) {
          console.error(
            `Invalid mode "${mode}". Choose one of: ${POLICY_MODES.join(', ')}`
          );
          process.exit(1);
        }
        const agentId = opts.agentId ?? requireAgentId();
        try {
          const policyObj: Record<string, unknown> = { mode };
          if (opts.rejectReason) policyObj.reject_reason = opts.rejectReason;
          const body = { communication_policy: policyObj };
          const res = await acnPatch<PolicyResponse>(`/agents/${agentId}/policy`, body);
          output(res, `Policy updated:\n${formatPolicy(res)}`);
        } catch (err) {
          handleError(err);
        }
      }
    );

  return cmd;
}
