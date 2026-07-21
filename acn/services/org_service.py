"""Org Harness service — Kernel + minimal work + thin Loop (ADR-0014)."""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

import structlog  # type: ignore[import-untyped]

from ..core.entities.org import (
    Org,
    OrgMembership,
    OrgOwner,
    OrgPrincipal,
    OrgWorkItem,
    WorkStatus,
)
from ..core.entities.subnet import Subnet
from ..core.exceptions import (
    AgentNotFoundException,
    OrgSubnetBindingConflictError,
    SubnetNotFoundException,
)
from ..core.interfaces.org_repository import IOrgRepository
from ..protocols.ap2 import WebhookEventType
from ..protocols.ap2.webhook import WebhookService
from .agent_service import AgentService
from .subnet_service import SubnetService

logger = structlog.get_logger()

CallerType = Literal["human", "agent"]


class OrgNotFoundError(Exception):
    def __init__(self, org_id: str) -> None:
        self.org_id = org_id
        super().__init__(f"Org not found: {org_id}")


class OrgPermissionError(Exception):
    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        super().__init__(message or reason)


class OrgConflictError(Exception):
    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        super().__init__(message or reason)


class OrgMembershipNotFoundError(Exception):
    def __init__(self, org_id: str, agent_id: str) -> None:
        self.org_id = org_id
        self.agent_id = agent_id
        super().__init__(f"Membership not found: {org_id}/{agent_id}")


class OrgWorkNotFoundError(Exception):
    def __init__(self, org_id: str, work_id: str) -> None:
        self.org_id = org_id
        self.work_id = work_id
        super().__init__(f"Work item not found: {org_id}/{work_id}")


class OrgService:
    def __init__(
        self,
        org_repository: IOrgRepository,
        subnet_service: SubnetService,
        agent_service: AgentService,
        webhook_service: WebhookService | None = None,
    ) -> None:
        self.repository = org_repository
        self.subnet_service = subnet_service
        self.agent_service = agent_service
        self.webhook_service = webhook_service

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _is_created_by(self, org: Org, caller_type: CallerType, caller_sub: str) -> bool:
        return (
            org.created_by.kind == caller_type
            and org.created_by.subject == caller_sub
        )

    def _is_owner(self, org: Org, caller_type: CallerType, caller_sub: str) -> bool:
        if org.owner.kind == "none":
            return False
        if org.owner.kind == "human" and caller_type == "human":
            return org.owner.subject == caller_sub
        if org.owner.kind == "agent" and caller_type == "agent":
            return org.owner.subject == caller_sub
        return False

    def _require_governance(
        self,
        org: Org,
        caller_type: CallerType,
        caller_sub: str,
        *,
        allow_created_by_when_none: bool = True,
    ) -> None:
        if org.status == "dissolved":
            raise OrgPermissionError("org_dissolved", "Org is dissolved")
        if org.owner.kind == "none":
            if allow_created_by_when_none and self._is_created_by(
                org, caller_type, caller_sub
            ):
                return
            raise OrgPermissionError(
                "created_by_only",
                "Only created_by may govern an unclaimed Org",
            )
        if self._is_owner(org, caller_type, caller_sub):
            return
        raise OrgPermissionError("ownership_mismatch", "Caller is not Org owner")

    # ------------------------------------------------------------------
    # Webhooks
    # ------------------------------------------------------------------

    async def _emit(
        self,
        org: Org,
        event: WebhookEventType,
        data: dict[str, Any],
    ) -> None:
        if not self.webhook_service:
            return
        try:
            subnet = await self.subnet_service.get_subnet(org.subnet_id)
        except SubnetNotFoundException:
            logger.warning(
                "org_webhook_skip_no_subnet",
                org_id=org.org_id,
                subnet_id=org.subnet_id,
                event=event.value,
            )
            return
        if not subnet.harness_url:
            return
        try:
            await self.webhook_service.send_to(
                url=subnet.harness_url,
                secret=subnet.harness_secret,
                event=event,
                task_id=org.org_id,
                data={"org_id": org.org_id, "subnet_id": org.subnet_id, **data},
                outbox=False,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "org_webhook_delivery_failed",
                org_id=org.org_id,
                event=event.value,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Create / get
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_subnet_slug(display_name: str) -> str:
        slug = re.sub(r"[^a-z0-9-]+", "-", display_name.lower()).strip("-")[:32] or "org"
        return f"org-{slug}-{secrets.token_hex(3)}"

    async def _require_steward_authorized(
        self,
        *,
        caller_type: CallerType,
        caller_sub: str,
        steward: str,
    ) -> None:
        """ADR-0014 D3: human may only use a human-owned steward agent."""
        try:
            agent = await self.agent_service.get_agent(steward)
        except AgentNotFoundException as e:
            raise ValueError(f"steward agent not found: {steward}") from e
        if caller_type == "agent":
            if steward != caller_sub:
                raise OrgPermissionError(
                    "steward_mismatch",
                    "Agent callers must use themselves as steward",
                )
            return
        # human / internal treated as human principal
        if agent.owner != caller_sub:
            raise OrgPermissionError(
                "steward_not_owned",
                "steward_agent_id must be an agent owned by the caller",
            )

    async def create_org(
        self,
        *,
        display_name: str,
        caller_type: CallerType,
        caller_sub: str,
        steward_agent_id: str | None = None,
        charter: dict[str, Any] | None = None,
        subnet_id: str | None = None,
        join_policy: Literal["open", "approval"] = "open",
        is_private: bool = False,
        plugins: dict[str, str] | None = None,
        harness_url: str | None = None,
        harness_secret: str | None = None,
    ) -> Org:
        if caller_type == "agent":
            steward = caller_sub
        else:
            if not steward_agent_id:
                raise ValueError("steward_agent_id required when human creates Org")
            steward = steward_agent_id

        await self._require_steward_authorized(
            caller_type=caller_type,
            caller_sub=caller_sub,
            steward=steward,
        )

        if is_private and join_policy == "open":
            join_policy = "approval"

        slug = subnet_id or self._generate_subnet_slug(display_name)
        try:
            existing = await self.subnet_service.get_subnet(slug)
        except SubnetNotFoundException:
            existing = None

        created_subnet = False
        if existing is not None:
            if existing.owner != steward:
                raise OrgConflictError(
                    "subnet_owner_mismatch",
                    f"Subnet '{slug}' exists but is not owned by steward",
                )
            bound = await self.repository.find_org_by_subnet(slug)
            if bound is not None and bound.status != "dissolved":
                raise OrgConflictError(
                    "subnet_already_bound",
                    f"Subnet '{slug}' is already bound to org {bound.org_id}",
                )
            subnet = existing
        else:
            subnet = await self.subnet_service.create_subnet(
                slug=slug,
                name=display_name,
                owner=steward,
                description=f"Org fence for {display_name}",
                is_private=is_private,
                join_policy=join_policy,
            )
            created_subnet = True
            try:
                await self.agent_service.join_subnet(steward, subnet.slug)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "org_steward_agent_join_failed",
                    steward=steward,
                    slug=subnet.slug,
                    exc_info=True,
                )

        org_id = f"org_{uuid4().hex}"
        now = datetime.now(UTC)
        org = Org(
            org_id=org_id,
            display_name=display_name,
            created_by=OrgPrincipal(kind=caller_type, subject=caller_sub),
            subnet_id=subnet.slug,
            steward_agent_id=steward,
            owner=OrgOwner(kind="none"),
            charter=charter or {},
            plugins=plugins
            or {"work": "minimal", "loop": "thin", "memory": "noop"},
            created_at=now,
            updated_at=now,
        )
        try:
            await self.repository.save_org(org)
            await self.repository.upsert_membership(
                OrgMembership(
                    org_id=org_id,
                    agent_id=steward,
                    role="manager",
                    status="active",
                )
            )
        except Exception as save_exc:
            if created_subnet:
                try:
                    await self.subnet_service.delete_subnet(subnet.slug, steward)
                except Exception:  # noqa: BLE001
                    logger.error(
                        "org_create_subnet_rollback_failed",
                        slug=subnet.slug,
                        org_id=org_id,
                        exc_info=True,
                    )
            try:
                await self.repository.delete_memberships_for_org(org_id)
                await self.repository.delete_org(org_id)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "org_create_row_rollback_failed",
                    org_id=org_id,
                    exc_info=True,
                )
            if isinstance(save_exc, OrgSubnetBindingConflictError):
                # Concurrent create won the fence claim after our
                # pre-check — same 409 surface as the pre-check path.
                raise OrgConflictError(
                    "subnet_already_bound",
                    str(save_exc),
                ) from save_exc
            raise

        if harness_url:
            try:
                await self.subnet_service.update_harness(
                    slug=subnet.slug,
                    owner=steward,
                    harness_url=harness_url,
                    harness_secret=harness_secret,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "org_create_harness_register_failed",
                    org_id=org_id,
                    slug=subnet.slug,
                    exc_info=True,
                )

        await self._emit(
            org,
            WebhookEventType.ORG_CREATED,
            {
                "display_name": org.display_name,
                "steward_agent_id": steward,
                "created_by": org.created_by.to_dict(),
            },
        )
        return org

    async def get_org(self, org_id: str) -> Org:
        org = await self.repository.find_org(org_id)
        if not org:
            raise OrgNotFoundError(org_id)
        return org

    # ------------------------------------------------------------------
    # Private-Org read ACL (aligned with subnet ACL V6 / issue #114)
    # ------------------------------------------------------------------

    async def _is_entitled_private_reader(
        self,
        org: Org,
        subnet: Subnet | None,
        caller_type: CallerType | None,
        caller_sub: str | None,
        *,
        admin: bool = False,
    ) -> bool:
        """True when the caller may read a private Org in full.

        Entitled principals (mirrors ``routes/subnets.py``
        ``_resolve_caller_access`` plus Org-native relationships):

        - internal token / ``acn:admin``
        - Org owner or ``created_by`` (human or agent)
        - the steward agent, the fence subnet owner, or any subnet member
        - any agent holding an **active** OrgMembership (covers degraded
          members not yet propagated into the fence)
        - a human who owns the steward agent (ownership-chain bridge)

        Deliberately narrow, matching subnet ACL V6 § 2: a human who owns
        a mere *member* (worker) agent is NOT entitled — membership is a
        collaboration edge and does not extend read trust upward to the
        member agent's holder. Such humans read via their agent's own
        API key.
        """
        if admin:
            return True
        if caller_type is None or not caller_sub:
            return False
        if self._is_owner(org, caller_type, caller_sub):
            return True
        if self._is_created_by(org, caller_type, caller_sub):
            return True
        if caller_type == "agent":
            if caller_sub == org.steward_agent_id:
                return True
            if subnet is not None and (
                caller_sub == subnet.owner or subnet.has_member(caller_sub)
            ):
                return True
            membership = await self.repository.find_membership(
                org.org_id, caller_sub
            )
            return membership is not None and membership.status == "active"
        # Human ownership-chain bridge: owning the steward agent grants read.
        try:
            steward = await self.agent_service.get_agent(org.steward_agent_id)
        except AgentNotFoundException:
            return False
        return steward.owner == caller_sub

    @staticmethod
    def _redacted_org_view(org: Org, *, fence_missing: bool) -> dict[str, Any]:
        """Minimal public projection of a private Org.

        Mirrors the ``SubnetStub`` philosophy: existence is not hidden
        (org_id is required to query at all), but ``charter``, ``plugins``,
        ``created_by``, owner subject, steward, subnet slug, and harness
        details are withheld from unauthorised readers.
        """
        return {
            "org_id": org.org_id,
            "status": org.status,
            "owner": {"kind": org.owner.kind},
            "private": True,
            "fencing": {"is_private": True, "missing": fence_missing},
        }

    async def ensure_private_readable(
        self,
        org_id: str,
        *,
        caller_type: CallerType | None = None,
        caller_sub: str | None = None,
        admin: bool = False,
    ) -> Org:
        """Gate member/work listings of a private Org.

        Raises ``OrgPermissionError("private_org")`` for unauthorised
        readers. A missing fence subnet is treated as private
        (conservative: we can no longer prove the fence was public).
        """
        org = await self.get_org(org_id)
        subnet: Subnet | None
        try:
            subnet = await self.subnet_service.get_subnet(org.subnet_id)
        except SubnetNotFoundException:
            subnet = None
        is_private = subnet.is_private if subnet is not None else True
        if not is_private:
            return org
        if await self._is_entitled_private_reader(
            org, subnet, caller_type, caller_sub, admin=admin
        ):
            return org
        raise OrgPermissionError(
            "private_org",
            "Org is bound to a private subnet; caller is not entitled to read it",
        )

    async def get_org_view(
        self,
        org_id: str,
        *,
        caller_type: CallerType | None = None,
        caller_sub: str | None = None,
        admin: bool = False,
    ) -> dict[str, Any]:
        """Org dict enriched with live fence / harness fields.

        Private-fence Orgs are redacted for unauthorised viewers (see
        ``_redacted_org_view``); public-fence Orgs are fully visible.
        """
        org = await self.get_org(org_id)
        view = org.to_dict()
        try:
            subnet = await self.subnet_service.get_subnet(org.subnet_id)
        except SubnetNotFoundException:
            if org.status == "active":
                org.status = "fence_missing"
                org.updated_at = datetime.now(UTC)
                await self.repository.save_org(org)
            # Fence gone → privacy no longer provable; restrict to
            # entitled readers (conservative default).
            if not await self._is_entitled_private_reader(
                org, None, caller_type, caller_sub, admin=admin
            ):
                return self._redacted_org_view(org, fence_missing=True)
            view["status"] = org.status
            view["harness_webhook"] = {"url": None, "registered": False}
            view["fencing"] = {
                **(view.get("fencing") or {}),
                "subnet_id": org.subnet_id,
                "missing": True,
            }
            return view

        if org.status == "fence_missing":
            org.status = "active"
            org.updated_at = datetime.now(UTC)
            await self.repository.save_org(org)
            view["status"] = "active"

        if subnet.is_private and not await self._is_entitled_private_reader(
            org, subnet, caller_type, caller_sub, admin=admin
        ):
            return self._redacted_org_view(org, fence_missing=False)

        view["harness_webhook"] = {
            "url": subnet.harness_url,
            "registered": bool(subnet.harness_url),
        }
        view["fencing"] = {
            "subnet_id": subnet.slug,
            "join_policy": subnet.join_policy,
            "is_private": subnet.is_private,
            "missing": False,
        }
        return view

    async def update_org(
        self,
        org_id: str,
        *,
        caller_type: CallerType,
        caller_sub: str,
        display_name: str | None = None,
        charter: dict[str, Any] | None = None,
        plugins: dict[str, str] | None = None,
    ) -> Org:
        org = await self.get_org(org_id)
        self._require_governance(org, caller_type, caller_sub)
        if display_name is not None:
            if not display_name.strip():
                raise ValueError("display_name cannot be empty")
            org.display_name = display_name.strip()
        if charter is not None:
            org.charter = charter
        if plugins is not None:
            merged = dict(org.plugins)
            merged.update(plugins)
            org.plugins = merged
        org.updated_at = datetime.now(UTC)
        await self.repository.save_org(org)
        return org

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------

    async def add_member(
        self,
        org_id: str,
        agent_id: str,
        *,
        caller_type: CallerType,
        caller_sub: str,
        role: str = "worker",
        reports_to: str | None = None,
    ) -> OrgMembership:
        org = await self.get_org(org_id)
        self._require_governance(org, caller_type, caller_sub)

        try:
            await self.agent_service.get_agent(agent_id)
        except AgentNotFoundException as e:
            raise ValueError(f"agent not found: {agent_id}") from e

        existing = await self.repository.find_membership(org_id, agent_id)
        if existing and existing.status == "active":
            raise OrgConflictError("already_member", f"{agent_id} already in org")

        # D4: subnet join first, then OrgMembership; compensate leave on fail.
        subnet = await self.subnet_service.get_subnet(org.subnet_id)
        already_subnet = subnet.has_member(agent_id)
        joined_now = False
        if not already_subnet:
            await self.subnet_service.add_member(org.subnet_id, agent_id)
            try:
                await self.agent_service.join_subnet(agent_id, org.subnet_id)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "org_member_agent_join_mirror_failed",
                    agent_id=agent_id,
                    slug=org.subnet_id,
                    exc_info=True,
                )
            joined_now = True

        membership = OrgMembership(
            org_id=org_id,
            agent_id=agent_id,
            role=role,
            reports_to=reports_to,
            status="active",
        )
        try:
            await self.repository.upsert_membership(membership)
        except Exception:
            if joined_now:
                try:
                    await self.subnet_service.remove_member(org.subnet_id, agent_id)
                    await self.agent_service.leave_subnet(agent_id, org.subnet_id)
                except Exception:  # noqa: BLE001
                    logger.error(
                        "org_member_compensate_leave_failed",
                        org_id=org_id,
                        agent_id=agent_id,
                        exc_info=True,
                    )
                    raise OrgConflictError(
                        "membership_sync_failed",
                        "Org membership write failed and subnet leave compensation failed",
                    ) from None
            raise

        await self._emit(
            org,
            WebhookEventType.ORG_MEMBER_ADDED,
            {"agent_id": agent_id, "role": role},
        )
        return membership

    async def remove_member(
        self,
        org_id: str,
        agent_id: str,
        *,
        caller_type: CallerType,
        caller_sub: str,
    ) -> OrgMembership:
        org = await self.get_org(org_id)
        self._require_governance(org, caller_type, caller_sub)

        membership = await self.repository.find_membership(org_id, agent_id)
        if not membership or membership.status != "active":
            raise OrgMembershipNotFoundError(org_id, agent_id)

        if agent_id == org.steward_agent_id and org.owner.kind == "none":
            raise OrgPermissionError(
                "cannot_remove_steward",
                "Cannot remove steward while Org has no owner",
            )

        membership.status = "inactive"
        await self.repository.upsert_membership(membership)

        try:
            await self.subnet_service.remove_member(org.subnet_id, agent_id)
            await self.agent_service.leave_subnet(agent_id, org.subnet_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "org_member_subnet_leave_failed",
                org_id=org_id,
                agent_id=agent_id,
                exc_info=True,
            )

        await self._emit(
            org,
            WebhookEventType.ORG_MEMBER_REMOVED,
            {"agent_id": agent_id},
        )
        return membership

    async def list_members(
        self, org_id: str, *, active_only: bool = True
    ) -> list[OrgMembership]:
        await self.get_org(org_id)
        return await self.repository.list_memberships(org_id, active_only=active_only)

    async def list_members_view(
        self, org_id: str, *, active_only: bool = True
    ) -> dict[str, Any]:
        """List members with subnet intersection / degraded flag (ADR-0014 D4.3)."""
        org = await self.get_org(org_id)
        members = await self.repository.list_memberships(
            org_id, active_only=active_only
        )
        fence_missing = False
        subnet_member_ids: set[str] = set()
        try:
            subnet = await self.subnet_service.get_subnet(org.subnet_id)
            subnet_member_ids = set(subnet.member_agent_ids)
        except SubnetNotFoundException:
            fence_missing = True
            if org.status == "active":
                org.status = "fence_missing"
                org.updated_at = datetime.now(UTC)
                await self.repository.save_org(org)

        items: list[dict[str, Any]] = []
        degraded_count = 0
        for m in members:
            in_subnet = m.agent_id in subnet_member_ids
            degraded = (m.status == "active") and (not in_subnet or fence_missing)
            if degraded:
                degraded_count += 1
            row = m.to_dict()
            row["acn"] = {
                "subnet_member": in_subnet,
                "degraded": degraded,
            }
            items.append(row)

        return {
            "org_id": org_id,
            "count": len(items),
            "degraded_count": degraded_count,
            "fence_missing": fence_missing,
            "members": items,
        }

    # ------------------------------------------------------------------
    # Ownership
    # ------------------------------------------------------------------

    async def _authorize_owner_designation(
        self,
        *,
        caller_type: CallerType,
        caller_sub: str,
        owner_kind: Literal["human", "agent"],
        owner_subject: str,
    ) -> None:
        """Prevent claim/transfer from designating an unauthorized agent owner."""
        if owner_kind == "human":
            if caller_type != "human" or owner_subject != caller_sub:
                raise OrgPermissionError(
                    "owner_subject_mismatch",
                    "human owner subject must equal the calling human",
                )
            return
        # owner_kind == agent
        try:
            agent = await self.agent_service.get_agent(owner_subject)
        except AgentNotFoundException as e:
            raise ValueError(f"owner agent not found: {owner_subject}") from e
        if caller_type == "agent":
            if owner_subject != caller_sub:
                raise OrgPermissionError(
                    "owner_subject_mismatch",
                    "agent callers may only claim/transfer as themselves",
                )
            return
        if agent.owner != caller_sub:
            raise OrgPermissionError(
                "owner_agent_not_owned",
                "designated owner agent must be owned by the calling human",
            )

    async def _align_subnet_owner(
        self,
        org: Org,
        *,
        new_owner_agent: str,
    ) -> tuple[bool, str]:
        """Transfer subnet to ``new_owner_agent`` if needed.

        Returns ``(transferred, previous_owner)`` for compensate-on-failure.
        """
        subnet = await self.subnet_service.get_subnet(org.subnet_id)
        previous = subnet.owner
        if previous == new_owner_agent:
            return False, previous
        await self.subnet_service.transfer_owner(
            org.subnet_id,
            current_owner=previous,
            new_owner=new_owner_agent,
        )
        org.steward_agent_id = new_owner_agent
        return True, previous

    async def _compensate_subnet_owner(
        self,
        org: Org,
        *,
        previous_owner: str,
        current_owner: str,
    ) -> None:
        try:
            await self.subnet_service.transfer_owner(
                org.subnet_id,
                current_owner=current_owner,
                new_owner=previous_owner,
            )
        except Exception:  # noqa: BLE001
            logger.error(
                "org_subnet_owner_compensate_failed",
                org_id=org.org_id,
                previous_owner=previous_owner,
                current_owner=current_owner,
                exc_info=True,
            )

    async def claim(
        self,
        org_id: str,
        *,
        caller_type: CallerType,
        caller_sub: str,
        owner_kind: Literal["human", "agent"] | None = None,
        owner_subject: str | None = None,
    ) -> Org:
        org = await self.get_org(org_id)
        if org.owner.kind != "none":
            raise OrgConflictError("already_owned", "Org already has an owner")
        if not self._is_created_by(org, caller_type, caller_sub):
            raise OrgPermissionError(
                "created_by_only",
                "Only created_by may claim an unclaimed Org",
            )

        kind: Literal["human", "agent"] = owner_kind or caller_type
        subject = owner_subject or caller_sub
        await self._authorize_owner_designation(
            caller_type=caller_type,
            caller_sub=caller_sub,
            owner_kind=kind,
            owner_subject=subject,
        )

        transferred = False
        previous_subnet_owner = ""
        if kind == "agent":
            transferred, previous_subnet_owner = await self._align_subnet_owner(
                org, new_owner_agent=subject
            )

        org.owner = OrgOwner(kind=kind, subject=subject)
        org.updated_at = datetime.now(UTC)
        try:
            await self.repository.save_org(org)
        except Exception:
            if transferred:
                await self._compensate_subnet_owner(
                    org,
                    previous_owner=previous_subnet_owner,
                    current_owner=subject,
                )
            raise

        await self._emit(
            org,
            WebhookEventType.ORG_OWNER_CHANGED,
            {"owner": org.owner.to_dict(), "action": "claim"},
        )
        return org

    async def transfer(
        self,
        org_id: str,
        *,
        caller_type: CallerType,
        caller_sub: str,
        new_owner_kind: Literal["human", "agent"],
        new_owner_subject: str,
    ) -> Org:
        org = await self.get_org(org_id)
        if org.owner.kind == "none":
            raise OrgPermissionError(
                "unclaimed",
                "Cannot transfer an unclaimed Org; claim first",
            )
        if not self._is_owner(org, caller_type, caller_sub):
            raise OrgPermissionError("ownership_mismatch", "Caller is not Org owner")

        # Transfer is intentional hand-off by the current owner. Agent targets
        # must exist; subnet aligns when the new owner is an agent.
        if new_owner_kind == "agent":
            try:
                await self.agent_service.get_agent(new_owner_subject)
            except AgentNotFoundException as e:
                raise ValueError(
                    f"owner agent not found: {new_owner_subject}"
                ) from e

        transferred = False
        previous_subnet_owner = ""
        if new_owner_kind == "agent":
            transferred, previous_subnet_owner = await self._align_subnet_owner(
                org, new_owner_agent=new_owner_subject
            )

        org.owner = OrgOwner(kind=new_owner_kind, subject=new_owner_subject)
        org.updated_at = datetime.now(UTC)
        try:
            await self.repository.save_org(org)
        except Exception:
            if transferred:
                await self._compensate_subnet_owner(
                    org,
                    previous_owner=previous_subnet_owner,
                    current_owner=new_owner_subject,
                )
            raise

        await self._emit(
            org,
            WebhookEventType.ORG_OWNER_CHANGED,
            {"owner": org.owner.to_dict(), "action": "transfer"},
        )
        return org

    async def release(
        self,
        org_id: str,
        *,
        caller_type: CallerType,
        caller_sub: str,
    ) -> Org:
        org = await self.get_org(org_id)
        if org.owner.kind == "none":
            raise OrgConflictError("already_unclaimed", "Org has no owner")
        if not self._is_owner(org, caller_type, caller_sub):
            raise OrgPermissionError("ownership_mismatch", "Caller is not Org owner")

        org.owner = OrgOwner(kind="none")
        org.updated_at = datetime.now(UTC)
        await self.repository.save_org(org)
        await self._emit(
            org,
            WebhookEventType.ORG_OWNER_CHANGED,
            {"owner": {"kind": "none"}, "action": "release"},
        )
        return org

    async def dissolve(
        self,
        org_id: str,
        *,
        caller_type: CallerType,
        caller_sub: str,
    ) -> Org:
        """Soft-dissolve: status flip only, by design.

        Memberships, work items, and the fence subnet are intentionally
        NOT cleaned up here — the subnet (and its members) survive so the
        same steward can rebind it to a new Org (``create_org`` accepts a
        subnet whose bound Org is dissolved), and rows stay for audit.
        Hard cleanup is the operator-path ``delete_org`` +
        ``delete_memberships_for_org`` / ``delete_work_for_org``.
        """
        org = await self.get_org(org_id)
        self._require_governance(org, caller_type, caller_sub)
        org.status = "dissolved"
        org.updated_at = datetime.now(UTC)
        await self.repository.save_org(org)
        await self._emit(org, WebhookEventType.ORG_DISSOLVED, {})
        return org

    # ------------------------------------------------------------------
    # Minimal work + thin Loop
    # ------------------------------------------------------------------

    async def create_work(
        self,
        org_id: str,
        *,
        title: str,
        caller_type: CallerType,
        caller_sub: str,
        assignee_agent_id: str | None = None,
    ) -> OrgWorkItem:
        org = await self.get_org(org_id)
        self._require_governance(org, caller_type, caller_sub)
        now = datetime.now(UTC)
        work = OrgWorkItem(
            work_id=f"work_{uuid4().hex[:16]}",
            org_id=org_id,
            title=title,
            assignee_agent_id=assignee_agent_id,
            status="todo",
            created_at=now,
            updated_at=now,
        )
        await self.repository.save_work(work)
        await self._emit(
            org,
            WebhookEventType.ORG_WORK_CREATED,
            work.to_dict(),
        )
        return work

    async def update_work_status(
        self,
        org_id: str,
        work_id: str,
        *,
        status: WorkStatus,
        caller_type: CallerType,
        caller_sub: str,
        assignee_agent_id: str | None = None,
    ) -> OrgWorkItem:
        org = await self.get_org(org_id)
        self._require_governance(org, caller_type, caller_sub)
        work = await self.repository.find_work(org_id, work_id)
        if not work:
            raise OrgWorkNotFoundError(org_id, work_id)
        work.status = status
        if assignee_agent_id is not None:
            work.assignee_agent_id = assignee_agent_id
        work.updated_at = datetime.now(UTC)
        await self.repository.save_work(work)
        await self._emit(
            org,
            WebhookEventType.ORG_WORK_UPDATED,
            work.to_dict(),
        )
        return work

    async def list_work(
        self, org_id: str, *, open_only: bool = False
    ) -> list[OrgWorkItem]:
        await self.get_org(org_id)
        return await self.repository.list_work(org_id, open_only=open_only)

    async def tick_loop(
        self,
        org_id: str,
        *,
        caller_type: CallerType,
        caller_sub: str,
    ) -> dict[str, Any]:
        """Thin Loop tick: list open work and emit org.loop_tick."""
        org = await self.get_org(org_id)
        self._require_governance(org, caller_type, caller_sub)
        if org.status != "active":
            raise OrgPermissionError("org_not_active", "Org Loop requires active status")

        open_work = await self.repository.list_work(org_id, open_only=True)
        payload = {
            "open_count": len(open_work),
            "work_ids": [w.work_id for w in open_work],
            "assignees": sorted(
                {w.assignee_agent_id for w in open_work if w.assignee_agent_id}
            ),
        }
        await self._emit(org, WebhookEventType.ORG_LOOP_TICK, payload)
        return payload
