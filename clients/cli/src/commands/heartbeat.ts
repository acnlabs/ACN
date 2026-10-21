import { Command } from 'commander';
import { acnPost } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';
import {
  declarationHeartbeatFields,
  fetchRuntimeModelReport,
  resolvePreferredModel,
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
      'Ignored. Models come from each configured provider list, not a handwritten list',
    )
    .option(
      '--clear-supported-models',
      'Clear providers and the derived model union on the server',
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
        // Clear wipes providers and the derived union together. A normal
        // heartbeat publishes provider lists only when every one was read.
        const clear = !!opts.clearSupportedModels;
        const report = clear ? {} : await fetchRuntimeModelReport({});
        const declared = declarationHeartbeatFields({ clear, report });
        const body: Record<string, unknown> = {};
        if (preferred) body.preferred_model = preferred;
        if (declared.providers !== undefined) {
          body.providers = declared.providers;
        }
        if (declared.supportedModels !== undefined) {
          body.supported_models = declared.supportedModels;
        }
        if (declared.modelPrices?.length) body.model_prices = declared.modelPrices;
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
