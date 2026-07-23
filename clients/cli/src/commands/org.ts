import { Command } from 'commander';
import { acnDelete, acnGet, acnPatch, acnPost } from '../api.js';
import { loadConfig } from '../config.js';
import { handleError, output } from '../output.js';

interface OrgInfo {
  org_id: string;
  display_name: string;
  owner?: { kind: string; subject?: string };
  created_by?: { kind: string; subject: string };
  fencing?: {
    subnet_id: string;
    join_policy?: string;
    is_private?: boolean;
    missing?: boolean;
  };
  subnet_id?: string;
  steward_agent_id?: string;
  status?: string;
  plugins?: Record<string, string>;
  harness_webhook?: { url?: string | null; registered?: boolean };
}

interface MemberInfo {
  org_id: string;
  agent_id: string;
  role: string;
  reports_to?: string | null;
  status: string;
  acn?: { subnet_member?: boolean; degraded?: boolean };
}

interface WorkInfo {
  work_id: string;
  org_id: string;
  title: string;
  status: string;
  assignee_agent_id?: string | null;
}

interface PublishedTaskInfo {
  task_id: string;
  title: string;
  status: string;
  subnet_slug?: string | null;
  metadata?: Record<string, unknown>;
}

function formatOrg(o: OrgInfo): string {
  const lines = [
    `  ID       : ${o.org_id}`,
    `  Name     : ${o.display_name}`,
    `  Status   : ${o.status ?? '—'}`,
    `  Owner    : ${o.owner?.kind ?? 'none'}${o.owner?.subject ? ` (${o.owner.subject})` : ''}`,
    `  Steward  : ${o.steward_agent_id ?? '—'}`,
    `  Subnet   : ${o.fencing?.subnet_id ?? o.subnet_id ?? '—'}`,
  ];
  if (o.fencing?.join_policy) lines.push(`  Join     : ${o.fencing.join_policy}`);
  if (o.harness_webhook?.registered) {
    lines.push(`  Harness  : ${o.harness_webhook.url ?? '(registered)'}`);
  }
  return lines.join('\n');
}

export function orgCommand(): Command {
  const cmd = new Command('org').description('Manage ACN organisations (Org Harness)');

  cmd
    .command('create')
    .description('Create an Org (binds/creates a subnet fence)')
    .requiredOption('--name <name>', 'Display name')
    .option('--steward <agent_id>', 'Steward agent (required for human JWT callers)')
    .option('--subnet <slug>', 'Bind existing subnet slug (must be owned by steward)')
    .option('--join-policy <policy>', 'open | approval', 'open')
    .option('--private', 'Private subnet fence', false)
    .option('--harness-url <url>', 'Register Org Harness webhook on the fence subnet')
    .option('--harness-secret <secret>', 'HMAC secret for harness webhook')
    .action(
      async (opts: {
        name: string;
        steward?: string;
        subnet?: string;
        joinPolicy?: string;
        private?: boolean;
        harnessUrl?: string;
        harnessSecret?: string;
      }) => {
        try {
          const body: Record<string, unknown> = {
            display_name: opts.name,
            is_private: Boolean(opts.private),
            join_policy: opts.joinPolicy ?? 'open',
          };
          if (opts.steward) body.steward_agent_id = opts.steward;
          if (opts.subnet) body.subnet_id = opts.subnet;
          if (opts.harnessUrl) body.harness_url = opts.harnessUrl;
          if (opts.harnessSecret) body.harness_secret = opts.harnessSecret;
          const org = await acnPost<OrgInfo>('/orgs', body);
          output(org, formatOrg(org));
        } catch (err) {
          handleError(err);
        }
      },
    );

  cmd
    .command('show <orgId>')
    .description('Show Org details')
    .action(async (orgId: string) => {
      try {
        const org = await acnGet<OrgInfo>(`/orgs/${orgId}`);
        output(org, formatOrg(org));
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('update <orgId>')
    .description('Update Org charter / plugins / display name')
    .option('--name <name>', 'New display name')
    .option('--charter <json>', 'Charter JSON object')
    .option('--plugins <json>', 'Plugins JSON object (merged)')
    .action(
      async (
        orgId: string,
        opts: { name?: string; charter?: string; plugins?: string },
      ) => {
        try {
          const body: Record<string, unknown> = {};
          if (opts.name) body.display_name = opts.name;
          if (opts.charter) body.charter = JSON.parse(opts.charter) as unknown;
          if (opts.plugins) body.plugins = JSON.parse(opts.plugins) as unknown;
          const org = await acnPatch<OrgInfo>(`/orgs/${orgId}`, body);
          output(org, formatOrg(org));
        } catch (err) {
          handleError(err);
        }
      },
    );

  const members = cmd.command('members').description('Manage Org members');

  members
    .command('list <orgId>')
    .description('List active members (marks degraded vs subnet fence)')
    .action(async (orgId: string) => {
      try {
        const res = await acnGet<{
          org_id: string;
          count: number;
          degraded_count: number;
          fence_missing: boolean;
          members: MemberInfo[];
        }>(`/orgs/${orgId}/members`);
        const text = (res.members ?? [])
          .map((m) => {
            const flags: string[] = [];
            if (m.acn?.degraded) flags.push('degraded');
            if (m.acn && !m.acn.subnet_member) flags.push('not-in-subnet');
            const flagStr = flags.length ? `  [${flags.join(',')}]` : '';
            return `  ${m.agent_id}  role=${m.role}  status=${m.status}${flagStr}`;
          })
          .join('\n');
        const header =
          res.degraded_count || res.fence_missing
            ? `  (degraded=${res.degraded_count} fence_missing=${res.fence_missing})\n`
            : '';
        output(res, header + (text || '  (no members)'));
      } catch (err) {
        handleError(err);
      }
    });

  members
    .command('add <orgId> <agentId>')
    .description('Add an agent member')
    .option('--role <role>', 'Member role', 'worker')
    .action(async (orgId: string, agentId: string, opts: { role: string }) => {
      try {
        const m = await acnPost<MemberInfo>(`/orgs/${orgId}/members`, {
          agent_id: agentId,
          role: opts.role,
        });
        output(m, `Added ${m.agent_id} as ${m.role}`);
      } catch (err) {
        handleError(err);
      }
    });

  members
    .command('remove <orgId> <agentId>')
    .description('Remove an agent member')
    .action(async (orgId: string, agentId: string) => {
      try {
        const m = await acnDelete<MemberInfo>(`/orgs/${orgId}/members/${agentId}`);
        output(m, `Removed ${m.agent_id}`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('claim <orgId>')
    .description('Claim ownership of an unclaimed Org (created_by only)')
    .option('--as <kind>', 'human | agent')
    .option('--subject <id>', 'Owner subject (defaults to caller)')
    .action(async (orgId: string, opts: { as?: string; subject?: string }) => {
      try {
        const body: Record<string, unknown> = {};
        if (opts.as) body.owner_kind = opts.as;
        if (opts.subject) body.owner_subject = opts.subject;
        const org = await acnPost<OrgInfo>(`/orgs/${orgId}/claim`, body);
        output(org, formatOrg(org));
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('transfer <orgId>')
    .description('Transfer Org ownership')
    .requiredOption('--kind <kind>', 'human | agent')
    .requiredOption('--subject <id>', 'New owner subject')
    .action(async (orgId: string, opts: { kind: string; subject: string }) => {
      try {
        const org = await acnPost<OrgInfo>(`/orgs/${orgId}/transfer`, {
          new_owner_kind: opts.kind,
          new_owner_subject: opts.subject,
        });
        output(org, formatOrg(org));
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('release <orgId>')
    .description('Release Org ownership back to none')
    .action(async (orgId: string) => {
      try {
        const org = await acnPost<OrgInfo>(`/orgs/${orgId}/release`, {});
        output(org, formatOrg(org));
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('dissolve <orgId>')
    .description('Dissolve an Org (owner or created_by when unclaimed)')
    .action(async (orgId: string) => {
      try {
        const org = await acnPost<OrgInfo>(`/orgs/${orgId}/dissolve`, {});
        output(org, formatOrg(org));
      } catch (err) {
        handleError(err);
      }
    });

  const work = cmd.command('work').description('Minimal Org work queue');

  work
    .command('list <orgId>')
    .description('List work items')
    .option('--open', 'Only open (todo / in_progress)', false)
    .action(async (orgId: string, opts: { open?: boolean }) => {
      try {
        const q = opts.open ? '?open_only=true' : '';
        const res = await acnGet<{ org_id: string; count: number; work: WorkInfo[] }>(
          `/orgs/${orgId}/work${q}`,
        );
        const text = (res.work ?? [])
          .map(
            (w) =>
              `  ${w.work_id}  [${w.status}]  ${w.title}` +
              (w.assignee_agent_id ? `  → ${w.assignee_agent_id}` : ''),
          )
          .join('\n');
        output(res, text || '  (no work)');
      } catch (err) {
        handleError(err);
      }
    });

  work
    .command('create <orgId>')
    .description('Create a work item')
    .requiredOption('--title <title>', 'Work title')
    .option('--assignee <agent_id>', 'Assignee agent')
    .action(async (orgId: string, opts: { title: string; assignee?: string }) => {
      try {
        const body: Record<string, unknown> = { title: opts.title };
        if (opts.assignee) body.assignee_agent_id = opts.assignee;
        const w = await acnPost<WorkInfo>(`/orgs/${orgId}/work`, body);
        output(w, `Created ${w.work_id}: ${w.title}`);
      } catch (err) {
        handleError(err);
      }
    });

  work
    .command('update <orgId> <workId>')
    .description('Update work status')
    .requiredOption('--status <status>', 'todo | in_progress | done | cancelled')
    .option('--assignee <agent_id>', 'Assignee agent')
    .action(
      async (
        orgId: string,
        workId: string,
        opts: { status: string; assignee?: string },
      ) => {
        try {
          const body: Record<string, unknown> = { status: opts.status };
          if (opts.assignee) body.assignee_agent_id = opts.assignee;
          const w = await acnPatch<WorkInfo>(
            `/orgs/${orgId}/work/${workId}`,
            body,
          );
          output(w, `Updated ${w.work_id} → ${w.status}`);
        } catch (err) {
          handleError(err);
        }
      },
    );

  cmd
    .command('tick <orgId>')
    .description('Thin Loop tick (lists open work, emits org.loop_tick)')
    .action(async (orgId: string) => {
      try {
        const res = await acnPost<{ org_id: string; open_count: number; work_ids: string[] }>(
          `/orgs/${orgId}/loop/tick`,
          {},
        );
        output(res, `Loop tick: ${res.open_count} open work item(s)`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('publish-task')
    .description(
      'Publish a Task Pool task attributed to an Org (network by default; not Org work)',
    )
    .requiredOption('--org <org_id>', 'Org id (stored as metadata.org_id)')
    .requiredOption('-t, --title <title>', 'Task title (min 3 chars)')
    .requiredOption('-d, --description <text>', 'Task description (min 10 chars)')
    .requiredOption('--tags <tags>', 'Required skill tags, comma-separated')
    .option('--deadline <hours>', 'Deadline in hours (default: 48)', '48')
    .option('--reward <amount>', 'Reward amount (default: 0)', '0')
    .option('--currency <currency>', 'Reward currency', 'ap_points')
    .option('--type <type>', 'Task type', 'general')
    .option('--max-participants <n>', 'Max participants', '1')
    .option(
      '--fence',
      'Scope task to the Org subnet fence (may deliver task.* to Org harness)',
      false,
    )
    .option('--subnet <slug>', 'Override subnet slug (implies fence; default: Org fence)')
    .action(
      async (opts: {
        org: string;
        title: string;
        description: string;
        tags: string;
        deadline?: string;
        reward?: string;
        currency?: string;
        type?: string;
        maxParticipants?: string;
        fence?: boolean;
        subnet?: string;
      }) => {
        const config = loadConfig();
        if (!config.api_key) {
          console.error(
            'No API key found. Run `acn join` first or `acn config set api-key <key>`.',
          );
          process.exit(1);
        }
        try {
          const org = await acnGet<OrgInfo>(`/orgs/${opts.org}`);
          const fenceSlug =
            opts.subnet ?? org.fencing?.subnet_id ?? org.subnet_id ?? undefined;
          const useFence = Boolean(opts.fence || opts.subnet);
          if (useFence && !fenceSlug) {
            console.error(
              `Org ${opts.org} has no fencing.subnet_id; cannot --fence / --subnet.`,
            );
            process.exit(1);
          }

          const body: Record<string, unknown> = {
            title: opts.title,
            description: opts.description,
            deadline_hours: parseInt(opts.deadline ?? '48', 10),
            required_tags: opts.tags
              .split(',')
              .map((s) => s.trim())
              .filter(Boolean),
            reward: opts.reward ?? '0',
            reward_currency: opts.currency ?? 'ap_points',
            task_type: opts.type ?? 'general',
            max_participants: parseInt(opts.maxParticipants ?? '1', 10),
            metadata: {
              org_id: opts.org,
              org_publish: true,
            },
          };
          if (useFence && fenceSlug) {
            body.subnet_slug = fenceSlug;
          }

          const task = await acnPost<PublishedTaskInfo>('/tasks/agent/create', body);
          const lines = [
            'Task published for Org (Task Pool — not Org work)',
            `  Task ID  : ${task.task_id}`,
            `  Org      : ${opts.org}`,
            `  Status   : ${task.status}`,
            `  Title    : ${task.title}`,
            `  Subnet   : ${task.subnet_slug ?? '(network / unscoped)'}`,
            `  metadata : org_id=${String(task.metadata?.org_id ?? opts.org)} org_publish=${String(task.metadata?.org_publish ?? true)}`,
          ];
          if (useFence) {
            lines.push(
              '  Note     : fenced — task.* may hit the Org harness webhook',
            );
          }
          output(task, lines.join('\n'));
        } catch (err) {
          handleError(err);
        }
      },
    );

  return cmd;
}
