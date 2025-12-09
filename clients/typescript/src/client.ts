/**
 * ACN HTTP Client
 * 
 * Official TypeScript client for ACN REST API.
 */

import type {
  ACNClientOptions,
  AgentInfo,
  AgentRegisterRequest,
  AgentRegisterResponse,
  AgentSearchOptions,
  AgentSearchResponse,
  AgentAnalytics,
  AgentActivity,
  AuditEvent,
  AuditQueryOptions,
  BroadcastBySkillRequest,
  BroadcastRequest,
  DashboardData,
  Message,
  MetricsData,
  PaymentCapability,
  PaymentDiscoveryOptions,
  PaymentStats,
  PaymentTask,
  SendMessageRequest,
  SubnetCreateRequest,
  SubnetCreateResponse,
  SubnetInfo,
  SystemHealth,
} from './types';

/**
 * ACN Client - HTTP API
 * 
 * @example
 * ```typescript
 * import { ACNClient } from '@acn/client';
 * 
 * const client = new ACNClient({ baseUrl: 'http://localhost:9000' });
 * 
 * // Search agents
 * const { agents } = await client.searchAgents({ skills: 'coding' });
 * 
 * // Get agent details
 * const agent = await client.getAgent('agent-123');
 * ```
 */
export class ACNClient {
  private baseUrl: string;
  private timeout: number;
  private headers: Record<string, string>;

  constructor(options: ACNClientOptions | string) {
    if (typeof options === 'string') {
      this.baseUrl = options.replace(/\/$/, '');
      this.timeout = 30000;
      this.headers = {};
    } else {
      this.baseUrl = options.baseUrl.replace(/\/$/, '');
      this.timeout = options.timeout ?? 30000;
      this.headers = options.headers ?? {};
      if (options.apiKey) {
        this.headers['X-API-Key'] = options.apiKey;
      }
    }
  }

  // ============================================
  // Internal HTTP Methods
  // ============================================

  private async request<T>(
    method: string,
    path: string,
    options?: {
      body?: unknown;
      params?: Record<string, string | number | boolean | undefined>;
    }
  ): Promise<T> {
    const url = new URL(`${this.baseUrl}${path}`);
    
    // Add query params
    if (options?.params) {
      Object.entries(options.params).forEach(([key, value]) => {
        if (value !== undefined) {
          url.searchParams.append(key, String(value));
        }
      });
    }

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), this.timeout);

    try {
      const response = await fetch(url.toString(), {
        method,
        headers: {
          'Content-Type': 'application/json',
          ...this.headers,
        },
        body: options?.body ? JSON.stringify(options.body) : undefined,
        signal: controller.signal,
      });

      if (!response.ok) {
        const error = await response.json().catch(() => ({ message: response.statusText }));
        throw new ACNError(response.status, error.detail || error.message || 'Request failed');
      }

      if (response.status === 204) {
        return undefined as T;
      }

      return response.json();
    } finally {
      clearTimeout(timeoutId);
    }
  }

  private get<T>(path: string, params?: Record<string, string | number | boolean | undefined>): Promise<T> {
    return this.request<T>('GET', path, { params });
  }

  private post<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>('POST', path, { body });
  }

  private delete<T>(path: string): Promise<T> {
    return this.request<T>('DELETE', path);
  }

  // ============================================
  // Health & Status
  // ============================================

  /** Check if ACN server is healthy */
  async health(): Promise<{ status: string }> {
    return this.get('/health');
  }

  /** Get server statistics */
  async getStats(): Promise<{
    total_agents: number;
    online_agents: number;
    total_messages: number;
  }> {
    return this.get('/api/v1/stats');
  }

  // ============================================
  // Agent Management
  // ============================================

  /** Register a new agent */
  async registerAgent(agent: AgentRegisterRequest): Promise<AgentRegisterResponse> {
    return this.post('/api/v1/agents/register', agent);
  }

  /** Get agent by ID */
  async getAgent(agentId: string): Promise<AgentInfo> {
    return this.get(`/api/v1/agents/${agentId}`);
  }

  /** Search agents */
  async searchAgents(options?: AgentSearchOptions): Promise<AgentSearchResponse> {
    return this.get('/api/v1/agents', {
      skills: options?.skills,
      status: options?.status,
    });
  }

  /** Unregister an agent */
  async unregisterAgent(agentId: string): Promise<{ success: boolean; message: string }> {
    return this.delete(`/api/v1/agents/${agentId}`);
  }

  /** Send agent heartbeat */
  async heartbeat(agentId: string): Promise<{ success: boolean }> {
    return this.post(`/api/v1/agents/${agentId}/heartbeat`);
  }

  /** Get agent endpoint */
  async getAgentEndpoint(agentId: string): Promise<{ endpoint: string }> {
    return this.get(`/api/v1/agents/${agentId}/endpoint`);
  }

  /** List all available skills */
  async getSkills(): Promise<{ skills: string[]; counts: Record<string, number> }> {
    return this.get('/api/v1/skills');
  }

  // ============================================
  // Subnet Management
  // ============================================

  /** Create a new subnet */
  async createSubnet(request: SubnetCreateRequest): Promise<SubnetCreateResponse> {
    return this.post('/api/v1/subnets', request);
  }

  /** List all subnets */
  async listSubnets(): Promise<{ subnets: SubnetInfo[] }> {
    return this.get('/api/v1/subnets');
  }

  /** Get subnet by ID */
  async getSubnet(subnetId: string): Promise<SubnetInfo> {
    return this.get(`/api/v1/subnets/${subnetId}`);
  }

  /** Delete a subnet */
  async deleteSubnet(subnetId: string, force = false): Promise<{ success: boolean }> {
    return this.request('DELETE', `/api/v1/subnets/${subnetId}`, {
      params: { force },
    });
  }

  /** Get agents in a subnet */
  async getSubnetAgents(subnetId: string): Promise<{ agents: AgentInfo[] }> {
    return this.get(`/api/v1/subnets/${subnetId}/agents`);
  }

  /** Join agent to subnet */
  async joinSubnet(agentId: string, subnetId: string): Promise<{ success: boolean }> {
    return this.post(`/api/v1/agents/${agentId}/subnets/${subnetId}`);
  }

  /** Remove agent from subnet */
  async leaveSubnet(agentId: string, subnetId: string): Promise<{ success: boolean }> {
    return this.delete(`/api/v1/agents/${agentId}/subnets/${subnetId}`);
  }

  /** Get agent's subnets */
  async getAgentSubnets(agentId: string): Promise<{ subnets: string[] }> {
    return this.get(`/api/v1/agents/${agentId}/subnets`);
  }

  // ============================================
  // Communication
  // ============================================

  /** Send message to an agent */
  async sendMessage(request: SendMessageRequest): Promise<{ success: boolean; message_id: string }> {
    return this.post('/api/v1/communication/send', request);
  }

  /** Broadcast message to multiple agents */
  async broadcast(request: BroadcastRequest): Promise<{ success: boolean; delivered_count: number }> {
    return this.post('/api/v1/communication/broadcast', request);
  }

  /** Broadcast message to agents with specific skill */
  async broadcastBySkill(request: BroadcastBySkillRequest): Promise<{ success: boolean; delivered_count: number }> {
    return this.post('/api/v1/communication/broadcast-by-skill', request);
  }

  /** Get message history for an agent */
  async getMessageHistory(
    agentId: string,
    options?: { limit?: number; offset?: number }
  ): Promise<{ messages: Message[] }> {
    return this.get(`/api/v1/communication/history/${agentId}`, options);
  }

  // ============================================
  // Payment Discovery
  // ============================================

  /** Set agent's payment capability */
  async setPaymentCapability(
    agentId: string,
    capability: PaymentCapability
  ): Promise<{ success: boolean }> {
    return this.post(`/api/v1/agents/${agentId}/payment-capability`, capability);
  }

  /** Get agent's payment capability */
  async getPaymentCapability(agentId: string): Promise<PaymentCapability | null> {
    return this.get(`/api/v1/agents/${agentId}/payment-capability`);
  }

  /** Discover agents that accept payments */
  async discoverPaymentAgents(options?: PaymentDiscoveryOptions): Promise<{ agents: AgentInfo[] }> {
    return this.get('/api/v1/payments/discover', {
      method: options?.method,
      network: options?.network,
      min_amount: options?.min_amount,
      max_amount: options?.max_amount,
    });
  }

  /** Get payment task by ID */
  async getPaymentTask(taskId: string): Promise<PaymentTask> {
    return this.get(`/api/v1/payments/tasks/${taskId}`);
  }

  /** Get agent's payment tasks */
  async getAgentPaymentTasks(
    agentId: string,
    options?: { role?: 'payer' | 'payee'; status?: string; limit?: number }
  ): Promise<{ tasks: PaymentTask[] }> {
    return this.get(`/api/v1/payments/tasks/agent/${agentId}`, options);
  }

  /** Get agent's payment statistics */
  async getPaymentStats(agentId: string): Promise<PaymentStats> {
    return this.get(`/api/v1/payments/stats/${agentId}`);
  }

  // ============================================
  // Monitoring & Analytics
  // ============================================

  /** Get Prometheus metrics (text format) */
  async getPrometheusMetrics(): Promise<string> {
    const response = await fetch(`${this.baseUrl}/metrics`);
    return response.text();
  }

  /** Get all metrics */
  async getMetrics(): Promise<MetricsData> {
    return this.get('/api/v1/monitoring/metrics');
  }

  /** Get system health */
  async getSystemHealth(): Promise<SystemHealth> {
    return this.get('/api/v1/monitoring/health');
  }

  /** Get dashboard data */
  async getDashboard(): Promise<DashboardData> {
    return this.get('/api/v1/monitoring/dashboard');
  }

  /** Get agent analytics */
  async getAgentAnalytics(): Promise<{ analytics: AgentAnalytics[] }> {
    return this.get('/api/v1/analytics/agents');
  }

  /** Get specific agent's activity */
  async getAgentActivity(
    agentId: string,
    options?: { start_time?: string; end_time?: string }
  ): Promise<AgentActivity> {
    return this.get(`/api/v1/analytics/agents/${agentId}`, options);
  }

  /** Get message analytics */
  async getMessageAnalytics(): Promise<Record<string, unknown>> {
    return this.get('/api/v1/analytics/messages');
  }

  /** Get latency analytics */
  async getLatencyAnalytics(): Promise<Record<string, unknown>> {
    return this.get('/api/v1/analytics/latency');
  }

  /** Get subnet analytics */
  async getSubnetAnalytics(): Promise<Record<string, unknown>> {
    return this.get('/api/v1/analytics/subnets');
  }

  // ============================================
  // Audit
  // ============================================

  /** Get audit events */
  async getAuditEvents(options?: AuditQueryOptions): Promise<{ events: AuditEvent[] }> {
    return this.get('/api/v1/audit/events', options as Record<string, string | number | boolean | undefined>);
  }

  /** Get recent audit events */
  async getRecentAuditEvents(limit = 100): Promise<{ events: AuditEvent[] }> {
    return this.get('/api/v1/audit/events/recent', { limit });
  }

  /** Get audit statistics */
  async getAuditStats(options?: { start_time?: string; end_time?: string }): Promise<Record<string, unknown>> {
    return this.get('/api/v1/audit/stats', options);
  }
}

/**
 * ACN API Error
 */
export class ACNError extends Error {
  constructor(
    public status: number,
    message: string
  ) {
    super(message);
    this.name = 'ACNError';
  }
}

