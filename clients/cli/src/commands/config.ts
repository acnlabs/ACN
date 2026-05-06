import { Command } from 'commander';
import { loadConfig, saveConfig, getConfigPath } from '../config.js';
import { output } from '../output.js';

const VALID_KEYS = ['api-key', 'agent-id', 'base-url'] as const;
type ConfigKey = (typeof VALID_KEYS)[number];

const KEY_MAP: Record<ConfigKey, 'api_key' | 'agent_id' | 'base_url'> = {
  'api-key': 'api_key',
  'agent-id': 'agent_id',
  'base-url': 'base_url',
};

export function configCommand(): Command {
  const cmd = new Command('config').description('Manage local ACN configuration');

  cmd
    .command('set <key> <value>')
    .description(`Set a config value. Keys: ${VALID_KEYS.join(', ')}`)
    .action((key: string, value: string) => {
      if (!VALID_KEYS.includes(key as ConfigKey)) {
        console.error(`Unknown key "${key}". Valid keys: ${VALID_KEYS.join(', ')}`);
        process.exit(1);
      }
      saveConfig({ [KEY_MAP[key as ConfigKey]]: value });
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
      const val = config[KEY_MAP[key as ConfigKey]];
      if (val === undefined) {
        console.error(`Key "${key}" is not set.`);
        process.exit(1);
      }
      output({ key, value: val }, val);
    });

  cmd
    .command('show')
    .description('Show all config values')
    .action(() => {
      const config = loadConfig();
      const path = getConfigPath();
      output(config, [
        `Config file: ${path}`,
        `  base-url : ${config.base_url}`,
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
