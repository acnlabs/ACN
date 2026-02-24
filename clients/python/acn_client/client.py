"""
ACN HTTP Client

Official Python client for ACN REST API.
"""

from typing import Any

import httpx

from .models import (
    AgentInfo,
    AgentRegisterRequest,
    BroadcastRequest,
    DashboardData,
    PaymentCapability,
    PaymentStats,
    PaymentTask,
    SendMessageRequest,
    SubnetCreateRequest,
    SubnetInfo,
)


class ACNError(Exception):
    """ACN API Error"""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"ACN Error {status_code}: {message}")


class ACNClient:
    """
    ACN Client - HTTP API

    Example:
        >>> async with ACNClient("http://localhost:9000") as client:
        ...     agents = await client.search_agents(skills=["coding"])
        ...     agent = await client.get_agent("agent-123")
    """

    def __init__(
        self,
        base_url: str = "http://localhost:9000",
        timeout: float = 30.0,
        api_key: str | None = None,
    ):
        """
        Initialize ACN Client

        Args:
            base_url: ACN server URL
            timeout: Request timeout in seconds
            api_key: Optional API key for authentication
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        headers = {}
        if api_key:
            headers["X-API-Key"] = api_key

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers=headers,
            trust_env=False,  # Don't use system proxy settings
        )

    async def close(self) -> None:
        """Close the HTTP client"""
        await self._client.aclose()

    async def __aenter__(self) -> "ACNClient":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make HTTP request"""
        # Filter None values from params
        if params:
            params = {k: v for k, v in params.items() if v is not None}

        response = await self._client.request(
            method=method,
            url=path,
            params=params,
            json=json,
        )

        if not response.is_success:
            try:
                error = response.json()
                message = error.get("detail", error.get("message", response.text))
            except Exception:
                message = response.text
            raise ACNError(response.status_code, message)

        if response.status_code == 204:
            return {}

        result: dict[str, Any] = response.json()
        return result

    # ============================================
    # Health & Status
    # ============================================

    async def health(self) -> dict[str, str]:
        """Check if ACN server is healthy"""
        return await self._request("GET", "/health")

    async def get_stats(self) -> dict[str, int]:
        """Get server statistics"""
        return await self._request("GET", "/api/v1/stats")

    # ============================================
    # Agent Management
    # ============================================

    async def register_agent(self, request: AgentRegisterRequest) -> dict[str, Any]:
        """Register a new agent"""
        return await self._request(
            "POST",
            "/api/v1/agents/register",
            json=request.model_dump(by_alias=True, exclude_none=True),
        )

    async def get_agent(self, agent_id: str) -> AgentInfo:
        """Get agent by ID"""
        data = await self._request("GET", f"/api/v1/agents/{agent_id}")
        return AgentInfo.model_validate(data)

    async def search_agents(
        self,
        skills: list[str] | None = None,
        status: str | None = "online",
        owner: str | None = None,
        name: str | None = None,
    ) -> list[AgentInfo]:
        """Search agents

        Args:
            skills: Filter by agent skills
            status: Filter by status (online, offline, all)
            owner: Filter by owner user ID
            name: Filter by name (partial match)
        """
        params = {
            "skills": ",".join(skills) if skills else None,
            "status": status,
            "owner": owner,
            "name": name,
        }
        # Remove None values
        params = {k: v for k, v in params.items() if v is not None}
        data = await self._request("GET", "/api/v1/agents", params=params)
        return [AgentInfo.model_validate(a) for a in data.get("agents", [])]

    async def unregister_agent(self, agent_id: str) -> dict[str, Any]:
        """Unregister an agent"""
        return await self._request("DELETE", f"/api/v1/agents/{agent_id}")

    async def heartbeat(self, agent_id: str) -> dict[str, Any]:
        """Send agent heartbeat"""
        return await self._request("POST", f"/api/v1/agents/{agent_id}/heartbeat")

    async def get_agent_endpoint(self, agent_id: str) -> str | None:
        """Get agent endpoint"""
        data = await self._request("GET", f"/api/v1/agents/{agent_id}/endpoint")
        return data.get("endpoint")

    async def get_skills(self) -> dict[str, Any]:
        """List all available skills"""
        return await self._request("GET", "/api/v1/skills")

    # ============================================
    # Subnet Management
    # ============================================

    async def create_subnet(self, request: SubnetCreateRequest) -> dict[str, Any]:
        """Create a new subnet"""
        return await self._request(
            "POST",
            "/api/v1/subnets",
            json=request.model_dump(exclude_none=True),
        )

    async def list_subnets(self) -> list[SubnetInfo]:
        """List all subnets"""
        data = await self._request("GET", "/api/v1/subnets")
        return [SubnetInfo.model_validate(s) for s in data.get("subnets", [])]

    async def get_subnet(self, subnet_id: str) -> SubnetInfo:
        """Get subnet by ID"""
        data = await self._request("GET", f"/api/v1/subnets/{subnet_id}")
        return SubnetInfo.model_validate(data)

    async def delete_subnet(self, subnet_id: str, force: bool = False) -> dict[str, Any]:
        """Delete a subnet"""
        return await self._request(
            "DELETE",
            f"/api/v1/subnets/{subnet_id}",
            params={"force": force},
        )

    async def get_subnet_agents(self, subnet_id: str) -> list[AgentInfo]:
        """Get agents in a subnet"""
        data = await self._request("GET", f"/api/v1/subnets/{subnet_id}/agents")
        return [AgentInfo.model_validate(a) for a in data.get("agents", [])]

    async def join_subnet(self, agent_id: str, subnet_id: str) -> dict[str, Any]:
        """Join agent to subnet"""
        return await self._request("POST", f"/api/v1/agents/{agent_id}/subnets/{subnet_id}")

    async def leave_subnet(self, agent_id: str, subnet_id: str) -> dict[str, Any]:
        """Remove agent from subnet"""
        return await self._request("DELETE", f"/api/v1/agents/{agent_id}/subnets/{subnet_id}")

    async def get_agent_subnets(self, agent_id: str) -> list[str]:
        """Get agent's subnets"""
        data = await self._request("GET", f"/api/v1/agents/{agent_id}/subnets")
        subnets: list[str] = data.get("subnets", [])
        return subnets

    # ============================================
    # Communication
    # ============================================

    async def send_message(self, request: SendMessageRequest) -> dict[str, Any]:
        """Send message to an agent"""
        return await self._request(
            "POST",
            "/api/v1/communication/send",
            json=request.model_dump(exclude_none=True),
        )

    async def broadcast(self, request: BroadcastRequest) -> dict[str, Any]:
        """Broadcast message to multiple agents"""
        return await self._request(
            "POST",
            "/api/v1/communication/broadcast",
            json=request.model_dump(exclude_none=True),
        )

    async def broadcast_by_skill(
        self,
        from_agent: str,
        skill: str,
        message_type: str,
        content: Any,
    ) -> dict[str, Any]:
        """Broadcast message to agents with specific skill"""
        return await self._request(
            "POST",
            "/api/v1/communication/broadcast-by-skill",
            json={
                "from_agent": from_agent,
                "skill": skill,
                "message_type": message_type,
                "content": content,
            },
        )

    async def get_message_history(
        self,
        agent_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Get message history for an agent"""
        data = await self._request(
            "GET",
            f"/api/v1/communication/history/{agent_id}",
            params={"limit": limit, "offset": offset},
        )
        messages: list[dict[str, Any]] = data.get("messages", [])
        return messages

    # ============================================
    # Payment Discovery
    # ============================================

    async def set_payment_capability(
        self,
        agent_id: str,
        capability: PaymentCapability,
    ) -> dict[str, Any]:
        """Set agent's payment capability"""
        return await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/payment-capability",
            json=capability.model_dump(exclude_none=True),
        )

    async def get_payment_capability(self, agent_id: str) -> PaymentCapability | None:
        """Get agent's payment capability"""
        try:
            data = await self._request("GET", f"/api/v1/agents/{agent_id}/payment-capability")
            return PaymentCapability.model_validate(data) if data else None
        except ACNError as e:
            if e.status_code == 404:
                return None
            raise

    async def discover_payment_agents(
        self,
        method: str | None = None,
        network: str | None = None,
        min_amount: float | None = None,
        max_amount: float | None = None,
    ) -> list[AgentInfo]:
        """Discover agents that accept payments"""
        data = await self._request(
            "GET",
            "/api/v1/payments/discover",
            params={
                "method": method,
                "network": network,
                "min_amount": min_amount,
                "max_amount": max_amount,
            },
        )
        return [AgentInfo.model_validate(a) for a in data.get("agents", [])]

    async def get_payment_task(self, task_id: str) -> PaymentTask:
        """Get payment task by ID"""
        data = await self._request("GET", f"/api/v1/payments/tasks/{task_id}")
        return PaymentTask.model_validate(data)

    async def get_agent_payment_tasks(
        self,
        agent_id: str,
        role: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[PaymentTask]:
        """Get agent's payment tasks"""
        data = await self._request(
            "GET",
            f"/api/v1/payments/tasks/agent/{agent_id}",
            params={"role": role, "status": status, "limit": limit},
        )
        return [PaymentTask.model_validate(t) for t in data.get("tasks", [])]

    async def get_payment_stats(self, agent_id: str) -> PaymentStats:
        """Get agent's payment statistics"""
        data = await self._request("GET", f"/api/v1/payments/stats/{agent_id}")
        return PaymentStats.model_validate(data)

    # ============================================
    # Monitoring & Analytics
    # ============================================

    async def get_dashboard(self) -> DashboardData:
        """Get dashboard data"""
        data = await self._request("GET", "/api/v1/monitoring/dashboard")
        return DashboardData.model_validate(data)

    async def get_metrics(self) -> dict[str, Any]:
        """Get all metrics"""
        return await self._request("GET", "/api/v1/monitoring/metrics")

    async def get_system_health(self) -> dict[str, Any]:
        """Get system health"""
        return await self._request("GET", "/api/v1/monitoring/health")

    async def get_agent_analytics(self) -> list[dict[str, Any]]:
        """Get agent analytics"""
        data = await self._request("GET", "/api/v1/analytics/agents")
        analytics: list[dict[str, Any]] = data.get("analytics", [])
        return analytics

    async def get_agent_activity(
        self,
        agent_id: str,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, Any]:
        """Get specific agent's activity"""
        return await self._request(
            "GET",
            f"/api/v1/analytics/agents/{agent_id}",
            params={"start_time": start_time, "end_time": end_time},
        )

    # ============================================
    # Audit
    # ============================================

    async def get_audit_events(
        self,
        event_type: str | None = None,
        actor_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Get audit events"""
        data = await self._request(
            "GET",
            "/api/v1/audit/events",
            params={
                "event_type": event_type,
                "actor_id": actor_id,
                "start_time": start_time,
                "end_time": end_time,
                "limit": limit,
                "offset": offset,
            },
        )
        events: list[dict[str, Any]] = data.get("events", [])
        return events

    async def get_recent_audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        """Get recent audit events"""
        data = await self._request(
            "GET",
            "/api/v1/audit/events/recent",
            params={"limit": limit},
        )
        events: list[dict[str, Any]] = data.get("events", [])
        return events

    # -------------------------------------------------------------------------
    # ERC-8004 On-Chain Identity
    # -------------------------------------------------------------------------

    async def register_onchain(
        self,
        agent_id: str,
        private_key: str | None = None,
        chain: str = "base",
        rpc_url: str | None = None,
        save_wallet_path: str | None = ".env",
    ) -> dict[str, Any]:
        """Register the agent on ERC-8004 Identity Registry and bind to ACN.

        Handles the full flow:
        1. Generate wallet if private_key is None (saved to save_wallet_path).
        2. Construct agentURI pointing to this agent's agent-registration.json.
        3. Build and sign register(agentURI) transaction.
        4. Broadcast and wait for receipt.
        5. Extract token ID from Registered event.
        6. POST /api/v1/onchain/agents/{agent_id}/bind to inform ACN.

        Args:
            agent_id: ACN agent ID (from join response).
            private_key: Ethereum private key (hex). None = auto-generate.
            chain: Target chain. "base" (mainnet) or "base-sepolia" (testnet).
            rpc_url: Custom RPC URL. Defaults to public endpoint for chain.
            save_wallet_path: File path to save generated wallet. Ignored if
                private_key is provided.

        Returns:
            dict with token_id, tx_hash, chain, agent_registration_url,
            wallet_address.
        """
        try:
            from eth_account import Account  # type: ignore[import-untyped]
            from web3 import Web3  # type: ignore[import-untyped]
        except ImportError as e:
            raise ImportError(
                "web3 is required for on-chain registration. "
                "Install it with: pip install web3"
            ) from e

        import json
        import os

        # ---- Chain configuration ----
        chain_configs: dict[str, dict[str, Any]] = {
            "base": {
                "rpc": "https://mainnet.base.org",
                "chain_id": 8453,
                "identity_contract": "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
                "namespace": "eip155:8453",
            },
            "base-sepolia": {
                "rpc": "https://sepolia.base.org",
                "chain_id": 84532,
                "identity_contract": "0x8004A818BFB912233c491871b3d84c89A494BD9e",
                "namespace": "eip155:84532",
            },
        }
        if chain not in chain_configs:
            raise ValueError(f"Unsupported chain: {chain}. Use 'base' or 'base-sepolia'.")
        cfg = chain_configs[chain]
        effective_rpc = rpc_url or cfg["rpc"]

        # ---- Wallet ----
        wallet_generated = False
        if private_key is None:
            account = Account.create()
            private_key = account.key.hex()
            wallet_address = account.address
            wallet_generated = True
            if save_wallet_path:
                _save_wallet_to_env(save_wallet_path, private_key, wallet_address)
            print(f"Wallet generated: {wallet_address}")
            print(f"  Private key saved to: {save_wallet_path}")
            print("  ⚠  Back up your private key!")
        else:
            account = Account.from_key(private_key)
            wallet_address = account.address

        # ---- agentURI ----
        agent_registration_url = (
            f"{self.base_url}/api/v1/agents/{agent_id}"
            "/.well-known/agent-registration.json"
        )

        # ---- Minimal Identity Registry ABI (register function + Registered event) ----
        identity_abi = [
            {
                "inputs": [{"internalType": "string", "name": "agentURI", "type": "string"}],
                "name": "register",
                "outputs": [{"internalType": "uint256", "name": "agentId", "type": "uint256"}],
                "stateMutability": "nonpayable",
                "type": "function",
            },
            {
                "anonymous": False,
                "inputs": [
                    {"indexed": True, "internalType": "uint256", "name": "agentId", "type": "uint256"},
                    {"indexed": False, "internalType": "string", "name": "agentURI", "type": "string"},
                    {"indexed": True, "internalType": "address", "name": "owner", "type": "address"},
                ],
                "name": "Registered",
                "type": "event",
            },
        ]

        # ---- Build & send transaction ----
        w3 = Web3(Web3.HTTPProvider(effective_rpc))
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(cfg["identity_contract"]),
            abi=identity_abi,
        )

        tx = contract.functions.register(agent_registration_url).build_transaction(
            {
                "from": wallet_address,
                "nonce": w3.eth.get_transaction_count(wallet_address),
                "chainId": cfg["chain_id"],
            }
        )
        signed = account.sign_transaction(tx)
        tx_hash_bytes = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash_bytes)
        tx_hash = receipt["transactionHash"].hex()

        # ---- Extract token ID from Registered event ----
        registered_events = contract.events.Registered().process_receipt(receipt)
        if not registered_events:
            raise RuntimeError("Registered event not found in transaction receipt")
        token_id: int = registered_events[0]["args"]["agentId"]

        # ---- Notify ACN ----
        await self._request(
            "POST",
            f"/api/v1/onchain/agents/{agent_id}/bind",
            json={"token_id": token_id, "chain": cfg["namespace"], "tx_hash": tx_hash},
        )

        print(f"\nAgent registered on-chain!")
        print(f"  Token ID:         {token_id}")
        print(f"  Tx Hash:          {tx_hash}")
        print(f"  Chain:            {cfg['namespace']}")
        print(f"  Registration URL: {agent_registration_url}")

        return {
            "token_id": token_id,
            "tx_hash": tx_hash,
            "chain": cfg["namespace"],
            "agent_registration_url": agent_registration_url,
            "wallet_address": wallet_address,
            "wallet_generated": wallet_generated,
        }


def _save_wallet_to_env(path: str, private_key: str, address: str) -> None:
    """Append wallet credentials to a .env file (creates if absent)."""
    import os

    lines = []
    if os.path.exists(path):
        with open(path) as f:
            lines = f.readlines()

    keys_to_set = {
        "WALLET_PRIVATE_KEY": private_key,
        "WALLET_ADDRESS": address,
    }
    existing_keys = {line.split("=")[0].strip() for line in lines if "=" in line}

    with open(path, "a") as f:
        for key, value in keys_to_set.items():
            if key not in existing_keys:
                f.write(f"{key}={value}\n")
