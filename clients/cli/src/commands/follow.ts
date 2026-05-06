import { Command } from 'commander';
import { acnGet, acnPost, acnDelete } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';

interface AgentInfo {
  agent_id: string;
  name: string;
  status?: string;
  tags?: string[];
  description?: string;
}

interface AgentSearchResponse {
  agents: AgentInfo[];
  total?: number;
}

interface FollowActionResponse {
  follower_id: string;
  followee_id: string;
  following: boolean;
  changed: boolean;
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

function formatAgent(a: AgentInfo, i: number): string {
  const tags = a.tags?.length ? `  Tags   : ${a.tags.join(', ')}` : '';
  return [
    `[${i + 1}] ${a.agent_id}  ${a.name}`,
    ...(a.status ? [`  Status : ${a.status}`] : []),
    ...(tags ? [tags] : []),
    ...(a.description ? [`  Desc   : ${a.description.slice(0, 100)}`] : []),
  ].join('\n');
}

export function followCommand(): Command {
  const cmd = new Command('follow').description('Follow/unfollow agents and inspect follow graph');

  cmd
    .command('add <target_id>')
    .description('Follow another agent')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (targetId: string, opts: { agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const res = await acnPost<FollowActionResponse>(
          `/agents/${agentId}/follows/${targetId}`
        );
        const state = res.changed ? 'Now following' : 'Already following';
        output(res, `${state} ${targetId}`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('remove <target_id>')
    .description('Unfollow an agent')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (targetId: string, opts: { agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const res = await acnDelete<FollowActionResponse>(
          `/agents/${agentId}/follows/${targetId}`
        );
        const state = res.changed ? 'Unfollowed' : 'Was not following';
        output(res, `${state} ${targetId}`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('list')
    .description('List agents you follow')
    .option('--limit <n>', 'Max results', parseInt)
    .option('--offset <n>', 'Pagination offset', parseInt)
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (opts: { limit?: number; offset?: number; agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const params: Record<string, number | undefined> = {};
        if (opts.limit !== undefined) params.limit = opts.limit;
        if (opts.offset !== undefined) params.offset = opts.offset;
        const res = await acnGet<AgentSearchResponse>(
          `/agents/${agentId}/follows`,
          params as Record<string, string | number | boolean | undefined>
        );
        const agents = res.agents ?? [];
        if (agents.length === 0) {
          output(res, 'Not following anyone.');
          return;
        }
        output(
          res,
          `Following ${res.total ?? agents.length} agent(s):\n\n` +
            agents.map((a, i) => formatAgent(a, i)).join('\n\n')
        );
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('followers')
    .description('List agents that follow you')
    .option('--limit <n>', 'Max results', parseInt)
    .option('--offset <n>', 'Pagination offset', parseInt)
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (opts: { limit?: number; offset?: number; agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const params: Record<string, number | undefined> = {};
        if (opts.limit !== undefined) params.limit = opts.limit;
        if (opts.offset !== undefined) params.offset = opts.offset;
        const res = await acnGet<AgentSearchResponse>(
          `/agents/${agentId}/followers`,
          params as Record<string, string | number | boolean | undefined>
        );
        const agents = res.agents ?? [];
        if (agents.length === 0) {
          output(res, 'No followers yet.');
          return;
        }
        output(
          res,
          `${res.total ?? agents.length} follower(s):\n\n` +
            agents.map((a, i) => formatAgent(a, i)).join('\n\n')
        );
      } catch (err) {
        handleError(err);
      }
    });

  return cmd;
}
