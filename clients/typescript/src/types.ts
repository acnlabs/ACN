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

/** Agent registration request */
export interface AgentRegisterRequest {
  id: string;
  name: string;
  description?: string;
  skills: string[];
  endpoint?: string;
  metadata?: Record<string, unknown>;
  wallet_address?: string;
  payment_capability?: PaymentCapability;
}

/** Agent registration response */
export interface AgentRegisterResponse {
  success: boolean;
  agent_id: string;
  message: string;
}

/** Agent search response */
export interface AgentSearchResponse {
  agents: AgentInfo[];
  total: number;
}

/** Agent search options */
export interface AgentSearchOptions {
  skills?: string;
  status?: AgentStatus;
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

/** Send message request */
export interface SendMessageRequest {
  from_agent: string;
  to_agent: string;
  message_type: MessageType;
  content: unknown;
  metadata?: Record<string, unknown>;
}

/** Broadcast strategy */
export type BroadcastStrategy = 'all' | 'random' | 'round_robin' | 'load_balanced';

/** Broadcast request */
export interface BroadcastRequest {
  from_agent: string;
  message_type: MessageType;
  content: unknown;
  strategy?: BroadcastStrategy;
  target_agents?: string[];
  metadata?: Record<string, unknown>;
}

/** Broadcast by skill request */
export interface BroadcastBySkillRequest {
  from_agent: string;
  skill: string;
  message_type: MessageType;
  content: unknown;
  strategy?: BroadcastStrategy;
  metadata?: Record<string, unknown>;
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

