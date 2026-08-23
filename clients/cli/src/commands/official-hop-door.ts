/**
 * Local OpenAI-compatible door for official Mode B hops.
 *
 * CLI 1.0.9+ completes official hops itself (Host /chat/completions).
 * This door remains for skill `official_hop.py --door` and runtimes that
 * honor OPENAI_BASE_URL for a local tool loop. Standard OpenAI SDKs will
 * not add X-Hop-Id / X-Agent-Id; the door forwards those to Host.
 * Host may stream SSE; the door pipes it (1.0.11+). BYO hops never open a door.
 */

import http from 'node:http';
import type { IncomingMessage, ServerResponse } from 'node:http';

import { asHostInferenceUrl } from './normalize-event.js';

export type OfficialHopDoor = {
  baseUrl: string;
  close: () => Promise<void>;
};

/** Official hop may hit Host: path + hop + allowlisted URL + JWT. */
export function canCompleteOfficialHop(opts: {
  inferencePath?: string | null;
  hopId?: string | null;
  hostInferenceUrl?: string | null;
  jwt?: string | null;
}): boolean {
  return Boolean(
    opts.inferencePath === 'official' &&
      opts.hopId?.trim() &&
      asHostInferenceUrl(opts.hostInferenceUrl) &&
      opts.jwt?.trim()
  );
}

function readBody(req: IncomingMessage): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on('data', (c: Buffer) => chunks.push(Buffer.from(c)));
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

function send(
  res: ServerResponse,
  status: number,
  body: Buffer,
  contentType = 'application/json'
): void {
  res.writeHead(status, {
    'content-type': contentType,
    'content-length': String(body.length),
  });
  res.end(body);
}

function isSseContentType(contentType: string): boolean {
  return contentType.toLowerCase().includes('text/event-stream');
}

async function pipeWebStream(
  body: ReadableStream<Uint8Array>,
  res: ServerResponse
): Promise<void> {
  const reader = body.getReader();
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!value?.length) continue;
      const ok = res.write(Buffer.from(value));
      if (!ok) {
        await new Promise<void>((resolve) => res.once('drain', resolve));
      }
    }
    res.end();
  } catch (err) {
    if (!res.writableEnded) res.destroy(err instanceof Error ? err : undefined);
  } finally {
    try {
      reader.releaseLock();
    } catch {
      /* already released */
    }
  }
}

async function handleDoorRequest(
  req: IncomingMessage,
  res: ServerResponse,
  opts: {
    upstream: string;
    hopId: string;
    agentId: string;
    jwt: string;
    fetchFn: typeof fetch;
  }
): Promise<void> {
  const path = (req.url ?? '').split('?')[0];
  if (
    req.method !== 'POST' ||
    (path !== '/v1/chat/completions' && path !== '/chat/completions')
  ) {
    send(res, 404, Buffer.from('{"error":"not_found"}'));
    return;
  }

  let payload: unknown;
  try {
    const raw = await readBody(req);
    payload = JSON.parse(raw.length ? raw.toString('utf-8') : '{}');
  } catch {
    send(res, 400, Buffer.from('{"error":"invalid_json"}'));
    return;
  }
  if (payload === null || typeof payload !== 'object' || Array.isArray(payload)) {
    send(res, 400, Buffer.from('{"error":"invalid_json"}'));
    return;
  }

  const body = { ...(payload as Record<string, unknown>) };
  delete body.agent_id;
  body.hop_id = opts.hopId;

  const headers: Record<string, string> = {
    authorization: `Bearer ${opts.jwt}`,
    'content-type': 'application/json',
    'X-Hop-Id': opts.hopId,
  };
  if (opts.agentId) headers['X-Agent-Id'] = opts.agentId;

  try {
    const upstream = await opts.fetchFn(opts.upstream, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
    });
    const ct = upstream.headers.get('content-type') || 'application/json';
    const stream =
      (body.stream === true || isSseContentType(ct)) && upstream.body != null;
    if (stream) {
      res.writeHead(upstream.status, {
        'content-type': isSseContentType(ct) ? ct : 'text/event-stream',
        'cache-control': 'no-cache',
        connection: 'keep-alive',
        'x-accel-buffering': 'no',
      });
      await pipeWebStream(upstream.body, res);
      return;
    }
    const out = Buffer.from(await upstream.arrayBuffer());
    send(res, upstream.status, out, ct);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    send(
      res,
      502,
      Buffer.from(JSON.stringify({ error: `upstream_unreachable:${msg.slice(0, 120)}` }))
    );
  }
}

function closeServer(server: http.Server): Promise<void> {
  return new Promise((resolve) => {
    server.closeAllConnections?.();
    server.close(() => resolve());
  });
}

/** Loopback OpenAI door. Null when Host URL is not allowlisted or hop/jwt missing. */
export async function startOfficialHopDoor(opts: {
  hostInferenceUrl: string;
  hopId: string;
  agentId: string;
  jwt: string;
  fetchFn?: typeof fetch;
}): Promise<OfficialHopDoor | null> {
  const dest = asHostInferenceUrl(opts.hostInferenceUrl);
  const hopId = opts.hopId.trim();
  const jwt = opts.jwt.trim();
  if (!dest || !hopId || !jwt) return null;

  const fetchFn = opts.fetchFn ?? fetch;
  const upstream = `${dest}/chat/completions`;
  const server = http.createServer((req, res) => {
    void handleDoorRequest(req, res, {
      upstream,
      hopId,
      agentId: opts.agentId,
      jwt,
      fetchFn,
    });
  });

  try {
    await new Promise<void>((resolve, reject) => {
      server.once('error', reject);
      server.listen(0, '127.0.0.1', () => resolve());
    });
  } catch {
    return null;
  }

  const addr = server.address();
  const port = typeof addr === 'object' && addr ? addr.port : 0;
  if (!port) {
    await closeServer(server);
    return null;
  }

  return {
    baseUrl: `http://127.0.0.1:${port}/v1`,
    close: () => closeServer(server),
  };
}
