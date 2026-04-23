"""Regression tests for P1-2: MetricsCollector must not expose high-cardinality
label traps.

Background
----------
Every unique (metric_name, label_values) tuple becomes a permanent Redis
key because counters have no TTL. The old schema keyed `acn_messages_total`
on `(from_agent, to_agent, status)` — at scale that's O(active_pairs)
dead storage. We now:

- pin the schema: only declared label keys survive
- drop `from_agent`/`to_agent` entirely from `acn_messages_total`
- sanitize length & charset so one bad caller can't mint a 100KB key
"""

from unittest.mock import AsyncMock

import pytest

from acn.monitoring.metrics import (
    _LABEL_VALUE_SAFE,
    _MAX_LABEL_VALUE_LEN,
    MetricsCollector,
)


@pytest.fixture
def metrics() -> tuple[MetricsCollector, AsyncMock]:
    fake_redis = AsyncMock()
    return MetricsCollector(fake_redis), fake_redis


@pytest.mark.asyncio
async def test_inc_message_count_ignores_agent_ids(metrics):
    """Whatever caller passes as from/to, the stored Redis key must
    contain neither — only `status=...` — so cardinality stays bounded.
    """
    m, fake_redis = metrics

    await m.inc_message_count(from_agent="agent-a", to_agent="agent-b")
    await m.inc_message_count(from_agent="agent-c", to_agent="agent-d")

    keys = [c.args[0] for c in fake_redis.incr.await_args_list]
    # Two calls, but they should collapse to the same single key —
    # that's the whole point of dropping the agent dimensions.
    assert len(set(keys)) == 1, f"cardinality leak: {keys}"
    key = keys[0]
    assert "from_agent=" not in key
    assert "to_agent=" not in key
    assert "status=success" in key


@pytest.mark.asyncio
async def test_messages_total_schema_has_no_agent_labels():
    """If the schema ever regains agent-id labels, this test will fire
    before a prod deploy can leak GB of counter keys.
    """
    meta = MetricsCollector.METRICS["acn_messages_total"]
    assert "from_agent" not in meta["labels"]
    assert "to_agent" not in meta["labels"]
    assert meta["labels"] == ["status"]


@pytest.mark.asyncio
async def test_sanitize_labels_drops_unknown_keys(metrics):
    """Declared-schema metrics reject labels the schema doesn't know
    about. This protects us from well-meaning callers stuffing a user
    id into e.g. `acn_errors_total`.
    """
    m, fake_redis = metrics

    await m.inc_counter(
        "errors_total",
        labels={"type": "timeout", "component": "router", "user_id": "u-123"},
    )

    key = fake_redis.incr.await_args.args[0]
    assert "type=timeout" in key
    assert "component=router" in key
    assert "user_id" not in key, f"unknown label leaked into key: {key}"


@pytest.mark.asyncio
async def test_sanitize_labels_caps_value_length(metrics):
    m, fake_redis = metrics

    giant = "x" * (_MAX_LABEL_VALUE_LEN + 500)
    await m.inc_counter("errors_total", labels={"type": giant, "component": "x"})

    key = fake_redis.incr.await_args.args[0]
    assert giant not in key
    assert "type=_overflow_" in key


@pytest.mark.asyncio
async def test_sanitize_labels_normalizes_delimiters(metrics):
    """`:` and `=` are key delimiters in `_build_key` / `_parse_key`.
    Letting raw values through would corrupt round-trips on export.
    """
    m, fake_redis = metrics

    await m.inc_counter(
        "errors_total", labels={"type": "a:b=c d", "component": "x"}
    )

    key = fake_redis.incr.await_args.args[0]
    suffix = key.split(":", 2)[-1]  # drop the "acn:metrics:" prefix
    # Only one `=` per label (the separator we write), no stray `:` or ` `.
    type_part = [p for p in suffix.split(":") if p.startswith("type=")][0]
    value = type_part.split("=", 1)[1]
    assert "=" not in value
    assert ":" not in value
    assert " " not in value
    # Safe pattern: the regex shouldn't match anything after sanitize.
    assert _LABEL_VALUE_SAFE.search(value) is None


@pytest.mark.asyncio
async def test_adhoc_counters_still_work_but_are_guarded(metrics):
    """A counter name not in `METRICS` skips the whitelist (so existing
    ad-hoc usage doesn't break) but must still get charset/length guards.
    """
    m, fake_redis = metrics

    await m.inc_counter("adhoc_thing", labels={"anything": "x" * 200})

    key = fake_redis.incr.await_args.args[0]
    assert "x" * 200 not in key
    assert "anything=_overflow_" in key


@pytest.mark.asyncio
async def test_read_path_uses_same_sanitize(metrics):
    """If readers don't sanitize, a caller that passes `"a:b=c"` writes
    under a sanitized key but reads under the raw key → always 0.
    This test pins both paths in lock-step.
    """
    m, fake_redis = metrics

    await m.inc_counter(
        "errors_total", labels={"type": "a:b=c", "component": "router"}
    )
    write_key = fake_redis.incr.await_args.args[0]

    fake_redis.get.return_value = b"7"
    await m.get_counter(
        "errors_total", labels={"type": "a:b=c", "component": "router"}
    )
    read_key = fake_redis.get.await_args.args[0]

    assert read_key == write_key


@pytest.mark.asyncio
async def test_histogram_observe_sanitizes_operation_label(metrics):
    m, fake_redis = metrics

    await m.observe_latency("route:msg=1", 0.1)

    # The lpush target is `{sanitized_key}:values`, so `:values` is
    # always the tail. We want to inspect only the `operation=...`
    # label segment, which sits between `operation=` and the next `:`.
    list_key = fake_redis.lpush.await_args.args[0]
    after_operation = list_key.split("operation=", 1)[1]
    label_value = after_operation.split(":", 1)[0]
    assert ":" not in label_value
    assert "=" not in label_value
    assert " " not in label_value


@pytest.mark.asyncio
async def test_gauge_set_also_sanitizes(metrics):
    """The sanitize pass must run on every label entry point, not just
    inc_counter, or `set_gauge` becomes the new back door.
    """
    m, fake_redis = metrics

    await m.set_gauge(
        "agents_registered",
        42,
        labels={"subnet": "public", "status": "active", "region": "us"},
    )

    key = fake_redis.set.await_args.args[0]
    assert "region=" not in key, f"unknown label leaked via set_gauge: {key}"
