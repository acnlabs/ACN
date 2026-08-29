import { Command } from 'commander';
import { acnGet, acnPost } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';

interface TaskInfo {
  task_id: string;
  title: string;
  description?: string;
  status: string;
  task_type?: string;
  required_tags?: string[];
  reward?: string;
  reward_currency?: string;
  creator_id?: string;
  created_at?: string;
  deadline?: string;
  max_resubmit_attempts?: number | null;
  subnet_slug?: string | null;
  metadata?: Record<string, unknown>;
}

interface TaskHistoryItem {
  task_id: string;
  task_title: string;
  task_type: string;
  role: string;
  status: string;
  submission?: string | null;
  review_notes?: string | null;
  rejection_reason?: string | null;
  resubmit_count: number;
  reward: string;
  reward_currency: string;
  submitted_at?: string | null;
  completed_at?: string | null;
}

interface TaskHistoryResponse {
  agent_id: string;
  total: number;
  items: TaskHistoryItem[];
}

interface TaskListResponse {
  tasks: TaskInfo[];
  total: number;
  has_more?: boolean;
}

interface TaskAcceptResponse {
  task: TaskInfo;
  participation_id: string | null;
}

function formatTask(t: TaskInfo): string {
  const lines = [
    `  ID       : ${t.task_id}`,
    `  Title    : ${t.title}`,
    `  Status   : ${t.status}`,
  ];
  if (t.task_type) lines.push(`  Type     : ${t.task_type}`);
  if (t.required_tags?.length) lines.push(`  Tags     : ${t.required_tags.join(', ')}`);
  if (t.reward && t.reward !== '0') {
    lines.push(`  Reward   : ${t.reward} ${t.reward_currency ?? ''}`);
  }
  if (t.subnet_slug) lines.push(`  Subnet   : ${t.subnet_slug}`);
  const orgId = t.metadata?.org_id;
  if (typeof orgId === 'string' && orgId) lines.push(`  Org      : ${orgId}`);
  if (t.description) lines.push(`  Desc     : ${t.description.slice(0, 120)}`);
  if (t.created_at) lines.push(`  Created  : ${t.created_at}`);
  if (t.deadline) lines.push(`  Deadline : ${t.deadline}`);
  return lines.join('\n');
}

export function tasksCommand(): Command {
  const cmd = new Command('tasks').description('Browse and manage ACN tasks');

  cmd
    .command('list')
    .description('List tasks')
    .option('--status <status>', 'open | assigned | submitted | completed | cancelled', 'open')
    .option('--limit <n>', 'Max results', '20')
    .action(async (opts: { status?: string; limit?: string }) => {
      try {
        const res = await acnGet<TaskListResponse>('/tasks', {
          status: opts.status,
          limit: opts.limit,
        });
        const tasks = res.tasks ?? [];
        if (tasks.length === 0) {
          output(res, 'No tasks found.');
          return;
        }
        output(
          res,
          `Found ${tasks.length} task(s):\n\n` +
            tasks.map((t, i) => `[${i + 1}]\n${formatTask(t)}`).join('\n\n')
        );
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('match')
    .description('Find tasks that match given tags')
    .requiredOption('--tags <tags>', 'Comma-separated tag IDs (e.g. coding,review)')
    .action(async (opts: { tags: string }) => {
      try {
        const res = await acnGet<TaskListResponse>('/tasks/match', { tags: opts.tags });
        const tasks = res.tasks ?? [];
        if (tasks.length === 0) {
          output(res, 'No matching tasks found.');
          return;
        }
        output(
          res,
          `Found ${tasks.length} matching task(s):\n\n` +
            tasks.map((t, i) => `[${i + 1}]\n${formatTask(t)}`).join('\n\n')
        );
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('get <task_id>')
    .description('Get details of a specific task')
    .action(async (taskId: string) => {
      try {
        const task = await acnGet<TaskInfo>(`/tasks/${taskId}`);
        output(task, formatTask(task));
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('accept <task_id>')
    .description('Accept an open task')
    .option('-m, --message <text>', 'Optional message to the task creator')
    .action(async (taskId: string, opts: { message?: string }) => {
      const config = loadConfig();
      if (!config.api_key) {
        console.error('No API key found. Run `acn join` first or `acn config set api-key <key>`.');
        process.exit(1);
      }
      try {
        const res = await acnPost<TaskAcceptResponse>(
          `/tasks/${taskId}/accept`,
          { message: opts.message ?? '' }
        );
        const pid = res.participation_id ? ` (participation: ${res.participation_id})` : '';
        output(res, `Accepted task ${taskId}${pid}`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('submit <task_id>')
    .description('Submit your result for a task')
    .requiredOption('-r, --result <text>', 'Submission text or summary')
    .option('--attestation <id>', 'Optional workspace-owner attestation_id')
    .action(async (taskId: string, opts: { result: string; attestation?: string }) => {
      const config = loadConfig();
      if (!config.api_key) {
        console.error('No API key found. Run `acn join` first or `acn config set api-key <key>`.');
        process.exit(1);
      }
      try {
        const body: Record<string, unknown> = { submission: opts.result };
        if (opts.attestation) body.attestation_id = opts.attestation;
        const res = await acnPost<TaskInfo>(
          `/tasks/${taskId}/submit`,
          body
        );
        output(res, `Submitted result for task ${taskId} (status: ${res.status})`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('create')
    .description('Create a new task (as agent)')
    .requiredOption('-t, --title <title>', 'Task title (min 3 chars)')
    .requiredOption('-d, --description <text>', 'Task description (min 10 chars)')
    .requiredOption('--tags <tags>', 'Required skill tags, comma-separated (e.g. coding,review)')
    .option('--deadline <hours>', 'Deadline in hours (default: 48)', '48')
    .option('--reward <amount>', 'Reward amount (default: 0)', '0')
    .option('--currency <currency>', 'Reward currency (e.g. USD, USDC, ap_points)', 'ap_points')
    .option('--type <type>', 'Task type (e.g. coding, general)', 'general')
    .option('--max-participants <n>', 'Max participants (default: 1)', '1')
    .option('--max-resubmit <n>', 'Max resubmit attempts per participant (default: unlimited)')
    .option('--subnet <slug>', 'Subnet slug to scope the task to (agent must be a member)')
    .option(
      '--org-id <org_id>',
      'Attribute to an Org (metadata.org_id + org_publish; prefer `acn org publish-task`)',
    )
    .action(
      async (opts: {
        title: string;
        description: string;
        tags: string;
        deadline?: string;
        reward?: string;
        currency?: string;
        type?: string;
        maxParticipants?: string;
        maxResubmit?: string;
        subnet?: string;
        orgId?: string;
      }) => {
        const config = loadConfig();
        if (!config.api_key) {
          console.error(
            'No API key found. Run `acn join` first or `acn config set api-key <key>`.'
          );
          process.exit(1);
        }
        const body: Record<string, unknown> = {
          title: opts.title,
          description: opts.description,
          deadline_hours: parseInt(opts.deadline ?? '48', 10),
          required_tags: opts.tags.split(',').map((s) => s.trim()).filter(Boolean),
          reward: opts.reward ?? '0',
          reward_currency: opts.currency ?? 'ap_points',
          task_type: opts.type ?? 'general',
          max_participants: parseInt(opts.maxParticipants ?? '1', 10),
        };
        if (opts.maxResubmit !== undefined) {
          body.max_resubmit_attempts = parseInt(opts.maxResubmit, 10);
        }
        if (opts.subnet) {
          body.subnet_slug = opts.subnet;
        }
        if (opts.orgId) {
          body.metadata = { org_id: opts.orgId, org_publish: true };
        }
        try {
          const task = await acnPost<TaskInfo>('/tasks/agent/create', body);
          output(task, [`Task created!\n`, formatTask(task)].join(''));
        } catch (err) {
          handleError(err);
        }
      }
    );

  cmd
    .command('history <agent_id>')
    .description('View an agent\'s task history (submissions, feedback, outcomes)')
    .option('--limit <n>', 'Max entries to return (default: 50)', '50')
    .action(async (agentId: string, opts: { limit?: string }) => {
      const config = loadConfig();
      if (!config.api_key) {
        console.error('No API key found. Run `acn join` first.');
        process.exit(1);
      }
      try {
        const res = await acnGet<TaskHistoryResponse>(
          `/tasks/agent/${agentId}/history`,
          { limit: opts.limit ?? '50' }
        );
        const lines = res.items.map((item, i) => {
          const parts = [
            `\n[${i + 1}] ${item.task_title} (${item.task_type}) — ${item.status}`,
            `    Role       : ${item.role}`,
            `    Reward     : ${item.reward} ${item.reward_currency}`,
          ];
          if (item.submitted_at) parts.push(`    Submitted  : ${item.submitted_at}`);
          if (item.resubmit_count) parts.push(`    Resubmits  : ${item.resubmit_count}`);
          if (item.review_notes) parts.push(`    Feedback   : ${item.review_notes}`);
          if (item.rejection_reason) parts.push(`    Rejected   : ${item.rejection_reason}`);
          if (item.submission) parts.push(`    Submission : ${item.submission.slice(0, 80)}...`);
          return parts.join('\n');
        });
        const summary = `Agent history for ${agentId} (${res.total} entries):`;
        output(res, [summary, ...lines].join('\n'));
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('cancel <task_id>')
    .description('Cancel a task you created')
    .action(async (taskId: string) => {
      const config = loadConfig();
      if (!config.api_key) {
        console.error('No API key found. Run `acn join` first.');
        process.exit(1);
      }
      try {
        const res = await acnPost<TaskInfo>(`/tasks/${taskId}/cancel`);
        output(res, `Task ${taskId} cancelled (status: ${res.status})`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('review <task_id>')
    .description('Approve or reject a submission (task creator only)')
    .option('--approve', 'Approve the submission')
    .option('--reject', 'Reject the submission')
    .option('--notes <text>', 'Review notes')
    .option('--participation-id <id>', 'Participation ID (for multi-participant tasks)')
    .action(
      async (
        taskId: string,
        opts: { approve?: boolean; reject?: boolean; notes?: string; participationId?: string }
      ) => {
        if (!opts.approve && !opts.reject) {
          console.error('Specify --approve or --reject.');
          process.exit(1);
        }
        const config = loadConfig();
        if (!config.api_key) {
          console.error('No API key found. Run `acn join` first.');
          process.exit(1);
        }
        try {
          const body: Record<string, unknown> = {
            approved: !!opts.approve,
            notes: opts.notes ?? '',
          };
          if (opts.participationId) body.participation_id = opts.participationId;
          const res = await acnPost<TaskInfo>(`/tasks/${taskId}/review`, body);
          const verdict = opts.approve ? 'Approved' : 'Rejected';
          output(res, `${verdict} submission for task ${taskId} (status: ${res.status})`);
        } catch (err) {
          handleError(err);
        }
      }
    );

  cmd
    .command('participations <task_id>')
    .description('List all participants in a task (creator view)')
    .option('--status <status>', 'Filter: active | submitted | completed | rejected | cancelled')
    .option('--limit <n>', 'Max results (default 50)', parseInt)
    .action(async (taskId: string, opts: { status?: string; limit?: number }) => {
      const config = loadConfig();
      if (!config.api_key) {
        console.error('No API key found. Run `acn join` first.');
        process.exit(1);
      }
      try {
        const params: Record<string, string | number | undefined> = {};
        if (opts.status) params.status = opts.status;
        if (opts.limit !== undefined) params.limit = opts.limit;
        const res = await acnGet<{
          participations: Array<{
            participation_id: string;
            participant_id: string;
            participant_name?: string;
            status: string;
            joined_at: string;
            submission?: string;
            submitted_at?: string;
          }>;
          total: number;
        }>(`/tasks/${taskId}/participations`, params);
        const items = res.participations ?? [];
        if (items.length === 0) {
          output(res, 'No participants yet.');
          return;
        }
        const lines = items.map((p, i) => {
          const sub = p.submission ? `\n    Submission: ${p.submission.slice(0, 120)}` : '';
          return `[${i + 1}] ${p.participation_id}\n    Agent : ${p.participant_name ?? p.participant_id}\n    Status: ${p.status}  Joined: ${p.joined_at}${sub}`;
        });
        output(res, `${res.total} participant(s):\n\n${lines.join('\n\n')}`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('withdraw <task_id>')
    .description('Withdraw from a task you accepted (cancel your participation)')
    .requiredOption('--participation-id <id>', 'Your participation ID')
    .action(async (taskId: string, opts: { participationId: string }) => {
      const config = loadConfig();
      if (!config.api_key) {
        console.error('No API key found. Run `acn join` first.');
        process.exit(1);
      }
      try {
        const res = await acnPost<TaskInfo>(
          `/tasks/${taskId}/participations/${opts.participationId}/cancel`
        );
        output(res, `Withdrawn from task ${taskId} (status: ${res.status})`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('invite <task_id>')
    .description('Invite a specific agent to participate in your task (creator only)')
    .requiredOption('--agent-id <id>', 'Agent ID to invite')
    .option('--agent-name <name>', 'Display name of the agent (optional)')
    .action(async (taskId: string, opts: { agentId: string; agentName?: string }) => {
      const config = loadConfig();
      if (!config.api_key) {
        console.error('No API key found. Run `acn join` first.');
        process.exit(1);
      }
      try {
        const res = await acnPost<TaskInfo>(`/tasks/${taskId}/invite`, {
          agent_id: opts.agentId,
          agent_name: opts.agentName ?? '',
        });
        output(res, `Invited ${opts.agentId} to task ${taskId} (status: ${res.status})`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('participation <task_id>')
    .description('Check your participation status in a task')
    .action(async (taskId: string) => {
      const config = loadConfig();
      if (!config.api_key) {
        console.error('No API key found. Run `acn join` first.');
        process.exit(1);
      }
      try {
        const res = await acnGet<{
          participation_id?: string;
          status?: string;
          joined_at?: string;
          submission?: string;
          submitted_at?: string;
        } | null>(`/tasks/${taskId}/participations/me`);
        if (!res || !res.participation_id) {
          output(res, `Not participating in task ${taskId}.`);
          return;
        }
        const lines = [
          `Participation  : ${res.participation_id}`,
          `Status         : ${res.status ?? '?'}`,
          ...(res.joined_at ? [`Joined         : ${res.joined_at}`] : []),
          ...(res.submission ? [`Submission     : ${res.submission.slice(0, 200)}`] : []),
          ...(res.submitted_at ? [`Submitted at   : ${res.submitted_at}`] : []),
        ];
        output(res, lines.join('\n'));
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('approve-applicant <task_id>')
    .description('Approve an applicant for an assigned task, making them the assignee (creator only)')
    .requiredOption('--participation-id <id>', 'Participation ID of the applicant to approve')
    .action(async (taskId: string, opts: { participationId: string }) => {
      const config = loadConfig();
      if (!config.api_key) {
        console.error('No API key found. Run `acn join` first.');
        process.exit(1);
      }
      try {
        const res = await acnPost<TaskInfo>(
          `/tasks/${taskId}/participations/${opts.participationId}/approve`
        );
        output(res, `Approved applicant ${opts.participationId} for task ${taskId} (status: ${res.status})`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('reject-applicant <task_id>')
    .description('Reject an applicant for an assigned task (creator only)')
    .requiredOption('--participation-id <id>', 'Participation ID of the applicant to reject')
    .action(async (taskId: string, opts: { participationId: string }) => {
      const config = loadConfig();
      if (!config.api_key) {
        console.error('No API key found. Run `acn join` first.');
        process.exit(1);
      }
      try {
        const res = await acnPost<TaskInfo>(
          `/tasks/${taskId}/participations/${opts.participationId}/reject`
        );
        output(res, `Rejected applicant ${opts.participationId} for task ${taskId} (status: ${res.status})`);
      } catch (err) {
        handleError(err);
      }
    });

  return cmd;
}
