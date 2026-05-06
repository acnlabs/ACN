import { Command } from 'commander';
import { acnPost } from '../api.js';
import { saveConfig } from '../config.js';
import { output, handleError } from '../output.js';

interface JoinResponse {
  agent_id: string;
  api_key: string;
  status: string;
  agent_card_url?: string;
}

export function joinCommand(): Command {
  return new Command('join')
    .description('Register this agent with ACN and save credentials locally')
    .requiredOption('-n, --name <name>', 'Agent name')
    .requiredOption('-s, --skills <skills>', 'Comma-separated skill IDs (e.g. coding,review)')
    .option('-e, --endpoint <url>', 'Public A2A endpoint URL of this agent')
    .option('-d, --description <text>', 'Agent description')
    .action(async (opts: { name: string; skills: string; endpoint?: string; description?: string }) => {
      const skills = opts.skills.split(',').map((s) => s.trim()).filter(Boolean);
      const body = {
        name: opts.name,
        description: opts.description ?? `${opts.name} — registered via acn-cli`,
        skills,
        ...(opts.endpoint ? { endpoint: opts.endpoint } : {}),
      };

      try {
        const res = await acnPost<JoinResponse>('/agents/join', body);
        saveConfig({ api_key: res.api_key, agent_id: res.agent_id });
        output(res, [
          `Registered successfully!`,
          `  Agent ID : ${res.agent_id}`,
          `  API Key  : ${res.api_key}`,
          `  Status   : ${res.status}`,
          ``,
          `Credentials saved to ~/.acn/config.json`,
        ].join('\n'));
      } catch (err) {
        handleError(err);
      }
    });
}
