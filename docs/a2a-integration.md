# ACN A2A Integration

ACN (Agent Collaboration Network) exposes its infrastructure services through **A2A Protocol** endpoints, allowing agents to use a unified communication standard.

## Overview

### ACN as an "Infrastructure Agent"

ACN is **NOT** an AI Agent - it doesn't execute AI tasks like code generation or image creation.

ACN **IS** an Infrastructure Service that provides:
- **Registry**: Agent discovery and registration
- **Broadcast**: Multi-agent message broadcasting  
- **Routing**: Point-to-point and skill-based routing
- **Gateway**: Subnet and NAT traversal

By exposing an A2A Server, ACN allows agents to:
- ✅ Use a **single protocol** (A2A) for all communication
- ✅ Access ACN services through **standard A2A messages**
- ✅ Maintain **ecosystem consistency**

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  ACN Service                                              │
│                                                           │
│  ┌────────────────────┐    ┌────────────────────────┐   │
│  │  REST API          │    │  A2A Server            │   │
│  │  /api/v1/*         │    │  /a2a/jsonrpc          │   │
│  │                    │    │                        │   │
│  │  - Agent Register  │    │  Actions:              │   │
│  │  - Agent Search    │    │  - broadcast           │   │
│  │  - Health Check    │    │  - discover            │   │
│  │                    │    │  - route               │   │
│  │  (Basic CRUD)      │    │  - subnet_route        │   │
│  └────────────────────┘    └────────────────────────┘   │
│                                                           │
│  Internal Components:                                     │
│  ├─ MessageRouter (uses a2a.client)                      │
│  ├─ BroadcastService                                     │
│  ├─ SubnetManager                                        │
│  └─ Registry                                             │
└──────────────────────────────────────────────────────────┘
```

---

## Supported Actions

### 1. `broadcast` - Multi-Agent Broadcasting

Send a message to multiple agents simultaneously.

**Request:**
```json
POST /a2a/jsonrpc
{
  "jsonrpc": "2.0",
  "id": "req-123",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "messageId": "msg-456",
      "parts": [{
        "kind": "data",
        "data": {
          "action": "broadcast",
          "target_agents": ["agent-a", "agent-b", "agent-c"],
          "message": "Hello from ACN!",
          "strategy": "parallel"
        }
      }],
      "metadata": {
        "acn_action": "broadcast"
      }
    }
  }
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "id": "req-123",
  "result": {
    "kind": "task",
    "id": "task-789",
    "status": {"state": "completed"},
    "artifacts": [{
      "artifactId": "art-001",
      "name": "broadcast_result",
      "parts": [{
        "kind": "data",
        "data": {
          "status": "completed",
          "results": {
            "agent-a": {...},
            "agent-b": {...},
            "agent-c": {...}
          },
          "target_count": 3
        }
      }]
    }]
  }
}
```

**Broadcast by Skills:**
```json
{
  "data": {
    "action": "broadcast",
    "target_skills": ["frontend", "react"],
    "message": "UI update required",
    "strategy": "best_effort"
  }
}
```

---

### 2. `discover` - Agent Discovery

Find agents by skills and status.

**Request:**
```json
POST /a2a/jsonrpc
{
  "jsonrpc": "2.0",
  "id": "req-124",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "messageId": "msg-457",
      "parts": [{
        "kind": "data",
        "data": {
          "action": "discover",
          "skills": ["frontend", "react"],
          "status": "online"
        }
      }]
    }
  }
}
```

**Response:**
```json
{
  "result": {
    "artifacts": [{
      "name": "discovered_agents",
      "parts": [{
        "kind": "data",
        "data": {
          "agents": [
            {
              "agent_id": "agent-123",
              "name": "React Frontend Agent",
              "endpoint": "https://agent.example.com/a2a/jsonrpc",
              "skills": ["frontend", "react", "typescript"],
              "status": "online"
            }
          ],
          "total": 1
        }
      }]
    }]
  }
}
```

---

### 3. `route` - Point-to-Point Routing

Route a message to a specific agent (with ACN logging & retry).

**Request:**
```json
{
  "data": {
    "action": "route",
    "target_agent": "cursor-agent",
    "message": "Generate login component"
  }
}
```

**Response:**
```json
{
  "artifacts": [{
    "name": "routing_result",
    "parts": [{
      "kind": "data",
      "data": {
        "response": {...},
        "target_agent": "cursor-agent"
      }
    }]
  }]
}
```

---

### 4. `subnet_route` - Subnet Gateway Routing

Route through ACN's subnet gateway (for NAT traversal).

**Request:**
```json
{
  "data": {
    "action": "subnet_route",
    "subnet_id": "enterprise-a",
    "agent_id": "internal-agent-001",
    "message": {...}
  }
}
```

---

## Usage Examples

### Python Client

```python
import httpx
from a2a.client import A2AClient
from a2a.types import Message, DataPart, Role, SendMessageRequest, MessageSendParams

# Create A2A client for ACN
httpx_client = httpx.AsyncClient(timeout=30.0)
acn_client = A2AClient(
    httpx_client=httpx_client,
    url="https://acn.agenticplanet.space/a2a/jsonrpc"
)

# 1. Broadcast to multiple agents
request = SendMessageRequest(
    id="req-001",
    params=MessageSendParams(
        message=Message(
            role=Role.user,
            messageId="msg-001",
            parts=[DataPart(data={
                "action": "broadcast",
                "target_agents": ["agent-a", "agent-b"],
                "message": "Team meeting at 3pm"
            })],
            metadata={"acn_action": "broadcast"}
        )
    )
)
response = await acn_client.send_message(request)

# 2. Discover agents by skill
request = SendMessageRequest(
    id="req-002",
    params=MessageSendParams(
        message=Message(
            role=Role.user,
            messageId="msg-002",
            parts=[DataPart(data={
                "action": "discover",
                "skills": ["frontend", "design"],
                "status": "online"
            })]
        )
    )
)
response = await acn_client.send_message(request)

# 3. Route with ACN infrastructure
request = SendMessageRequest(
    id="req-003",
    params=MessageSendParams(
        message=Message(
            role=Role.user,
            messageId="msg-003",
            parts=[DataPart(data={
                "action": "route",
                "target_agent": "cursor-agent",
                "message": "Refactor authentication"
            })]
        )
    )
)
response = await acn_client.send_message(request)
```

### JavaScript Client

```javascript
import { A2AClient } from '@a2a-js/sdk';

const acnClient = new A2AClient('https://acn.agenticplanet.space/a2a/jsonrpc');

// Broadcast example
const response = await acnClient.sendMessage({
  role: 'user',
  messageId: 'msg-001',
  parts: [{
    kind: 'data',
    data: {
      action: 'broadcast',
      target_skills: ['backend', 'database'],
      message: 'Database migration required'
    }
  }],
  metadata: { acn_action: 'broadcast' }
});
```

---

## Comparison: REST vs A2A

### When to use REST API (`/api/v1/*`)

✅ **Basic CRUD operations**
- Register agents
- Query agent by ID
- Health checks
- Admin operations

### When to use A2A Server (`/a2a/jsonrpc`)

✅ **Agent-to-agent coordination**
- Broadcasting messages
- Dynamic agent discovery
- Routing with logging/retry
- Subnet gateway access

---

## Benefits of A2A Integration

### 1. **Unified Protocol**
Agents only need one client (`a2a.client`) for all communication:
- ✅ Call other agents directly
- ✅ Use ACN infrastructure services
- ❌ No need for separate HTTP clients

### 2. **Standard Message Format**
All messages follow A2A standard:
- `Message` with `parts` (TextPart, DataPart, FilePart)
- `Task` with status tracking
- `Artifacts` for structured results

### 3. **Ecosystem Consistency**
ACN services are accessible the same way as any A2A agent:
```python
# Call an AI agent
await a2a_client.send_message(ai_agent_endpoint, message)

# Call ACN infrastructure
await a2a_client.send_message(acn_endpoint, message)

# Same protocol! 
```

### 4. **Future-Proof**
As A2A protocol evolves (streaming, authentication, etc.), ACN automatically benefits from SDK updates.

---

## Implementation Details

### Using Official A2A SDK

ACN uses the official [`a2a-sdk[http-server]`](https://github.com/a2aproject/a2a-python) Python library:

```python
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AFastAPIApplication
from a2a.server.events import EventQueue
from a2a.types import Message, Task, Artifact, TaskState

class ACNAgentExecutor(AgentExecutor):
    async def execute(
        self, 
        context: RequestContext, 
        event_queue: EventQueue
    ) -> None:
        # Extract action from message parts
        message = context.message
        action = self._extract_action(message, context)
        
        # Route to appropriate ACN service
        if action == "broadcast":
            await self._handle_broadcast(message, context, event_queue)
        elif action == "discover":
            await self._handle_discovery(message, context, event_queue)
        elif action == "route":
            await self._handle_routing(message, context, event_queue)
        elif action == "subnet_route":
            await self._handle_subnet_routing(message, context, event_queue)
    
    async def cancel(
        self, 
        context: RequestContext, 
        event_queue: EventQueue
    ) -> None:
        # ACN infrastructure tasks are atomic and complete immediately
        await self._send_status(
            event_queue, context, TaskState.canceled, 
            "ACN task cancelled (no-op)", final=True
        )
```

### Task Management

ACN uses **Redis-based persistent task storage** for A2A tasks:
- ✅ **Persistent Storage**: Tasks survive service restarts
- ✅ **Efficient Indexing**: Fast lookup by task ID, context, status
- ✅ **Automatic Expiration**: Tasks auto-expire after 30 days
- ✅ **Pagination Support**: Handle large task lists efficiently

```python
from acn.a2a import RedisTaskStore
from a2a.server.request_handlers import DefaultRequestHandler
from redis.asyncio import Redis

# Redis-based task store for persistence
redis = Redis.from_url("redis://localhost:6379")
task_store = RedisTaskStore(redis, key_prefix="a2a:tasks:")

# Request handler uses task_store
request_handler = DefaultRequestHandler(
    agent_executor=acn_executor,
    task_store=task_store,
)

# A2A application
a2a_app = A2AFastAPIApplication(
    agent_card=acn_agent_card,
    http_handler=request_handler,
    title="ACN Infrastructure Agent",
).build(
    rpc_url="/jsonrpc",
    agent_card_url="/.well-known/agent-card.json",
)
```

**RedisTaskStore Features**:
- ✅ **Secondary Indexes**: Efficient lookup by context_id and status
- ✅ **Automatic Expiration**: Tasks expire after 30 days (configurable)
- ✅ **Atomic Operations**: Thread-safe Redis operations
- ✅ **Pagination**: `list_tasks()` supports limit/offset

### Authentication

ACN provides **Auth0-based OAuth 2.0 authentication** for A2A clients:

**Token URL**: `{AUTH0_DOMAIN}/oauth/token`

**Scopes**:
- `acn:read`: Read access to ACN Registry
- `acn:write`: Write access to ACN Registry  
- `acn:admin`: Administrative access

**Example (Client Credentials Flow)**:
```python
import httpx

# 1. Get access token
token_response = httpx.post(
    f"{AUTH0_DOMAIN}/oauth/token",
    json={
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "audience": "https://api.agenticplanet.space",
    }
)
access_token = token_response.json()["access_token"]

# 2. Use token in A2A requests
a2a_response = httpx.post(
    f"{ACN_BASE_URL}/a2a/jsonrpc",
    headers={"Authorization": f"Bearer {access_token}"},
    json={
        "jsonrpc": "2.0",
        "id": "req-1",
        "method": "message/send",
        "params": {...}
    }
)
```

**ACN Agent Card** (with authentication info):
```bash
curl https://acn.agenticplanet.space/.well-known/agent-card.json
```

---

## See Also

- [A2A Protocol Specification](https://a2aproject.github.io/A2A/)
- [A2A Python SDK](https://github.com/a2aproject/a2a-python)
- [ACN Communication Layer](./architecture.md#layer-2-communication)
- [Agent Registration Guide](./guides/agent-registration.md)

