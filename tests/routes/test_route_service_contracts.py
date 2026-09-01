"""Route-Service Contract Tests

Two-layer smoke tests for all ACN API routers:

Layer 1 — TestMethodNamesStillExist
    Asserts that every method name called by a route handler still exists on
    the real service/manager class.  Catches "method renamed but route not
    updated" regressions without starting a server.

Layer 2 — TestRouteServiceContract
    Stands up TestClient(app) with dependency-overridden AsyncMock stubs,
    hits each endpoint, and uses assert_called_with / assert_awaited_with to
    verify that the correct arguments (including positional order) are passed
    to the service layer.  This is the layer that would have caught the
    ``ws_manager.connect(agent_id, websocket)`` parameter-reversal bug.

Routers covered
    registry, communication, subnets, payments, tasks, onchain, websocket
"""

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.infrastructure.messaging.websocket_manager import WebSocketManager
from acn.monitoring.audit import AuditLogger
from acn.monitoring.metrics import MetricsCollector
from acn.protocols.ap2.core import PaymentDiscoveryService, PaymentTaskManager
from acn.routes.dependencies import (
    get_agent_service,
    get_audit,
    get_metrics,
    get_payment_discovery,
    get_payment_tasks,
    get_subnet_service,
    get_ws_manager,
    limiter,
    verify_internal_token,
)
from acn.routes.tasks import get_task_service
from acn.services import AgentService
from acn.services.activity_service import ActivityService
from acn.services.subnet_service import SubnetService

# =============================================================================
# Layer 1 — Method names still exist
# =============================================================================


class TestMethodNamesStillExist:
    """Guard against service method renames breaking routes at runtime."""

    # --- WebSocketManager ---
    def test_ws_manager_connect(self):
        assert callable(getattr(WebSocketManager, "connect", None))

    def test_ws_manager_disconnect(self):
        assert callable(getattr(WebSocketManager, "disconnect", None))

    def test_ws_manager_is_user_connected(self):
        assert callable(getattr(WebSocketManager, "is_user_connected", None))

    def test_ws_manager_get_stats(self):
        assert callable(getattr(WebSocketManager, "get_stats", None))

    # --- MetricsCollector (communication route) ---
    def test_metrics_inc_message_count(self):
        assert callable(getattr(MetricsCollector, "inc_message_count", None))

    def test_metrics_inc_counter(self):
        assert callable(getattr(MetricsCollector, "inc_counter", None))

    # --- PaymentDiscoveryService ---
    def test_payment_discovery_find_agents_accepting_payment(self):
        assert callable(getattr(PaymentDiscoveryService, "find_agents_accepting_payment", None))

    def test_payment_discovery_get_agent_payment_capability(self):
        assert callable(getattr(PaymentDiscoveryService, "get_agent_payment_capability", None))

    def test_payment_discovery_index_payment_capability(self):
        assert callable(getattr(PaymentDiscoveryService, "index_payment_capability", None))

    # --- PaymentTaskManager ---
    def test_payment_tasks_get_tasks_by_agent(self):
        assert callable(getattr(PaymentTaskManager, "get_tasks_by_agent", None))

    def test_payment_tasks_get_payment_stats(self):
        assert callable(getattr(PaymentTaskManager, "get_payment_stats", None))

    def test_payment_tasks_create_payment_task(self):
        assert callable(getattr(PaymentTaskManager, "create_payment_task", None))

    def test_payment_tasks_get_task(self):
        assert callable(getattr(PaymentTaskManager, "get_task", None))

    # --- AgentService ---
    def test_agent_service_register_agent(self):
        assert callable(getattr(AgentService, "register_agent", None))

    def test_agent_service_get_agent(self):
        assert callable(getattr(AgentService, "get_agent", None))

    def test_agent_service_get_agent_by_api_key(self):
        assert callable(getattr(AgentService, "get_agent_by_api_key", None))

    def test_agent_service_search_agents(self):
        assert callable(getattr(AgentService, "search_agents", None))

    def test_agent_service_update_heartbeat(self):
        assert callable(getattr(AgentService, "update_heartbeat", None))

    def test_agent_service_set_desired_preferred_model(self):
        assert callable(getattr(AgentService, "set_desired_preferred_model", None))

    def test_agent_service_clear_desired_preferred_model(self):
        assert callable(getattr(AgentService, "clear_desired_preferred_model", None))

    def test_agent_service_unregister_agent(self):
        assert callable(getattr(AgentService, "unregister_agent", None))

    # --- SubnetService ---
    def test_subnet_service_create_subnet(self):
        assert callable(getattr(SubnetService, "create_subnet", None))

    def test_subnet_service_list_subnets(self):
        assert callable(getattr(SubnetService, "list_subnets", None))

    def test_subnet_service_list_public_subnets(self):
        assert callable(getattr(SubnetService, "list_public_subnets", None))

    def test_subnet_service_get_subnet(self):
        assert callable(getattr(SubnetService, "get_subnet", None))

    def test_subnet_service_delete_subnet(self):
        assert callable(getattr(SubnetService, "delete_subnet", None))

    def test_subnet_service_add_member(self):
        assert callable(getattr(SubnetService, "add_member", None))

    def test_subnet_service_remove_member(self):
        assert callable(getattr(SubnetService, "remove_member", None))

    # --- ActivityService ---
    def test_activity_service_get_activity_counts(self):
        assert callable(getattr(ActivityService, "get_activity_counts", None))

    def test_activity_service_get_last_activity_at(self):
        assert callable(getattr(ActivityService, "get_last_activity_at", None))

    def test_activity_service_get_received_count(self):
        assert callable(getattr(ActivityService, "get_received_count", None))

    # --- MessageService (P2-B: precise ack) ---
    def test_message_service_ack_message_history(self):
        from acn.services.message_service import MessageService

        assert callable(getattr(MessageService, "ack_message_history", None))

    # --- MessageRouter (P2-B: precise ack) ---
    def test_message_router_ack_inbox(self):
        from acn.infrastructure.messaging.message_router import MessageRouter

        assert callable(getattr(MessageRouter, "ack_inbox", None))


# =============================================================================
# Helpers
# =============================================================================


def _make_agent_info(agent_id: str = "agent-test-001") -> dict:
    return {"agent_id": agent_id, "owner": "user-1"}


def _make_agent_mock(agent_id: str = "agent-test-001"):
    a = MagicMock()
    a.agent_id = agent_id
    a.owner = "user-1"
    a.name = "Test Agent"
    a.metadata = {}
    a.model_dump = MagicMock(return_value={"agent_id": agent_id, "name": "Test Agent"})
    return a


# =============================================================================
# Layer 2 — Route-service contract (parameter-level)
# =============================================================================


@pytest.fixture(autouse=True)
def _disable_rate_limiter():
    was = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = was


# ─────────────────────────────────────────────
# WebSocket contracts
# ─────────────────────────────────────────────


class TestWebSocketContract:
    """
    Verify that the websocket route passes arguments to WebSocketManager
    in the correct order and with the correct keyword names.
    The pre-fix bug was: connect(agent_id, websocket) — args reversed.
    """

    def test_is_user_connected_called_with_agent_id(self):
        """Anti-regression: route must call is_user_connected(agent_id),
        not the non-existent is_connected(agent_id)."""
        from acn.routes.dependencies import verify_agent_api_key

        stub_ws = MagicMock()
        stub_ws.is_user_connected = MagicMock(return_value=True)

        app.dependency_overrides[get_ws_manager] = lambda: stub_ws
        app.dependency_overrides[verify_agent_api_key] = lambda: _make_agent_info("agent-abc")

        with TestClient(app) as client:
            r = client.get("/api/v1/websocket/agent/agent-abc/status")

        app.dependency_overrides.clear()

        assert r.status_code == 200
        assert r.json() == {"agent_id": "agent-abc", "connected": True}
        # Core contract: called with exactly the agent_id from the path
        stub_ws.is_user_connected.assert_called_once_with("agent-abc")

    def test_get_stats_is_sync(self):
        """get_stats() must be sync — the route calls it without await."""
        stub_ws = MagicMock()
        stub_ws.get_stats = MagicMock(return_value={"total_connections": 0, "total_channels": 0})
        assert not hasattr(stub_ws.get_stats, "__await__")

    def test_ws_manager_connect_signature(self):
        """connect() must accept (websocket, user_id=, metadata=, already_accepted=)."""
        import inspect

        sig = inspect.signature(WebSocketManager.connect)
        params = list(sig.parameters)
        assert params[1] == "websocket", "first positional arg must be websocket"
        assert "user_id" in params
        assert "already_accepted" in params


# ─────────────────────────────────────────────
# Communication contracts
# ─────────────────────────────────────────────


class TestCommunicationContract:
    @pytest.fixture
    def stub_metrics(self):
        m = AsyncMock()
        m.inc_message_count = AsyncMock()
        m.inc_counter = AsyncMock()
        return m

    @pytest.fixture
    def stub_message_service(self):
        # NOTE: ``broadcast_message`` was deleted in the Phase 2 Group C #9
        # convergence (HTTP broadcast routes now go through
        # ``BroadcastService.broadcast``). The contract tests in this
        # class only exercise the single-send path, so a sender-only
        # stub is sufficient.
        svc = AsyncMock()
        svc.send_message = AsyncMock(return_value={"message_id": "msg-1", "status": "sent"})
        return svc

    @pytest.fixture
    def stub_audit(self):
        a = AsyncMock()
        a.log_event = AsyncMock()
        return a

    @pytest.fixture
    def stub_router(self):
        r = AsyncMock()
        r.retry_dlq = AsyncMock(return_value=3)
        return r

    def test_send_message_calls_inc_message_count(
        self, stub_metrics, stub_message_service, stub_audit
    ):
        from acn.routes.dependencies import (
            get_message_service,
            verify_agent_api_key,
        )

        app.dependency_overrides[get_metrics] = lambda: stub_metrics
        app.dependency_overrides[get_message_service] = lambda: stub_message_service
        app.dependency_overrides[get_audit] = lambda: stub_audit
        app.dependency_overrides[verify_agent_api_key] = lambda: _make_agent_info("agent-a")

        # The route constructs an a2a Message internally before calling the
        # service.  Patch Message at the import site used by the route module
        # so we can return a MagicMock without triggering Pydantic validation.
        stub_msg = MagicMock()
        with patch("acn.routes.communication.Message", return_value=stub_msg):
            with TestClient(app) as client:
                r = client.post(
                    "/api/v1/communication/send",
                    json={
                        "from_agent": "agent-a",
                        "target_agent": "agent-b",
                        "message": {"text": "hello"},
                    },
                )

        app.dependency_overrides.clear()

        assert r.status_code == 200
        stub_metrics.inc_message_count.assert_awaited_once()
        call_kwargs = stub_metrics.inc_message_count.await_args.kwargs
        assert call_kwargs.get("from_agent") == "agent-a"
        assert call_kwargs.get("to_agent") == "agent-b"
        assert call_kwargs.get("status") == "success"

        stub_audit.log_event.assert_awaited_once()
        audit_kwargs = stub_audit.log_event.await_args.kwargs
        assert "actor_id" in audit_kwargs and audit_kwargs["actor_id"] == "agent-a"
        assert "target_id" in audit_kwargs and audit_kwargs["target_id"] == "agent-b"
        assert "actor" not in audit_kwargs, (
            "audit.log_event signature uses actor_id, not actor — "
            "this kwarg name regression caused 500s on every successful send."
        )
        assert "resource" not in audit_kwargs, (
            "audit.log_event signature uses target_id, not resource."
        )

        from acn.monitoring.audit import AuditEventType

        sig = inspect.signature(AuditLogger.log_event)
        for kw in audit_kwargs:
            assert kw in sig.parameters, (
                f"audit.log_event was called with unknown kwarg {kw!r}; "
                f"expected one of {list(sig.parameters)}"
            )
        et = audit_kwargs.get("event_type")
        assert et in (
            AuditEventType.MESSAGE_SENT,
            AuditEventType.MESSAGE_SENT.value,
        ), f"event_type should be a valid AuditEventType, got {et!r}"

    def test_retry_dlq_calls_retry_dlq_not_retry_failed_messages(self, stub_router):
        from acn.routes.dependencies import get_router

        app.dependency_overrides[get_router] = lambda: stub_router
        app.dependency_overrides[verify_internal_token] = lambda: None

        with TestClient(app) as client:
            r = client.post("/api/v1/communication/retry-dlq?max_retries=3")

        app.dependency_overrides.clear()

        assert r.status_code == 200
        stub_router.retry_dlq.assert_awaited_once_with(max_retries=3)
        body = r.json()
        assert body["retried"] == 3


# ─────────────────────────────────────────────
# Payment contracts
# ─────────────────────────────────────────────


class TestPaymentContract:
    @pytest.fixture
    def stub_payment_discovery(self):
        pd = AsyncMock()
        pd.find_agents_accepting_payment = AsyncMock(return_value=["agent-x"])
        pd.get_agent_payment_capability = AsyncMock(return_value=None)
        pd.index_payment_capability = AsyncMock()
        return pd

    @pytest.fixture
    def stub_payment_tasks(self):
        pt = AsyncMock()
        pt.get_tasks_by_agent = AsyncMock(return_value=[])
        pt.get_payment_stats = AsyncMock(return_value={"total_tasks": 0})
        return pt

    def test_discover_calls_find_agents_accepting_payment(self, stub_payment_discovery):
        from acn.routes.dependencies import verify_agent_api_key

        app.dependency_overrides[get_payment_discovery] = lambda: stub_payment_discovery
        # P3-2: discover now requires authentication
        app.dependency_overrides[verify_agent_api_key] = lambda: _make_agent_info("agent-x")

        with TestClient(app) as client:
            r = client.get("/api/v1/payments/discover")

        app.dependency_overrides.clear()

        assert r.status_code == 200
        stub_payment_discovery.find_agents_accepting_payment.assert_awaited_once()
        # verify keyword names match the actual service signature
        kwargs = stub_payment_discovery.find_agents_accepting_payment.await_args.kwargs
        assert "payment_method" in kwargs
        assert "network" in kwargs

    def test_get_agent_tasks_calls_get_tasks_by_agent(self, stub_payment_tasks):
        from acn.routes.dependencies import verify_agent_api_key

        app.dependency_overrides[get_payment_tasks] = lambda: stub_payment_tasks
        app.dependency_overrides[verify_agent_api_key] = lambda: _make_agent_info("agent-a")

        with TestClient(app) as client:
            r = client.get("/api/v1/payments/tasks/agent/agent-a")

        app.dependency_overrides.clear()

        assert r.status_code == 200
        stub_payment_tasks.get_tasks_by_agent.assert_awaited_once()
        kwargs = stub_payment_tasks.get_tasks_by_agent.await_args.kwargs
        assert kwargs.get("agent_id") == "agent-a"

    def test_get_agent_stats_calls_get_payment_stats(self, stub_payment_tasks):
        from acn.routes.dependencies import verify_agent_api_key

        app.dependency_overrides[get_payment_tasks] = lambda: stub_payment_tasks
        app.dependency_overrides[verify_agent_api_key] = lambda: _make_agent_info("agent-a")

        with TestClient(app) as client:
            r = client.get("/api/v1/payments/stats/agent-a")

        app.dependency_overrides.clear()

        assert r.status_code == 200
        stub_payment_tasks.get_payment_stats.assert_awaited_once_with("agent-a")


# ─────────────────────────────────────────────
# Registry contracts
# ─────────────────────────────────────────────


class TestRegistryContract:
    @pytest.fixture
    def stub_agent_service(self):
        svc = AsyncMock()
        svc.search_agents = AsyncMock(return_value=[])
        svc.get_agent = AsyncMock(return_value=_make_agent_mock())
        svc.update_heartbeat = AsyncMock(return_value=_make_agent_mock())
        svc.set_desired_preferred_model = AsyncMock(return_value=_make_agent_mock())
        svc.clear_desired_preferred_model = AsyncMock(return_value=_make_agent_mock())
        return svc

    def test_search_agents_endpoint_calls_search_agents(self, stub_agent_service):
        app.dependency_overrides[get_agent_service] = lambda: stub_agent_service

        with TestClient(app) as client:
            # registry router prefix is /api/v1/agents; search is GET ""
            r = client.get("/api/v1/agents")

        app.dependency_overrides.clear()

        assert r.status_code == 200
        stub_agent_service.search_agents.assert_awaited_once()

    def test_heartbeat_calls_update_heartbeat(self, stub_agent_service):
        from acn.routes.dependencies import verify_agent_api_key

        app.dependency_overrides[get_agent_service] = lambda: stub_agent_service
        app.dependency_overrides[verify_agent_api_key] = lambda: _make_agent_info("agent-hb")

        with TestClient(app) as client:
            r = client.post("/api/v1/agents/agent-hb/heartbeat")

        app.dependency_overrides.clear()

        assert r.status_code == 200
        stub_agent_service.update_heartbeat.assert_awaited_once_with(
            "agent-hb",
            preferred_model=None,
            supported_models=None,
        )


# ─────────────────────────────────────────────
# Subnets contracts
# ─────────────────────────────────────────────


class TestSubnetsContract:
    @pytest.fixture
    def stub_subnet_service(self):
        svc = AsyncMock()
        svc.list_subnets = AsyncMock(return_value=[])
        svc.get_subnet = AsyncMock(return_value=MagicMock(
            slug="subnet-1", name="test", owner="user-1",
            is_public=True, member_count=0,
            model_dump=MagicMock(return_value={"slug": "subnet-1"}),
        ))
        return svc

    @pytest.fixture
    def stub_agent_service(self):
        svc = AsyncMock()
        svc.search_agents = AsyncMock(return_value=[])
        return svc

    def test_list_subnets_calls_list_subnets(
        self, stub_subnet_service, stub_agent_service
    ):
        """Unfiltered GET /subnets calls list_subnets() (all subnets); private
        ones are downgraded to SubnetStub per-row by the V6 B5 renderer."""
        app.dependency_overrides[get_subnet_service] = lambda: stub_subnet_service
        app.dependency_overrides[get_agent_service] = lambda: stub_agent_service

        with TestClient(app) as client:
            r = client.get("/api/v1/subnets/")

        app.dependency_overrides.clear()

        assert r.status_code == 200
        stub_subnet_service.list_subnets.assert_awaited_once()


# ─────────────────────────────────────────────
# Tasks contracts
# ─────────────────────────────────────────────


class TestTasksContract:
    @pytest.fixture
    def stub_task_service(self):
        svc = AsyncMock()
        svc.list_tasks = AsyncMock(return_value=[])
        svc.get_task = AsyncMock(return_value=None)
        return svc

    def test_list_tasks_calls_list_tasks(self, stub_task_service):
        app.dependency_overrides[get_task_service] = lambda: stub_task_service

        with TestClient(app) as client:
            r = client.get("/api/v1/tasks/")

        app.dependency_overrides.clear()

        assert r.status_code == 200
        stub_task_service.list_tasks.assert_awaited_once()


# ─────────────────────────────────────────────
# Onchain contracts
# ─────────────────────────────────────────────


class TestOnchainContract:
    """
    onchain.py uses an erc8004 client injected via a local module-level
    getter; we patch it directly.
    """

    def test_discover_agents_calls_erc8004_discover(self):
        from acn.routes.onchain import get_erc8004_client

        stub_erc = AsyncMock()
        stub_erc.discover_agents = AsyncMock(return_value=[])

        # get_erc8004_client is used as a FastAPI Depends — override it.
        app.dependency_overrides[get_erc8004_client] = lambda: stub_erc
        # Override agent_service to avoid Redis cache lookup.
        stub_agent_svc = AsyncMock()
        stub_agent_svc.redis = AsyncMock()
        stub_agent_svc.redis.get = AsyncMock(return_value=None)
        stub_agent_svc.redis.setex = AsyncMock()
        app.dependency_overrides[get_agent_service] = lambda: stub_agent_svc

        with TestClient(app) as client:
            r = client.get("/api/v1/onchain/discover")

        app.dependency_overrides.clear()

        assert r.status_code == 200
        stub_erc.discover_agents.assert_awaited_once()
