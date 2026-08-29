import { Command } from 'commander';
import { acnGet, acnPost } from '../api.js';
import { handleError, output } from '../output.js';

interface WorkspaceInfo {
  workspace_id: string;
  owner_agent_id: string;
  display_name: string;
  execution_env?: { kind?: string; uri?: string; hint?: string };
  admit?: string;
  org_id?: string | null;
  task_id?: string | null;
  allowlist?: string[];
  status?: string;
}

function formatWorkspace(w: WorkspaceInfo): string {
  const env = w.execution_env;
  const lines = [
    `  ID       : ${w.workspace_id}`,
    `  Name     : ${w.display_name}`,
    `  Owner    : ${w.owner_agent_id}`,
    `  Admit    : ${w.admit ?? '—'}`,
    `  Status   : ${w.status ?? '—'}`,
  ];
  if (w.org_id) lines.push(`  Org      : ${w.org_id}`);
  if (w.task_id) lines.push(`  Task     : ${w.task_id}`);
  if (env?.kind) {
    lines.push(`  Exec env : ${env.kind} ${env.uri ?? ''}`.trim());
    if (env.hint) lines.push(`             ${env.hint}`);
  }
  return lines.join('\n');
}

export function workspaceCommand(): Command {
  const cmd = new Command('workspace').description(
    'Register an execution workspace (pointer + admit; ACN does not run a sandbox)',
  );

  cmd
    .command('create')
    .description('Register a workspace (usually an existing git URL)')
    .requiredOption('--name <name>', 'Display name')
    .requiredOption(
      '--execution-env <json>',
      'JSON object: {"kind":"git"|"url","uri":"...","hint"?}',
    )
    .option('--admit <kind>', 'org | task | allowlist', 'allowlist')
    .option('--org <orgId>', 'Required when --admit org')
    .option('--task <taskId>', 'Required when --admit task')
    .option('--allowlist <ids>', 'Comma-separated agent ids (admit=allowlist)')
    .action(
      async (opts: {
        name: string;
        executionEnv: string;
        admit: string;
        org?: string;
        task?: string;
        allowlist?: string;
      }) => {
        try {
          const execution_env = JSON.parse(opts.executionEnv) as unknown;
          const body: Record<string, unknown> = {
            display_name: opts.name,
            execution_env,
            admit: opts.admit,
          };
          if (opts.org) body.org_id = opts.org;
          if (opts.task) body.task_id = opts.task;
          if (opts.allowlist) {
            body.allowlist = opts.allowlist
              .split(',')
              .map((s) => s.trim())
              .filter(Boolean);
          }
          const w = await acnPost<WorkspaceInfo>('/workspaces', body);
          output(w, formatWorkspace(w));
        } catch (err) {
          handleError(err);
        }
      },
    );

  cmd
    .command('show <workspaceId>')
    .description('Show a workspace (404 if you cannot enter)')
    .action(async (workspaceId: string) => {
      try {
        const w = await acnGet<WorkspaceInfo>(`/workspaces/${workspaceId}`);
        output(w, formatWorkspace(w));
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('show-attestation <workspaceId> <attestationId>')
    .description('Show a workspace-owner attestation (same admit as show)')
    .action(async (workspaceId: string, attestationId: string) => {
      try {
        const att = await acnGet<Record<string, unknown>>(
          `/workspaces/${workspaceId}/attestations/${attestationId}`,
        );
        output(att, `  Attestation : ${attestationId}\n  Workspace   : ${workspaceId}`);
      } catch (err) {
        handleError(err);
      }
    });

  cmd
    .command('close <workspaceId>')
    .description('Close a workspace (owner only; GET still works for the owner)')
    .action(async (workspaceId: string) => {
      try {
        const w = await acnPost<WorkspaceInfo>(`/workspaces/${workspaceId}/close`, {});
        output(w, formatWorkspace(w));
      } catch (err) {
        handleError(err);
      }
    });

  return cmd;
}
