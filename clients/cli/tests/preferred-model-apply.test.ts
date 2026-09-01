import { describe, expect, it } from 'vitest';

import {
  isOwnerPreferredModelApplyFrame,
  isPreferredModelApplyPath,
  modelAllowedBySupported,
  parsePreferredModelApplyBody,
} from '../src/commands/preferred-model-apply.js';

describe('isPreferredModelApplyPath', () => {
  it('matches the control-channel path with optional query or trailing slash', () => {
    expect(isPreferredModelApplyPath('/acn/v1/preferred-model')).toBe(true);
    expect(isPreferredModelApplyPath('/acn/v1/preferred-model/')).toBe(true);
    expect(isPreferredModelApplyPath('/acn/v1/preferred-model?x=1')).toBe(true);
    expect(isPreferredModelApplyPath('acn/v1/preferred-model')).toBe(true);
    expect(isPreferredModelApplyPath('/a2a')).toBe(false);
  });

  it('does not match a suffixed or prefixed lookalike', () => {
    expect(isPreferredModelApplyPath('/foo/acn/v1/preferred-model')).toBe(false);
    expect(isPreferredModelApplyPath('/acn/v1/preferred-model/extra')).toBe(false);
    expect(isPreferredModelApplyPath('/acn/v1/preferred-model-x')).toBe(false);
  });
});

describe('isOwnerPreferredModelApplyFrame', () => {
  const headers = { 'x-acn-preferred-model-apply': '1' };

  it('requires POST, owner marker, and no public caller header', () => {
    expect(
      isOwnerPreferredModelApplyFrame({
        path: '/acn/v1/preferred-model',
        method: 'POST',
        headers,
      })
    ).toBe(true);
  });

  it('rejects GET, missing marker, or X-ACN-Caller-Agent', () => {
    expect(
      isOwnerPreferredModelApplyFrame({
        path: '/acn/v1/preferred-model',
        method: 'GET',
        headers,
      })
    ).toBe(false);
    expect(
      isOwnerPreferredModelApplyFrame({
        path: '/acn/v1/preferred-model',
        method: 'POST',
        headers: { 'content-type': 'application/json' },
      })
    ).toBe(false);
    expect(
      isOwnerPreferredModelApplyFrame({
        path: '/acn/v1/preferred-model',
        method: 'POST',
        headers: { ...headers, 'X-ACN-Caller-Agent': 'attacker' },
      })
    ).toBe(false);
  });
});

describe('parsePreferredModelApplyBody', () => {
  it('requires preferred_model', () => {
    expect(parsePreferredModelApplyBody('{"preferred_model":"  a/b  "}')).toEqual({
      ok: true,
      preferred_model: 'a/b',
    });
    expect(parsePreferredModelApplyBody('{}').ok).toBe(false);
    expect(parsePreferredModelApplyBody('not-json').ok).toBe(false);
  });
});

describe('modelAllowedBySupported', () => {
  it('allows any model when the list is empty', () => {
    expect(modelAllowedBySupported('a/b')).toBe(true);
    expect(modelAllowedBySupported('a/b', [])).toBe(true);
  });

  it('is case-insensitive against the reported list', () => {
    expect(modelAllowedBySupported('A/B', ['a/b', 'c/d'])).toBe(true);
    expect(modelAllowedBySupported('x/y', ['a/b'])).toBe(false);
  });
});
