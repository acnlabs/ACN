/**
 * CLI tests for `acn workspace` (execution doorplate — not a sandbox).
 */

import { Command } from 'commander';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { acnGet, acnPost } from '../src/api.js';
import { output } from '../src/output.js';
import { workspaceCommand } from '../src/commands/workspace.js';

vi.mock('../src/api.js', () => ({
  acnGet: vi.fn(),
  acnPost: vi.fn(),
}));

vi.mock('../src/output.js', () => ({
  output: vi.fn(),
  isJsonMode: vi.fn(() => false),
  handleError: vi.fn((err: unknown) => {
    const msg = err instanceof Error ? err.message : String(err);
    throw new Error(`handleError:${msg}`);
  }),
}));

async function runWorkspace(args: string[]): Promise<void> {
  const root = new Command();
  root.exitOverride();
  root.addCommand(workspaceCommand());
  await root.parseAsync(['node', 'acn', 'workspace', ...args]);
}

describe('acn workspace', () => {
  beforeEach(() => {
    vi.mocked(acnPost).mockReset();
    vi.mocked(acnGet).mockReset();
  });

  afterEach(() => {
    vi.clearAllMocks();
    vi.restoreAllMocks();
  });

  it('create POSTs /workspaces', async () => {
    vi.mocked(acnPost).mockResolvedValue({
      workspace_id: 'ws_1',
      display_name: 'yard',
      owner_agent_id: 'agt_a',
      admit: 'org',
      org_id: 'org_1',
      status: 'active',
    } as never);

    await runWorkspace([
      'create',
      '--name',
      'yard',
      '--admit',
      'org',
      '--org',
      'org_1',
      '--execution-env',
      '{"kind":"git","uri":"https://github.com/acme/squad.git"}',
    ]);

    expect(acnPost).toHaveBeenCalledWith('/workspaces', {
      display_name: 'yard',
      execution_env: { kind: 'git', uri: 'https://github.com/acme/squad.git' },
      admit: 'org',
      org_id: 'org_1',
    });
  });

  it('attest POSTs owner slip without runtime_attested', async () => {
    vi.mocked(acnPost).mockResolvedValue({
      attestation_id: 'att_1',
      kind: 'workspace_owner',
      workspace_id: 'ws_1',
    } as never);

    await runWorkspace([
      'attest',
      'ws_1',
      '--agent',
      'agt_worker',
      '--run-id',
      'run-9',
      '--task',
      'task_1',
      '--artifact',
      '{"git_sha":"deadbeef"}',
    ]);

    expect(acnPost).toHaveBeenCalledWith('/workspaces/ws_1/attestations', {
      agent_id: 'agt_worker',
      run_id: 'run-9',
      task_id: 'task_1',
      artifact: { git_sha: 'deadbeef' },
    });
    expect(output).toHaveBeenCalledWith(
      expect.objectContaining({ attestation_id: 'att_1' }),
      expect.stringContaining('att_1'),
    );
  });

  it('show GETs /workspaces/{id}', async () => {
    vi.mocked(acnGet).mockResolvedValue({
      workspace_id: 'ws_1',
      display_name: 'yard',
      owner_agent_id: 'agt_a',
      status: 'active',
    } as never);

    await runWorkspace(['show', 'ws_1']);
    expect(acnGet).toHaveBeenCalledWith('/workspaces/ws_1');
  });
});
