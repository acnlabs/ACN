import { Command } from 'commander';
import { acnPost } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';
import {
  chatIdFromEnv,
  injectChatIdIntoInvokeMessage,
  recordPendingCallee,
} from './invoke-chat-context.js';

interface InvokeResponse {
  request_id?: string;
  hop_id?: string;
  to?: string;
  from?: string;
  status?: string;
  slot?: string;
  fallback_from?: string;
  attempts?: unknown;
  usage?: unknown;
  delivery?: unknown;
}

function requireApiKey(): string {
  const config = loadConfig();
  if (!config.api_key) {
    console.error('No API key found. Run `acn join` first or `acn config set api-key <key>`.');
    process.exit(1);
  }
  return config.api_key;
}

function parseMessage(opts: { text?: string; message?: string }): Record<string, unknown> {
  if (opts.message) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(opts.message);
    } catch {
      console.error('--message must be a JSON object, e.g. \'{"text":"hello"}\'.');
      process.exit(1);
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      console.error('--message must be a JSON object, e.g. \'{"text":"hello"}\'.');
      process.exit(1);
    }
    return parsed as Record<string, unknown>;
  }
  if (opts.text !== undefined) {
    return { text: opts.text };
  }
  console.error('Provide --text or --message.');
  process.exit(1);
}

export function invokeCommand(): Command {
  return new Command('invoke')
    .description(
      'Call another ACN agent through AgentRouter (paid hop:invoke receipt). Not chat, not Match, not `acn message send` (pipe, not the billable door).'
    )
    .option('--to <agent_id>', 'Target agent id (specified-id; failover only if --slot is also set)')
    .option('--slot <slot_id>', 'Platform slot (v0: text.reply). Enables same-slot failover')
    .option('-t, --text <text>', 'Message text')
    .option('--message <json>', 'Raw message JSON object (overrides --text)')
    .option('--request-id <id>', 'Caller-supplied request id for the hop receipt')
    .action(
      async (opts: {
        to?: string;
        slot?: string;
        text?: string;
        message?: string;
        requestId?: string;
      }) => {
        requireApiKey();
        const to = opts.to?.trim() || undefined;
        const slot = opts.slot?.trim() || undefined;
        if (!to && !slot) {
          console.error('Provide --to and/or --slot.');
          process.exit(1);
        }
        const message = parseMessage(opts);
        const jobToken = (process.env.ACN_JOB_TOKEN || "").trim();
        if (jobToken) {
          const meta =
            message.metadata &&
            typeof message.metadata === "object" &&
            !Array.isArray(message.metadata)
              ? { ...(message.metadata as Record<string, unknown>) }
              : {};
          const ap =
            meta.agentplanet &&
            typeof meta.agentplanet === "object" &&
            !Array.isArray(meta.agentplanet)
              ? { ...(meta.agentplanet as Record<string, unknown>) }
              : {};
          ap.job_token = jobToken;
          meta.agentplanet = ap;
          message.metadata = meta;
        }
        const chatId = chatIdFromEnv();
        if (chatId) injectChatIdIntoInvokeMessage(message, chatId);
        const body: Record<string, unknown> = { message };
        if (to) body.to = to;
        if (slot) body.slot = slot;
        if (opts.requestId?.trim()) body.request_id = opts.requestId.trim();

        try {
          const res = await acnPost<InvokeResponse>('/invoke', body);
          if (chatId && res.hop_id && res.to) {
            recordPendingCallee(chatId, {
              to: res.to,
              hopId: res.hop_id,
              status: res.status,
            });
          }
          const hop = res.hop_id ? ` hop=${res.hop_id}` : '';
          const status = res.status ? ` status=${res.status}` : '';
          const winner = res.to ? ` to=${res.to}` : '';
          const fallback = res.fallback_from ? ` fallback_from=${res.fallback_from}` : '';
          output(res, `Invoked${winner}${hop}${status}${fallback}`);
        } catch (err) {
          handleError(err);
        }
      }
    );
}
