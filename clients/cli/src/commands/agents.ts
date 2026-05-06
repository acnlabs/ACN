import { Command } from 'commander';
import { acnGet } from '../api.js';
import { output, handleError } from '../output.js';

interface AgentInfo {
  agent_id: string;
  name: string;
  status: string;
  skills: string[];
  description?: string;
  endpoint?: string;
  created_at?: string;
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
    `  Skills   : ${a.skills.join(', ') || '(none)'}`,
    ...(a.description ? [`  Desc     : ${a.description}`] : []),
    ...(a.endpoint ? [`  Endpoint : ${a.endpoint}`] : []),
  ].join('\n');
}

export function agentsCommand(): Command {
  const cmd = new Command('agents').description('Discover and inspect agents on ACN');

  cmd
    .command('list')
    .description('List agents')
    .option('--skill <skill>', 'Filter by skill ID')
    .option('--name <name>', 'Filter by name')
    .option('--status <status>', 'online | offline | all (default: online)', 'online')
    .action(async (opts: { skill?: string; name?: string; status?: string }) => {
      try {
        const res = await acnGet<AgentSearchResponse>('/agents', {
          skill: opts.skill,
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

  return cmd;
}
