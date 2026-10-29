"""Internal chat-group subnets.

The human who owns the chat is the subnet's governance owner and is not a
member. Only the group's agents are members. Gated by X-Internal-Token.
"""

from __future__ import annotations

import re

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..core.errors import ACNHTTPError, ErrorCode
from ..core.exceptions import SubnetNotFoundException
from .dependencies import AgentServiceDep, InternalTokenDep, SubnetServiceDep

router = APIRouter(prefix="/api/v1/subnets/internal/chat-groups", tags=["subnets"])
logger = structlog.get_logger()

_CHAT_ID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


class ChatGroupCreate(BaseModel):
    chat_id: str = Field(..., min_length=8, max_length=80)
    owner_subject: str = Field(..., min_length=1, max_length=200)
    name: str = Field(..., min_length=1, max_length=200)
    member_agent_ids: list[str] = Field(default_factory=list)


class ChatGroupMember(BaseModel):
    owner_subject: str = Field(..., min_length=1, max_length=200)
    agent_id: str = Field(..., min_length=1, max_length=128)


class ChatGroupDelete(BaseModel):
    owner_subject: str = Field(..., min_length=1, max_length=200)


def chat_group_slug(chat_id: str) -> str:
    if not _CHAT_ID.match(chat_id):
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"reason": "invalid_chat_id"},
        )
    return f"chat-{chat_id}"


def _reject_reserved_owner(owner_subject: str) -> None:
    if owner_subject in {"backend@internal", "system"}:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"reason": "invalid_owner"},
        )


async def _require_chat_subnet(subnet_service, slug: str, owner_subject: str):
    try:
        subnet = await subnet_service.get_subnet(slug)
    except SubnetNotFoundException as exc:
        raise ACNHTTPError(
            ErrorCode.SUBNET_NOT_FOUND,
            404,
            details={"slug": slug},
        ) from exc
    kind = getattr(subnet, "owner_kind", "agent")
    purpose = (subnet.metadata or {}).get("purpose")
    if kind != "human" or subnet.owner != owner_subject or purpose != "chat_group":
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            403,
            details={"reason": "chat_subnet_owner_mismatch"},
        )
    return subnet


@router.post("", status_code=201)
async def create_chat_group_subnet(
    request: Request,
    body: ChatGroupCreate,
    _: InternalTokenDep,
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
):
    _reject_reserved_owner(body.owner_subject)
    slug = chat_group_slug(body.chat_id)
    try:
        existing = await subnet_service.get_subnet(slug)
    except SubnetNotFoundException:
        existing = None

    if existing is None:
        subnet = await subnet_service.create_subnet(
            slug=slug,
            name=body.name,
            owner=body.owner_subject,
            owner_kind="human",
            include_owner_as_member=False,
            description=f"Chat group {body.chat_id}",
            is_private=True,
            join_policy="approval",
            metadata={"purpose": "chat_group", "chat_id": body.chat_id},
        )
    else:
        subnet = await _require_chat_subnet(subnet_service, slug, body.owner_subject)

    for agent_id in body.member_agent_ids:
        if not agent_id or agent_id == subnet.owner:
            continue
        await subnet_service.add_member(slug, agent_id)
        await agent_service.join_subnet(agent_id, slug)

    logger.info("chat_group_subnet_ready", slug=slug, chat_id=body.chat_id)
    return {
        "slug": slug,
        "owner": body.owner_subject,
        "owner_kind": "human",
    }


@router.post("/{slug}/members", status_code=204)
async def add_chat_group_member(
    slug: str,
    body: ChatGroupMember,
    _: InternalTokenDep,
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
):
    subnet = await _require_chat_subnet(subnet_service, slug, body.owner_subject)
    if body.agent_id == subnet.owner:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"reason": "owner_is_not_a_member"},
        )
    await subnet_service.add_member(slug, body.agent_id)
    await agent_service.join_subnet(body.agent_id, slug)
    return None


@router.delete("/{slug}/members/{agent_id}", status_code=204)
async def remove_chat_group_member(
    slug: str,
    agent_id: str,
    body: ChatGroupDelete,
    _: InternalTokenDep,
    subnet_service: SubnetServiceDep = None,
    agent_service: AgentServiceDep = None,
):
    subnet = await _require_chat_subnet(subnet_service, slug, body.owner_subject)
    if agent_id == subnet.owner:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"reason": "cannot_remove_owner"},
        )
    await subnet_service.remove_member(slug, agent_id)
    try:
        await agent_service.leave_subnet(agent_id, slug)
    except Exception:  # noqa: BLE001
        logger.warning("chat_group_leave_subnet_failed", slug=slug, agent_id=agent_id, exc_info=True)
    return None


@router.delete("/{slug}", status_code=204)
async def delete_chat_group_subnet(
    slug: str,
    body: ChatGroupDelete,
    _: InternalTokenDep,
    subnet_service: SubnetServiceDep = None,
):
    await _require_chat_subnet(subnet_service, slug, body.owner_subject)
    await subnet_service.delete_subnet(slug, body.owner_subject)
    return None
