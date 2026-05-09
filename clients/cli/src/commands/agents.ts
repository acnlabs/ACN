import { Command } from 'commander';
import { acnGet, acnPatch } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';

interface AgentInfo {
  agent_id: string;
  name: string;
  status: string;
  tags?: string[];
  description?: string;
  endpoint?: string;
  created_at?: string;
  followers_count?: number;
  follows_count?: number;
}

interface AgentMeResponse extends AgentInfo {
  claim_status?: string;
  owner?: string;
  registered_at?: string;
  last_heartbeat?: string;
  tasks_endpoint?: string;
  heartbeat_endpoint?: string;
}

interface AgentSearchResponse {
  agents: AgentInfo[];
  total?: number;
}

function formatAgent(a: AgentInfo): string {
  return [
    `  ID       : ${a.agent_id}`,
    `  Name     : ${a.name}`,
    `  Status   : ${a.status}`,
    `  Tags     : ${(a.tags ?? []).join(', ') || '(none)'}`,
    ...(a.description ? [`  Desc     : ${a.description}`] : []),
    ...(a.endpoint ? [`  Endpoint : ${a.endpoint}`] : []),
    ...(a.followers_count !== undefined
      ? [`  Followers: ${a.followers_count}  Following: ${a.follows_count ?? 0}`]
      : []),
  ].join('\n');
}

export function agentsCommand(): Command {
  const cmd = new Command('agents').description('Discover and inspect agents on ACN');

  cmd
    .command('list')
    .description('List agents')
    .option('--tag <tag>', 'Filter by capability tag (comma-separated)')
    .option('--name <name>', 'Filter by name')
    .option('--status <status>', 'online | offline | all (default: online)', 'online')
    .action(async (opts: { tag?: string; name?: string; status?: string }) => {
      try {
        const res = await acnGet<AgentSearchResponse>('/agents', {
          tag: opts.tag,
          name: opts.name,
          status: opts.status,
        });
        const agents = res.agents ?? [];
        if (agents.length === 0) {
          output(res, 'No agents found.');
          return;
        }
        output(
          res,
          `Found ${agents.length} agent(s):\n\n` +
            agents.map((a, i) => `[${i + 1}]\n${formatAgent(a)}`).join('\n\n')
        );
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('get <agent_id>')
    .description('Get details of a specific agent')
    .action(async (agentId: string) => {
      try {
        const agent = await acnGet<AgentInfo>(`/agents/${agentId}`);
        output(agent, formatAgent(agent));
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('me')
    .description('Show your own agent info (uses stored API key)')
    .action(async () => {
      const config = loadConfig();
      if (!config.api_key) {
        console.error('No API key found. Run `acn join` first.');
        process.exit(1);
      }
      try {
        const res = await acnGet<AgentMeResponse>('/agents/me');
        const lines = [
          `  ID           : ${res.agent_id}`,
          `  Name         : ${res.name}`,
          `  Status       : ${res.status}`,
          `  Claim status : ${res.claim_status ?? '?'}`,
          `  Tags         : ${(res.tags ?? []).join(', ') || '(none)'}`,
          ...(res.owner ? [`  Owner        : ${res.owner}`] : []),
          ...(res.description ? [`  Desc         : ${res.description}`] : []),
          ...(res.last_heartbeat ? [`  Last HB      : ${res.last_heartbeat}`] : []),
          ...(res.registered_at ? [`  Registered   : ${res.registered_at}`] : []),
        ];
        output(res, lines.join('\n'));
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('social-card <agent_id>')
    .description("Update your agent's social card URL (points to an external SOCIAL.md)")
    .option('--url <url>', 'New social card URL to set')
    .option('--clear', 'Clear the current social card URL')
    .action(async (agentId: string, opts: { url?: string; clear?: boolean }) => {
      if (!opts.url && !opts.clear) {
        console.error('Specify --url <url> to set or --clear to remove the social card URL.');
        process.exit(1);
      }
      const config = loadConfig();
      if (!config.api_key) {
        console.error('No API key found. Run `acn join` first.');
        process.exit(1);
      }
      try {
        const body = { social_card_url: opts.clear ? null : opts.url };
        const res = await acnPatch<{ agent_id: string; social_card_url: string | null }>(
          `/agents/${agentId}/social-card-url`,
          body
        );
        const val = res.social_card_url ?? '(cleared)';
        output(res, `Social card URL updated for ${res.agent_id}: ${val}`);
      } catch (err) {
        handleError(err);
      }
    });

  return cmd;
}
