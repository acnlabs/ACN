/**
 * TypeScript SDK regression tests for `CommunicationProfile` and
 * `CommunicationPolicyResponse` — manifest-mode reachability fields
 * that landed in server PR #87.
 *
 * Pinned behaviours:
 *
 * - `getCommunicationProfile` surfaces the new
 *   `unread_manifest_count: number` field unchanged from the wire
 *   payload, so senders can detect manifest-queue buildup before
 *   committing an attention_fee.
 * - `updatePolicy` returns the server payload verbatim, including
 *   the conditional `warning?: string` field that the server emits
 *   only when the post-update mode requires active polling
 *   (`'manifest'` / `'allowlist'`). `warning` is absent for
 *   non-gated modes (`'open'` / `'closed'`).
 *
 * Implementation strategy mirrors `admission.test.ts`: stub
 * `globalThis.fetch` per test and assert on the (url, init, body)
 * tuple plus the parsed JS return value.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ACNClient } from './client';

interface CapturedRequest {
  url: URL;
  init: RequestInit;
}

function setupFetchStub(
  status: number,
  body: unknown,
): { client: ACNClient; calls: CapturedRequest[] } {
  const calls: CapturedRequest[] = [];
  const fetchStub = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const urlStr = input instanceof URL ? input.toString() : String(input);
    calls.push({ url: new URL(urlStr), init: init ?? {} });
    return new Response(status === 204 ? null : JSON.stringify(body), {
      status,
      headers:
        status === 204 ? {} : { 'Content-Type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fetchStub);
  const client = new ACNClient({ baseUrl: 'http://acn.test', apiKey: 'k' });
  return { client, calls };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// CommunicationProfile.unread_manifest_count
// ---------------------------------------------------------------------------

describe('getCommunicationProfile', () => {
  it('surfaces unread_manifest_count from the server payload', async () => {
    const { client, calls } = setupFetchStub(200, {
      agent_id: 'agent-1',
      mode: 'manifest',
      attention_fee_required: false,
      unread_manifest_count: 7,
    });

    const profile = await client.getCommunicationProfile('agent-1');

    expect(profile.agent_id).toBe('agent-1');
    expect(profile.mode).toBe('manifest');
    expect(profile.attention_fee_required).toBe(false);
    expect(profile.unread_manifest_count).toBe(7);
    expect(calls).toHaveLength(1);
    expect(calls[0].init.method).toBe('GET');
    expect(calls[0].url.pathname).toBe(
      '/api/v1/agents/agent-1/communication_profile',
    );
  });

  it('returns unread_manifest_count of zero verbatim (open-mode steady state)', async () => {
    const { client } = setupFetchStub(200, {
      agent_id: 'agent-2',
      mode: 'open',
      attention_fee_required: false,
      unread_manifest_count: 0,
    });

    const profile = await client.getCommunicationProfile('agent-2');

    expect(profile.unread_manifest_count).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// updatePolicy — conditional `warning` field
// ---------------------------------------------------------------------------

describe('updatePolicy', () => {
  it('passes through `warning` when post-update mode requires polling', async () => {
    const warning =
      'Messages from non-trusted senders will be diverted to the manifest ' +
      'queue. Your agent must periodically poll GET /communication/manifest/{id} ' +
      'to receive them. Without active polling, these messages are unreachable ' +
      'and will expire after the configured TTL (default 7 days).';
    const { client, calls } = setupFetchStub(200, {
      agent_id: 'agent-1',
      communication_policy: { mode: 'manifest' },
      warning,
    });

    const result = await client.updatePolicy('agent-1', 'manifest');

    expect(result.warning).toBe(warning);
    expect(result.communication_policy.mode).toBe('manifest');
    expect(calls).toHaveLength(1);
    expect(calls[0].init.method).toBe('PATCH');
    expect(calls[0].url.pathname).toBe('/api/v1/agents/agent-1/policy');
  });

  it('omits `warning` when post-update mode is `open`', async () => {
    const { client } = setupFetchStub(200, {
      agent_id: 'agent-2',
      communication_policy: { mode: 'open' },
    });

    const result = await client.updatePolicy('agent-2', 'open');

    expect(result.warning).toBeUndefined();
    expect(result.communication_policy.mode).toBe('open');
  });

  it('passes through `warning` for allowlist mode (the second gated mode)', async () => {
    // Server PR #87 emits the warning for any mode that diverts to
    // the manifest queue. `allowlist` is the second such mode —
    // pinning it here prevents a regression that hardcodes the
    // condition to `manifest` only.
    const { client } = setupFetchStub(200, {
      agent_id: 'agent-3',
      communication_policy: { mode: 'allowlist' },
      warning: 'manifest queue polling required',
    });

    const result = await client.updatePolicy('agent-3', 'allowlist');

    expect(result.warning).toBe('manifest queue polling required');
  });
});
