import { Command } from 'commander';
import { acnPost } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';

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
  const cmd = new Command('message').description('Send messages to agents on ACN');

  cmd
    .command('send <agent_id>')
    .description('Send a direct message to a specific agent')
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
        output(res, `Message sent to ${agentId}${res.message_id ? ` (id: ${res.message_id})` : ''}`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('broadcast')
    .description('Broadcast a message to multiple agents')
    .requiredOption('-t, --text <text>', 'Message text')
    .option('--skill <skill>', 'Broadcast only to agents with this skill')
    .option(
      '--strategy <strategy>',
      'parallel | sequential (default: parallel)',
      'parallel'
    )
    .action(async (opts: { text: string; skill?: string; strategy?: string }) => {
      const { agent_id } = requireCredentials();
      try {
        let res: { status?: string; broadcast_id?: string; total?: number; successful?: number };
        if (opts.skill) {
          res = await acnPost('/communication/broadcast-by-tag', {
            from_agent: agent_id,
            tags: [opts.skill],
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
