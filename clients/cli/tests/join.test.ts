/**
 * `acn join --invite` must land on POST /agents/join.
 * Interfaze prompts tell agents to pass ji_…; npm CLI has to actually send it.
 */

import { Command } from 'commander';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { acnPost } from '../src/api.js';
import { inferRegion, resolveBaseUrl, saveConfig } from '../src/config.js';
import { output } from '../src/output.js';
import { joinCommand } from '../src/commands/join.js';

vi.mock('../src/api.js', () => ({
  acnPost: vi.fn(),
}));

vi.mock('../src/config.js', () => ({
  inferRegion: vi.fn(),
  resolveBaseUrl: vi.fn(),
  saveConfig: vi.fn(),
}));

vi.mock('../src/output.js', () => ({
  output: vi.fn(),
  isJsonMode: vi.fn(() => false),
  handleError: vi.fn((err: unknown) => {
    const msg = err instanceof Error ? err.message : String(err);
    throw new Error(`handleError:${msg}`);
  }),
}));

async function runJoin(args: string[]): Promise<void> {
  const root = new Command();
  root.addCommand(joinCommand());
  await root.parseAsync(['node', 'acn', 'join', ...args]);
}

const successPayload = {
  agent_id: 'agent-1',
  api_key: 'acn_TEST_KEY',
  status: 'active',
  claim_status: 'unclaimed',
  claim_url: 'https://interfaze.io/claim/agent-1?token=t',
};

describe('acn join --invite', () => {
  beforeEach(() => {
    vi.mocked(resolveBaseUrl).mockReturnValue('https://api.acnlabs.dev');
    vi.mocked(inferRegion).mockReturnValue('global');
    vi.mocked(acnPost).mockResolvedValue(successPayload as never);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('POSTs invite on /agents/join', async () => {
    await runJoin([
      '--name',
      'WalkAgent',
      '--tags',
      'chat',
      '--invite',
      'ji_abc',
    ]);

    expect(acnPost).toHaveBeenCalledTimes(1);
    const [path, body, opts] = vi.mocked(acnPost).mock.calls[0]!;
    expect(path).toBe('/agents/join');
    expect(body).toMatchObject({
      name: 'WalkAgent',
      tags: ['chat'],
      invite: 'ji_abc',
    });
    expect(opts).toEqual({ baseUrl: 'https://api.acnlabs.dev' });
    expect(saveConfig).toHaveBeenCalled();
    expect(output).toHaveBeenCalled();
  });

  it('trims invite whitespace', async () => {
    await runJoin([
      '--name',
      'WalkAgent',
      '--tags',
      'chat',
      '--invite',
      '  ji_abc  ',
    ]);

    const [, body] = vi.mocked(acnPost).mock.calls[0]!;
    expect(body).toMatchObject({ invite: 'ji_abc' });
  });

  it('omits invite when the flag is absent', async () => {
    await runJoin(['--name', 'WalkAgent', '--tags', 'chat']);

    const [, body] = vi.mocked(acnPost).mock.calls[0]!;
    expect(body).not.toHaveProperty('invite');
  });

  it('omits whitespace-only invite', async () => {
    await runJoin([
      '--name',
      'WalkAgent',
      '--tags',
      'chat',
      '--invite',
      '   ',
    ]);

    const [, body] = vi.mocked(acnPost).mock.calls[0]!;
    expect(body).not.toHaveProperty('invite');
  });
});
