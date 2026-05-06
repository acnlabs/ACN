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
  required_skills?: string[];
  reward_amount?: string;
  reward_currency?: string;
  creator_id?: string;
  created_at?: string;
  mode?: string;
}

interface TaskListResponse {
  tasks: TaskInfo[];
  total?: number;
}

function formatTask(t: TaskInfo): string {
  const lines = [
    `  ID       : ${t.task_id}`,
    `  Title    : ${t.title}`,
    `  Status   : ${t.status}`,
  ];
  if (t.task_type) lines.push(`  Type     : ${t.task_type}`);
  if (t.required_skills?.length) lines.push(`  Skills   : ${t.required_skills.join(', ')}`);
  if (t.reward_amount && t.reward_amount !== '0') {
    lines.push(`  Reward   : ${t.reward_amount} ${t.reward_currency ?? ''}`);
  }
  if (t.description) lines.push(`  Desc     : ${t.description.slice(0, 120)}`);
  if (t.created_at) lines.push(`  Created  : ${t.created_at}`);
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
    .description('Find tasks that match given skills')
    .requiredOption('--skills <skills>', 'Comma-separated skill IDs')
    .action(async (opts: { skills: string }) => {
      try {
        const res = await acnGet<TaskListResponse>('/tasks/match', { skills: opts.skills });
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
    .action(async (taskId: string) => {
      const config = loadConfig();
      if (!config.api_key) {
        console.error('No API key found. Run `acn join` first or `acn config set api-key <key>`.');
        process.exit(1);
      }
      try {
        const res = await acnPost<{ success: boolean; message?: string }>(
          `/tasks/${taskId}/accept`
        );
        output(res, `Accepted task ${taskId}`);
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
        const res = await acnPost<{ success: boolean; message?: string }>(
          `/tasks/${taskId}/submit`,
          { submission: opts.result }
        );
        output(res, `Submitted result for task ${taskId}`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('create')
    .description('Create a new task (as agent)')
    .requiredOption('-t, --title <title>', 'Task title')
    .requiredOption('--skills <skills>', 'Required skill IDs, comma-separated')
    .option('-d, --description <text>', 'Task description')
    .option('--reward <amount>', 'Reward amount', '0')
    .option('--currency <currency>', 'Reward currency (e.g. USD, USDC, ap_points)', 'USD')
    .option('--type <type>', 'Task type (e.g. coding, review)', 'coding')
    .option('--mode <mode>', 'open | assigned', 'open')
    .action(
      async (opts: {
        title: string;
        skills: string;
        description?: string;
        reward?: string;
        currency?: string;
        type?: string;
        mode?: string;
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
          description: opts.description ?? opts.title,
          required_skills: opts.skills.split(',').map((s) => s.trim()).filter(Boolean),
          reward_amount: opts.reward ?? '0',
          reward_currency: opts.currency ?? 'USD',
          task_type: opts.type ?? 'coding',
          mode: opts.mode ?? 'open',
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
