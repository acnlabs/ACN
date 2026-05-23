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
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
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
    reward_currency: Mapped[str] = mapped_column(String(32), nullable=False, default="credits")
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
    resubmit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

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
    # ``status`` column deliberately removed in the alive-as-single-source
    # phase 2 refactor — see ``alembic/versions/f7b9c2d4e8a1_drop_agents_status_column.py``.
    # Online-ness is derived from the Redis ``acn:agents:{id}:alive``
    # TTL key at read time (``AgentService.is_alive`` /
    # ``filter_alive``). The API-layer field ``AgentInfo.status``
    # remains unchanged and is computed in the route serializers.
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
        # ``ix_agents_status_online_agent_id`` deliberately removed
        # alongside the ``status`` column itself — see the migration
        # ``f7b9c2d4e8a1_drop_agents_status_column.py``. Its only
        # reader (``mark_offline_stale``) is gone since Phase 1.
    )


# =============================================================================
# Subnets
# =============================================================================


class SubnetModel(Base):
    __tablename__ = "subnets"

    # ``slug`` is the URL-safe human-readable primary key (renamed from
    # ``subnet_id`` in migration ``xxxx_rename_subnet_id_to_slug``).
    slug: Mapped[str] = mapped_column("slug", String, primary_key=True)
    # Opaque UUID — secondary identifier for SubnetStub privacy.
    # Server-default ``gen_random_uuid()`` fills the column on INSERT
    # so existing call sites that don't set ``id`` keep working.
    # See ``acn/core/entities/subnet.py`` §Identifiers and
    # ``alembic/versions/2b3c4d5e6f7a_add_subnet_uuid.py``.
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        nullable=False,
        unique=True,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    owner: Mapped[str] = mapped_column(String, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    security_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    member_agent_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    subnet_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    harness_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    harness_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Nesting fields (ADR-0003). Both ``default`` (Python / ORM path)
    # and ``server_default`` (DDL path) are set on ``lifecycle`` so a
    # fresh INSERT through the ORM matches a backfilled row inserted
    # via Alembic — avoids the "ORM default ≠ DB default" drift trap.
    # ``parent_slug`` renamed from ``parent_subnet_id`` in same migration.
    parent_slug: Mapped[str | None] = mapped_column("parent_slug", String, nullable=True)
    lifecycle: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="persistent",
        server_default="persistent",
    )
    linked_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Join admission policy (ADR-0004). Same ``default`` + ``server_default``
    # discipline as ``lifecycle`` — keeps ORM INSERTs and Alembic
    # backfills converging on the same default. The Alembic migration
    # (``f0a1b2c3d4e5_add_subnet_join_policy_field``) flips existing
    # ``is_private=true`` rows from this ``'open'`` default to
    # ``'approval'`` in the same DDL event. ``length=16`` mirrors the
    # ADR data-model table and blocks free-form strings if a future
    # caller bypasses the entity-layer ``Literal`` check.
    join_policy: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="open",
        server_default="open",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        # Partial indexes for nesting lookups (ADR-0003). ``WHERE ...
        # IS NOT NULL`` keeps the index small — top-level subnets
        # without a parent / linked task don't contribute rows.
        Index(
            "subnets_parent_idx",
            "parent_slug",
            postgresql_where=text("parent_slug IS NOT NULL"),
        ),
        Index(
            "subnets_linked_task_idx",
            "linked_task_id",
            postgresql_where=text("linked_task_id IS NOT NULL"),
        ),
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


# =============================================================================
# Settlement Outbox (Saga v0.1)
# =============================================================================
#
# Durable inbox of "settlement events" emitted when a task transitions to
# ``COMPLETED``. Producer: ``task_service.complete_task`` writes a row in the
# same DB transaction that flips ``tasks.status`` (transactional outbox
# pattern — see ``acn/docs/_drafts/settlement-saga-design.md``).
# Consumer: ``SettlementWorker`` claims rows with ``FOR UPDATE SKIP LOCKED``,
# runs the three settlement steps idempotently (escrow release / reward
# distribute / reputation write), and marks the row done.
#
# Why a relational outbox rather than a Redis Streams / Kafka topic:
# atomicity. The whole point of the saga is that the task status change and
# the "you owe me settlement" event are written in the *same* PostgreSQL
# transaction, so a process crash between the two is impossible. Redis or
# Kafka cannot participate in a Postgres ACID transaction, so they would
# reintroduce the very window we are trying to close.
#
# Generic columns (``event_id`` / ``trigger`` / ``payload``) anticipate
# future triggers (``dispute_refund``, ``auto_complete``) without schema
# churn; v0.1 only emits ``trigger='review_pass'``.
class SettlementOutboxModel(Base):
    __tablename__ = "settlement_outbox"

    # Surrogate id — outbox rows are processed in roughly insertion order, but
    # the worker doesn't rely on it (claim_batch keys off
    # ``state`` + ``next_attempt_at``). Useful for ops "show me the last N".
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Business idempotency key — generated by producer as
    # ``uuid5(NS, f"{task_id}:{trigger}")``. UNIQUE means a double enqueue
    # (e.g. retry from the API edge) is silently rejected at the DB layer,
    # so even buggy producers cannot double-spend.
    event_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False, unique=True)

    # Originating task. Not a FK — outbox rows must survive task deletion for
    # audit; in practice tasks are never deleted, but keeping the column
    # FK-free avoids ON DELETE plumbing for an unreachable case.
    # No column-level ``index=True`` here — we declare an explicitly-named
    # index in ``__table_args__`` so the migration name matches.
    task_id: Mapped[str] = mapped_column(String, nullable=False)

    # Discriminator for why the event was emitted. v0.1 only emits
    # ``review_pass``; ``dispute_refund``, ``auto_complete`` etc. are
    # reserved for later triggers, intentionally NOT mixed into v0.1 to
    # keep ``step_status`` schema stable.
    trigger: Mapped[str] = mapped_column(String(64), nullable=False)

    # All context the worker needs to execute settlement without re-reading
    # the (possibly mutated) task row. ``amount`` / ``currency`` /
    # ``agent_id`` / ``creator_user_id`` / ``agent_owner_user_id`` /
    # ``payment_task_id`` / ``reward`` snapshot.
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Coarse state machine — drives worker scheduling.
    #   pending     : freshly enqueued, never tried
    #   retrying    : at least one attempt failed retriably; wait until
    #                 ``next_attempt_at`` then try again
    #   done        : all steps succeeded, terminal success
    #   dead        : exceeded MAX_ATTEMPTS or hit non-retriable error, terminal
    #                 failure (alert + manual intervention via DLQ SQL)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")

    # Per-step bookkeeping so worker can resume from mid-saga without redoing
    # already-done steps (e.g. escrow release succeeded but reward distribute
    # crashed). Shape:
    #   {"escrow_release": "done"|"pending"|"skipped",
    #    "reward_distribute": "done"|"pending"|"skipped",
    #    "reputation_write": "done"|"pending"|"skipped"}
    step_status: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Retry counter. Bumped on each failed attempt. Capped at
    # ``SETTLEMENT_MAX_ATTEMPTS`` (default 12) — at the cap, state→dead.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Last failure detail (str(exception) or HTTP status + body snippet),
    # cleared on successful completion. NULL while pending. Indexed via
    # ix_settlement_outbox_state_next on (state, next_attempt_at), so
    # last_error is *not* part of any index — it's debug breadcrumbs.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # When the worker should next try this row. On enqueue: now(). On
    # retry: ``now() + backoff(attempts)``. Worker query joins this with
    # ``state IN ('pending','retrying')`` to pick eligible rows.
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        # Hot path index: worker's claim_batch query is
        #   WHERE state IN ('pending','retrying') AND next_attempt_at <= now()
        #   ORDER BY next_attempt_at FOR UPDATE SKIP LOCKED
        # ``done`` rows accumulate but are filtered by the partial predicate,
        # so query stays O(pending+retrying) regardless of total table size.
        Index(
            "ix_settlement_outbox_state_next",
            "state",
            "next_attempt_at",
            postgresql_where=text("state IN ('pending', 'retrying')"),
        ),
        # Lookup-by-task index — used by DLQ inspection and replay tools to
        # find "the settlement event for this task". Not on hot path.
        Index("ix_settlement_outbox_task_id", "task_id"),
    )


# =============================================================================
# Reputation Events (Saga v0.1, off-chain reputation container)
# =============================================================================
#
# Why a dedicated off-chain table instead of just writing to ERC-8004 chain:
# v0.1 sidesteps the chain-write rabbit hole (private key custody, gas
# budgeting, nonce management, partial-failure refund). The chain-write
# adapter is reserved for v1; the table schema and write API are deliberately
# shaped to match ERC-8004 semantics so the v1 migration is "replay history
# onto chain" rather than "redesign the events".
#
# Why a separate table rather than appending to ``settlement_outbox.payload``:
# reputation is a permanent agent-side artifact, settlement events are
# transient operational rows. They have different access patterns
# (reputation reads are by agent / aggregated; settlement reads are by
# state). Separating them keeps each table's indexes cheap.
#
# Why ``metadata`` JSONB instead of a FK back to ``tasks``:
# the smoke-test isolation contract (plan §7) says reputation queries must
# filter out events whose source task was a smoke task; doing this via a
# join against ``tasks`` would couple every reputation read to ``tasks``
# schema. Instead, the producer copies the relevant flag into the event's
# own metadata at write time. ``reputation_events`` then stands alone and
# old smoke rows can be archived without touching ``tasks``.
class ReputationEventModel(Base):
    __tablename__ = "reputation_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # The agent being reviewed / validated. Indexed for the most common
    # query "give me the reputation history of agent X". Unbounded
    # ``String`` (matches ``AgentModel.agent_id`` exactly) — review fix R11
    # caught that a 64-char cap here would silent-truncate against the
    # unbounded ``agents.agent_id`` / ``tasks.task_id`` columns the moment
    # any future ID prefix exceeded 64 chars, decoupling reputation rows
    # from their source agent/task without any error signal.
    agent_id: Mapped[str] = mapped_column(String, nullable=False)

    # Originating task. Indexed for "give me reputation events for this task"
    # (used by ops + by the smoke-isolation backfill). Unbounded for the
    # same reason as ``agent_id`` above — must match ``tasks.task_id`` and
    # ``settlement_outbox.task_id`` (both unbounded ``String``).
    task_id: Mapped[str] = mapped_column(String, nullable=False)

    # Discriminator for the event type:
    #   feedback   : counterparty's review of the assignee's delivery
    #                (creator -> assignee feedback at task approval time).
    #   validation : third-party / validator attestation about a task
    #                outcome. v0.1 emits feedback only; the schema reserves
    #                ``validation`` so the v0.2 validator flow doesn't need
    #                a migration. Worker step ``reputation_write`` writes
    #                exactly one ``feedback`` row per accepted task.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)

    # v0.1 ALWAYS NULL — task review has approve/reject but no graded
    # score input. v0.2 introduces a 0-100 score and backfills NULLs as
    # "unscored" (not 0, which would mean "rated zero"). Storage type
    # picked to match ERC-8004 ``Feedback.score`` (uint8). NULL is
    # distinguished from 0 at the application layer.
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Optional pointer to off-chain evidence (image, signed JSON, IPFS).
    # ACN does not store the evidence itself — that would balloon the
    # table. Just the URI. NULL is common (most accepted tasks have no
    # evidence beyond the approval itself).
    evidence_uri: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Who issued the event. For ``feedback``: the task creator id.
    # For ``validation``: the validator agent id. NOT NULL because
    # anonymous reputation is meaningless — every event must be
    # attributable for Sybil resistance and dispute handling.
    # Unbounded ``String`` for the same reason as ``agent_id`` /
    # ``task_id`` above (signer IS an agent_id from another row).
    signer: Mapped[str] = mapped_column(String, nullable=False)

    # Validation attestation payload (signed JSON). ``feedback`` rows
    # leave this NULL; ``validation`` rows store the validator's
    # signed proof so a downstream consumer (chain replay in v1, ops
    # dispute review) can verify authenticity. JSONB so we can query
    # specific keys without rehydration.
    attestation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Event metadata. v0.1 carries ``smoke_test`` (copied from
    # ``task.metadata`` at write time) and is the column the
    # ReputationQueryService filters on to keep smoke-test reputation
    # out of production reads. JSONB column name is ``event_metadata``
    # because SQLAlchemy reserves the unqualified ``metadata`` attribute
    # on the declarative ``Base``.
    event_metadata: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        # Idempotency: ``ReputationService.record_feedback`` is allowed
        # to be called multiple times for the same (agent, task) — the
        # worker retries on transient errors — but only the first call
        # should produce a reputation row. UNIQUE here is the
        # safety net; ``INSERT ... ON CONFLICT DO NOTHING`` in the
        # repository turns the collision into a no-op.
        UniqueConstraint(
            "agent_id",
            "task_id",
            "kind",
            name="uq_reputation_events_agent_task_kind",
        ),
        # "Reputation of agent X" — primary query path. Production
        # reads from this index millions of times more often than from
        # task_id index, so it's defined first.
        Index("ix_reputation_events_agent_id", "agent_id"),
        # "Events for task X" — used by smoke-test backfill / ops.
        Index("ix_reputation_events_task_id", "task_id"),
    )


# =============================================================================
# Subnet Join Requests (ADR-0004 Phase 2 Slice 2.1)
# =============================================================================
#
# Three-in-one table backing the ``SubnetJoinRequest`` entity. Single ``kind``
# discriminator covers ``join_request`` / ``invitation`` / ``allowlist_auto``;
# every other column has uniform semantics across the three flows. See the
# entity's docstring for the per-kind state-transition table and ADR-0004
# §"SubnetJoinRequest schema (three-in-one table)" for the full data-model
# rationale.
#
# Why no FK to ``subnets.slug``: ADR §"Cascade deletion" explicitly
# chooses a manual cascade for symmetry with ADR-0003's parent-subnet
# cascade — the service layer DELETEs both tables in the same transaction
# so cascade behaviour is observable through code, not hidden in DDL. A
# future ADR is free to add the FK once cascade observability is no
# longer a design goal.
#
# The unique partial index on ``(subnet_id, agent_id) WHERE status='pending'``
# is the schema-level enforcement of the "at most one pending per
# (subnet, agent) across all kinds" invariant that the §join branch table
# relies on. Without it, a self-join racing an invitation could create
# two concurrent pending rows that both try to transition to approved.


class SubnetJoinRequestModel(Base):
    __tablename__ = "subnet_join_requests"

    # ``String`` for ``request_id`` (UUID4 stored as text) mirrors the
    # convention every other ID column in this codebase uses (``agent_id``,
    # ``task_id``, ``subnet_id``). UUID column type would force callers to
    # use ``uuid.UUID`` rather than the lower-case-hex string the rest of
    # the codebase passes around.
    request_id: Mapped[str] = mapped_column(String, primary_key=True)
    slug: Mapped[str] = mapped_column("subnet_id", String, nullable=False)
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    # ``length=16`` mirrors the ADR data-model table for ``kind``; the
    # three legal values (``join_request`` is the longest at 13 chars)
    # fit comfortably with headroom for a future fourth kind.
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # ``length=16`` likewise covers the four legal status values
    # (``withdrawn`` is the longest at 9 chars) with headroom.
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    initiated_by: Mapped[str] = mapped_column(String, nullable=False)
    # Nullable: NULL while ``status='pending'``; set on transition out.
    # Entity-layer ``__post_init__`` enforces the bidirectional coherence
    # (NULL iff pending) — this column's nullability tracks it.
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Cap at 500 chars matches ``SubnetJoinRequest._NOTE_MAX_LEN``;
    # ``Text`` column with app-layer enforcement mirrors the same pattern
    # ``AgentAllowlistModel.reason`` uses (DB unbounded, app truncates).
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # **The** invariant of this table. ``UNIQUE … WHERE status='pending'``
        # lets terminal rows (approved / rejected / withdrawn) accumulate
        # freely for audit while blocking a second pending row for the
        # same ``(subnet, agent)`` regardless of kind. Backed by Redis's
        # reverse index ``acn:subnets:{s}:pending_by_agent:{a}`` for the
        # cache layer.
        Index(
            "subnet_join_requests_pending_unique",
            "subnet_id",
            "agent_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        # Owner-facing listing path:
        # ``GET /subnets/{s}/join-requests`` and ``/invitations``. The
        # filter on ``kind`` / ``status`` is application-side; the index
        # only needs ``subnet_id`` to seek.
        Index("ix_subnet_join_requests_subnet_id", "subnet_id"),
        # Invitee-facing listing path:
        # ``GET /agents/{a}/subnet-invitations``. Composite predicate
        # selects the pending invitations the agent must decide; partial
        # index keeps the size proportional to in-flight invitations
        # rather than full audit history.
        Index(
            "ix_subnet_join_requests_agent_pending_invitations",
            "agent_id",
            postgresql_where=text(
                "kind = 'invitation' AND status = 'pending'"
            ),
        ),
    )


# =============================================================================
# Subnet Admission Allowlist (ADR-0004 Phase 2 Slice 2.1)
# =============================================================================
#
# Per-subnet preauthorisation set: an entry preapproves the
# ``(subnet_id, agent_id)`` pair for the §join branch 4 fast path.
# Distinct from ``AgentAllowlistModel`` (which lives in the comm-policy
# namespace, governs **agent-to-agent messaging** under
# ``communication_policy.mode=allowlist``); the table-name prefix
# ``subnet_`` keeps the two namespaces unambiguous in DBA-side queries.
#
# Composite primary key ``(subnet_id, agent_id)`` makes the natural-key
# uniqueness DB-enforced; no surrogate id needed. Mirrors the convention
# ``AgentAllowlistModel`` uses for the same shape.


class SubnetAllowlistModel(Base):
    __tablename__ = "subnet_allowlist"

    slug: Mapped[str] = mapped_column("subnet_id", String, nullable=False)
    # ``agent_id`` references ``agents.agent_id`` — but **no FK**. Symmetric
    # with ``subnet_join_requests``: ADR §"Cascade deletion" makes the
    # cascade manual for observability, and the route-layer existence
    # check (per ADR §SubnetAllowlist "Allowlist add requires the target
    # agent_id to already exist in the agent registry") returns a clean
    # 404 AGENT_NOT_FOUND instead of a raw IntegrityError.
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    # Owner agent_id who added the entry. Required (entity layer rejects
    # empty) so every mutation is audit-traceable; the synthetic
    # ``system:<reason>`` actor convention from
    # ``SubnetJoinRequest.SYSTEM_ALLOWLIST_ACTOR`` covers admin-side
    # paths that lack a real owner identity.
    added_by: Mapped[str] = mapped_column(String, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        PrimaryKeyConstraint("subnet_id", "agent_id"),
        # Reverse lookup: "which subnets is agent X preauthorised on?"
        # — used by the agent-facing dashboard view; ops-only today,
        # cheap enough to keep around for future API surfaces.
        Index("ix_subnet_allowlist_agent_id", "agent_id"),
    )
