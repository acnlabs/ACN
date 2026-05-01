"""SQLAlchemy ORM Models for PostgreSQL

Maps domain entities to relational tables.

Design decisions:
- tasks.active_participants_count NOT stored here; Redis counter is authoritative
- agents.api_key stored as plain text (encryption at-rest is Railway's responsibility)
- JSONB used for flexible metadata/config fields
- ARRAY types for tags/subnet_ids (supports @> containment queries)
"""

from datetime import UTC, datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# =============================================================================
# Tasks
# =============================================================================


class TaskModel(Base):
    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    creator_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    creator_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    reward_amount: Mapped[str] = mapped_column(String(64), nullable=False, default="0")
    reward_currency: Mapped[str] = mapped_column(String(32), nullable=False, default="ap_points")
    assignee_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    is_multi_participant: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_completions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    required_tags: Mapped[list[str] | None] = mapped_column("required_skills", ARRAY(String), nullable=True)  # DB column: "required_skills" (backward compat)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    task_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    subnet_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)

    __table_args__ = (
        Index("ix_tasks_mode", "mode"),
        Index("ix_tasks_required_skills", "required_skills", postgresql_using="gin"),
    )


# =============================================================================
# Participations
# =============================================================================


class ParticipationModel(Base):
    __tablename__ = "participations"

    participation_id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    participant_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    participant_name: Mapped[str] = mapped_column(Text, nullable=False)
    participant_type: Mapped[str] = mapped_column(String(32), nullable=False, default="agent")
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    submission: Mapped[str | None] = mapped_column(Text, nullable=True)
    submission_artifacts: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_response_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    review_request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_participations_task_participant", "task_id", "participant_id"),
    )


# =============================================================================
# Agents
# =============================================================================


class AgentModel(Base):
    __tablename__ = "agents"

    agent_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    owner: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="online")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str] | None] = mapped_column("skills", ARRAY(String), nullable=True)  # DB column: "skills" (backward compat)
    subnet_ids: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    api_key: Mapped[str | None] = mapped_column(String, nullable=True)
    auth0_client_id: Mapped[str | None] = mapped_column(String, nullable=True)
    auth0_token_endpoint: Mapped[str | None] = mapped_column(String, nullable=True)
    claim_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    verification_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    referrer_id: Mapped[str | None] = mapped_column(String, nullable=True)
    wallet_address: Mapped[str | None] = mapped_column(String, nullable=True)
    wallet_addresses: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    accepts_payment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    payment_methods: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    token_pricing: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    agent_card: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # SOCIAL.md pointer (URL only; body is fetched on demand by clients).
    # See https://agentsocial.one — clients honor Cache-Control / ETag from
    # the source URL; ACN deliberately does NOT cache the body to avoid
    # turning into a stale mirror of every agent's social profile.
    social_card_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Gateway-level communication policy (see docs/features/acn-communication-economic-model.md).
    # JSONB so we can extend the schema in Phase 2/3 (allowlist, rate_limit,
    # attention_fee thresholds, ...) without another migration. NULL is treated
    # as the implicit default {"mode": "open"} by the domain layer.
    communication_policy: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    agent_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    owner_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_agents_owner_endpoint", "owner", "endpoint"),
        Index("ix_agents_api_key", "api_key", unique=True),
        Index("ix_agents_tags", "skills", postgresql_using="gin"),  # DB column: "skills" (backward compat)
        Index("ix_agents_wallet_addresses", "wallet_addresses", postgresql_using="gin"),
        # Supports mark_offline_stale keyset pagination over ONLINE agents.
        # Partial index keeps it tiny (only live agents) and lets the
        # `WHERE status='online' ORDER BY agent_id LIMIT N` scan be an
        # index-only range seek.
        Index(
            "ix_agents_status_online_agent_id",
            "agent_id",
            postgresql_where="status = 'online'",
        ),
    )


# =============================================================================
# Subnets
# =============================================================================


class SubnetModel(Base):
    __tablename__ = "subnets"

    subnet_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    owner: Mapped[str] = mapped_column(String, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    security_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    member_agent_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    subnet_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


# =============================================================================
# Billing Transactions
# =============================================================================


class BillingTransactionModel(Base):
    __tablename__ = "billing_transactions"

    transaction_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    agent_owner_id: Mapped[str | None] = mapped_column(String, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    total_credits: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    network_fee_credits: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    agent_income_credits: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


# =============================================================================
# Activities
# =============================================================================


class ActivityModel(Base):
    __tablename__ = "activities"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    actor_name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    event_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), index=True
    )


# =============================================================================
# Agent Allowlist (Phase 2 PR #2)
# =============================================================================
#
# Per-agent inbound trust list, paired with ``communication_policy.mode=
# allowlist``. When that mode is active, only senders whose ``agent_id``
# appears in the row's ``target_id`` column reach the recipient's inbox;
# everyone else is diverted into the manifest queue (the "outside the
# trust list, but worth surfacing later" path established by PR #1).
#
# Why a relational table rather than embedding the list inside
# ``agents.communication_policy`` JSONB:
#
# * Schema simplicity. The policy JSONB is supposed to stay small and
#   ``allowed_keys = {"mode", "reject_reason"}`` (see
#   ``services/policy_service.py:validate_policy_dict``); adding a
#   list-typed key would force every read of the policy to scan the
#   members and would explode policy dict size for high-trust agents.
# * Reverse lookups. ``INDEX(target_id)`` lets ops/anti-abuse queries
#   ask "who has this agent in their allowlist" without table-scanning
#   every JSONB row. Public API does not expose this lookup (privacy
#   semantics: the recipient's allowlist is private), but it is
#   essential for incident response.
# * Cascade semantics. ON DELETE CASCADE on both columns means agent
#   unregistration automatically cleans up dangling allowlist edges
#   without an application-layer sweep. Redis SET cache (TTL 30s) is
#   the eventual-consistency layer; a sweep job is unnecessary.
class AgentAllowlistModel(Base):
    __tablename__ = "agent_allowlist"

    # ``owner_id`` and ``target_id`` are both ``agents.agent_id`` strings
    # (NOT UUIDs — see PR #2 plan P0-1; the design doc previously said
    # UUID but the canonical agent identifier in this codebase is
    # String, e.g. ``agent-cursor-v1``). FK to ``agents.agent_id`` lets
    # the database enforce existence + cascade clean-up; the service
    # layer ALSO does an existence check up-front so it can return a
    # clean 404 instead of a raw IntegrityError.
    owner_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("agents.agent_id", ondelete="CASCADE"),
        nullable=False,
    )
    target_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("agents.agent_id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    # Free-form note from the owner ("trusted partner agent", etc.).
    # Surfaced in ``GET /agents/{id}/allowlist`` listing for owner
    # convenience; never exposed to ``target_id``. Capped only by
    # Postgres's TEXT (≈ 1 GB) — application layer truncates at 200
    # chars before write.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Composite primary key — (owner_id, target_id) uniqueness is
        # the natural key, no surrogate id needed. SADD on a Redis SET
        # mirrors this set-shaped semantic on the cache side.
        PrimaryKeyConstraint("owner_id", "target_id"),
        # Reverse lookup index. Not exposed via API; ops-only.
        Index("ix_agent_allowlist_target_id", "target_id"),
    )
