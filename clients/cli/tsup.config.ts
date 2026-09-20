import { defineConfig } from 'tsup';

export default defineConfig({
  entry: ['src/index.ts'],
  format: ['cjs'],
  clean: true,
  // jose@6 is ESM-only; CJS dist cannot `require("jose")`.
  noExternal: ['jose'],
  banner: {
    js: '#!/usr/bin/env node',
  },
});
