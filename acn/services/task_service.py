"""Task Service

Business logic for task management, including AP2 payment integration.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import structlog

from ..core.entities import Participation, ParticipationStatus, Task, TaskStatus
from ..core.interfaces import IAgentRepository, IEscrowProvider, ISubnetRepository, ITaskRepository
from ..infrastructure.task_pool import TaskPool
from ..protocols.ap2 import PaymentTaskManager, WebhookEventType, WebhookService
from ..protocols.ap2.core import AP_POINTS
from .activity_service import ActivityService

logger = structlog.get_logger()


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
        """
        self.repository = repository
        self.task_pool = task_pool or TaskPool(repository)
        self.payment_manager = payment_manager
        self.webhook = webhook_service
        self.activity = activity_service
        self.escrow = escrow_client
        self.agent_repository = agent_repository
        self.subnet_repository = subnet_repository

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
        reward_currency: str = "ap_points",
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
        subnet_id: str | None = None,
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
            reward_currency: Currency (ap_points, USD, USDC, ETH)
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
            subnet_id=subnet_id,
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
        if reward_currency.lower() not in [AP_POINTS, "points", "0"] and float(reward) > 0:
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
        if task.subnet_id:
            if not self.subnet_repository:
                raise PermissionError("Subnet access control not configured")
            subnet = await self.subnet_repository.find_by_id(task.subnet_id)
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
        await self.repository.save(task)

        # Update escrow: set assignee + IN_PROGRESS
        if self.escrow and task.reward_currency.lower() in (AP_POINTS, "points"):
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
        if self.escrow and task.reward_currency.lower() in (AP_POINTS, "points"):
            try:
                escrow_info = await self.escrow.get_by_task(task.task_id)
                if escrow_info.success and escrow_info.escrow_id:
                    if escrow_info.status == "locked":
                        # First participant: activate the pool
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
            except Exception as e:
                logger.warning("escrow_pool_activate_failed", task_id=task.task_id, error=str(e))

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

        if task.status == TaskStatus.REJECTED:
            task.resubmit(submission, artifacts)
        else:
            task.submit(submission, artifacts)
        await self.repository.save(task)

        # Sync escrow status
        if self.escrow and task.reward_currency.lower() in (AP_POINTS, "points"):
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
            task.reward_currency.lower() in (AP_POINTS, "points")
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
        if task.reward_currency.lower() in (AP_POINTS, "points") and float(task.reward) > 0:
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
            if task.reward_currency.lower() in (AP_POINTS, "points") and float(task.reward) > 0:
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
        if self.escrow and task.reward_currency.lower() in (AP_POINTS, "points"):
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

    async def complete_task(
        self,
        task_id: str,
        approver_id: str,
        notes: str | None = None,
    ) -> Task:
        """
        Complete/approve a task

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
        """
        task = await self.get_task(task_id)

        # Verify approver is the creator
        if task.creator_id != approver_id:
            raise PermissionError("Only the task creator can approve")

        # Complete the task
        task.complete(approver_id, notes)
        await self.repository.save(task)

        # Record completion for the agent
        if task.assignee_id:
            await self.task_pool.record_completion(task_id, task.assignee_id)

        # Release payment if exists
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

        # Distribute reward for points-based tasks
        if (
            task.reward_currency.lower() in (AP_POINTS, "points")
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
        """
        task = await self.get_task(task_id)

        # Verify reviewer is the creator
        if task.creator_id != reviewer_id:
            raise PermissionError("Only the task creator can reject")

        task.reject(reviewer_id, notes)
        await self.repository.save(task)

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

        return task

    async def cancel_task(self, task_id: str, canceller_id: str) -> Task:
        """
        Cancel a task.

        For multi-participant tasks, also batch-cancels all active participations.
        """
        task = await self.get_task(task_id)

        if task.creator_id != canceller_id:
            raise PermissionError("Only the creator can cancel a task")

        # Batch cancel all active participations for multi-participant tasks
        if task._is_multi():
            cancelled_count = await self.task_pool.batch_cancel_participations(task_id)
            logger.info(
                "participations_cancelled_on_task_cancel",
                task_id=task_id,
                cancelled_count=cancelled_count,
            )

        task.cancel()
        await self.repository.save(task)

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
        if self.escrow and task.reward_currency.lower() in (AP_POINTS, "points"):
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

        return task

    async def list_tasks(
        self,
        mode: str | None = None,
        status: TaskStatus | None = None,
        creator_id: str | None = None,
        assignee_id: str | None = None,
        tags: list[str] | None = None,
        group_id: str | None = None,
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
            limit: Maximum tasks to return
            offset: Pagination offset

        Returns:
            List of tasks
        """
        # Use different repository methods based on filters
        if group_id:
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

    async def is_subnet_member(self, subnet_id: str, agent_id: str) -> bool:
        """Check whether an agent is a member of the given subnet."""
        if not self.subnet_repository:
            return False
        subnet = await self.subnet_repository.find_by_id(subnet_id)
        if not subnet:
            return False
        return agent_id in (subnet.member_agent_ids or set())

    async def _notify_webhook(self, event: WebhookEventType, task: Task) -> None:
        """Send webhook notification"""
        if not self.webhook:
            return

        try:
            await self.webhook.send_event(
                event=event,
                task_id=task.task_id,
                data={
                    "status": task.status.value,
                    "creator_id": task.creator_id,
                    "assignee_id": task.assignee_id,
                    "reward": task.reward,
                    "reward_currency": task.reward_currency,
                    "max_participants": task.max_participants,
                },
            )
        except Exception as e:
            logger.warning("webhook_notification_failed", error=str(e))

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
