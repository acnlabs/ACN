"""Follow Service.

Business logic for the agent-follow social graph layer described in
``docs/features/acn-follow-proposal.md``.

Responsibilities live here (not in routes / not in repository):
  - Reject self-follows (``A → A``) — they corrupt influence metrics
    and have no semantic meaning.
  - Enforce the per-agent follow ceiling (``MAX_FOLLOWS``) so a single
    runaway agent cannot blow up Redis memory.
  - Verify that the followee actually exists (404 vs. silent dangling
    pointer).
  - Idempotency: repeating an existing follow / unfollow is a normal
    success, not an error — clients re-issue on retry, and the proposal
    explicitly mandates 200-on-repeat.

Routes own HTTP-shape concerns (status codes, auth checks); this layer
only raises domain errors.
"""

import structlog  # type: ignore[import-untyped]

from ..core.exceptions import AgentNotFoundException
from ..core.interfaces import IAgentRepository, IFollowRepository

logger = structlog.get_logger()


# Per-agent ceiling for the *outgoing* follows index.
#
# Matches the figure called out in the proposal ("单个 agent 最多关注
# 10,000 个"). Picked to:
#   - Cap Redis memory: ZSET of 10k entries ≈ 0.5 MB upper bound.
#   - Mirror what mature social platforms enforce as a noise floor
#     against follow-spam bots, while staying generous enough that no
#     legitimate agent will hit it in normal use.
#
# The ceiling is *only* on outgoing follows; followers (incoming) are
# uncapped because we do not let one agent restrict another's actions.
MAX_FOLLOWS: int = 10_000


class FollowLimitExceededError(Exception):
    """Raised when ``follower`` already follows ``MAX_FOLLOWS`` agents."""


class SelfFollowError(Exception):
    """Raised when an agent tries to follow itself."""


class FollowService:
    """Orchestrates follow / unfollow / lookup over the follow graph."""

    def __init__(
        self,
        follow_repository: IFollowRepository,
        agent_repository: IAgentRepository,
    ):
        self.repository = follow_repository
        self.agent_repository = agent_repository

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    async def follow(self, follower_id: str, followee_id: str) -> bool:
        """Make ``follower_id`` follow ``followee_id``.

        Returns:
            True if a new edge was created, False if the edge already
            existed (idempotent path; routes still respond 200 OK).

        Raises:
            SelfFollowError: ``follower_id == followee_id``.
            AgentNotFoundException: ``followee_id`` is not registered.
            FollowLimitExceededError: follower already at ``MAX_FOLLOWS``.
        """
        if follower_id == followee_id:
            raise SelfFollowError(
                "An agent cannot follow itself"
            )

        # Followee existence check — keeps the index from filling up
        # with pointers to ids that never existed (a common abuse vector
        # otherwise: spam-follow random uuids to inflate "followers" of
        # unknown ids if we were to skip this).
        if not await self.agent_repository.exists(followee_id):
            raise AgentNotFoundException(
                f"Agent {followee_id} not found"
            )

        # Capacity check — done *before* the ZADD so we never accept the
        # 10001-th edge and then race to remove it. ZCARD is O(1) so the
        # extra round-trip is negligible.
        # Edge case: if the follow already exists, count is unchanged so
        # we must allow it through even at exactly MAX_FOLLOWS.
        current = await self.repository.count_following(follower_id)
        if current >= MAX_FOLLOWS and not await self.repository.is_following(
            follower_id, followee_id
        ):
            raise FollowLimitExceededError(
                f"Follow limit reached ({MAX_FOLLOWS}); unfollow some agents first"
            )

        created = await self.repository.add(follower_id, followee_id)
        if created:
            logger.info(
                "agent_followed",
                follower_id=follower_id,
                followee_id=followee_id,
            )
        return created

    async def unfollow(self, follower_id: str, followee_id: str) -> bool:
        """Remove the follow edge if it exists.

        Returns:
            True if an edge was actually removed, False if nothing was
            following (idempotent path).
        """
        removed = await self.repository.remove(follower_id, followee_id)
        if removed:
            logger.info(
                "agent_unfollowed",
                follower_id=follower_id,
                followee_id=followee_id,
            )
        return removed

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def is_following(self, follower_id: str, followee_id: str) -> bool:
        return await self.repository.is_following(follower_id, followee_id)

    async def list_following(
        self,
        agent_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[str]:
        return await self.repository.list_following(agent_id, limit, offset)

    async def list_followers(
        self,
        agent_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[str]:
        return await self.repository.list_followers(agent_id, limit, offset)

    async def get_counts(self, agent_id: str) -> tuple[int, int]:
        """Return ``(following_count, followers_count)`` for ``agent_id``.

        Used by ``GET /agents/{id}`` to populate the per-agent counts.
        """
        following = await self.repository.count_following(agent_id)
        followers = await self.repository.count_followers(agent_id)
        return following, followers

    async def get_counts_batch(
        self, agent_ids: list[str]
    ) -> dict[str, tuple[int, int]]:
        """Batch-fetch counts for many agents in a single round-trip.

        Used by list endpoints (``GET /agents/{id}/follows``,
        ``/followers``) that return ``AgentInfo`` objects with their
        counts populated.
        """
        return await self.repository.count_follows_batch(agent_ids)

    async def cleanup_agent(self, agent_id: str) -> None:
        """Delete every follow edge mentioning ``agent_id``.

        Called from the agent unregistration path so a deleted agent
        does not leave dangling pointers in other agents' indexes.
        """
        await self.repository.cleanup_agent(agent_id)
        logger.info("follow_data_cleaned", agent_id=agent_id)
