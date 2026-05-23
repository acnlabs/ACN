"""Unit tests for ``scripts/backfill_subnet_join_policy.py`` (ADR-0004 Phase 1).

Pins the four observable states ``_backfill_one`` produces and the
idempotency contract its sentinel field guarantees:

1. ``missing``    — HASH disappeared between SCAN and HGETALL.
2. ``already_done`` — sentinel ``backfill_v0004=done`` already present;
   no read / no write of ``join_policy``.
3. ``already_set``  — ``join_policy`` already populated (saved through
   the entity path after the field landed) but sentinel missing;
   sentinel gets written, ``join_policy`` left untouched.
4. ``updated``      — ``join_policy`` missing; written for the first
   time. Value is ``"approval"`` when ``is_private == "True"``
   (matches Alembic backfill semantic), else ``"open"``.

Also pins the dual bytes/str decode path the script uses to read
keys: a ``decode_responses=False`` client returns ``dict[bytes,
bytes]`` from ``hgetall``; the script reads through ``_decode`` so
both shapes behave identically.

These tests avoid ``fakeredis`` (optional dependency, not always
installed) and stub the Redis client via ``AsyncMock`` — the same
pattern ``test_subnet_repository_redis_join_policy.py`` uses.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def backfill_module():
    """Load ``scripts/backfill_subnet_join_policy.py`` as a module.

    ``scripts/`` is not a package; we use ``spec_from_file_location``
    so the test stays robust against future scripts-dir layout
    changes (mirrors how
    ``test_alembic_subnet_join_policy_migration.py`` loads the
    Alembic revision).
    """
    repo_root = Path(__file__).resolve().parents[2]
    script_path = repo_root / "scripts" / "backfill_subnet_join_policy.py"
    spec = importlib.util.spec_from_file_location(
        "_test_backfill_subnet_join_policy", script_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# _backfill_one — the four observable outcomes
# ---------------------------------------------------------------------------


class TestBackfillOne:
    @pytest.mark.asyncio
    async def test_missing_hash_returns_missing(self, backfill_module):
        """Hash disappeared between SCAN and HGETALL (concurrent
        delete). The script must not write anything and must report
        ``missing`` so the operator can see it in the summary."""
        redis = AsyncMock()
        redis.hgetall.return_value = {}

        status, value = await backfill_module._backfill_one(
            redis, "acn:subnets:info:ghost"
        )

        assert status == "missing"
        assert value is None
        redis.hset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sentinel_present_returns_already_done(self, backfill_module):
        """Idempotency contract: a row that already carries
        ``backfill_v0004=done`` is short-circuited without re-reading
        or re-writing ``join_policy``. The operator's repeat-run cost
        is one HGETALL, period."""
        redis = AsyncMock()
        redis.hgetall.return_value = {
            "slug": "subnet-1",
            "is_private": "True",
            "join_policy": "approval",
            "backfill_v0004": "done",
        }

        status, value = await backfill_module._backfill_one(
            redis, "acn:subnets:info:subnet-1"
        )

        assert status == "already_done"
        assert value is None
        redis.hset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_existing_join_policy_writes_only_sentinel(self, backfill_module):
        """The row has ``join_policy`` (a normal ``save()`` after the
        field landed) but no sentinel. The script must NOT overwrite
        ``join_policy`` — only write the sentinel so future passes
        short-circuit. Overwriting would clobber a non-default value
        a user just set."""
        redis = AsyncMock()
        redis.hgetall.return_value = {
            "slug": "subnet-2",
            "is_private": "False",
            "join_policy": "approval",
            # no sentinel
        }

        status, value = await backfill_module._backfill_one(
            redis, "acn:subnets:info:subnet-2"
        )

        assert status == "already_set"
        assert value == "approval"
        redis.hset.assert_awaited_once()
        _, kwargs = redis.hset.await_args
        mapping = kwargs["mapping"]
        # Sentinel field must be present.
        assert mapping == {"backfill_v0004": "done"}
        # ``join_policy`` MUST NOT appear in the mapping — otherwise
        # the script would overwrite a value the user just set.
        assert "join_policy" not in mapping

    @pytest.mark.asyncio
    async def test_missing_field_private_row_writes_approval(self, backfill_module):
        """Legacy private row predating ADR-0004 — missing
        ``join_policy``, ``is_private == "True"``. The script must
        write ``join_policy='approval'`` (matches the Alembic
        ``UPDATE ... WHERE is_private=true`` semantic) plus the
        sentinel, in a single HSET."""
        redis = AsyncMock()
        redis.hgetall.return_value = {
            "slug": "subnet-priv-legacy",
            "is_private": "True",
            # no join_policy, no sentinel
        }

        status, value = await backfill_module._backfill_one(
            redis, "acn:subnets:info:subnet-priv-legacy"
        )

        assert status == "updated"
        assert value == "approval"
        redis.hset.assert_awaited_once()
        _, kwargs = redis.hset.await_args
        mapping = kwargs["mapping"]
        assert mapping == {
            "join_policy": "approval",
            "backfill_v0004": "done",
        }

    @pytest.mark.asyncio
    async def test_missing_field_public_row_writes_open(self, backfill_module):
        """Legacy public row predating ADR-0004 — missing
        ``join_policy``, ``is_private`` not "True". The script
        writes ``'open'`` (the entity default) so the stored
        representation matches what ``_dict_to_subnet`` would
        already auto-infer on read."""
        redis = AsyncMock()
        redis.hgetall.return_value = {
            "slug": "subnet-pub-legacy",
            "is_private": "False",
        }

        status, value = await backfill_module._backfill_one(
            redis, "acn:subnets:info:subnet-pub-legacy"
        )

        assert status == "updated"
        assert value == "open"
        _, kwargs = redis.hset.await_args
        assert kwargs["mapping"] == {
            "join_policy": "open",
            "backfill_v0004": "done",
        }

    @pytest.mark.asyncio
    async def test_missing_field_missing_is_private_writes_open(self, backfill_module):
        """Pathological legacy row — no ``is_private`` key at all.
        ``_backfill_one`` must treat that as ``False`` (the entity
        default) and write ``join_policy='open'`` rather than
        crashing or writing ``'approval'`` (which would over-restrict
        a row that was never marked private)."""
        redis = AsyncMock()
        redis.hgetall.return_value = {"slug": "subnet-pathological"}

        status, value = await backfill_module._backfill_one(
            redis, "acn:subnets:info:subnet-pathological"
        )

        assert status == "updated"
        assert value == "open"


# ---------------------------------------------------------------------------
# Dual bytes/str decode path
# ---------------------------------------------------------------------------


class TestDualDecode:
    """The script connects via ``aioredis.from_url(REDIS_URL)``
    without ``decode_responses=True`` (line 135 of the script), so
    ``hgetall`` returns ``dict[bytes, bytes]``. The script's
    ``_decode`` helper and dual-key ``raw.get(b"...") or
    raw.get("...")`` pattern must handle that shape exactly the
    same way as ``dict[str, str]``."""

    @pytest.mark.asyncio
    async def test_bytes_keys_and_values_decoded_correctly(self, backfill_module):
        redis = AsyncMock()
        redis.hgetall.return_value = {
            b"slug": b"subnet-bytes",
            b"is_private": b"True",
            # no join_policy, no sentinel
        }

        status, value = await backfill_module._backfill_one(
            redis, "acn:subnets:info:subnet-bytes"
        )

        assert status == "updated"
        assert value == "approval"

    @pytest.mark.asyncio
    async def test_bytes_sentinel_is_recognised(self, backfill_module):
        """Sentinel comparison must work whether the value comes back
        as ``bytes`` or ``str``."""
        redis = AsyncMock()
        redis.hgetall.return_value = {
            b"slug": b"subnet-bytes-done",
            b"is_private": b"True",
            b"join_policy": b"approval",
            b"backfill_v0004": b"done",
        }

        status, _ = await backfill_module._backfill_one(
            redis, "acn:subnets:info:subnet-bytes-done"
        )

        assert status == "already_done"
        redis.hset.assert_not_awaited()


# ---------------------------------------------------------------------------
# _decode helper
# ---------------------------------------------------------------------------


class TestDecodeHelper:
    """``_decode`` is the bottom-of-the-stack primitive. Pin its
    three input shapes explicitly so future refactors don't drop
    one silently."""

    def test_decode_none_returns_none(self, backfill_module):
        assert backfill_module._decode(None) is None

    def test_decode_bytes_returns_str(self, backfill_module):
        assert backfill_module._decode(b"approval") == "approval"

    def test_decode_str_returns_unchanged(self, backfill_module):
        assert backfill_module._decode("approval") == "approval"
