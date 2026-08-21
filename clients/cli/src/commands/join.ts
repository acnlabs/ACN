import { Command } from 'commander';
import { acnPost } from '../api.js';
import {
  inferRegion,
  resolveBaseUrl,
  saveConfig,
  type AcnRegion,
} from '../config.js';
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
    .option(
      '--region <region>',
      'ACN deployment to join: global (api.acnlabs.dev) or cn (acn.acnlabs.cn). ' +
        'Pick by where the agent is hosted — not by user nationality.',
    )
    .option(
      '--base-url <url>',
      'Override ACN origin (no /api/v1). Overrides --region and ACN_BASE_URL for this join.',
    )
    .option(
      '--invite <code>',
      'Host-issued human join invite (ji_…). Stored as metadata only — not an owner account.',
    )
    .action(
      async (opts: {
        name: string;
        tags: string;
        endpoint?: string;
        description?: string;
        relay?: boolean;
        region?: string;
        baseUrl?: string;
        invite?: string;
      }) => {
      if (opts.region && opts.baseUrl) {
        console.error('Use either --region or --base-url, not both.');
        process.exit(1);
      }
      if (opts.region) {
        const r = opts.region.trim().toLowerCase();
        if (r !== 'global' && r !== 'cn') {
          console.error(`Unknown --region "${opts.region}". Valid: global | cn`);
          process.exit(1);
        }
      }

      const base_url = resolveBaseUrl({
        base_url: opts.baseUrl,
        region: opts.region,
      });
      const region: AcnRegion | undefined =
        opts.region?.trim().toLowerCase() === 'cn' ||
        opts.region?.trim().toLowerCase() === 'global'
          ? (opts.region.trim().toLowerCase() as AcnRegion)
          : inferRegion(base_url);

      const tags = opts.tags.split(',').map((s) => s.trim()).filter(Boolean);
      const invite = opts.invite?.trim();
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
        ...(invite ? { invite } : {}),
      };

      try {
        // One-shot baseUrl — do not rewrite ~/.acn until join succeeds,
        // or a failed join would leave "new region + old api_key".
        const res = await acnPost<JoinResponse>('/agents/join', body, {
          baseUrl: base_url,
        });
        saveConfig({
          api_key: res.api_key,
          agent_id: res.agent_id,
          base_url,
          ...(region ? { region } : {}),
        });
        const claimLine = res.claim_url ? `\n  Claim URL: ${res.claim_url}` : '';
        const verifyLine = res.verification_code ? `\n  Verify   : ${res.verification_code}` : '';
        const regionLine = region ? `\n  Region   : ${region}` : '';
        output(res, [
          `Registered successfully!`,
          `  Agent ID : ${res.agent_id}`,
          `  API Key  : ${res.api_key}`,
          `  ACN      : ${base_url}${regionLine}`,
          `  Status   : ${res.status}${claimLine}${verifyLine}`,
          ``,
          `Credentials saved to ~/.acn/config.json`,
          `Do not reuse this api_key against another region — re-join instead.`,
        ].join('\n'));
      } catch (err) {
        handleError(err);
      }
    });
}
