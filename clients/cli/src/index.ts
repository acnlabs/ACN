import { Command } from 'commander';
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { version } = require('../package.json') as { version: string };
import { setJsonMode } from './output.js';
import { configCommand } from './commands/config.js';
import { joinCommand } from './commands/join.js';
import { heartbeatCommand } from './commands/heartbeat.js';
import { rotateKeyCommand } from './commands/rotate-key.js';
import { agentsCommand } from './commands/agents.js';
import { tasksCommand } from './commands/tasks.js';
import { messageCommand } from './commands/message.js';
import { invokeCommand } from './commands/invoke.js';
import { notifyCommand } from './commands/notify.js';
import { inboxCommand } from './commands/inbox.js';
import { listenCommand } from './commands/listen.js';
import { deliveryCommand } from './commands/delivery.js';
import { sessionCommand } from './commands/session.js';
import { subnetCommand } from './commands/subnet.js';
import { orgCommand } from './commands/org.js';
import { followCommand } from './commands/follow.js';
import { walletCommand } from './commands/wallet.js';
import { payCommand } from './commands/pay.js';

const program = new Command();

program
  .name('acn')
  .description('ACN CLI — Agent Collaboration Network command-line interface')
  .version(version)
  .option('--json', 'Output raw JSON (useful for agent parsing)')
  .hook('preAction', (thisCommand) => {
    const opts = thisCommand.opts() as { json?: boolean };
    if (opts.json) setJsonMode(true);
  });

program.addCommand(configCommand());
program.addCommand(joinCommand());
program.addCommand(heartbeatCommand());
program.addCommand(rotateKeyCommand());
program.addCommand(agentsCommand());
program.addCommand(tasksCommand());
program.addCommand(messageCommand());
program.addCommand(invokeCommand());
program.addCommand(notifyCommand());
program.addCommand(inboxCommand());
program.addCommand(listenCommand());
program.addCommand(deliveryCommand());
program.addCommand(sessionCommand());
program.addCommand(subnetCommand());
program.addCommand(orgCommand());
program.addCommand(followCommand());
program.addCommand(walletCommand());
program.addCommand(payCommand());

program.parse(process.argv);
