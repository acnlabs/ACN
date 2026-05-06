import { Command } from 'commander';
import { acnGet, acnPost, acnDelete } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';

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

function formatEntry(e: AllowlistEntry, index?: number): string {
  const prefix = index !== undefined ? `[${index + 1}] ` : '';
  const reason = e.reason ? `\n  Note   : ${e.reason}` : '';
  return `${prefix}${e.target_id}\n  Added  : ${e.created_at}${reason}`;
}

export function allowlistCommand(): Command {
  const cmd = new Command('allowlist').description(
    "Manage agent's trusted sender allowlist (used in 'allowlist' policy mode)"
  );

  cmd
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
            entries.map((e, i) => formatEntry(e, i)).join('\n\n')
        );
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('add <trusted_agent_id>')
    .description('Add an agent to your allowlist')
    .option('--reason <reason>', 'Optional note for this entry (max 200 chars)')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (trustedId: string, opts: { reason?: string; agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        // target_id goes in the URL path; body carries only the optional reason
        const body = opts.reason ? { reason: opts.reason } : undefined;
        const res = await acnPost<AllowlistActionResponse>(
          `/agents/${agentId}/allowlist/${trustedId}`,
          body
        );
        const changed = res.changed ? ' (newly added)' : ' (already trusted)';
        output(res, `Added ${trustedId} to your allowlist${changed}.`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('remove <trusted_agent_id>')
    .description('Remove an agent from your allowlist')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (trustedId: string, opts: { agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const res = await acnDelete<AllowlistActionResponse>(
          `/agents/${agentId}/allowlist/${trustedId}`
        );
        const changed = res.changed ? ' (removed)' : ' (was not on list)';
        output(res, `Removed ${trustedId} from your allowlist${changed}.`);
      } catch (err) {
        handleError(err);
      }
    });

  return cmd;
}
