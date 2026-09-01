/**
 * Host → listen default-model apply (Interfaze Settings round-trip).
 *
 * ACN relays POST /acn/v1/preferred-model over the Mode B control channel.
 * Listen updates the reported preferred model, optional runtime hook, then
 * heartbeats so Interfaze can confirm success.
 */

import { spawn } from 'child_process';
import type { ChildProcess } from 'child_process';

import { postAgentHeartbeat } from './model-heartbeat.js';

export const PREFERRED_MODEL_APPLY_PATH = '/acn/v1/preferred-model';
/** Set only by Owner/Internal relay. Not a secret — also reject public caller header. */
export const PREFERRED_MODEL_APPLY_HEADER = 'x-acn-preferred-model-apply';

const HOOK_TIMEOUT_MS = 15_000;

/** Owner-only control path. Exact match after stripping query / trailing slash. */
export function normalizePreferredModelApplyPath(path?: string): string {
  const raw = (path || '').split('?')[0].trim();
  const stripped = raw.replace(/\/+$/, '') || '/';
  return stripped.startsWith('/') ? stripped : `/${stripped}`;
}

export function isPreferredModelApplyPath(path?: string): boolean {
  return normalizePreferredModelApplyPath(path) === PREFERRED_MODEL_APPLY_PATH;
}

function headerValue(
  headers: Record<string, string> | undefined,
  name: string
): string {
  if (!headers) return '';
  const want = name.toLowerCase();
  for (const [key, value] of Object.entries(headers)) {
    if (key.toLowerCase() === want) return String(value ?? '').trim();
  }
  return '';
}

/**
 * Host apply: POST + exact path + Owner marker, and not a public A2A caller.
 * A frame on this path without the marker must 403 — never fall through to
 * --forward / --runtime / --exec.
 */
export function isOwnerPreferredModelApplyFrame(frame: {
  path?: string;
  method?: string;
  headers?: Record<string, string>;
}): boolean {
  if (!isPreferredModelApplyPath(frame.path)) return false;
  if ((frame.method || 'GET').toUpperCase() !== 'POST') return false;
  if (headerValue(frame.headers, 'x-acn-caller-agent')) return false;
  return headerValue(frame.headers, PREFERRED_MODEL_APPLY_HEADER) === '1';
}

export function parsePreferredModelApplyBody(
  raw: string
): { ok: true; preferred_model: string } | { ok: false; reason: string } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { ok: false, reason: 'invalid_json' };
  }
  if (!parsed || typeof parsed !== 'object') {
    return { ok: false, reason: 'invalid_json' };
  }
  const mid = String(
    (parsed as { preferred_model?: unknown }).preferred_model || ''
  )
    .trim()
    .slice(0, 200);
  if (!mid) return { ok: false, reason: 'preferred_model_required' };
  return { ok: true, preferred_model: mid };
}

export function modelAllowedBySupported(
  modelId: string,
  supported?: string[]
): boolean {
  if (!supported || supported.length === 0) return true;
  const want = modelId.trim().toLowerCase();
  return supported.some((id) => id.trim().toLowerCase() === want);
}

export function runPreferredModelHook(
  cmd: string,
  modelId: string,
  spawnFn: typeof spawn = spawn
): Promise<{ ok: true } | { ok: false; reason: string }> {
  return new Promise((resolve) => {
    let settled = false;
    const child: ChildProcess = spawnFn(cmd, { shell: true });
    const stderr: Buffer[] = [];
    const finish = (result: { ok: true } | { ok: false; reason: string }) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };
    const timer = setTimeout(() => {
      child.kill('SIGTERM');
      finish({ ok: false, reason: 'hook_timeout' });
    }, HOOK_TIMEOUT_MS);
    child.stderr?.on('data', (d: Buffer) => stderr.push(Buffer.from(d)));
    child.on('error', (e: Error) =>
      finish({ ok: false, reason: e.message.slice(0, 200) })
    );
    child.on('close', (code: number | null) => {
      if (code === 0) {
        finish({ ok: true });
        return;
      }
      const detail = Buffer.concat(stderr).toString('utf-8').slice(0, 80);
      finish({
        ok: false,
        reason: detail ? `hook_exit_${code}:${detail}` : `hook_exit_${code}`,
      });
    });
    child.stdin?.end(JSON.stringify({ preferred_model: modelId }));
  });
}

export async function applyPreferredModelOnListen(opts: {
  modelId: string;
  agentId: string;
  apiKey: string;
  baseUrl: string;
  supportedModels?: string[];
  onSetPreferredModel?: string;
  spawnFn?: typeof spawn;
  heartbeatFn?: typeof postAgentHeartbeat;
  logFn?: (line: string) => void;
}): Promise<
  | { ok: true; preferred_model: string }
  | { ok: false; reason: string; status: number }
> {
  const mid = opts.modelId.trim().slice(0, 200);
  if (!mid) return { ok: false, reason: 'preferred_model_required', status: 400 };
  if (!modelAllowedBySupported(mid, opts.supportedModels)) {
    return { ok: false, reason: 'unsupported_model', status: 400 };
  }
  if (opts.onSetPreferredModel?.trim()) {
    const hooked = await runPreferredModelHook(
      opts.onSetPreferredModel,
      mid,
      opts.spawnFn
    );
    if (!hooked.ok) {
      return { ok: false, reason: hooked.reason, status: 409 };
    }
  }
  process.env.ACN_PREFERRED_MODEL = mid;
  const hb = await (opts.heartbeatFn ?? postAgentHeartbeat)({
    baseUrl: opts.baseUrl,
    agentId: opts.agentId,
    apiKey: opts.apiKey,
    preferredModel: mid,
    supportedModels: opts.supportedModels,
  });
  if (!hb.ok) {
    return { ok: false, reason: `heartbeat_${hb.reason}`, status: 502 };
  }
  opts.logFn?.(
    `[acn listen] preferred_model applied=${mid}` +
      (opts.onSetPreferredModel ? ' hook=yes' : ' hook=no')
  );
  return { ok: true, preferred_model: mid };
}
