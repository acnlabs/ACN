import { loadConfig } from './config.js';

export class AcnApiError extends Error {
  constructor(
    public status: number,
    public body: unknown,
    message: string
  ) {
    super(message);
    this.name = 'AcnApiError';
  }
}

export async function acnFetch<T>(
  path: string,
  options: RequestInit & { params?: Record<string, string | number | boolean | undefined> } = {}
): Promise<T> {
  const config = loadConfig();
  const { params, ...fetchOptions } = options;

  const url = new URL(`${config.base_url}/api/v1${path}`);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined) url.searchParams.set(k, String(v));
    }
  }

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(fetchOptions.headers as Record<string, string> | undefined),
  };

  if (config.api_key) {
    headers['Authorization'] = `Bearer ${config.api_key}`;
  }

  const res = await fetch(url.toString(), { ...fetchOptions, headers });

  if (!res.ok) {
    let body: unknown;
    try { body = await res.json(); } catch { body = await res.text(); }
    const detail =
      typeof body === 'object' && body !== null && 'detail' in body
        ? String((body as { detail: unknown }).detail)
        : String(body);
    throw new AcnApiError(res.status, body, `HTTP ${res.status}: ${detail}`);
  }

  if (res.status === 204 || res.headers.get('content-length') === '0') {
    return {} as T;
  }
  return res.json() as Promise<T>;
}

export function acnGet<T>(
  path: string,
  params?: Record<string, string | number | boolean | undefined>
): Promise<T> {
  return acnFetch<T>(path, { method: 'GET', params });
}

export function acnPost<T>(path: string, body?: unknown): Promise<T> {
  return acnFetch<T>(path, {
    method: 'POST',
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

export function acnDelete<T>(path: string): Promise<T> {
  return acnFetch<T>(path, { method: 'DELETE' });
}
