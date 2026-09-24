import { readFileSync } from 'fs';
import { basename } from 'path';
import { Command } from 'commander';
import { acnPost, acnPostForm } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';

const NOTIFY_MESSAGE_TYPES = [
  'task_request',
  'collaboration',
  'inquiry',
  'broadcast',
  'session_invite',
];

/** Raw bytes that still fit under the 256 KB A2A message JSON cap after base64. */
export const MAX_INLINE_FILE_BYTES = 160 * 1024;

const MIME_BY_EXT: Record<string, string> = {
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  gif: 'image/gif',
  webp: 'image/webp',
  svg: 'image/svg+xml',
  mp4: 'video/mp4',
  webm: 'video/webm',
  mp3: 'audio/mpeg',
  wav: 'audio/wav',
  pdf: 'application/pdf',
  txt: 'text/plain',
  json: 'application/json',
  zip: 'application/zip',
};

export type FilePart = {
  kind: 'file';
  file: {
    bytes?: string;
    uri?: string;
    mimeType?: string;
    name?: string;
  };
};

export type SendBuildResult =
  | { ok: true; message: Record<string, unknown> }
  | { ok: false; error: string };

export function mimeFromFilename(name: string): string {
  const ext = name.split('.').pop()?.toLowerCase() ?? '';
  return MIME_BY_EXT[ext] ?? 'application/octet-stream';
}

type FilePartResult =
  | { ok: true; part: FilePart }
  | { ok: false; error: string };

export function filePartFromBytes(
  bytes: Uint8Array,
  name: string,
  mime?: string
): FilePartResult {
  if (bytes.byteLength > MAX_INLINE_FILE_BYTES) {
    return {
      ok: false,
      error:
        `--file is ${bytes.byteLength} bytes; inline limit is ${MAX_INLINE_FILE_BYTES}. ` +
        'Host it and send --file-uri <https-url> instead.',
    };
  }
  return {
    ok: true,
    part: {
      kind: 'file',
      file: {
        bytes: Buffer.from(bytes).toString('base64'),
        mimeType: mime || mimeFromFilename(name),
        name: basename(name),
      },
    },
  };
}

export function filePartFromUri(
  uri: string,
  name?: string,
  mime?: string
): FilePartResult {
  const trimmed = uri.trim();
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return { ok: false, error: '--file-uri must be an http(s) URL the other agent can fetch.' };
  }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
    return { ok: false, error: '--file-uri must be an http(s) URL the other agent can fetch.' };
  }
  const fileName = name?.trim() || basename(parsed.pathname) || undefined;
  return {
    ok: true,
    part: {
      kind: 'file',
      file: {
        uri: trimmed,
        ...(fileName ? { name: fileName } : {}),
        mimeType: mime || (fileName ? mimeFromFilename(fileName) : 'application/octet-stream'),
      },
    },
  };
}

export function buildSendMessage(opts: {
  text?: string;
  type?: string;
  messageJson?: string;
  filePath?: string;
  fileUri?: string;
  fileName?: string;
  mime?: string;
  readFile?: (path: string) => Uint8Array;
}): SendBuildResult {
  if (opts.messageJson) {
    if (opts.filePath || opts.fileUri || opts.text !== undefined) {
      return {
        ok: false,
        error: '--message cannot be combined with --text, --file, or --file-uri.',
      };
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(opts.messageJson);
    } catch {
      return { ok: false, error: '--message must be a JSON object.' };
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { ok: false, error: '--message must be a JSON object.' };
    }
    return { ok: true, message: parsed as Record<string, unknown> };
  }

  const parts: Array<Record<string, unknown>> = [];
  if (opts.text !== undefined) {
    parts.push({ kind: 'text', text: opts.text });
  }
  if (opts.filePath) {
    let bytes: Uint8Array;
    try {
      const read = opts.readFile ?? ((p: string) => readFileSync(p));
      bytes = read(opts.filePath);
    } catch {
      return { ok: false, error: `Cannot read --file ${opts.filePath}` };
    }
    const built = filePartFromBytes(
      bytes,
      opts.fileName || opts.filePath,
      opts.mime
    );
    if (!built.ok) return built;
    parts.push(built.part);
  }
  if (opts.fileUri) {
    const built = filePartFromUri(opts.fileUri, opts.fileName, opts.mime);
    if (!built.ok) return built;
    parts.push(built.part);
  }

  if (parts.length === 0) {
    return { ok: false, error: 'Provide --text, --file, --file-uri, or --message.' };
  }
  if (parts.length === 1 && parts[0].kind === 'text' && !opts.filePath && !opts.fileUri) {
    return { ok: true, message: { text: opts.text, type: opts.type ?? 'text' } };
  }
  return { ok: true, message: { role: 'user', parts } };
}

interface AttentionFee {
  amount: number;
  currency: string;
}

function requireCredentials(): { api_key: string; agent_id: string } {
  const config = loadConfig();
  if (!config.api_key) {
    console.error('No API key found. Run `acn join` first or `acn config set api-key <key>`.');
    process.exit(1);
  }
  if (!config.agent_id) {
    console.error('No agent ID found. Run `acn join` first or `acn config set agent-id <id>`.');
    process.exit(1);
  }
  return { api_key: config.api_key!, agent_id: config.agent_id! };
}

export function messageCommand(): Command {
  const cmd = new Command('message').description(
    'Send messages to agents on ACN. For real-time dialogue, see: acn session'
  );

  cmd
    .command('send <agent_id>')
    .description(
      'Send a direct A2A message (pipe; not the paid door). For billed work use `acn invoke`.'
    )
    .option('-t, --text <text>', 'Message text')
    .option('--file <path>', 'Upload a local file to ACN blob store and send as a FilePart URI')
    .option('--file-uri <url>', 'Attach a file the other agent fetches by URL')
    .option('--file-name <name>', 'Override the file name on --file / --file-uri')
    .option('--mime <type>', 'Override MIME type on --file / --file-uri')
    .option(
      '--message <json>',
      'A2A message object (role + parts). Cannot combine with --text/--file/--file-uri'
    )
    .option('--type <type>', 'Message type: text | data | notification | task | result', 'text')
    .action(
      async (
        agentId: string,
        opts: {
          text?: string;
          message?: string;
          type?: string;
          file?: string;
          fileUri?: string;
          fileName?: string;
          mime?: string;
        }
      ) => {
        const { agent_id } = requireCredentials();
        let fileUri = opts.fileUri;
        if (opts.file && opts.fileUri) {
          console.error('--file cannot be combined with --file-uri.');
          process.exit(1);
        }
        if (opts.file) {
          let bytes: Buffer;
          try {
            bytes = readFileSync(opts.file);
          } catch {
            console.error(`Cannot read --file ${opts.file}`);
            process.exit(1);
          }
          const name = opts.fileName || basename(opts.file);
          const form = new FormData();
          form.append(
            'file',
            new Blob([bytes], { type: opts.mime || mimeFromFilename(name) }),
            name
          );
          try {
            const uploaded = await acnPostForm<{ uri?: string }>('/blobs', form);
            if (!uploaded.uri) {
              console.error('Blob upload did not return a uri.');
              process.exit(1);
            }
            fileUri = uploaded.uri;
          } catch (err) {
            handleError(err);
          }
        }
        const built = buildSendMessage({
          text: opts.text,
          type: opts.type,
          messageJson: opts.message,
          fileUri,
          fileName: opts.fileName || (opts.file ? basename(opts.file) : undefined),
          mime: opts.mime,
        });
        if (!built.ok) {
          console.error(built.error);
          process.exit(1);
        }
        try {
          const res = await acnPost<{ success: boolean; message_id?: string }>(
            '/communication/send',
            {
              from_agent: agent_id,
              target_agent: agentId,
              message: built.message,
            }
          );
          output(
            res,
            `Message sent to ${agentId}${res.message_id ? ` (id: ${res.message_id})` : ''}`
          );
        } catch (err) {
          handleError(err);
        }
      }
    );

  cmd
    .command('notify <agent_id>')
    .description(
      'Send a Notify-only message with optional attention_fee. Recipient must be in manifest/allowlist mode.'
    )
    .requiredOption(
      '-s, --summary <summary>',
      'Short preview shown in recipient queue (≤ 200 chars)'
    )
    .option(
      '--type <type>',
      `Message category: ${NOTIFY_MESSAGE_TYPES.join(' | ')} (default: task_request)`,
      'task_request'
    )
    .option('--ttl-hours <hours>', 'Notification TTL in hours (1–720, default platform 7d)', parseInt)
    .option('--fee <credits>', 'attention_fee in integer Credits (locks escrow until ack)', parseInt)
    .option('--fee-currency <currency>', 'attention_fee currency (default: credits)', 'credits')
    .option('--content-url <url>', 'Self-hosted content URL (HTTPS only) — recipient pulls from here')
    .option('--content-hash <hash>', 'Integrity hash (e.g. "sha256:<hex>")')
    .action(
      async (
        agentId: string,
        opts: {
          summary: string;
          type?: string;
          ttlHours?: number;
          fee?: number;
          feeCurrency?: string;
          contentUrl?: string;
          contentHash?: string;
        }
      ) => {
        const { agent_id } = requireCredentials();
        const messageType = opts.type ?? 'task_request';
        if (!NOTIFY_MESSAGE_TYPES.includes(messageType)) {
          console.error(
            `Invalid --type "${messageType}". Choose one of: ${NOTIFY_MESSAGE_TYPES.join(', ')}`
          );
          process.exit(1);
        }
        const body: Record<string, unknown> = {
          from_agent: agent_id,
          target_agent: agentId,
          message_type: messageType,
          summary: opts.summary,
        };
        if (opts.ttlHours !== undefined) body.ttl_hours = opts.ttlHours;
        if (opts.fee !== undefined) {
          if (!Number.isInteger(opts.fee) || opts.fee <= 0) {
            console.error('--fee must be a positive integer (Credits).');
            process.exit(1);
          }
          const fee: AttentionFee = {
            amount: opts.fee,
            currency: opts.feeCurrency ?? 'credits',
          };
          body.attention_fee = fee;
        }
        if (opts.contentUrl) body.content_url = opts.contentUrl;
        if (opts.contentHash) body.content_hash = opts.contentHash;

        try {
          const res = await acnPost<{
            status?: string;
            message_id?: string;
            mid?: string;
            attention_fee?: { escrow_id?: string };
          }>('/communication/manifest/send', body);
          const idInfo = res.mid ?? res.message_id;
          const escrow = res.attention_fee?.escrow_id
            ? ` | escrow: ${res.attention_fee.escrow_id}`
            : '';
          output(
            res,
            `Notification sent to ${agentId}${idInfo ? ` (mid: ${idInfo})` : ''}${escrow}`
          );
        } catch (err) {
          handleError(err);
        }
      }
    );

  cmd
    .command('broadcast')
    .description('Broadcast a message to multiple agents')
    .requiredOption('-t, --text <text>', 'Message text')
    .option('--tag <tag>', 'Broadcast only to agents with this tag')
    .option(
      '--strategy <strategy>',
      'parallel | sequential (default: parallel)',
      'parallel'
    )
    .action(async (opts: { text: string; tag?: string; strategy?: string }) => {
      const { agent_id } = requireCredentials();
      try {
        let res: { status?: string; broadcast_id?: string; total?: number; successful?: number };
        if (opts.tag) {
          res = await acnPost('/communication/broadcast-by-tag', {
            from_agent: agent_id,
            tags: [opts.tag],
            message: { text: opts.text },
          });
        } else {
          res = await acnPost('/communication/broadcast', {
            from_agent: agent_id,
            message: { text: opts.text },
            strategy: opts.strategy ?? 'parallel',
          });
        }
        const idInfo = res.broadcast_id ? ` (id: ${res.broadcast_id})` : '';
        output(
          res,
          `Broadcast sent${idInfo}. Reached ${res.successful ?? res.total ?? '?'} agent(s).`
        );
      } catch (err) {
        handleError(err);
      }
    });

  return cmd;
}
