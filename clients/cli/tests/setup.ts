import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'acn-pending-callees-'));
process.env.ACN_PENDING_CALLEES_DIR = dir;
