import { writeFileSync } from 'fs';
import { Command } from 'commander';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { acnGet, acnGetBytes, acnPost } from '../src/api.js';
import { loadConfig } from '../src/config.js';
import { output } from '../src/output.js';
import { blobCommand, parseBlobRef, safeDownloadName } from '../src/commands/blob.js';

vi.mock('../src/api.js', () => ({
  acnGet: vi.fn(),
  acnPost: vi.fn(),
  acnGetBytes: vi.fn(),
}));

vi.mock('../src/config.js', () => ({
  loadConfig: vi.fn(),
}));

vi.mock('fs', () => ({
  writeFileSync: vi.fn(),
}));

vi.mock('../src/output.js', () => ({
  output: vi.fn(),
  isJsonMode: vi.fn(() => false),
  handleError: vi.fn((err: unknown) => {
    const msg = err instanceof Error ? err.message : String(err);
    throw new Error(`handleError:${msg}`);
  }),
}));

async function runBlob(args: string[]): Promise<void> {
  const root = new Command();
  root.exitOverride();
  root.addCommand(blobCommand());
  await root.parseAsync(['node', 'acn', 'blob', ...args]);
}

describe('parseBlobRef', () => {
  it('accepts a raw uuid', () => {
    expect(parseBlobRef('11111111-1111-1111-1111-111111111111')).toEqual({
      blobId: '11111111-1111-1111-1111-111111111111',
    });
  });

  it('extracts id and sig from a URI', () => {
    expect(
      parseBlobRef(
        'https://api.test/api/v1/blobs/11111111-1111-1111-1111-111111111111?sig=ab',
      ),
    ).toEqual({
      blobId: '11111111-1111-1111-1111-111111111111',
      sig: 'ab',
    });
  });
});

describe('blobCommand', () => {
  beforeEach(() => {
    vi.mocked(loadConfig).mockReturnValue({
      api_key: 'acn_test',
      agent_id: 'agent-a',
      base_url: 'http://localhost:8000',
    } as ReturnType<typeof loadConfig>);
    vi.mocked(acnGet).mockReset();
    vi.mocked(acnPost).mockReset();
    vi.mocked(acnGetBytes).mockReset();
    vi.mocked(writeFileSync).mockReset();
    vi.mocked(output).mockReset();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('GETs usage', async () => {
    vi.mocked(acnGet).mockResolvedValue({
      agent_id: 'agent-a',
      mailbox_bytes: 5,
      mailbox_cap_bytes: 50,
      retained_bytes: 0,
      retained_cap_bytes: 100,
      used_bytes: 5,
      free_ttl_seconds: 3600,
      credits_per_gib_day: 1,
    });
    await runBlob(['usage']);
    expect(acnGet).toHaveBeenCalledWith('/blobs/usage');
    expect(output).toHaveBeenCalled();
  });

  it('POSTs extend from a signed URI', async () => {
    vi.mocked(acnPost).mockResolvedValue({
      id: '11111111-1111-1111-1111-111111111111',
      owner_id: 'agent-a',
      name: 'hi.txt',
      size: 5,
      exp: 1,
      retained: true,
      uri: 'https://api.test/api/v1/blobs/11111111-1111-1111-1111-111111111111?sig=ab',
      charged: 0.1,
    });
    await runBlob([
      'extend',
      'https://api.test/api/v1/blobs/11111111-1111-1111-1111-111111111111?sig=ab',
      '--days',
      '7',
    ]);
    expect(acnPost).toHaveBeenCalledWith(
      '/blobs/11111111-1111-1111-1111-111111111111/extend',
      { extra_days: 7, sig: 'ab' },
    );
  });

  it('GETs blob bytes and writes the file', async () => {
    vi.mocked(acnGetBytes).mockResolvedValue({
      bytes: new Uint8Array([1, 2, 3]),
      contentType: 'text/plain',
      filename: 'hi.txt',
    });
    await runBlob([
      'get',
      'https://api.test/api/v1/blobs/11111111-1111-1111-1111-111111111111?sig=ab',
      '-o',
      '/tmp/out.bin',
    ]);
    expect(acnGetBytes).toHaveBeenCalledWith(
      '/blobs/11111111-1111-1111-1111-111111111111',
      { sig: 'ab' },
    );
    expect(writeFileSync).toHaveBeenCalledWith(
      '/tmp/out.bin',
      expect.any(Uint8Array),
    );
  });

  it('defaults to a basename when Content-Disposition has a path', async () => {
    vi.mocked(acnGetBytes).mockResolvedValue({
      bytes: new Uint8Array([1, 2, 3]),
      contentType: 'text/plain',
      filename: '../../tmp/pwned.txt',
    });
    await runBlob([
      'get',
      'https://api.test/api/v1/blobs/11111111-1111-1111-1111-111111111111?sig=ab',
    ]);
    expect(writeFileSync).toHaveBeenCalledWith(
      'pwned.txt',
      expect.any(Uint8Array),
    );
  });
});

describe('safeDownloadName', () => {
  it('strips path segments', () => {
    expect(safeDownloadName('../../tmp/pwned.txt', 'fallback')).toBe('pwned.txt');
    expect(safeDownloadName('/etc/passwd', 'fallback')).toBe('passwd');
    expect(safeDownloadName('.', 'fallback')).toBe('fallback');
  });
});
