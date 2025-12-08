"""
Tests for ACN Monitoring & Analytics Layer

Tests cover:
- MetricsCollector: Counter, gauge, histogram operations
- AuditLogger: Event logging and querying
- Analytics: Statistics and reporting
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from acn.monitoring import Analytics, AuditLogger, MetricsCollector
from acn.monitoring.audit import AuditEvent, AuditEventType, AuditLevel

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_redis():
    """Create a mock Redis client"""
    redis = AsyncMock()

    # Store data in memory for testing
    redis._data = {}
    redis._lists = {}
    redis._streams = {}

    async def mock_set(key, value):
        redis._data[key] = value

    async def mock_get(key):
        return redis._data.get(key)

    async def mock_incr(key, amount=1):
        current = int(redis._data.get(key, 0))
        redis._data[key] = str(current + amount)
        return current + amount

    async def mock_incrbyfloat(key, amount):
        current = float(redis._data.get(key, 0))
        redis._data[key] = str(current + amount)
        return current + amount

    async def mock_keys(pattern):
        import fnmatch

        pattern = pattern.replace("*", ".*")
        return [k for k in redis._data.keys() if fnmatch.fnmatch(k, pattern.replace(".*", "*"))]

    async def mock_lpush(key, *values):
        if key not in redis._lists:
            redis._lists[key] = []
        for v in values:
            redis._lists[key].insert(0, v)
        return len(redis._lists[key])

    async def mock_lrange(key, start, end):
        if key not in redis._lists:
            return []
        if end == -1:
            return redis._lists[key][start:]
        return redis._lists[key][start : end + 1]

    async def mock_ltrim(key, start, end):
        if key in redis._lists:
            redis._lists[key] = redis._lists[key][start : end + 1]

    async def mock_llen(key):
        return len(redis._lists.get(key, []))

    async def mock_xadd(stream, data, maxlen=None):
        if stream not in redis._streams:
            redis._streams[stream] = []
        entry_id = f"{int(datetime.now(UTC).timestamp() * 1000)}-{len(redis._streams[stream])}"
        # Store data with bytes keys like real Redis
        bytes_data = {
            k.encode() if isinstance(k, str) else k: v.encode() if isinstance(v, str) else v
            for k, v in data.items()
        }
        redis._streams[stream].append((entry_id, bytes_data))
        if maxlen and len(redis._streams[stream]) > maxlen:
            redis._streams[stream] = redis._streams[stream][-maxlen:]
        return entry_id

    async def mock_xrevrange(stream, max="+", min="-", count=None):
        if stream not in redis._streams:
            return []
        entries = list(reversed(redis._streams[stream]))
        if count:
            entries = entries[:count]
        return entries

    async def mock_xlen(stream):
        return len(redis._streams.get(stream, []))

    async def mock_expire(key, seconds):
        pass

    async def mock_hgetall(key):
        return redis._data.get(key, {})

    async def mock_xtrim(stream, minid=None):
        # Simple mock that doesn't actually trim
        return 0

    redis.set = mock_set
    redis.get = mock_get
    redis.incr = mock_incr
    redis.incrbyfloat = mock_incrbyfloat
    redis.keys = mock_keys
    redis.lpush = mock_lpush
    redis.lrange = mock_lrange
    redis.ltrim = mock_ltrim
    redis.llen = mock_llen
    redis.xadd = mock_xadd
    redis.xrevrange = mock_xrevrange
    redis.xlen = mock_xlen
    redis.expire = mock_expire
    redis.hgetall = mock_hgetall
    redis.xtrim = mock_xtrim

    return redis


@pytest.fixture
def metrics_collector(mock_redis):
    """Create MetricsCollector instance"""
    return MetricsCollector(mock_redis)


@pytest.fixture
def audit_logger(mock_redis):
    """Create AuditLogger instance"""
    return AuditLogger(mock_redis)


@pytest.fixture
def analytics_instance(mock_redis):
    """Create Analytics instance"""
    return Analytics(mock_redis)


# =============================================================================
# MetricsCollector Tests
# =============================================================================


class TestMetricsCollector:
    """Tests for MetricsCollector"""

    @pytest.mark.asyncio
    async def test_start_stop(self, metrics_collector):
        """Test start and stop"""
        await metrics_collector.start()
        assert metrics_collector._started is True

        await metrics_collector.stop()
        assert metrics_collector._started is False

    @pytest.mark.asyncio
    async def test_inc_counter(self, metrics_collector):
        """Test incrementing counter"""
        await metrics_collector.start()

        await metrics_collector.inc_counter(
            "messages_total", labels={"from_agent": "a", "to_agent": "b", "status": "success"}
        )

        value = await metrics_collector.get_counter(
            "messages_total", labels={"from_agent": "a", "to_agent": "b", "status": "success"}
        )
        assert value == 1

        # Increment again
        await metrics_collector.inc_counter(
            "messages_total", labels={"from_agent": "a", "to_agent": "b", "status": "success"}
        )
        value = await metrics_collector.get_counter(
            "messages_total", labels={"from_agent": "a", "to_agent": "b", "status": "success"}
        )
        assert value == 2

    @pytest.mark.asyncio
    async def test_inc_message_count(self, metrics_collector):
        """Test convenience method for message count"""
        await metrics_collector.start()

        await metrics_collector.inc_message_count("agent-a", "agent-b", "success")
        await metrics_collector.inc_message_count("agent-a", "agent-b", "failed")

        success = await metrics_collector.get_counter(
            "messages_total",
            labels={"from_agent": "agent-a", "to_agent": "agent-b", "status": "success"},
        )
        failed = await metrics_collector.get_counter(
            "messages_total",
            labels={"from_agent": "agent-a", "to_agent": "agent-b", "status": "failed"},
        )

        assert success == 1
        assert failed == 1

    @pytest.mark.asyncio
    async def test_set_gauge(self, metrics_collector):
        """Test setting gauge value"""
        await metrics_collector.start()

        await metrics_collector.set_gauge(
            "agents_registered", 42, labels={"subnet": "public", "status": "active"}
        )

        value = await metrics_collector.get_gauge(
            "agents_registered", labels={"subnet": "public", "status": "active"}
        )
        assert value == 42.0

    @pytest.mark.asyncio
    async def test_inc_dec_gauge(self, metrics_collector):
        """Test incrementing and decrementing gauge"""
        await metrics_collector.start()

        await metrics_collector.set_gauge("gateway_connections", 5, labels={"subnet": "team-a"})

        await metrics_collector.inc_gauge("gateway_connections", 2, labels={"subnet": "team-a"})
        value = await metrics_collector.get_gauge(
            "gateway_connections", labels={"subnet": "team-a"}
        )
        assert value == 7.0

        await metrics_collector.dec_gauge("gateway_connections", 3, labels={"subnet": "team-a"})
        value = await metrics_collector.get_gauge(
            "gateway_connections", labels={"subnet": "team-a"}
        )
        assert value == 4.0

    @pytest.mark.asyncio
    async def test_observe_latency(self, metrics_collector, mock_redis):
        """Test recording latency observation"""
        await metrics_collector.start()

        await metrics_collector.observe_latency("route_message", 0.015)
        await metrics_collector.observe_latency("route_message", 0.025)
        await metrics_collector.observe_latency("route_message", 0.010)

        stats = await metrics_collector.get_histogram_stats(
            "latency_seconds", labels={"operation": "route_message"}
        )

        assert stats["count"] == 3
        assert stats["min"] == 0.010
        assert stats["max"] == 0.025

    @pytest.mark.asyncio
    async def test_timer_context_manager(self, metrics_collector, mock_redis):
        """Test timer context manager"""
        await metrics_collector.start()

        async with metrics_collector.timer("test_operation"):
            await asyncio.sleep(0.01)  # 10ms

        # Verify latency was recorded
        key = "acn:metrics:acn_latency_seconds:operation=test_operation:values"
        assert key in mock_redis._lists
        assert len(mock_redis._lists[key]) == 1

    @pytest.mark.asyncio
    async def test_prometheus_export(self, metrics_collector, mock_redis):
        """Test Prometheus format export"""
        await metrics_collector.start()

        await metrics_collector.inc_counter(
            "messages_total", labels={"from_agent": "a", "to_agent": "b", "status": "success"}
        )
        await metrics_collector.set_gauge(
            "agents_registered", 10, labels={"subnet": "public", "status": "active"}
        )

        output = await metrics_collector.prometheus_export()

        assert "acn_uptime_seconds" in output
        assert isinstance(output, str)


# =============================================================================
# AuditLogger Tests
# =============================================================================


class TestAuditLogger:
    """Tests for AuditLogger"""

    @pytest.mark.asyncio
    async def test_start_stop(self, audit_logger, mock_redis):
        """Test start and stop logging"""
        await audit_logger.start()
        assert audit_logger._started is True

        # Should have logged system_started event
        assert len(mock_redis._streams.get("acn:audit:stream", [])) >= 1

        await audit_logger.stop()
        assert audit_logger._started is False

    @pytest.mark.asyncio
    async def test_log_event(self, audit_logger, mock_redis):
        """Test logging an event"""
        event_id = await audit_logger.log_event(
            event_type=AuditEventType.AGENT_REGISTERED,
            actor_id="admin",
            target_id="cursor-agent",
            subnet_id="public",
            details={"skills": ["code", "test"]},
        )

        assert event_id is not None
        assert len(mock_redis._streams["acn:audit:stream"]) == 1

    @pytest.mark.asyncio
    async def test_log_agent_registered(self, audit_logger, mock_redis):
        """Test convenience method for agent registration"""
        event_id = await audit_logger.log_agent_registered(
            agent_id="test-agent",
            subnet_id="team-a",
            skills=["chat", "search"],
            source_ip="192.168.1.1",
        )

        assert event_id is not None

    @pytest.mark.asyncio
    async def test_log_message_sent(self, audit_logger, mock_redis):
        """Test logging message sent"""
        event_id = await audit_logger.log_message_sent(
            from_agent="agent-a",
            to_agent="agent-b",
            message_id="msg-123",
        )

        assert event_id is not None

    @pytest.mark.asyncio
    async def test_log_message_failed(self, audit_logger, mock_redis):
        """Test logging message failure"""
        event_id = await audit_logger.log_message_failed(
            from_agent="agent-a",
            to_agent="agent-b",
            message_id="msg-123",
            error="Connection timeout",
        )

        assert event_id is not None

    @pytest.mark.asyncio
    async def test_log_auth_success(self, audit_logger, mock_redis):
        """Test logging successful authentication"""
        event_id = await audit_logger.log_auth_success(
            agent_id="test-agent",
            subnet_id="enterprise",
            auth_method="bearer",
        )

        assert event_id is not None

    @pytest.mark.asyncio
    async def test_log_auth_failure(self, audit_logger, mock_redis):
        """Test logging authentication failure"""
        event_id = await audit_logger.log_auth_failure(
            agent_id="unknown",
            subnet_id="enterprise",
            reason="Invalid token",
            source_ip="10.0.0.1",
        )

        assert event_id is not None

    @pytest.mark.asyncio
    async def test_query_events(self, audit_logger, mock_redis):
        """Test querying events"""
        # Log some events
        await audit_logger.log_agent_registered("agent-1", "public")
        await audit_logger.log_agent_registered("agent-2", "team-a")
        await audit_logger.log_message_sent("agent-1", "agent-2", "msg-1")

        # Query all events
        events = await audit_logger.query_events(limit=10)
        assert len(events) == 3

        # Query by event type
        events = await audit_logger.query_events(
            event_type=AuditEventType.AGENT_REGISTERED,
            limit=10,
        )
        assert len(events) == 2

    @pytest.mark.asyncio
    async def test_get_recent_events(self, audit_logger, mock_redis):
        """Test getting recent events"""
        await audit_logger.log_agent_registered("agent-1", "public")
        await audit_logger.log_agent_registered("agent-2", "team-a")

        events = await audit_logger.get_recent_events(limit=5)
        assert len(events) == 2

    @pytest.mark.asyncio
    async def test_count_events(self, audit_logger, mock_redis):
        """Test counting events"""
        await audit_logger.log_agent_registered("agent-1", "public")
        await audit_logger.log_agent_registered("agent-2", "team-a")
        await audit_logger.log_message_sent("agent-1", "agent-2", "msg-1")

        count = await audit_logger.count_events()
        assert count == 3

    @pytest.mark.asyncio
    async def test_get_event_stats(self, audit_logger, mock_redis):
        """Test getting event statistics"""
        await audit_logger.log_agent_registered("agent-1", "public")
        await audit_logger.log_agent_registered("agent-2", "team-a")
        await audit_logger.log_message_sent("agent-1", "agent-2", "msg-1")
        await audit_logger.log_error("Connection failed", "message_router")

        stats = await audit_logger.get_event_stats()

        assert stats["total"] == 4
        assert AuditEventType.AGENT_REGISTERED.value in stats["by_type"]
        assert stats["by_type"][AuditEventType.AGENT_REGISTERED.value] == 2

    @pytest.mark.asyncio
    async def test_export_events_json(self, audit_logger, mock_redis):
        """Test exporting events as JSON"""
        await audit_logger.log_agent_registered("agent-1", "public")

        export = await audit_logger.export_events(format="json")

        assert isinstance(export, str)
        assert "agent-1" in export

    @pytest.mark.asyncio
    async def test_export_events_csv(self, audit_logger, mock_redis):
        """Test exporting events as CSV"""
        await audit_logger.log_agent_registered("agent-1", "public")

        export = await audit_logger.export_events(format="csv")

        assert isinstance(export, str)
        assert "agent_registered" in export


# =============================================================================
# Analytics Tests
# =============================================================================


class TestAnalytics:
    """Tests for Analytics"""

    @pytest.mark.asyncio
    async def test_get_agent_stats(self, analytics_instance, mock_redis):
        """Test getting agent statistics"""
        stats = await analytics_instance.get_agent_stats()

        assert "total" in stats
        assert "by_status" in stats
        assert "by_subnet" in stats
        assert "by_skill" in stats

    @pytest.mark.asyncio
    async def test_get_message_stats(self, analytics_instance, mock_redis):
        """Test getting message statistics"""
        stats = await analytics_instance.get_message_stats()

        assert "total" in stats
        assert "success" in stats
        assert "failed" in stats
        assert "success_rate" in stats

    @pytest.mark.asyncio
    async def test_get_latency_stats(self, analytics_instance, mock_redis):
        """Test getting latency statistics"""
        stats = await analytics_instance.get_latency_stats()

        assert "route_message" in stats
        assert "register" in stats

    @pytest.mark.asyncio
    async def test_get_subnet_stats(self, analytics_instance, mock_redis):
        """Test getting subnet statistics"""
        stats = await analytics_instance.get_subnet_stats()

        assert "total" in stats
        assert "subnets" in stats
        # Should at least have public subnet
        assert len(stats["subnets"]) >= 1

    @pytest.mark.asyncio
    async def test_get_system_health(self, analytics_instance, mock_redis):
        """Test getting system health"""
        health = await analytics_instance.get_system_health()

        assert "status" in health
        assert health["status"] in ["healthy", "degraded", "unhealthy"]
        assert "health_score" in health
        assert 0 <= health["health_score"] <= 100
        assert "issues" in health
        assert "summary" in health

    @pytest.mark.asyncio
    async def test_get_dashboard_data(self, analytics_instance, mock_redis):
        """Test getting dashboard data"""
        data = await analytics_instance.get_dashboard_data()

        assert "health" in data
        assert "agents" in data
        assert "messages" in data
        assert "latency" in data
        assert "subnets" in data
        assert "timestamp" in data

    @pytest.mark.asyncio
    async def test_generate_report(self, analytics_instance, mock_redis):
        """Test generating report"""
        report = await analytics_instance.generate_report(report_type="daily")

        assert "report_type" in report
        assert report["report_type"] == "daily"
        assert "period" in report
        assert "summary" in report
        assert "agents" in report
        assert "messages" in report

    @pytest.mark.asyncio
    async def test_get_message_volume(self, analytics_instance, mock_redis):
        """Test getting message volume over time"""
        volume = await analytics_instance.get_message_volume(hours=24)

        assert isinstance(volume, list)
        # Should have buckets for 24 hours
        assert len(volume) > 0


# =============================================================================
# AuditEvent Model Tests
# =============================================================================


class TestAuditEventModel:
    """Tests for AuditEvent model"""

    def test_create_event(self):
        """Test creating audit event"""
        event = AuditEvent(
            id="test-123",
            event_type=AuditEventType.AGENT_REGISTERED,
            actor_id="admin",
            target_id="test-agent",
        )

        assert event.id == "test-123"
        assert event.event_type == AuditEventType.AGENT_REGISTERED
        assert event.level == AuditLevel.INFO
        assert event.actor_id == "admin"
        assert event.target_id == "test-agent"

    def test_event_with_details(self):
        """Test event with details"""
        event = AuditEvent(
            id="test-456",
            event_type=AuditEventType.MESSAGE_FAILED,
            level=AuditLevel.ERROR,
            details={"error": "Connection timeout", "retry_count": 3},
        )

        assert event.level == AuditLevel.ERROR
        assert event.details["error"] == "Connection timeout"
        assert event.details["retry_count"] == 3

    def test_event_serialization(self):
        """Test event serialization"""
        event = AuditEvent(
            id="test-789",
            event_type=AuditEventType.SUBNET_CREATED,
            subnet_id="team-a",
            details={"name": "Team A Network"},
        )

        data = event.model_dump()

        assert data["id"] == "test-789"
        assert data["event_type"] == "subnet_created"
        assert data["subnet_id"] == "team-a"


# =============================================================================
# Integration Tests
# =============================================================================


class TestMonitoringIntegration:
    """Integration tests for monitoring components"""

    @pytest.mark.asyncio
    async def test_metrics_audit_integration(self, mock_redis):
        """Test metrics and audit working together"""
        metrics = MetricsCollector(mock_redis)
        audit = AuditLogger(mock_redis)

        await metrics.start()
        await audit.start()

        # Simulate agent registration
        await metrics.inc_registration_count("public")
        await audit.log_agent_registered("test-agent", "public", ["code"])

        # Verify both recorded
        reg_count = await metrics.get_counter("registrations_total", labels={"subnet": "public"})
        assert reg_count == 1

        events = await audit.query_events(event_type=AuditEventType.AGENT_REGISTERED)
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_full_message_flow_monitoring(self, mock_redis):
        """Test monitoring a full message flow"""
        metrics = MetricsCollector(mock_redis)
        audit = AuditLogger(mock_redis)
        analytics = Analytics(mock_redis)

        await metrics.start()
        await audit.start()

        # Simulate message sending
        await metrics.inc_message_count("agent-a", "agent-b", "success")
        await metrics.observe_latency("route_message", 0.015)
        await audit.log_message_sent("agent-a", "agent-b", "msg-123")

        # Check metrics
        msg_stats = await analytics.get_message_stats()
        # Note: The mock doesn't fully support the query patterns used in analytics
        # This is a simplified check
        assert "total" in msg_stats

        # Check audit
        events = await audit.query_events(event_type=AuditEventType.MESSAGE_SENT)
        assert len(events) == 1
