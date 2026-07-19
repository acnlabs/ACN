import { afterEach, describe, expect, it } from 'vitest';
import { ACNClient } from './client';
import {
  ACN_HOSTED_URLS,
  hostedBaseUrl,
  normalizeBaseUrl,
  resolveHostedBaseUrl,
} from './regions';

describe('ADR-0013 hosted regions', () => {
  const prev = process.env.ACN_BASE_URL;

  afterEach(() => {
    if (prev === undefined) delete process.env.ACN_BASE_URL;
    else process.env.ACN_BASE_URL = prev;
  });

  it('maps presets and strips /api/v1', () => {
    expect(hostedBaseUrl('cn')).toBe(ACN_HOSTED_URLS.cn);
    expect(normalizeBaseUrl('https://acn.acnlabs.cn/api/v1/')).toBe(ACN_HOSTED_URLS.cn);
  });

  it('resolve precedence', () => {
    expect(resolveHostedBaseUrl({ region: 'global' })).toBe(ACN_HOSTED_URLS.global);
    expect(
      resolveHostedBaseUrl({ env: { ACN_BASE_URL: 'https://env.example/' } }),
    ).toBe('https://env.example');
    expect(() =>
      resolveHostedBaseUrl({ region: 'cn', baseUrl: 'https://x' }),
    ).toThrow(/not both/);
  });

  it('ACNClient accepts region', () => {
    const client = new ACNClient({ region: 'cn', apiKey: 'acn_test' });
    // baseUrl is private; hit a path builder via any cast for assertion
    expect((client as unknown as { baseUrl: string }).baseUrl).toBe(ACN_HOSTED_URLS.cn);
  });
});
