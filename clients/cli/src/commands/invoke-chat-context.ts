/**
 * Hunter-chat invoke context: bind chat_id on /invoke and merge callees on writeback.
 * Agents must not rely on the LLM to paste orchestration into complete JSON.
 */

import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

export const ACN_CHAT_ID_ENV = 'ACN_CHAT_ID';

export type PendingCallee = {
  agent_id: string;
  hop_id: string;
  status: 'accepted' | 'sent' | 'completed' | 'failed';
  name?: string;
};

const PENDING_CALLEES_DIR_ENV = 'ACN_PENDING_CALLEES_DIR';

function pendingCalleesRoot(env: NodeJS.ProcessEnv = process.env): string {
  const override = (env[PENDING_CALLEES_DIR_ENV] || '').trim();
  if (override) return override;
  return path.join(os.homedir(), '.acn', 'pending-callees');
}

function storePath(chatId: string, env: NodeJS.ProcessEnv = process.env): string {
  const safe = chatId.replace(/[^a-zA-Z0-9._-]+/g, '_').slice(0, 120);
  return path.join(pendingCalleesRoot(env), `${safe}.json`);
}

function readStore(chatId: string): PendingCallee[] {
  try {
    const raw = fs.readFileSync(storePath(chatId), 'utf-8');
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    const out: PendingCallee[] = [];
    for (const row of parsed) {
      if (!row || typeof row !== 'object') continue;
      const r = row as Record<string, unknown>;
      const agent_id = typeof r.agent_id === 'string' ? r.agent_id.trim() : '';
      const hop_id = typeof r.hop_id === 'string' ? r.hop_id.trim() : '';
      const status = typeof r.status === 'string' ? r.status.trim().toLowerCase() : '';
      if (!agent_id || !hop_id.startsWith('hop:invoke:')) continue;
      if (status !== 'accepted' && status !== 'sent' && status !== 'completed' && status !== 'failed') {
        continue;
      }
      const name = typeof r.name === 'string' ? r.name.trim().slice(0, 200) : undefined;
      out.push({
        agent_id,
        hop_id,
        status: status as PendingCallee['status'],
        ...(name ? { name } : {}),
      });
    }
    return out.slice(-8);
  } catch {
    return [];
  }
}

function writeStore(chatId: string, rows: PendingCallee[]): void {
  const file = storePath(chatId);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  if (!rows.length) {
    try {
      fs.unlinkSync(file);
    } catch {
      /* absent ok */
    }
    return;
  }
  fs.writeFileSync(file, JSON.stringify(rows.slice(-8), null, 0), 'utf-8');
}

export function chatIdFromEnv(env: NodeJS.ProcessEnv = process.env): string | null {
  const raw = (env[ACN_CHAT_ID_ENV] || '').trim();
  return raw || null;
}

/** Ensure ACN invoke body carries metadata.agentplanet.chat_id for hunter admit. */
export function injectChatIdIntoInvokeMessage(
  message: Record<string, unknown>,
  chatId: string
): void {
  const id = chatId.trim();
  if (!id) return;
  const metaRaw = message.metadata;
  const meta =
    metaRaw && typeof metaRaw === 'object' && !Array.isArray(metaRaw)
      ? { ...(metaRaw as Record<string, unknown>) }
      : {};
  const apRaw = meta.agentplanet;
  const ap =
    apRaw && typeof apRaw === 'object' && !Array.isArray(apRaw)
      ? { ...(apRaw as Record<string, unknown>) }
      : {};
  ap.chat_id = id;
  meta.agentplanet = ap;
  message.metadata = meta;
}

function mapInvokeStatus(status: string | undefined): PendingCallee['status'] {
  const s = (status || '').trim().toLowerCase();
  if (s === 'failed' || s === 'rejected') return 'failed';
  if (s === 'completed' || s === 'delivered' || s === 'writeback') return 'completed';
  if (s === 'sent') return 'sent';
  return 'accepted';
}

export function recordPendingCallee(
  chatId: string,
  row: { to: string; hopId: string; status?: string; name?: string }
): void {
  const chat = chatId.trim();
  const agent_id = row.to.trim();
  const hop_id = row.hopId.trim();
  if (!chat || !agent_id || !hop_id.startsWith('hop:invoke:')) return;
  const next: PendingCallee = {
    agent_id,
    hop_id,
    status: mapInvokeStatus(row.status),
    ...(row.name?.trim() ? { name: row.name.trim().slice(0, 200) } : {}),
  };
  const prev = readStore(chat).filter((c) => c.hop_id.toLowerCase() !== hop_id.toLowerCase());
  prev.push(next);
  writeStore(chat, prev);
}

export function takePendingCalleesForWriteback(chatId: string): PendingCallee[] {
  const chat = chatId.trim();
  if (!chat) return [];
  const rows = readStore(chat);
  writeStore(chat, []);
  return rows;
}

export function mergeOrchestrationWithPending(
  chatId: string,
  orchestration: Record<string, unknown> | undefined
): Record<string, unknown> | undefined {
  const pending = takePendingCalleesForWriteback(chatId);
  if (!pending.length) return orchestration;
  const base =
    orchestration && typeof orchestration === 'object' ? { ...orchestration } : {};
  const existing = Array.isArray(base.callees) ? [...base.callees] : [];
  const seen = new Set(
    existing
      .filter((r) => r && typeof r === 'object')
      .map((r) => {
        const hop = (r as Record<string, unknown>).hop_id;
        return typeof hop === 'string' ? hop.toLowerCase() : '';
      })
      .filter(Boolean)
  );
  for (const row of pending) {
    const key = row.hop_id.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    existing.push({
      agent_id: row.agent_id,
      hop_id: row.hop_id,
      status: row.status,
      ...(row.name ? { name: row.name } : {}),
    });
  }
  if (!existing.length) return orchestration;
  base.callees = existing;
  return base;
}
