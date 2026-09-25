import { mkdtempSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { Command } from 'commander';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { acnPost, acnPostForm } from '../src/api.js';
import { loadConfig } from '../src/config.js';
import { output } from '../src/output.js';
import {
  MAX_INLINE_FILE_BYTES,
  buildSendMessage,
  messageCommand,
} from '../src/commands/message.js';

vi.mock('../src/api.js', () => ({
  acnPost: vi.fn(),
  acnPostForm: vi.fn(),
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

async function runSend(args: string[]): Promise<void> {
  const root = new Command();
  root.exitOverride();
  root.addCommand(messageCommand());
  await root.parseAsync(['node', 'acn', 'message', 'send', ...args]);
}

async function runBroadcast(args: string[]): Promise<void> {
  const root = new Command();
  root.exitOverride();
  root.addCommand(messageCommand());
  await root.parseAsync(['node', 'acn', 'message', 'broadcast', ...args]);
}

describe('buildSendMessage', () => {
  it('keeps the text-only convenience shape', () => {
    const built = buildSendMessage({ text: 'hello' });
    expect(built).toEqual({
      ok: true,
      message: { text: 'hello', type: 'text' },
    });
  });

  it('inlines a small file as FilePart', () => {
    const built = buildSendMessage({
      text: 'diagram',
      filePath: '/tmp/hi.txt',
      readFile: () => Buffer.from('hello'),
    });
    expect(built.ok).toBe(true);
    if (!built.ok) return;
    expect(built.message).toEqual({
      role: 'user',
      parts: [
        { kind: 'text', text: 'diagram' },
        {
          kind: 'file',
          file: {
            bytes: Buffer.from('hello').toString('base64'),
            mimeType: 'text/plain',
            name: 'hi.txt',
          },
        },
      ],
    });
  });

  it('rejects a file over the inline cap', () => {
    const built = buildSendMessage({
      filePath: 'big.bin',
      readFile: () => Buffer.alloc(MAX_INLINE_FILE_BYTES + 1),
    });
    expect(built.ok).toBe(false);
    if (built.ok) return;
    expect(built.error).toContain('--file-uri');
  });

  it('sends a fetchable URI instead of bytes', () => {
    const built = buildSendMessage({
      fileUri: 'https://example.com/a.png',
    });
    expect(built.ok).toBe(true);
    if (!built.ok) return;
    expect(built.message).toEqual({
      role: 'user',
      parts: [
        {
          kind: 'file',
          file: {
            uri: 'https://example.com/a.png',
            name: 'a.png',
            mimeType: 'image/png',
          },
        },
      ],
    });
  });

  it('rejects a non-http URI', () => {
    const built = buildSendMessage({ fileUri: 'file:///tmp/a.png' });
    expect(built.ok).toBe(false);
  });
});

describe('acn message send', () => {
  beforeEach(() => {
    vi.mocked(loadConfig).mockReturnValue({
      api_key: 'acn_TEST_KEY',
      agent_id: 'agent-a',
      base_url: 'https://api.test',
    });
    vi.mocked(acnPost).mockResolvedValue({ message_id: 'm1' } as never);
    vi.mocked(acnPostForm).mockResolvedValue({
      uri: 'https://api.test/api/v1/blobs/11111111-1111-1111-1111-111111111111?exp=1&sig=ab',
    } as never);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('POSTs /communication/send with --text', async () => {
    await runSend(['agent-b', '--text', 'hello']);
    expect(acnPost).toHaveBeenCalledWith('/communication/send', {
      from_agent: 'agent-a',
      target_agent: 'agent-b',
      message: { text: 'hello', type: 'text' },
    });
    expect(output).toHaveBeenCalledWith(
      { message_id: 'm1' },
      'Message delivered to agent-b (id: m1)'
    );
  });

  it('prints a known send status and ignores a foreign one', async () => {
    vi.mocked(acnPost).mockResolvedValueOnce({ status: 'queued', message_id: 'm2' } as never);
    await runSend(['agent-b', '--text', 'hello']);
    expect(output).toHaveBeenCalledWith(
      { status: 'queued', message_id: 'm2' },
      'Message queued to agent-b (id: m2)'
    );

    vi.mocked(acnPost).mockResolvedValueOnce({ status: { state: 'completed' } } as never);
    await runSend(['agent-b', '--text', 'hello']);
    expect(output).toHaveBeenLastCalledWith(
      { status: { state: 'completed' } },
      'Message delivered to agent-b'
    );
  });

  it('POSTs a FilePart for --file-uri', async () => {
    await runSend(['agent-b', '--file-uri', 'https://example.com/a.png']);
    expect(acnPost).toHaveBeenCalledWith('/communication/send', {
      from_agent: 'agent-a',
      target_agent: 'agent-b',
      message: {
        role: 'user',
        parts: [
          {
            kind: 'file',
            file: {
              uri: 'https://example.com/a.png',
              name: 'a.png',
              mimeType: 'image/png',
            },
          },
        ],
      },
    });
  });

  it('uploads --file to /blobs then sends the returned URI', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'acn-file-'));
    const filePath = join(dir, 'sketch.png');
    writeFileSync(filePath, 'png-bytes');
    await runSend(['agent-b', '--text', 'diagram', '--file', filePath]);
    expect(acnPostForm).toHaveBeenCalledTimes(1);
    expect(vi.mocked(acnPostForm).mock.calls[0]?.[0]).toBe('/blobs');
    expect(acnPost).toHaveBeenCalledWith('/communication/send', {
      from_agent: 'agent-a',
      target_agent: 'agent-b',
      message: {
        role: 'user',
        parts: [
          { kind: 'text', text: 'diagram' },
          {
            kind: 'file',
            file: {
              uri: 'https://api.test/api/v1/blobs/11111111-1111-1111-1111-111111111111?exp=1&sig=ab',
              name: 'sketch.png',
              mimeType: 'image/png',
            },
          },
        ],
      },
    });
  });
});

describe('acn message broadcast', () => {
  beforeEach(() => {
    vi.mocked(loadConfig).mockReturnValue({
      api_key: 'acn_TEST_KEY',
      agent_id: 'agent-a',
      base_url: 'https://api.test',
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('summarizes per-target statuses instead of the successful count', async () => {
    vi.mocked(acnPost).mockResolvedValue({
      broadcast_id: 'b1',
      successful: 2,
      responses: [{ status: 'queued' }, { status: 'queued' }, { status: 'delivered' }],
    } as never);
    await runBroadcast(['--text', 'hello']);
    expect(output).toHaveBeenCalledWith(
      expect.objectContaining({ broadcast_id: 'b1' }),
      'Broadcast sent (id: b1). 1 delivered, 2 queued.'
    );
  });
});
