import { Command } from 'commander';
import { acnGet } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';

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

export function walletCommand(): Command {
  return new Command('wallet')
    .description("View agent's wallet and payment info")
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (opts: { agentId?: string }) => {
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
    });
}
