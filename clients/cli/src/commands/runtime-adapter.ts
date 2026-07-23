/**
 * Runtime wake adapters for `acn listen --runtime`.
 * CLI already answered A2A; these only notify the host agent.
 */

import { spawn } from 'child_process';
import type { ChildProcess } from 'child_process';
import type { NormalizedEvent } from './normalize-event.js';

export type RuntimeId = 'http' | 'command' | 'log';

export interface RuntimeWakeOptions {
  runtime: RuntimeId;
  wakeUrl?: string;
  wakeHeaders?: Record<string, string>;
  wakeExec?: string;
  wakeTimeoutMs?: number;
}

export type WakeResult =
  | { ok: true }
  | { ok: false; reason: string };

export interface WakeDeps {
  fetchFn?: typeof fetch;
  spawnFn?: typeof spawn;
  logFn?: (line: string) => void;
}

const DEFAULT_WAKE_TIMEOUT_MS = 5000;

/** Parse repeated `--wake-header 'Key: Value'` strings. */
export function parseWakeHeaders(raw: string[] | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const item of raw ?? []) {
    const idx = item.indexOf(':');
    if (idx <= 0) {
      throw new Error(
        `Invalid --wake-header "${item}". Expected "Header-Name: value".`
      );
    }
    const key = item.slice(0, idx).trim();
    const value = item.slice(idx + 1).trim();
    if (!key) {
      throw new Error(
        `Invalid --wake-header "${item}". Expected "Header-Name: value".`
      );
    }
    out[key] = value;
  }
  return out;
}

export function validateRuntimeOptions(opts: {
  runtime?: string;
  wakeUrl?: string;
  wakeExec?: string;
}): string | null {
  if (!opts.runtime) return null;
  if (opts.runtime !== 'http' && opts.runtime !== 'command' && opts.runtime !== 'log') {
    return `Unknown --runtime "${opts.runtime}". Use: http | command | log`;
  }
  if (opts.runtime === 'http' && !opts.wakeUrl) {
    return '--runtime http requires --wake-url <url>';
  }
  if (opts.runtime === 'command' && !opts.wakeExec) {
    return '--runtime command requires --wake-exec <cmd>';
  }
  return null;
}

async function wakeHttp(
  event: NormalizedEvent,
  opts: RuntimeWakeOptions,
  deps: WakeDeps
): Promise<WakeResult> {
  const fetchFn = deps.fetchFn ?? fetch;
  const timeoutMs = opts.wakeTimeoutMs ?? DEFAULT_WAKE_TIMEOUT_MS;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetchFn(opts.wakeUrl!, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        ...(opts.wakeHeaders ?? {}),
      },
      body: JSON.stringify(event),
      signal: controller.signal,
    });
    // Drain the body so keep-alive sockets are not left half-open.
    try {
      await res.arrayBuffer();
    } catch {
      /* ignore drain errors */
    }
    if (res.status < 200 || res.status >= 300) {
      return { ok: false, reason: `http_${res.status}` };
    }
    return { ok: true };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (controller.signal.aborted || /abort|timeout/i.test(msg)) {
      return { ok: false, reason: 'timeout' };
    }
    return { ok: false, reason: msg.slice(0, 200) };
  } finally {
    clearTimeout(timer);
  }
}

function wakeCommand(
  event: NormalizedEvent,
  opts: RuntimeWakeOptions,
  deps: WakeDeps
): Promise<WakeResult> {
  const spawnFn = deps.spawnFn ?? spawn;
  const timeoutMs = opts.wakeTimeoutMs ?? DEFAULT_WAKE_TIMEOUT_MS;
  const body = Buffer.from(JSON.stringify(event), 'utf-8');

  return new Promise((resolve) => {
    let settled = false;
    const child: ChildProcess = spawnFn(opts.wakeExec!, { shell: true });
    const errOut: Buffer[] = [];

    const finish = (result: WakeResult) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };

    const timer = setTimeout(() => {
      child.kill('SIGTERM');
      finish({ ok: false, reason: 'timeout' });
    }, timeoutMs);

    child.stderr?.on('data', (d: Buffer) => errOut.push(Buffer.from(d)));
    child.on('error', (e: Error) =>
      finish({ ok: false, reason: e.message.slice(0, 200) })
    );
    child.on('close', (code: number | null) => {
      if (code === 0) finish({ ok: true });
      else {
        const detail = Buffer.concat(errOut).toString('utf-8').slice(0, 80);
        finish({
          ok: false,
          reason: detail ? `exit_${code}:${detail}` : `exit_${code}`,
        });
      }
    });

    child.stdin?.end(body);
  });
}

function wakeLog(event: NormalizedEvent, deps: WakeDeps): WakeResult {
  const logFn = deps.logFn ?? ((line: string) => console.error(line));
  logFn(JSON.stringify(event));
  return { ok: true };
}

/** Wake the host runtime. Never throws. */
export async function wakeRuntime(
  event: NormalizedEvent,
  opts: RuntimeWakeOptions,
  deps: WakeDeps = {}
): Promise<WakeResult> {
  switch (opts.runtime) {
    case 'http':
      return wakeHttp(event, opts, deps);
    case 'command':
      return wakeCommand(event, opts, deps);
    case 'log':
      return wakeLog(event, deps);
    default:
      return { ok: false, reason: `unknown_runtime` };
  }
}

export { DEFAULT_WAKE_TIMEOUT_MS };
