import { writeFileSync } from 'fs';
import { basename } from 'path';
import { Command } from 'commander';
import { acnGet, acnGetBytes, acnPost } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';

const BLOB_ID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function parseBlobRef(ref: string): { blobId: string; sig?: string } {
  const trimmed = ref.trim();
  if (BLOB_ID.test(trimmed)) return { blobId: trimmed };
  let url: URL;
  try {
    url = new URL(trimmed);
  } catch {
    throw new Error('blob ref must be a blob id or http(s) URI');
  }
  const blobId = url.pathname.split('/').filter(Boolean).pop() ?? '';
  if (!BLOB_ID.test(blobId)) {
    throw new Error('blob ref must be a blob id or http(s) URI');
  }
  const sig = url.searchParams.get('sig') ?? undefined;
  return { blobId, sig: sig || undefined };
}

interface BlobUsage {
  agent_id: string;
  mailbox_bytes: number;
  mailbox_cap_bytes: number;
  retained_bytes: number;
  retained_cap_bytes: number;
  used_bytes: number;
  free_ttl_seconds: number;
  credits_per_gib_day: number;
}

interface BlobObject {
  id: string;
  owner_id: string;
  name: string;
  size: number;
  exp: number;
  retained: boolean;
  uri: string;
  charged?: number;
}

export function safeDownloadName(name: string | undefined, fallback: string): string {
  const base = basename((name || fallback).replace(/\\/g, '/'));
  if (!base || base === '.' || base === '..') return fallback;
  return base;
}

function requireCredentials(): void {
  const config = loadConfig();
  if (!config.api_key) {
    console.error('No API key found. Run `acn join` first or `acn config set api-key <key>`.');
    process.exit(1);
  }
  if (!config.agent_id) {
    console.error('No agent ID found. Run `acn join` first or `acn config set agent-id <id>`.');
    process.exit(1);
  }
}

function formatUsage(u: BlobUsage): string {
  return [
    `  Agent    : ${u.agent_id}`,
    `  Mailbox  : ${u.mailbox_bytes} / ${u.mailbox_cap_bytes} (${u.free_ttl_seconds}s TTL)`,
    `  Retained : ${u.retained_bytes} / ${u.retained_cap_bytes}`,
    `  Rate     : ${u.credits_per_gib_day} credits / GiB-day`,
  ].join('\n');
}

function formatBlob(b: BlobObject): string {
  const lines = [
    `  ID       : ${b.id}`,
    `  Owner    : ${b.owner_id}`,
    `  Name     : ${b.name}`,
    `  Size     : ${b.size}`,
    `  Exp      : ${b.exp}`,
    `  Retained : ${b.retained}`,
    `  URI      : ${b.uri}`,
  ];
  if (b.charged !== undefined) lines.push(`  Charged  : ${b.charged}`);
  return lines.join('\n');
}

export function blobCommand(): Command {
  const cmd = new Command('blob').description(
    'ACN mailbox blobs for A2A FilePart URIs (usage + paid extend)',
  );

  cmd
    .command('usage')
    .description('Show mailbox and retained quota')
    .action(async () => {
      requireCredentials();
      try {
        const usage = await acnGet<BlobUsage>('/blobs/usage');
        output(usage, formatUsage(usage));
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('extend <blob>')
    .description('Keep a blob URI alive (consumer pays Credits)')
    .requiredOption('--days <n>', 'Extra days to retain (1–90)')
    .option('--sig <hex>', 'Capability sig if <blob> is an id, not a URI')
    .action(async (blob: string, opts: { days: string; sig?: string }) => {
      requireCredentials();
      try {
        const extraDays = Number(opts.days);
        if (!Number.isInteger(extraDays) || extraDays < 1 || extraDays > 90) {
          console.error('--days must be an integer from 1 to 90.');
          process.exit(1);
        }
        const parsed = parseBlobRef(blob);
        const body: Record<string, unknown> = { extra_days: extraDays };
        const sig = opts.sig ?? parsed.sig;
        if (sig) body.sig = sig;
        const result = await acnPost<BlobObject>(`/blobs/${parsed.blobId}/extend`, body);
        output(result, formatBlob(result));
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('get <blob>')
    .description('Download blob bytes (capability URI or owner API key)')
    .option('-o, --out <path>', 'Write to this path (default: blob filename)')
    .option('--sig <hex>', 'Capability sig if <blob> is an id, not a URI')
    .action(async (blob: string, opts: { out?: string; sig?: string }) => {
      requireCredentials();
      try {
        const parsed = parseBlobRef(blob);
        const sig = opts.sig ?? parsed.sig;
        const downloaded = await acnGetBytes(`/blobs/${parsed.blobId}`, {
          sig,
        });
        const outPath = opts.out || safeDownloadName(downloaded.filename, parsed.blobId);
        writeFileSync(outPath, downloaded.bytes);
        output(
          { path: outPath, bytes: downloaded.bytes.byteLength, blob_id: parsed.blobId },
          `  Wrote ${downloaded.bytes.byteLength} bytes to ${outPath}`,
        );
      } catch (err) {
        handleError(err);
      }
    });

  return cmd;
}
