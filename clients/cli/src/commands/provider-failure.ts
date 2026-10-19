/**
 * A human chat may try the official model once after the wake script says
 * its own provider rejected, timed out, or could not be reached.
 * The listen deadline is not that signal. Agent-to-agent invoke never matches.
 */

import { mkdirSync, readFileSync, writeFileSync } from 'fs';
import { homedir } from 'os';
import { dirname, join } from 'path';

/** Shelf-safe on both the global key and the China key. */
export const OFFICIAL_FALLBACK_MODEL = 'deepseek/deepseek-chat';

const PROVIDER_GAVE_UP_MARKS = [
  'provider_rejected',
  'provider_timeout',
  'provider_unreachable',
] as const;

const KEEP_IDS = 200;
const KEEP_PENDING = 100;

export function providerGiveUpMark(text: string): string | null {
  const low = (text || '').toLowerCase();
  for (const mark of PROVIDER_GAVE_UP_MARKS) {
    if (low.includes(mark)) return mark;
  }
  return null;
}

/** True only for the three stderr signals. A listen timeout does not match. */
export function providerGaveUp(reason: string): boolean {
  return providerGiveUpMark(reason) !== null;
}

/** Use the provider signal when it appears anywhere in stderr, not only the first 80 characters. */
export function reasonWithProviderSignal(stderr: string, fallback: string): string {
  return providerGiveUpMark(stderr) ?? fallback;
}

interface PendingReply {
  id: string;
  content: string;
}

interface LetterBook {
  failed: string[];
  delivered: string[];
  pending: PendingReply[];
}

export interface OfficialSpentStore {
  failed(id: string): boolean;
  delivered(id: string): boolean;
  pending(id: string): string | null;
  markFailed(id: string): void;
  markPending(id: string, content: string): void;
  markDelivered(id: string): void;
}

function blankBook(): LetterBook {
  return { failed: [], delivered: [], pending: [] };
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'string') : [];
}

function parseBook(raw: unknown): LetterBook {
  if (Array.isArray(raw)) {
    return { failed: strings(raw), delivered: [], pending: [] };
  }
  if (!raw || typeof raw !== 'object') return blankBook();
  const rec = raw as { failed?: unknown; delivered?: unknown; pending?: unknown };
  const pending: PendingReply[] = [];
  if (Array.isArray(rec.pending)) {
    for (const item of rec.pending) {
      if (!item || typeof item !== 'object') continue;
      const row = item as { id?: unknown; content?: unknown };
      if (typeof row.id === 'string' && row.id && typeof row.content === 'string' && row.content) {
        pending.push({ id: row.id, content: row.content });
      }
    }
  }
  return { failed: strings(rec.failed), delivered: strings(rec.delivered), pending };
}

function trimBook(book: LetterBook): LetterBook {
  return {
    failed: book.failed.slice(-KEEP_IDS),
    delivered: book.delivered.slice(-KEEP_IDS),
    pending: book.pending.slice(-KEEP_PENDING),
  };
}

function without(ids: string[], id: string): string[] {
  return ids.filter((item) => item !== id);
}

function applyFailed(book: LetterBook, id: string): LetterBook {
  return trimBook({
    failed: [...without(book.failed, id), id],
    delivered: without(book.delivered, id),
    pending: book.pending.filter((item) => item.id !== id),
  });
}

function applyPending(book: LetterBook, id: string, content: string): LetterBook {
  return trimBook({
    failed: without(book.failed, id),
    delivered: without(book.delivered, id),
    pending: [...book.pending.filter((item) => item.id !== id), { id, content }],
  });
}

function applyDelivered(book: LetterBook, id: string): LetterBook {
  return trimBook({
    failed: without(book.failed, id),
    delivered: [...without(book.delivered, id), id],
    pending: book.pending.filter((item) => item.id !== id),
  });
}

export class MemoryOfficialSpent implements OfficialSpentStore {
  private book = blankBook();

  failed(id: string): boolean {
    return this.book.failed.includes(id);
  }
  delivered(id: string): boolean {
    return this.book.delivered.includes(id);
  }
  pending(id: string): string | null {
    return this.book.pending.find((item) => item.id === id)?.content ?? null;
  }
  markFailed(id: string): void {
    this.book = applyFailed(this.book, id);
  }
  markPending(id: string, content: string): void {
    this.book = applyPending(this.book, id, content);
  }
  markDelivered(id: string): void {
    this.book = applyDelivered(this.book, id);
  }
}

/**
 * Per-agent record of official tries.
 * failed: the door was called and did not answer. Do not call again.
 * pending: the door answered and the chat writeback did not. Post that reply again.
 * delivered: the chat already has the reply. Ack without calling a model.
 * An older file that is only a list of ids is treated as failed.
 */
export function fileOfficialSpent(agentId: string): OfficialSpentStore {
  const file = join(homedir(), '.acn', 'official-spent', `${agentId}.json`);
  const read = (): LetterBook => {
    try {
      return parseBook(JSON.parse(readFileSync(file, 'utf-8')) as unknown);
    } catch {
      return blankBook();
    }
  };
  const write = (book: LetterBook) => {
    mkdirSync(dirname(file), { recursive: true });
    writeFileSync(file, JSON.stringify(trimBook(book)));
  };
  return {
    failed(id: string) {
      return read().failed.includes(id);
    },
    delivered(id: string) {
      return read().delivered.includes(id);
    },
    pending(id: string) {
      return read().pending.find((item) => item.id === id)?.content ?? null;
    },
    markFailed(id: string) {
      write(applyFailed(read(), id));
    },
    markPending(id: string, content: string) {
      write(applyPending(read(), id, content));
    },
    markDelivered(id: string) {
      write(applyDelivered(read(), id));
    },
  };
}
