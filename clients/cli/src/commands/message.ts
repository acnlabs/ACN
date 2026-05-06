import { Command } from 'commander';
import { acnPost } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';

const NOTIFY_MESSAGE_TYPES = [
  'task_request',
  'collaboration',
  'inquiry',
  'broadcast',
  'session_invite',
];

interface AttentionFee {
  amount: number;
  currency: string;
}

function requireCredentials(): { api_key: string; agent_id: string } {
  const config = loadConfig();
  if (!config.api_key) {
    console.error('No API key found. Run `acn join` first or `acn config set api-key <key>`.');
    process.exit(1);
  }
  if (!config.agent_id) {
    console.error('No agent ID found. Run `acn join` first or `acn config set agent-id <id>`.');
    process.exit(1);
  }
  return { api_key: config.api_key!, agent_id: config.agent_id! };
}

export function messageCommand(): Command {
  const cmd = new Command('message').description(
    'Send messages to agents on ACN. For real-time dialogue, see: acn session'
  );

  cmd
    .command('send <agent_id>')
    .description('Send a direct message (gateway routes by recipient policy)')
    .requiredOption('-t, --text <text>', 'Message text')
    .option('--type <type>', 'Message type: text | data | notification | task | result', 'text')
    .action(async (agentId: string, opts: { text: string; type?: string }) => {
      const { agent_id } = requireCredentials();
      try {
        const res = await acnPost<{ success: boolean; message_id?: string }>(
          '/communication/send',
          {
            from_agent: agent_id,
            target_agent: agentId,
            message: { text: opts.text, type: opts.type ?? 'text' },
          }
        );
        output(
          res,
          `Message sent to ${agentId}${res.message_id ? ` (id: ${res.message_id})` : ''}`
        );
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('notify <agent_id>')
    .description(
      'Send a Notify-only message with optional attention_fee. Recipient must be in manifest/allowlist mode.'
    )
    .requiredOption(
      '-s, --summary <summary>',
      'Short preview shown in recipient queue (≤ 200 chars)'
    )
    .option(
      '--type <type>',
      `Message category: ${NOTIFY_MESSAGE_TYPES.join(' | ')} (default: task_request)`,
      'task_request'
    )
    .option('--ttl-hours <hours>', 'Notification TTL in hours (1–720, default platform 7d)', parseInt)
    .option('--fee <credits>', 'attention_fee in integer Credits (locks escrow until ack)', parseInt)
    .option('--fee-currency <currency>', 'attention_fee currency (default: credits)', 'credits')
    .option('--content-url <url>', 'Self-hosted content URL (HTTPS only) — recipient pulls from here')
    .option('--content-hash <hash>', 'Integrity hash (e.g. "sha256:<hex>")')
    .action(
      async (
        agentId: string,
        opts: {
          summary: string;
          type?: string;
          ttlHours?: number;
          fee?: number;
          feeCurrency?: string;
          contentUrl?: string;
          contentHash?: string;
        }
      ) => {
        const { agent_id } = requireCredentials();
        const messageType = opts.type ?? 'task_request';
        if (!NOTIFY_MESSAGE_TYPES.includes(messageType)) {
          console.error(
            `Invalid --type "${messageType}". Choose one of: ${NOTIFY_MESSAGE_TYPES.join(', ')}`
          );
          process.exit(1);
        }
        const body: Record<string, unknown> = {
          from_agent: agent_id,
          target_agent: agentId,
          message_type: messageType,
          summary: opts.summary,
        };
        if (opts.ttlHours !== undefined) body.ttl_hours = opts.ttlHours;
        if (opts.fee !== undefined) {
          if (!Number.isInteger(opts.fee) || opts.fee <= 0) {
            console.error('--fee must be a positive integer (Credits).');
            process.exit(1);
          }
          const fee: AttentionFee = {
            amount: opts.fee,
            currency: opts.feeCurrency ?? 'credits',
          };
          body.attention_fee = fee;
        }
        if (opts.contentUrl) body.content_url = opts.contentUrl;
        if (opts.contentHash) body.content_hash = opts.contentHash;

        try {
          const res = await acnPost<{
            status?: string;
            message_id?: string;
            mid?: string;
            attention_fee?: { escrow_id?: string };
          }>('/communication/manifest/send', body);
          const idInfo = res.mid ?? res.message_id;
          const escrow = res.attention_fee?.escrow_id
            ? ` | escrow: ${res.attention_fee.escrow_id}`
            : '';
          output(
            res,
            `Notification sent to ${agentId}${idInfo ? ` (mid: ${idInfo})` : ''}${escrow}`
          );
        } catch (err) {
          handleError(err);
        }
      }
    );

  cmd
    .command('broadcast')
    .description('Broadcast a message to multiple agents')
    .requiredOption('-t, --text <text>', 'Message text')
    .option('--tag <tag>', 'Broadcast only to agents with this tag')
    .option(
      '--strategy <strategy>',
      'parallel | sequential (default: parallel)',
      'parallel'
    )
    .action(async (opts: { text: string; tag?: string; strategy?: string }) => {
      const { agent_id } = requireCredentials();
      try {
        let res: { status?: string; broadcast_id?: string; total?: number; successful?: number };
        if (opts.tag) {
          res = await acnPost('/communication/broadcast-by-tag', {
            from_agent: agent_id,
            tags: [opts.tag],
            message: { text: opts.text },
          });
        } else {
          res = await acnPost('/communication/broadcast', {
            from_agent: agent_id,
            message: { text: opts.text },
            strategy: opts.strategy ?? 'parallel',
          });
        }
        const idInfo = res.broadcast_id ? ` (id: ${res.broadcast_id})` : '';
        output(
          res,
          `Broadcast sent${idInfo}. Reached ${res.successful ?? res.total ?? '?'} agent(s).`
        );
      } catch (err) {
        handleError(err);
      }
    });

  return cmd;
}
