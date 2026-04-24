"""Smoke tests for the /monitoring and /analytics API routes.

Before this test suite existed, 8 of the 9 endpoints exposed under
`acn/routes/monitoring.py` and `acn/routes/analytics.py` silently
returned 500 because the routes called method names that did not exist
on MetricsCollector / Analytics. There were no route-level tests in the
repository (only unit tests of the services), so the breakage was
invisible.

These tests stand up `TestClient(app)` with the Metrics / Analytics / auth
dependencies overridden by AsyncMock stubs that return schema-shaped
dummy payloads, then assert every route returns 200 + a well-formed body.
They do NOT test the real Redis-backed implementation — that's already
covered by `tests/monitoring/`. The point here is to guard against another
"route calls a method that doesn't exist" regression.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.routes.dependencies import get_analytics, get_metrics, limiter, verify_internal_token

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def stub_metrics() -> AsyncMock:
    """A stub MetricsCollector whose methods match the real public surface."""
    m = AsyncMock()
    m.prometheus_export = AsyncMock(
        return_value="# HELP acn_agents_total Total registered agents\n"
        "# TYPE acn_agents_total gauge\n"
        "acn_agents_total 0\n"
    )
    m.get_all_metrics = AsyncMock(
        return_value={
            "counters": {"acn_messages_total": 0},
            "gauges": {"acn_agents_online": 0},
            "histograms": {},
        }
    )
    return m


@pytest.fixture
def stub_analytics() -> AsyncMock:
    """A stub Analytics whose methods match the real public surface."""
    a = AsyncMock()
    a.get_agent_stats = AsyncMock(
        return_value={
            "total": 0,
            "by_status": {"active": 0, "inactive": 0, "unknown": 0},
            "by_subnet": {},
            "by_tag": {},
            "recent_registrations": [],
        }
    )
    a.get_agent_activity = AsyncMock(
        return_value={
            "agent_id": "test-agent",
            "period_hours": 168,
            "messages_sent": None,
            "messages_received": None,
            "errors": None,
            "last_heartbeat": None,
            "data_source_note": "per-agent message and error counts require PG...",
        }
    )
    a.get_message_stats = AsyncMock(
        return_value={
            "total": 0,
            "success": 0,
            "failed": 0,
            "success_rate": 100.0,
            "in_dlq": 0,
        }
    )
    a.get_latency_stats = AsyncMock(
        return_value={
            "by_operation": {},
            "overall": {"count": 0, "avg_ms": 0, "p95_ms": 0, "p99_ms": 0},
        }
    )
    a.get_subnet_stats = AsyncMock(return_value={"total": 0, "subnets": []})
    a.get_system_health = AsyncMock(
        return_value={
            "status": "healthy",
            "health_score": 100,
            "issues": [],
            "summary": {
                "agents_total": 0,
                "agents_active": 0,
                "messages_total": 0,
                "success_rate": 100.0,
                "errors_total": 0,
            },
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
    )
    a.get_dashboard_data = AsyncMock(
        return_value={
            "health": {"status": "healthy", "health_score": 100, "issues": []},
            "agents": {"total": 0},
            "messages": {"total": 0},
            "latency": {"overall": {}},
            "subnets": {"total": 0},
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
    )
    return a


@pytest.fixture
def client(stub_metrics: AsyncMock, stub_analytics: AsyncMock):
    """TestClient with dependencies overridden and rate limiter disabled.

    We override the *dependency functions* (not the underlying singletons)
    because lifespan never runs in this test context, so the real
    `_metrics` / `_analytics` globals would be None.
    """
    # slowapi's Limiter is attached at decorator time; the only clean way to
    # turn it off in tests is to flip .enabled, which short-circuits the
    # per-request check.
    was_enabled = limiter.enabled
    limiter.enabled = False

    app.dependency_overrides[get_metrics] = lambda: stub_metrics
    app.dependency_overrides[get_analytics] = lambda: stub_analytics
    # Internal token is checked via verify_internal_token — override to no-op.
    app.dependency_overrides[verify_internal_token] = lambda: None

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()
    limiter.enabled = was_enabled


# -----------------------------------------------------------------------------
# /monitoring
# -----------------------------------------------------------------------------


class TestMonitoringRoutes:
    def test_prometheus_metrics_calls_prometheus_export(
        self, client: TestClient, stub_metrics: AsyncMock
    ):
        """Anti-regression: route must call prometheus_export(),
        NOT the non-existent export_prometheus()."""
        r = client.get("/metrics")

        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]
        assert "acn_agents_total" in r.text
        stub_metrics.prometheus_export.assert_awaited_once()

    def test_get_all_metrics_returns_json(
        self, client: TestClient, stub_metrics: AsyncMock
    ):
        r = client.get("/api/v1/monitoring/metrics")

        assert r.status_code == 200
        body = r.json()
        assert "counters" in body and "gauges" in body
        stub_metrics.get_all_metrics.assert_awaited_once()

    def test_health_delegates_to_analytics_not_metrics(
        self,
        client: TestClient,
        stub_metrics: AsyncMock,
        stub_analytics: AsyncMock,
    ):
        """Anti-regression: health was previously calling a non-existent
        metrics.get_health_status(). The correct source is analytics."""
        r = client.get("/api/v1/monitoring/health")

        assert r.status_code == 200
        body = r.json()
        assert body["status"] in {"healthy", "degraded", "unhealthy"}
        assert "health_score" in body
        stub_analytics.get_system_health.assert_awaited_once()
        stub_metrics.get_health_status.assert_not_called()  # never existed

    def test_dashboard_calls_only_analytics_dashboard_data(
        self,
        client: TestClient,
        stub_metrics: AsyncMock,
        stub_analytics: AsyncMock,
    ):
        """Anti-regression: dashboard previously called metrics.get_summary()
        and analytics.get_summary() (neither exists). Now a single call to
        analytics.get_dashboard_data() returns everything."""
        r = client.get("/api/v1/monitoring/dashboard")

        assert r.status_code == 200
        body = r.json()
        assert {"health", "agents", "messages", "latency", "subnets"} <= set(body)
        stub_analytics.get_dashboard_data.assert_awaited_once()
        # metrics is not involved in dashboard anymore
        stub_metrics.get_all_metrics.assert_not_called()


# -----------------------------------------------------------------------------
# /analytics
# -----------------------------------------------------------------------------


class TestAnalyticsRoutes:
    def test_agents_calls_get_agent_stats(
        self, client: TestClient, stub_analytics: AsyncMock
    ):
        """Anti-regression: route was calling get_agent_analytics (absent)."""
        r = client.get("/api/v1/analytics/agents")

        assert r.status_code == 200
        body = r.json()
        assert "total" in body and "by_status" in body
        stub_analytics.get_agent_stats.assert_awaited_once()

    def test_agent_activity_converts_days_to_hours(
        self, client: TestClient, stub_analytics: AsyncMock
    ):
        """Anti-regression: route was passing start_time= but the real
        signature is hours=. Here we confirm days→hours conversion is
        what reaches Analytics, and that per-agent counters are null-by-
        design (P1-9)."""
        r = client.get("/api/v1/analytics/agents/test-agent?days=7")

        assert r.status_code == 200
        body = r.json()
        # The stub returns None for all three fields (see stub_analytics fixture).
        # When a real ActivityService is injected, messages_sent and errors
        # will be integers derived from PG activity_events; messages_received
        # remains None until a task-join aggregation is implemented (BACKLOG).
        assert body["messages_sent"] is None
        assert body["messages_received"] is None
        assert body["errors"] is None
        stub_analytics.get_agent_activity.assert_awaited_once()
        call_kwargs = stub_analytics.get_agent_activity.await_args.kwargs
        call_args = stub_analytics.get_agent_activity.await_args.args
        # hours should be 7 * 24 = 168
        assert call_kwargs.get("hours") == 168 or 168 in call_args

    def test_agent_activity_default_window_is_7_days(
        self, client: TestClient, stub_analytics: AsyncMock
    ):
        r = client.get("/api/v1/analytics/agents/test-agent")
        assert r.status_code == 200
        call_kwargs = stub_analytics.get_agent_activity.await_args.kwargs
        call_args = stub_analytics.get_agent_activity.await_args.args
        # default days=7 → 168 hours
        assert call_kwargs.get("hours") == 168 or 168 in call_args

    def test_messages_calls_get_message_stats(
        self, client: TestClient, stub_analytics: AsyncMock
    ):
        r = client.get("/api/v1/analytics/messages")

        assert r.status_code == 200
        body = r.json()
        assert "total" in body and "success_rate" in body
        stub_analytics.get_message_stats.assert_awaited_once()

    def test_latency_calls_get_latency_stats_without_hours(
        self, client: TestClient, stub_analytics: AsyncMock
    ):
        """Anti-regression: the old route passed start_time= to a method
        that didn't exist; the real method takes no time arg."""
        r = client.get("/api/v1/analytics/latency")

        assert r.status_code == 200
        body = r.json()
        assert "overall" in body
        stub_analytics.get_latency_stats.assert_awaited_once_with()

    def test_subnets_calls_get_subnet_stats(
        self, client: TestClient, stub_analytics: AsyncMock
    ):
        r = client.get("/api/v1/analytics/subnets")

        assert r.status_code == 200
        body = r.json()
        assert "total" in body and "subnets" in body
        stub_analytics.get_subnet_stats.assert_awaited_once()


# -----------------------------------------------------------------------------
# Schema drift guards
# -----------------------------------------------------------------------------


class TestMethodNamesStillExist:
    """Pin the method names the routes depend on. If somebody renames one of
    these in the service class without updating the route, CI fails here
    instead of silently at runtime."""

    def test_metrics_surface(self):
        from acn.monitoring.metrics import MetricsCollector

        for name in ("prometheus_export", "get_all_metrics"):
            assert hasattr(MetricsCollector, name), (
                f"MetricsCollector.{name}() removed/renamed — "
                "acn/routes/monitoring.py still calls it"
            )

    def test_analytics_surface(self):
        from acn.monitoring.analytics import Analytics

        for name in (
            "get_agent_stats",
            "get_agent_activity",
            "get_message_stats",
            "get_latency_stats",
            "get_subnet_stats",
            "get_system_health",
            "get_dashboard_data",
        ):
            assert hasattr(Analytics, name), (
                f"Analytics.{name}() removed/renamed — "
                "acn/routes/{monitoring,analytics}.py still calls it"
            )
