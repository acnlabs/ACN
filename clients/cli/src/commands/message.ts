import { Command } from 'commander';
import { acnPost } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';

function requireApiKey(): void {
  const config = loadConfig();
  if (!config.api_key) {
    console.error('No API key found. Run `acn join` first or `acn config set api-key <key>`.');
    process.exit(1);
  }
}

export function messageCommand(): Command {
  const cmd = new Command('message').description('Send messages to agents on ACN');

  cmd
    .command('send <agent_id>')
    .description('Send a direct message to a specific agent')
    .requiredOption('-t, --text <text>', 'Message text')
    .option('--type <type>', 'Message type: text | data | notification | task | result', 'text')
    .action(async (agentId: string, opts: { text: string; type?: string }) => {
      requireApiKey();
      try {
        const res = await acnPost<{ success: boolean; message_id?: string }>(
          '/messages/send',
          {
            target_agent_id: agentId,
            message: opts.text,
            message_type: opts.type ?? 'text',
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
      requireApiKey();
      try {
        let res: { success: boolean; delivered_count?: number };
        if (opts.skill) {
          res = await acnPost('/messages/broadcast/skill', {
            message: opts.text,
            skill: opts.skill,
            strategy: opts.strategy ?? 'parallel',
          });
        } else {
          res = await acnPost('/messages/broadcast', {
            message: opts.text,
            strategy: opts.strategy ?? 'parallel',
          });
        }
        output(
          res,
          `Broadcast sent. Delivered to ${res.delivered_count ?? '?'} agent(s).`
        );
      } catch (err) {
        handleError(err);
      }
    });

  return cmd;
}
