/**
 * Hosted ACN region presets (ADR-0013).
 *
 * Register where the agent is hosted: China infra → `cn`, overseas → `global`.
 * API keys are not portable across regions.
 */

export const ACN_HOSTED_URLS = {
  global: 'https://api.acnlabs.dev',
  cn: 'https://acn.acnlabs.cn',
} as const;

export type AcnRegion = keyof typeof ACN_HOSTED_URLS;

/** Strip trailing slashes and a mistaken `/api/v1` suffix. */
export function normalizeBaseUrl(url: string): string {
  let u = url.trim().replace(/\/+$/, '');
  u = u.replace(/\/api\/v1$/i, '');
  return u.replace(/\/+$/, '');
}

export function hostedBaseUrl(region: string): string {
  const key = region.trim().toLowerCase();
  if (key === 'global' || key === 'cn') {
    return ACN_HOSTED_URLS[key];
  }
  throw new Error(`Unknown region "${region}". Valid: global | cn`);
}

/**
 * Resolve origin: `baseUrl` → `region` → `ACN_BASE_URL` env.
 * Returns `undefined` when nothing is set (caller may default to localhost).
 */
export function resolveHostedBaseUrl(options: {
  region?: string;
  baseUrl?: string;
  env?: Record<string, string | undefined>;
}): string | undefined {
  if (options.baseUrl !== undefined && options.region !== undefined) {
    throw new Error('Use either baseUrl or region, not both');
  }
  if (options.baseUrl !== undefined) {
    return normalizeBaseUrl(options.baseUrl);
  }
  if (options.region !== undefined) {
    return hostedBaseUrl(options.region);
  }
  const environ = options.env ?? (typeof process !== 'undefined' ? process.env : {});
  const fromEnv = environ.ACN_BASE_URL?.trim();
  if (fromEnv) {
    return normalizeBaseUrl(fromEnv);
  }
  return undefined;
}
