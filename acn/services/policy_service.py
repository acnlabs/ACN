"""Communication Policy Check Service

Pure-logic gateway-level access control.

This service is the single source of truth for whether an inbound
message is allowed to reach a recipient agent. It is invoked from the
two real downstream sinks for inbound traffic:

* ``MessageRouter.route()`` — covers HTTP send/broadcast (public and
  internal), the A2A protocol entry point's ``route`` / ``broadcast``
  actions, and DLQ retry. All five paths converge on
  ``MessageRouter.route``, so installing the check there is sufficient
  for HTTP-derived traffic.
* ``SubnetManager.forward_request()`` — covers the WebSocket-pushed
  delivery path used by subnet-attached agents, which bypasses
  ``MessageRouter`` entirely.

Why a fully type-decoupled API:
- The router holds an ``AgentInfo`` (Pydantic) for endpoint discovery;
  the subnet manager holds an ``Agent`` entity (dataclass). Forcing
  one concrete type at the service boundary would require an
  unnecessary conversion at the other call site.
- Taking ``(sender_id, recipient_id, recipient_policy)`` keeps the
  service a true pure function — every branch is a unit test with no
  Redis / Postgres / Pydantic fixture.

See docs/features/acn-communication-economic-model.md
"Phase 1 网关执行点决策" for the full design rationale.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog  # type: ignore[import-untyped]

from ..core.exceptions import PolicyRejected

logger = structlog.get_logger()


# Reserved namespace prefix for ACN-trusted backend services. Senders
# whose ``sender_id`` starts with this prefix bypass policy entirely;
# legitimacy is already established upstream by the
# ``X-Internal-Token`` + ``assert_system_caller`` pair on
# ``POST /communication/internal/send``. Centralising the constant here
# keeps the exemption rule single-sourced.
SYSTEM_SENDER_PREFIX = "system:"


# Phase 1 supported modes. Kept as a module-level frozenset so the
# input validator (``validate_policy_dict``) and the runtime check
# (``check_inbound``) cannot drift apart — e.g. accidentally adding
# a new mode to validation without wiring the corresponding decision
# branch would otherwise pass schema review and silently fail-closed
# at runtime.
# Phase 2 prototype PR #1 expansion (Group A #1):
# - ``manifest`` — accept-but-divert: messages are stashed in the
#   manifest queue (``acn:manifest:{<agent_id>}`` ZSET) and the agent
#   gets a WS notification with sender_id + summary. Full content is
#   pulled on demand via ``GET /communication/content/{mid}``.
#
# Phase 2 prototype PR #2 will add ``allowlist`` mode (gates on a
# relational allow set; membership stored in a separate PG table —
# *not* inside the policy dict). It is intentionally absent from
# the validator until PR #2 is wired so users cannot store half-baked
# allowlist policies that activate on upgrade.
SUPPORTED_POLICY_MODES = frozenset({"open", "closed", "manifest"})

# Cap on user-supplied ``reject_reason`` strings. Long enough for a
# short human explanation ("on vacation until 2026-05") but short
# enough to not let an attacker exfiltrate large payloads through
# the public 403 response body. Aligns with typical agent metadata
# field caps elsewhere in the codebase (description=500).
MAX_REJECT_REASON_LEN = 200


def validate_policy_dict(value: Any) -> dict[str, Any] | None:
    """Validate a user-supplied ``communication_policy`` dict.

    Used by ``AgentRegisterRequest`` / ``AgentJoinRequest`` /
    ``PATCH /agents/{id}/policy`` so all three entry points enforce
    the same schema and produce identical error messages. Living
    here (alongside ``check_inbound``) prevents validator-vs-runtime
    drift — adding a new mode requires touching this file in two
    aligned places.

    Phase 1 + Phase 2 PR #1 accepted shape::

        {"mode": "open" | "closed" | "manifest",
         "reject_reason"?: str (≤ MAX_REJECT_REASON_LEN chars)}

    Note that ``mode=manifest`` does NOT take any extra config in the
    policy dict (no per-agent summary length cap, no per-agent TTL):
    those constants live in ``services/manifest_service.py`` and are
    intentionally globally tuned in Phase 2 to avoid each agent owner
    needing to discover a safe default. ``reject_reason`` is reused
    as an optional human label for the manifest queue UI.

    Returns:
        ``None`` if ``value`` is None (lets the caller decide whether
        to backfill the default elsewhere); otherwise the validated
        dict. We deliberately don't auto-fill ``mode`` here so the
        caller can distinguish "user didn't set policy" from "user
        explicitly chose open" if they ever need to.

    Raises:
        ValueError with a short, stable message describing the first
        violation. The string is exposed to clients via Pydantic's
        422 response body, so it must NOT include sensitive context.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("communication_policy must be a JSON object")

    # Strict schema in Phase 1: reject unknown top-level keys.
    # Reasoning: Phase 2/3 will add ``manifest`` / ``fee_gated`` and
    # related keys. If we silently accept arbitrary keys today,
    # callers will store half-baked Phase 2/3 config that suddenly
    # activates on upgrade — breaking the "policy is the explicit
    # contract you opted into" guarantee. Rejecting here forces a
    # conscious upgrade.
    allowed_keys = {"mode", "reject_reason"}
    extra_keys = set(value) - allowed_keys
    if extra_keys:
        # Sort for deterministic error messages — easier to test &
        # easier to read in audit logs.
        raise ValueError(
            f"communication_policy contains unsupported key(s): {sorted(extra_keys)}. "
            f"Phase 1 supports {sorted(allowed_keys)}"
        )

    mode = value.get("mode")
    if mode is None:
        raise ValueError("communication_policy.mode is required")
    if not isinstance(mode, str):
        raise ValueError("communication_policy.mode must be a string")
    if mode not in SUPPORTED_POLICY_MODES:
        raise ValueError(
            f"communication_policy.mode must be one of {sorted(SUPPORTED_POLICY_MODES)}, "
            f"got {mode!r}"
        )

    reject_reason = value.get("reject_reason")
    if reject_reason is not None:
        if not isinstance(reject_reason, str):
            raise ValueError("communication_policy.reject_reason must be a string")
        if len(reject_reason) > MAX_REJECT_REASON_LEN:
            raise ValueError(
                f"communication_policy.reject_reason must be ≤ "
                f"{MAX_REJECT_REASON_LEN} characters"
            )

    # Return a fresh dict so accidental caller mutation doesn't
    # propagate back into the request payload.
    out: dict[str, Any] = {"mode": mode}
    if reject_reason is not None:
        out["reject_reason"] = reject_reason
    return out


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome of a policy check.

    ``allow=True`` callers ignore ``reason`` / ``reject_reason`` but
    MUST honour ``route_to``. ``allow=False`` callers surface
    ``reason`` to metrics/audit and may surface ``reject_reason`` (the
    recipient's free-form explanation) to the sender.

    Phase 2 (Group A #4) introduces ``route_to``:

    * ``"inbox"`` (or None) — the historical default; deliver to the
      recipient's inbox (and push WS via the agent_message channel).
    * ``"manifest"`` — divert to the manifest queue; only sender +
      summary are visible until the recipient explicitly pulls
      content. Set by ``mode=manifest`` and by ``mode=allowlist`` when
      the sender is *not* in the recipient's allowlist.

    ``route_to`` is only meaningful when ``allow=True``.
    """

    allow: bool
    reason: str | None = None
    reject_reason: str | None = None
    route_to: str | None = None


class PolicyCheckService:
    """Decide whether an inbound message is allowed to reach the recipient.

    Phase 1 + Phase 2 PR #1 supports three modes:

    * ``open``     — accept and route to inbox (legacy default; the
      field is backfilled to ``{"mode": "open"}`` on every ``Agent``
      instance, so an absent policy lands here too).
    * ``closed``   — reject with ``reason="policy_closed"``.
    * ``manifest`` — accept but route to the manifest queue
      (``decision.route_to == "manifest"``). The router writes a
      summary entry and pushes a WS notification; full content is
      pulled on demand by the recipient.

    Any other mode value (e.g. ``allowlist`` arriving before Phase 2
    PR #2 is shipped, or a typo) is treated as **fail-closed** —
    rejected with ``reason="policy_unknown_mode"`` and a warning log.
    Rationale:

    * Unknown modes almost always mean a misconfiguration, and
      silently accepting would hide it; the agent owner notices
      immediately when their own test sends start failing.
    * Forward-compatibility (e.g. ``allowlist`` shipping from a
      future client before the server upgrade) is handled via
      versioned releases, not via fail-open semantics — the server
      upgrade installs the new branch in this method.

    The ``message_meta`` keyword-only parameter is unused in Phase 1
    but reserved so Phase 2/3 (``manifest`` size threshold,
    ``fee_gated`` attention_fee minimum) can extend the contract
    without breaking call sites.
    """

    def check_inbound(
        self,
        sender_id: str,
        recipient_id: str,
        recipient_policy: dict[str, Any] | None,
        *,
        message_meta: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        """Return the access decision for ``sender_id`` -> ``recipient_id``.

        Args:
            sender_id: ACN agent id of the sender, or a reserved
                ``system:<slug>`` namespace value for ACN-internal
                callers (which bypass policy entirely).
            recipient_id: ACN agent id of the recipient. Used only for
                logging / audit attribution; the actual decision comes
                from ``recipient_policy``.
            recipient_policy: The recipient's ``communication_policy``
                dict. ``None`` is treated as ``{"mode": "open"}`` to
                preserve the legacy default for agents that predate
                Step 1 of the rollout.
            message_meta: Reserved for Phase 2/3 extensions
                (``manifest`` size threshold, ``fee_gated`` attention
                minimum). Phase 1 ignores it; keyword-only so callers
                cannot positionally bind to it accidentally.

        Returns:
            ``PolicyDecision``. This method never raises for policy
            reasons; callers needing short-circuit semantics
            (router, subnet manager) use ``check_inbound_or_raise``.
        """
        del message_meta  # Phase 1 ignores; reserved for Phase 2/3.

        if sender_id.startswith(SYSTEM_SENDER_PREFIX):
            return PolicyDecision(allow=True)

        # ``Agent.__post_init__`` guarantees ``communication_policy`` is
        # a dict containing a ``mode`` key, but we still read defensively
        # in case an older row read directly from Redis bypasses the
        # entity (e.g. ``AgentRegistry`` paths predating Step 2.2).
        policy = recipient_policy or {"mode": "open"}
        mode = policy.get("mode", "open")

        if mode == "open":
            return PolicyDecision(allow=True, route_to="inbox")

        if mode == "closed":
            return PolicyDecision(
                allow=False,
                reason="policy_closed",
                reject_reason=policy.get("reject_reason"),
            )

        if mode == "manifest":
            # Accept-but-divert. ``allow=True`` so the router doesn't
            # increment ``acn_messages_rejected_total`` (these aren't
            # rejections from the sender's perspective — the message
            # is accepted, just stashed in a low-attention queue), and
            # ``route_to="manifest"`` instructs the router to skip the
            # inbox write and call ManifestService.write instead.
            return PolicyDecision(allow=True, route_to="manifest")

        logger.warning(
            "policy_unknown_mode",
            recipient_id=recipient_id,
            mode=mode,
        )
        return PolicyDecision(
            allow=False,
            reason="policy_unknown_mode",
            reject_reason=policy.get("reject_reason"),
        )

    def check_inbound_or_raise(
        self,
        sender_id: str,
        recipient_id: str,
        recipient_policy: dict[str, Any] | None,
        *,
        message_meta: dict[str, Any] | None = None,
    ) -> None:
        """Raise ``PolicyRejected`` if the recipient's policy denies the message.

        Convenience wrapper used by call sites that want short-circuit
        semantics (skip inbox/DLQ write, propagate rejection upward).
        """
        decision = self.check_inbound(
            sender_id=sender_id,
            recipient_id=recipient_id,
            recipient_policy=recipient_policy,
            message_meta=message_meta,
        )
        if decision.allow:
            return
        # ``reason`` is non-None on every reject branch above, so the
        # cast to ``str`` is safe; we keep the assert for a clear early
        # crash if someone adds a new reject branch and forgets to set it.
        assert decision.reason is not None, (
            "PolicyDecision(allow=False) must always carry a reason"
        )
        raise PolicyRejected(
            reason=decision.reason,
            reject_reason=decision.reject_reason,
            recipient_id=recipient_id,
        )
