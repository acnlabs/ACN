import { Command } from 'commander';
import { acnPost } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';
import {
  resolvePreferredModel,
  resolveSupportedModels,
} from './model-heartbeat.js';

export function heartbeatCommand(): Command {
  return new Command('heartbeat')
    .description('Send a heartbeat to keep this agent online')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to value in ~/.acn/config.json)')
    .option(
      '-m, --model <modelId>',
      'Declare runtime model (Host Catalog id) for Host Pricing prefill — self-reported ' +
        '(env: ACN_PREFERRED_MODEL)',
    )
    .option(
      '--supported-models <ids>',
      'Comma-separated models this runtime can run (Interfaze composer; env: ACN_SUPPORTED_MODELS)',
    )
    .option(
      '--clear-supported-models',
      'Clear metadata.supported_models on the server (sends empty list)',
    )
    .action(
      async (opts: {
        agentId?: string;
        model?: string;
        supportedModels?: string;
        clearSupportedModels?: boolean;
      }) => {
      const config = loadConfig();
      const agentId = opts.agentId ?? config.agent_id;

      if (!agentId) {
        console.error('No agent ID found. Run `acn join` first or pass --agent-id.');
        process.exit(1);
      }

      try {
        if (opts.clearSupportedModels && opts.supportedModels?.trim()) {
          console.error(
            'Use either --supported-models or --clear-supported-models, not both.',
          );
          process.exit(1);
        }
        const preferred = resolvePreferredModel({ model: opts.model });
        const supported = resolveSupportedModels({
          models: opts.supportedModels,
          clear: !!opts.clearSupportedModels,
        });
        const body: Record<string, unknown> = {};
        if (preferred) body.preferred_model = preferred;
        if (supported !== undefined) body.supported_models = supported;
        const res = await acnPost<{
          status?: string;
          preferred_model?: string;
          supported_models?: string[];
        }>(`/agents/${agentId}/heartbeat`, Object.keys(body).length ? body : undefined);
        const notes = [
          res.preferred_model ? `preferred_model=${res.preferred_model}` : '',
          res.supported_models != null
            ? `supported_models=${res.supported_models.join(',') || '(cleared)'}`
            : '',
        ].filter(Boolean);
        const modelNote = notes.length ? ` (${notes.join(' ')})` : '';
        output(res, `Heartbeat sent for agent ${agentId}${modelNote}`);
      } catch (err) {
        handleError(err);
      }
    });
}
