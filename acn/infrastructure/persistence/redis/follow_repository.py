"""Redis Implementation of IFollowRepository.

Storage layout (mirrors the design proposed in
``docs/features/acn-follow-proposal.md``):

  ZSET acn:follows:{follower_id}     member=followee_id  score=created_at(epoch)
  ZSET acn:followers:{followee_id}   member=follower_id   score=created_at(epoch)

Both indexes are dual-written under a pipeline so a single Redis
round-trip persists the relation. We deliberately *do not* maintain a
separate ``acn:follows_count:{id}`` string counter (which the proposal
mentions as a possibility): ``ZCARD`` on a sorted set is already O(1) and
removing the redundant counter eliminates a class of consistency bugs
where an INCR/DECR could drift from the underlying set on partial
failure. Counts are therefore always derived from the source-of-truth
ZSET.
"""

import time

import redis.asyncio as redis  # type: ignore[import-untyped]

from ....core.interfaces import IFollowRepository


def _follows_key(follower_id: str) -> str:
    return f"acn:follows:{follower_id}"


def _followers_key(followee_id: str) -> str:
    return f"acn:followers:{followee_id}"


class RedisFollowRepository(IFollowRepository):
    """Redis-backed implementation of the follow graph."""

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    async def add(self, follower_id: str, followee_id: str) -> bool:
        """Atomically add the edge in both indexes.

        ``nx=True`` semantics:
          - If the edge already exists, ZADD is a no-op and the original
            ``created_at`` score is preserved. This matters because the
            score is the source of truth for "most-recently followed"
            ordering — without NX, a repeat-follow (e.g. user double-
            clicks the button) would re-stamp the edge to the current
            time and push it back to the top of the follower's feed
            even though no new intent was expressed.
          - If the edge does not exist, ZADD with NX behaves as a
            normal create and returns 1.

        We use the ``follows`` side's return value as the canonical
        "newly created?" signal. Cross-index split-brain (one side has
        the edge, the other doesn't, after a partial failure) is
        self-healing under NX: whichever side is missing gets the entry
        added; the side that already had it stays untouched.
        """
        score = time.time()
        pipe = self.redis.pipeline()
        pipe.zadd(_follows_key(follower_id), {followee_id: score}, nx=True)
        pipe.zadd(_followers_key(followee_id), {follower_id: score}, nx=True)
        results = await pipe.execute()
        # First result is the number of new members on the follows index.
        return bool(results[0])

    async def remove(self, follower_id: str, followee_id: str) -> bool:
        pipe = self.redis.pipeline()
        pipe.zrem(_follows_key(follower_id), followee_id)
        pipe.zrem(_followers_key(followee_id), follower_id)
        results = await pipe.execute()
        return bool(results[0])

    async def is_following(self, follower_id: str, followee_id: str) -> bool:
        score = await self.redis.zscore(_follows_key(follower_id), followee_id)
        return score is not None

    async def list_following(
        self,
        agent_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[str]:
        # ``zrevrange`` orders from highest score (most recent) to lowest;
        # callers can paginate by passing ``offset``/``limit``.
        end = offset + limit - 1
        members = await self.redis.zrevrange(_follows_key(agent_id), offset, end)
        return [_decode(m) for m in members]

    async def list_followers(
        self,
        agent_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[str]:
        end = offset + limit - 1
        members = await self.redis.zrevrange(_followers_key(agent_id), offset, end)
        return [_decode(m) for m in members]

    async def count_following(self, agent_id: str) -> int:
        return int(await self.redis.zcard(_follows_key(agent_id)))

    async def count_followers(self, agent_id: str) -> int:
        return int(await self.redis.zcard(_followers_key(agent_id)))

    async def count_follows_batch(
        self, agent_ids: list[str]
    ) -> dict[str, tuple[int, int]]:
        if not agent_ids:
            return {}
        pipe = self.redis.pipeline()
        for aid in agent_ids:
            pipe.zcard(_follows_key(aid))
            pipe.zcard(_followers_key(aid))
        raw = await pipe.execute()
        # raw is interleaved: [follows_n, followers_n, follows_n, followers_n, ...]
        out: dict[str, tuple[int, int]] = {}
        for i, aid in enumerate(agent_ids):
            following = int(raw[2 * i] or 0)
            followers = int(raw[2 * i + 1] or 0)
            out[aid] = (following, followers)
        return out

    async def cleanup_agent(self, agent_id: str) -> None:
        """Delete all edges referencing ``agent_id``.

        Two-phase walk:
          1. Read this agent's full follow list and follower list.
          2. For each entry, drop the reverse-index pointer back to the
             deleted agent, then delete this agent's own indexes.

        With the ZSET cap of 10 000 (enforced at the service layer) this
        is at most ~20 000 ZREMs which fits in a pipeline. Failure mid-
        cleanup leaves only stale reverse references — cosmetic, not
        correctness-affecting, because consumers always re-resolve
        follower/followee ids via the AgentRepository.
        """
        following = await self.redis.zrange(_follows_key(agent_id), 0, -1)
        followers = await self.redis.zrange(_followers_key(agent_id), 0, -1)

        if following or followers:
            pipe = self.redis.pipeline()
            # Forget this agent in everyone's reverse index.
            for fid in followers:
                pipe.zrem(_follows_key(_decode(fid)), agent_id)
            for fid in following:
                pipe.zrem(_followers_key(_decode(fid)), agent_id)
            pipe.delete(_follows_key(agent_id))
            pipe.delete(_followers_key(agent_id))
            await pipe.execute()


def _decode(value: object) -> str:
    """Redis returns bytes when ``decode_responses=False``.

    The codebase mixes both modes (most repos rely on
    ``decode_responses=True`` set on the client); accept both so this
    repository works whichever wiring the caller chose.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)
