import { Command } from 'commander';
import {
  baseUrlForRegion,
  getConfigPath,
  loadConfig,
  saveConfig,
  type AcnRegion,
} from '../config.js';
import { output } from '../output.js';

const VALID_KEYS = ['api-key', 'agent-id', 'base-url', 'region'] as const;
type ConfigKey = (typeof VALID_KEYS)[number];

const KEY_MAP: Record<Exclude<ConfigKey, 'region'>, 'api_key' | 'agent_id' | 'base_url'> = {
  'api-key': 'api_key',
  'agent-id': 'agent_id',
  'base-url': 'base_url',
};

export function configCommand(): Command {
  const cmd = new Command('config').description('Manage local ACN configuration');

  cmd
    .command('set <key> <value>')
    .description(
      `Set a config value. Keys: ${VALID_KEYS.join(', ')}. ` +
        `region is global|cn (sets base-url). Env ACN_BASE_URL overrides base-url at runtime.`,
    )
    .action((key: string, value: string) => {
      if (!VALID_KEYS.includes(key as ConfigKey)) {
        console.error(`Unknown key "${key}". Valid keys: ${VALID_KEYS.join(', ')}`);
        process.exit(1);
      }
      if (key === 'region') {
        try {
          const base_url = baseUrlForRegion(value);
          const region = value.trim().toLowerCase() as AcnRegion;
          saveConfig({ region, base_url });
          output({ key, value: region, base_url }, `Set region = ${region} (base-url = ${base_url})`);
        } catch (err) {
          console.error(err instanceof Error ? err.message : String(err));
          process.exit(1);
        }
        return;
      }
      saveConfig({ [KEY_MAP[key as Exclude<ConfigKey, 'region'>]]: value });
      output({ key, value }, `Set ${key} = ${value}`);
    });

  cmd
    .command('get <key>')
    .description('Get a single config value')
    .action((key: string) => {
      if (!VALID_KEYS.includes(key as ConfigKey)) {
        console.error(`Unknown key "${key}". Valid keys: ${VALID_KEYS.join(', ')}`);
        process.exit(1);
      }
      const config = loadConfig();
      const val =
        key === 'region'
          ? config.region
          : config[KEY_MAP[key as Exclude<ConfigKey, 'region'>]];
      if (val === undefined) {
        console.error(`Key "${key}" is not set.`);
        process.exit(1);
      }
      output({ key, value: val }, String(val));
    });

  cmd
    .command('show')
    .description('Show all config values')
    .action(() => {
      const config = loadConfig();
      const path = getConfigPath();
      const envOverride = process.env.ACN_BASE_URL?.trim()
        ? ` (ACN_BASE_URL override active)`
        : '';
      output(config, [
        `Config file: ${path}`,
        `  region   : ${config.region ?? '(custom / unknown)'}`,
        `  base-url : ${config.base_url}${envOverride}`,
        `  api-key  : ${config.api_key ? maskKey(config.api_key) : '(not set)'}`,
        `  agent-id : ${config.agent_id ?? '(not set)'}`,
      ].join('\n'));
    });

  return cmd;
}

function maskKey(key: string): string {
  if (key.length <= 8) return '****';
  return key.slice(0, 6) + '...' + key.slice(-4);
}
