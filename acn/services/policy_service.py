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

from collections.abc import Awaitable, Callable
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
# Phase 2 prototype PR #2 expansion (Group B #3):
# - ``allowlist`` — sender-aware divert: senders in the recipient's
#   ``agent_allowlist`` row set go to inbox; everyone else diverts
#   to the manifest queue (the same path manifest mode established
#   in PR #1). Membership is stored in a separate PG table
#   (``agent_allowlist`` — see infrastructure/persistence/postgres/
#   models.py:AgentAllowlistModel) — *not* inside the policy dict
#   itself. Keeping members out of the policy JSONB preserves the
#   strict-keys schema below (only ``mode`` / ``reject_reason`` are
#   accepted in the dict; arbitrary half-baked keys are still
#   422'd) and lets the relational layer cascade-clean on agent
#   unregistration.
SUPPORTED_POLICY_MODES = frozenset({"open", "closed", "manifest", "allowlist"})

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

    Phase 1 + Phase 2 PR #1 + PR #2 accepted shape::

        {"mode": "open" | "closed" | "manifest" | "allowlist",
         "reject_reason"?: str (≤ MAX_REJECT_REASON_LEN chars)}

    Note that ``mode=manifest`` and ``mode=allowlist`` do NOT take
    any extra config in the policy dict (no per-agent summary
    length cap, no per-agent TTL, no inline member list): those
    constants live in ``services/manifest_service.py`` and are
    intentionally globally tuned in Phase 2 to avoid each agent
    owner needing to discover a safe default; allowlist members
    live in the relational ``agent_allowlist`` table (see
    AllowlistService). The strict-keys check below still rejects
    raw ``allowlist: [...]`` / ``manifest_threshold`` /
    ``attention_fee`` etc. inline — preserving "policy is the
    explicit contract you opted into" against drive-by additions.

    ``reject_reason`` is reused across modes as an optional human
    label (manifest queue UI, allowlist denial 403 body).

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

    Phase 1 + Phase 2 (PR #1 + PR #2) supports four modes:

    * ``open``       — accept and route to inbox (legacy default; the
      field is backfilled to ``{"mode": "open"}`` on every ``Agent``
      instance, so an absent policy lands here too).
    * ``closed``     — reject with ``reason="policy_closed"``.
    * ``manifest``   — accept but route to the manifest queue
      (``decision.route_to == "manifest"``). The router writes a
      summary entry and pushes a WS notification; full content is
      pulled on demand by the recipient.
    * ``allowlist``  — accept-and-route based on sender membership
      in the recipient's relational allowlist (``agent_allowlist``
      table, see PR #2). Senders on the list go to inbox; everyone
      else diverts to manifest. The membership check happens via
      an injected async callback (``is_in_allowlist`` parameter on
      ``check_inbound``) — keeping this service free of repository
      handles preserves its "pure logic" property: no IO except
      what the caller explicitly threads in. See PR #2 plan P0-2
      decision for the why.

      **Empty allowlist semantics**: if the recipient's allowlist
      is empty the decision is "divert to manifest", NOT "fail
      open" or "fail closed-as-rejection". This matches the user
      intent of allowlist mode ("only people I explicitly trust go
      straight to my inbox; the rest goes to a queue I check
      periodically") and avoids two failure modes that were
      considered:

      - Treating empty as "block all" would silently rejection-bomb
        senders on the day after the recipient flipped to allowlist
        mode, which is the worst possible UX for a fresh adopter.
      - Treating empty as "open" defeats the security purpose.

      Diverting matches PR #1's manifest semantics (graceful
      degradation, non-loss).

      **Failure modes** (PR #2 plan P0-3): when ``is_in_allowlist``
      raises (Redis outage, PG outage), this service does NOT
      surface the exception to the caller as an error response.
      Instead it logs the failure and **fail-closes to the
      manifest queue**: the message is preserved (recipient can
      still pull from manifest) but doesn't bypass the trust check
      it should have failed. The alternative (fail-open to inbox)
      would let an adversary deny-of-service the cache and
      promote-bomb the recipient's inbox; the alternative
      (reject-all) would lose messages. Manifest divert is the
      least-bad failure mode.

    Any other mode value (typo, future mode arriving from a
    forward client) is treated as **fail-closed** rejection. Same
    rationale as before: misconfiguration must be loudly visible,
    not silently allowed.

    The ``message_meta`` keyword-only parameter is reserved so
    Phase 3 (``fee_gated`` attention_fee minimum) can extend the
    contract without breaking call sites.

    **Async-ness**: ``check_inbound`` is now ``async`` because the
    ``allowlist`` branch awaits the membership callback. ``open``
    / ``closed`` / ``manifest`` paths still return synchronously
    via ``async def`` but cost no IO; the marginal coroutine
    overhead is irrelevant against the per-message router work.
    """

    async def check_inbound(
        self,
        sender_id: str,
        recipient_id: str,
        recipient_policy: dict[str, Any] | None,
        *,
        message_meta: dict[str, Any] | None = None,
        is_in_allowlist: Callable[[str, str], Awaitable[bool]] | None = None,
        shared_subnet_ids: set[str] | None = None,
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
            message_meta: Reserved for Phase 3 extensions
                (``fee_gated`` attention minimum).
            is_in_allowlist: Async callback wired by the router /
                subnet layer (typically
                ``AllowlistService.is_member``). Required for
                ``mode=allowlist``; ignored for other modes.
                Injected (rather than holding a service handle on
                self) so this class has no IO dependencies — keeps
                unit tests trivial and lets non-router callers
                supply a stub.
            shared_subnet_ids: Pre-computed set of non-reserved subnet
                IDs that both sender and recipient share. When
                non-empty, agents in ``manifest`` or ``allowlist``
                mode treat the sender as implicitly trusted and route
                to inbox (normal delivery) instead of manifest. The
                ``public`` and ``system`` subnets are excluded by the
                caller so membership in those does not grant implicit
                trust. ``None`` or empty set means no shared subnets.

        Returns:
            ``PolicyDecision``. This method never raises for policy
            reasons; callers needing short-circuit semantics
            (router, subnet manager) use ``check_inbound_or_raise``.
        """
        del message_meta  # reserved for Phase 3.

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
            if shared_subnet_ids:
                logger.debug(
                    "policy_subnet_trust_bypass",
                    sender_id=sender_id,
                    recipient_id=recipient_id,
                    shared_subnets=sorted(shared_subnet_ids),
                )
                return PolicyDecision(allow=True, route_to="inbox")
            return PolicyDecision(allow=True, route_to="manifest")

        if mode == "allowlist":
            # Subnet co-membership grants implicit trust (checked before
            # the explicit allowlist so we skip the IO call when possible).
            if shared_subnet_ids:
                logger.debug(
                    "policy_subnet_trust_bypass",
                    sender_id=sender_id,
                    recipient_id=recipient_id,
                    shared_subnets=sorted(shared_subnet_ids),
                )
                return PolicyDecision(allow=True, route_to="inbox")

            # The router is responsible for wiring
            # ``is_in_allowlist``; if it didn't, we cannot safely
            # decide — treat as configuration error and fail-closed
            # to manifest (same direction as the IO-failure path
            # below; never lose a message).
            if is_in_allowlist is None:
                logger.error(
                    "policy_allowlist_callback_missing",
                    recipient_id=recipient_id,
                    sender_id=sender_id,
                )
                return PolicyDecision(allow=True, route_to="manifest")

            try:
                is_member = await is_in_allowlist(recipient_id, sender_id)
            except Exception as exc:  # noqa: BLE001
                # PR #2 plan P0-3: fail-closed to manifest. We log
                # at WARNING (operationally interesting but
                # recoverable) rather than ERROR — repeated logs at
                # ERROR would page on-call for a Redis blip that
                # the next is_member miss would heal.
                logger.warning(
                    "policy_allowlist_check_failed_fail_closed_manifest",
                    recipient_id=recipient_id,
                    sender_id=sender_id,
                    error=str(exc),
                )
                return PolicyDecision(allow=True, route_to="manifest")

            if is_member:
                return PolicyDecision(allow=True, route_to="inbox")
            # Sender is not on the allowlist — divert to manifest.
            # See class docstring for the empty-allowlist semantics.
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

    async def check_inbound_or_raise(
        self,
        sender_id: str,
        recipient_id: str,
        recipient_policy: dict[str, Any] | None,
        *,
        message_meta: dict[str, Any] | None = None,
        is_in_allowlist: Callable[[str, str], Awaitable[bool]] | None = None,
        shared_subnet_ids: set[str] | None = None,
    ) -> None:
        """Raise ``PolicyRejected`` if the recipient's policy denies the message.

        Convenience wrapper used by call sites that want short-circuit
        semantics (skip inbox/DLQ write, propagate rejection upward).

        Note that ``allowlist`` mode never reaches the raise branch:
        non-members divert to manifest (``allow=True``,
        ``route_to="manifest"``) rather than reject. So this helper
        is only useful for ``open`` / ``closed`` paths today.
        """
        decision = await self.check_inbound(
            sender_id=sender_id,
            recipient_id=recipient_id,
            recipient_policy=recipient_policy,
            message_meta=message_meta,
            is_in_allowlist=is_in_allowlist,
            shared_subnet_ids=shared_subnet_ids,
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
