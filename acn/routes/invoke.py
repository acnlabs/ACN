"""AgentRouter network door — POST /api/v1/invoke (D16).

Agent callers use the same Bearer as /communication/send.
Host proxies with X-Internal-Token and payer.kind=human.
Delivery reuses MessageService; no attention_fee.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from ..config import get_settings
from ..core.errors import ACN_DEFAULT_RESPONSES, ACNHTTPError, ErrorCode
from ..core.exceptions import AgentNotFoundException, PolicyRejected
from ..core.validators import check_dict_size_256k
from ..invoke_slots import (
    MAX_SLOT_ATTEMPTS,
    SlotContractError,
    agent_declares_slot,
    list_slot_candidates,
    parse_declared_slots,
    policy_mode,
    require_platform_slot,
)
from ..monitoring.audit import AuditEventType
from .communication import _payload_to_a2a_message, _result_message_id
from .dependencies import (
    AgentServiceDep,
    AuditDep,
    MessageServiceDep,
    MetricsDep,
    verify_agent_api_key,
    verify_internal_token,
)

router = APIRouter(prefix="/api/v1", tags=["invoke"], responses=ACN_DEFAULT_RESPONSES)
logger = structlog.get_logger()

SYSTEM_FROM = "system:agent-router"
_LOCAL_PREFIXES = ("local:", "sys:")


class InvokePayer(BaseModel):
    kind: Literal["human"]
    user_id: str = Field(..., min_length=3, max_length=128)


class InvokeCompleteRequest(BaseModel):
    request_id: str | None = Field(default=None, max_length=80)
    hop_id: str | None = Field(default=None, max_length=200)
    usage: dict[str, Any] | None = None

    @field_validator("request_id", "hop_id")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    @model_validator(mode="after")
    def _require_request_or_hop(self):
        if self.request_id is None and self.hop_id is None:
            raise ValueError("Either request_id or hop_id is required")
        return self


class InvokeRequest(BaseModel):
    to: str | None = Field(default=None, max_length=128)
    slot: str | None = Field(default=None, max_length=64)
    message: dict[str, Any]
    request_id: str | None = Field(default=None, max_length=80)
    payer: InvokePayer | None = None

    @field_validator("to", "slot")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    @field_validator("message")
    @classmethod
    def _message_size(cls, v: dict[str, Any]) -> dict[str, Any]:
        return check_dict_size_256k("message", v)

    @model_validator(mode="after")
    def _require_to_or_slot(self):
        if self.to is None and self.slot is None:
            raise ValueError("Either to or slot is required")
        return self


def _bare_id(raw: str) -> str:
    value = raw.strip()
    return value[4:] if value.startswith("acn:") else value


def _reject_local_or_system(raw: str) -> str:
    value = raw.strip()
    lowered = value.lower()
    if lowered.startswith(_LOCAL_PREFIXES):
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"reason": "local_or_system_agent_forbidden", "agent_id": raw},
        )
    bare = _bare_id(value)
    if bare.startswith(_LOCAL_PREFIXES):
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"reason": "local_or_system_agent_forbidden", "agent_id": raw},
        )
    return bare


def _invoke_hop_id(request_id: str, callee: str) -> str:
    return f"hop:invoke:{request_id}:{callee}"


def _parse_invoke_hop_id(hop_id: str) -> tuple[str, str] | None:
    prefix = "hop:invoke:"
    if not hop_id.startswith(prefix):
        return None
    request_id, sep, callee = hop_id[len(prefix) :].rpartition(":")
    if not sep or not request_id or not callee:
        return None
    return request_id, callee


def _slot_http_error(exc: SlotContractError) -> ACNHTTPError:
    return ACNHTTPError(
        ErrorCode.INVALID_REQUEST,
        400,
        details={"reason": exc.reason, **exc.extra},
    )


async def _authenticate_invoke(
    request: Request,
    body: InvokeRequest,
    agent_service: AgentServiceDep,
) -> tuple[str, str, str]:
    """Return (from_agent, caller_kind, caller_id)."""
    is_host = bool(request.headers.get("X-Internal-Token"))
    if is_host:
        verify_internal_token(
            request,
            x_internal_token=request.headers["X-Internal-Token"],
        )
        if body.payer is None or body.payer.kind != "human":
            raise ACNHTTPError(
                ErrorCode.INVALID_REQUEST,
                400,
                details={"reason": "host_invoke_requires_human_payer"},
            )
        return SYSTEM_FROM, "host", body.payer.user_id
    agent_info = await verify_agent_api_key(
        request,
        background_tasks=None,  # type: ignore[arg-type]
        authorization=request.headers.get("Authorization") or "",
        agent_service=agent_service,
    )
    from_agent = agent_info["agent_id"]
    return from_agent, "agent", from_agent


async def _resolve_candidates(
    *,
    to: str | None,
    slot: str | None,
    caller_kind: str,
    agent_service: AgentServiceDep,
) -> tuple[list[str], str | None]:
    """Return (callee_ids, slot_id). Slot requests may include fallbacks (D22–D26)."""
    slot_id: str | None = None
    if slot is not None:
        try:
            slot_id = require_platform_slot(slot)["id"]
        except SlotContractError as exc:
            raise _slot_http_error(exc) from exc

    if to and slot_id is None:
        return [_reject_local_or_system(to)], None

    assert slot_id is not None
    preferred: str | None = None
    if to:
        preferred = _reject_local_or_system(to)
        try:
            agent = await agent_service.get_agent(preferred)
        except AgentNotFoundException as exc:
            raise ACNHTTPError(ErrorCode.AGENT_NOT_FOUND, 404, details={}) from exc
        if not agent_declares_slot(agent, slot_id):
            raise ACNHTTPError(
                ErrorCode.AGENT_NOT_FOUND,
                404,
                details={"reason": "slot_not_declared", "slot": slot_id},
            )

    pool = await agent_service.search_agents(status="all")
    alive_ids = await agent_service.batch_alive([a.agent_id for a in pool])
    ordered = list_slot_candidates(
        pool,
        slot_id=slot_id,
        alive_ids=alive_ids,
        caller_kind=caller_kind,
        preferred=preferred,
    )
    ids = [a.agent_id for a in ordered]
    if preferred and preferred not in ids:
        ids = [preferred, *ids]
    if not ids:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"reason": "no_slot_provider", "slot": slot_id},
        )
    return ids[:MAX_SLOT_ATTEMPTS], slot_id


@router.get("/invoke/slots/{slot_id}")
async def list_slot_providers(
    request: Request,
    slot_id: str,
    agent_service: AgentServiceDep,
) -> dict[str, Any]:
    """Directory of agents that declared this platform slot (D21)."""
    is_host = bool(request.headers.get("X-Internal-Token"))
    if is_host:
        verify_internal_token(
            request,
            x_internal_token=request.headers["X-Internal-Token"],
        )
    else:
        await verify_agent_api_key(
            request,
            background_tasks=None,  # type: ignore[arg-type]
            authorization=request.headers.get("Authorization") or "",
            agent_service=agent_service,
        )
    try:
        spec = require_platform_slot(slot_id)
    except SlotContractError as exc:
        raise _slot_http_error(exc) from exc

    candidates = await agent_service.search_agents(status="all")
    declarers = [a for a in candidates if agent_declares_slot(a, spec["id"])]
    alive_ids = await agent_service.batch_alive([a.agent_id for a in declarers])
    providers = [
        {
            "agent_id": a.agent_id,
            "name": getattr(a, "name", None),
            "online": a.agent_id in alive_ids,
            "mode": policy_mode(a),
            "owner": getattr(a, "owner", None),
            "invoke_slots": parse_declared_slots(getattr(a, "metadata", None)),
        }
        for a in sorted(declarers, key=lambda item: item.agent_id)
    ]
    return {"slot": spec, "providers": providers}


@router.post("/invoke/complete")
async def complete_invoke(
    request: Request,
    body: InvokeCompleteRequest,
    agent_service: AgentServiceDep,
) -> dict[str, Any]:
    """Callee writeback for Mode B invoke usage (D40–D42)."""
    agent_info = await verify_agent_api_key(
        request,
        background_tasks=None,  # type: ignore[arg-type]
        authorization=request.headers.get("Authorization") or "",
        agent_service=agent_service,
    )
    callee = str(agent_info["agent_id"])
    request_id = body.request_id
    if body.hop_id:
        parsed = _parse_invoke_hop_id(body.hop_id)
        if parsed is None:
            raise ACNHTTPError(
                ErrorCode.INVALID_REQUEST,
                400,
                details={"reason": "malformed_invoke_hop"},
            )
        hop_request, hop_callee = parsed
        if hop_callee != callee:
            raise ACNHTTPError(
                ErrorCode.INVALID_REQUEST,
                403,
                details={"reason": "invoke_complete_forbidden"},
            )
        if request_id and request_id != hop_request:
            raise ACNHTTPError(
                ErrorCode.INVALID_REQUEST,
                400,
                details={"reason": "request_id_mismatch"},
            )
        request_id = hop_request
    if not request_id:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"reason": "request_id_required"},
        )
    settled = await _forward_backend_complete(
        request_id=request_id,
        callee=callee,
        caller=callee,
        usage=body.usage,
        delivery_status="writeback",
    )
    return {
        "request_id": request_id,
        "hop_id": _invoke_hop_id(request_id, callee),
        "to": callee,
        **settled,
    }


@router.post("/invoke")
async def invoke(
    request: Request,
    body: InvokeRequest,
    message_service: MessageServiceDep,
    metrics: MetricsDep,
    audit: AuditDep,
    agent_service: AgentServiceDep,
) -> dict[str, Any]:
    """Specified-id or slot invoke. Host door uses internal token; agents use API key."""
    from_agent, caller_kind, caller_id = await _authenticate_invoke(
        request, body, agent_service
    )
    candidates, slot_id = await _resolve_candidates(
        to=body.to,
        slot=body.slot,
        caller_kind=caller_kind,
        agent_service=agent_service,
    )
    request_id = (body.request_id or "").strip() or str(uuid.uuid4())
    allow_fallback = slot_id is not None and len(candidates) > 1
    attempts: list[dict[str, str]] = []
    last_error: ACNHTTPError | None = None
    callee = candidates[0]
    result: Any = None

    for callee in candidates:
        hop_id = _invoke_hop_id(request_id, callee)
        envelope = {
            "role": "user",
            "parts": [
                {
                    "kind": "text",
                    "text": body.message.get("text")
                    if isinstance(body.message.get("text"), str)
                    else str(body.message),
                }
            ],
            "metadata": {
                "agentplanet": {
                    "invoke": {
                        "request_id": request_id,
                        "hop_id": hop_id,
                        "caller_kind": caller_kind,
                        **({"slot": slot_id} if slot_id else {}),
                    }
                }
            },
        }
        if isinstance(body.message.get("role"), str) and isinstance(
            body.message.get("parts"), list
        ):
            envelope = {
                **{
                    k: v
                    for k, v in body.message.items()
                    if k not in ("message_id", "messageId")
                },
                "metadata": {
                    **(body.message.get("metadata") or {}),
                    "agentplanet": {
                        **((body.message.get("metadata") or {}).get("agentplanet") or {}),
                        "invoke": {
                            "request_id": request_id,
                            "hop_id": hop_id,
                            "caller_kind": caller_kind,
                            **({"slot": slot_id} if slot_id else {}),
                        },
                    },
                },
            }
        try:
            message = _payload_to_a2a_message(envelope)
            result = await message_service.send_message(
                from_agent_id=from_agent,
                to_agent_id=callee,
                message=message,
                priority="normal",
            )
            last_error = None
            break
        except (AgentNotFoundException, PolicyRejected) as exc:
            reason = (
                exc.reason
                if isinstance(exc, PolicyRejected)
                else "agent_not_found"
            )
            attempts.append({"to": callee, "status": "failed", "reason": reason})
            await metrics.inc_message_count(
                from_agent=from_agent, to_agent=callee, status="rejected"
            )
            if isinstance(exc, PolicyRejected):
                last_error = ACNHTTPError(
                    ErrorCode.COMMUNICATION_REJECTED,
                    403,
                    details={"reason": exc.reason},
                )
            else:
                last_error = ACNHTTPError(ErrorCode.AGENT_NOT_FOUND, 404, details={})
            if not allow_fallback:
                raise last_error from exc
            continue

    if last_error is not None or result is None:
        if last_error is not None:
            raise last_error
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            details={"reason": "no_slot_provider", "slot": slot_id},
        )

    hop_id = _invoke_hop_id(request_id, callee)
    await metrics.inc_message_count(
        from_agent=from_agent, to_agent=callee, status="success"
    )
    await audit.log_event(
        event_type=AuditEventType.MESSAGE_SENT,
        actor_id=caller_id,
        actor_type="system" if caller_kind == "host" else "agent",
        target_id=callee,
        target_type="agent",
        message_id=_result_message_id(result),
    )

    delivery = result if isinstance(result, dict) else {"status": "sent"}
    usage = delivery.get("usage") if isinstance(delivery.get("usage"), dict) else None

    if caller_kind == "agent":
        await _notify_backend_complete(
            request_id=request_id,
            callee=callee,
            caller=from_agent,
            usage=usage,
            delivery_status=str(delivery.get("status") or "sent"),
        )

    payload: dict[str, Any] = {
        "request_id": request_id,
        "hop_id": hop_id,
        "to": callee,
        "from": from_agent,
        "status": delivery.get("status") or "sent",
        "delivery": delivery,
        "usage": usage,
        **({"slot": slot_id} if slot_id else {}),
    }
    if attempts:
        payload["fallback_from"] = attempts[0]["to"]
        payload["attempts"] = attempts
    return payload


async def _forward_backend_complete(
    *,
    request_id: str,
    callee: str,
    caller: str,
    usage: dict[str, Any] | None,
    delivery_status: str,
) -> dict[str, Any]:
    """Agent-facing writeback waits for Host settle (D40)."""
    settings = get_settings()
    if not settings.backend_url or not settings.internal_api_token:
        raise HTTPException(status_code=503, detail="backend_unconfigured")
    url = f"{settings.backend_url.rstrip('/')}/api/internal/agent-router/complete"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                url,
                json={
                    "request_id": request_id,
                    "callee_agent_id": callee,
                    "caller_agent_id": caller,
                    "usage": usage,
                    "delivery_status": delivery_status,
                },
                headers={"X-Internal-Token": settings.internal_api_token},
            )
    except httpx.HTTPError as exc:
        logger.warning("invoke_complete_unreachable", error=str(exc))
        raise HTTPException(status_code=503, detail="backend_unreachable") from exc
    if resp.status_code >= 400:
        detail: dict[str, Any]
        try:
            payload = resp.json()
            detail = payload if isinstance(payload, dict) else {"raw": payload}
        except ValueError:
            detail = {"raw": resp.text[:300]}
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            resp.status_code if resp.status_code in (400, 402, 403, 404, 422) else 502,
            details={"reason": "host_complete_failed", **detail},
        )
    data = resp.json() if resp.content else {}
    return data if isinstance(data, dict) else {"status": "accepted"}


async def _notify_backend_complete(
    *,
    request_id: str,
    callee: str,
    caller: str,
    usage: dict[str, Any] | None,
    delivery_status: str,
) -> None:
    settings = get_settings()
    if not settings.backend_url or not settings.internal_api_token:
        logger.info("invoke_backend_complete_skipped", reason="backend_unconfigured")
        return
    url = f"{settings.backend_url.rstrip('/')}/api/internal/agent-router/complete"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                url,
                json={
                    "request_id": request_id,
                    "callee_agent_id": callee,
                    "caller_agent_id": caller,
                    "usage": usage,
                    "delivery_status": delivery_status,
                },
                headers={"X-Internal-Token": settings.internal_api_token},
            )
        if resp.status_code >= 400:
            logger.warning(
                "invoke_backend_complete_failed",
                status=resp.status_code,
                hop=f"hop:invoke:{request_id}:{callee}",
            )
    except httpx.HTTPError as exc:
        logger.warning("invoke_backend_complete_unreachable", error=str(exc))
