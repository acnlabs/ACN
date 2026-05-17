import { Command } from 'commander';
import { acnGet, acnPost, acnDelete, acnPatch } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';

interface SubnetInfo {
  subnet_id: string;
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
  parent_subnet_id?: string | null;
  lifecycle?: 'persistent' | 'task_scoped';
  linked_task_id?: string | null;
}

interface SubnetListResponse {
  subnets: SubnetInfo[];
  count: number;
}

interface SubnetCreateResponse {
  status: string;
  subnet_id: string;
  is_public: boolean;
  gateway_a2a_url: string;
  gateway_ws_url: string;
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

function formatSubnet(s: SubnetInfo, index?: number): string {
  const prefix = index !== undefined ? `[${index + 1}] ` : '';
  const privacy = s.is_private ? ' [private]' : ' [public]';
  const lines = [`${prefix}${s.subnet_id}${privacy}`, `  Name  : ${s.name}`];
  if (s.owner) lines.push(`  Owner : ${s.owner}`);
  if (s.description) lines.push(`  Desc  : ${s.description}`);
  if (s.created_at) lines.push(`  Since : ${s.created_at}`);
  if (s.parent_subnet_id) {
    lines.push(`  Parent: ${s.parent_subnet_id}`);
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
        const res = await acnGet<{ subnet_id: string; agents: unknown[]; count: number }>(
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
    .description('Join a subnet')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (subnetId: string, opts: { agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const res = await acnPost<{ status: string; agent_id: string; subnet_id: string }>(
          `/subnets/${agentId}/subnets/${subnetId}`
        );
        output(res, `Joined subnet ${subnetId} (status: ${res.status})`);
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
        const res = await acnDelete<{ status: string; agent_id: string; subnet_id: string }>(
          `/subnets/${agentId}/subnets/${subnetId}`
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
        const body: Record<string, unknown> = {
          name: opts.name,
          is_private: !!opts.private,
        };
        if (opts.id) body.subnet_id = opts.id;
        if (opts.description) body.description = opts.description;
        if (opts.parent) body.parent_subnet_id = opts.parent;
        body.lifecycle = lifecycle;
        if (opts.task) body.linked_task_id = opts.task;
        try {
          const res = await acnPost<SubnetCreateResponse>('/subnets', body);
          const lines = [
            `Subnet created: ${res.subnet_id}`,
            `  Visibility : ${res.is_public ? 'public' : 'private'}`,
            `  Gateway A2A: ${res.gateway_a2a_url}`,
            `  Gateway WS : ${res.gateway_ws_url}`,
          ];
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
          `Subnet ${res.subnet_id} lifecycle=${lifecycle}` +
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
        const res = await acnDelete<{ status: string; subnet_id: string }>(
          `/subnets/${subnetId}`
        );
        output(res, `Subnet ${res.subnet_id} ${res.status}`);
      } catch (err) {
        handleError(err);
      }
    });

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
          subnet_id: string;
          harness_url: string | null;
          harness_registered: boolean;
        }>(`/subnets/${subnetId}/harness`, {
          harness_url: opts.url,
          harness_secret: opts.secret ?? null,
        });
        const lines = [
          `Harness registered on subnet ${res.subnet_id}`,
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
          subnet_id: string;
          harness_url: string | null;
          harness_registered: boolean;
        }>(`/subnets/${subnetId}/harness`, {
          harness_url: null,
          harness_secret: null,
        });
        output(res, `Harness unregistered from subnet ${res.subnet_id}`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd.addCommand(harness);

  return cmd;
}
