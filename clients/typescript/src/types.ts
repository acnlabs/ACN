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
 * Send message request — aligned with ACN server v0.5+.
 *
 * `message` must follow the A2A message shape, e.g.:
 * `{ role: 'user', parts: [{ type: 'text', text: 'hello' }] }`
 *
 * `attention_fee` and `content_url` only apply when the recipient is
 * in manifest mode; they are silently ignored or return 400 otherwise.
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
}

/** Response from list_manifest */
export interface ManifestListResponse {
  entries: ManifestEntry[];
}

/**
 * Response from fetch_manifest_content.
 * When self_hosted=true, content lives at content_url on the sender's
 * server; the `content` field is absent.
 */
export interface ManifestContentResponse {
  mid: string;
  owner_id: string;
  self_hosted?: boolean;
  content_url?: string;
  content_hash?: string;
  content?: Record<string, unknown>;
}

// ============================================
// Payment Types
// ============================================

/** Supported payment methods */
export type PaymentMethod = 
  | 'USDC' 
  | 'USDT' 
  | 'ETH' 
  | 'DAI' 
  | 'CREDIT_CARD' 
  | 'BANK_TRANSFER'
  | 'PLATFORM_CREDITS';

/** Supported networks */
export type PaymentNetwork = 
  | 'ETHEREUM' 
  | 'POLYGON' 
  | 'BASE' 
  | 'ARBITRUM' 
  | 'OPTIMISM'
  | 'SOLANA';

/** Payment capability */
export interface PaymentCapability {
  accepts_payment: boolean;
  wallet_address?: string;
  supported_methods: PaymentMethod[];
  supported_networks: PaymentNetwork[];
  min_amount?: number;
  max_amount?: number;
  currency?: string;
}

/** Payment task status */
export type PaymentTaskStatus = 
  | 'pending' 
  | 'in_progress' 
  | 'completed' 
  | 'failed' 
  | 'cancelled';

/** Payment task */
export interface PaymentTask {
  id: string;
  payer_agent_id: string;
  payee_agent_id: string;
  amount: number;
  currency: string;
  method: PaymentMethod;
  network?: PaymentNetwork;
  status: PaymentTaskStatus;
  created_at: string;
  updated_at: string;
  transaction_hash?: string;
  metadata?: Record<string, unknown>;
}

/** Payment discovery options */
export interface PaymentDiscoveryOptions {
  method?: PaymentMethod;
  network?: PaymentNetwork;
  min_amount?: number;
  max_amount?: number;
}

/** Payment statistics */
export interface PaymentStats {
  total_received: number;
  total_sent: number;
  transaction_count: number;
  avg_amount: number;
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
































