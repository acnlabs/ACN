import { Command } from 'commander';
import { acnGet, acnPost, acnDelete } from '../api.js';
import { loadConfig } from '../config.js';
import { output, handleError } from '../output.js';

interface SessionEntry {
  session_id: string;
  inviter_id: string;
  invitee_id: string;
  status: 'pending' | 'accepted' | 'rejected' | 'closed';
  created_at: number;
  expires_at: number;
  metadata?: Record<string, unknown>;
}

interface PendingResponse {
  agent_id: string;
  count: number;
  sessions: SessionEntry[];
}

function requireAgentId(): string {
  const config = loadConfig();
  if (!config.api_key) {
    console.error('No API key found. Run `acn join` first or `acn config set api-key <key>`.');
    process.exit(1);
  }
  if (!config.agent_id) {
    console.error('No agent ID found. Run `acn join` first or `acn config set agent-id <id>`.');
    process.exit(1);
  }
  return config.agent_id!;
}

function parseMetadata(raw: string | undefined): Record<string, unknown> | undefined {
  if (!raw) return undefined;
  try {
    const parsed = JSON.parse(raw);
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      console.error('--metadata must be a JSON object.');
      process.exit(1);
    }
    return parsed as Record<string, unknown>;
  } catch {
    console.error('--metadata must be valid JSON.');
    process.exit(1);
  }
}

function formatEntry(s: SessionEntry, index?: number): string {
  const prefix = index !== undefined ? `[${index + 1}] ` : '';
  const created = new Date(s.created_at).toISOString();
  const expires = new Date(s.expires_at).toISOString();
  const lines = [
    `${prefix}${s.session_id}`,
    `  Status   : ${s.status}`,
    `  Inviter  : ${s.inviter_id}`,
    `  Invitee  : ${s.invitee_id}`,
    `  Created  : ${created}`,
    `  Expires  : ${expires}`,
  ];
  if (s.metadata && Object.keys(s.metadata).length > 0) {
    lines.push(`  Metadata : ${JSON.stringify(s.metadata)}`);
  }
  return lines.join('\n');
}

export function sessionCommand(): Command {
  const cmd = new Command('session').description(
    'Real-time session layer: bidirectional channel between two agents'
  );

  cmd
    .command('invite <target_agent_id>')
    .description('Invite an agent to a real-time session')
    .option('--ttl-seconds <s>', 'Session TTL in seconds (60–1800, default 300)', parseInt)
    .option('--metadata <json>', 'Optional JSON object attached to the invitation (max 4KB)')
    .action(
      async (
        targetId: string,
        opts: { ttlSeconds?: number; metadata?: string }
      ) => {
        requireAgentId();
        try {
          const body: Record<string, unknown> = {};
          if (opts.ttlSeconds !== undefined) body.ttl_seconds = opts.ttlSeconds;
          const metadata = parseMetadata(opts.metadata);
          if (metadata) body.metadata = metadata;
          const res = await acnPost<SessionEntry>(
            `/sessions/invite/${targetId}`,
            body
          );
          output(
            res,
            `Session invite sent to ${targetId}\n${formatEntry(res)}`
          );
        } catch (err) {
          handleError(err);
        }
      }
    );

  cmd
    .command('accept <session_id>')
    .description('Accept a pending session invitation (invitee only)')
    .action(async (sessionId: string) => {
      requireAgentId();
      try {
        const res = await acnPost<SessionEntry>(`/sessions/${sessionId}/accept`);
        output(res, `Session accepted.\n${formatEntry(res)}`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('reject <session_id>')
    .description('Reject a pending session invitation (invitee only)')
    .action(async (sessionId: string) => {
      requireAgentId();
      try {
        const res = await acnPost<SessionEntry>(`/sessions/${sessionId}/reject`);
        output(res, `Session rejected.\n${formatEntry(res)}`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('close <session_id>')
    .description('Close an active session (either party may close)')
    .action(async (sessionId: string) => {
      requireAgentId();
      try {
        const res = await acnDelete<SessionEntry>(`/sessions/${sessionId}`);
        output(res, `Session closed.\n${formatEntry(res)}`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('pending')
    .description('List pending session invitations addressed to you')
    .action(async () => {
      requireAgentId();
      try {
        const res = await acnGet<PendingResponse>('/sessions/pending');
        const sessions = res.sessions ?? [];
        if (sessions.length === 0) {
          output(res, 'No pending session invitations.');
          return;
        }
        output(
          res,
          `${sessions.length} pending invitation(s):\n\n` +
            sessions.map((s, i) => formatEntry(s, i)).join('\n\n')
        );
      } catch (err) {
        handleError(err);
      }
    });

  return cmd;
}
