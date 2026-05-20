/**
 * CLI tests for `acn rotate-key` (H1 — pre-launch security audit).
 *
 * Exercise:
 *   - Wire shape: POST /agents/{id}/rotate-key with the configured agent_id
 *     when --agent-id is not supplied.
 *   - --agent-id override beats the config value.
 *   - --save updates ~/.acn/config.json with the freshly-minted key.
 *   - Default (no --save) does NOT touch config — defensive against
 *     accidental writes when a script runs the command twice.
 *   - Missing api_key in config short-circuits with a recovery hint rather
 *     than producing a 401 from the server.
 *   - Missing agent_id (no flag, no config) exits 1.
 */

import { Command } from 'commander';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { acnPost } from '../src/api.js';
import { loadConfig, saveConfig } from '../src/config.js';
import { output } from '../src/output.js';
import { rotateKeyCommand } from '../src/commands/rotate-key.js';

vi.mock('../src/api.js', () => ({
  acnPost: vi.fn(),
}));

vi.mock('../src/config.js', () => ({
  loadConfig: vi.fn(),
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

async function runRotateKey(args: string[]): Promise<void> {
  const root = new Command();
  root.addCommand(rotateKeyCommand());
  await root.parseAsync(['node', 'acn', 'rotate-key', ...args]);
}

const successPayload = {
  success: true,
  agent_id: 'agent-1',
  api_key: 'acn_NEW_KEY_xyz',
  message: 'API key rotated.',
};

describe('acn rotate-key', () => {
  beforeEach(() => {
    vi.mocked(loadConfig).mockReturnValue({
      api_key: 'acn_OLD_KEY',
      agent_id: 'agent-1',
      base_url: 'https://api.test',
    });
    vi.mocked(acnPost).mockResolvedValue(successPayload as never);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('POSTs /agents/{id}/rotate-key using config agent_id by default', async () => {
    await runRotateKey([]);

    expect(acnPost).toHaveBeenCalledTimes(1);
    expect(acnPost).toHaveBeenCalledWith('/agents/agent-1/rotate-key');
  });

  it('--agent-id overrides the configured agent_id', async () => {
    await runRotateKey(['--agent-id', 'agent-override']);

    expect(acnPost).toHaveBeenCalledWith('/agents/agent-override/rotate-key');
  });

  it('does NOT write to ~/.acn/config.json by default', async () => {
    await runRotateKey([]);

    expect(saveConfig).not.toHaveBeenCalled();
  });

  it('--save persists the new api_key to config', async () => {
    await runRotateKey(['--save']);

    expect(saveConfig).toHaveBeenCalledTimes(1);
    expect(saveConfig).toHaveBeenCalledWith({ api_key: 'acn_NEW_KEY_xyz' });
  });

  it('prints the new key and a follow-up hint when not --save', async () => {
    await runRotateKey([]);

    // The new plaintext key is printed exactly once and on its own line so
    // copy-paste from a terminal does not pull surrounding text.
    expect(output).toHaveBeenCalledWith(
      successPayload,
      expect.stringContaining('New API key: acn_NEW_KEY_xyz')
    );
    expect(output).toHaveBeenCalledWith(
      successPayload,
      expect.stringContaining('acn config set api_key acn_NEW_KEY_xyz')
    );
  });

  it('omits the manual config-set hint when --save is used', async () => {
    await runRotateKey(['--save']);

    expect(output).toHaveBeenCalledWith(
      successPayload,
      expect.not.stringContaining('acn config set api_key')
    );
    expect(output).toHaveBeenCalledWith(
      successPayload,
      expect.stringContaining('config.json updated')
    );
  });

  it('exits 1 with recovery hint when the local config has no api_key', async () => {
    vi.mocked(loadConfig).mockReturnValue({
      agent_id: 'agent-1',
      base_url: 'https://api.test',
    });
    const exitSpy = vi
      .spyOn(process, 'exit')
      .mockImplementation(((code?: number) => {
        throw new Error(`EXIT:${code}`);
      }) as never);
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    await expect(runRotateKey([])).rejects.toThrow('EXIT:1');

    expect(acnPost).not.toHaveBeenCalled();
    expect(errSpy).toHaveBeenCalledWith(
      expect.stringContaining('No API key found'),
    );

    exitSpy.mockRestore();
    errSpy.mockRestore();
  });

  it('exits 1 when neither --agent-id nor configured agent_id is present', async () => {
    vi.mocked(loadConfig).mockReturnValue({
      api_key: 'acn_OLD_KEY',
      base_url: 'https://api.test',
    });
    const exitSpy = vi
      .spyOn(process, 'exit')
      .mockImplementation(((code?: number) => {
        throw new Error(`EXIT:${code}`);
      }) as never);
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    await expect(runRotateKey([])).rejects.toThrow('EXIT:1');

    expect(acnPost).not.toHaveBeenCalled();
    expect(errSpy).toHaveBeenCalledWith(
      expect.stringContaining('No agent ID found'),
    );

    exitSpy.mockRestore();
    errSpy.mockRestore();
  });
});
