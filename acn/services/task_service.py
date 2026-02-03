"""Task Service

Business logic for task management, including AP2 payment integration.
"""

from datetime import datetime, timedelta
from uuid import uuid4

import structlog

from ..core.entities import Task, TaskMode, TaskStatus
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
            deadline = datetime.now() + timedelta(hours=deadline_hours)

        # Calculate total budget
        # For non-repeatable: budget = reward_amount × 1
        # For repeatable: budget = reward_amount × max_completions
        reward_float = float(reward_amount) if reward_amount else 0
        if is_repeatable and max_completions:
            total_budget = str(reward_float * max_completions)
        else:
            # Single completion tasks (including assigned mode)
            total_budget = str(reward_float)
            # For non-repeatable open tasks, enforce max_completions = 1
            if mode == TaskMode.OPEN and not is_repeatable:
                max_completions = 1

        # Create task entity
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
            task.assigned_at = datetime.now()

        # Lock escrow for points-based tasks (human creators)
        if (
            self.escrow
            and creator_type == "human"
            and reward_currency.lower() == "points"
            and float(total_budget) > 0
        ):
            # Creator ID should be the user_id for human creators
            result = await self.escrow.lock(
                user_id=creator_id,
                task_id=task_id,
                amount=float(total_budget),
                description=f"Escrow for task: {title}",
            )
            if not result.success:
                raise ValueError(f"Failed to lock budget: {result.error}")
            logger.info(
                "escrow_locked_for_task",
                task_id=task_id,
                amount=total_budget,
                creator_id=creator_id,
            )

        # Deduct from Agent balance for points-based tasks (agent creators)
        if (
            creator_type == "agent"
            and reward_currency.lower() == "points"
            and float(total_budget) > 0
        ):
            # Use WalletClient to call Backend API
            if self.wallet_client:
                result = await self.wallet_client.spend(
                    agent_id=creator_id,
                    amount=float(total_budget),
                    description=f"Task creation: {task_id}",
                )
                if not result.success:
                    raise ValueError(f"Failed to deduct from agent balance: {result.error}")
                logger.info(
                    "agent_balance_deducted_for_task",
                    task_id=task_id,
                    agent_id=creator_id,
                    amount=total_budget,
                    remaining_balance=result.credits,
                )
            # Legacy fallback: use agent_repository directly
            elif self.agent_repository:
                agent = await self.agent_repository.find_by_id(creator_id)
                if not agent:
                    raise ValueError(f"Agent {creator_id} not found")
                try:
                    agent.spend(float(total_budget))
                    await self.agent_repository.save(agent)
                    logger.info(
                        "agent_balance_deducted_for_task_legacy",
                        task_id=task_id,
                        agent_id=creator_id,
                        amount=total_budget,
                        remaining_balance=agent.balance,
                    )
                except ValueError as e:
                    raise ValueError(f"Failed to deduct from agent balance: {e}")
            else:
                raise ValueError("Wallet client or agent repository not configured")

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
    ) -> Task:
        """
        Accept a task

        Args:
            task_id: Task identifier
            agent_id: Accepting agent ID
            agent_name: Accepting agent name

        Returns:
            Updated task

        Raises:
            TaskNotFoundException: If task not found
            ValueError: If task cannot be accepted
        """
        task = await self.get_task(task_id)

        # Check if agent already completed (for non-repeatable tasks)
        if not task.is_repeatable:
            has_completed = await self.task_pool.has_agent_completed(task_id, agent_id)
            if has_completed:
                raise ValueError("You have already completed this task")

        # Accept the task
        task.accept(agent_id, agent_name)
        await self.repository.save(task)

        # Record activity
        if self.activity:
            await self.activity.record_task_accepted(
                agent_id=agent_id,
                agent_name=agent_name,
                task_id=task_id,
                task_title=task.title,
            )

        logger.info(
            "task_accepted",
            task_id=task_id,
            agent_id=agent_id,
        )

        return task

    async def submit_task(
        self,
        task_id: str,
        agent_id: str,
        submission: str,
        artifacts: list[dict] | None = None,
    ) -> Task:
        """
        Submit task result

        Args:
            task_id: Task identifier
            agent_id: Submitting agent ID
            submission: Result/deliverable
            artifacts: Optional artifacts

        Returns:
            Updated task

        Raises:
            TaskNotFoundException: If task not found
            PermissionError: If agent is not the assignee
            ValueError: If task is not in progress
        """
        task = await self.get_task(task_id)

        # Verify assignee
        if task.assignee_id != agent_id:
            raise PermissionError("Only the assigned agent can submit")

        # Submit
        task.submit(submission, artifacts)
        await self.repository.save(task)

        # Record activity
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

        # Handle auto-approval
        if task.approval_type == "auto":
            logger.info(
                "auto_approving_task",
                task_id=task_id,
            )
            # Auto-complete using creator as approver
            task = await self._auto_complete_task(task)

        # Handle validator-based approval (future)
        elif task.approval_type == "validator" and task.validator_id:
            # TODO: Implement validator logic when official tasks are defined
            logger.info(
                "validator_approval_pending",
                task_id=task_id,
                validator_id=task.validator_id,
            )

        return task

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
        from ..protocols.ap2 import WebhookEventType

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
        Cancel a task

        Args:
            task_id: Task identifier
            canceller_id: Person cancelling (for authorization)

        Returns:
            Updated task

        Raises:
            PermissionError: If not the creator
        """
        task = await self.get_task(task_id)

        # Only creator can cancel
        if task.creator_id != canceller_id:
            raise PermissionError("Only the creator can cancel a task")

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

        # Refund escrow for points-based tasks (human creators)
        if (
            self.escrow
            and task.creator_type == "human"
            and task.reward_currency.lower() == "points"
        ):
            # Calculate remaining budget
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
                    )
                else:
                    logger.error(
                        "failed_to_refund_escrow",
                        task_id=task_id,
                        error=result.error,
                    )

        # Refund to Agent balance for points-based tasks (agent creators)
        if task.creator_type == "agent" and task.reward_currency.lower() == "points":
            remaining = task.remaining_budget()
            if remaining > 0:
                try:
                    # Use WalletClient to call Backend API
                    if self.wallet_client:
                        result = await self.wallet_client.receive(
                            agent_id=task.creator_id,
                            amount=remaining,
                            description=f"Refund for cancelled task: {task_id}",
                        )
                        if result.success:
                            logger.info(
                                "agent_balance_refunded_for_task",
                                task_id=task_id,
                                agent_id=task.creator_id,
                                amount=remaining,
                                new_balance=result.credits,
                            )
                        else:
                            logger.error(
                                "failed_to_refund_agent_balance",
                                task_id=task_id,
                                error=result.error,
                            )
                    # Legacy fallback: use agent_repository directly
                    elif self.agent_repository:
                        agent = await self.agent_repository.find_by_id(task.creator_id)
                        if agent:
                            agent.receive(remaining)
                            await self.agent_repository.save(agent)
                            logger.info(
                                "agent_balance_refunded_for_task_legacy",
                                task_id=task_id,
                                agent_id=task.creator_id,
                                amount=remaining,
                                new_balance=agent.balance,
                            )
                except Exception as e:
                    logger.error(
                        "failed_to_refund_agent_balance",
                        task_id=task_id,
                        error=str(e),
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
    ) -> dict:
        """
        Distribute task reward between agent and owner based on owner_share.

        New flow (using WalletClient):
        1. Call Backend agent_add_earnings API which handles:
           - Split based on owner_share
           - Add agent_amount to agent wallet earnings
           - Add owner_amount to owner wallet earnings
        2. Return distribution details

        Legacy flow (using agent_repository):
        1. Get agent from repository
        2. Split reward: agent_amount + owner_amount based on agent.owner_share
        3. Add agent_amount to agent.balance
        4. If owner exists, release owner_amount to owner.earnings via escrow
        5. Save agent

        Args:
            task: Completed task
            amount: Reward amount
            description: Transaction description

        Returns:
            dict with distribution details
        """
        if not task.assignee_id:
            return {"success": False, "error": "No assignee"}

        try:
            # Use WalletClient to call Backend API
            if self.wallet_client:
                result = await self.wallet_client.add_earnings(
                    agent_id=task.assignee_id,
                    amount=amount,
                    description=description,
                )
                if result.success:
                    logger.info(
                        "reward_distributed_via_wallet_client",
                        task_id=task.task_id,
                        agent_id=task.assignee_id,
                        total_amount=amount,
                        agent_amount=result.agent_amount,
                        owner_amount=result.owner_amount,
                    )
                    return {
                        "success": True,
                        "agent_amount": result.agent_amount,
                        "owner_amount": result.owner_amount,
                        "agent_balance": result.credits,
                    }
                else:
                    logger.error(
                        "reward_distribution_failed",
                        task_id=task.task_id,
                        error=result.error,
                    )
                    return {"success": False, "error": result.error}

            # Legacy fallback: use agent_repository directly
            if not self.agent_repository:
                logger.warning("agent_repository_not_configured_for_reward")
                return {"success": False, "error": "Agent repository not configured"}

            # Get agent
            agent = await self.agent_repository.find_by_id(task.assignee_id)
            if not agent:
                logger.error("agent_not_found_for_reward", agent_id=task.assignee_id)
                return {"success": False, "error": "Agent not found"}

            # Calculate split based on owner_share
            agent_amount, owner_amount = agent.add_earnings(amount)

            # Save agent with updated balance
            await self.agent_repository.save(agent)

            logger.info(
                "reward_split_calculated_legacy",
                task_id=task.task_id,
                agent_id=task.assignee_id,
                total_amount=amount,
                agent_amount=agent_amount,
                owner_amount=owner_amount,
                owner_share=agent.owner_share,
            )

            # If owner exists and has owner_amount, release to owner's earnings
            if owner_amount > 0 and agent.owner and self.escrow:
                result = await self.escrow.release(
                    creator_user_id=task.creator_id,
                    agent_owner_user_id=agent.owner,
                    task_id=task.task_id,
                    amount=owner_amount,
                    description=f"{description} (owner share)",
                )
                if result.success:
                    logger.info(
                        "owner_share_released",
                        task_id=task.task_id,
                        owner_id=agent.owner,
                        amount=owner_amount,
                    )
                else:
                    logger.error(
                        "failed_to_release_owner_share",
                        task_id=task.task_id,
                        error=result.error,
                    )

            return {
                "success": True,
                "agent_amount": agent_amount,
                "owner_amount": owner_amount,
                "agent_balance": agent.balance,
            }

        except Exception as e:
            logger.error(
                "reward_distribution_failed",
                task_id=task.task_id,
                agent_id=task.assignee_id,
                error=str(e),
            )
            return {"success": False, "error": str(e)}
