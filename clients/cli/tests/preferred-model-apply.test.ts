import { describe, expect, it } from 'vitest';
import { exportJWK, generateKeyPair, SignJWT } from 'jose';

import {
  handleRuntimeApplyHttp,
  isOwnerPreferredModelApplyFrame,
  isPreferredModelApplyPath,
  modelAllowedBySupported,
  parsePreferredModelApplyBody,
  verifyRuntimeCommand,
} from '../src/commands/preferred-model-apply.js';

describe('isPreferredModelApplyPath', () => {
  it('matches the control-channel path with optional query or trailing slash', () => {
    expect(isPreferredModelApplyPath('/acn/v1/preferred-model')).toBe(true);
    expect(isPreferredModelApplyPath('/acn/v1/preferred-model/')).toBe(true);
    expect(isPreferredModelApplyPath('/acn/v1/preferred-model?x=1')).toBe(true);
    expect(isPreferredModelApplyPath('acn/v1/preferred-model')).toBe(true);
    expect(isPreferredModelApplyPath('/acn/v1/runtime')).toBe(true);
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
    expect(
      isOwnerPreferredModelApplyFrame({
        path: '/acn/v1/runtime',
        method: 'POST',
        headers: { 'x-acn-runtime-apply': '1' },
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

describe('verifyRuntimeCommand / handleRuntimeApplyHttp', () => {
  async function hostKeys() {
    const { publicKey, privateKey } = await generateKeyPair('RS256', {
      extractable: true,
    });
    const jwk = await exportJWK(publicKey);
    jwk.kid = 'test-runtime';
    jwk.use = 'sig';
    jwk.alg = 'RS256';
    return { privateKey, jwks: { keys: [jwk] } };
  }

  it('accepts a host runtime JWT and rejects an agent-shaped JWT', async () => {
    const { privateKey, jwks } = await hostKeys();
    const patch = { preferred_model: 'minimax/minimax-m2.5' };
    const token = await new SignJWT({
      acn_principal: 'host',
      acn_action: 'runtime',
      runtime: patch,
    })
      .setProtectedHeader({ alg: 'RS256', kid: 'test-runtime' })
      .setIssuer('https://acn.test')
      .setSubject('acn')
      .setAudience('agent-1')
      .setIssuedAt()
      .setExpirationTime('60s')
      .sign(privateKey);
    expect(
      await verifyRuntimeCommand({
        token,
        agentId: 'agent-1',
        issuer: 'https://acn.test',
        patch,
        jwks,
      })
    ).toEqual({ ok: true });

    const agentTok = await new SignJWT({ acn_principal: 'agent' })
      .setProtectedHeader({ alg: 'RS256', kid: 'test-runtime' })
      .setIssuer('https://acn.test')
      .setSubject('agent-1')
      .setAudience('https://api.test')
      .setIssuedAt()
      .setExpirationTime('60s')
      .sign(privateKey);
    const rejected = await verifyRuntimeCommand({
      token: agentTok,
      agentId: 'agent-1',
      issuer: 'https://acn.test',
      patch,
      jwks,
    });
    expect(rejected.ok).toBe(false);
  });

  it('applies after JWT verify and heartbeats', async () => {
    const { privateKey, jwks } = await hostKeys();
    const patch = { preferred_model: 'minimax/minimax-m2.5' };
    const token = await new SignJWT({
      acn_principal: 'host',
      acn_action: 'runtime',
      runtime: patch,
    })
      .setProtectedHeader({ alg: 'RS256', kid: 'test-runtime' })
      .setIssuer('https://acn.test')
      .setSubject('acn')
      .setAudience('agent-1')
      .setIssuedAt()
      .setExpirationTime('60s')
      .sign(privateKey);
    const heartbeatFn = async () => ({ ok: true as const });
    const out = await handleRuntimeApplyHttp({
      authorization: `Bearer ${token}`,
      body: JSON.stringify(patch),
      agentId: 'agent-1',
      issuer: 'https://acn.test',
      jwks,
      apiKey: 'k',
      baseUrl: 'https://acn.test',
      heartbeatFn,
    });
    expect(out.status).toBe(200);
    expect(JSON.parse(out.body).preferred_model).toBe(patch.preferred_model);
  });

  it('401s without a bearer token', async () => {
    const out = await handleRuntimeApplyHttp({
      body: '{"preferred_model":"a/b"}',
      agentId: 'agent-1',
      issuer: 'https://acn.test',
      jwks: { keys: [] },
      apiKey: 'k',
      baseUrl: 'https://acn.test',
    });
    expect(out.status).toBe(401);
  });
});
