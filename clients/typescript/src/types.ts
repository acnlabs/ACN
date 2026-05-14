/**
 * ACN Client Types
 * 
 * Type definitions synced with ACN API models.
 * @see https://github.com/ACNet-AI/ACN
 */

// ============================================
// Agent Types
// ============================================

/** Agent status */
export type AgentStatus = 'online' | 'offline' | 'busy';

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
  subnet_id?: string;
}

// ============================================
// Subnet Types
// ============================================

/** Subnet information */
export interface SubnetInfo {
  id: string;
  name: string;
  description?: string;
  created_at: string;
  agent_count: number;
  metadata?: Record<string, unknown>;
}

/** Subnet creation request */
export interface SubnetCreateRequest {
  name: string;
  description?: string;
  metadata?: Record<string, unknown>;
}

/** Subnet creation response */
export interface SubnetCreateResponse {
  success: boolean;
  subnet_id: string;
  message: string;
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
 */
export interface CommunicationProfile {
  agent_id: string;
  mode: 'open' | 'manifest' | 'allowlist' | 'closed';
  attention_fee_required: boolean;
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
  subnet_id: string | null;
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
  subnet_id?: string | null;
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
































