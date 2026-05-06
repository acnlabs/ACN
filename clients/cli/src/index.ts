import { Command } from 'commander';
import { setJsonMode } from './output.js';
import { configCommand } from './commands/config.js';
import { joinCommand } from './commands/join.js';
import { heartbeatCommand } from './commands/heartbeat.js';
import { agentsCommand } from './commands/agents.js';
import { tasksCommand } from './commands/tasks.js';
import { messageCommand } from './commands/message.js';

const program = new Command();

program
  .name('acn')
  .description('ACN CLI — Agent Collaboration Network command-line interface')
  .version('0.1.0')
  .option('--json', 'Output raw JSON (useful for agent parsing)')
  .hook('preAction', (thisCommand) => {
    const opts = thisCommand.opts() as { json?: boolean };
    if (opts.json) setJsonMode(true);
  });

program.addCommand(configCommand());
program.addCommand(joinCommand());
program.addCommand(heartbeatCommand());
program.addCommand(agentsCommand());
program.addCommand(tasksCommand());
program.addCommand(messageCommand());

program.parse(process.argv);
