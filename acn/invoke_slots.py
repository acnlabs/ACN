"""AgentRouter P2 — platform slot allowlist and declaration helpers.

See docs/product/agent-router-v0.md D17–D21.
Slot ≠ tag. Unknown ids are rejected. Platform owns input/output/pricing.
"""

from __future__ import annotations

from typing import Any

PLATFORM_SLOTS: dict[str, dict[str, str]] = {
    "text.reply": {
        "id": "text.reply",
        "input": "text",
        "output": "text",
        "pricing": "l2_token",
    }
}

MAX_INVOKE_SLOTS = 8
MAX_SLOT_ATTEMPTS = 3
METADATA_KEY = "invoke_slots"


class SlotContractError(ValueError):
    def __init__(self, message: str, *, reason: str, extra: dict[str, Any] | None = None):
        super().__init__(message)
        self.reason = reason
        self.extra = extra or {}


def normalize_slot_id(raw: str | None) -> str:
    return (raw or "").strip()


def require_platform_slot(raw: str | None) -> dict[str, str]:
    slot_id = normalize_slot_id(raw)
    if not slot_id:
        raise SlotContractError("slot is required", reason="slot_required")
    spec = PLATFORM_SLOTS.get(slot_id)
    if spec is None:
        raise SlotContractError(
            "Unknown invoke slot",
            reason="unknown_slot",
            extra={"slot": slot_id},
        )
    return dict(spec)


def parse_declared_slots(metadata: dict[str, Any] | None) -> list[dict[str, str]]:
    raw = (metadata or {}).get(METADATA_KEY)
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        slot_id = ""
        if isinstance(item, str):
            slot_id = normalize_slot_id(item)
        elif isinstance(item, dict):
            slot_id = normalize_slot_id(item.get("id") or item.get("slot"))
        if not slot_id or slot_id in seen or slot_id not in PLATFORM_SLOTS:
            continue
        seen.add(slot_id)
        out.append(dict(PLATFORM_SLOTS[slot_id]))
    return out


def agent_declares_slot(agent: Any, slot_id: str) -> bool:
    metadata = getattr(agent, "metadata", None)
    if metadata is None and isinstance(agent, dict):
        metadata = agent.get("metadata")
    return any(item["id"] == slot_id for item in parse_declared_slots(metadata))


def policy_mode(agent: Any) -> str:
    raw = getattr(agent, "communication_policy", None)
    if raw is None and isinstance(agent, dict):
        raw = (
            agent.get("communication_policy")
            or agent.get("reception_policy")
            or agent.get("policy")
        )
    if isinstance(raw, dict):
        mode = raw.get("mode")
        return str(mode).lower() if mode else "open"
    if isinstance(raw, str) and raw:
        return raw.lower()
    return "open"


def normalize_invoke_slots(raw: list[Any] | None) -> list[dict[str, str]]:
    """Validate a PATCH body and return platform-owned contracts.

    Empty list clears. Unknown / duplicate ids are rejected (not silently dropped)
    so callers cannot think they declared a slot the platform does not know.
    """
    if raw is None:
        raise SlotContractError("invoke_slots is required", reason="slot_required")
    if len(raw) > MAX_INVOKE_SLOTS:
        raise SlotContractError(
            f"At most {MAX_INVOKE_SLOTS} invoke slots",
            reason="too_many_slots",
            extra={"max": MAX_INVOKE_SLOTS},
        )
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        slot_id = ""
        if isinstance(item, str):
            slot_id = normalize_slot_id(item)
        elif isinstance(item, dict):
            slot_id = normalize_slot_id(item.get("id") or item.get("slot"))
        else:
            raise SlotContractError("Each slot must be {id}", reason="invalid_slot_shape")
        spec = require_platform_slot(slot_id)
        if spec["id"] in seen:
            raise SlotContractError(
                "Duplicate invoke slot",
                reason="duplicate_slot",
                extra={"slot": spec["id"]},
            )
        seen.add(spec["id"])
        out.append(spec)
    return out


def _agent_id(agent: Any) -> str:
    return str(getattr(agent, "agent_id", None) or agent.get("agent_id") or "")


def list_slot_candidates(
    agents: list[Any],
    *,
    slot_id: str,
    alive_ids: set[str],
    caller_kind: str,
    preferred: str | None = None,
    allowed_ids: set[str] | None = None,
) -> list[Any]:
    """Ordered same-slot candidates (D20/D26): online first, then agent_id.

    Host path without ``allowed_ids`` only auto-includes ``open``.
    Host path with ``allowed_ids`` (D66) stays inside that set and does
    **not** re-filter by ACN ``open`` — Host already did the human ACL.
    Agent path skips ``closed``.
    ``preferred`` (when present and already in the list) is moved to the front.
    """
    declarers = [a for a in agents if agent_declares_slot(a, slot_id)]
    if allowed_ids is not None:
        declarers = [a for a in declarers if _agent_id(a) in allowed_ids]
    elif caller_kind == "host":
        declarers = [a for a in declarers if policy_mode(a) in ("open", "")]
    else:
        declarers = [a for a in declarers if policy_mode(a) != "closed"]
    declarers.sort(key=lambda a: (0 if _agent_id(a) in alive_ids else 1, _agent_id(a)))
    if preferred:
        pref = [a for a in declarers if _agent_id(a) == preferred]
        rest = [a for a in declarers if _agent_id(a) != preferred]
        return pref + rest
    return declarers


def pick_slot_provider(
    agents: list[Any],
    *,
    slot_id: str,
    alive_ids: set[str],
    caller_kind: str,
) -> Any | None:
    """Deterministic pick: first of :func:`list_slot_candidates`."""
    candidates = list_slot_candidates(
        agents, slot_id=slot_id, alive_ids=alive_ids, caller_kind=caller_kind
    )
    return candidates[0] if candidates else None
