import { Command } from 'commander';
import { acnPost } from '../api.js';
import { loadConfig, saveConfig } from '../config.js';
import { output, handleError, isJsonMode } from '../output.js';

interface RotateKeyResponse {
  success: boolean;
  agent_id: string;
  api_key: string;
  message?: string;
}

export function rotateKeyCommand(): Command {
  return new Command('rotate-key')
    .description(
      'Rotate this agent\'s API key (H1). The old key stops working immediately.'
    )
    .option(
      '-i, --agent-id <id>',
      'Agent ID (defaults to value in ~/.acn/config.json)'
    )
    .option(
      '--save',
      'Persist the new key to ~/.acn/config.json (overwrites api_key). ' +
        'Without this flag the new key is only printed; you must run ' +
        '`acn config set api_key <new>` yourself, or every subsequent CLI ' +
        'call will 401 against the freshly-invalidated old key.',
      false
    )
    .action(async (opts: { agentId?: string; save?: boolean }) => {
      const config = loadConfig();
      const agentId = opts.agentId ?? config.agent_id;

      if (!agentId) {
        // Match the heartbeat command's "no agent" failure mode so users
        // see the same hint regardless of which command they hit first.
        console.error(
          'No agent ID found. Run `acn join` first or pass --agent-id.'
        );
        process.exit(1);
      }

      if (!config.api_key) {
        // The CLI only knows how to authenticate as the agent itself
        // (Bearer <api_key>). Owner recovery is Labs web UI → agent detail
        // → "Reset API Key" (Auth0 / CN JWT). Point at that concrete path
        // instead of a generic "owner-side rotation" that used to have no UI.
        console.error(
          'No API key found in ~/.acn/config.json. The CLI rotates with the ' +
            'current agent key.\n' +
            `If you have lost the key, sign in to Labs as the agent owner, open ` +
            `/agents/${agentId}, click "Reset API Key", then run:\n` +
            '  acn config set api_key <new>'
        );
        process.exit(1);
      }

      try {
        const res = await acnPost<RotateKeyResponse>(
          `/agents/${agentId}/rotate-key`
        );

        if (opts.save) {
          saveConfig({ api_key: res.api_key });
        }

        if (isJsonMode()) {
          output(res, '');
          return;
        }

        // Two-line warning + new key + follow-up. The point of separating
        // the new key onto its own line is so an operator copy-pasting from
        // a terminal does not accidentally include surrounding text.
        const lines = [
          `Agent ${res.agent_id}: API key rotated. Previous key is now INVALID.`,
          '',
          `New API key: ${res.api_key}`,
          '',
          opts.save
            ? '~/.acn/config.json updated. CLI will use the new key on next call.'
            : 'Store this key securely. To make this CLI use it, run:',
          ...(opts.save ? [] : [`  acn config set api_key ${res.api_key}`]),
        ];
        output(res, lines.join('\n'));
      } catch (err) {
        handleError(err);
      }
    });
}
