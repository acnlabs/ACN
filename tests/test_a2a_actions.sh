#!/bin/bash
# ACN A2A Actions E2E Test

BASE_URL="http://localhost:8002"

echo "======================================"
echo "ACN A2A Actions E2E Test"
echo "======================================"

# Test 1: Discovery
echo -e "\n📍 Test 1: DISCOVER"
curl -s -X POST "$BASE_URL/a2a/jsonrpc" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "test-discover",
    "method": "message/send",
    "params": {
      "message": {
        "role": "user",
        "messageId": "msg-discover-1",
        "parts": [{
          "kind": "data",
          "data": {
            "action": "discover",
            "skills": [],
            "status": "online"
          }
        }]
      }
    }
  }' | python -m json.tool | jq -r '.result.status.state, .result.artifacts[0].parts[0].data.total'

# Test 2: Broadcast (需要已注册的 agents)
echo -e "\n📡 Test 2: BROADCAST"
curl -s -X POST "$BASE_URL/a2a/jsonrpc" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "test-broadcast",
    "method": "message/send",
    "params": {
      "message": {
        "role": "user",
        "messageId": "msg-broadcast-1",
        "parts": [{
          "kind": "data",
          "data": {
            "action": "broadcast",
            "target_agents": ["test-agent-1", "test-agent-2"],
            "message": "Hello from ACN!",
            "strategy": "parallel"
          }
        }],
        "metadata": {
          "from_agent": "test-client"
        }
      }
    }
  }' | python -m json.tool | jq -r '.result.status.state'

# Test 3: Routing (点对点)
echo -e "\n🔀 Test 3: ROUTE (Point-to-Point)"
curl -s -X POST "$BASE_URL/a2a/jsonrpc" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "test-route",
    "method": "message/send",
    "params": {
      "message": {
        "role": "user",
        "messageId": "msg-route-1",
        "parts": [{
          "kind": "data",
          "data": {
            "action": "route",
            "target_agent": "cursor-agent-1",
            "message": "Test message"
          }
        }],
        "metadata": {
          "from_agent": "test-client"
        }
      }
    }
  }' | python -m json.tool | jq -r '.result.status.state, .result.status.message.parts[0].text'

# Test 4: Subnet Routing
echo -e "\n🌐 Test 4: SUBNET_ROUTE"
curl -s -X POST "$BASE_URL/a2a/jsonrpc" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "test-subnet",
    "method": "message/send",
    "params": {
      "message": {
        "role": "user",
        "messageId": "msg-subnet-1",
        "parts": [{
          "kind": "data",
          "data": {
            "action": "subnet_route",
            "subnet_id": "test-subnet-1",
            "agent_id": "subnet-agent-1",
            "message": {"test": "data"}
          }
        }]
      }
    }
  }' | python -m json.tool | jq -r '.result.status.state, .result.status.message.parts[0].text'

echo -e "\n======================================"
echo "✅ All tests completed"
echo "======================================"


