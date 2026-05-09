import { Command } from 'commander';
import { acnGet, acnPost } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';

interface PaymentTaskSummary {
  task_id: string;
  status: string;
  buyer_agent: string;
  seller_agent: string;
  amount: string;
  currency?: string;
  payment_method?: string | null;
  network?: string | null;
  created_at?: string;
}

interface AgentPaymentTasksResponse {
  agent_id: string;
  tasks: PaymentTaskSummary[];
}

interface PaymentRoleStats {
  count: number;
  total_amount: string;
}

interface PaymentStatsResponse {
  total_tasks?: number;
  as_buyer?: PaymentRoleStats;
  as_seller?: PaymentRoleStats;
  by_status?: Record<string, number>;
  completed_transactions?: number;
  [key: string]: unknown;
}

interface EstimateCostResponse {
  agent_id: string;
  estimate: {
    input_tokens?: number;
    output_tokens?: number;
    total_usd?: number;
    network_fee_usd?: number;
    agent_income_usd?: number;
    total_credits?: number;
    network_fee_credits?: number;
    agent_income_credits?: number;
    [key: string]: unknown;
  };
  note?: string;
}

interface AgentWalletsResponse {
  agent_id: string;
  accepts_payment?: boolean;
  payment_methods?: string[];
  wallet_addresses?: Record<string, string>;
  platform_credits_id?: string;
  token_pricing?: unknown;
  erc8004?: {
    token_id?: string;
    chain?: string;
    tx_hash?: string;
    registered_at?: string;
  } | null;
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

function parseCsv(value: string): string[] {
  return value
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

async function showWalletInfo(opts: { agentId?: string }): Promise<void> {
  const agentId = opts.agentId ?? requireAgentId();
  try {
    const res = await acnGet<AgentWalletsResponse>(`/agents/${agentId}/wallets`);
    const lines: string[] = [`Agent  : ${res.agent_id}`];
    if (res.accepts_payment !== undefined)
      lines.push(`Accepts payment : ${res.accepts_payment}`);
    if (res.payment_methods?.length)
      lines.push(`Methods : ${res.payment_methods.join(', ')}`);
    if (res.wallet_addresses && Object.keys(res.wallet_addresses).length) {
      lines.push('Wallets :');
      for (const [chain, addr] of Object.entries(res.wallet_addresses)) {
        lines.push(`  ${chain.padEnd(10)}: ${addr}`);
      }
    }
    if (res.erc8004) {
      lines.push(`ERC-8004: token_id=${res.erc8004.token_id} chain=${res.erc8004.chain}`);
    }
    if (res.token_pricing) {
      lines.push(`Pricing : ${JSON.stringify(res.token_pricing)}`);
    }
    output(res, lines.join('\n'));
  } catch (err) {
    handleError(err);
  }
}

export function walletCommand(): Command {
  const cmd = new Command('wallet').description("View and manage agent's wallet & payment info");

  cmd
    .command('info', { isDefault: true })
    .description('Show wallet, payment methods, pricing, and ERC-8004 status')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(showWalletInfo);

  cmd
    .command('set-capability')
    .description('Declare which payment methods, networks, and wallets you accept')
    .requiredOption(
      '--methods <csv>',
      'Supported payment methods, e.g. usdc,eth,platform_credits'
    )
    .requiredOption('--networks <csv>', 'Supported networks, e.g. ethereum,base')
    .option(
      '--wallets <json>',
      'Wallet addresses by network, JSON, e.g. \'{"ethereum":"0x..."}\''
    )
    .option('--no-accepts', 'Disable accepting payments (default: enabled)')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(
      async (opts: {
        methods: string;
        networks: string;
        wallets?: string;
        accepts?: boolean;
        agentId?: string;
      }) => {
        const agentId = opts.agentId ?? requireAgentId();
        let walletMap: Record<string, string> = {};
        if (opts.wallets) {
          try {
            const parsed = JSON.parse(opts.wallets);
            if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
              throw new Error('--wallets must be a JSON object');
            }
            walletMap = parsed as Record<string, string>;
          } catch (err) {
            console.error(
              `Invalid --wallets JSON: ${err instanceof Error ? err.message : String(err)}`
            );
            process.exit(1);
          }
        }
        const body = {
          supported_methods: parseCsv(opts.methods),
          supported_networks: parseCsv(opts.networks),
          wallet_addresses: walletMap,
          accepts_payment: opts.accepts !== false,
        };
        try {
          const res = await acnPost<{ status: string; agent_id: string }>(
            `/payments/${agentId}/payment-capability`,
            body
          );
          output(res, `Payment capability ${res.status} for ${res.agent_id}`);
        } catch (err) {
          handleError(err);
        }
      }
    );

  cmd
    .command('set-pricing')
    .description('Set OpenAI-style per-million-token pricing for your agent (USD)')
    .requiredOption('--input <usd>', 'USD per 1M input tokens')
    .requiredOption('--output <usd>', 'USD per 1M output tokens')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(
      async (opts: { input: string; output: string; agentId?: string }) => {
        const agentId = opts.agentId ?? requireAgentId();
        const inputPrice = Number(opts.input);
        const outputPrice = Number(opts.output);
        if (!Number.isFinite(inputPrice) || inputPrice < 0) {
          console.error('--input must be a non-negative number.');
          process.exit(1);
        }
        if (!Number.isFinite(outputPrice) || outputPrice < 0) {
          console.error('--output must be a non-negative number.');
          process.exit(1);
        }
        const body = {
          input_price_per_million: inputPrice,
          output_price_per_million: outputPrice,
        };
        try {
          const res = await acnPost<{
            status: string;
            agent_id: string;
            token_pricing: { input_price_per_million: number; output_price_per_million: number; currency: string };
            network_fee_rate?: number;
          }>(`/payments/${agentId}/token-pricing`, body);
          const fee = res.network_fee_rate !== undefined
            ? `, network fee rate ${res.network_fee_rate}`
            : '';
          output(
            res,
            `Pricing ${res.status} for ${res.agent_id}: ` +
              `input=$${res.token_pricing.input_price_per_million}/1M, ` +
              `output=$${res.token_pricing.output_price_per_million}/1M ` +
              `(${res.token_pricing.currency})${fee}`
          );
        } catch (err) {
          handleError(err);
        }
      }
    );

  cmd
    .command('tasks')
    .description("List the payment tasks the current agent is involved in")
    .option('--status <s>', 'Filter by status (e.g. created, payment_confirmed, task_completed)')
    .option('--limit <n>', 'Max number of tasks to return (default 50)')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (opts: { status?: string; limit?: string; agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      const params: Record<string, string | number | undefined> = {};
      if (opts.status) params.status = opts.status;
      if (opts.limit !== undefined) {
        const limit = Number(opts.limit);
        if (!Number.isFinite(limit) || limit <= 0) {
          console.error('--limit must be a positive number.');
          process.exit(1);
        }
        params.limit = limit;
      }
      try {
        const res = await acnGet<AgentPaymentTasksResponse>(
          `/payments/tasks/agent/${agentId}`,
          params
        );
        const tasks = res.tasks ?? [];
        if (tasks.length === 0) {
          output(res, `No payment tasks for ${agentId}.`);
          return;
        }
        const lines = [
          `${tasks.length} task(s) for ${agentId}:`,
          `  ${'TASK_ID'.padEnd(36)}  ROLE  ${'STATUS'.padEnd(20)}  AMOUNT  VIA  COUNTERPARTY`,
        ];
        for (const t of tasks) {
          const role = t.buyer_agent === agentId ? 'pay ' : 'recv';
          const counterparty = t.buyer_agent === agentId ? t.seller_agent : t.buyer_agent;
          const method = t.payment_method ? `${t.payment_method}` : '?';
          const network = t.network ? `/${t.network}` : '';
          lines.push(
            `  ${t.task_id.padEnd(36)}  ${role}  ${t.status.padEnd(20)}  ` +
              `${t.amount} ${t.currency ?? ''}  via ${method}${network}  -> ${counterparty}`
          );
        }
        output(res, lines.join('\n'));
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('stats')
    .description("Show the current agent's payment statistics")
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (opts: { agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const res = await acnGet<PaymentStatsResponse>(`/payments/stats/${agentId}`);
        const lines = [`Stats for ${agentId}:`];
        if (res.total_tasks !== undefined)
          lines.push(`  Total tasks            : ${res.total_tasks}`);
        if (res.completed_transactions !== undefined)
          lines.push(`  Completed transactions : ${res.completed_transactions}`);
        if (res.as_buyer) {
          lines.push(
            `  As buyer               : ${res.as_buyer.count} task(s), ` +
              `total ${res.as_buyer.total_amount}`
          );
        }
        if (res.as_seller) {
          lines.push(
            `  As seller              : ${res.as_seller.count} task(s), ` +
              `total ${res.as_seller.total_amount}`
          );
        }
        if (res.by_status && Object.keys(res.by_status).length) {
          lines.push('  By status:');
          const entries = Object.entries(res.by_status).sort((a, b) => b[1] - a[1]);
          for (const [status, count] of entries) {
            lines.push(`    ${status.padEnd(20)} ${count}`);
          }
        }
        output(res, lines.join('\n'));
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('estimate <agent_id>')
    .description("Estimate the cost of calling another agent before invoking")
    .requiredOption('--input-tokens <n>', 'Estimated input token count')
    .requiredOption('--output-tokens <n>', 'Estimated output token count')
    .action(
      async (
        agentId: string,
        opts: { inputTokens: string; outputTokens: string }
      ) => {
        const inputTokens = Number(opts.inputTokens);
        const outputTokens = Number(opts.outputTokens);
        if (!Number.isFinite(inputTokens) || inputTokens < 0) {
          console.error('--input-tokens must be a non-negative number.');
          process.exit(1);
        }
        if (!Number.isFinite(outputTokens) || outputTokens < 0) {
          console.error('--output-tokens must be a non-negative number.');
          process.exit(1);
        }
        try {
          const res = await acnPost<EstimateCostResponse>('/payments/billing/estimate', {
            agent_id: agentId,
            estimated_input_tokens: inputTokens,
            estimated_output_tokens: outputTokens,
          });
          const e = res.estimate ?? {};
          const lines = [
            `Estimate for ${res.agent_id} (${inputTokens} in / ${outputTokens} out tokens):`,
            `  Total           : $${e.total_usd ?? '?'}  (${e.total_credits ?? '?'} credits)`,
            `  Network fee     : $${e.network_fee_usd ?? '?'}  (${e.network_fee_credits ?? '?'} credits)`,
            `  Agent income    : $${e.agent_income_usd ?? '?'}  (${e.agent_income_credits ?? '?'} credits)`,
          ];
          if (res.note) lines.push(`  Note: ${res.note}`);
          output(res, lines.join('\n'));
        } catch (err) {
          handleError(err);
        }
      }
    );

  return cmd;
}
