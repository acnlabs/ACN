/**
 * Host → agent runtime apply (Interfaze Settings default-model round-trip).
 *
 * ACN delivers POST /acn/v1/runtime (listen also accepts /acn/v1/preferred-model).
 * Listen updates the reported preferred model, optional runtime hook, then
 * heartbeats so Interfaze can confirm success.
 */

import { spawn } from 'child_process';
import type { ChildProcess } from 'child_process';
import { createLocalJWKSet, createRemoteJWKSet, jwtVerify, type JSONWebKeySet } from 'jose';

import { postAgentHeartbeat } from './model-heartbeat.js';

export const RUNTIME_APPLY_PATH = '/acn/v1/runtime';
export const PREFERRED_MODEL_APPLY_PATH = '/acn/v1/preferred-model';
export const RUNTIME_APPLY_HEADER = 'x-acn-runtime-apply';
/** Alias for older ACN builds. Not a secret. */
export const PREFERRED_MODEL_APPLY_HEADER = 'x-acn-preferred-model-apply';

const HOOK_TIMEOUT_MS = 15_000;

/** Owner-only control path. Exact match after stripping query / trailing slash. */
export function normalizePreferredModelApplyPath(path?: string): string {
  const raw = (path || '').split('?')[0].trim();
  const stripped = raw.replace(/\/+$/, '') || '/';
  return stripped.startsWith('/') ? stripped : `/${stripped}`;
}

export function isPreferredModelApplyPath(path?: string): boolean {
  const n = normalizePreferredModelApplyPath(path);
  return n === PREFERRED_MODEL_APPLY_PATH || n === RUNTIME_APPLY_PATH;
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
  return (
    headerValue(frame.headers, PREFERRED_MODEL_APPLY_HEADER) === '1' ||
    headerValue(frame.headers, RUNTIME_APPLY_HEADER) === '1'
  );
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

export const isRuntimeApplyPath = isPreferredModelApplyPath;
export const isOwnerRuntimeApplyFrame = isOwnerPreferredModelApplyFrame;

function runtimePatchesEqual(a: unknown, b: unknown): boolean {
  try {
    return JSON.stringify(a) === JSON.stringify(b);
  } catch {
    return false;
  }
}

export async function verifyRuntimeCommand(opts: {
  token: string;
  agentId: string;
  issuer: string;
  patch: Record<string, unknown>;
  jwks: JSONWebKeySet | URL;
}): Promise<{ ok: true } | { ok: false; reason: string }> {
  const getKey =
    opts.jwks instanceof URL
      ? createRemoteJWKSet(opts.jwks)
      : createLocalJWKSet(opts.jwks);
  try {
    const { payload } = await jwtVerify(opts.token, getKey, {
      issuer: opts.issuer,
      audience: opts.agentId,
      algorithms: ['RS256'],
    });
    if (payload.sub !== 'acn') return { ok: false, reason: 'runtime_jwt_sub' };
    if (payload.acn_principal !== 'host') {
      return { ok: false, reason: 'runtime_jwt_principal' };
    }
    if (payload.acn_action !== 'runtime') {
      return { ok: false, reason: 'runtime_jwt_action' };
    }
    if (!runtimePatchesEqual(payload.runtime, opts.patch)) {
      return { ok: false, reason: 'runtime_jwt_body_mismatch' };
    }
    return { ok: true };
  } catch (e) {
    const msg = e instanceof Error ? e.message.slice(0, 80) : 'runtime_jwt_invalid';
    return { ok: false, reason: msg };
  }
}

/** Mode A reference: verify Host JWT, apply preferred_model, heartbeat. */
export async function handleRuntimeApplyHttp(opts: {
  authorization?: string;
  body: string;
  agentId: string;
  issuer: string;
  jwks: JSONWebKeySet | URL;
  apiKey: string;
  baseUrl: string;
  supportedModels?: string[];
  onSetPreferredModel?: string;
  spawnFn?: typeof spawn;
  heartbeatFn?: typeof postAgentHeartbeat;
  logFn?: (line: string) => void;
}): Promise<{ status: number; body: string }> {
  const token = (opts.authorization || '').replace(/^Bearer\s+/i, '').trim();
  if (!token) {
    return { status: 401, body: JSON.stringify({ error: 'runtime_jwt_required' }) };
  }
  const parsed = parsePreferredModelApplyBody(opts.body);
  if (!parsed.ok) {
    return { status: 400, body: JSON.stringify({ error: parsed.reason }) };
  }
  const patch = { preferred_model: parsed.preferred_model };
  const verified = await verifyRuntimeCommand({
    token,
    agentId: opts.agentId,
    issuer: opts.issuer,
    patch,
    jwks: opts.jwks,
  });
  if (!verified.ok) {
    return { status: 401, body: JSON.stringify({ error: verified.reason }) };
  }
  const applied = await applyPreferredModelOnListen({
    modelId: parsed.preferred_model,
    agentId: opts.agentId,
    apiKey: opts.apiKey,
    baseUrl: opts.baseUrl,
    supportedModels: opts.supportedModels,
    onSetPreferredModel: opts.onSetPreferredModel,
    spawnFn: opts.spawnFn,
    heartbeatFn: opts.heartbeatFn,
    logFn: opts.logFn,
  });
  if (!applied.ok) {
    return {
      status: applied.status,
      body: JSON.stringify({ error: applied.reason }),
    };
  }
  return {
    status: 200,
    body: JSON.stringify({ ok: true, preferred_model: applied.preferred_model }),
  };
}
