/**
 * @acn/client - Official TypeScript client for ACN
 * 
 * Agent Collaboration Network (ACN) is an open-source infrastructure
 * for AI agent registration, discovery, communication, and payments.
 * 
 * @example
 * ```typescript
 * import { ACNClient, ACNRealtime } from '@acn/client';
 * 
 * // HTTP Client
 * const client = new ACNClient('http://localhost:9000');
 * const agents = await client.searchAgents({ skills: 'coding' });
 * 
 * // Real-time WebSocket
 * const realtime = new ACNRealtime('ws://localhost:9000');
 * realtime.subscribe('agents', (msg) => console.log(msg));
 * await realtime.connect();
 * ```
 * 
 * @packageDocumentation
 */

// HTTP Client
export { ACNClient, ACNError } from './client';

// WebSocket Client
export { ACNRealtime, subscribeToACN } from './realtime';
export type { WSEventHandler, WSState } from './realtime';

// Types
export type {
  // Client Options
  ACNClientOptions,
  ApiResponse,
  
  // Agent Types
  AgentStatus,
  AgentInfo,
  AgentRegisterRequest,
  AgentRegisterResponse,
  AgentSearchOptions,
  AgentSearchResponse,
  
  // Subnet Types
  SubnetInfo,
  SubnetCreateRequest,
  SubnetCreateResponse,
  
  // Communication Types
  MessageType,
  Message,
  SendMessageRequest,
  BroadcastStrategy,
  BroadcastRequest,
  BroadcastBySkillRequest,
  
  // Payment Types
  PaymentMethod,
  PaymentNetwork,
  PaymentCapability,
  PaymentTaskStatus,
  PaymentTask,
  PaymentDiscoveryOptions,
  PaymentStats,
  
  // Monitoring Types
  SystemHealth,
  ComponentHealth,
  DashboardData,
  MetricsData,
  
  // Analytics Types
  AgentAnalytics,
  AgentActivity,
  ActivityEntry,
  
  // Audit Types
  AuditEvent,
  AuditQueryOptions,
  
  // WebSocket Types
  WSMessage,
  WSEventType,
  WSConnectionOptions,
} from './types';

