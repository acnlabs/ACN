"""
ACN Metrics Collector

Collects and exposes metrics for monitoring ACN performance.
Supports Prometheus format for integration with Grafana dashboards.

Metrics collected:
- Agent counts (registered, active, by subnet)
- Message counts (sent, received, failed)
- Latency (message routing, API response)
- Subnet statistics
- Gateway statistics

Architecture:
    ┌──────────────────────────────────────────────────────┐
    │                  MetricsCollector                     │
    │                                                        │
    │  Counters:                    Gauges:                 │
    │  ├─ acn_messages_total       ├─ acn_agents_active    │
    │  ├─ acn_registrations_total  ├─ acn_subnets_total    │
    │  └─ acn_errors_total         └─ acn_connections      │
    │                                                        │
    │  Histograms:                  Summary:                │
    │  └─ acn_latency_seconds      └─ acn_message_size     │
    └──────────────────────────────────────────────────────┘
"""

import time
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from redis.asyncio import Redis


class MetricType(str, Enum):
    """Types of metrics"""

    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


class MetricsCollector:
    """
    Collects and manages ACN metrics.

    Uses Redis for distributed metric storage and supports Prometheus export.

    Example:
        metrics = MetricsCollector(redis_client)
        await metrics.start()

        # Increment counters
        await metrics.inc_counter("messages_sent", labels={"from": "agent-a"})

        # Set gauges
        await metrics.set_gauge("active_agents", 42)

        # Record latency
        await metrics.observe_latency("route_message", 0.015)

        # Get Prometheus output
        output = await metrics.prometheus_export()
    """

    # Metric definitions
    METRICS = {
        # Counters
        "acn_messages_total": {
            "type": MetricType.COUNTER,
            "help": "Total number of A2A messages processed",
            "labels": ["from_agent", "to_agent", "status"],
        },
        "acn_registrations_total": {
            "type": MetricType.COUNTER,
            "help": "Total number of agent registrations",
            "labels": ["subnet"],
        },
        "acn_broadcasts_total": {
            "type": MetricType.COUNTER,
            "help": "Total number of broadcast messages",
            "labels": ["type"],
        },
        "acn_errors_total": {
            "type": MetricType.COUNTER,
            "help": "Total number of errors",
            "labels": ["type", "component"],
        },
        # Gauges
        "acn_agents_registered": {
            "type": MetricType.GAUGE,
            "help": "Number of registered agents",
            "labels": ["subnet", "status"],
        },
        "acn_subnets_total": {
            "type": MetricType.GAUGE,
            "help": "Number of active subnets",
            "labels": [],
        },
        "acn_gateway_connections": {
            "type": MetricType.GAUGE,
            "help": "Number of gateway WebSocket connections",
            "labels": ["subnet"],
        },
        "acn_websocket_connections": {
            "type": MetricType.GAUGE,
            "help": "Number of frontend WebSocket connections",
            "labels": ["channel"],
        },
        # Histograms
        "acn_latency_seconds": {
            "type": MetricType.HISTOGRAM,
            "help": "Latency in seconds",
            "labels": ["operation"],
            "buckets": [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
        },
        "acn_message_size_bytes": {
            "type": MetricType.HISTOGRAM,
            "help": "Message size in bytes",
            "labels": ["direction"],
            "buckets": [100, 500, 1000, 5000, 10000, 50000, 100000, 500000],
        },
    }

    def __init__(self, redis: Redis):
        """
        Initialize metrics collector.

        Args:
            redis: Redis client for distributed storage
        """
        self.redis = redis
        self._prefix = "acn:metrics:"
        self._started = False
        self._start_time = datetime.now(UTC)

    async def start(self):
        """Start the metrics collector"""
        self._started = True
        self._start_time = datetime.now(UTC)
        # Initialize all metrics to 0
        for name, meta in self.METRICS.items():
            if meta["type"] == MetricType.GAUGE and not meta["labels"]:
                await self.redis.set(f"{self._prefix}{name}", "0")

    async def stop(self):
        """Stop the metrics collector"""
        self._started = False

    # =========================================================================
    # Counter Operations
    # =========================================================================

    async def inc_counter(self, name: str, value: int = 1, labels: dict[str, str] | None = None):
        """
        Increment a counter metric.

        Args:
            name: Counter name (without acn_ prefix)
            value: Value to increment by (default 1)
            labels: Label values
        """
        full_name = f"acn_{name}" if not name.startswith("acn_") else name
        key = self._build_key(full_name, labels)
        await self.redis.incr(key, value)

    async def inc_message_count(self, from_agent: str, to_agent: str, status: str = "success"):
        """Convenience method to increment message counter"""
        await self.inc_counter(
            "messages_total",
            labels={"from_agent": from_agent, "to_agent": to_agent, "status": status},
        )

    async def inc_registration_count(self, subnet: str = "public"):
        """Convenience method to increment registration counter"""
        await self.inc_counter("registrations_total", labels={"subnet": subnet})

    async def inc_error_count(self, error_type: str, component: str):
        """Convenience method to increment error counter"""
        await self.inc_counter("errors_total", labels={"type": error_type, "component": component})

    # =========================================================================
    # Gauge Operations
    # =========================================================================

    async def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None):
        """
        Set a gauge metric value.

        Args:
            name: Gauge name (without acn_ prefix)
            value: Value to set
            labels: Label values
        """
        full_name = f"acn_{name}" if not name.startswith("acn_") else name
        key = self._build_key(full_name, labels)
        await self.redis.set(key, str(value))

    async def inc_gauge(self, name: str, value: float = 1, labels: dict[str, str] | None = None):
        """Increment a gauge value"""
        full_name = f"acn_{name}" if not name.startswith("acn_") else name
        key = self._build_key(full_name, labels)
        await self.redis.incrbyfloat(key, value)

    async def dec_gauge(self, name: str, value: float = 1, labels: dict[str, str] | None = None):
        """Decrement a gauge value"""
        full_name = f"acn_{name}" if not name.startswith("acn_") else name
        key = self._build_key(full_name, labels)
        await self.redis.incrbyfloat(key, -value)

    async def set_agent_count(self, count: int, subnet: str = "public", status: str = "active"):
        """Convenience method to set agent count"""
        await self.set_gauge(
            "agents_registered", count, labels={"subnet": subnet, "status": status}
        )

    async def set_subnet_count(self, count: int):
        """Convenience method to set subnet count"""
        await self.set_gauge("subnets_total", count)

    async def set_gateway_connections(self, count: int, subnet: str):
        """Convenience method to set gateway connection count"""
        await self.set_gauge("gateway_connections", count, labels={"subnet": subnet})

    async def set_websocket_connections(self, count: int, channel: str = "default"):
        """Convenience method to set websocket connection count"""
        await self.set_gauge("websocket_connections", count, labels={"channel": channel})

    # =========================================================================
    # Histogram Operations
    # =========================================================================

    async def observe_latency(self, operation: str, latency_seconds: float):
        """
        Record a latency observation.

        Args:
            operation: Operation name (e.g., "route_message", "register")
            latency_seconds: Latency in seconds
        """
        key = self._build_key("acn_latency_seconds", {"operation": operation})

        # Store in a Redis list for histogram calculation
        await self.redis.lpush(f"{key}:values", str(latency_seconds))
        await self.redis.ltrim(f"{key}:values", 0, 9999)  # Keep last 10000 values

        # Update sum and count for average calculation
        await self.redis.incrbyfloat(f"{key}:sum", latency_seconds)
        await self.redis.incr(f"{key}:count")

    async def observe_message_size(self, size_bytes: int, direction: str = "outgoing"):
        """
        Record a message size observation.

        Args:
            size_bytes: Message size in bytes
            direction: "incoming" or "outgoing"
        """
        key = self._build_key("acn_message_size_bytes", {"direction": direction})

        await self.redis.lpush(f"{key}:values", str(size_bytes))
        await self.redis.ltrim(f"{key}:values", 0, 9999)
        await self.redis.incrbyfloat(f"{key}:sum", size_bytes)
        await self.redis.incr(f"{key}:count")

    def timer(self, operation: str):
        """
        Context manager for timing operations.

        Usage:
            async with metrics.timer("route_message"):
                await do_routing()
        """
        return _Timer(self, operation)

    # =========================================================================
    # Query Operations
    # =========================================================================

    async def get_counter(self, name: str, labels: dict[str, str] | None = None) -> int:
        """Get counter value"""
        full_name = f"acn_{name}" if not name.startswith("acn_") else name
        key = self._build_key(full_name, labels)
        value = await self.redis.get(key)
        return int(value) if value else 0

    async def get_gauge(self, name: str, labels: dict[str, str] | None = None) -> float:
        """Get gauge value"""
        full_name = f"acn_{name}" if not name.startswith("acn_") else name
        key = self._build_key(full_name, labels)
        value = await self.redis.get(key)
        return float(value) if value else 0.0

    async def get_histogram_stats(
        self, name: str, labels: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """
        Get histogram statistics.

        Returns:
            Dict with count, sum, avg, min, max, percentiles
        """
        full_name = f"acn_{name}" if not name.startswith("acn_") else name
        key = self._build_key(full_name, labels)

        # Get values
        raw_values = await self.redis.lrange(f"{key}:values", 0, -1)
        values = [float(v) for v in raw_values] if raw_values else []

        count = await self.redis.get(f"{key}:count")
        total_sum = await self.redis.get(f"{key}:sum")

        if not values:
            return {
                "count": 0,
                "sum": 0,
                "avg": 0,
                "min": 0,
                "max": 0,
                "p50": 0,
                "p90": 0,
                "p99": 0,
            }

        sorted_values = sorted(values)

        return {
            "count": int(count) if count else len(values),
            "sum": float(total_sum) if total_sum else sum(values),
            "avg": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
            "p50": self._percentile(sorted_values, 50),
            "p90": self._percentile(sorted_values, 90),
            "p99": self._percentile(sorted_values, 99),
        }

    # =========================================================================
    # Prometheus Export
    # =========================================================================

    async def prometheus_export(self) -> str:
        """
        Export metrics in Prometheus text format.

        Returns:
            String in Prometheus exposition format
        """
        lines = []

        # Get all metric keys
        keys = await self.redis.keys(f"{self._prefix}*")

        metrics_data: dict[str, list[tuple[dict[str, str], str]]] = {}

        for key in keys:
            key_str = key.decode() if isinstance(key, bytes) else key

            # Skip histogram internal keys
            if ":values" in key_str or ":sum" in key_str or ":count" in key_str:
                continue

            # Parse metric name and labels
            metric_name, labels = self._parse_key(key_str)

            if metric_name not in metrics_data:
                metrics_data[metric_name] = []

            value = await self.redis.get(key)
            if value:
                metrics_data[metric_name].append(
                    (labels, value.decode() if isinstance(value, bytes) else str(value))
                )

        # Format output
        for metric_name in sorted(metrics_data.keys()):
            if metric_name in self.METRICS:
                meta = self.METRICS[metric_name]
                lines.append(f"# HELP {metric_name} {meta['help']}")
                lines.append(f"# TYPE {metric_name} {meta['type'].value}")

            for labels, value in metrics_data[metric_name]:
                if labels:
                    label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
                    lines.append(f"{metric_name}{{{label_str}}} {value}")
                else:
                    lines.append(f"{metric_name} {value}")

        # Add histogram data
        for metric_name, meta in self.METRICS.items():
            if meta["type"] == MetricType.HISTOGRAM:
                for label_combo in meta.get("labels", []):
                    stats = await self.get_histogram_stats(
                        metric_name, {label_combo: "*"} if label_combo else None
                    )
                    if stats["count"] > 0:
                        lines.append(f"# HELP {metric_name} {meta['help']}")
                        lines.append(f"# TYPE {metric_name} histogram")
                        lines.append(f"{metric_name}_count {stats['count']}")
                        lines.append(f"{metric_name}_sum {stats['sum']}")

        # Add uptime
        uptime_seconds = (datetime.now(UTC) - self._start_time).total_seconds()
        lines.append("# HELP acn_uptime_seconds ACN service uptime in seconds")
        lines.append("# TYPE acn_uptime_seconds gauge")
        lines.append(f"acn_uptime_seconds {uptime_seconds:.2f}")

        return "\n".join(lines)

    async def get_all_metrics(self) -> dict[str, Any]:
        """
        Get all metrics as a dictionary.

        Returns:
            Dictionary of all metrics with their values
        """
        result = {}

        for metric_name, meta in self.METRICS.items():
            if meta["type"] == MetricType.HISTOGRAM:
                result[metric_name] = await self.get_histogram_stats(metric_name)
            elif meta["labels"]:
                # Get all label combinations
                pattern = f"{self._prefix}{metric_name}:*"
                keys = await self.redis.keys(pattern)
                values = {}
                for key in keys:
                    key_str = key.decode() if isinstance(key, bytes) else key
                    _, labels = self._parse_key(key_str)
                    value = await self.redis.get(key)
                    if value:
                        label_key = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
                        values[label_key] = float(value)
                result[metric_name] = values
            else:
                result[metric_name] = await self.get_gauge(metric_name)

        # Add computed metrics
        uptime_seconds = (datetime.now(UTC) - self._start_time).total_seconds()
        result["acn_uptime_seconds"] = uptime_seconds

        return result

    # =========================================================================
    # Internal Helpers
    # =========================================================================

    def _build_key(self, name: str, labels: dict[str, str] | None) -> str:
        """Build Redis key from metric name and labels"""
        if not labels:
            return f"{self._prefix}{name}"

        label_str = ":".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{self._prefix}{name}:{label_str}"

    def _parse_key(self, key: str) -> tuple[str, dict[str, str]]:
        """Parse Redis key to extract metric name and labels"""
        key = key.replace(self._prefix, "")

        parts = key.split(":")
        metric_name = parts[0]
        labels = {}

        for part in parts[1:]:
            if "=" in part:
                k, v = part.split("=", 1)
                labels[k] = v

        return metric_name, labels

    @staticmethod
    def _percentile(sorted_values: list[float], percentile: int) -> float:
        """Calculate percentile from sorted values"""
        if not sorted_values:
            return 0.0
        k = (len(sorted_values) - 1) * percentile / 100
        f = int(k)
        c = f + 1 if f + 1 < len(sorted_values) else f
        return sorted_values[f] + (k - f) * (sorted_values[c] - sorted_values[f])


class _Timer:
    """Context manager for timing operations"""

    def __init__(self, metrics: MetricsCollector, operation: str):
        self.metrics = metrics
        self.operation = operation
        self.start_time: float = 0

    async def __aenter__(self):
        self.start_time = time.perf_counter()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        elapsed = time.perf_counter() - self.start_time
        await self.metrics.observe_latency(self.operation, elapsed)
        return False



































