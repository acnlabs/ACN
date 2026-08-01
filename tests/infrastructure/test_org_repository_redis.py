"""RedisOrgRepository regressions — fence-binding atomicity (ADR-0014).

Postgres enforces "one Org per subnet" with ``uq_orgs_subnet_id``; Redis
has no unique constraint, so ``save_org`` must claim the
``acn:orgs:by_subnet:{slug}`` pointer with ``SET NX``. These tests pin:

- fresh-create vs fresh-create on the same subnet → second save raises
  ``OrgSubnetBindingConflictError`` (the race the service pre-check
  cannot close);
- a **dissolved** holder releases the pointer and a new Org may rebind;
- a **dangling** pointer (org payload deleted out-of-band) is evicted;
- ``delete_org`` releases the pointer only when it still points at the
  deleted org (guarded release);
- basic round-trips (org / membership / work) against the real key
  layout, exercised end-to-end on fakeredis.

fakeredis runs in bytes mode by default; production composes the client
with ``decode_responses=True`` (see ``acn/api.py``), so the fixture
mirrors that.
"""

from __future__ import annotations

import pytest
from fakeredis import aioredis as fakeredis_async

from acn.core.entities.org import Org, OrgMembership, OrgPrincipal, OrgWorkItem
from acn.core.exceptions import OrgSubnetBindingConflictError
from acn.infrastructure.persistence.redis.org_repository import (
    RedisOrgRepository,
    _org_key,
    _org_subnet_idx,
    _work_key,
    _work_set_key,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def fake_redis():
    client = fakeredis_async.FakeRedis(decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()


@pytest.fixture
def repo(fake_redis):
    return RedisOrgRepository(fake_redis)


def _org(org_id: str, subnet_id: str, **overrides) -> Org:
    defaults = {
        "org_id": org_id,
        "display_name": f"Org {org_id}",
        "created_by": OrgPrincipal(kind="agent", subject="agt_steward"),
        "subnet_id": subnet_id,
        "steward_agent_id": "agt_steward",
    }
    defaults.update(overrides)
    return Org(**defaults)


# ---------------------------------------------------------------------------
# Fence-binding claim (SET NX)
# ---------------------------------------------------------------------------


async def test_second_org_on_same_subnet_conflicts(repo):
    await repo.save_org(_org("org_a", "fence-1"))
    with pytest.raises(OrgSubnetBindingConflictError) as ei:
        await repo.save_org(_org("org_b", "fence-1"))
    assert ei.value.subnet_id == "fence-1"
    assert ei.value.bound_org_id == "org_a"
    # Loser leaves no row behind: payload is written before the claim,
    # and deleted again when the claim is lost.
    assert await repo.find_org("org_b") is None
    stewarded = await repo.list_orgs_by_steward("agt_steward")
    assert [o.org_id for o in stewarded] == ["org_a"]


async def test_fresh_create_writes_payload_before_claim(repo, fake_redis):
    """Ordering contract (#177 review): payload BEFORE pointer claim.

    The dangling eviction treats "pointer without payload" as a dead
    binding. If a fresh create claimed the pointer first, a concurrent
    create could observe exactly that state, evict the pointer, and
    double-bind the subnet. Pinning the write order closes the race.
    """
    order: list[str] = []
    real_set = fake_redis.set

    async def spy_set(key, *args, **kwargs):
        order.append(key)
        return await real_set(key, *args, **kwargs)

    fake_redis.set = spy_set
    try:
        await repo.save_org(_org("org_a", "fence-1"))
    finally:
        fake_redis.set = real_set

    assert order.index(_org_key("org_a")) < order.index(
        _org_subnet_idx("fence-1")
    )


async def test_interleaved_fresh_creates_keep_single_binding(repo, fake_redis):
    """Regression for the claim-before-write × dangling-eviction race.

    Simulates T1 suspended mid-create (payload written, pointer not yet
    claimed) while T2 completes a full create for the same subnet. When
    T1 resumes its claim it must lose with a conflict — T2's binding
    must NOT be evicted as dangling, and the pointer must keep exactly
    one holder.
    """
    import json

    t1 = _org("org_t1", "fence-1")
    # T1 mid-flight: payload persisted, claim not yet attempted.
    await fake_redis.set(_org_key("org_t1"), json.dumps(t1.to_dict()))

    # T2 runs to completion and wins the fence.
    await repo.save_org(_org("org_t2", "fence-1"))
    assert (await repo.find_org_by_subnet("fence-1")).org_id == "org_t2"

    # T1 resumes: its claim must observe an ACTIVE holder and back off.
    with pytest.raises(OrgSubnetBindingConflictError) as ei:
        await repo._claim_subnet_binding(t1)
    assert ei.value.bound_org_id == "org_t2"
    assert (await repo.find_org_by_subnet("fence-1")).org_id == "org_t2"


async def test_resave_same_org_is_idempotent(repo):
    org = _org("org_a", "fence-1")
    await repo.save_org(org)
    org.display_name = "Renamed"
    await repo.save_org(org)  # must not raise
    loaded = await repo.find_org("org_a")
    assert loaded is not None
    assert loaded.display_name == "Renamed"
    assert (await repo.find_org_by_subnet("fence-1")).org_id == "org_a"


async def test_dissolved_holder_releases_fence_for_rebind(repo):
    org_a = _org("org_a", "fence-1")
    await repo.save_org(org_a)
    org_a.status = "dissolved"
    await repo.save_org(org_a)  # dissolution releases the pointer
    assert await repo.find_org_by_subnet("fence-1") is None

    await repo.save_org(_org("org_b", "fence-1"))  # rebind succeeds
    assert (await repo.find_org_by_subnet("fence-1")).org_id == "org_b"
    # The dissolved org row itself survives (soft-dissolve, audit trail).
    assert (await repo.find_org("org_a")).status == "dissolved"


async def test_stale_pointer_to_dissolved_org_is_evicted(repo, fake_redis):
    """Pointer still present but holder is dissolved (pre-fix rows)."""
    org_a = _org("org_a", "fence-1", status="dissolved")
    # Simulate legacy state: payload says dissolved but pointer remains.
    import json

    await fake_redis.set(_org_key("org_a"), json.dumps(org_a.to_dict()))
    await fake_redis.set(_org_subnet_idx("fence-1"), "org_a")

    await repo.save_org(_org("org_b", "fence-1"))
    assert (await repo.find_org_by_subnet("fence-1")).org_id == "org_b"


async def test_dangling_pointer_is_evicted(repo, fake_redis):
    await fake_redis.set(_org_subnet_idx("fence-1"), "org_ghost")  # no payload
    await repo.save_org(_org("org_b", "fence-1"))
    assert (await repo.find_org_by_subnet("fence-1")).org_id == "org_b"


async def test_delete_org_releases_only_own_pointer(repo):
    org_a = _org("org_a", "fence-1")
    await repo.save_org(org_a)
    org_a.status = "dissolved"
    await repo.save_org(org_a)
    await repo.save_org(_org("org_b", "fence-1"))  # took over the fence

    # Deleting the dissolved predecessor must NOT clobber org_b's binding.
    assert await repo.delete_org("org_a") is True
    assert (await repo.find_org_by_subnet("fence-1")).org_id == "org_b"


# ---------------------------------------------------------------------------
# Round-trips on the real key layout
# ---------------------------------------------------------------------------


async def test_org_round_trip_and_steward_index(repo):
    await repo.save_org(_org("org_a", "fence-1"))
    loaded = await repo.find_org("org_a")
    assert loaded is not None
    assert loaded.subnet_id == "fence-1"
    assert loaded.created_by.subject == "agt_steward"
    stewarded = await repo.list_orgs_by_steward("agt_steward")
    assert [o.org_id for o in stewarded] == ["org_a"]


async def test_membership_round_trip_and_active_filter(repo):
    await repo.save_org(_org("org_a", "fence-1"))
    await repo.upsert_membership(
        OrgMembership(org_id="org_a", agent_id="agt_w", role="worker")
    )
    inactive = OrgMembership(
        org_id="org_a", agent_id="agt_gone", role="worker", status="inactive"
    )
    await repo.upsert_membership(inactive)

    active = await repo.list_memberships("org_a", active_only=True)
    assert [m.agent_id for m in active] == ["agt_w"]
    everyone = await repo.list_memberships("org_a", active_only=False)
    assert {m.agent_id for m in everyone} == {"agt_w", "agt_gone"}
    assert await repo.delete_memberships_for_org("org_a") == 2


async def test_work_round_trip_and_open_filter(repo):
    await repo.save_org(_org("org_a", "fence-1"))
    await repo.save_work(OrgWorkItem(work_id="w1", org_id="org_a", title="todo A"))
    done = OrgWorkItem(work_id="w2", org_id="org_a", title="done B", status="done")
    await repo.save_work(done)

    open_items = await repo.list_work("org_a", open_only=True)
    assert [w.work_id for w in open_items] == ["w1"]
    all_items = await repo.list_work("org_a", open_only=False)
    assert {w.work_id for w in all_items} == {"w1", "w2"}
    assert await repo.delete_work_for_org("org_a") == 2


async def test_work_metadata_round_trip(repo):
    await repo.save_org(_org("org_a", "fence-1"))
    meta = {"wave": {"role": "child", "wave_id": "wv_x", "root_work_id": "w_root"}}
    await repo.save_work(
        OrgWorkItem(
            work_id="w1",
            org_id="org_a",
            title="shard",
            metadata=meta,
        )
    )
    got = await repo.find_work("org_a", "w1")
    assert got is not None
    assert got.metadata == meta
    assert "metadata" in got.to_dict()
    # legacy row without metadata key still loads
    import json

    legacy = OrgWorkItem(work_id="w2", org_id="org_a", title="old")
    raw = legacy.to_dict()
    del raw["metadata"]
    await repo.redis.set(_work_key("org_a", "w2"), json.dumps(raw))
    await repo.redis.sadd(_work_set_key("org_a"), "w2")
    loaded = await repo.find_work("org_a", "w2")
    assert loaded is not None
    assert loaded.metadata is None
