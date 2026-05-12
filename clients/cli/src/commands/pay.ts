import { Command } from 'commander';
import { acnGet, acnPost } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';

interface CreatePaymentTaskResponse {
  task_id: string;
  status: string;
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
  return config.agent_id;
}

export function payCommand(): Command {
  const cmd = new Command('pay').description('Manage payment tasks between agents');

  // ── sub-command: create ──────────────────────────────────────────────
  const createCmd = new Command('create').description('Create a payment task to another agent');
  createCmd
    .requiredOption('--to <agent>', 'Recipient agent ID')
    .requiredOption('--amount <n>', 'Payment amount (positive number)')
    .requiredOption('--currency <c>', 'Currency code, e.g. USD, USDC')
    .requiredOption('--method <m>', 'Payment method, e.g. usdc, eth, platform_credits')
    .requiredOption('--network <n>', 'Network, e.g. ethereum, base, solana')
    .option('--description <text>', 'Free-text description for the payment task')
    .option('--metadata <json>', 'Additional metadata as JSON object')
    .action(
      async (opts: {
        to: string;
        amount: string;
        currency: string;
        method: string;
        network: string;
        description?: string;
        metadata?: string;
      }) => {
        const fromAgent = requireAgentId();

        const amount = Number(opts.amount);
        if (!Number.isFinite(amount) || amount <= 0) {
          console.error('--amount must be a positive number.');
          process.exit(1);
        }

        let metadata: Record<string, unknown> | undefined;
        if (opts.metadata) {
          try {
            const parsed = JSON.parse(opts.metadata);
            if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
              throw new Error('--metadata must be a JSON object');
            }
            metadata = parsed as Record<string, unknown>;
          } catch (err) {
            console.error(
              `Invalid --metadata JSON: ${err instanceof Error ? err.message : String(err)}`
            );
            process.exit(1);
          }
        }

        const body: Record<string, unknown> = {
          from_agent: fromAgent,
          to_agent: opts.to,
          amount,
          currency: opts.currency,
          payment_method: opts.method,
          network: opts.network,
        };
        if (opts.description !== undefined) body.description = opts.description;
        if (metadata !== undefined) body.metadata = metadata;

        try {
          const res = await acnPost<CreatePaymentTaskResponse>('/payments/tasks', body);
          output(
            res,
            `Payment task ${res.status}: ${res.task_id}\n` +
              `  ${fromAgent} -> ${opts.to}: ${amount} ${opts.currency} via ${opts.method}/${opts.network}`
          );
        } catch (err) {
          handleError(err);
        }
      }
    );

  // ── sub-command: confirm ─────────────────────────────────────────────
  const confirmCmd = new Command('confirm').description(
    'Confirm an external payment has been made (buyer only)'
  );
  confirmCmd
    .requiredOption('--task-id <id>', 'Payment task ID to confirm')
    .requiredOption(
      '--tx-hash <hash>',
      'On-chain transaction hash or external payment reference (e.g. Stripe charge ID)'
    )
    .action(async (opts: { taskId: string; txHash: string }) => {
      try {
        const res = await acnPost<{ task_id: string; status: string; tx_hash: string }>(
          `/payments/tasks/${opts.taskId}/confirm`,
          { tx_hash: opts.txHash }
        );
        output(
          res,
          `Payment confirmed: ${res.task_id}\n` +
            `  status : ${res.status}\n` +
            `  tx_hash: ${res.tx_hash}`
        );
      } catch (err) {
        handleError(err);
      }
    });

  // ── sub-command: status ──────────────────────────────────────────────
  const statusCmd = new Command('status').description(
    'Show payment tasks for the authenticated agent'
  );
  statusCmd
    .option('--status <s>', 'Filter by status (e.g. created, payment_confirmed)')
    .option('--limit <n>', 'Max results (default 50)', '50')
    .action(async (opts: { status?: string; limit: string }) => {
      const agentId = requireAgentId();
      try {
        const res = await acnGet<{ agent_id: string; tasks: unknown[] }>(
          `/payments/tasks/agent/${agentId}`,
          { status: opts.status, limit: Number(opts.limit) }
        );
        output(res, `${res.tasks.length} payment task(s) for ${res.agent_id}`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd.addCommand(createCmd);
  cmd.addCommand(confirmCmd);
  cmd.addCommand(statusCmd);

  return cmd;
}
