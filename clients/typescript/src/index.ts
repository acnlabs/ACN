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

// Hosted regions (ADR-0013)
export {
  ACN_HOSTED_URLS,
  hostedBaseUrl,
  normalizeBaseUrl,
  resolveHostedBaseUrl,
} from './regions';
export type { AcnRegion } from './regions';

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
  AgentSearchStatus,
  AgentInfo,
  AgentJoinRequest,
  AgentJoinResponse,
  AgentRegisterRequest,
  AgentRegisterResponse,
  AgentSearchOptions,
  AgentSearchResponse,
  
  // Subnet Types
  SubnetInfo,
  SubnetChildrenListResponse,
  SubnetCreateRequest,
  SubnetCreateResponse,
  SubnetLifecycle,

  // Subnet Admission Types (ADR-0004)
  SubnetJoinPolicy,
  SubnetAllowlistEntry,
  SubnetAllowlistListResponse,
  SubnetJoinRequestRow,
  SubnetJoinRequestListResponse,
  SubnetJoinRequestListOptions,
  SubnetInvitationListResponse,
  SubnetInvitationListOptions,
  SubnetInvitationSendResponse,
  AgentSubnetInvitationsResponse,

  // Communication Types
  MessageType,
  Message,
  AttentionFee,
  SendMessageRequest,
  SendMessageResponse,
  BroadcastStrategy,
  BroadcastRequest,
  BroadcastByTagRequest,
  BroadcastBySkillRequest,
  ManifestEntry,
  ManifestListResponse,
  ManifestContentResponse,
  ManifestMessageType,
  ManifestSendRequest,
  CommunicationProfile,

  // Session Types
  SessionStatus,
  SessionEntry,
  SessionInviteRequest,
  PendingSessionsResponse,

  // Payment Types
  PaymentMethod,
  PaymentNetwork,
  PaymentCapability,
  PaymentTaskStatus,
  PaymentTask,
  PaymentDiscoveryOptions,
  PaymentRoleStats,
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

  // Follow / Social Graph Types
  FollowActionResponse,
  FollowCheckResponse,

  // Communication Policy Types
  CommunicationPolicyMode,
  CommunicationPolicyResponse,

  // Delivery transport Types
  DeliveryTransport,
  DeliveryTransportSet,
  DeliveryResponse,

  // Allowlist Types
  AllowlistActionResponse,
  AllowlistEntry,
  AllowlistListResponse,

  // Org Harness Types
  Org,
  OrgCreateRequest,
  OrgFencing,
  OrgLoopTickResponse,
  OrgWorkCreateRequest,
  OrgWorkItem,
  OrgWorkListResponse,
  OrgWorkStatus,
  OrgWorkUpdateRequest,
  Workspace,
  WorkspaceAttestation,
  WorkspaceAttestationCreateRequest,
  WorkspaceCreateRequest,
} from './types';

// Value exports (constants)
export {
  KNOWN_PAYMENT_TASK_STATUSES,
  KNOWN_INBOX_MESSAGE_STATUSES,
  orgSubnetId,
} from './types';

// Task pool types
export type {
  Task,
  TaskStatus,
  TaskAcceptResponse,
  TaskCreateRequest,
  TaskListOptions,
  TaskListResponse,
  Participation,
  ParticipationListResponse,
  SubnetHarnessRequest,
} from './types';
































