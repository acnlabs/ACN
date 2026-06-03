import { Command } from 'commander';
import { acnPost } from '../api.js';
import { saveConfig } from '../config.js';
import { output, handleError } from '../output.js';

interface JoinResponse {
  agent_id: string;
  api_key: string;
  status: string;
  claim_status?: string;
  verification_code?: string;
  claim_url?: string;
  referral_url?: string;
  agent_card_url?: string;
}

export function joinCommand(): Command {
  return new Command('join')
    .description('Register this agent with ACN and save credentials locally')
    .requiredOption('-n, --name <name>', 'Agent name')
    .requiredOption('-t, --tags <tags>', 'Comma-separated capability tags (e.g. coding,review)')
    .option('-e, --endpoint <url>', 'Public A2A endpoint URL of this agent')
    .option('-d, --description <text>', 'Agent description')
    .option(
      '--relay',
      'Receive messages in real time over an outbound WebSocket (run `acn listen`) ' +
        'instead of hosting a public endpoint. Registers in open/push mode with no delivery URL.',
    )
    .action(
      async (opts: {
        name: string;
        tags: string;
        endpoint?: string;
        description?: string;
        relay?: boolean;
      }) => {
      const tags = opts.tags.split(',').map((s) => s.trim()).filter(Boolean);
      const body = {
        name: opts.name,
        description: opts.description ?? `${opts.name} — registered via acn-cli`,
        tags,
        ...(opts.endpoint ? { endpoint: opts.endpoint } : {}),
        // ADR-0012 Mode B: relay delivery needs a push mode (open) so the
        // gateway actually pushes inbound messages down the WebSocket; the
        // server-side validator then waives the public-URL requirement.
        ...(opts.relay
          ? { delivery: 'relay', communication_policy: { mode: 'open' } }
          : {}),
      };

      try {
        const res = await acnPost<JoinResponse>('/agents/join', body);
        saveConfig({ api_key: res.api_key, agent_id: res.agent_id });
        const claimLine = res.claim_url ? `\n  Claim URL: ${res.claim_url}` : '';
        const verifyLine = res.verification_code ? `\n  Verify   : ${res.verification_code}` : '';
        output(res, [
          `Registered successfully!`,
          `  Agent ID : ${res.agent_id}`,
          `  API Key  : ${res.api_key}`,
          `  Status   : ${res.status}${claimLine}${verifyLine}`,
          ``,
          `Credentials saved to ~/.acn/config.json`,
        ].join('\n'));
      } catch (err) {
        handleError(err);
      }
    });
}
