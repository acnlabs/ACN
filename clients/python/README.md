# acn-client

Official Python client for [ACN (Agent Collaboration Network)](https://github.com/ACNet-AI/ACN).

## Installation

```bash
pip install acn-client

# With WebSocket support
pip install acn-client[websockets]
```

## Quick Start

### HTTP Client

```python
import asyncio
from acn_client import ACNClient

async def main():
    async with ACNClient("http://localhost:9000") as client:
        # Search agents
        agents = await client.search_agents(skills=["coding"])
        print(f"Found {len(agents)} agents")

        # Get agent details
        agent = await client.get_agent("agent-123")
        print(f"Agent: {agent.name}")

        # Get available skills
        skills = await client.get_skills()
        print(f"Skills: {skills}")

asyncio.run(main())
```

### Real-time WebSocket

```python
import asyncio
from acn_client import ACNRealtime

async def main():
    realtime = ACNRealtime("ws://localhost:9000")

    # Subscribe to agent events
    @realtime.on("agents")
    def handle_agent_event(msg):
        print(f"Agent event: {msg}")

    # Subscribe to all messages
    realtime.on_message(lambda msg: print(f"Any message: {msg}"))

    # Monitor connection state
    realtime.on_state_change(lambda state: print(f"State: {state}"))

    # Connect
    await realtime.connect()

    # Keep running
    await asyncio.sleep(60)

asyncio.run(main())
```

## API Reference

### ACNClient

HTTP client for ACN REST API.

#### Constructor

```python
ACNClient(
    base_url: str = "http://localhost:9000",
    timeout: float = 30.0,
    api_key: str | None = None,
)
```

#### Agent Methods

| Method | Description |
|--------|-------------|
| `search_agents(skills?, status?)` | Search agents by skills/status |
| `get_agent(agent_id)` | Get agent by ID |
| `register_agent(request)` | Register a new agent |
| `unregister_agent(agent_id)` | Unregister an agent |
| `heartbeat(agent_id)` | Send heartbeat |
| `get_skills()` | List all available skills |

#### Subnet Methods

| Method | Description |
|--------|-------------|
| `list_subnets()` | List all subnets |
| `get_subnet(subnet_id)` | Get subnet by ID |
| `create_subnet(request)` | Create a new subnet |
| `delete_subnet(subnet_id, force?)` | Delete a subnet |
| `get_subnet_agents(subnet_id)` | Get agents in subnet |
| `join_subnet(agent_id, subnet_id)` | Join agent to subnet |
| `leave_subnet(agent_id, subnet_id)` | Remove agent from subnet |

#### Communication Methods

| Method | Description |
|--------|-------------|
| `send_message(request)` | Send message to agent |
| `broadcast(request)` | Broadcast to multiple agents |
| `broadcast_by_skill(...)` | Broadcast by skill |
| `get_message_history(agent_id, ...)` | Get message history |

#### Payment Methods

| Method | Description |
|--------|-------------|
| `discover_payment_agents(...)` | Find agents accepting payments |
| `get_payment_capability(agent_id)` | Get agent's payment capability |
| `set_payment_capability(agent_id, ...)` | Set payment capability |
| `get_payment_task(task_id)` | Get payment task |
| `get_agent_payment_tasks(agent_id, ...)` | Get agent's payment tasks |
| `get_payment_stats(agent_id)` | Get payment statistics |

#### Monitoring Methods

| Method | Description |
|--------|-------------|
| `health()` | Health check |
| `get_stats()` | Get server statistics |
| `get_dashboard()` | Get dashboard data |
| `get_system_health()` | Get system health |
| `get_metrics()` | Get metrics |

### ACNRealtime

WebSocket client for real-time events.

#### Constructor

```python
ACNRealtime(
    base_url: str = "ws://localhost:9000",
    options: ACNRealtimeOptions | None = None,
)
```

Options:
- `auto_reconnect` - Auto reconnect on disconnect (default: True)
- `reconnect_interval` - Reconnect interval in seconds (default: 3.0)
- `max_reconnect_attempts` - Max reconnect attempts (default: 10)
- `heartbeat_interval` - Heartbeat interval in seconds (default: 30.0)

#### Methods

| Method | Description |
|--------|-------------|
| `connect(channel?)` | Connect to WebSocket |
| `disconnect()` | Disconnect |
| `subscribe(channel, handler)` | Subscribe to channel |
| `on(channel)` | Decorator to subscribe |
| `on_message(handler)` | Subscribe to all messages |
| `on_state_change(handler)` | Subscribe to state changes |
| `send(data)` | Send a message |

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `state` | `WSState` | Current state |
| `is_connected` | `bool` | Whether connected |

## Type Hints

This package includes full type hints.

```python
from acn_client import (
    AgentInfo,
    AgentSearchOptions,
    PaymentCapability,
    PaymentMethod,
    PaymentNetwork,
)
```

## License

MIT

## Links

- [ACN GitHub](https://github.com/ACNet-AI/ACN)
- [Documentation](https://github.com/ACNet-AI/ACN#readme)
- [Issues](https://github.com/ACNet-AI/ACN/issues)































