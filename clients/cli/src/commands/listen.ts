import { Command } from 'commander';
import { spawn } from 'child_process';
import type { ChildProcess } from 'child_process';
import WebSocket from 'ws';
import { loadConfig } from '../config.js';
import {
  buildChatWritebackOptions,
  DEFAULT_COMPLETE_TIMEOUT_MS,
  validateChatWritebackOptions,
  type ChatWritebackOptions,
} from './chat-writeback.js';
import { DedupeStore } from './normalize-event.js';
import { dispatchLocalReceiver } from './local-receiver.js';
import {
  DEFAULT_WAKE_TIMEOUT_MS,
  parseWakeHeaders,
  validateRuntimeOptions,
  type RuntimeId,
} from './runtime-adapter.js';
import {
  formatModelHeartbeatLog,
  postAgentHeartbeat,
  resolvePreferredModel,
  resolveSupportedModels,
} from './model-heartbeat.js';

/** Re-export model heartbeat helpers (tests historically imported from listen). */
export {
  formatModelHeartbeatLog,
  postAgentHeartbeat,
  resolvePreferredModel,
  resolveSupportedModels,
} from './model-heartbeat.js';

/**
 * ADR-0012 Mode B — agent-side listener.
 *
 * For an agent that registered without a public HTTP endpoint, this holds an
 * OUTBOUND WebSocket to ACN (so no inbound port / public IP / TLS is needed)
 * and answers requests that land at the agent's proxy address
 * (`{base}/api/v1/agents/{id}`) in real time.
 *
 * Frame protocol (see ADR-0012):
 *   ACN  -> agent : {type:"a2a_request",  id, method, path, headers, body, body_encoding, deadline_ms}
 *   agent -> ACN  : {type:"a2a_response", id, status, headers, body, body_encoding}
 *   keepalive     : {type:"ping"} ⇄ {type:"pong"}
 *
 * ADR-0012 P2d streaming (#171): when the forwarded local server answers with
 * an SSE response (content-type text/event-stream), the listener streams it
 * back as chunk frames instead of one buffered response:
 *   agent -> ACN  : {type:"a2a_stream_chunk", id, seq, data, data_encoding}
 *   agent -> ACN  : {type:"a2a_stream_end",   id, status?, error?}
 * Non-SSE responses keep using a single a2a_response — purely additive.
 *
 * Handler modes:
 *   --runtime http|command|log  built-in A2A receiver + wake host (production default)
 *   --forward <url>             tunnel to a local A2A server (compat; SSE streamed)
 *   --exec <command>            stdout = full A2A JSON-RPC response (compat; buffered)
 *
 * Chat writeback (optional, with --runtime):
 *   When a relayed message carries metadata.agentplanet.chat_id + reply_path,
 *   ask the host for {"content"} then POST Chat Gateway agent-messages.
 *   Task / Org wakes keep using --wake-url / --wake-exec.
 */

export interface A2aRequestFrame {
  type: 'a2a_request';
  id: string;
  method?: string;
  path?: string;
  headers?: Record<string, string>;
  body?: string;
  body_encoding?: 'utf-8' | 'base64';
  deadline_ms?: number;
}

export interface A2aResponseFrame {
  type: 'a2a_response';
  id: string;
  status: number;
  headers: Record<string, string>;
  body: string;
  body_encoding?: 'utf-8' | 'base64';
}

// ADR-0012 P2d streaming (#171) — agent -> ACN reply frames for an SSE response.
export interface A2aStreamChunkFrame {
  type: 'a2a_stream_chunk';
  id: string;
  seq: number;
  data: string;
  data_encoding?: 'utf-8' | 'base64';
}

export interface A2aStreamEndFrame {
  type: 'a2a_stream_end';
  id: string;
  status?: number;
  error?: string;
}

export type OutboundFrame = A2aResponseFrame | A2aStreamChunkFrame | A2aStreamEndFrame;

/** Emit one reply frame to ACN over the control channel. */
export type SendFrame = (frame: OutboundFrame) => void;

export interface RuntimeHandlerOptions {
  runtime: RuntimeId;
  wakeUrl?: string;
  wakeHeaders?: Record<string, string>;
  wakeExec?: string;
  wakeTimeoutMs?: number;
  dedupe: boolean;
  dedupeTtlSec: number;
  chatWriteback?: ChatWritebackOptions;
}

export interface HandlerOptions {
  forward?: string;
  exec?: string;
  runtime?: RuntimeHandlerOptions;
}

interface HandlerDeps {
  fetchFn?: typeof fetch;
  spawnFn?: typeof spawn;
  logFn?: (line: string) => void;
  /** Shared across requests when using --runtime (set by runListener). */
  dedupeStore?: DedupeStore;
}

// Headers that must not be replayed to the downstream handler: they describe
// the ACN<->agent transport, not the original request, and replaying them
// corrupts the local request (wrong Host, stale Content-Length, etc.).
const STRIP_HEADERS = new Set([
  'host',
  'content-length',
  'connection',
  'transfer-encoding',
]);

/** Derive the WebSocket control-channel URL from the REST base URL. */
export function toWebsocketUrl(baseUrl: string, agentId: string): string {
  const u = new URL(baseUrl);
  u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
  u.pathname = `/ws/${agentId}`;
  u.search = '';
  return u.toString();
}

function decodeBody(frame: A2aRequestFrame): Buffer {
  if (frame.body_encoding === 'base64') {
    return Buffer.from(frame.body ?? '', 'base64');
  }
  return Buffer.from(frame.body ?? '', 'utf-8');
}

function errorResponse(id: string, status: number, detail: string): A2aResponseFrame {
  return {
    type: 'a2a_response',
    id,
    status,
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ error: detail }),
  };
}

/**
 * Dispatch a relayed request to the configured handler, emitting one or more
 * reply frames via ``send``. This is the streaming-capable entry used by the
 * live socket: a ``--forward`` SSE response is streamed as chunk frames, every
 * other case emits a single ``a2a_response``. Pure except for the injected
 * ``fetch`` / ``spawn`` deps, so it is unit testable without a live socket.
 *
 * For ``--runtime``, the A2A response is emitted before wake completes.
 */
export async function dispatchA2aRequest(
  frame: A2aRequestFrame,
  opts: HandlerOptions,
  send: SendFrame,
  deps: HandlerDeps = {}
): Promise<void> {
  const bodyBuf = decodeBody(frame);
  try {
    if (opts.runtime) {
      const store =
        deps.dedupeStore ?? new DedupeStore(opts.runtime.dedupeTtlSec);
      dispatchLocalReceiver(
        frame.id,
        bodyBuf.toString('utf-8'),
        opts.runtime,
        store,
        send,
        {
          fetchFn: deps.fetchFn,
          spawnFn: deps.spawnFn,
          logFn: deps.logFn,
        }
      );
      return;
    }
    if (opts.forward) {
      await forwardToHttp(frame, bodyBuf, opts.forward, deps.fetchFn ?? fetch, send);
      return;
    }
    if (opts.exec) {
      send(await runExec(frame, bodyBuf, opts.exec, deps.spawnFn ?? spawn));
      return;
    }
    send(errorResponse(frame.id, 500, 'no handler configured'));
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    send(errorResponse(frame.id, 502, `handler failed: ${msg}`));
  }
}

/**
 * Back-compat single-frame API: collect the frames ``dispatchA2aRequest``
 * emits into one ``A2aResponseFrame``. A streamed (SSE) response is buffered by
 * concatenating its chunks. Kept for non-streaming callers and unit tests.
 */
export async function handleA2aRequest(
  frame: A2aRequestFrame,
  opts: HandlerOptions,
  deps: HandlerDeps = {}
): Promise<A2aResponseFrame> {
  const frames: OutboundFrame[] = [];
  await dispatchA2aRequest(frame, opts, (f) => frames.push(f), deps);

  if (frames.length === 1 && frames[0].type === 'a2a_response') {
    return frames[0];
  }
  // Streamed response: fold the chunks back into a single buffered body.
  const chunks = frames.filter(
    (f): f is A2aStreamChunkFrame => f.type === 'a2a_stream_chunk'
  );
  if (chunks.length > 0) {
    const buf = Buffer.concat(
      chunks.map((c) =>
        c.data_encoding === 'base64'
          ? Buffer.from(c.data, 'base64')
          : Buffer.from(c.data, 'utf-8')
      )
    );
    const end = frames.find(
      (f): f is A2aStreamEndFrame => f.type === 'a2a_stream_end'
    );
    return {
      type: 'a2a_response',
      id: frame.id,
      status: end?.status ?? 200,
      headers: { 'content-type': 'text/event-stream' },
      body: buf.toString('utf-8'),
    };
  }
  const single = frames.find(
    (f): f is A2aResponseFrame => f.type === 'a2a_response'
  );
  return single ?? errorResponse(frame.id, 500, 'no response produced');
}

/** Headers to replay to the downstream handler (transport headers stripped). */
function buildForwardHeaders(frame: A2aRequestFrame): Record<string, string> {
  const headers: Record<string, string> = {};
  for (const [k, v] of Object.entries(frame.headers ?? {})) {
    if (!STRIP_HEADERS.has(k.toLowerCase())) headers[k] = v;
  }
  return headers;
}

async function forwardToHttp(
  frame: A2aRequestFrame,
  bodyBuf: Buffer,
  base: string,
  fetchFn: typeof fetch,
  send: SendFrame
): Promise<void> {
  const suffix =
    frame.path && frame.path !== '/' ? '/' + frame.path.replace(/^\//, '') : '';
  const targetUrl = base.replace(/\/$/, '') + suffix;
  const method = (frame.method ?? 'POST').toUpperCase();

  const init: RequestInit = { method, headers: buildForwardHeaders(frame) };
  if (method !== 'GET' && method !== 'HEAD') {
    init.body = bodyBuf;
  }

  const res = await fetchFn(targetUrl, init);
  const contentType = res.headers.get('content-type') ?? 'application/json';

  // ADR-0012 P2d (#171): an SSE response is streamed back chunk-by-chunk.
  // Chunks are base64-encoded so a multi-byte UTF-8 sequence split across two
  // network reads is never corrupted — ACN reassembles the raw bytes in order.
  if (contentType.includes('text/event-stream') && res.body) {
    const reader = (res.body as ReadableStream<Uint8Array>).getReader();
    let seq = 0;
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value && value.length > 0) {
          send({
            type: 'a2a_stream_chunk',
            id: frame.id,
            seq: seq++,
            data: Buffer.from(value).toString('base64'),
            data_encoding: 'base64',
          });
        }
      }
      send({ type: 'a2a_stream_end', id: frame.id, status: res.status });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      send({ type: 'a2a_stream_end', id: frame.id, error: msg });
    } finally {
      reader.releaseLock();
    }
    return;
  }

  const respText = await res.text();
  send({
    type: 'a2a_response',
    id: frame.id,
    status: res.status,
    headers: { 'content-type': contentType },
    body: respText,
  });
}

function runExec(
  frame: A2aRequestFrame,
  bodyBuf: Buffer,
  command: string,
  spawnFn: typeof spawn
): Promise<A2aResponseFrame> {
  return new Promise((resolve) => {
    const child: ChildProcess = spawnFn(command, { shell: true });
    const out: Buffer[] = [];
    const errOut: Buffer[] = [];

    child.stdout?.on('data', (d: Buffer) => out.push(Buffer.from(d)));
    child.stderr?.on('data', (d: Buffer) => errOut.push(Buffer.from(d)));
    child.on('error', (e: Error) =>
      resolve(errorResponse(frame.id, 502, `exec error: ${e.message}`))
    );
    child.on('close', (code: number | null) => {
      const stdout = Buffer.concat(out).toString('utf-8');
      if (code === 0) {
        resolve({
          type: 'a2a_response',
          id: frame.id,
          status: 200,
          headers: { 'content-type': 'application/json' },
          body: stdout,
        });
      } else {
        const stderr = Buffer.concat(errOut).toString('utf-8');
        resolve(
          errorResponse(frame.id, 500, `exec exited ${code}: ${stderr.slice(0, 500)}`)
        );
      }
    });

    child.stdin?.end(bodyBuf);
  });
}

function rawToString(data: WebSocket.RawData): string {
  if (typeof data === 'string') return data;
  if (Buffer.isBuffer(data)) return data.toString('utf-8');
  if (Array.isArray(data)) return Buffer.concat(data).toString('utf-8');
  return Buffer.from(data as ArrayBuffer).toString('utf-8');
}

interface ListenerConfig extends HandlerOptions {
  agentId: string;
  apiKey: string;
  baseUrl: string;
  /** Host Catalog model id — self-reported via REST heartbeat while listening. */
  preferredModel?: string;
  /**
   * Models this runtime can run — self-reported for Interfaze composer.
   * ``[]`` clears server metadata; ``undefined`` leaves unchanged.
   */
  supportedModels?: string[];
}

const KEEPALIVE_INTERVAL_MS = 30_000;
/** Explicit REST heartbeat with model fields (WS ping does not carry model). */
const MODEL_HEARTBEAT_INTERVAL_MS = 15 * 60_000;
const INITIAL_BACKOFF_MS = 1_000;
const MAX_BACKOFF_MS = 30_000;

function runListener(cfg: ListenerConfig): void {
  const wsUrl = toWebsocketUrl(cfg.baseUrl, cfg.agentId);
  let backoff = INITIAL_BACKOFF_MS;
  let stopped = false;
  const dedupeStore = cfg.runtime
    ? new DedupeStore(cfg.runtime.dedupeTtlSec)
    : undefined;

  const connect = (): void => {
    const ws = new WebSocket(wsUrl, {
      headers: { Authorization: `Bearer ${cfg.apiKey}` },
    });
    let keepalive: ReturnType<typeof setInterval> | undefined;
    let modelHeartbeat: ReturnType<typeof setInterval> | undefined;

    const sendModelHeartbeat = (): void => {
      if (stopped) return;
      // ``[]`` is a deliberate clear — still heartbeat.
      if (!cfg.preferredModel && cfg.supportedModels === undefined) return;
      void postAgentHeartbeat({
        baseUrl: cfg.baseUrl,
        agentId: cfg.agentId,
        apiKey: cfg.apiKey,
        preferredModel: cfg.preferredModel,
        supportedModels: cfg.supportedModels,
      }).then((r) => {
        if (!r.ok) {
          console.error(`[acn listen] model heartbeat failed: ${r.reason}`);
          return;
        }
        const note = formatModelHeartbeatLog({
          preferred: r.preferred_model ?? cfg.preferredModel,
          supported: r.supported_models ?? cfg.supportedModels,
        });
        console.error(`[acn listen] model heartbeat ok${note}`);
      });
    };

    ws.on('open', () => {
      // Status to stderr so stdout stays clean for pipe consumers.
      const mode = cfg.runtime
        ? `runtime=${cfg.runtime.runtime}`
        : cfg.forward
          ? `forward=${cfg.forward}`
          : `exec`;
      const modelNote = formatModelHeartbeatLog({
        preferred: cfg.preferredModel,
        supported: cfg.supportedModels,
      });
      console.error(
        `[acn listen] connected as ${cfg.agentId} → ${wsUrl} (${mode})${modelNote}`
      );
      backoff = INITIAL_BACKOFF_MS;
      keepalive = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'ping' }));
        }
      }, KEEPALIVE_INTERVAL_MS);
      // REST heartbeat carries model fields; WS ping alone cannot.
      if (cfg.preferredModel || cfg.supportedModels !== undefined) {
        sendModelHeartbeat();
        modelHeartbeat = setInterval(sendModelHeartbeat, MODEL_HEARTBEAT_INTERVAL_MS);
      }
    });

    ws.on('message', (data: WebSocket.RawData) => {
      let frame: unknown;
      try {
        frame = JSON.parse(rawToString(data));
      } catch {
        return;
      }
      if (!frame || typeof frame !== 'object') return;
      const f = frame as Partial<A2aRequestFrame>;
      if (f.type === 'a2a_request' && typeof f.id === 'string') {
        // Stream-aware: dispatch emits one a2a_response, OR a run of
        // a2a_stream_chunk frames + a2a_stream_end for an SSE response (P2d).
        const send: SendFrame = (out) => {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify(out));
          }
        };
        void dispatchA2aRequest(f as A2aRequestFrame, cfg, send, { dedupeStore });
      }
      // {type:"pong"} keepalive ack and the {type:"system"} welcome frame
      // need no action.
    });

    ws.on('close', (code: number, reason: Buffer) => {
      if (keepalive) clearInterval(keepalive);
      if (modelHeartbeat) clearInterval(modelHeartbeat);
      if (stopped) return;
      // 4401/4403 are auth failures (bad/mismatched API key) and 4429 is
      // "too many connections" — none are transient, so retrying forever
      // just hammers the server. Surface and exit.
      if (code === 4401 || code === 4403 || code === 4429) {
        console.error(
          `[acn listen] fatal close (code ${code}): ${reason.toString() || 'see api_key / agent_id'}`
        );
        process.exit(1);
      }
      console.error(
        `[acn listen] disconnected (code ${code}); reconnecting in ${backoff / 1000}s`
      );
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
    });

    ws.on('error', (err: Error) => {
      // 'close' fires right after and owns the reconnect decision; just log.
      console.error(`[acn listen] socket error: ${err.message}`);
    });
  };

  process.on('SIGINT', () => {
    stopped = true;
    console.error('\n[acn listen] stopping');
    process.exit(0);
  });

  connect();
}

function collectWakeHeader(value: string, previous: string[]): string[] {
  previous.push(value);
  return previous;
}

/** Validate mutually exclusive handler flags. Returns an error message or null. */
export function validateListenHandlerFlags(opts: {
  runtime?: string;
  forward?: string;
  exec?: string;
  wakeUrl?: string;
  wakeExec?: string;
}): string | null {
  const modes = [opts.runtime, opts.forward, opts.exec].filter(Boolean);
  if (modes.length === 0) {
    return (
      'Provide a handler: --runtime http|command|log (recommended), ' +
      'or legacy --forward <url> / --exec <command>.'
    );
  }
  if (modes.length > 1) {
    return 'Use only one handler: --runtime, --forward, or --exec — not combined.';
  }
  return validateRuntimeOptions({
    runtime: opts.runtime,
    wakeUrl: opts.wakeUrl,
    wakeExec: opts.wakeExec,
  });
}

export function listenCommand(): Command {
  const cmd = new Command('listen')
    .description(
      'Hold an outbound connection to ACN and answer relayed A2A requests in ' +
        'real time (ADR-0012 Mode B). Prefer --runtime for production; ' +
        '--forward/--exec remain as compatibility tunnels.'
    )
    .option(
      '--runtime <id>',
      'Built-in A2A receiver + wake host: http | command | log (no local A2A port)'
    )
    .option('--wake-url <url>', 'POST target for --runtime http')
    .option(
      '--wake-header <k:v>',
      'Extra header for --runtime http (repeatable)',
      collectWakeHeader,
      [] as string[]
    )
    .option(
      '--wake-exec <cmd>',
      'Shell command for --runtime command (event JSON on stdin). ' +
        'Not the same as legacy --exec (which must print a full A2A response).'
    )
    .option(
      '--wake-timeout <ms>',
      'Wake timeout in ms (default 5000)',
      String(DEFAULT_WAKE_TIMEOUT_MS)
    )
    .option('--no-dedupe', 'Disable in-process task/message id dedupe (default: on)')
    .option('--dedupe-ttl <sec>', 'Dedupe window seconds (default 3600)', '3600')
    .option(
      '--forward <url>',
      'Compat: tunnel each request to a local A2A HTTP server'
    )
    .option(
      '--exec <command>',
      'Compat: shell per request; stdout must be a full A2A JSON-RPC response'
    )
    .option(
      '--chat-writeback',
      'On Chat Gateway envelopes: complete host reply then POST agent-messages ' +
        'with ACN agent JWT (requires --runtime; uses config api-key)'
    )
    .option(
      '--chat-api-base <url>',
      'Chat Gateway origin for writeback ' +
        '(env: ACN_CHAT_API_BASE or AGENTPLANET_API_BASE)'
    )
    .option(
      '--chat-token <token>',
      'Deprecated/ignored: writeback now mints ACN agent JWT from api-key'
    )
    .option(
      '--chat-complete-url <url>',
      'BYO complete URL (optional if official-only; mutually exclusive with --chat-complete-exec)'
    )
    .option(
      '--chat-complete-exec <cmd>',
      'BYO only (optional if official-only): stdin event → stdout {"content"}. Official hops complete via Host.'
    )
    .option(
      '--chat-complete-timeout <ms>',
      `Host complete timeout in ms (default ${DEFAULT_COMPLETE_TIMEOUT_MS})`,
      String(DEFAULT_COMPLETE_TIMEOUT_MS)
    )
    .option('-i, --agent-id <id>', 'Agent ID (defaults to config)')
    .option(
      '-m, --model <modelId>',
      'Declare runtime model (Host Catalog id) on connect + every 15m via REST heartbeat ' +
        '(self-reported; env: ACN_PREFERRED_MODEL)'
    )
    .option(
      '--supported-models <ids>',
      'Comma-separated Host Catalog model ids this runtime can run ' +
        '(Interfaze composer dropdown; env: ACN_SUPPORTED_MODELS)'
    )
    .option(
      '--clear-supported-models',
      'Clear metadata.supported_models on the server (sends empty list)'
    )
    .action(
      (opts: {
        runtime?: string;
        wakeUrl?: string;
        wakeHeader?: string[];
        wakeExec?: string;
        wakeTimeout?: string;
        dedupe?: boolean;
        dedupeTtl?: string;
        forward?: string;
        exec?: string;
        chatWriteback?: boolean;
        chatApiBase?: string;
        chatToken?: string;
        chatCompleteUrl?: string;
        chatCompleteExec?: string;
        chatCompleteTimeout?: string;
        agentId?: string;
        model?: string;
        supportedModels?: string;
        clearSupportedModels?: boolean;
      }) => {
        const config = loadConfig();
        const apiKey = config.api_key;
        const agentId = opts.agentId ?? config.agent_id;

        if (!apiKey) {
          console.error(
            'No API key found. Run `acn join` first or `acn config set api-key <key>`.'
          );
          process.exit(1);
        }
        if (!agentId) {
          console.error(
            'No agent ID found. Run `acn join` first or `acn config set agent-id <id>`.'
          );
          process.exit(1);
        }

        const flagErr = validateListenHandlerFlags({
          runtime: opts.runtime,
          forward: opts.forward,
          exec: opts.exec,
          wakeUrl: opts.wakeUrl,
          wakeExec: opts.wakeExec,
        });
        if (flagErr) {
          console.error(flagErr);
          process.exit(1);
        }

        const chatApiBase =
          opts.chatApiBase?.trim() ||
          process.env.ACN_CHAT_API_BASE?.trim() ||
          process.env.AGENTPLANET_API_BASE?.trim();
        const chatCompleteUrl =
          opts.chatCompleteUrl?.trim() ||
          process.env.ACN_CHAT_COMPLETE_URL?.trim();
        const chatCompleteExec = opts.chatCompleteExec?.trim();

        if (opts.chatWriteback && !opts.runtime) {
          console.error('--chat-writeback requires --runtime http|command|log.');
          process.exit(1);
        }
        if (
          opts.chatWriteback &&
          (opts.chatToken?.trim() ||
            process.env.ACN_CHAT_WRITEBACK_TOKEN?.trim() ||
            process.env.AGENTPLANET_INTERNAL_TOKEN?.trim() ||
            process.env.AGENTPLANET_INTERNAL_API_TOKEN?.trim())
        ) {
          console.error(
            '[acn listen] warning: --chat-token / AGENTPLANET_INTERNAL_TOKEN is ignored; ' +
              'writeback authenticates with ACN agent JWT minted from your api-key.'
          );
        }

        const chatErr = validateChatWritebackOptions({
          chatWriteback: opts.chatWriteback,
          chatApiBase,
          chatCompleteUrl,
          chatCompleteExec,
          agentId,
          apiKey,
        });
        if (chatErr) {
          console.error(chatErr);
          process.exit(1);
        }

        let wakeHeaders: Record<string, string> | undefined;
        if (opts.runtime === 'http') {
          try {
            wakeHeaders = parseWakeHeaders(opts.wakeHeader);
          } catch (err) {
            console.error(err instanceof Error ? err.message : String(err));
            process.exit(1);
          }
        }

        const wakeTimeoutMs = Number.parseInt(opts.wakeTimeout ?? '', 10);
        const dedupeTtlSec = Number.parseInt(opts.dedupeTtl ?? '', 10);
        const chatCompleteTimeoutMs = Number.parseInt(
          opts.chatCompleteTimeout ?? '',
          10
        );
        if (opts.runtime && (!Number.isFinite(wakeTimeoutMs) || wakeTimeoutMs <= 0)) {
          console.error('--wake-timeout must be a positive integer (ms).');
          process.exit(1);
        }
        if (opts.runtime && (!Number.isFinite(dedupeTtlSec) || dedupeTtlSec <= 0)) {
          console.error('--dedupe-ttl must be a positive integer (seconds).');
          process.exit(1);
        }
        if (
          opts.chatWriteback &&
          (!Number.isFinite(chatCompleteTimeoutMs) || chatCompleteTimeoutMs <= 0)
        ) {
          console.error('--chat-complete-timeout must be a positive integer (ms).');
          process.exit(1);
        }

        const chatWriteback = buildChatWritebackOptions({
          chatWriteback: opts.chatWriteback,
          chatApiBase,
          acnBaseUrl: config.base_url,
          apiKey: apiKey!,
          chatCompleteUrl,
          chatCompleteExec,
          chatCompleteTimeoutMs: opts.chatWriteback
            ? chatCompleteTimeoutMs
            : undefined,
          agentId,
          audience:
            process.env.ACN_CHAT_JWT_AUDIENCE?.trim() ||
            process.env.AGENTPLANET_JWT_AUDIENCE?.trim(),
        });

        const preferredModel = resolvePreferredModel({ model: opts.model });
        if (opts.clearSupportedModels && opts.supportedModels?.trim()) {
          console.error(
            'Use either --supported-models or --clear-supported-models, not both.'
          );
          process.exit(1);
        }
        const supportedModels = resolveSupportedModels({
          models: opts.supportedModels,
          clear: !!opts.clearSupportedModels,
        });

        runListener({
          agentId,
          apiKey: apiKey!,
          baseUrl: config.base_url,
          preferredModel,
          supportedModels,
          forward: opts.forward,
          exec: opts.exec,
          runtime: opts.runtime
            ? {
                runtime: opts.runtime as RuntimeId,
                wakeUrl: opts.wakeUrl,
                wakeHeaders,
                wakeExec: opts.wakeExec,
                wakeTimeoutMs,
                // commander: --no-dedupe sets dedupe=false; default true
                dedupe: opts.dedupe !== false,
                dedupeTtlSec,
                chatWriteback,
              }
            : undefined,
        });
      }
    );

  return cmd;
}
