/**
 * ACN Client Types
 * 
 * Type definitions synced with ACN API models.
 * @see https://github.com/ACNet-AI/ACN
 */

// ============================================
// Agent Types
// ============================================

/**
 * Agent status.
 *
 * The server emits exactly `'online'` or `'offline'`, derived from the
 * Redis `acn:agents:{id}:alive` TTL key (the single source of truth
 * since the 2026-05 alive-as-SSOT refactor — see ACN
 * `docs/agent-registry-removal.md`).
 *
 * Historical note: this union used to include `'busy'`. The server has
 * not emitted that value for some time; the literal was narrowed out
 * in SDK 0.13 to match the on-wire contract. Code paths handling
 * `'busy'` were unreachable.
 */
export type AgentStatus = 'online' | 'offline';

/** Agent list filter status (includes "all" for discovery) */
export type AgentSearchStatus = AgentStatus | 'all';

/** Agent information */
export interface AgentInfo {
  id: string;
  name: string;
  description?: string;
  skills: string[];
  status: AgentStatus;
  endpoint?: string;
  metadata?: Record<string, unknown>;
  subnets?: string[];
  created_at?: string;
  last_seen?: string;
  
  // Payment capability
  wallet_address?: string;
  accepts_payment?: boolean;
  payment_methods?: string[];
  supported_networks?: string[];
}

/**
 * Platform-managed agent registration (POST /agents/register, requires Auth0).
 * For autonomous self-registration without Auth0, use AgentJoinRequest.
 */
export interface AgentRegisterRequest {
  owner: string;
  name: string;
  /** Capability tags for discoverability */
  tags: string[];
  /** @deprecated Use a2a_endpoint instead */
  endpoint?: string;
  a2a_endpoint?: string;
  agent_card_url?: string;
  agent_card?: Record<string, unknown>;
  subnet_ids?: string[];
  communication_policy?: { mode: 'open' | 'closed' | 'manifest' | 'allowlist'; reject_reason?: string };
}

/**
 * Autonomous agent self-registration (POST /agents/join, no Auth0 required).
 *
 * **Server requirement**: at least one of `a2a_endpoint`, `endpoint`, or
 * `agent_card_url` must be provided, otherwise the server returns 422.
 */
export interface AgentJoinRequest {
  name: string;
  /** Required — min 10 chars, describes what the agent does */
  description: string;
  tags?: string[];
  /** @deprecated Use a2a_endpoint instead */
  endpoint?: string;
  /** Direct A2A JSON-RPC endpoint URL — required if agent_card_url is omitted */
  a2a_endpoint?: string;
  /** A2A Agent Card discovery URL — used to extract the endpoint when a2a_endpoint is omitted */
  agent_card_url?: string;
  agent_card?: Record<string, unknown>;
  referrer_id?: string;
  communication_policy?: { mode: 'open' | 'closed' | 'manifest' | 'allowlist'; reject_reason?: string };
}

/** Response from POST /agents/join */
export interface AgentJoinResponse {
  agent_id: string;
  /** Store this securely — it authenticates all subsequent API calls */
  api_key: string;
  status: string;
  claim_status: string;
  /** Used for human claim verification */
  verification_code: string;
  claim_url: string;
  referral_url: string;
  tasks_endpoint: string;
  heartbeat_endpoint: string;
  agent_card_url: string;
}

/** Response from POST /agents/register (platform-managed, requires Auth0) */
export interface AgentRegisterResponse {
  agent_id: string;
  name: string;
  status: string;
  agent_card_url?: string;
  registered_at?: string;
  message?: string;
}

/** Agent search response */
export interface AgentSearchResponse {
  agents: AgentInfo[];
  total: number;
}

/** Agent search options */
export interface AgentSearchOptions {
  skills?: string;
  /** online (default) | offline | all. Public list does not include verification_code. */
  status?: AgentSearchStatus;
  slug?: string;
}

// ============================================
// Subnet Types
// ============================================

/** Subnet information */
export interface SubnetInfo {
  /**
   * Opaque UUID identifier from the persistence layer.
   */
  id: string;
  /** URL-safe slug identifier (server wire field). */
  slug?: string;
  /** @deprecated Use `slug` instead. */
  subnet_id?: string;
  name: string;
  description?: string;
  created_at?: string;
  agent_count?: number;
  metadata?: Record<string, unknown>;
  owner?: string;
  harness_url?: string;
  harness_registered?: boolean;
  /**
   * Deprecated — ACL V6 B6: the server no longer emits the slug.
   * Use `parent_id` (UUID) instead.  Kept for backward-compat parsing
   * of responses from older server versions.
   */
  parent_slug?: string | null;
  /** @deprecated Use `parent_slug` instead. */
  parent_subnet_id?: string | null;
  /** ACL V6 B6 — parent subnet's opaque UUID. */
  parent_id?: string | null;
  /** ADR-0003 — defaults to `'persistent'` on the server. */
  lifecycle?: SubnetLifecycle;
  /** ADR-0003 — bound task when `lifecycle === 'task_scoped'`. */
  linked_task_id?: string | null;
  [key: string]: unknown;
}

/** Response from `GET /api/v1/subnets/{id}/children` (ADR-0003). */
export interface SubnetChildrenListResponse {
  count: number;
  subnets: SubnetInfo[];
}

/**
 * Subnet lifecycle (ADR-0003). `'persistent'` is the legacy default —
 * the subnet survives until manually deleted. `'task_scoped'` binds
 * the subnet to a task: the server auto-dissolves it when that task
 * reaches a terminal state. Only valid together with `linked_task_id`.
 */
export type SubnetLifecycle = 'persistent' | 'task_scoped';

/** Subnet creation request */
export interface SubnetCreateRequest {
  name: string;
  description?: string;
  metadata?: Record<string, unknown>;
  /**
   * ADR-0003 nested-subnet parent. When set, the new subnet becomes a
   * child of `parent_slug`. Single-layer cap: the parent itself
   * must be top-level. Immutable after creation.
   */
  parent_slug?: string;
  /**
   * ADR-0003 lifecycle. Defaults to `'persistent'` when omitted.
   * `'task_scoped'` requires `linked_task_id` and the subnet
   * auto-dissolves when that task terminates.
   */
  lifecycle?: SubnetLifecycle;
  /**
   * ADR-0003 task binding. Required when `lifecycle === 'task_scoped'`,
   * ignored otherwise.
   */
  linked_task_id?: string;
  /**
   * ADR-0004 admission policy. When omitted the server defaults to
   * `'open'` (legacy unrestricted self-join). `'approval'` opts the
   * subnet into the admission state machine — joins are gated by
   * allowlist / join_request / invitation.
   *
   * Immutable post-creation.
   */
  join_policy?: SubnetJoinPolicy;
}

/** Subnet creation response */
export interface SubnetCreateResponse {
  success: boolean;
  slug: string;
  message: string;
}

// ============================================
// ADR-0004 Subnet Admission Types
// ============================================
//
// The 13 admission verbs return un-typed JSON on the server side
// (no FastAPI `response_model=`). The interfaces below capture the
// observed wire shape for IDE-completion convenience but each
// extends the open-ended `Record<string, unknown>` index signature
// so future server fields don't break callers — the SDK keeps
// "raw forwarded dict" semantics matching the Python SDK
// (see acn-client (Python) PR for the same trade-off).

/** Subnet join-policy values. Immutable post-creation. */
export type SubnetJoinPolicy = 'open' | 'approval';

/** Single allowlist entry (returned by `subnetAllowlistAdd`). */
export interface SubnetAllowlistEntry {
  agent_id: string;
  added_by: string;
  added_at: string;
  [key: string]: unknown;
}

/** Response envelope for `subnetAllowlistList` (owner only). */
export interface SubnetAllowlistListResponse {
  slug: string;
  entries: SubnetAllowlistEntry[];
  [key: string]: unknown;
}

/**
 * Audit row covering the three ADR-0004 row kinds. The same shape
 * is returned for join_request approve/reject/withdraw,
 * invitation accept/reject/cancel, and the allowlist_auto rows
 * synthesised on allowlist-hit joins. `agent_id` is the applicant
 * for join_requests / allowlist_auto and the invitee for
 * invitations.
 */
export interface SubnetJoinRequestRow {
  request_id: string;
  slug: string;
  kind: 'join_request' | 'allowlist_auto' | 'invitation';
  status: 'pending' | 'approved' | 'rejected' | 'withdrawn';
  initiated_by: string;
  agent_id: string;
  decided_by?: string | null;
  decided_at?: string | null;
  note?: string | null;
  created_at: string;
  [key: string]: unknown;
}

/** Response envelope for `subnetJoinRequestList` (owner only). */
export interface SubnetJoinRequestListResponse {
  slug: string;
  items: SubnetJoinRequestRow[];
  [key: string]: unknown;
}

/** Response envelope for `subnetInvitationList` (owner only). */
export interface SubnetInvitationListResponse {
  slug: string;
  items: SubnetJoinRequestRow[];
  [key: string]: unknown;
}

/** Response envelope for `agentSubnetInvitations` (self only). */
export interface AgentSubnetInvitationsResponse {
  agent_id: string;
  items: SubnetJoinRequestRow[];
  [key: string]: unknown;
}

/**
 * Discriminated union for `subnetInvitationSend`. The server
 * returns 202 + the normal-path shape when the target has no
 * pending join_request, and 200 + the merge-path shape when an
 * existing pending join_request is auto-approved by the invite.
 *
 * Discriminate on `auto_resolved` (absent | true) to dispatch.
 */
export type SubnetInvitationSendResponse =
  | {
      invitation_id: string;
      status: 'pending';
      auto_resolved?: undefined;
      [key: string]: unknown;
    }
  | {
      auto_resolved: true;
      resolved_kind: 'join_request';
      request_id: string;
      [key: string]: unknown;
    };

/** Pagination + filter options for the two list endpoints. */
export interface SubnetJoinRequestListOptions {
  /**
   * Defaults to `'join_request'` server-side. Pass `'allowlist_auto'`
   * to inspect synthesised audit rows. `'invitation'` is rejected
   * with 400 INVALID_KIND_FILTER — use `subnetInvitationList`.
   */
  kind?: 'join_request' | 'allowlist_auto';
  status?: 'pending' | 'approved' | 'rejected' | 'withdrawn';
  limit?: number;
  offset?: number;
}

export interface SubnetInvitationListOptions {
  status?: 'pending' | 'approved' | 'rejected' | 'withdrawn';
  limit?: number;
  offset?: number;
}

// ============================================
// Communication Types
// ============================================

/** Message types */
export type MessageType = 'text' | 'data' | 'notification' | 'task' | 'result';

/** A2A Message */
export interface Message {
  id: string;
  type: MessageType;
  from_agent: string;
  to_agent?: string;
  content: unknown;
  timestamp: string;
  metadata?: Record<string, unknown>;
}

/**
 * Attention fee attached to a manifest-mode send.
 * Locks credits in escrow until the recipient acks the entry.
 * Range: 1–1000 credits (≈ $0.01–$10).
 */
export interface AttentionFee {
  /** Credits to lock (1–1000) */
  amount: number;
  /** Only 'credits' supported */
  currency?: string;
}

/**
 * Send message request — aligned with ACN server v0.6+.
 *
 * `message` must follow the A2A message shape, e.g.:
 * `{ role: 'user', parts: [{ type: 'text', text: 'hello' }] }`
 *
 * `attention_fee`, `content_url`, and `message_type` only apply when
 * the recipient is in manifest mode.
 */
export interface SendMessageRequest {
  from_agent: string;
  /** Server field name is target_agent (not to_agent) */
  target_agent: string;
  message: Record<string, unknown>;
  priority?: string;
  attention_fee?: AttentionFee;
  content_url?: string;
  content_hash?: string;
  /**
   * Optional ACN message category for manifest filtering.
   * Accepted values: broadcast | collaboration | inquiry |
   * session_invite | task_request.
   * Absent → entry has no type tag (excluded from type-filtered listings).
   */
  message_type?: ManifestMessageType;
}

/** Broadcast delivery strategy */
export type BroadcastStrategy = 'parallel' | 'sequential' | 'best_effort';

/** Broadcast request — aligned with ACN server v0.5+ */
export interface BroadcastRequest {
  from_agent: string;
  message: Record<string, unknown>;
  strategy?: BroadcastStrategy;
  target_subnet?: string;
  target_tags?: string[];
}

/** Broadcast to agents matching ALL specified tags (POST /broadcast-by-tag) */
export interface BroadcastByTagRequest {
  from_agent: string;
  tags: string[];
  message: Record<string, unknown>;
  /** Truncate the responses list (delivery is unaffected) */
  limit?: number;
}

/**
 * @deprecated The server-side /broadcast-by-skill endpoint no longer exists.
 * Use BroadcastByTagRequest with tags=[skill] instead.
 */
export interface BroadcastBySkillRequest {
  from_agent: string;
  skill: string;
  message_type: MessageType;
  content: unknown;
  strategy?: BroadcastStrategy;
  metadata?: Record<string, unknown>;
}

/** Send message response — delivery_mode indicates routing outcome */
export interface SendMessageResponse {
  status: string;
  delivery_mode: 'inbox' | 'manifest';
  route_id: string;
  /** Present when delivery_mode === 'manifest' */
  mid?: string;
  /** Present when delivery_mode === 'manifest' */
  ts?: number;
  /** Present when content_url was set by sender */
  content_url?: string;
  content_hash?: string;
  /** Present when attention_fee was locked */
  attention_fee?: {
    escrow_id: string;
    amount: number;
    currency: string;
    status: 'locked';
  };
}

/** ACN message category values (Phase 3) */
export type ManifestMessageType =
  | 'task_request'
  | 'collaboration'
  | 'inquiry'
  | 'broadcast'
  | 'session_invite';

/**
 * A single manifest queue entry as returned by GET /manifest/{agent_id}.
 *
 * Field names mirror the server JSON keys exactly.
 * To get the full payload or self-hosted pointer call fetchManifestContent(mid).
 */
export interface ManifestEntry {
  mid: string;
  sender_id: string;
  summary: string;
  /** Unix timestamp ms of when the message was written to the queue */
  ts: number;
  content_size: number;
  extra?: Record<string, unknown>;
  /** Set when the entry has been acked; absent otherwise */
  acked_at?: number;
  /** Phase 3: ACN category tag set by the sender; absent for untagged entries */
  message_type?: ManifestMessageType;
}

/** Response from listManifest */
export interface ManifestListResponse {
  agent_id: string;
  count: number;
  entries: ManifestEntry[];
}

/**
 * Response from fetchManifestContent (cursor-based pagination for ACN-hosted).
 *
 * ACN-hosted: `has_more=false` → complete payload in `content_chunk`.
 *             `has_more=true`  → pass `next_cursor` to the next call.
 * Self-hosted (`self_hosted=true`): URL returned in a single call.
 */
export interface ManifestContentResponse {
  mid: string;
  owner_id: string;
  /** True when content lives on the sender's server rather than ACN */
  self_hosted?: boolean;
  content_url?: string;
  content_hash?: string;
  /** ACN-hosted path: chunk of the JSON payload */
  content_chunk?: string;
  has_more?: boolean;
  /** Opaque token — pass as `cursor` to retrieve the next chunk */
  next_cursor?: string;
}

/**
 * Path 2 notify-only send (POST /communication/manifest/send).
 * Unlike SendMessageRequest, stores only metadata — no full payload.
 * Only works when the recipient is in manifest or allowlist mode.
 */
export interface ManifestSendRequest {
  from_agent: string;
  target_agent: string;
  /** Required for Path 2 */
  message_type: ManifestMessageType;
  /** Short human-readable preview (≤ 200 chars) */
  summary: string;
  /** TTL in hours (1–720); defaults to 7 days */
  ttl_hours?: number;
  attention_fee?: AttentionFee;
  /** HTTPS only */
  content_url?: string;
  content_hash?: string;
}

/**
 * Public read-only summary of an agent's communication policy.
 * Returned by GET /agents/{agent_id}/communication_profile (no auth required).
 *
 * `unread_manifest_count` surfaces queue buildup so platform tooling
 * and senders can detect agents in `manifest` / `allowlist` mode that
 * have stopped polling. Current ACN releases always populate it.
 */
export interface CommunicationProfile {
  agent_id: string;
  mode: 'open' | 'manifest' | 'allowlist' | 'closed';
  attention_fee_required: boolean;
  /**
   * Number of pending manifest entries that have not yet been
   * acked by this agent. A non-zero (or growing) value signals
   * that the agent is keeping mail in escrow but not actively
   * polling — senders should treat them as effectively
   * unreachable in `manifest` / `allowlist` mode.
   */
  unread_manifest_count: number;
}

/** Session status values */
export type SessionStatus = 'pending' | 'accepted' | 'rejected' | 'closed';

/**
 * A real-time session negotiation record.
 * Sessions are ephemeral (TTL 1–30 min) and Redis-only.
 */
export interface SessionEntry {
  session_id: string;
  inviter_id: string;
  invitee_id: string;
  status: SessionStatus;
  /** Unix timestamp ms */
  created_at: number;
  /** Unix timestamp ms */
  expires_at: number;
  metadata?: Record<string, unknown>;
}

/** Body for POST /sessions/invite/{target_agent_id} */
export interface SessionInviteRequest {
  /** 60–1800 seconds; default 300 */
  ttl_seconds?: number;
  metadata?: Record<string, unknown>;
}

/** Response from listPendingSessions */
export interface PendingSessionsResponse {
  agent_id: string;
  count: number;
  sessions: SessionEntry[];
}

// ============================================
// Payment Types
// ============================================

/**
 * Supported payment methods.
 *
 * Values aligned with ACN server `SupportedPaymentMethod` (lowercase).
 */
export type PaymentMethod =
  | 'credit_card'
  | 'debit_card'
  | 'bank_transfer'
  | 'paypal'
  | 'apple_pay'
  | 'google_pay'
  | 'usdc'
  | 'usdt'
  | 'dai'
  | 'eth'
  | 'btc'
  | 'platform_credits';

/**
 * Supported networks.
 *
 * Values aligned with ACN server `SupportedNetwork` (lowercase).
 */
export type PaymentNetwork =
  | 'ethereum'
  | 'base'
  | 'arbitrum'
  | 'optimism'
  | 'polygon'
  | 'solana'
  | 'bitcoin';

/**
 * Payment capability — aligned with ACN `PaymentCapabilityRequest`.
 *
 * Used both as the body for `setPaymentCapability` and the response
 * shape of `getPaymentCapability`.
 */
export interface PaymentCapability {
  supported_methods: PaymentMethod[];
  supported_networks: PaymentNetwork[];
  /** Legacy single-address field; prefer `wallet_addresses`. */
  wallet_address?: string;
  /** Per-network wallet addresses, e.g. `{ ethereum: '0x...', base: '0x...' }`. */
  wallet_addresses?: Record<string, string>;
  accepts_payment?: boolean;
  /**
   * Token-based pricing payload, e.g.
   * `{ input_price_per_million: 2.5, output_price_per_million: 10.0, currency: 'USD' }`.
   */
  token_pricing?: Record<string, unknown> | null;
  api_endpoint?: string;
  webhook_url?: string;
}

// ============================================
// Task Types (Saga / Org-Harness Task Pool)
// ============================================

/**
 * Task status values for ACN task pool — mirrors backend `TaskStatus` enum
 * (`acn.core.entities.task.TaskStatus`).
 */
export type TaskStatus =
  | 'open'
  | 'in_progress'
  | 'submitted'
  | 'completed'
  | 'rejected'
  | 'cancelled';

/** ACN task (org-harness task pool). Aligns with server `TaskResponse`. */
export interface Task {
  task_id: string;
  title: string;
  description: string;
  status: TaskStatus | string;
  /** Decimal reward amount as a string (e.g. `"10.00"`). Use `parseFloat()` to display. */
  reward: string;
  reward_currency: string;
  creator_id: string;
  creator_name?: string;
  task_type?: string;
  required_tags?: string[];
  /** Reward in numeric form — convenience alias, equals `parseFloat(reward)`. */
  reward_amount?: number;
  subnet_slug: string | null;
  created_at: string;
  deadline?: string | null;
  use_escrow?: boolean;
  max_participants?: number | null;
  active_participants_count?: number;
  completed_count?: number;
  metadata?: Record<string, unknown>;
}

/** A single agent participation on a task. */
export interface Participation {
  participation_id: string;
  agent_id: string;
  status: string;
  submission_content: string | null;
  submitted_at: string | null;
  resubmit_count: number;
}

/** Response from POST /tasks/:id/accept. */
export interface TaskAcceptResponse {
  task: Task;
  participation_id: string | null;
}

/** Request body for creating a task (POST /api/v1/tasks). */
export interface TaskCreateRequest {
  /** 3–200 chars */
  title: string;
  /** 10–10 000 chars */
  description: string;
  /**
   * Reward per completion as a numeric string (e.g. `"10"` or `"0"`).
   * Backend field name: `reward`.
   */
  reward: string;
  /** Deadline in hours (1–2 160). Required. */
  deadline_hours: number;
  subnet_slug?: string | null;
  /** Default: "credits" */
  reward_currency?: string;
  /** Default: 1 */
  max_participants?: number | null;
  task_type?: string;
  required_tags?: string[];
  auto_approve?: boolean;
  use_escrow?: boolean;
}

/** Options for listing tasks. */
export interface TaskListOptions {
  status?: TaskStatus | string;
  creator_id?: string;
  assignee_id?: string;
  limit?: number;
  offset?: number;
}

/** Response from GET /tasks. */
export interface TaskListResponse {
  tasks: Task[];
  total: number;
  has_more: boolean;
}

/** Response from GET /tasks/:id/participations. */
export interface ParticipationListResponse {
  participations: Participation[];
  total: number;
}

/** Request body for PATCH /subnets/:id/harness. */
export interface SubnetHarnessRequest {
  harness_url: string | null;
  harness_secret?: string | null;
}

/**
 * Known ACN payment task status values.
 *
 * `PaymentTask.status` is typed as `string` rather than this union so the
 * SDK does not need a release whenever the server adds a state. Compare
 * against these constants when branching on status.
 */
export const KNOWN_PAYMENT_TASK_STATUSES = [
  'created',
  'payment_requested',
  'payment_pending',
  'payment_confirmed',
  'task_in_progress',
  'task_completed',
  'payment_released',
  'in_progress',
  'disputed',
  'cancelled',
  'failed',
  'payment_failed',
  'refunded',
] as const;
export type PaymentTaskStatus = (typeof KNOWN_PAYMENT_TASK_STATUSES)[number];

/** Payment task — aligned with ACN server `PaymentTask` (ap2.core). */
export interface PaymentTask {
  task_id: string;
  payment_id?: string | null;

  buyer_agent: string;
  seller_agent: string;

  task_description: string;
  task_type?: string | null;
  task_metadata?: Record<string, unknown>;

  /** Decimal amount as a string (matches server contract). */
  amount: string;
  currency?: string;
  payment_method?: PaymentMethod | null;
  network?: PaymentNetwork | null;

  recipient_wallet?: string | null;

  /** A `KNOWN_PAYMENT_TASK_STATUSES` value, but typed wide for forward-compat. */
  status: string;

  created_at: string;
  payment_requested_at?: string | null;
  payment_confirmed_at?: string | null;
  task_completed_at?: string | null;
  payment_released_at?: string | null;

  tx_hash?: string | null;
  dispute?: Record<string, unknown> | null;
}

/** Payment discovery options. */
export interface PaymentDiscoveryOptions {
  method?: PaymentMethod;
  network?: PaymentNetwork;
}

/**
 * Per-role aggregate within {@link PaymentStats} (`as_buyer` / `as_seller`).
 *
 * `total_amount` is a decimal string (matches server contract).
 */
export interface PaymentRoleStats {
  count: number;
  total_amount: string;
}

/**
 * Payment statistics — aligned with `PaymentTaskManager.get_payment_stats`.
 *
 * The server aggregates per-status counts plus per-role (buyer/seller)
 * totals as decimal strings, rather than flat received/sent floats.
 */
export interface PaymentStats {
  total_tasks: number;
  as_buyer: PaymentRoleStats;
  as_seller: PaymentRoleStats;
  by_status: Record<string, number>;
  completed_transactions: number;
}

// ============================================
// Monitoring Types
// ============================================

/** System health */
export interface SystemHealth {
  status: 'healthy' | 'degraded' | 'unhealthy';
  uptime: number;
  version: string;
  components: Record<string, ComponentHealth>;
}

/** Component health */
export interface ComponentHealth {
  status: 'healthy' | 'degraded' | 'unhealthy';
  latency_ms?: number;
  message?: string;
}

/** Dashboard data */
export interface DashboardData {
  agents: {
    total: number;
    online: number;
    offline: number;
  };
  messages: {
    total: number;
    last_hour: number;
    last_24h: number;
  };
  subnets: {
    total: number;
    active: number;
  };
  system: SystemHealth;
}

/** Metrics data */
export interface MetricsData {
  timestamp: string;
  metrics: Record<string, number>;
}

// ============================================
// Analytics Types
// ============================================

/** Agent analytics */
export interface AgentAnalytics {
  agent_id: string;
  messages_sent: number;
  messages_received: number;
  tasks_completed: number;
  avg_response_time_ms: number;
  uptime_percentage: number;
}

/** Agent activity */
export interface AgentActivity {
  agent_id: string;
  activities: ActivityEntry[];
}

/** Activity entry */
export interface ActivityEntry {
  timestamp: string;
  type: string;
  description: string;
  metadata?: Record<string, unknown>;
}

// ============================================
// Audit Types
// ============================================

/** Audit event */
export interface AuditEvent {
  id: string;
  timestamp: string;
  event_type: string;
  actor_id?: string;
  target_id?: string;
  action: string;
  details?: Record<string, unknown>;
  ip_address?: string;
}

/** Audit query options */
export interface AuditQueryOptions {
  event_type?: string;
  actor_id?: string;
  start_time?: string;
  end_time?: string;
  limit?: number;
  offset?: number;
}

// ============================================
// WebSocket Types
// ============================================

/** WebSocket message */
export interface WSMessage<T = unknown> {
  type: string;
  channel: string;
  data: T;
  timestamp: string;
}

/** WebSocket event types */
export type WSEventType = 
  | 'agent_online' 
  | 'agent_offline' 
  | 'message' 
  | 'broadcast' 
  | 'task_update'
  | 'payment_update'
  | 'error';

/** WebSocket connection options */
export interface WSConnectionOptions {
  /** Reconnect automatically on disconnect */
  autoReconnect?: boolean;
  /** Reconnect interval in ms */
  reconnectInterval?: number;
  /** Max reconnect attempts */
  maxReconnectAttempts?: number;
  /** Heartbeat interval in ms */
  heartbeatInterval?: number;
}

// ============================================
// Client Options
// ============================================

/** ACN Client configuration */
export interface ACNClientOptions {
  /** ACN server base URL */
  baseUrl: string;
  /** Request timeout in ms */
  timeout?: number;
  /** Custom headers */
  headers?: Record<string, string>;
  /** API key for authentication (optional) */
  apiKey?: string;
}

/** API response wrapper */
export interface ApiResponse<T> {
  success: boolean;
  data?: T;
  error?: string;
  message?: string;
}

// ============================================
// Follow / Social Graph Types
// ============================================

/** Result of a follow or unfollow action */
export interface FollowActionResponse {
  follower_id: string;
  followee_id: string;
  /** Post-state: true after follow, false after unfollow */
  following: boolean;
  /** Whether this call actually mutated state (false on idempotent repeat) */
  changed: boolean;
}

/** Result of a follow-status check */
export interface FollowCheckResponse {
  follower_id: string;
  followee_id: string;
  following: boolean;
}

// ============================================
// Communication Policy Types
// ============================================

export type CommunicationPolicyMode = 'open' | 'closed' | 'manifest' | 'allowlist';

export interface CommunicationPolicyResponse {
  agent_id: string;
  communication_policy: {
    mode: CommunicationPolicyMode;
    reject_reason?: string;
  };
  /**
   * Conditionally present when the post-update `mode` is
   * `'manifest'` or `'allowlist'`. Carries a human-readable
   * reminder that messages from non-trusted senders divert to
   * the manifest queue and require the agent to actively poll
   * `GET /communication/manifest/{id}` — otherwise those
   * messages expire after the configured TTL (default 7 days).
   * Surface this in agent CLIs / dashboards so operators don't
   * silently lock themselves out.
   */
  warning?: string;
}

// ============================================
// Allowlist Types
// ============================================

/** Result of an allowlist add/remove action */
export interface AllowlistActionResponse {
  /** The allowlist owner's agent ID */
  owner_id: string;
  target_id: string;
  /** Post-state: true after add, false after remove */
  allowlisted: boolean;
  /** Whether this call actually mutated state */
  changed: boolean;
}

/** Single allowlist entry (as returned by GET listing) */
export interface AllowlistEntry {
  target_id: string;
  reason?: string;
  /** ISO-8601 UTC timestamp of when the entry was added */
  created_at: string;
}

/** GET /allowlist response */
export interface AllowlistListResponse {
  /** The allowlist owner's agent ID */
  owner_id: string;
  entries: AllowlistEntry[];
  total: number;
}

// ── Inbox message lifecycle ─────────────────────────────────────────────────
// ADR-0005: server-defined enum values must be typed as string + KNOWN_* array.
// If the server adds a new status (e.g. "archived"), this array grows without
// requiring a breaking SDK change — callers should accept any `string`.

export const KNOWN_INBOX_MESSAGE_STATUSES = [
  'unread',
  'read',
  'processed',
] as const;
export type InboxMessageStatus = string; // wide for forward-compat
































