/**
 * CLI regression tests for ADR-0003 subnet nesting commands (issue #55).
 * Mocks HTTP helpers from src/api.ts — exercises Request shapes + exit paths
 * without a live ACN backend.
 */

import { Command } from 'commander';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { acnGet, acnPost } from '../src/api.js';
import { loadConfig } from '../src/config.js';
import { output } from '../src/output.js';
import { subnetCommand } from '../src/commands/subnet.js';

vi.mock('../src/api.js', () => ({
  acnGet: vi.fn(),
  acnPost: vi.fn(),
  acnDelete: vi.fn(),
  acnPatch: vi.fn(),
}));

vi.mock('../src/config.js', () => ({
  loadConfig: vi.fn(() => ({
    api_key: 'sk-test',
    agent_id: 'agent-1',
    base_url: 'https://api.test',
  })),
}));

vi.mock('../src/output.js', () => ({
  output: vi.fn(),
  handleError: vi.fn((err: unknown) => {
    const msg = err instanceof Error ? err.message : String(err);
    throw new Error(`handleError:${msg}`);
  }),
}));

async function runSubnet(args: string[]): Promise<void> {
  const root = new Command();
  root.addCommand(subnetCommand());
  // Default Commander mode: argv[0]=binary, argv[1]=script, rest=user args.
  await root.parseAsync(['node', 'acn', 'subnet', ...args]);
}

describe('subnet create (ADR-0003)', () => {
  const createResponse = {
    status: 'created',
    subnet_id: 'subnet-test-1',
    is_public: true,
    gateway_a2a_url: 'https://gw/a2a/subnet-test-1',
    gateway_ws_url: 'https://gw/ws/subnet-test-1',
  };

  beforeEach(() => {
    vi.mocked(loadConfig).mockReturnValue({
      api_key: 'sk-test',
      agent_id: 'agent-1',
      base_url: 'https://api.test',
    });
    vi.mocked(acnPost).mockResolvedValue(createResponse as never);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('passes parent_subnet_id when --parent is set', async () => {
    await runSubnet(['create', '--name', 'Squad', '--parent', 'net-parent']);

    expect(acnPost).toHaveBeenCalledTimes(1);
    expect(acnPost).toHaveBeenCalledWith(
      '/subnets',
      expect.objectContaining({
        name: 'Squad',
        parent_subnet_id: 'net-parent',
        is_private: false,
        lifecycle: 'persistent',
      })
    );
  });

  it('passes lifecycle + linked_task_id for task_scoped + --task', async () => {
    await runSubnet([
      'create',
      '--name',
      'T',
      '--lifecycle',
      'task_scoped',
      '--task',
      'task-xyz',
    ]);

    expect(acnPost).toHaveBeenCalledWith(
      '/subnets',
      expect.objectContaining({
        name: 'T',
        lifecycle: 'task_scoped',
        linked_task_id: 'task-xyz',
        is_private: false,
      })
    );
  });

  it('exits 2 when task_scoped without --task', async () => {
    const exitSpy = vi
      .spyOn(process, 'exit')
      .mockImplementation(((code?: number) => {
        throw new Error(`EXIT:${code}`);
      }) as never);

    await expect(
      runSubnet(['create', '--name', 'Bad', '--lifecycle', 'task_scoped'])
    ).rejects.toThrow('EXIT:2');

    exitSpy.mockRestore();
    expect(acnPost).not.toHaveBeenCalled();
  });

  it('exits 2 when persistent with --task', async () => {
    const exitSpy = vi
      .spyOn(process, 'exit')
      .mockImplementation(((code?: number) => {
        throw new Error(`EXIT:${code}`);
      }) as never);

    await expect(
      runSubnet([
        'create',
        '--name',
        'Bad',
        '--lifecycle',
        'persistent',
        '--task',
        'task-1',
      ])
    ).rejects.toThrow('EXIT:2');

    exitSpy.mockRestore();
    expect(acnPost).not.toHaveBeenCalled();
  });
});

describe('subnet list --parent', () => {
  beforeEach(() => {
    vi.mocked(loadConfig).mockReturnValue({
      base_url: 'https://api.test',
    });
    vi.mocked(acnGet).mockResolvedValue({
      subnets: [],
      count: 0,
    } as never);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('GET /subnets?parent=<id> with encoded parent id', async () => {
    await runSubnet(['list', '--parent', 'parent:with&chars']);

    expect(acnGet).toHaveBeenCalledWith(
      `/subnets?parent=${encodeURIComponent('parent:with&chars')}`
    );
  });
});

describe('subnet promote', () => {
  beforeEach(() => {
    vi.mocked(loadConfig).mockReturnValue({
      api_key: 'sk-test',
      base_url: 'https://api.test',
    });
    vi.mocked(acnPost).mockResolvedValue({
      subnet_id: 'squad-1',
      name: 'Squad',
      lifecycle: 'persistent',
      linked_task_id: null,
    } as never);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('POST /subnets/<id>/promote and prints lifecycle', async () => {
    await runSubnet(['promote', 'squad-1']);

    expect(acnPost).toHaveBeenCalledWith('/subnets/squad-1/promote');
    expect(output).toHaveBeenCalledWith(
      expect.anything(),
      expect.stringMatching(/Subnet squad-1 lifecycle=persistent/)
    );
  });
});
