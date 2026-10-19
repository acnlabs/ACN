/**
 * When a person's own provider rejects or times out, listen may try official once.
 * Agent-to-agent invoke never matches this.
 */

import { mkdirSync, readFileSync, writeFileSync } from 'fs';
import { homedir } from 'os';
import { dirname, join } from 'path';

/** Shelf-safe on both the global key and the China key. */
export const OFFICIAL_FALLBACK_MODEL = 'deepseek/deepseek-chat';

export function providerGaveUp(reason: string): boolean {
  const low = (reason || '').toLowerCase();
  if (!low) return false;
  if (low === 'timeout' || low.endsWith(':timeout') || low.includes('timeout')) {
    return true;
  }
  const marks = [
    'provider_rejected',
    'provider_timeout',
    'provider_unreachable',
    '403',
    'disabled',
    'invocation permission',
    'not authorized',
    'unauthorized',
    'api key',
  ];
  return marks.some((mark) => low.includes(mark));
}

export interface OfficialSpentStore {
  has(id: string): boolean;
  remember(id: string): void;
}

export class MemoryOfficialSpent implements OfficialSpentStore {
  private ids = new Set<string>();
  has(id: string): boolean {
    return this.ids.has(id);
  }
  remember(id: string): void {
    this.ids.add(id);
  }
}

/** Letters whose official try already failed. Later replays must not call a model. */
export function fileOfficialSpent(agentId: string): OfficialSpentStore {
  const file = join(homedir(), '.acn', 'official-spent', `${agentId}.json`);
  const read = (): string[] => {
    try {
      const parsed = JSON.parse(readFileSync(file, 'utf-8')) as unknown;
      return Array.isArray(parsed) ? parsed.filter((item) => typeof item === 'string') : [];
    } catch {
      return [];
    }
  };
  return {
    has(id: string) {
      return read().includes(id);
    },
    remember(id: string) {
      const ids = read().filter((item) => item !== id);
      ids.push(id);
      mkdirSync(dirname(file), { recursive: true });
      writeFileSync(file, JSON.stringify(ids.slice(-40)));
    },
  };
}
