import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, describe, expect, it } from 'vitest';

import {
  BYO_DEGRADE_ENTER_STREAK,
  BYO_DEGRADE_EXIT_STREAK,
  loadByoDegradeState,
  looksLikeByoModelId,
  resolveOfficialDefaultModel,
  tickByoDegrade,
} from '../src/commands/byo-degrade.js';

const homes: string[] = [];

function tempEnv(agentId = 'agent-test'): NodeJS.ProcessEnv {
  const root = mkdtempSync(join(tmpdir(), 'acn-byo-degrade-'));
  homes.push(root);
  return { ACN_HOME: root, ACN_OFFICIAL_DEFAULT_MODEL: undefined } as NodeJS.ProcessEnv;
}

afterEach(() => {
  while (homes.length) {
    const root = homes.pop();
    if (root) rmSync(root, { recursive: true, force: true });
  }
});

const byoProviders = [
  {
    id: 'tencenttokenplan',
    models: ['tencenttokenplan/kimi-k2.5', 'tencenttokenplan/glm-5'],
    current: true,
  },
];

describe('resolveOfficialDefaultModel', () => {
  it('prefers ACN_OFFICIAL_DEFAULT_MODEL', () => {
    expect(
      resolveOfficialDefaultModel({
        ACN_OFFICIAL_DEFAULT_MODEL: 'deepseek/deepseek-v4-flash',
      })
    ).toBe('deepseek/deepseek-v4-flash');
  });

  it('falls back to dual-shelf default', () => {
    expect(resolveOfficialDefaultModel({})).toBe('deepseek/deepseek-chat');
  });
});

describe('looksLikeByoModelId', () => {
  it('flags TokenPlan-style ids and keeps Host book ids', () => {
    expect(looksLikeByoModelId('tencenttokenplan/kimi-k2.5')).toBe(true);
    expect(looksLikeByoModelId('deepseek/deepseek-chat')).toBe(false);
    expect(looksLikeByoModelId('openai/gpt-4o-mini')).toBe(false);
  });
});

describe('tickByoDegrade', () => {
  it('enters after ENTER_STREAK quota rounds without wiping healthy snapshot', () => {
    const env = tempEnv();
    const agentId = 'comiclaw';

    // Healthy refresh — snapshot last good providers.
    tickByoDegrade({
      agentId,
      quotaExhausted: false,
      hasByoModels: true,
      providers: byoProviders,
      preferredModel: 'tencenttokenplan/kimi-k2.5',
      env,
    });

    for (let i = 0; i < BYO_DEGRADE_ENTER_STREAK - 1; i++) {
      const mid = tickByoDegrade({
        agentId,
        quotaExhausted: true,
        hasByoModels: false,
        providers: [{ id: 'tencenttokenplan', models: [], current: true }],
        preferredModel: 'tencenttokenplan/kimi-k2.5',
        env,
      });
      expect(mid.enterOrStayDegraded).toBe(false);
      expect(mid.state.degraded).toBe(false);
    }

    const entered = tickByoDegrade({
      agentId,
      quotaExhausted: true,
      hasByoModels: false,
      providers: [{ id: 'tencenttokenplan', models: [], current: true }],
      preferredModel: 'tencenttokenplan/kimi-k2.5',
      env,
    });
    expect(entered.enterOrStayDegraded).toBe(true);
    expect(entered.state.degraded).toBe(true);
    expect(entered.state.snapshot?.providers).toEqual(byoProviders);
    expect(entered.state.snapshot?.preferredModel).toBe('tencenttokenplan/kimi-k2.5');

    const disk = loadByoDegradeState(agentId, env);
    expect(disk.snapshot?.providers?.[0]?.models).toEqual(byoProviders[0].models);
  });

  it('restores snapshot after EXIT_STREAK healthy rounds', () => {
    const env = tempEnv();
    const agentId = 'restore-me';

    tickByoDegrade({
      agentId,
      quotaExhausted: false,
      hasByoModels: true,
      providers: byoProviders,
      preferredModel: 'tencenttokenplan/glm-5',
      env,
    });
    for (let i = 0; i < BYO_DEGRADE_ENTER_STREAK; i++) {
      tickByoDegrade({
        agentId,
        quotaExhausted: true,
        hasByoModels: false,
        providers: [{ id: 'tencenttokenplan', models: [], current: true }],
        env,
      });
    }

    let last = tickByoDegrade({
      agentId,
      quotaExhausted: false,
      hasByoModels: true,
      providers: byoProviders,
      preferredModel: 'tencenttokenplan/glm-5',
      env,
    });
    expect(last.enterOrStayDegraded).toBe(true);
    expect(last.justRestored).toBe(false);

    for (let i = 0; i < BYO_DEGRADE_EXIT_STREAK - 1; i++) {
      last = tickByoDegrade({
        agentId,
        quotaExhausted: false,
        hasByoModels: true,
        providers: byoProviders,
        preferredModel: 'tencenttokenplan/glm-5',
        env,
      });
      expect(last.justRestored).toBe(i === BYO_DEGRADE_EXIT_STREAK - 2);
      if (i < BYO_DEGRADE_EXIT_STREAK - 2) {
        expect(last.enterOrStayDegraded).toBe(true);
      }
    }
    expect(last.justRestored).toBe(true);
    expect(last.enterOrStayDegraded).toBe(false);
    expect(last.state.degraded).toBe(false);
    expect(last.state.snapshot?.providers).toEqual(byoProviders);
  });

  it('does not advance enter on ambiguous empty (no quota flag)', () => {
    const env = tempEnv();
    const agentId = 'ambiguous';
    const a = tickByoDegrade({
      agentId,
      quotaExhausted: false,
      hasByoModels: false,
      providers: [],
      env,
    });
    expect(a.state.enterStreak).toBe(0);
    expect(a.enterOrStayDegraded).toBe(false);
  });
});
