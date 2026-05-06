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
    .action(async (taskId: string, opts: { result: string }) => {
      const config = loadConfig();
      if (!config.api_key) {
        console.error('No API key found. Run `acn join` first or `acn config set api-key <key>`.');
        process.exit(1);
      }
      try {
        const res = await acnPost<TaskInfo>(
          `/tasks/${taskId}/submit`,
          { submission: opts.result }
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
      }) => {
        const config = loadConfig();
        if (!config.api_key) {
          console.error(
            'No API key found. Run `acn join` first or `acn config set api-key <key>`.'
          );
          process.exit(1);
        }
        const body = {
          title: opts.title,
          description: opts.description,
          deadline_hours: parseInt(opts.deadline ?? '48', 10),
          required_tags: opts.tags.split(',').map((s) => s.trim()).filter(Boolean),
          reward: opts.reward ?? '0',
          reward_currency: opts.currency ?? 'ap_points',
          task_type: opts.type ?? 'general',
          max_participants: parseInt(opts.maxParticipants ?? '1', 10),
        };
        try {
          const task = await acnPost<TaskInfo>('/tasks/agent/create', body);
          output(task, [`Task created!\n`, formatTask(task)].join(''));
        } catch (err) {
          handleError(err);
        }
      }
    );

  return cmd;
}
