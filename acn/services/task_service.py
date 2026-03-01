"""Task Service

Business logic for task management, including AP2 payment integration.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import structlog

from ..core.entities import Participation, ParticipationStatus, Task, TaskMode, TaskStatus
from ..core.interfaces import IAgentRepository, ITaskRepository
from ..infrastructure.task_pool import TaskPool
from ..protocols.ap2 import PaymentTaskManager, WebhookEventType, WebhookService
from .activity_service import ActivityService
from .escrow_client import EscrowClient
from .wallet_client import WalletClient

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
        escrow_client: EscrowClient | None = None,
        agent_repository: IAgentRepository | None = None,
        wallet_client: WalletClient | None = None,
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
            wallet_client: Backend wallet client for agent balance (optional)
        """
        self.repository = repository
        self.task_pool = task_pool or TaskPool(repository)
        self.payment_manager = payment_manager
        self.webhook = webhook_service
        self.activity = activity_service
        self.escrow = escrow_client
        self.agent_repository = agent_repository
        self.wallet_client = wallet_client

    async def create_task(
        self,
        creator_type: str,
        creator_id: str,
        creator_name: str,
        title: str,
        description: str,
        mode: TaskMode = TaskMode.OPEN,
        task_type: str = "general",
        required_skills: list[str] | None = None,
        reward_amount: str = "0",
        reward_currency: str = "USD",
        is_repeatable: bool = False,
        is_multi_participant: bool = False,
        allow_repeat_by_same: bool = False,
        max_completions: int | None = None,
        deadline_hours: int | None = None,
        assignee_id: str | None = None,
        assignee_name: str | None = None,
        approval_type: str = "manual",
        validator_id: str | None = None,
        metadata: dict | None = None,
    ) -> Task:
        """
        Create a new task

        Args:
            creator_type: "human" or "agent"
            creator_id: Creator identifier
            creator_name: Creator display name
            title: Task title
            description: Task description
            mode: Task mode (open/assigned)
            task_type: Task type category
            required_skills: Skills needed to complete
            reward_amount: Reward amount (string for precision)
            reward_currency: Reward currency (USD, USDC, points, etc.)
            is_repeatable: Can be completed multiple times (open mode only)
            max_completions: Maximum completions (None = unlimited)
            deadline_hours: Deadline in hours from now
            assignee_id: Pre-assigned agent ID (assigned mode)
            assignee_name: Pre-assigned agent name
            metadata: Additional metadata

        Returns:
            Created task
        """
        task_id = str(uuid4())

        # Calculate deadline
        deadline = None
        if deadline_hours:
            deadline = datetime.now(UTC) + timedelta(hours=deadline_hours)

        # Calculate total budget
        # - Multi-participant with max: budget = reward_amount × max_completions
        # - Multi-participant unlimited (max_completions=None): budget = reward_amount
        #   (per-completion payouts via release_partial; creator balance checked each time)
        # - Single completion: budget = reward_amount
        reward_float = float(reward_amount) if reward_amount else 0
        if (is_repeatable or is_multi_participant) and max_completions:
            total_budget = str(reward_float * max_completions)
        else:
            total_budget = str(reward_float)
            # For single-participant open tasks, enforce max_completions = 1
            if mode == TaskMode.OPEN and not is_repeatable and not is_multi_participant:
                max_completions = 1

        # Create task entity
        # Note: __post_init__ handles backward compat sync:
        #   is_repeatable=True → is_multi_participant=True
        #   is_multi_participant=True → is_repeatable=True (for old API consumers)
        task = Task(
            task_id=task_id,
            mode=mode,
            creator_type=creator_type,
            creator_id=creator_id,
            creator_name=creator_name,
            title=title,
            description=description,
            task_type=task_type,
            required_skills=required_skills or [],
            reward_amount=reward_amount,
            reward_currency=reward_currency,
            reward_unit="completion",  # Default for now
            total_budget=total_budget,
            released_amount="0",
            is_multi_participant=is_multi_participant or is_repeatable,
            allow_repeat_by_same=allow_repeat_by_same,
            is_repeatable=is_repeatable,
            max_completions=max_completions,
            deadline=deadline,
            approval_type=approval_type,
            validator_id=validator_id,
            metadata=metadata or {},
        )

        # For assigned mode, set assignee if provided
        if mode == TaskMode.ASSIGNED and assignee_id:
            task.assignee_id = assignee_id
            task.assignee_name = assignee_name
            task.assigned_at = datetime.now(UTC)

        # 统一 escrow 锁定：human 和 agent 创建者都走 v2 escrow
        if self.escrow and reward_currency.lower() == "points" and float(total_budget) > 0:
            logger.info(
                "escrow_lock_attempt",
                creator_type=creator_type,
                creator_id=creator_id,
                task_id=task_id,
                amount=float(total_budget),
            )
            result = await self.escrow.lock_v2(
                task_id=task_id,
                creator_id=creator_id,
                creator_type=creator_type,
                amount=float(total_budget),
                description=f"Escrow for task: {title}",
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
        if reward_currency.lower() not in ["points", "0"] and float(reward_amount) > 0:
            if self.payment_manager:
                try:
                    payment_task = await self.payment_manager.create_task(
                        buyer_agent=creator_id,
                        description=f"Payment for task: {title}",
                        amount=reward_amount,
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
                    # Continue without payment - task creator can add later

        # Save to repository and add to pool
        await self.task_pool.add(task)

        # Send webhook notification
        await self._notify_webhook(WebhookEventType.TASK_CREATED, task)

        # Record activity
        if self.activity:
            await self.activity.record_task_created(
                creator_type=creator_type,
                creator_id=creator_id,
                creator_name=creator_name,
                task_id=task_id,
                task_title=title,
                reward_amount=reward_amount,
                reward_currency=reward_currency,
            )

        logger.info(
            "task_created",
            task_id=task_id,
            mode=mode.value,
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

        # ---- Multi-participant path ----
        if task.is_multi_participant:
            return await self._join_task(task, agent_id, agent_name, agent_type)

        # ---- Assigned mode, no assignee yet: create application (participation with APPLIED) ----
        if task.mode == TaskMode.ASSIGNED and task.assignee_id is None:
            # Check duplicate: user already applied?
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

        # ---- Single-participant path (original) ----
        # is_multi_participant is the single source of truth; is_repeatable is kept for API compat only
        if not task.is_multi_participant:
            has_completed = await self.task_pool.has_agent_completed(task_id, agent_id)
            if has_completed:
                raise ValueError("You have already completed this task")

        task.accept(agent_id, agent_name)
        await self.repository.save(task)

        # Update escrow: set assignee + IN_PROGRESS
        if self.escrow and task.reward_currency.lower() == "points":
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
            max_completions=task.max_completions,
            allow_repeat=task.allow_repeat_by_same,
        )

        # Activate escrow pool on first join (LOCKED -> ACTIVE)
        if self.escrow and task.reward_currency.lower() == "points":
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
        if task.is_multi_participant:
            p = await self._resolve_participation(task_id, agent_id, participation_id)
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
            if task.approval_type == "auto":
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
            raise PermissionError("Only the assigned agent can submit")

        task.submit(submission, artifacts)
        await self.repository.save(task)

        # Sync escrow status
        if self.escrow and task.reward_currency.lower() == "points":
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
            approval_type=task.approval_type,
        )

        if task.approval_type == "auto":
            logger.info("auto_approving_task", task_id=task_id)
            task = await self._auto_complete_task(task)
        elif task.approval_type == "validator" and task.validator_id:
            logger.info(
                "validator_approval_pending",
                task_id=task_id,
                validator_id=task.validator_id,
            )

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

        # Distribute reward for points-based tasks (Agent + Owner split)
        if (
            task.reward_currency.lower() == "points"
            and float(task.reward_amount) > 0
            and task.assignee_id
        ):
            reward_result = await self._distribute_reward(
                task=task,
                amount=float(task.reward_amount),
                description=f"Auto-reward for task: {task.title}",
            )
            if reward_result["success"]:
                logger.info(
                    "auto_reward_distributed",
                    task_id=task.task_id,
                    agent_amount=reward_result.get("agent_amount"),
                    owner_amount=reward_result.get("owner_amount"),
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
                reward_amount=task.reward_amount,
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
        if not task.max_completions or new_count < task.max_completions:
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
        if task.reward_currency.lower() == "points" and float(task.reward_amount) > 0:
            reward_result = await self._distribute_reward(
                task=task,
                amount=float(task.reward_amount),
                description=f"Auto-reward for task: {task.title} (participation {p.participation_id})",
                participant_id=p.participant_id,
            )
            if reward_result["success"]:
                logger.info(
                    "auto_reward_distributed_participation",
                    task_id=task.task_id,
                    participation_id=p.participation_id,
                    amount=reward_result.get("agent_amount"),
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

        if not task.is_multi_participant:
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
            if task.reward_currency.lower() == "points" and float(task.reward_amount) > 0:
                await self._distribute_reward(
                    task=task,
                    amount=float(task.reward_amount),
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
                    reward_amount=task.reward_amount,
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
        if task.mode != TaskMode.ASSIGNED or task.assignee_id:
            raise ValueError("Task is not in assigned mode or already has an assignee")
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
        if self.escrow and task.reward_currency.lower() == "points":
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
        if task.mode != TaskMode.ASSIGNED or task.assignee_id:
            raise ValueError("Task is not in assigned mode or already has an assignee")
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

        # Distribute reward for points-based tasks (Agent + Owner split)
        if (
            task.reward_currency.lower() == "points"
            and float(task.reward_amount) > 0
            and task.assignee_id
        ):
            reward_result = await self._distribute_reward(
                task=task,
                amount=float(task.reward_amount),
                description=f"Reward for task: {task.title}",
            )
            if reward_result["success"]:
                logger.info(
                    "reward_distributed",
                    task_id=task_id,
                    agent_amount=reward_result.get("agent_amount"),
                    owner_amount=reward_result.get("owner_amount"),
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
                reward_amount=task.reward_amount,
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
        if task.is_multi_participant:
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
        if self.escrow and task.reward_currency.lower() == "points":
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
        mode: TaskMode | None = None,
        status: TaskStatus | None = None,
        creator_id: str | None = None,
        assignee_id: str | None = None,
        skills: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        """
        List tasks with filters

        Args:
            mode: Filter by mode
            status: Filter by status
            creator_id: Filter by creator
            assignee_id: Filter by assignee
            skills: Filter by skills
            limit: Maximum tasks to return
            offset: Pagination offset

        Returns:
            List of tasks
        """
        # Use different repository methods based on filters
        if creator_id:
            tasks = await self.repository.find_by_creator(creator_id, limit)
        elif assignee_id:
            tasks = await self.repository.find_by_assignee(assignee_id, limit)
        elif status:
            tasks = await self.repository.find_by_status(status, limit)
        else:
            tasks = await self.task_pool.get_open_tasks(
                mode=mode,
                skills=skills,
                limit=limit,
                offset=offset,
            )

        return tasks

    async def get_tasks_for_agent(
        self,
        agent_skills: list[str],
        limit: int = 20,
    ) -> list[Task]:
        """
        Get tasks suitable for an agent

        Args:
            agent_skills: Agent's skill list
            limit: Maximum tasks to return

        Returns:
            List of matching tasks
        """
        return await self.task_pool.find_tasks_for_agent(agent_skills, limit)

    async def _notify_webhook(self, event: WebhookEventType, task: Task) -> None:
        """Send webhook notification"""
        if not self.webhook:
            return

        try:
            await self.webhook.send_event(
                event=event,
                task_id=task.task_id,
                data={
                    "mode": task.mode.value,
                    "status": task.status.value,
                    "creator_id": task.creator_id,
                    "assignee_id": task.assignee_id,
                    "reward_amount": task.reward_amount,
                    "reward_currency": task.reward_currency,
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

        if not self.escrow:
            logger.error("escrow_client_not_configured_for_reward")
            return {"success": False, "error": "Escrow client not configured"}

        try:
            # 尝试通过 v2 escrow 查找对应的 escrow 记录
            escrow_info = await self.escrow.get_by_task(task.task_id)

            if escrow_info.success and escrow_info.escrow_id:
                logger.info(
                    "reward_via_escrow_release",
                    task_id=task.task_id,
                    escrow_id=escrow_info.escrow_id,
                    recipient_id=recipient_id,
                    is_multi=task.is_multi_participant,
                )

                # Ensure escrow is activated
                if escrow_info.status == "locked":
                    await self.escrow.accept_v2(
                        escrow_id=escrow_info.escrow_id,
                        assignee_id=recipient_id,
                        assignee_type="agent",
                    )

                # Multi-participant: use release_partial for per-completion payouts
                if task.is_multi_participant:
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
                        )
                        return {
                            "success": True,
                            "agent_amount": amount,
                            "owner_amount": 0,
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
                    )
                    return {
                        "success": True,
                        "agent_amount": amount,
                        "owner_amount": 0,
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
                        "agent_amount": amount,
                        "owner_amount": 0,
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
