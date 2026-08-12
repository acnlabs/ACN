import { Command } from 'commander';
import { acnPost } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';

export function heartbeatCommand(): Command {
  return new Command('heartbeat')
    .description('Send a heartbeat to keep this agent online')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to value in ~/.acn/config.json)')
    .option(
      '-m, --model <modelId>',
      'Declare runtime model (Host Catalog id) for Host Pricing prefill — self-reported',
    )
    .action(async (opts: { agentId?: string; model?: string }) => {
      const config = loadConfig();
      const agentId = opts.agentId ?? config.agent_id;

      if (!agentId) {
        console.error('No agent ID found. Run `acn join` first or pass --agent-id.');
        process.exit(1);
      }

      try {
        const body =
          opts.model && opts.model.trim()
            ? { preferred_model: opts.model.trim() }
            : undefined;
        const res = await acnPost<{
          status?: string;
          preferred_model?: string;
        }>(`/agents/${agentId}/heartbeat`, body);
        const modelNote = res.preferred_model
          ? ` (preferred_model=${res.preferred_model})`
          : '';
        output(res, `Heartbeat sent for agent ${agentId}${modelNote}`);
      } catch (err) {
        handleError(err);
      }
    });
}
