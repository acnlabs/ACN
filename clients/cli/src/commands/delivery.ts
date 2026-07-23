import { Command } from 'commander';
import { acnGet, acnPatch } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';

type DeliveryTransport = 'direct' | 'relay' | 'none';

interface DeliveryResponse {
  agent_id: string;
  delivery: DeliveryTransport;
  endpoint?: string | null;
  communication_mode?: string;
  a2a_handshake_ok?: boolean | null;
  next_step_hint?: string | null;
}

const DELIVERY_DESC: Record<DeliveryTransport, string> = {
  direct:
    'direct (Mode A) — ACN dials your public A2A endpoint over HTTP',
  relay:
    'relay (Mode B) — hold an outbound WebSocket with `acn listen`; no public URL',
  none:
    'none — pull/reject only (communication_policy is manifest or closed; not Mode A/B)',
};

function requireAgentId(): string {
  const config = loadConfig();
  if (!config.api_key) {
    console.error(
      'No API key found. Run `acn join` first or `acn config set api-key <key>`.'
    );
    process.exit(1);
  }
  if (!config.agent_id) {
    console.error(
      'No agent ID found. Run `acn join` first or `acn config set agent-id <id>`.'
    );
    process.exit(1);
  }
  return config.agent_id!;
}

function formatDelivery(d: DeliveryResponse): string {
  const lines = [
    `Delivery : ${DELIVERY_DESC[d.delivery] ?? d.delivery}`,
    `Policy   : ${d.communication_mode ?? '?'}  (reception — not the same as delivery)`,
    `Endpoint : ${d.endpoint ?? '(none)'}`,
  ];
  if (d.a2a_handshake_ok === false) {
    lines.push('A2A probe: false — URL reachable but not JSON-RPC; fix the path');
  } else if (d.a2a_handshake_ok === true) {
    lines.push('A2A probe: ok');
  }
  if (d.next_step_hint) {
    lines.push('', d.next_step_hint);
  }
  return lines.join('\n');
}

export function deliveryCommand(): Command {
  const cmd = new Command('delivery').description(
    'Inbound delivery transport (Mode A direct / Mode B relay). ' +
      'Orthogonal to reception policy (`acn inbox mode`).'
  );

  cmd
    .command('get')
    .description('Show derived delivery transport (direct | relay | none)')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (opts: { agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const res = await acnGet<DeliveryResponse>(`/agents/${agentId}/delivery`);
        output(res, formatDelivery(res));
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('set <transport>')
    .description(
      'Switch delivery without re-registering: relay (Mode B) or direct (Mode A). ' +
        'Requires push reception policy (open / allowlist).'
    )
    .option(
      '-e, --endpoint <url>',
      'Required for direct: public A2A JSON-RPC URL (e.g. https://host/a2a)'
    )
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(
      async (
        transport: string,
        opts: { endpoint?: string; agentId?: string }
      ) => {
        const agentId = opts.agentId ?? requireAgentId();
        const normalized = transport.trim().toLowerCase();
        if (normalized !== 'relay' && normalized !== 'direct') {
          console.error(
            `Unknown transport "${transport}". Use: relay | direct`
          );
          process.exit(1);
        }
        if (normalized === 'direct' && !opts.endpoint) {
          console.error(
            'direct requires --endpoint <url> (full A2A path, e.g. https://host/a2a).'
          );
          process.exit(1);
        }
        if (normalized === 'relay' && opts.endpoint) {
          console.error(
            'relay must not include --endpoint (clear the public URL; use `acn listen`).'
          );
          process.exit(1);
        }

        const body =
          normalized === 'relay'
            ? { delivery: 'relay' as const }
            : { delivery: 'direct' as const, endpoint: opts.endpoint! };

        try {
          const res = await acnPatch<DeliveryResponse>(
            `/agents/${agentId}/delivery`,
            body
          );
          const followUp =
            res.delivery === 'relay'
              ? [
                  '',
                  'Next: run the Mode B listener (built-in A2A + wake host):',
                  '  acn listen --runtime http --wake-url http://127.0.0.1:PORT/wake',
                  'Compat: acn listen --forward http://localhost:PORT',
                ]
              : [];
          output(res, [formatDelivery(res), ...followUp].join('\n'));
        } catch (err) {
          handleError(err);
        }
      }
    );

  return cmd;
}
