import { Command } from 'commander';
import { acnGet, acnPost, acnDelete } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';

// ─── Offline history inbox (policy=open) ─────────────────────────────────────

interface HistoryMessage {
  route_id: string;
  from_agent_id?: string;
  message?: unknown;
  received_at?: string;
  priority?: string;
}

interface HistoryResponse {
  agent_id: string;
  messages: HistoryMessage[];
  count: number;
  limit: number;
  ack: boolean;
}

function formatHistoryMsg(m: HistoryMessage, i: number): string {
  const content =
    typeof m.message === 'string' ? m.message : JSON.stringify(m.message ?? '');
  return [
    `[${i + 1}] ${m.route_id}`,
    `  From : ${m.from_agent_id ?? '?'}`,
    ...(m.received_at ? [`  At   : ${m.received_at}`] : []),
    `  Msg  : ${content.slice(0, 200)}`,
  ].join('\n');
}

interface ManifestEntry {
  mid: string;
  sender_id: string;
  summary?: string;
  ts: number;
  content_size?: number;
  acked_at?: number;
  extra?: Record<string, unknown>;
}

interface ManifestListResponse {
  agent_id: string;
  count: number;
  entries: ManifestEntry[];
}

interface ContentResponse {
  mid: string;
  owner_id: string;
  content?: unknown;
  self_hosted?: boolean;
  content_url?: string;
  content_hash?: string;
}

interface AckResponse {
  agent_id: string;
  mid: string;
  acked: boolean;
  acked_at?: number;
  attention_fee?: {
    escrow_id?: string;
    currency?: string;
    amount?: number;
    agent_amount?: number;
    acn_amount?: number;
    provider_amount?: number;
    receipt_id?: string;
  };
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

function formatEntry(e: ManifestEntry, index?: number): string {
  const prefix = index !== undefined ? `[${index + 1}] ` : '';
  const acked = e.acked_at ? ' [acked]' : '';
  const ts = new Date(e.ts).toISOString();
  const lines = [
    `${prefix}${e.mid}${acked}`,
    `  From    : ${e.sender_id}`,
    `  Sent    : ${ts}`,
  ];
  if (e.summary) lines.push(`  Summary : ${e.summary}`);
  if (e.content_size) lines.push(`  Size    : ${e.content_size} bytes`);
  return lines.join('\n');
}

export function inboxCommand(): Command {
  const cmd = new Command('inbox').description('Manage incoming messages (manifest queue)');

  cmd
    .command('list')
    .description('List pending notifications in manifest queue')
    .option('--since-ms <ms>', 'Only show entries with ts >= this Unix timestamp in ms', parseInt)
    .option('--limit <n>', 'Max entries to return (default 50, max 200)', parseInt)
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (opts: { sinceMs?: number; limit?: number; agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const params: Record<string, number | undefined> = {};
        if (opts.sinceMs !== undefined) params.since_ms = opts.sinceMs;
        if (opts.limit !== undefined) params.limit = opts.limit;
        const res = await acnGet<ManifestListResponse>(
          `/communication/manifest/${agentId}`,
          params as Record<string, string | number | boolean | undefined>
        );
        const entries = res.entries ?? [];
        if (entries.length === 0) {
          output(res, 'Manifest queue is empty.');
          return;
        }
        output(
          res,
          `${entries.length} notification(s):\n\n` +
            entries.map((e, i) => formatEntry(e, i)).join('\n\n')
        );
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('pull <mid>')
    .description('Pull full message content for a notification')
    .action(async (mid: string) => {
      requireAgentId();
      try {
        const res = await acnGet<ContentResponse>(`/communication/content/${mid}`);
        if (res.self_hosted) {
          const hashInfo = res.content_hash ? `\nHash: ${res.content_hash}` : '';
          output(res, `Self-hosted content (fetch directly from sender):\nURL: ${res.content_url}${hashInfo}`);
        } else {
          const content =
            typeof res.content === 'string'
              ? res.content
              : JSON.stringify(res.content, null, 2);
          output(res, `Content:\n${content}`);
        }
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('ack <mid>')
    .description('Release attention_fee from a paid notification (entry must have a locked fee; use delete for unpaid entries)')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (mid: string, opts: { agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const res = await acnPost<AckResponse>(
          `/communication/manifest/${agentId}/${mid}/ack`
        );
        const fee = res.attention_fee;
        const feeInfo = fee?.agent_amount !== undefined
          ? ` | fee released: ${fee.agent_amount} ${fee.currency ?? ''} (receipt: ${fee.receipt_id ?? '?'})`
          : '';
        output(res, `Acknowledged ${mid}${feeInfo}`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('delete <mid>')
    .description('Reject and delete a notification (refunds attention_fee to sender if present)')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (mid: string, opts: { agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const res = await acnDelete<{ deleted?: boolean; attention_fee?: { refunded?: boolean } }>(
          `/communication/manifest/${agentId}/${mid}`
        );
        const refundInfo = res.attention_fee?.refunded ? ' (sender refunded)' : '';
        output(res, `Deleted notification ${mid}${refundInfo}`);
      } catch (err) {
        handleError(err);
      }
    });

  // ── history subgroup ──────────────────────────────────────────────────────
  const history = new Command('history').description(
    'Offline direct-delivery inbox (policy=open). Full messages stored when you were unreachable.'
  );

  history
    .command('list')
    .description('List offline messages (full content)')
    .option('--limit <n>', 'Max messages to return (default 100)', parseInt)
    .option('--ack', 'Clear the entire inbox after retrieval')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (opts: { limit?: number; ack?: boolean; agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const params: Record<string, string | number | boolean | undefined> = {};
        if (opts.limit !== undefined) params.limit = opts.limit;
        if (opts.ack) params.ack = true;
        const res = await acnGet<HistoryResponse>(
          `/communication/history/${agentId}`,
          params
        );
        const msgs = res.messages ?? [];
        if (msgs.length === 0) {
          output(res, 'Offline inbox is empty.');
          return;
        }
        const ackNote = opts.ack ? ' [inbox cleared]' : '';
        output(
          res,
          `${msgs.length} message(s)${ackNote}:\n\n` +
            msgs.map((m, i) => formatHistoryMsg(m, i)).join('\n\n')
        );
      } catch (err) {
        handleError(err);
      }
    });

  history
    .command('ack <route_ids...>')
    .description('Selectively acknowledge specific messages by route_id')
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .action(async (routeIds: string[], opts: { agentId?: string }) => {
      const agentId = opts.agentId ?? requireAgentId();
      try {
        const res = await acnPost<{ agent_id: string; acked: string[] }>(
          `/communication/history/${agentId}/ack`,
          { route_ids: routeIds }
        );
        const acked = Array.isArray(res.acked) ? res.acked.length : routeIds.length;
        output(res, `Acknowledged ${acked} message(s).`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd.addCommand(history);

  return cmd;
}
