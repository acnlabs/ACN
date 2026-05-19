"""Shared Redis HASH/value decoding helpers.

Both ``RedisSubnetJoinRequestRepository`` and
``RedisSubnetAllowlistRepository`` (and any future Redis repo)
need to be tolerant of the client's ``decode_responses`` setting:
production registry composition uses ``decode_responses=False``
(bytes-mode) but backfill scripts and ad-hoc tooling sometimes
construct a client without that flag. Without normalisation, every
``data.get("field")`` silently misses when the dict comes back
byte-keyed, corrupting parsing in ways that are very hard to
debug.

This module is the consolidated home for the two helpers — same
shape as ``RedisSubnetRepository._normalize_redis_dict``, factored
out so multiple repos can share it instead of each carrying its
own copy (review fix N2).
"""

from __future__ import annotations


def decode_value(value: object) -> str:
    """Coerce bytes / str / None to ``str`` (empty for ``None``).

    The empty-string-for-None convention matches the empty-string-
    is-NULL serialisation discipline ``SubnetJoinRequest.to_dict``
    (and friends) use for Redis HASH storage.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def normalize_hash(raw: object) -> dict[str, str]:
    """Coerce a Redis HASH dict to ``dict[str, str]`` regardless of
    the client's ``decode_responses`` flag.

    Returns ``{}`` for falsy input (matches ``HGETALL`` on a
    missing HASH — redis-py returns an empty dict, not ``None``)."""
    if not raw or not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = k.decode() if isinstance(k, bytes) else str(k)
        val = v.decode() if isinstance(v, bytes) else v
        out[key] = val
    return out
