/**
 * Local OpenAI-compatible door for official Mode B hops.
 *
 * Standard OpenAI SDKs will not add X-Hop-Id / X-Agent-Id. Listen starts
 * this loopback server, injects OPENAI_BASE_URL / OPENAI_API_KEY into
 * --chat-complete-exec, and forwards POST /v1/chat/completions to Host.
 * BYO hops never open a door.
 */

import http from 'node:http';
import type { IncomingMessage, ServerResponse } from 'node:http';

import { asHostInferenceUrl } from './normalize-event.js';

export type OfficialHopDoor = {
  baseUrl: string;
  close: () => Promise<void>;
};

export function shouldOpenOfficialDoor(opts: {
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
    const out = Buffer.from(await upstream.arrayBuffer());
    const ct = upstream.headers.get('content-type') || 'application/json';
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
