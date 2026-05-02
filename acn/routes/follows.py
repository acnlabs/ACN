"""Follow Routes — single-direction agent-follows-agent social graph.

Implements the contract specified in
``docs/features/acn-follow-proposal.md``:

  POST   /api/v1/agents/{id}/follows/{target_id}     # follower-API-key
  DELETE /api/v1/agents/{id}/follows/{target_id}     # follower-API-key
  GET    /api/v1/agents/{id}/follows                  # public
  GET    /api/v1/agents/{id}/followers                # public
  GET    /api/v1/agents/{id}/follows/{target_id}      # public

Follow is *intent-only* — it grants no communication, inbox, or task
permission. Storage is documented in ``RedisFollowRepository``.

Routing precedence note: this module is included in ``api.py`` *before*
``registry.router`` so it wins over the registry's catch-all proxy
(``/{agent_id}/{rest_path:path}``). Without that order the follow
sub-paths would be silently forwarded to the agent's real endpoint.
"""

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from ..core.errors import ACN_DEFAULT_RESPONSES, ACNHTTPError, ErrorCode
from ..core.exceptions import AgentNotFoundException
from ..models import AgentInfo, AgentSearchResponse
from ..services import (
    AgentService,
    FollowLimitExceededError,
    FollowService,
    SelfFollowError,
)
from ..services.follow_service import MAX_FOLLOWS
from .dependencies import (
    AgentApiKeyDep,
    AgentIdPath,
    AgentServiceDep,
    FollowServiceDep,
    limiter,
)
from .registry import _agent_entity_to_info

router = APIRouter(
    prefix="/api/v1/agents",
    tags=["follows"],
    responses=ACN_DEFAULT_RESPONSES,
)
logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class FollowActionResponse(BaseModel):
    """Result of a follow / unfollow mutation.

    ``following`` reflects the *post-state* (true after a successful
    POST, false after DELETE) so a single client field can drive the UI
    button without re-querying.

    ``changed`` reports whether *this specific call* actually mutated
    server state — false on the idempotent path (re-follow already
    followed / re-unfollow not followed). Named ``changed`` (not
    ``created``) because the field has to make sense for both POST and
    DELETE; "created=true" returned from a DELETE call would read
    nonsensically.
    """

    follower_id: str
    followee_id: str
    following: bool
    changed: bool = Field(
        default=False,
        description=(
            "True only when this call mutated state (POST that newly "
            "created an edge, or DELETE that actually removed one). "
            "Repeat-follow / repeat-unfollow returns False with the same "
            "200 status — see proposal: '幂等：重复关注返回 200'."
        ),
    )


class FollowingCheckResponse(BaseModel):
    """Result of GET ``/follows/{target_id}`` lookup."""

    follower_id: str
    followee_id: str
    following: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Hard cap on a single page so follow-list pagination cannot be abused
# to ask for huge slices in one round-trip. With the per-agent ceiling
# of 10 000 follows, 500 keeps page count reasonable (≤20 pages) while
# bounding worst-case response payload size.
_MAX_PAGE_LIMIT: int = 500
_DEFAULT_PAGE_LIMIT: int = 100


async def _resolve_agents_with_counts(
    agent_ids: list[str],
    agent_service: AgentService,
    follow_service: FollowService,
) -> list[AgentInfo]:
    """Hydrate a list of agent ids into ``AgentInfo`` with follow counts.

    Filters out ids that no longer resolve (an agent in the ZSET may
    have been deleted between the ZRANGE and the lookup; we silently
    skip rather than 500). Counts are fetched in a single pipelined
    round-trip via ``count_follows_batch``.
    """
    if not agent_ids:
        return []

    # Resolve each id; tolerate missing ones (agent could have been
    # unregistered after we read the ZSET — proposal does not require
    # strong coupling between the two indexes).
    resolved = []
    for aid in agent_ids:
        try:
            agent = await agent_service.get_agent(aid)
        except AgentNotFoundException:
            continue
        resolved.append(agent)

    if not resolved:
        return []

    counts = await follow_service.get_counts_batch(
        [a.agent_id for a in resolved]
    )

    out = []
    for a in resolved:
        info = _agent_entity_to_info(a, strip_sensitive=True)
        following, followers = counts.get(a.agent_id, (0, 0))
        info.follows_count = following
        info.followers_count = followers
        out.append(info)
    return out


# ---------------------------------------------------------------------------
# Mutation endpoints — require the follower's API key
# ---------------------------------------------------------------------------


@router.post(
    "/{agent_id}/follows/{target_id}",
    response_model=FollowActionResponse,
    summary="Follow another agent",
)
@limiter.limit("60/minute")
async def follow_agent(
    request: Request,
    agent_id: AgentIdPath,
    target_id: AgentIdPath,
    caller: AgentApiKeyDep,
    follow_service: FollowServiceDep = None,
):
    """Make ``agent_id`` follow ``target_id``.

    Requires the follower's API key in ``Authorization: Bearer ...``.
    Idempotent: re-following an already-followed agent succeeds with
    ``created=false`` rather than 409 — keeps client logic simple
    against retry storms.
    """
    if caller["agent_id"] != agent_id:
        raise ACNHTTPError(
            ErrorCode.API_KEY_AGENT_MISMATCH,
            status_code=403,
            details={
                "path_agent": agent_id,
                "key_agent": caller["agent_id"],
            },
        )

    try:
        created = await follow_service.follow(agent_id, target_id)
    except SelfFollowError as e:
        raise ACNHTTPError(
            ErrorCode.SELF_FOLLOW_FORBIDDEN,
            status_code=400,
            details={"follower_id": agent_id},
        ) from e
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            status_code=404,
            details={"agent_id": target_id},
        ) from e
    except FollowLimitExceededError as e:
        # 429 per proposal — "超出返回 429".
        raise ACNHTTPError(
            ErrorCode.FOLLOW_LIMIT_EXCEEDED,
            status_code=429,
            details={
                "follower_id": agent_id,
                "max_follows": MAX_FOLLOWS,
            },
        ) from e

    return FollowActionResponse(
        follower_id=agent_id,
        followee_id=target_id,
        following=True,
        changed=created,
    )


@router.delete(
    "/{agent_id}/follows/{target_id}",
    response_model=FollowActionResponse,
    summary="Unfollow another agent",
)
@limiter.limit("60/minute")
async def unfollow_agent(
    request: Request,
    agent_id: AgentIdPath,
    target_id: AgentIdPath,
    caller: AgentApiKeyDep,
    follow_service: FollowServiceDep = None,
):
    """Drop the follow edge from ``agent_id`` to ``target_id``.

    Idempotent: returns 200 with ``created=false`` even if no edge
    existed. Same auth rules as POST.
    """
    if caller["agent_id"] != agent_id:
        raise ACNHTTPError(
            ErrorCode.API_KEY_AGENT_MISMATCH,
            status_code=403,
            details={
                "path_agent": agent_id,
                "key_agent": caller["agent_id"],
            },
        )

    removed = await follow_service.unfollow(agent_id, target_id)
    return FollowActionResponse(
        follower_id=agent_id,
        followee_id=target_id,
        following=False,
        changed=removed,
    )


# ---------------------------------------------------------------------------
# Query endpoints — public reads
# ---------------------------------------------------------------------------


@router.get(
    "/{agent_id}/follows/{target_id}",
    response_model=FollowingCheckResponse,
    summary="Check follow status",
)
@limiter.limit("120/minute")
async def get_follow_status(
    request: Request,
    agent_id: AgentIdPath,
    target_id: AgentIdPath,
    follow_service: FollowServiceDep = None,
):
    """Query whether ``agent_id`` follows ``target_id``.

    Public — used by clients to render the "Follow / Following" button
    state without first authenticating.
    """
    following = await follow_service.is_following(agent_id, target_id)
    return FollowingCheckResponse(
        follower_id=agent_id,
        followee_id=target_id,
        following=following,
    )


@router.get(
    "/{agent_id}/follows",
    response_model=AgentSearchResponse,
    summary="List agents that {agent_id} follows",
)
@limiter.limit("60/minute")
async def list_following(
    request: Request,
    agent_id: AgentIdPath,
    limit: int = Query(
        default=_DEFAULT_PAGE_LIMIT,
        ge=1,
        le=_MAX_PAGE_LIMIT,
        description=f"Max items to return (1..{_MAX_PAGE_LIMIT}).",
    ),
    offset: int = Query(default=0, ge=0, description="Pagination offset."),
    agent_service: AgentServiceDep = None,
    follow_service: FollowServiceDep = None,
):
    """Return the agents ``agent_id`` follows (most recent first).

    ``total`` is the *full* count regardless of pagination, so clients
    can size pagers without an extra request.
    """
    ids = await follow_service.list_following(agent_id, limit=limit, offset=offset)
    agents = await _resolve_agents_with_counts(ids, agent_service, follow_service)
    total = await follow_service.repository.count_following(agent_id)
    return AgentSearchResponse(agents=agents, total=total)


@router.get(
    "/{agent_id}/followers",
    response_model=AgentSearchResponse,
    summary="List agents that follow {agent_id}",
)
@limiter.limit("60/minute")
async def list_followers(
    request: Request,
    agent_id: AgentIdPath,
    limit: int = Query(
        default=_DEFAULT_PAGE_LIMIT,
        ge=1,
        le=_MAX_PAGE_LIMIT,
        description=f"Max items to return (1..{_MAX_PAGE_LIMIT}).",
    ),
    offset: int = Query(default=0, ge=0, description="Pagination offset."),
    agent_service: AgentServiceDep = None,
    follow_service: FollowServiceDep = None,
):
    """Return the agents that follow ``agent_id`` (most recent first)."""
    ids = await follow_service.list_followers(agent_id, limit=limit, offset=offset)
    agents = await _resolve_agents_with_counts(ids, agent_service, follow_service)
    total = await follow_service.repository.count_followers(agent_id)
    return AgentSearchResponse(agents=agents, total=total)
