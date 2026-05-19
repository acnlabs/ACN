"""RedisSubnetJoinRequestRepository regressions (ADR-0004 Slice 2.1).

Tests split into two layers:

1. **Lua call contract** — verified with mock (KEYS/ARGV order,
   discriminator-driven KEYS list size). fakeredis 2.x doesn't ship
   Lua interpreter by default (would need the ``lupa`` extra,
   adding a C-extension dependency we'd rather avoid in CI). Mock
   is the same pattern ``test_task_repository_all_participations_cap``
   uses for ``LUA_JOIN_TASK`` — pins the call contract without
   requiring an actual Lua runtime.

2. **Read paths** (``find_pending_for``, ``list_by_subnet``) —
   verified against fakeredis (in-process) to exercise the actual
   Redis key layout end-to-end. The HASH + reverse-index +
   listing-SET layout is the most error-prone part of the
   implementation; mock tests would only verify call shape, not
   the layout's read coherence.

What gets pinned
----------------
- ``CREATE_PENDING_LUA`` script registration is lazy
  (``register_script`` not called until first ``save``).
- The KEYS list for ``CREATE_PENDING_LUA`` is exactly the 4-key
  layout ADR §"Redis layout and atomicity" specifies, in the
  documented order.
- The KEYS list for ``DECIDE_LUA`` is the 3-key layout.
- ARGV[2] is the discriminator the Lua script uses to decide
  whether to SADD the per-agent invitation set.
- ``existing`` outcome with a DIFFERENT request_id raises
  ``SubnetJoinRequestPendingError`` (THE collision contract).
- ``existing`` outcome with the SAME request_id is a no-op
  (idempotent re-save — safe for transient-failure retry).
- ``find_pending_for`` correctly dereferences the reverse index
  to its HASH.
- ``find_pending_for`` warns + returns None on a dangling
  reverse-index pointer (defence against the deadlock state
  the Lua envelope is supposed to prevent).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fakeredis import aioredis as fakeredis_async

from acn.core.entities import SubnetJoinRequest
from acn.infrastructure.persistence.postgres.subnet_join_request_repository import (
    SubnetJoinRequestPendingError,
)
from acn.infrastructure.persistence.redis.subnet_join_request_repository import (
    CREATE_PENDING_LUA,
    DECIDE_LUA,
    RedisSubnetJoinRequestRepository,
    _agent_invitations_key,
    _invitation_set_member,
    _pending_by_agent_key,
    _request_hash_key,
    _subnet_listing_key,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def fake_redis():
    """Per-test fakeredis. ``decode_responses=False`` matches the
    production composition (registry uses bytes-mode); the
    ``_decode`` / ``_normalize_hash`` helpers in the repo bridge
    bytes ↔ str transparently."""
    client = fakeredis_async.FakeRedis(decode_responses=False)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


def _make_request(**overrides) -> SubnetJoinRequest:
    defaults: dict = {
        "request_id": "req-1",
        "subnet_id": "s-1",
        "agent_id": "a-1",
        "kind": "join_request",
        "status": "pending",
        "initiated_by": "a-1",
    }
    defaults.update(overrides)
    return SubnetJoinRequest(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Lua call contract (mock-based)
# ---------------------------------------------------------------------------


class TestLuaCallContract:
    @pytest.mark.asyncio
    async def test_create_pending_keys_layout_is_the_documented_four(self):
        """KEYS[1..4] must be exactly the 4-key layout ADR §"Redis
        layout and atomicity" specifies, in the documented order.
        Swap them and the Lua script writes to the wrong place
        without any test or runtime error."""
        captured: dict = {}

        async def fake_script(*, keys, args, client):
            captured["keys"] = keys
            captured["args"] = args
            return [b"created", args[0].encode()]

        mock_redis = MagicMock()
        # ``register_script`` returns a callable; we replace it with
        # ``fake_script`` so the Lua never actually runs.
        mock_redis.register_script = MagicMock(return_value=fake_script)

        repo = RedisSubnetJoinRequestRepository(mock_redis)
        req = _make_request(subnet_id="sub-A", agent_id="agt-B", request_id="r-1")
        await repo.save(req)

        assert captured["keys"] == [
            _pending_by_agent_key("sub-A", "agt-B"),
            _request_hash_key("sub-A", "r-1"),
            _subnet_listing_key("sub-A"),
            _agent_invitations_key("agt-B"),
        ]
        # ARGV[1] = request_id, ARGV[2] = kind discriminator.
        assert captured["args"][0] == "r-1"
        assert captured["args"][1] == "join_request"

    @pytest.mark.asyncio
    async def test_create_pending_argv_kind_drives_invitation_set_sadd(self):
        """ARGV[2]='invitation' is the discriminator the Lua script
        uses to conditionally SADD ``acn:agents:{a}:subnet_invitations``.
        Pin the discriminator passing so a refactor that swaps the
        position of ``kind`` in ARGV can't silently break invitee-
        facing listings."""
        captured: dict = {}

        async def fake_script(*, keys, args, client):
            captured["args"] = args
            return [b"created", args[0].encode()]

        mock_redis = MagicMock()
        mock_redis.register_script = MagicMock(return_value=fake_script)

        repo = RedisSubnetJoinRequestRepository(mock_redis)
        await repo.save(_make_request(kind="invitation"))
        assert captured["args"][1] == "invitation"

    @pytest.mark.asyncio
    async def test_create_pending_argv3_is_invitation_composite_key(self):
        """ARGV[3] is the ``subnet_id:request_id`` composite key
        the Lua script SADDs into the per-agent invitations SET.
        The composite layout is the B1 fix — bare request_id storage
        would force ``list_pending_invitations_for_agent`` into an
        O(N·S) full-keyspace scan to find each invitation's subnet."""
        captured: dict = {}

        async def fake_script(*, keys, args, client):
            captured["args"] = args
            return [b"created", args[0].encode()]

        mock_redis = MagicMock()
        mock_redis.register_script = MagicMock(return_value=fake_script)

        repo = RedisSubnetJoinRequestRepository(mock_redis)
        await repo.save(
            _make_request(
                subnet_id="sub-A",
                request_id="r-1",
                kind="invitation",
            )
        )
        assert captured["args"][2] == _invitation_set_member("sub-A", "r-1")
        assert captured["args"][2] == "sub-A:r-1"

    @pytest.mark.asyncio
    async def test_create_pending_exists_with_different_id_raises(self):
        """``exists`` outcome + different request_id → the partial-
        index collision. THE 409 surface. If this gets swallowed
        every duplicate join attempt looks like a success and the
        unique-pending invariant evaporates."""

        async def fake_script(*, keys, args, client):
            return [b"exists", b"different-req-id"]

        mock_redis = MagicMock()
        mock_redis.register_script = MagicMock(return_value=fake_script)

        repo = RedisSubnetJoinRequestRepository(mock_redis)
        with pytest.raises(SubnetJoinRequestPendingError) as exc_info:
            await repo.save(_make_request(request_id="my-req-id"))
        assert exc_info.value.subnet_id == "s-1"
        assert exc_info.value.agent_id == "a-1"

    @pytest.mark.asyncio
    async def test_create_pending_exists_with_same_id_is_noop(self):
        """``exists`` outcome + SAME request_id is an idempotent
        re-save (the service layer may safely retry create on
        transient network failures). Must NOT raise."""

        async def fake_script(*, keys, args, client):
            return [b"exists", args[0].encode()]  # same request_id

        mock_redis = MagicMock()
        mock_redis.register_script = MagicMock(return_value=fake_script)

        repo = RedisSubnetJoinRequestRepository(mock_redis)
        # Should not raise.
        await repo.save(_make_request())

    @pytest.mark.asyncio
    async def test_decide_uses_three_key_layout(self):
        """Transition path uses a different KEYS layout (no listing
        SET — that membership persists across the state change).
        Pin the 3-key layout in documented order."""
        captured: dict = {}

        async def fake_script(*, keys, args, client):
            captured["keys"] = keys
            captured["args"] = args
            return 1

        mock_redis = MagicMock()
        mock_redis.register_script = MagicMock(return_value=fake_script)

        repo = RedisSubnetJoinRequestRepository(mock_redis)
        ts = datetime.now(UTC)
        await repo.save(
            _make_request(
                kind="invitation",
                status="approved",
                decided_by="a-1",
                decided_at=ts,
            )
        )
        assert captured["keys"] == [
            _pending_by_agent_key("s-1", "a-1"),
            _request_hash_key("s-1", "req-1"),
            _agent_invitations_key("a-1"),
        ]

    def test_lua_scripts_are_module_level_constants(self):
        """``CREATE_PENDING_LUA`` and ``DECIDE_LUA`` must be exported
        so operator tooling (script SHA inspection, manual replay)
        can import them. Pin the exports so a future refactor that
        inlines them can't break the operator interface."""
        assert isinstance(CREATE_PENDING_LUA, str)
        assert isinstance(DECIDE_LUA, str)
        assert "redis.call" in CREATE_PENDING_LUA
        assert "redis.call" in DECIDE_LUA


# ---------------------------------------------------------------------------
# Read paths against real fakeredis
# ---------------------------------------------------------------------------


class TestReadPathsAgainstFakeRedis:
    @pytest.mark.asyncio
    async def test_find_pending_for_dereferences_reverse_index(
        self, fake_redis
    ):
        """Seed the reverse index + HASH manually (bypassing the Lua
        save path so we don't need lupa), then verify the read
        composes correctly. Verifies the layout contract without
        needing the Lua interpreter."""
        repo = RedisSubnetJoinRequestRepository(fake_redis)
        req = _make_request(
            subnet_id="s-x", agent_id="a-x", request_id="r-x"
        )

        await fake_redis.set(_pending_by_agent_key("s-x", "a-x"), b"r-x")
        await fake_redis.hset(  # type: ignore[misc]
            _request_hash_key("s-x", "r-x"),
            mapping=req.to_dict(),
        )

        found = await repo.find_pending_for("s-x", "a-x")
        assert found is not None
        assert found.request_id == "r-x"
        assert found.subnet_id == "s-x"
        assert found.is_pending is True

    @pytest.mark.asyncio
    async def test_find_pending_for_returns_none_when_no_reverse_index(
        self, fake_redis
    ):
        repo = RedisSubnetJoinRequestRepository(fake_redis)
        assert await repo.find_pending_for("ghost", "ghost") is None

    @pytest.mark.asyncio
    async def test_find_pending_for_warns_on_dangling_reverse_index(
        self, fake_redis, caplog
    ):
        """Reverse index points at a non-existent HASH — the very
        deadlock the Lua envelope prevents in healthy deployments.
        Defensive contract: log warning, return None (so the caller
        sees "no pending" rather than a 500). Without this guard a
        single corrupted reverse-index entry would silently brick
        the agent's ability to ever request join for that subnet."""
        repo = RedisSubnetJoinRequestRepository(fake_redis)
        await fake_redis.set(
            _pending_by_agent_key("s-x", "a-x"), b"r-missing"
        )
        # NO HSET — HASH doesn't exist.

        import logging
        caplog.set_level(logging.WARNING)
        found = await repo.find_pending_for("s-x", "a-x")
        assert found is None
        assert any(
            "dangling reverse index" in rec.message for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_list_pending_invitations_dereferences_composite_keys(
        self, fake_redis
    ):
        """End-to-end fakeredis: invitee SET stores
        ``subnet_id:request_id`` composite keys; ``list_pending_*``
        deref's each directly to its subnet HASH without any
        keyspace scan. Pins the B1 perf fix at the integration level
        — not just the Lua call shape."""
        repo = RedisSubnetJoinRequestRepository(fake_redis)
        # Seed two invitations for agent-X across different subnets.
        ts = datetime.now(UTC)
        for sub_idx in [1, 2]:
            req = SubnetJoinRequest(
                request_id=f"r-{sub_idx}",
                subnet_id=f"s-{sub_idx}",
                agent_id="agent-X",
                kind="invitation",
                status="pending",
                initiated_by="owner-1",
                created_at=ts,
            )
            await fake_redis.hset(  # type: ignore[misc]
                _request_hash_key(f"s-{sub_idx}", f"r-{sub_idx}"),
                mapping=req.to_dict(),
            )
            await fake_redis.sadd(  # type: ignore[misc]
                _agent_invitations_key("agent-X"),
                _invitation_set_member(f"s-{sub_idx}", f"r-{sub_idx}"),
            )

        rows = await repo.list_pending_invitations_for_agent("agent-X")
        assert len(rows) == 2
        assert {r.subnet_id for r in rows} == {"s-1", "s-2"}

    @pytest.mark.asyncio
    async def test_list_pending_invitations_skips_legacy_bare_rid_members(
        self, fake_redis
    ):
        """Defensive path: a stale Slice 2.1 v1 SET member (bare
        request_id without ``subnet_id:`` prefix) is silently
        skipped rather than triggering the old expensive scan or
        raising a parse error."""
        repo = RedisSubnetJoinRequestRepository(fake_redis)
        await fake_redis.sadd(  # type: ignore[misc]
            _agent_invitations_key("agent-X"),
            b"legacy-bare-rid",
        )
        rows = await repo.list_pending_invitations_for_agent("agent-X")
        assert rows == []

    @pytest.mark.asyncio
    async def test_list_by_subnet_filters_by_kind_and_status(
        self, fake_redis
    ):
        repo = RedisSubnetJoinRequestRepository(fake_redis)

        # Seed: 3 rows on s-1 (2 join_requests, 1 invitation).
        for i, (kind, status) in enumerate(
            [
                ("join_request", "pending"),
                ("join_request", "rejected"),
                ("invitation", "pending"),
            ]
        ):
            rid = f"r-{i}"
            ts = datetime.now(UTC)
            kwargs: dict = {
                "request_id": rid,
                "subnet_id": "s-1",
                "agent_id": f"a-{i}",
                "kind": kind,
                "status": status,
                "initiated_by": f"a-{i}",
            }
            if status != "pending":
                kwargs["decided_by"] = "owner-1"
                kwargs["decided_at"] = ts
            req = SubnetJoinRequest(**kwargs)  # type: ignore[arg-type]
            await fake_redis.sadd(_subnet_listing_key("s-1"), rid.encode())  # type: ignore[misc]
            await fake_redis.hset(  # type: ignore[misc]
                _request_hash_key("s-1", rid),
                mapping=req.to_dict(),
            )

        only_join = await repo.list_by_subnet("s-1", kind="join_request")
        assert len(only_join) == 2
        assert all(r.kind == "join_request" for r in only_join)

        only_pending = await repo.list_by_subnet("s-1", status="pending")
        assert len(only_pending) == 2
        assert all(r.is_pending for r in only_pending)

        only_invitation_pending = await repo.list_by_subnet(
            "s-1", kind="invitation", status="pending"
        )
        assert len(only_invitation_pending) == 1
        assert only_invitation_pending[0].kind == "invitation"
