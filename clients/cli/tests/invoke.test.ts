/**
 * CLI tests for `acn invoke` (AgentRouter agent door — D32–D34).
 */

import { Command } from 'commander';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { acnPost } from '../src/api.js';
import { loadConfig } from '../src/config.js';
import { output } from '../src/output.js';
import { invokeCommand } from '../src/commands/invoke.js';

vi.mock('../src/api.js', () => ({
  acnPost: vi.fn(),
}));

vi.mock('../src/config.js', () => ({
  loadConfig: vi.fn(),
}));

vi.mock('../src/output.js', () => ({
  output: vi.fn(),
  isJsonMode: vi.fn(() => false),
  handleError: vi.fn((err: unknown) => {
    const msg = err instanceof Error ? err.message : String(err);
    throw new Error(`handleError:${msg}`);
  }),
}));

async function runInvoke(args: string[]): Promise<void> {
  const root = new Command();
  root.exitOverride();
  root.addCommand(invokeCommand());
  await root.parseAsync(['node', 'acn', 'invoke', ...args]);
}

const successPayload = {
  request_id: 'req-1',
  hop_id: 'hop:invoke:req-1:agent-b',
  to: 'agent-b',
  from: 'agent-a',
  status: 'accepted',
};

describe('acn invoke', () => {
  beforeEach(() => {
    vi.mocked(loadConfig).mockReturnValue({
      api_key: 'acn_TEST_KEY',
      agent_id: 'agent-a',
      base_url: 'https://api.test',
    });
    vi.mocked(acnPost).mockResolvedValue(successPayload as never);
  });

  afterEach(() => {
    vi.clearAllMocks();
    vi.restoreAllMocks();
  });

  it('POSTs /invoke with --to and --text', async () => {
    await runInvoke(['--to', 'agent-b', '--text', 'hello']);

    expect(acnPost).toHaveBeenCalledTimes(1);
    expect(acnPost).toHaveBeenCalledWith('/invoke', {
      to: 'agent-b',
      message: { text: 'hello' },
    });
    expect(output).toHaveBeenCalledWith(
      successPayload,
      expect.stringContaining('hop=hop:invoke:req-1:agent-b')
    );
  });

  it('forwards --slot and --request-id', async () => {
    await runInvoke([
      '--to',
      'agent-b',
      '--slot',
      'text.reply',
      '--text',
      'hi',
      '--request-id',
      'req-cli',
    ]);

    expect(acnPost).toHaveBeenCalledWith('/invoke', {
      to: 'agent-b',
      slot: 'text.reply',
      message: { text: 'hi' },
      request_id: 'req-cli',
    });
  });

  it('allows --slot without --to', async () => {
    await runInvoke(['--slot', 'text.reply', '--text', 'pick one']);

    expect(acnPost).toHaveBeenCalledWith('/invoke', {
      slot: 'text.reply',
      message: { text: 'pick one' },
    });
  });

  it('parses --message JSON and does not call Host', async () => {
    await runInvoke(['--to', 'agent-b', '--message', '{"text":"raw"}']);

    expect(acnPost).toHaveBeenCalledWith('/invoke', {
      to: 'agent-b',
      message: { text: 'raw' },
    });
    const path = vi.mocked(acnPost).mock.calls[0]?.[0];
    expect(path).toBe('/invoke');
    expect(String(path)).not.toContain('agent-router');
  });

  it('exits 1 when neither --to nor --slot is set', async () => {
    const exitSpy = vi.spyOn(process, 'exit').mockImplementation(((code?: number) => {
      throw new Error(`exit:${code}`);
    }) as never);

    await expect(runInvoke(['--text', 'hello'])).rejects.toThrow(/exit:1/);
    expect(acnPost).not.toHaveBeenCalled();
    exitSpy.mockRestore();
  });

  it('exits 1 when there is no message body', async () => {
    const exitSpy = vi.spyOn(process, 'exit').mockImplementation(((code?: number) => {
      throw new Error(`exit:${code}`);
    }) as never);

    await expect(runInvoke(['--to', 'agent-b'])).rejects.toThrow(/exit:1/);
    expect(acnPost).not.toHaveBeenCalled();
    exitSpy.mockRestore();
  });

  it('exits 1 when config has no api_key', async () => {
    vi.mocked(loadConfig).mockReturnValue({
      agent_id: 'agent-a',
      base_url: 'https://api.test',
    });
    const exitSpy = vi.spyOn(process, 'exit').mockImplementation(((code?: number) => {
      throw new Error(`exit:${code}`);
    }) as never);

    await expect(runInvoke(['--to', 'agent-b', '--text', 'hi'])).rejects.toThrow(/exit:1/);
    expect(acnPost).not.toHaveBeenCalled();
    exitSpy.mockRestore();
  });
});
