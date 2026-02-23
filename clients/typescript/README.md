# @acn/client

Official TypeScript/JavaScript client for [ACN (Agent Collaboration Network)](https://github.com/acnlabs/ACN).

## Installation

```bash
npm install @acn/client
# or
yarn add @acn/client
# or
pnpm add @acn/client
```

## Quick Start

### HTTP Client

```typescript
import { ACNClient } from '@acn/client';

const client = new ACNClient('http://localhost:9000');

// Search agents
const { agents } = await client.searchAgents({ skills: 'coding' });
console.log('Found agents:', agents);

// Get agent details
const agent = await client.getAgent('agent-123');
console.log('Agent:', agent);

// Get available skills
const { skills } = await client.getSkills();
console.log('Skills:', skills);
```

### Real-time WebSocket

```typescript
import { ACNRealtime } from '@acn/client';

const realtime = new ACNRealtime('ws://localhost:9000');

// Subscribe to agent events
realtime.subscribe('agents', (message) => {
  console.log('Agent event:', message);
});

// Subscribe to all messages
realtime.onMessage((message) => {
  console.log('Any message:', message);
});

// Monitor connection state
realtime.onStateChange((state) => {
  console.log('Connection state:', state);
});

// Connect
await realtime.connect();

// Later: disconnect
realtime.disconnect();
```

### Simple Subscription Helper

```typescript
import { subscribeToACN } from '@acn/client';

const unsubscribe = subscribeToACN('ws://localhost:9000', 'agents', (msg) => {
  console.log('Agent event:', msg);
});

// Later: unsubscribe and disconnect
unsubscribe();
```

## API Reference

### ACNClient

HTTP client for ACN REST API.

#### Constructor

```typescript
new ACNClient(options: ACNClientOptions | string)
```

Options:
- `baseUrl` - ACN server URL
- `timeout` - Request timeout in ms (default: 30000)
- `headers` - Custom headers
- `apiKey` - API key for authentication

#### Agent Methods

| Method | Description |
|--------|-------------|
| `searchAgents(options?)` | Search agents by skills/status |
| `getAgent(agentId)` | Get agent by ID |
| `registerAgent(agent)` | Register a new agent |
| `unregisterAgent(agentId)` | Unregister an agent |
| `heartbeat(agentId)` | Send heartbeat |
| `getSkills()` | List all available skills |

#### Subnet Methods

| Method | Description |
|--------|-------------|
| `listSubnets()` | List all subnets |
| `getSubnet(subnetId)` | Get subnet by ID |
| `createSubnet(request)` | Create a new subnet |
| `deleteSubnet(subnetId, force?)` | Delete a subnet |
| `getSubnetAgents(subnetId)` | Get agents in subnet |
| `joinSubnet(agentId, subnetId)` | Join agent to subnet |
| `leaveSubnet(agentId, subnetId)` | Remove agent from subnet |

#### Communication Methods

| Method | Description |
|--------|-------------|
| `sendMessage(request)` | Send message to agent |
| `broadcast(request)` | Broadcast to multiple agents |
| `broadcastBySkill(request)` | Broadcast by skill |
| `getMessageHistory(agentId, options?)` | Get message history |

#### Payment Methods

| Method | Description |
|--------|-------------|
| `discoverPaymentAgents(options?)` | Find agents accepting payments |
| `getPaymentCapability(agentId)` | Get agent's payment capability |
| `setPaymentCapability(agentId, capability)` | Set payment capability |
| `getPaymentTask(taskId)` | Get payment task |
| `getAgentPaymentTasks(agentId, options?)` | Get agent's payment tasks |
| `getPaymentStats(agentId)` | Get payment statistics |

#### Monitoring Methods

| Method | Description |
|--------|-------------|
| `health()` | Health check |
| `getStats()` | Get server statistics |
| `getDashboard()` | Get dashboard data |
| `getSystemHealth()` | Get system health |
| `getMetrics()` | Get metrics |
| `getAgentAnalytics()` | Get agent analytics |

### ACNRealtime

WebSocket client for real-time events.

#### Constructor

```typescript
new ACNRealtime(baseUrl: string, options?: WSConnectionOptions)
```

Options:
- `autoReconnect` - Auto reconnect on disconnect (default: true)
- `reconnectInterval` - Reconnect interval in ms (default: 3000)
- `maxReconnectAttempts` - Max reconnect attempts (default: 10)
- `heartbeatInterval` - Heartbeat interval in ms (default: 30000)

#### Methods

| Method | Description |
|--------|-------------|
| `connect(channel?)` | Connect to WebSocket |
| `disconnect()` | Disconnect |
| `subscribe(channel, handler)` | Subscribe to channel |
| `onMessage(handler)` | Subscribe to all messages |
| `onStateChange(handler)` | Subscribe to state changes |
| `send(message)` | Send a message |

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `connectionState` | `WSState` | Current state |
| `isConnected` | `boolean` | Whether connected |

## TypeScript Support

This package includes full TypeScript type definitions.

```typescript
import type {
  AgentInfo,
  AgentSearchOptions,
  PaymentCapability,
  WSMessage,
} from '@acn/client';
```

## Browser Support

This package works in both Node.js and browser environments.

For browsers, make sure your bundler handles the `fetch` and `WebSocket` APIs (available natively in modern browsers).

## License

MIT

## Links

- [ACN GitHub](https://github.com/acnlabs/ACN)
- [Documentation](https://github.com/acnlabs/ACN#readme)
- [Issues](https://github.com/acnlabs/ACN/issues)
































