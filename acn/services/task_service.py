"""Task Service

Business logic for task management, including AP2 payment integration.
"""

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

import structlog

from ..core.entities import Participation, ParticipationStatus, Task, TaskStatus
from ..core.exceptions import SubnetNotFoundException
from ..core.interfaces import (
    IAgentRepository,
    IEscrowProvider,
    ISettlementOutboxRepository,
    ISubnetRepository,
    ITaskRepository,
    IUnitOfWork,
    SettlementEvent,
)
from ..infrastructure.task_pool import TaskPool
from ..protocols.ap2 import PaymentTaskManager, WebhookEventType, WebhookService
from ..protocols.ap2.core import PLATFORM_CURRENCIES
from .activity_service import ActivityService
from .subnet_service import SubnetService

logger = structlog.get_logger()

# Namespace for ``event_id = uuid5(NS, f"{task_id}:{trigger}")``. Using a
# fixed URL-based namespace means the same (task_id, trigger) pair always
# resolves to the same event_id across processes/replicas — the outbox
# UNIQUE(event_id) constraint then guarantees at-most-once enqueue even
# if ``complete_task`` is retried by an upstream caller.
#
# DO NOT CHANGE THIS STRING.
# It's the seed for every event_id ever written to ``settlement_outbox``.
# Changing it on a v0.2 upgrade would cause new ``complete_task`` calls
# to mint event_ids that miss the existing UNIQUE(event_id) collisions
# with in-flight retried events — silently re-enqueueing the same
# (task_id, trigger) pair and causing the worker to settle twice.
#
# Forward-compatible evolution: when v0.2 needs to change the
# semantics of an event, do NOT touch this namespace. Instead, bump
# the TRIGGER string passed to ``uuid5`` (e.g.
# ``review_pass`` → ``review_pass_v2``). That changes the input to
# the hash without disturbing already-enqueued rows: legacy retries
# of ``review_pass`` events keep their event_ids; new ``review_pass_v2``
# events live in a disjoint id space. The namespace UUID is
# write-once, the trigger string is the actual versioning lever.
_OUTBOX_EVENT_NS = uuid5(NAMESPACE_URL, "https://acnlabs.dev/settlement-outbox/v0.1")


class TaskNotFoundException(Exception):
    """Task not found"""

    pass


class TaskService:
    """
    Task Service

    Orchestrates task-related business operations.
    Integrates with:
    - TaskPool for task discovery
    - AP2 PaymentTaskManager for payment handling
    - WebhookService for event notifications
    """

    def __init__(
        self,
        repository: ITaskRepository,
        task_pool: TaskPool | None = None,
        payment_manager: PaymentTaskManager | None = None,
        webhook_service: WebhookService | None = None,
        activity_service: ActivityService | None = None,
        escrow_client: IEscrowProvider | None = None,
        agent_repository: IAgentRepository | None = None,
        subnet_repository: ISubnetRepository | None = None,
        subnet_service: SubnetService | None = None,
        # Settlement saga v0.1 — keyword-only, all three OPTIONAL so
        # Redis-only / in-memory deployments and legacy test fixtures
        # constructed without saga wiring keep working unchanged. The
        # saga path activates only when ``settlement_outbox``,
        # ``unit_of_work`` and ``outbox_enqueue_required=True`` are all
        # satisfied; otherwise ``complete_task`` falls back to its
        # legacy non-atomic path — which IS the pre-v0.1 production
        # behavior, no warning needed.
        settlement_outbox: ISettlementOutboxRepository | None = None,
        unit_of_work: IUnitOfWork | None = None,
        outbox_enqueue_required: bool = True,
    ):
        """
        Initialize Task Service

        Args:
            repository: Task repository
            task_pool: Task pool (created if not provided)
            payment_manager: AP2 payment manager (optional)
            webhook_service: Webhook service (optional)
            activity_service: Activity service for recording events (optional)
            escrow_client: Labs escrow client for budget management (optional)
            agent_repository: Agent repository for looking up agent owners (optional)
            subnet_repository: Subnet repository for visibility/access control (optional)
            subnet_service: Subnet service used by the ADR-0003 Phase 3
                ``task_scoped`` cascade hook (``complete_task`` /
                ``reject_task`` / ``cancel_task`` dissolve subnets
                whose ``linked_task_id == task_id``). Optional — when
                ``None`` the cascade is silently skipped so legacy
                test fixtures keep working. Production wiring in
                ``api.py`` always supplies one.
            settlement_outbox: Outbox repository for the settlement saga
                (v0.1). When None, ``complete_task`` uses its legacy
                non-atomic path — see docstring there.
            unit_of_work: Transaction boundary used to atomically commit
                the CAS save + outbox enqueue. When None, saga is off.
            outbox_enqueue_required: Emergency lever. When False, the
                saga path is force-disabled at runtime regardless of
                the two dependencies above. Production should always
                run True; the flag exists so an on-call operator can
                disarm the new write path without a redeploy.
        """
        self.repository = repository
        self.task_pool = task_pool or TaskPool(repository)
        self.payment_manager = payment_manager
        self.webhook = webhook_service
        self.activity = activity_service
        self.escrow = escrow_client
        self.agent_repository = agent_repository
        self.subnet_repository = subnet_repository
        self.subnet_service = subnet_service
        # Saga wiring — see attribute docstrings on each, and the
        # decision matrix on ``_saga_enabled`` below.
        self.settlement_outbox = settlement_outbox
        self.unit_of_work = unit_of_work
        self.outbox_enqueue_required = outbox_enqueue_required

    @property
    def _saga_enabled(self) -> bool:
        """Whether ``complete_task`` should run the atomic CAS+enqueue
        path on this call.

        All three must hold:
        - ``settlement_outbox`` injected (PG-mode deployment)
        - ``unit_of_work`` injected (PG-mode deployment)
        - ``outbox_enqueue_required=True`` (no emergency disarm)

        Read on every call so flipping the env var mid-run is honored
        without restart for the next invocation. We don't memoize.
        """
        return (
            self.settlement_outbox is not None
            and self.unit_of_work is not None
            and self.outbox_enqueue_required
        )

    async def create_task(
        self,
        creator_type: str,
        creator_id: str,
        creator_name: str,
        title: str,
        description: str,
        task_type: str = "general",
        required_tags: list[str] | None = None,
        reward: str = "0",
        reward_currency: str = "credits",
        max_participants: int | None = 1,
        completion_mode: str = "independent",
        auto_approve: bool = False,
        require_join_approval: bool = False,
        allow_repeat_by_same: bool = False,
        max_total_budget: str | None = None,
        use_escrow: bool = False,
        group_id: str | None = None,
        deadline_hours: int | None = None,
        metadata: dict | None = None,
        subnet_slug: str | None = None,
        max_resubmit_attempts: int | None = None,
    ) -> Task:
        """
        Create a new task.

        Args:
            creator_type: "human" or "agent"
            creator_id: Creator identifier
            creator_name: Creator display name
            title: Task title
            description: Task description
            task_type: Task type category
            required_tags: Tags needed to complete
            reward: Reward per completion (numeric string)
            reward_currency: Currency (credits, ap_points legacy, USD, USDC, ETH)
            max_participants: 1=single, N=fixed, None=unlimited
            completion_mode: "independent" | "competitive" | "collaborative"
            auto_approve: True → submissions auto-complete without review
            require_join_approval: True → solvers must apply and be approved to join
            allow_repeat_by_same: True → same solver can complete again
            max_total_budget: Budget cap for unlimited-capacity tasks
            use_escrow: True → lock budget in escrow at creation
            deadline_hours: Deadline in hours from now
            metadata: Extensible metadata

        Returns:
            Created task
        """
        task_id = str(uuid4())

        # Calculate deadline
        deadline = None
        if deadline_hours:
            deadline = datetime.now(UTC) + timedelta(hours=deadline_hours)

        # Calculate total_budget
        reward_float = float(reward) if reward else 0
        if max_participants is not None and max_participants > 1:
            total_budget = str(reward_float * max_participants)
        elif max_participants is None:
            # Bounty: use explicit cap or fall back to single reward amount
            total_budget = max_total_budget or str(reward_float)
        else:
            total_budget = str(reward_float)

        # Snapshot Org Harness URL+secret from the parent subnet onto the task.
        # Done at creation only so all later events (TASK_ACCEPTED / SUBMITTED /
        # COMPLETED / CANCELLED) can deliver to the harness without paying for
        # an extra subnet read per event. If the subnet owner rotates the
        # harness URL later, only NEW tasks pick it up — in-flight tasks stay
        # bound to the harness that owned them at creation, which is the
        # desired guarantee for any orchestrator that has already taken over.
        metadata = dict(metadata) if metadata else {}
        if subnet_slug and self.subnet_repository:
            try:
                parent_subnet = await self.subnet_repository.find_by_id(subnet_slug)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "task_create_subnet_harness_snapshot_failed",
                    subnet_slug=subnet_slug,
                    error=str(e),
                )
                parent_subnet = None
            if parent_subnet and parent_subnet.harness_url:
                metadata["harness_url"] = parent_subnet.harness_url
                if parent_subnet.harness_secret:
                    metadata["harness_secret"] = parent_subnet.harness_secret

        task = Task(
            task_id=task_id,
            creator_type=creator_type,
            creator_id=creator_id,
            creator_name=creator_name,
            title=title,
            description=description,
            task_type=task_type,
            required_tags=required_tags or [],
            reward=reward,
            reward_currency=reward_currency,
            total_budget=total_budget,
            released_amount="0",
            max_total_budget=max_total_budget,
            max_participants=max_participants,
            completion_mode=completion_mode,
            auto_approve=auto_approve,
            require_join_approval=require_join_approval,
            allow_repeat_by_same=allow_repeat_by_same,
            use_escrow=use_escrow,
            group_id=group_id,
            deadline=deadline,
            metadata=metadata or {},
            subnet_slug=subnet_slug,
            max_resubmit_attempts=max_resubmit_attempts,
        )

        # Escrow lock: explicit opt-in only
        if self.escrow and use_escrow and float(total_budget) > 0:
            logger.info(
                "escrow_lock_attempt",
                creator_type=creator_type,
                creator_id=creator_id,
                task_id=task_id,
                amount=float(total_budget),
            )
            escrow_config = metadata.get("escrow_config") if metadata else None
            result = await self.escrow.lock_v2(
                task_id=task_id,
                creator_id=creator_id,
                creator_type=creator_type,
                amount=float(total_budget),
                currency=reward_currency,
                description=f"Escrow for task: {title}",
                escrow_config=escrow_config,
            )
            if not result.success:
                raise ValueError(f"Failed to lock budget: {result.error}")
            logger.info(
                "escrow_locked_for_task",
                task_id=task_id,
                escrow_id=result.escrow_id,
                amount=total_budget,
                creator_id=creator_id,
                creator_type=creator_type,
            )

        # Create AP2 payment task if real currency
        if reward_currency.lower() not in PLATFORM_CURRENCIES | {"0"} and float(reward) > 0:
            if self.payment_manager:
                try:
                    payment_task = await self.payment_manager.create_task(
                        buyer_agent=creator_id,
                        description=f"Payment for task: {title}",
                        amount=reward,
                        currency=reward_currency,
                    )
                    task.payment_task_id = payment_task.task_id
                    logger.info(
                        "payment_task_created",
                        task_id=task_id,
                        payment_task_id=payment_task.task_id,
                    )
                except Exception as e:
                    logger.error("failed_to_create_payment_task", error=str(e))

        await self.task_pool.add(task)
        await self._notify_webhook(WebhookEventType.TASK_CREATED, task)

        if self.activity:
            await self.activity.record_task_created(
                creator_type=creator_type,
                creator_id=creator_id,
                creator_name=creator_name,
                task_id=task_id,
                task_title=title,
                reward=reward,
                reward_currency=reward_currency,
            )

        logger.info(
            "task_created",
            task_id=task_id,
            max_participants=max_participants,
            title=title,
            creator_id=creator_id,
        )

        return task

    async def get_task(self, task_id: str) -> Task:
        """
        Get a task by ID

        Args:
            task_id: Task identifier

        Returns:
            Task entity

        Raises:
            TaskNotFoundException: If task not found
        """
        task = await self.repository.find_by_id(task_id)
        if not task:
            raise TaskNotFoundException(f"Task {task_id} not found")
        return task

    async def accept_task(
        self,
        task_id: str,
        agent_id: str,
        agent_name: str,
        agent_type: str = "agent",
    ) -> tuple[Task, str | None]:
        """
        Accept a task.

        For multi-participant tasks, creates a Participation and returns its ID.
        For single-participant tasks, uses the original assignee flow.

        Returns:
            Tuple of (updated task, participation_id or None)
        """
        task = await self.get_task(task_id)

        if task.creator_id == agent_id:
            raise PermissionError("Creator cannot accept their own task")

        # ---- Subnet access control ----
        if task.subnet_slug:
            if not self.subnet_repository:
                raise PermissionError("Subnet access control not configured")
            subnet = await self.subnet_repository.find_by_id(task.subnet_slug)
            if not subnet:
                raise PermissionError("Task subnet not found or deleted")
            if agent_id not in (subnet.member_agent_ids or set()):
                raise PermissionError("Agent is not a member of the task's subnet")

        # ---- Invited solver: bypass require_join_approval ----
        is_invited = agent_id in (task.invited_agent_ids or [])

        # ---- Multi-participant path ----
        if task._is_multi():
            return await self._join_task(task, agent_id, agent_name, agent_type)

        # ---- Join-approval path: solver must apply and be approved ----
        if task.require_join_approval and not is_invited and task.assignee_id is None:
            existing = await self.task_pool.get_user_participation(
                task_id, agent_id, active_only=True
            )
            if existing and existing.status == ParticipationStatus.APPLIED:
                raise ValueError("You have already applied for this task")
            participation = Participation(
                participation_id=Participation.new_id(),
                task_id=task_id,
                participant_id=agent_id,
                participant_name=agent_name,
                participant_type=agent_type,
                status=ParticipationStatus.APPLIED,
            )
            await self.repository.add_application(task_id, participation)
            if self.activity:
                await self.activity.record_task_accepted(
                    agent_id=agent_id,
                    agent_name=agent_name,
                    task_id=task_id,
                    task_title=task.title,
                )
            logger.info(
                "task_applied",
                task_id=task_id,
                participation_id=participation.participation_id,
                agent_id=agent_id,
            )
            return task, participation.participation_id

        # ---- Single-participant direct-accept path ----
        if not task._is_multi():
            has_completed = await self.task_pool.has_agent_completed(task_id, agent_id)
            if has_completed:
                raise ValueError("You have already completed this task")

        task.accept(agent_id, agent_name)
        # CAS: only persist if the task was still OPEN in the DB at the time
        # of this write. Two concurrent accept requests will both transition
        # the in-memory entity to IN_PROGRESS, but exactly one will land its
        # UPDATE (the one whose WHERE clause matches status='open'). The other
        # sees rowcount==0 and gets a clear ValueError rather than silently
        # double-assigning the task.
        accepted = await self.repository.compare_and_save(
            task, expected_status=TaskStatus.OPEN
        )
        if not accepted:
            raise ValueError("Task has already been accepted by another agent")

        # Update escrow: set assignee + IN_PROGRESS
        if self.escrow and task.reward_currency.lower() in PLATFORM_CURRENCIES:
            try:
                escrow_info = await self.escrow.get_by_task(task_id)
                if escrow_info.success and escrow_info.escrow_id:
                    await self.escrow.accept_v2(
                        escrow_id=escrow_info.escrow_id,
                        assignee_id=agent_id,
                        assignee_type="agent",
                    )
                    logger.info(
                        "escrow_accepted",
                        task_id=task_id,
                        escrow_id=escrow_info.escrow_id,
                        agent_id=agent_id,
                    )
            except Exception as e:
                logger.warning("escrow_accept_failed", task_id=task_id, error=str(e))

        if self.activity:
            await self.activity.record_task_accepted(
                agent_id=agent_id,
                agent_name=agent_name,
                task_id=task_id,
                task_title=task.title,
            )

        logger.info("task_accepted", task_id=task_id, agent_id=agent_id)

        # Symmetry with the multi-participant path (_join_task line 630): emit
        # TASK_ACCEPTED webhook so Org Harnesses (e.g. paperclip-acn-plugin)
        # can mirror state. Without this, single-participant tasks silently
        # skip the accept notification and downstream harnesses are stuck
        # showing "open".
        await self._notify_webhook(WebhookEventType.TASK_ACCEPTED, task)
        return task, None

    async def invite_agent(
        self,
        task_id: str,
        inviter_id: str,
        invitee_id: str,
        invitee_name: str = "",
    ) -> Task:
        """Creator invites a specific solver to the task.

        Invited solvers can join even when require_join_approval is True.
        """
        task = await self.get_task(task_id)

        if task.creator_id != inviter_id:
            raise PermissionError("Only the task creator can invite solvers")

        if invitee_id == task.creator_id:
            raise ValueError("Creator cannot invite themselves")

        if task.status != TaskStatus.OPEN:
            raise ValueError(f"Cannot invite solvers to a task in status: {task.status.value}")

        if invitee_id in (task.invited_agent_ids or []):
            raise ValueError("This solver has already been invited")

        task.invited_agent_ids = list(task.invited_agent_ids or []) + [invitee_id]
        await self.repository.save(task)

        if self.activity:
            await self.activity.record(
                event_type="task_invite",
                actor_type=task.creator_type,
                actor_id=inviter_id,
                actor_name=task.creator_name,
                description=f"Invited {invitee_name or invitee_id} to task: {task.title}",
                task_id=task_id,
                metadata={"invitee_id": invitee_id},
            )

        logger.info(
            "task_solver_invited",
            task_id=task_id,
            inviter_id=inviter_id,
            invitee_id=invitee_id,
        )
        return task

    async def _join_task(
        self,
        task: Task,
        agent_id: str,
        agent_name: str,
        agent_type: str = "agent",
    ) -> tuple[Task, str]:
        """Join a multi-participant task (creates a Participation atomically)"""
        participation = Participation(
            participation_id=Participation.new_id(),
            task_id=task.task_id,
            participant_id=agent_id,
            participant_name=agent_name,
            participant_type=agent_type,
        )

        pid = await self.task_pool.join_task(
            task_id=task.task_id,
            participation=participation,
            max_completions=task.max_participants,
            allow_repeat=task.allow_repeat_by_same,
        )

        # Activate escrow pool on first join (LOCKED -> ACTIVE)
        #
        # M10 — compensating rollback on escrow failure:
        #   ``task_pool.join_task`` above already committed the Participation
        #   row.  If escrow activation then fails the two subsystems diverge:
        #   the task shows 1 participant but the escrow is still LOCKED.
        #
        #   We compensate by cancelling the participation so both subsystems
        #   return to their pre-join state.  If the cancellation itself fails
        #   (double-failure), we log at ERROR for ops alerting and still
        #   propagate the original exception so the caller gets a clean 500
        #   rather than silently succeeding with inconsistent data.
        #
        #   Note: only ``accept_v2`` is a state-changing escrow call;
        #   ``get_by_task`` is read-only and its failure still falls through
        #   to the outer branch (no escrow activation attempted, no
        #   inconsistency created).
        if self.escrow and task.reward_currency.lower() in PLATFORM_CURRENCIES:
            try:
                escrow_info = await self.escrow.get_by_task(task.task_id)
            except Exception as e:
                # Read-only probe failed — treat as "no escrow found", continue.
                logger.warning(
                    "escrow_probe_failed_on_join",
                    task_id=task.task_id,
                    error=str(e),
                )
                escrow_info = None  # type: ignore[assignment]

            if escrow_info is not None and escrow_info.success and escrow_info.escrow_id:
                if escrow_info.status == "locked":
                    # First participant: activate the pool — state-changing call.
                    try:
                        await self.escrow.accept_v2(
                            escrow_id=escrow_info.escrow_id,
                            assignee_id=agent_id,
                            assignee_type=agent_type,
                        )
                        logger.info(
                            "escrow_pool_activated",
                            task_id=task.task_id,
                            escrow_id=escrow_info.escrow_id,
                        )
                    except Exception as escrow_exc:
                        # M10 compensation path: undo the join so we don't
                        # leave a participation with no matching escrow state.
                        logger.error(
                            "escrow_pool_activate_failed_compensating",
                            task_id=task.task_id,
                            participation_id=pid,
                            error=str(escrow_exc),
                        )
                        try:
                            await self.task_pool.cancel_participation(pid, task.task_id)
                            logger.info(
                                "join_rolled_back_after_escrow_failure",
                                task_id=task.task_id,
                                participation_id=pid,
                            )
                        except Exception as cancel_exc:
                            # Double failure — participation persists despite
                            # escrow being stuck; ops must reconcile manually.
                            logger.error(
                                "join_rollback_failed_manual_reconciliation_required",
                                task_id=task.task_id,
                                participation_id=pid,
                                escrow_error=str(escrow_exc),
                                cancel_error=str(cancel_exc),
                            )
                        raise escrow_exc

        if self.activity:
            await self.activity.record_task_accepted(
                agent_id=agent_id,
                agent_name=agent_name,
                task_id=task.task_id,
                task_title=task.title,
            )

        # Refresh task to get updated active_participants_count
        task = await self.get_task(task.task_id)

        logger.info(
            "task_joined",
            task_id=task.task_id,
            participation_id=pid,
            agent_id=agent_id,
        )

        await self._notify_webhook(WebhookEventType.TASK_ACCEPTED, task)
        return task, pid

    async def submit_task(
        self,
        task_id: str,
        agent_id: str,
        submission: str,
        artifacts: list[dict] | None = None,
        participation_id: str | None = None,
    ) -> Task:
        """
        Submit task result.

        For multi-participant tasks, submits the participation.
        For single-participant tasks, uses the original task-level submission.

        Args:
            participation_id: Optional — required for multi-participant, auto-found if omitted
        """
        task = await self.get_task(task_id)

        # ---- Multi-participant path ----
        if task._is_multi():
            p = await self._resolve_participation(task_id, agent_id, participation_id)
            if p.status == ParticipationStatus.REJECTED:
                if (
                    task.max_resubmit_attempts is not None
                    and p.resubmit_count >= task.max_resubmit_attempts
                ):
                    raise ValueError(
                        f"Max resubmit attempts ({task.max_resubmit_attempts}) reached"
                    )
                p.resubmit(submission, artifacts)
            else:
                p.submit(submission, artifacts)
            await self.repository.save_participation(p)

            if self.activity:
                await self.activity.record_task_submitted(
                    agent_id=agent_id,
                    agent_name=p.participant_name,
                    task_id=task_id,
                    task_title=task.title,
                )

            await self._notify_webhook(WebhookEventType.TASK_SUBMITTED, task)

            # Auto-approval for participation
            if task.auto_approve:
                await self._auto_complete_participation(task, p)

            logger.info(
                "participation_submitted",
                task_id=task_id,
                participation_id=p.participation_id,
                agent_id=agent_id,
            )
            return task

        # ---- Single-participant path (original) ----
        if task.assignee_id != agent_id:
            raise PermissionError("Only the assigned solver can submit")

        # Snapshot the pre-transition status for the CAS below.
        # Two concurrent submits can both pass the assignee check, both call
        # task.submit() / task.resubmit(), and both reach the escrow call,
        # double-triggering submit_v2. CAS lets only one winner through.
        expected_status = task.status

        if task.status == TaskStatus.REJECTED:
            if (
                task.max_resubmit_attempts is not None
                and task.resubmit_count >= task.max_resubmit_attempts
            ):
                raise ValueError(
                    f"Max resubmit attempts ({task.max_resubmit_attempts}) reached"
                )
            task.resubmit(submission, artifacts)  # increments task.resubmit_count
        else:
            task.submit(submission, artifacts)

        won = await self.repository.compare_and_save(task, expected_status=expected_status)
        if not won:
            logger.info(
                "submit_task_lost_race",
                task_id=task_id,
                agent_id=agent_id,
            )
            return await self.get_task(task_id)

        # Sync escrow status
        if self.escrow and task.reward_currency.lower() in PLATFORM_CURRENCIES:
            try:
                escrow_info = await self.escrow.get_by_task(task_id)
                if escrow_info.success and escrow_info.escrow_id:
                    result = await self.escrow.submit_v2(escrow_info.escrow_id)
                    if result.success:
                        logger.info(
                            "escrow_submitted",
                            task_id=task_id,
                            escrow_id=escrow_info.escrow_id,
                            auto_release_at=result.auto_release_at,
                        )
                    else:
                        logger.warning(
                            "escrow_submit_failed",
                            task_id=task_id,
                            error=result.error,
                        )
            except Exception as e:
                logger.warning("escrow_submit_error", task_id=task_id, error=str(e))

        if self.activity:
            await self.activity.record_task_submitted(
                agent_id=agent_id,
                agent_name=task.assignee_name or agent_id,
                task_id=task_id,
                task_title=task.title,
            )

        logger.info(
            "task_submitted",
            task_id=task_id,
            agent_id=agent_id,
            auto_approve=task.auto_approve,
        )

        await self._notify_webhook(WebhookEventType.TASK_SUBMITTED, task)

        if task.auto_approve:
            logger.info("auto_approving_task", task_id=task_id)
            task = await self._auto_complete_task(task)

        return task

    async def _resolve_participation(
        self, task_id: str, agent_id: str, participation_id: str | None
    ) -> Participation:
        """Resolve a participation — by explicit ID or by auto-finding user's active one."""
        if participation_id:
            p = await self.task_pool.get_participation(participation_id)
            if not p:
                raise ValueError(f"Participation {participation_id} not found")
            if p.participant_id != agent_id:
                raise PermissionError("This participation belongs to another user")
            return p

        # Auto-find user's most recent active/submitted participation
        p = await self.task_pool.get_user_participation(task_id, agent_id, active_only=True)
        if not p:
            raise ValueError("No active participation found for this user in this task")
        return p

    async def _auto_complete_task(self, task: Task) -> Task:
        """
        Auto-complete a task (for auto-approval type)

        Args:
            task: Task to complete

        Returns:
            Completed task
        """
        # Complete the task (using system as reviewer for auto-approval)
        task.complete(reviewer_id="system:auto", notes="Auto-approved on submission")
        await self.repository.save(task)

        # Record completion for the agent
        if task.assignee_id:
            await self.task_pool.record_completion(task.task_id, task.assignee_id)

        # Distribute reward for points-based tasks
        if (
            task.reward_currency.lower() in PLATFORM_CURRENCIES
            and float(task.reward) > 0
            and task.assignee_id
        ):
            reward_result = await self._distribute_reward(
                task=task,
                amount=float(task.reward),
                description=f"Auto-reward for task: {task.title}",
            )
            if reward_result["success"]:
                logger.info(
                    "auto_reward_distributed",
                    task_id=task.task_id,
                    agent_amount=reward_result.get("agent_amount"),
                    acn_amount=reward_result.get("acn_amount"),
                    provider_amount=reward_result.get("provider_amount"),
                )
            else:
                logger.error(
                    "auto_reward_distribution_failed",
                    task_id=task.task_id,
                    error=reward_result.get("error"),
                )

        # Send webhook notification
        await self._notify_webhook(WebhookEventType.TASK_COMPLETED, task)

        # Record activity
        if self.activity and task.assignee_id:
            await self.activity.record_task_approved(
                approver_type="system",
                approver_id="system:auto",
                approver_name="Auto-Approval",
                agent_id=task.assignee_id,
                agent_name=task.assignee_name or task.assignee_id,
                task_id=task.task_id,
                task_title=task.title,
                reward=task.reward,
                reward_currency=task.reward_currency,
            )

        logger.info(
            "task_auto_completed",
            task_id=task.task_id,
            assignee_id=task.assignee_id,
        )

        return task

    async def _check_and_finalize_exhaustion(self, task: Task, new_count: int) -> bool:
        """
        Check if a multi-participant task has reached its max completions.
        If so, cancel remaining participations and mark task COMPLETED.

        Args:
            task: Task entity (will be mutated and saved if exhausted)
            new_count: The latest completed_count from the atomic Lua operation

        Returns:
            True if task was finalized as COMPLETED
        """
        if not task.max_participants or new_count < task.max_participants:
            return False

        await self.task_pool.batch_cancel_participations(task.task_id)
        # Sync counters from Lua results before saving to avoid overwriting:
        # - completed_count: from the atomic completion Lua script return value
        # - active_participants_count: batch_cancel Lua scripts set this to 0 on the hash;
        #   we must sync it here so save() doesn't overwrite with a stale in-memory value
        task.completed_count = new_count
        task.active_participants_count = 0
        task.status = TaskStatus.COMPLETED
        task.completed_at = datetime.now(UTC)
        await self.repository.save(task)
        logger.info("task_exhausted", task_id=task.task_id, completed_count=new_count)
        return True

    async def _auto_complete_participation(self, task: Task, p: Participation) -> None:
        """Auto-complete a participation (for auto-approval tasks)"""
        new_count = await self.task_pool.complete_participation(
            participation_id=p.participation_id,
            task_id=task.task_id,
            reviewer_id="system:auto",
            notes="Auto-approved on submission",
        )

        # Record completion
        await self.task_pool.record_completion(task.task_id, p.participant_id)

        # Distribute reward
        if task.reward_currency.lower() in PLATFORM_CURRENCIES and float(task.reward) > 0:
            reward_result = await self._distribute_reward(
                task=task,
                amount=float(task.reward),
                description=f"Auto-reward for task: {task.title} (participation {p.participation_id})",
                participant_id=p.participant_id,
            )
            if reward_result["success"]:
                logger.info(
                    "auto_reward_distributed_participation",
                    task_id=task.task_id,
                    participation_id=p.participation_id,
                    agent_amount=reward_result.get("agent_amount"),
                    acn_amount=reward_result.get("acn_amount"),
                    provider_amount=reward_result.get("provider_amount"),
                )

        # 批准事件（auto_approve 路径）：与人工审批路径对称，驱动 backend XP 等下游
        await self._notify_participation_webhook(
            WebhookEventType.PARTICIPATION_APPROVED, task, p
        )

        # Check if task is exhausted (all slots filled)
        await self._check_and_finalize_exhaustion(task, new_count)

    async def review_participation(
        self,
        task_id: str,
        approver_id: str,
        approved: bool,
        participation_id: str | None = None,
        agent_id: str | None = None,
        notes: str | None = None,
    ) -> Task:
        """
        Approve or reject a specific participation.

        Args:
            participation_id: Explicit participation ID (preferred)
            agent_id: Agent ID (if no participation_id, finds submitted participation)
            approved: Whether to approve
            notes: Review notes
        """
        task = await self.get_task(task_id)

        if task.creator_id != approver_id:
            raise PermissionError("Only the task creator can review")

        if not task._is_multi():
            # Delegate to single-participant flow
            if approved:
                return await self.complete_task(task_id, approver_id, notes)
            else:
                return await self.reject_task(task_id, approver_id, notes)

        # Resolve participation
        if participation_id:
            p = await self.task_pool.get_participation(participation_id)
            if not p or p.task_id != task_id:
                raise ValueError("Participation not found")
        elif agent_id:
            p = await self.repository.find_participation_by_user_and_task(
                task_id, agent_id, active_only=False
            )
            if not p or p.status != ParticipationStatus.SUBMITTED:
                raise ValueError("No submitted participation found for this agent")
        else:
            raise ValueError("Either participation_id or agent_id is required")

        if approved:
            new_count = await self.task_pool.complete_participation(
                participation_id=p.participation_id,
                task_id=task_id,
                reviewer_id=approver_id,
                notes=notes,
            )

            await self.task_pool.record_completion(task_id, p.participant_id)

            # Distribute per-completion reward
            if task.reward_currency.lower() in PLATFORM_CURRENCIES and float(task.reward) > 0:
                await self._distribute_reward(
                    task=task,
                    amount=float(task.reward),
                    description=f"Reward for task: {task.title} (participation {p.participation_id})",
                    participant_id=p.participant_id,
                )

            if self.activity:
                await self.activity.record_task_approved(
                    approver_type=task.creator_type,
                    approver_id=approver_id,
                    approver_name=task.creator_name,
                    agent_id=p.participant_id,
                    agent_name=p.participant_name,
                    task_id=task_id,
                    task_title=task.title,
                    reward=task.reward,
                    reward_currency=task.reward_currency,
                )

            # Check if task is exhausted (uses extracted common method)
            await self._check_and_finalize_exhaustion(task, new_count)

            logger.info(
                "participation_approved",
                task_id=task_id,
                participation_id=p.participation_id,
                new_completed_count=new_count,
            )

            # 对称于 PARTICIPATION_REJECTED：批准事件驱动下游（backend 驯养师 XP 等）
            await self._notify_participation_webhook(
                WebhookEventType.PARTICIPATION_APPROVED, task, p
            )
        else:
            # Reject participation — set status to REJECTED and decrement active count
            was_active = p.status in (ParticipationStatus.ACTIVE, ParticipationStatus.SUBMITTED)
            p.reject(approver_id, notes)
            await self.repository.save_participation(p)

            # Manually decrement active count (don't use atomic_cancel which overwrites to 'cancelled')
            # Fix: use active_count key (consistent with Lua scripts in task_repository.py)
            if was_active:
                try:
                    await self.repository.decrement_active_count(task_id)
                except Exception:
                    logger.warning(
                        "active_count_decrement_failed",
                        task_id=task_id,
                        participation_id=p.participation_id,
                    )

            if self.activity and hasattr(self.activity, "record_task_rejected"):
                await self.activity.record_task_rejected(
                    reviewer_type=task.creator_type,
                    reviewer_id=approver_id,
                    reviewer_name=task.creator_name,
                    agent_id=p.participant_id,
                    task_id=task_id,
                    task_title=task.title,
                    reason=notes or "",
                )

            logger.info(
                "participation_rejected",
                task_id=task_id,
                participation_id=p.participation_id,
            )

            await self._notify_participation_webhook(
                WebhookEventType.PARTICIPATION_REJECTED, task, p
            )

        return await self.get_task(task_id)

    async def cancel_participation(
        self,
        task_id: str,
        participation_id: str,
        canceller_id: str,
    ) -> Task:
        """Cancel a participation (participant withdraws)"""
        p = await self.task_pool.get_participation(participation_id)
        if not p:
            raise ValueError("Participation not found")
        if p.participant_id != canceller_id:
            raise PermissionError("Only the participant can cancel their participation")
        if p.task_id != task_id:
            raise ValueError("Participation does not belong to this task")

        await self.task_pool.cancel_participation(participation_id, task_id)

        logger.info(
            "participation_cancelled_by_user",
            task_id=task_id,
            participation_id=participation_id,
            agent_id=canceller_id,
        )

        return await self.get_task(task_id)

    async def approve_applicant(
        self,
        task_id: str,
        participation_id: str,
        approver_id: str,
    ) -> Task:
        """Approve an applicant for an assigned task (creator only). Sets them as assignee."""
        task = await self.get_task(task_id)
        if task.creator_id != approver_id:
            raise PermissionError("Only the task creator can approve applicants")
        if not task.require_join_approval or task.assignee_id:
            raise ValueError("Task does not require join approval or already has an assignee")
        p = await self.task_pool.get_participation(participation_id)
        if not p or p.task_id != task_id:
            raise ValueError("Participation not found")
        if p.status != ParticipationStatus.APPLIED:
            raise ValueError("Participation is not an application")

        # Set task assignee and status
        task.accept(p.participant_id, p.participant_name or p.participant_id)
        await self.repository.save(task)

        # Mark this participation as active (no longer applied)
        p.status = ParticipationStatus.ACTIVE
        await self.repository.save_participation(p)

        # Escrow: set assignee + IN_PROGRESS
        if self.escrow and task.reward_currency.lower() in PLATFORM_CURRENCIES:
            try:
                escrow_info = await self.escrow.get_by_task(task_id)
                if escrow_info.success and escrow_info.escrow_id:
                    await self.escrow.accept_v2(
                        escrow_id=escrow_info.escrow_id,
                        assignee_id=p.participant_id,
                        assignee_type=p.participant_type or "agent",
                    )
                    logger.info(
                        "escrow_accepted",
                        task_id=task_id,
                        escrow_id=escrow_info.escrow_id,
                        agent_id=p.participant_id,
                    )
            except Exception as e:
                logger.warning("escrow_accept_failed", task_id=task_id, error=str(e))

        # Cancel other applied participations
        others = await self.repository.find_participations_by_task(
            task_id, status=ParticipationStatus.APPLIED.value, limit=100
        )
        for other in others:
            if other.participation_id != participation_id:
                other.cancel()
                await self.repository.save_participation(other)

        if self.activity:
            await self.activity.record_task_accepted(
                agent_id=p.participant_id,
                agent_name=p.participant_name or p.participant_id,
                task_id=task_id,
                task_title=task.title,
            )
        logger.info(
            "applicant_approved",
            task_id=task_id,
            participation_id=participation_id,
            assignee_id=p.participant_id,
        )
        return await self.get_task(task_id)

    async def reject_applicant(
        self,
        task_id: str,
        participation_id: str,
        approver_id: str,
    ) -> Task:
        """Reject an applicant for an assigned task (creator only)."""
        task = await self.get_task(task_id)
        if task.creator_id != approver_id:
            raise PermissionError("Only the task creator can reject applicants")
        if not task.require_join_approval or task.assignee_id:
            raise ValueError("Task does not require join approval or already has an assignee")
        p = await self.task_pool.get_participation(participation_id)
        if not p or p.task_id != task_id:
            raise ValueError("Participation not found")
        if p.status != ParticipationStatus.APPLIED:
            raise ValueError("Participation is not an application")

        p.cancel()
        await self.repository.save_participation(p)
        logger.info(
            "applicant_rejected",
            task_id=task_id,
            participation_id=participation_id,
        )
        return await self.get_task(task_id)

    # ========== Participation Queries ==========

    async def get_task_participations(
        self,
        task_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Participation]:
        """Get participations for a task"""
        return await self.task_pool.get_task_participations(task_id, status, limit, offset)

    async def get_user_participation(
        self,
        task_id: str,
        user_id: str,
    ) -> Participation | None:
        """Get a user's current participation in a task"""
        return await self.task_pool.get_user_participation(task_id, user_id, active_only=False)

    def _build_review_pass_event(
        self,
        task: Task,
        approver_id: str,
        notes: str | None,
    ) -> SettlementEvent:
        """Construct the outbox event for a ``review_pass`` completion.

        ``event_id`` is deterministic — uuid5 of ``(task_id, trigger)``
        — so an upstream caller retrying ``complete_task`` after a
        timeout cannot create a second outbox row. The DB UNIQUE
        constraint silently drops the dup (see ``enqueue``).

        ``step_status`` is pre-computed: any step that's a no-op for
        this specific task (no escrow, or zero reward, etc.) is marked
        ``skipped`` up front. This means:

        - The worker never has to re-derive "is there anything to do
          for step N?" from the payload — it just trusts step_status.
        - DLQ counts and step-duration metrics aren't polluted by
          synthetic "completed in 0ms" no-op steps.
        - The downstream business invariant "step in {pending, done,
          skipped}" is preserved when read by ops dashboards.

        ``payload`` snapshot is what the worker will read at
        execution time — by then ``tasks`` may have been mutated
        (e.g. status moved forward), so we freeze the values the
        saga needs *now*. Currency normalization here matches the
        legacy reward-distribution branch one-to-one.
        """
        currency_lower = task.reward_currency.lower()
        reward_value = float(task.reward) if task.reward else 0.0

        # Decide per-step status up front. Note the gating criteria are
        # NOT the same as the legacy ``complete_task`` branches:
        #
        # - ``has_reward`` is the same criterion ``_distribute_reward``
        #   uses (platform currency + positive reward + assignee
        #   present). It's a precondition for BOTH ``escrow_release``
        #   AND ``reward_distribute`` — a zero-reward escrow has
        #   nothing for the worker to release, so producing such an
        #   event would just trigger 12 worker retries before DLQ
        #   (worker rejects ``amount <= 0``).
        #
        # - ``escrow_release`` additionally requires ``task.use_escrow``
        #   to be True. ``payment_task_id`` is the AP2-protocol
        #   tracking handle and exists for many tasks that never
        #   touched escrow; gating on it would have the worker call
        #   backend ``POST /release`` for tasks with no escrow row,
        #   which 404/400s every time. ``task.use_escrow=True`` is
        #   the only signal that actually predicts "there's an
        #   escrow to release".
        #
        # - ``reward_distribute`` is ``pending`` whenever
        #   ``has_reward`` holds. For ``use_escrow=True`` tasks the
        #   actual money movement is folded into ``escrow.release``
        #   already, so the worker's reward step is a logged no-op.
        #   For ``use_escrow=False`` tasks the reward concept is
        #   off-chain bookkeeping — legacy ``_distribute_reward``
        #   returns ``via=off_chain`` without moving funds. Keeping
        #   the step ``pending`` here ensures the saga step_status
        #   matrix reflects "reward exists conceptually" so a future
        #   on-chain reward distributor has a hook to slot into
        #   without producer-side schema churn.
        #
        # - ``reputation_write`` runs for every accepted task with a
        #   counterparty — reward-less / payment-less tasks still
        #   earn reputation, that's the whole point of reputation.
        has_reward = (
            currency_lower in PLATFORM_CURRENCIES and reward_value > 0 and bool(task.assignee_id)
        )
        needs_escrow_release = bool(task.use_escrow) and has_reward

        step_status: dict[str, str] = {
            "escrow_release": "pending" if needs_escrow_release else "skipped",
            "reward_distribute": "pending" if has_reward else "skipped",
            "reputation_write": "pending" if task.assignee_id else "skipped",
        }

        # The worker reads ``payload`` instead of refetching ``tasks``
        # so a parallel mutation (e.g. soft-delete) doesn't corrupt
        # settlement. Keep the dict JSON-serialisable.
        #
        # ``is_multi`` is captured at enqueue time, NOT computed by the
        # worker. Two reasons:
        #   1. The worker uses it to pick between ``escrow.release``
        #      (single) and ``escrow.release_partial`` (multi). The
        #      two endpoints have different state transitions and
        #      different return shapes — the worker must not guess.
        #   2. ``max_participants`` could in principle be mutated
        #      after submission (it can't today, but the producer
        #      side is the only place that holds the task entity, so
        #      freezing it is cheap insurance). Freezing matches the
        #      "payload is a snapshot" contract in plan §3.
        #
        # ``use_escrow`` is a DIAGNOSTIC field — the worker does NOT
        # read it. The actual control signal for the escrow step is
        # ``step_status['escrow_release']`` decided above. We snapshot
        # ``use_escrow`` here so operators triaging a DLQ row can see
        # at a glance whether the task was supposed to lock funds at
        # creation, without joining back to the ``tasks`` table.
        payload: dict[str, object] = {
            "task_id": task.task_id,
            "creator_id": task.creator_id,
            "assignee_id": task.assignee_id,
            "payment_task_id": task.payment_task_id,
            "reward": str(task.reward),
            "reward_currency": task.reward_currency,
            "task_title": task.title,
            "approver_id": approver_id,
            "review_notes": notes,
            "use_escrow": bool(task.use_escrow),
            "is_multi": task._is_multi(),
            "metadata": dict(task.metadata) if task.metadata else {},
        }

        return SettlementEvent(
            event_id=str(uuid5(_OUTBOX_EVENT_NS, f"{task.task_id}:review_pass")),
            task_id=task.task_id,
            trigger="review_pass",
            payload=payload,
            step_status=step_status,
        )

    async def complete_task(
        self,
        task_id: str,
        approver_id: str,
        notes: str | None = None,
    ) -> Task:
        """
        Complete/approve a task.

        Args:
            task_id: Task identifier
            approver_id: Approver ID (must be creator)
            notes: Review notes

        Returns:
            Updated task

        Raises:
            TaskNotFoundException: If task not found
            PermissionError: If approver is not the creator
            ValueError: If task is not submitted

        Concurrency model (security audit H3):
            Two concurrent ``complete_task`` calls on the same task
            previously both passed the in-memory ``status == SUBMITTED``
            check, both ran ``task.complete()``, and both reached
            ``record_completion`` / ``payment release`` /
            ``_distribute_reward`` — i.e. a *double-pay* race. The fix
            is a CAS save: the second caller loses the ``SUBMITTED →
            COMPLETED`` transition at the repository layer and
            short-circuits before any side effect runs, returning the
            current task state for idempotent semantics.

        Settlement saga v0.1 — see
        ``acn/docs/_drafts/settlement-saga-design.md``:
            When all of ``settlement_outbox`` / ``unit_of_work`` /
            ``outbox_enqueue_required`` are present (default production
            wiring), this method runs CAS save + outbox INSERT inside
            a single ACID transaction (the "atomic" path) and returns.
            The ``SettlementWorker`` then drives escrow release +
            reward distribute + reputation write asynchronously. An
            enqueue failure rolls back the state transition and raises
            HTTP 500 — the caller MUST retry.

            When the saga deps are not all present (Redis-only mode,
            test fixtures, or emergency disarm via
            ``OUTBOX_ENQUEUE_REQUIRED=false``), the legacy synchronous
            path runs — CAS save in its own transaction, no outbox
            row, payment + reward synchronously inline. This is what
            production ran prior to v0.1, kept as an in-place rollback
            lever should the saga need to be disabled urgently without
            redeploying. The two paths are mutually exclusive — there
            is no longer a double-write window.

        Known limitation — HTTP retry vs state-machine non-idempotency:
            If a caller retries this endpoint after a network failure
            but the FIRST call already fully committed (CAS + enqueue
            + commit + side effects), the second call's
            ``task.complete()`` will raise ``ValueError`` because the
            task is no longer in ``SUBMITTED``. The caller observes
            an error response even though the operation succeeded.
            Workaround for clients: treat 4xx "task not in SUBMITTED"
            from ``complete_task`` as a probable success and confirm
            via ``GET /tasks/{id}``. We do NOT auto-recover here
            because returning success for a re-entrant call that
            crossed a state-machine boundary would silently swallow
            genuine client bugs (e.g. trying to approve an already
            rejected task). A future v0.2 may add a separate
            ``GET /tasks/{id}/settlement`` to give clients an
            explicit settlement-status read path.
        """
        task = await self.get_task(task_id)

        if task.creator_id != approver_id:
            raise PermissionError("Only the task creator can approve")

        # In-memory state-machine transition (raises if already moved).
        # ``task.complete()`` checks ``status == SUBMITTED`` itself, but that
        # check alone is not concurrency-safe — see the docstring above.
        task.complete(approver_id, notes)

        # === Step 1: CAS persist (saga or legacy) ===
        # We deliberately keep the transaction window narrow: only the
        # CAS UPDATE and the outbox INSERT live inside it. Reading the
        # already-completed task for an idempotent return value
        # (``get_task`` on a lost race) is done AFTER the transaction
        # exits — running a second SELECT inside the active UoW would
        # borrow a second connection from the pool, inflate the
        # transaction footprint, and risk PgBouncer pool exhaustion
        # under load.
        won: bool = False
        if self._saga_enabled:
            # Saga path — CAS save and outbox enqueue execute in a
            # single transaction. If either fails the other reverts;
            # the API surfaces 500 and the caller retries.
            # ``_build_review_pass_event`` is pure (no IO) so we build
            # the event before opening the transaction — keeps the
            # session window short.
            assert self.settlement_outbox is not None  # noqa: S101 — narrows for type checker
            assert self.unit_of_work is not None  # noqa: S101 — narrows for type checker
            event = self._build_review_pass_event(task, approver_id, notes)
            try:
                async with self.unit_of_work.transaction() as session:
                    won = await self.repository.compare_and_save(
                        task,
                        expected_status=TaskStatus.SUBMITTED,
                        session=session,
                    )
                    if won:
                        await self.settlement_outbox.enqueue(event, session=session)
                    # Lost-race branch falls through: tx exits with no
                    # dirty changes; the empty commit is harmless and
                    # cheaper than threading rollback through the UoW.
            except Exception as exc:
                # The saga's atomicity guarantee: if we reach here,
                # neither the CAS nor the enqueue committed. The
                # caller sees 500 and we expect a retry.
                logger.error(
                    "complete_task_saga_failed",
                    task_id=task_id,
                    approver_id=approver_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                raise
            if won:
                # Transaction committed atomically. From this point on
                # the outbox row exists and the worker drives
                # settlement to completion asynchronously. The legacy
                # inline branches below are gated by ``_saga_enabled``
                # and only run when the saga is explicitly disabled —
                # no double-write window.
                logger.info(
                    "settlement_outbox_enqueued",
                    task_id=task_id,
                    event_id=event.event_id,
                    step_status=event.step_status,
                )
        else:
            # Legacy path — repository opens / commits its own
            # transaction; no outbox row.
            won = await self.repository.compare_and_save(task, expected_status=TaskStatus.SUBMITTED)

        # Idempotent short-circuit on lost CAS. Runs AFTER the saga
        # transaction has closed, so the SELECT here doesn't share a
        # session with the writer and doesn't hold the writer's lock
        # any longer than necessary.
        if not won:
            logger.info(
                "complete_task_lost_race",
                task_id=task_id,
                approver_id=approver_id,
                path="saga" if self._saga_enabled else "legacy",
            )
            return await self.get_task(task_id)

        # === Step 2: side effects ===
        # ``record_completion`` is a Redis index update for the task
        # pool. It's NOT part of the settlement saga and runs
        # unconditionally after a successful CAS regardless of which
        # path produced the CAS.
        if task.assignee_id:
            await self.task_pool.record_completion(task_id, task.assignee_id)

        # === Step 3: legacy synchronous payment + reward (emergency-disarm only) ===
        # When saga is enabled (production default), the SettlementWorker
        # drives escrow release + reward distribute + reputation write
        # asynchronously from the outbox row enqueued above — these
        # inline calls are skipped entirely to avoid the previous
        # double-write window where both paths could race on the
        # same payment/reward and silently corrupt each other.
        #
        # When saga is disabled (``OUTBOX_ENQUEUE_REQUIRED=false`` or
        # missing PG deps in Redis-only / test fixtures), this block
        # is the only settlement path: synchronous payment status flip
        # and synchronous reward distribute, exactly as production ran
        # prior to the saga rollout. Kept as an in-place rollback lever
        # so saga can be disarmed without a redeploy.
        if not self._saga_enabled:
            if task.payment_task_id and self.payment_manager:
                try:
                    await self.payment_manager.update_status(
                        task.payment_task_id,
                        "completed",
                    )
                    logger.info(
                        "payment_released",
                        task_id=task_id,
                        payment_task_id=task.payment_task_id,
                    )
                except Exception as e:
                    logger.error("failed_to_release_payment", error=str(e))

            if (
                task.reward_currency.lower() in PLATFORM_CURRENCIES
                and float(task.reward) > 0
                and task.assignee_id
            ):
                reward_result = await self._distribute_reward(
                    task=task,
                    amount=float(task.reward),
                    description=f"Reward for task: {task.title}",
                )
                if reward_result["success"]:
                    logger.info(
                        "reward_distributed",
                        task_id=task_id,
                        agent_amount=reward_result.get("agent_amount"),
                        acn_amount=reward_result.get("acn_amount"),
                        provider_amount=reward_result.get("provider_amount"),
                    )
                else:
                    logger.error(
                        "reward_distribution_failed",
                        task_id=task_id,
                        error=reward_result.get("error"),
                    )

        # Send webhook notification
        await self._notify_webhook(WebhookEventType.TASK_COMPLETED, task)

        # Record activity
        if self.activity and task.assignee_id:
            await self.activity.record_task_approved(
                approver_type=task.creator_type,
                approver_id=approver_id,
                approver_name=task.creator_name,
                agent_id=task.assignee_id,
                agent_name=task.assignee_name or task.assignee_id,
                task_id=task_id,
                task_title=task.title,
                reward=task.reward,
                reward_currency=task.reward_currency,
            )

        logger.info(
            "task_completed",
            task_id=task_id,
            approver_id=approver_id,
            assignee_id=task.assignee_id,
        )

        # ADR-0003 Phase 3 — dissolve any task_scoped child subnets
        # linked to this task. Runs after the full settlement Saga
        # so a cascade failure cannot roll back the completion.
        await self._dissolve_task_scoped_subnets(task_id)

        return task

    async def reject_task(
        self,
        task_id: str,
        reviewer_id: str,
        notes: str | None = None,
    ) -> Task:
        """
        Reject a task submission

        Args:
            task_id: Task identifier
            reviewer_id: Reviewer ID (must be creator)
            notes: Rejection reason

        Returns:
            Updated task

        Raises:
            PermissionError: If reviewer is not the creator

        Concurrency: same CAS pattern as ``complete_task`` — locks the
        ``SUBMITTED → REJECTED`` transition so a concurrent
        ``complete_task`` that already advanced the state machine can't
        be silently overwritten back to REJECTED.
        """
        task = await self.get_task(task_id)

        # Verify reviewer is the creator
        if task.creator_id != reviewer_id:
            raise PermissionError("Only the task creator can reject")

        task.reject(reviewer_id, notes)
        won = await self.repository.compare_and_save(task, expected_status=TaskStatus.SUBMITTED)
        if not won:
            logger.info(
                "reject_task_lost_race",
                task_id=task_id,
                reviewer_id=reviewer_id,
            )
            return await self.get_task(task_id)

        # Record activity
        if self.activity and task.assignee_id:
            await self.activity.record_task_rejected(
                reviewer_type=task.creator_type,
                reviewer_id=reviewer_id,
                reviewer_name=task.creator_name,
                agent_id=task.assignee_id,
                task_id=task_id,
                task_title=task.title,
                reason=notes or "",
            )

        logger.info(
            "task_rejected",
            task_id=task_id,
            reviewer_id=reviewer_id,
        )

        await self._notify_webhook(WebhookEventType.TASK_REJECTED, task)

        # ADR-0003 Phase 3 — dissolve any task_scoped child subnets
        # linked to this task. Runs after the webhook so a cascade
        # failure cannot roll back the rejection.
        await self._dissolve_task_scoped_subnets(task_id)

        return task

    async def cancel_task(self, task_id: str, canceller_id: str) -> Task:
        """
        Cancel a task.

        For multi-participant tasks, also batch-cancels all active participations.

        Concurrency (security audit H3): cancel can be issued from many task
        states (OPEN / IN_PROGRESS / SUBMITTED / REJECTED). We capture the
        current status as the CAS expectation so a concurrent cancel by the
        same creator (or by an admin in the future) only refunds escrow once.
        """
        task = await self.get_task(task_id)

        if task.creator_id != canceller_id:
            raise PermissionError("Only the creator can cancel a task")

        # Already cancelled? Return early (idempotent).
        if task.status == TaskStatus.CANCELLED:
            return task

        # Snapshot the pre-transition status; the CAS below requires the
        # persisted row to still be in this exact state.
        expected_status = task.status

        # Run the in-memory transition first so we know the destination is legal
        # (e.g. ``Task.cancel()`` raises if status is COMPLETED). CAS comes next
        # — only when CAS wins do we touch participations / payment / escrow.
        task.cancel()
        won = await self.repository.compare_and_save(task, expected_status=expected_status)
        if not won:
            logger.info(
                "cancel_task_lost_race",
                task_id=task_id,
                canceller_id=canceller_id,
                expected_status=expected_status.value,
            )
            return await self.get_task(task_id)

        # Batch cancel all active participations for multi-participant tasks.
        # MUST run AFTER the CAS won — otherwise a concurrent ``complete_task``
        # that wins the task-status CAS while we lose ours would still leave
        # us having flipped every participation to CANCELLED, producing
        # ``task=COMPLETED`` with ``participations=all CANCELLED`` (security
        # audit H3 follow-up).
        if task._is_multi():
            cancelled_count = await self.task_pool.batch_cancel_participations(task_id)
            logger.info(
                "participations_cancelled_on_task_cancel",
                task_id=task_id,
                cancelled_count=cancelled_count,
            )

        # Cancel payment if exists
        if task.payment_task_id and self.payment_manager:
            try:
                await self.payment_manager.update_status(
                    task.payment_task_id,
                    "cancelled",
                )
            except Exception as e:
                logger.error("failed_to_cancel_payment", error=str(e))

        # 统一 escrow 退款：human 和 agent 创建者都走 escrow refund
        if self.escrow and task.reward_currency.lower() in PLATFORM_CURRENCIES:
            remaining = task.remaining_budget()
            if remaining > 0:
                result = await self.escrow.refund(
                    user_id=task.creator_id,
                    task_id=task_id,
                    amount=remaining,
                    description=f"Refund for cancelled task: {task.title}",
                )
                if result.success:
                    logger.info(
                        "escrow_refunded_for_task",
                        task_id=task_id,
                        amount=remaining,
                        creator_id=task.creator_id,
                        creator_type=task.creator_type,
                    )
                else:
                    logger.error(
                        "failed_to_refund_escrow",
                        task_id=task_id,
                        creator_type=task.creator_type,
                        error=result.error,
                    )

        # Send webhook notification
        await self._notify_webhook(WebhookEventType.TASK_CANCELLED, task)

        # Record activity
        if self.activity:
            await self.activity.record_task_cancelled(
                canceller_type=task.creator_type,
                canceller_id=canceller_id,
                canceller_name=task.creator_name,
                task_id=task_id,
                task_title=task.title,
            )

        logger.info("task_cancelled", task_id=task_id, canceller_id=canceller_id)

        # ADR-0003 Phase 3 — dissolve any task_scoped child subnets
        # linked to this task. Runs after escrow refund / webhook /
        # activity so a cascade failure cannot roll back the
        # cancellation.
        await self._dissolve_task_scoped_subnets(task_id)

        return task

    async def list_tasks(
        self,
        mode: str | None = None,
        status: TaskStatus | None = None,
        creator_id: str | None = None,
        assignee_id: str | None = None,
        tags: list[str] | None = None,
        group_id: str | None = None,
        board_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        requesting_agent_id: str | None = None,
    ) -> list[Task]:
        """
        List tasks with filters

        Args:
            mode: Filter by mode
            status: Filter by status
            creator_id: Filter by creator
            assignee_id: Filter by assignee
            tags: Filter by agent tags
            group_id: Filter by collaboration group
            board_id: Filter by TaskBoard (metadata hint; SoT enforced by backend)
            limit: Maximum tasks to return
            offset: Pagination offset

        Returns:
            List of tasks
        """
        # Use different repository methods based on filters
        if board_id:
            tasks = await self.repository.find_by_board(board_id, limit)
            # find_by_board 不带状态条件，这里补后置过滤（板内视图常配 status=open）
            if status:
                tasks = [t for t in tasks if t.status == status]
        elif group_id:
            tasks = await self.repository.find_by_group(group_id, limit)
        elif creator_id:
            tasks = await self.repository.find_by_creator(creator_id, limit)
        elif assignee_id:
            tasks = await self.repository.find_by_assignee(assignee_id, limit)
        elif status:
            tasks = await self.repository.find_by_status(status, limit)
        else:
            tasks = await self.task_pool.get_open_tasks(
                mode=mode,
                tags=tags,
                limit=limit,
                offset=offset,
                requesting_agent_id=requesting_agent_id,
            )

        return tasks

    async def get_tasks_for_agent(
        self,
        agent_tags: list[str],
        limit: int = 20,
    ) -> list[Task]:
        """
        Get tasks suitable for an agent

        Args:
            agent_tags: Agent's tag list
            limit: Maximum tasks to return

        Returns:
            List of matching tasks
        """
        return await self.task_pool.find_tasks_for_agent(agent_tags, limit)

    async def get_agent_task_history(
        self,
        agent_id: str,
        limit: int = 50,
    ) -> list[dict]:
        """Return a condensed task history for an agent, suitable for Dreaming / self-reflection.

        Merges two sources:
        - Single-participant tasks where the agent is the assignee
        - Multi-participant participations

        Each entry contains the task spec, the agent's submission, and all
        review feedback so the agent (or a Harness dreaming loop) can extract
        patterns and update its own memory without issuing multiple API calls.
        """
        results: list[dict] = []
        seen_task_ids: set[str] = set()

        # ── Single-participant tasks (agent was the sole assignee) ──────────
        single_tasks = await self.repository.find_by_assignee(agent_id, limit)
        for task in single_tasks:
            seen_task_ids.add(task.task_id)
            results.append({
                "task_id": task.task_id,
                "task_title": task.title,
                "task_type": task.task_type,
                "task_description": task.description,
                "role": "assignee",
                "status": task.status.value,
                "submission": task.submission,
                "review_notes": task.review_notes,
                "rejection_reason": None,
                "resubmit_count": task.resubmit_count,
                "reward": task.reward,
                "reward_currency": task.reward_currency,
                "participation_id": None,
                "slug": task.subnet_slug,
                "joined_at": task.assigned_at.isoformat() if task.assigned_at else None,
                "submitted_at": task.submitted_at.isoformat() if task.submitted_at else None,
                "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            })

        # ── Multi-participant: agent joined as a participant ─────────────────
        participations = await self.repository.find_participations_by_user(agent_id, limit)
        # Batch-fetch all distinct task IDs (including ones already in single-task results,
        # in the unlikely case the same task appears in both paths)
        multi_task_ids = {p.task_id for p in participations}
        task_cache: dict[str, Task] = {}
        for tid in multi_task_ids:
            try:
                task_cache[tid] = await self.get_task(tid)
            except Exception:  # noqa: BLE001
                pass  # task deleted/inaccessible — skip

        for p in participations:
            task = task_cache.get(p.task_id)
            if not task:
                continue
            results.append({
                "task_id": task.task_id,
                "task_title": task.title,
                "task_type": task.task_type,
                "task_description": task.description,
                "role": "participant",
                "status": p.status.value,
                "submission": p.submission,
                "review_notes": p.review_notes,
                "rejection_reason": p.rejection_reason,
                "resubmit_count": p.resubmit_count,
                "reward": task.reward,
                "reward_currency": task.reward_currency,
                "participation_id": p.participation_id,
                "slug": task.subnet_slug,
                "joined_at": p.joined_at.isoformat() if p.joined_at else None,
                "submitted_at": p.submitted_at.isoformat() if p.submitted_at else None,
                "completed_at": p.completed_at.isoformat() if p.completed_at else None,
            })

        # Sort newest-first by submitted_at, falling back to joined_at
        def _sort_key(e: dict) -> str:
            return e.get("submitted_at") or e.get("joined_at") or ""

        results.sort(key=_sort_key, reverse=True)
        return results[:limit]

    async def is_subnet_member(self, slug: str, agent_id: str) -> bool:
        """Check whether an agent is a member of the given subnet."""
        if not self.subnet_repository:
            return False
        subnet = await self.subnet_repository.find_by_id(slug)
        if not subnet:
            return False
        return agent_id in (subnet.member_agent_ids or set())

    async def _dissolve_task_scoped_subnets(self, task_id: str) -> None:
        """ADR-0003 Phase 3 — best-effort cascade dissolve of any
        ``task_scoped`` subnets bound to ``task_id``.

        Runs at the very tail of ``complete_task`` / ``reject_task`` /
        ``cancel_task``, after the full settlement Saga is durable
        (CAS save, escrow release / refund, activity record,
        platform + Org-Harness webhooks). Placing it last is
        intentional — by the time we reach this method the task
        transition is already final and any failure here must NOT
        roll the transition back.

        Failure modes:

        * ``subnet_repository`` or ``subnet_service`` is missing
          (legacy test fixtures) → silent no-op; production wiring
          in ``api.py`` always supplies both.
        * ``find_by_linked_task`` raises → log ``warning`` and
          return; nothing more to do.
        * ``delete_subnet`` raises ``SubnetNotFoundException`` →
          ``debug`` (concurrent dissolve already won; treated as a
          successful no-op per ADR §"Idempotency on concurrent
          dissolution").
        * Any other ``delete_subnet`` failure → ``warning`` and
          continue with the next match. Orphan subnets left behind
          this way behave as regular ``persistent`` subnets per ADR
          Decision #6 — ops cleanup via manual ``delete_subnet`` or
          a future reconciler.

        Cascade is keyed on ``Subnet.lifecycle == "task_scoped"``
        AFTER the repository lookup, NOT in the repository query
        itself, because ``find_by_linked_task`` returns every subnet
        carrying the linked task ID regardless of lifecycle. A
        ``persistent`` subnet that was promoted out of
        ``task_scoped`` after the task started keeps its
        ``linked_task_id == None`` (set by
        ``SubnetService.promote_to_persistent``), so this filter is
        belt-and-braces — it only kicks in if someone hand-edits a
        persistent subnet's ``linked_task_id`` outside the service
        layer.
        """
        if self.subnet_repository is None or self.subnet_service is None:
            return

        try:
            candidates = await self.subnet_repository.find_by_linked_task(task_id)
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            logger.warning(
                "task_scoped_cascade_lookup_failed",
                task_id=task_id,
                error=str(exc),
            )
            return

        for subnet in candidates:
            if subnet.lifecycle != "task_scoped":
                continue
            try:
                await self.subnet_service.delete_subnet(
                    subnet.slug, owner="system"
                )
                logger.info(
                    "task_scoped_subnet_dissolved",
                    task_id=task_id,
                    subnet_slug=subnet.slug,
                )
            except SubnetNotFoundException:
                # Concurrent dissolve already won — treat as success
                # per ADR §"Idempotency on concurrent dissolution".
                logger.debug(
                    "task_scoped_cascade_subnet_already_gone",
                    task_id=task_id,
                    subnet_slug=subnet.slug,
                )
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                logger.warning(
                    "task_scoped_cascade_failed",
                    task_id=task_id,
                    subnet_slug=subnet.slug,
                    error=str(exc),
                )

    async def _notify_webhook(self, event: WebhookEventType, task: Task) -> None:
        """Send webhook notification.

        Two delivery targets:

        1. Platform-level default webhook (``self.webhook.send_event``) — same
           as before; configured via ``ACN_WEBHOOK_URL`` env.
        2. Per-subnet Org Harness — if the task carries ``harness_url`` in
           ``metadata`` (snapshotted at creation in :meth:`create_task`),
           deliver the same payload there too, HMAC-signed with the snapshotted
           ``harness_secret``. This is what makes Org Harnesses pluggable:
           Paperclip / OpenHarness / etc. only need to register a URL on the
           subnet and they will receive the full task lifecycle.
        """
        if not self.webhook:
            return

        payload = {
            "status": task.status.value,
            "creator_id": task.creator_id,
            "assignee_id": task.assignee_id,
            "reward": task.reward,
            "reward_currency": task.reward_currency,
            "max_participants": task.max_participants,
            "slug": task.subnet_slug,
            "task_type": task.task_type,
            # TaskBoard hints（backend XP 处理器用；SoT 仍是 backend board_tasks 表）
            "board_id": (task.metadata or {}).get("board_id"),
            "xp_reward": (task.metadata or {}).get("xp_reward"),
            # 成长清单 G6：体验招募 vs 官方（勿把 platform_secret 等整包 metadata 打出去）
            "kind": (task.metadata or {}).get("kind"),
            "metadata": {
                k: (task.metadata or {}).get(k)
                for k in ("kind", "target_agent_id", "board_id", "xp_reward")
                if (task.metadata or {}).get(k) is not None
            },
        }

        try:
            await self.webhook.send_event(
                event=event,
                task_id=task.task_id,
                data=payload,
            )
        except Exception as e:
            logger.warning("webhook_notification_failed", error=str(e))

        # Per-subnet Org Harness delivery (uses snapshot on task.metadata).
        harness_url = (task.metadata or {}).get("harness_url")
        if harness_url:
            harness_secret = (task.metadata or {}).get("harness_secret")
            try:
                await self.webhook.send_to(
                    url=harness_url,
                    secret=harness_secret,
                    event=event,
                    task_id=task.task_id,
                    data=payload,
                    outbox=False,  # Org-Harness lifecycle: fire-and-forget, reconcile out-of-band
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "task_harness_webhook_failed",
                    task_id=task.task_id,
                    subnet_slug=task.subnet_slug,
                    webhook_event=event.value if hasattr(event, "value") else str(event),
                    error=str(e),
                )

    async def _notify_participation_webhook(
        self,
        event: WebhookEventType,
        task: Task,
        participation: Participation,
    ) -> None:
        """Send webhook for participation-level events (e.g. PARTICIPATION_REJECTED).

        Extends :meth:`_notify_webhook` with participation-specific fields so
        that Org Harnesses can identify *which* participant was affected and
        trigger targeted follow-up actions (re-assign, notify, grade again).
        """
        if not self.webhook:
            return

        payload = {
            "status": task.status.value,
            "creator_id": task.creator_id,
            "slug": task.subnet_slug,
            "participation_id": participation.participation_id,
            "participant_id": participation.participant_id,
            "participant_name": participation.participant_name,
            "participation_status": participation.status.value,
            "resubmit_count": participation.resubmit_count,
            "max_resubmit_attempts": task.max_resubmit_attempts,
            "rejection_reason": participation.rejection_reason,
            "reward": task.reward,
            "reward_currency": task.reward_currency,
            "task_type": task.task_type,
            # TaskBoard hints（backend XP 处理器用；SoT 仍是 backend board_tasks 表）
            "board_id": (task.metadata or {}).get("board_id"),
            "xp_reward": (task.metadata or {}).get("xp_reward"),
            "kind": (task.metadata or {}).get("kind"),
            "metadata": {
                k: (task.metadata or {}).get(k)
                for k in ("kind", "target_agent_id", "board_id", "xp_reward")
                if (task.metadata or {}).get(k) is not None
            },
        }

        try:
            await self.webhook.send_event(
                event=event,
                task_id=task.task_id,
                data=payload,
            )
        except Exception as e:
            logger.warning("participation_webhook_notification_failed", error=str(e))

        harness_url = (task.metadata or {}).get("harness_url")
        if harness_url:
            harness_secret = (task.metadata or {}).get("harness_secret")
            try:
                await self.webhook.send_to(
                    url=harness_url,
                    secret=harness_secret,
                    event=event,
                    task_id=task.task_id,
                    data=payload,
                    outbox=False,  # Org-Harness lifecycle: fire-and-forget, reconcile out-of-band
                )
            except Exception as e:
                logger.warning(
                    "task_harness_participation_webhook_failed",
                    task_id=task.task_id,
                    subnet_slug=task.subnet_slug,
                    webhook_event=event.value if hasattr(event, "value") else str(event),
                    error=str(e),
                )

    async def _get_agent_owner_id(self, agent_id: str) -> str | None:
        """
        Get the owner user_id of an agent

        Args:
            agent_id: Agent identifier

        Returns:
            Owner user_id or None if not found
        """
        if not self.agent_repository:
            logger.warning("agent_repository_not_configured")
            return None

        try:
            agent = await self.agent_repository.find_by_id(agent_id)
            if agent and agent.owner:
                return agent.owner
            return None
        except Exception as e:
            logger.error("failed_to_get_agent_owner", agent_id=agent_id, error=str(e))
            return None

    async def _distribute_reward(
        self,
        task: "Task",
        amount: float,
        description: str,
        participant_id: str | None = None,
    ) -> dict:
        """
        Distribute task reward to agent.

        For multi-participant tasks, uses release_partial for per-completion payouts.
        For single-participant tasks, uses full release via v1 endpoint.
        """
        recipient_id = participant_id or task.assignee_id
        if not recipient_id:
            return {"success": False, "error": "No assignee"}

        # Escrow is opt-in: skip if task doesn't use escrow or client not configured
        if not self.escrow or not task.use_escrow:
            logger.info(
                "escrow_skipped_for_reward",
                task_id=task.task_id,
                use_escrow=task.use_escrow,
                has_escrow_client=bool(self.escrow),
            )
            return {"success": True, "via": "off_chain"}

        try:
            escrow_info = await self.escrow.get_by_task(task.task_id)

            if escrow_info.success and escrow_info.escrow_id:
                logger.info(
                    "reward_via_escrow_release",
                    task_id=task.task_id,
                    escrow_id=escrow_info.escrow_id,
                    recipient_id=recipient_id,
                    is_multi=task._is_multi(),
                )

                # Ensure escrow is activated
                if escrow_info.status == "locked":
                    await self.escrow.accept_v2(
                        escrow_id=escrow_info.escrow_id,
                        assignee_id=recipient_id,
                        assignee_type="agent",
                    )

                # Multi-participant: use release_partial for per-completion payouts
                if task._is_multi():
                    result = await self.escrow.release_partial(
                        escrow_id=escrow_info.escrow_id,
                        recipient_id=recipient_id,
                        recipient_type="agent",
                        amount=amount,
                        notes=description,
                    )
                    if result.success:
                        # Track released amount on the task entity and persist
                        task.release_reward()
                        await self.repository.save(task)
                        logger.info(
                            "reward_released_partial",
                            task_id=task.task_id,
                            escrow_id=escrow_info.escrow_id,
                            recipient_id=recipient_id,
                            amount=amount,
                            agent_amount=result.agent_amount,
                            acn_amount=result.acn_amount,
                            provider_amount=result.provider_amount,
                            proof=result.proof,
                        )
                        return {
                            "success": True,
                            "agent_amount": result.agent_amount,
                            "acn_amount": result.acn_amount,
                            "provider_amount": result.provider_amount,
                            "proof": result.proof,
                            "via": "escrow_release_partial",
                        }
                    else:
                        logger.error(
                            "escrow_release_partial_failed",
                            task_id=task.task_id,
                            error=result.error,
                        )
                        return {"success": False, "error": result.error}

                # Single-participant: full release via v1 path
                if escrow_info.status in ("locked", "in_progress"):
                    await self.escrow.submit_v2(escrow_info.escrow_id)

                result = await self.escrow.release(
                    creator_user_id=task.creator_id,
                    agent_owner_user_id=recipient_id,
                    task_id=task.task_id,
                    amount=amount,
                    description=description,
                )

                if result.success:
                    logger.info(
                        "reward_released_via_escrow",
                        task_id=task.task_id,
                        escrow_id=escrow_info.escrow_id,
                        recipient_id=recipient_id,
                        amount=amount,
                        agent_amount=result.agent_amount,
                        acn_amount=result.acn_amount,
                        provider_amount=result.provider_amount,
                        proof=result.proof,
                    )
                    return {
                        "success": True,
                        "agent_amount": result.agent_amount,
                        "acn_amount": result.acn_amount,
                        "provider_amount": result.provider_amount,
                        "proof": result.proof,
                        "via": "escrow_release",
                    }
                else:
                    logger.error(
                        "escrow_release_failed",
                        task_id=task.task_id,
                        error=result.error,
                    )
                    return {"success": False, "error": result.error}
            else:
                logger.info(
                    "reward_via_v1_escrow_release",
                    task_id=task.task_id,
                    recipient_id=recipient_id,
                )
                result = await self.escrow.release(
                    creator_user_id=task.creator_id,
                    agent_owner_user_id=recipient_id,
                    task_id=task.task_id,
                    amount=amount,
                    description=description,
                )
                if result.success:
                    return {
                        "success": True,
                        "agent_amount": result.agent_amount,
                        "acn_amount": result.acn_amount,
                        "provider_amount": result.provider_amount,
                        "proof": result.proof,
                        "via": "v1_escrow_release",
                    }
                else:
                    return {"success": False, "error": result.error}

        except Exception as e:
            logger.error(
                "reward_distribution_failed",
                task_id=task.task_id,
                recipient_id=recipient_id,
                error=str(e),
            )
            return {"success": False, "error": str(e)}
