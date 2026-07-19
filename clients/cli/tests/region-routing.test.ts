import { afterEach, describe, expect, it } from 'vitest';
import {
  REGION_BASE_URLS,
  baseUrlForRegion,
  inferRegion,
  normalizeBaseUrl,
  resolveBaseUrl,
} from '../src/config.js';

describe('dual-region ACN routing', () => {
  const prev = process.env.ACN_BASE_URL;

  afterEach(() => {
    if (prev === undefined) delete process.env.ACN_BASE_URL;
    else process.env.ACN_BASE_URL = prev;
  });

  it('maps region presets', () => {
    expect(baseUrlForRegion('global')).toBe(REGION_BASE_URLS.global);
    expect(baseUrlForRegion('CN')).toBe(REGION_BASE_URLS.cn);
    expect(() => baseUrlForRegion('eu')).toThrow(/Unknown region/);
  });

  it('infers region from known origins', () => {
    expect(inferRegion(REGION_BASE_URLS.global)).toBe('global');
    expect(inferRegion(REGION_BASE_URLS.cn + '/')).toBe('cn');
    expect(inferRegion('https://self-hosted.example')).toBeUndefined();
  });

  it('strips trailing slash and mistaken /api/v1', () => {
    expect(normalizeBaseUrl('https://acn.acnlabs.cn/')).toBe(REGION_BASE_URLS.cn);
    expect(normalizeBaseUrl('https://acn.acnlabs.cn/api/v1')).toBe(REGION_BASE_URLS.cn);
    expect(normalizeBaseUrl('https://acn.acnlabs.cn/api/v1/')).toBe(REGION_BASE_URLS.cn);
    expect(normalizeBaseUrl('https://api.acnlabs.dev/api/v1')).toBe(
      REGION_BASE_URLS.global,
    );
  });

  it('resolveBaseUrl precedence: override > env > default', () => {
    process.env.ACN_BASE_URL = 'https://env.example';
    expect(resolveBaseUrl({ region: 'cn' })).toBe(REGION_BASE_URLS.cn);
    expect(resolveBaseUrl({ base_url: 'https://custom.example/' })).toBe(
      'https://custom.example',
    );
    expect(resolveBaseUrl({ base_url: 'https://custom.example/api/v1' })).toBe(
      'https://custom.example',
    );
    expect(resolveBaseUrl()).toBe('https://env.example');
    delete process.env.ACN_BASE_URL;
    // Without a config file in the test env, falls back to hosted global.
    expect(resolveBaseUrl()).toBe(REGION_BASE_URLS.global);
  });

  it('env base_url drives inferred region (not a stale file label)', () => {
    process.env.ACN_BASE_URL = REGION_BASE_URLS.cn;
    expect(inferRegion(resolveBaseUrl())).toBe('cn');
    process.env.ACN_BASE_URL = 'https://acn.acnlabs.cn/api/v1';
    expect(inferRegion(resolveBaseUrl())).toBe('cn');
  });
});
