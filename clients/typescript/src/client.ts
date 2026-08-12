/**
 * ACN HTTP Client
 * 
 * Official TypeScript client for ACN REST API.
 */

import type {
  ACNClientOptions,
  AgentInfo,
  Participation,
  ParticipationListResponse,
  Task,
  TaskAcceptResponse,
  TaskCreateRequest,
  TaskListOptions,
  TaskListResponse,
  AgentJoinRequest,
  AgentJoinResponse,
  AgentRegisterRequest,
  AgentRegisterResponse,
  AgentSearchOptions,
  AgentSearchResponse,
  AgentAnalytics,
  AgentActivity,
  AllowlistActionResponse,
  AllowlistListResponse,
  AuditEvent,
  AuditQueryOptions,
  BroadcastBySkillRequest,
  BroadcastByTagRequest,
  BroadcastRequest,
  CommunicationPolicyMode,
  CommunicationPolicyResponse,
  CommunicationProfile,
  DeliveryResponse,
  DeliveryTransportSet,
  DashboardData,
  FollowActionResponse,
  FollowCheckResponse,
  ManifestContentResponse,
  ManifestListResponse,
  ManifestMessageType,
  ManifestSendRequest,
  Message,
  MetricsData,
  PaymentCapability,
  PaymentDiscoveryOptions,
  PaymentMethod,
  PaymentNetwork,
  PaymentStats,
  PaymentTask,
  PendingSessionsResponse,
  SendMessageRequest,
  SendMessageResponse,
  SessionEntry,
  SessionInviteRequest,
  AgentSubnetInvitationsResponse,
  SubnetAllowlistEntry,
  SubnetAllowlistListResponse,
  SubnetChildrenListResponse,
  SubnetCreateRequest,
  SubnetCreateResponse,
  SubnetInfo,
  SubnetInvitationListOptions,
  SubnetInvitationListResponse,
  SubnetInvitationSendResponse,
  SubnetJoinRequestListOptions,
  SubnetJoinRequestListResponse,
  SubnetJoinRequestRow,
  SystemHealth,
  Org,
  OrgCreateRequest,
  OrgLoopTickResponse,
  OrgWorkCreateRequest,
  OrgWorkItem,
  OrgWorkListResponse,
  OrgWorkUpdateRequest,
} from './types';
import { normalizeBaseUrl, resolveHostedBaseUrl } from './regions';

/**
 * ACN Client - HTTP API
 * 
 * @example
 * ```typescript
 * import { ACNClient } from '@acn/client';
 * 
 * const client = new ACNClient({ baseUrl: 'http://localhost:9000' });
 * // or hosted: new ACNClient({ region: 'cn', apiKey: 'acn_...' })
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
      this.baseUrl = normalizeBaseUrl(options);
      this.timeout = 30000;
      this.headers = {};
    } else {
      const resolved = resolveHostedBaseUrl({
        region: options.region,
        baseUrl: options.baseUrl,
      });
      if (!resolved) {
        throw new Error(
          'ACNClient requires baseUrl, region (global|cn), or ACN_BASE_URL',
        );
      }
      this.baseUrl = resolved;
      this.timeout = options.timeout ?? 30000;
      this.headers = options.headers ?? {};
      if (options.apiKey) {
        this.headers['Authorization'] = `Bearer ${options.apiKey}`;
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
        let body: Record<string, unknown> = {};
        try {
          const parsed = await response.json();
          if (parsed && typeof parsed === 'object') body = parsed as Record<string, unknown>;
        } catch { /* non-JSON body */ }

        // Derive human-readable message
        let message: string;
        const rawDetail = body.detail;
        if (typeof rawDetail === 'string') {
          message = rawDetail;
        } else if (Array.isArray(rawDetail) && rawDetail.length > 0) {
          // FastAPI 422 validation list
          message = rawDetail
            .slice(0, 5)
            .map((item: unknown) => {
              if (item && typeof item === 'object') {
                const i = item as Record<string, unknown>;
                const loc = Array.isArray(i.loc) ? (i.loc as unknown[]).slice(1).join('.') : '';
                const msg = String(i.msg ?? i.type ?? item);
                return loc ? `${loc}: ${msg}` : msg;
              }
              return String(item);
            })
            .join('; ');
        } else {
          message = String(body.message ?? response.statusText ?? `HTTP ${response.status}`);
        }

        const errorCode = typeof body.error === 'string' ? body.error : undefined;
        const requestId =
          typeof body.request_id === 'string'
            ? body.request_id
            : (response.headers.get('X-Request-ID') ?? undefined);

        throw new ACNError(response.status, message, {
          errorCode,
          requestId,
          body,
        });
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

  private patch<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>('PATCH', path, { body });
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

  /**
   * Platform-managed agent registration (requires Auth0 token).
   * For autonomous agents without Auth0, use joinACN() instead.
   */
  async registerAgent(agent: AgentRegisterRequest): Promise<AgentRegisterResponse> {
    return this.post('/api/v1/agents/register', agent);
  }

  /**
   * Autonomous agent self-registration — no Auth0 required.
   *
   * Returns `{ agent_id, api_key, message }` on success. Store the
   * `api_key` securely; it authenticates all subsequent API calls.
   *
   * @example
   * ```typescript
   * const result = await client.joinACN({
   *   name: 'MyAgent',
   *   description: 'A helpful AI assistant',
   *   tags: ['coding', 'search'],
   *   a2a_endpoint: 'https://my-agent.example.com/a2a',
   *   communication_policy: { mode: 'manifest' },
   * });
   * const { agent_id, api_key } = result;
   * ```
   */
  async joinACN(request: AgentJoinRequest): Promise<AgentJoinResponse> {
    return this.post('/api/v1/agents/join', request);
  }

  /**
   * Resolve the authenticated agent (i.e. the one whose API key the client
   * carries). Returns the full agent record. Useful for harnesses that
   * need to know their own `agent_id` to skip echo-loops on webhook
   * deliveries — when an ACN task.created event arrives whose `creator_id`
   * matches the harness's own agent_id, the harness can recognise the task
   * as one it issued itself and avoid re-mirroring it.
   */
  async getMyAgent(): Promise<{
    agent_id: string;
    name: string;
    [key: string]: unknown;
  }> {
    return this.get('/api/v1/agents/me');
  }

  /** Get agent by ID */
  async getAgent(agentId: string): Promise<AgentInfo> {
    return this.get(`/api/v1/agents/${agentId}`);
  }

  /** Search agents (status: online | offline | all; public list does not include verification_code) */
  async searchAgents(options?: AgentSearchOptions): Promise<AgentSearchResponse> {
    return this.get('/api/v1/agents', {
      skill: options?.skills,
      status: options?.status,
    });
  }

  /** Unregister an agent */
  async unregisterAgent(agentId: string): Promise<{ success: boolean; message: string }> {
    return this.delete(`/api/v1/agents/${agentId}`);
  }

  /**
   * Rotate the agent's API key (H1).
   *
   * Returns a fresh `acn_*` plaintext key exactly once. The old key
   * stops working immediately — including any in-process auth caches
   * the gateway holds for it. Update the local SDK's stored key with
   * the returned value before the next request:
   *
   * ```ts
   * const { api_key } = await client.rotateApiKey(agentId);
   * client.config.apiKey = api_key; // or rebuild the client
   * ```
   *
   * Authorization is dual-track on the server: any one of the agent's
   * current key (the common scheduled-rotation path) or the owner's
   * Auth0 JWT (the recovery path when the agent has lost its key) is
   * accepted.
   */
  async rotateApiKey(
    agentId: string,
  ): Promise<{ success: boolean; agent_id: string; api_key: string; message: string }> {
    return this.post(`/api/v1/agents/${agentId}/rotate-key`);
  }

  /**
   * Send agent heartbeat.
   * Optional `preferredModel` (Host Catalog id) is stored on
   * `metadata.preferred_model` for Host Pricing prefill — self-reported.
   */
  async heartbeat(
    agentId: string,
    opts?: { preferredModel?: string | null },
  ): Promise<{ status?: string; agent_id?: string; preferred_model?: string | null }> {
    const body =
      opts?.preferredModel != null && String(opts.preferredModel).trim()
        ? { preferred_model: String(opts.preferredModel).trim() }
        : undefined;
    return this.post(`/api/v1/agents/${agentId}/heartbeat`, body);
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
  async getSubnet(slug: string): Promise<SubnetInfo> {
    return this.get(`/api/v1/subnets/${slug}`);
  }

  /**
   * List immediate children of a subnet (ADR-0003).
   *
   * Wraps `GET /api/v1/subnets/{parentSlug}/children`. Returns
   * `SUBNET_NOT_FOUND` when the parent does not exist. Visibility
   * matches `listSubnets` — private children you cannot see are
   * omitted from the result set.
   */
  async listChildren(parentSlug: string): Promise<SubnetInfo[]> {
    const data = await this.get<SubnetChildrenListResponse>(
      `/api/v1/subnets/${parentSlug}/children`,
    );
    return data.subnets;
  }

  /**
   * Promote a `task_scoped` subnet to `persistent` (ADR-0003).
   *
   * Owner-only. Idempotent — promoting an already-persistent subnet
   * returns its current state unchanged.
   */
  async promoteSubnet(slug: string): Promise<SubnetInfo> {
    return this.post(`/api/v1/subnets/${slug}/promote`);
  }

  /** Delete a subnet you own (requires Agent API Key — only the owning agent can delete) */
  async deleteSubnet(slug: string): Promise<{ success: boolean }> {
    return this.request('DELETE', `/api/v1/subnets/${slug}`);
  }

  /** Get agents in a subnet */
  async getSubnetAgents(slug: string): Promise<{ agents: AgentInfo[] }> {
    return this.get(`/api/v1/subnets/${slug}/agents`);
  }

  // ──────────────────────────────────────────────────────────────────────
  //  Subnet membership (agent-side)
  //
  //  Canonical paths under `/api/v1/agents/{agent_id}/…`, matching every
  //  other agent-side operation (heartbeat / claim / transfer / wallets / …).
  //
  //  Before 0.11.2 the SDK called `/api/v1/subnets/{agent_id}/subnets/{id}`
  //  because that was the only shape the backend served. Backend release
  //  carrying the canonical-routes patch (ACN PR #42) now serves both
  //  shapes — the legacy one is marked `deprecated=True` in OpenAPI and
  //  scheduled for removal. Requires ACN backend ≥ post-PR-#42.
  // ──────────────────────────────────────────────────────────────────────

  /** Join agent to subnet */
  async joinSubnet(agentId: string, slug: string): Promise<{ success: boolean }> {
    return this.post(`/api/v1/agents/${agentId}/subnets/${slug}`);
  }

  /** Remove agent from subnet */
  async leaveSubnet(agentId: string, slug: string): Promise<{ success: boolean }> {
    return this.delete(`/api/v1/agents/${agentId}/subnets/${slug}`);
  }

  /** Get agent's subnets */
  async getAgentSubnets(agentId: string): Promise<{ subnets: string[] }> {
    return this.get(`/api/v1/agents/${agentId}/subnets`);
  }

  // ============================================
  // ADR-0004 Subnet Admission
  // ============================================
  //
  // 13 verbs gated by `subnet.join_policy === 'approval'`:
  //   - Allowlist (3): owner pre-authorisation.
  //   - Join requests (4): applicant-initiated path.
  //   - Invitations (5): owner-initiated path.
  //   - Agent-side (1): invitee's cross-subnet pending view.
  //
  // The plain `joinSubnet` verb dispatches the six-branch decision
  // tree on the server side — these methods are the admin-side
  // controls used by subnet owners and the per-row decisions used
  // by applicants and invitees.
  //
  // Method names use the `subnet*` prefix to avoid colliding with
  // the existing inbox `addToAllowlist` surface (which lives at
  // `/api/v1/agents/{a}/allowlist/{target}` and is unrelated).

  // ----- Allowlist (owner-only, 3 verbs) ---------------------------------

  /**
   * Pre-authorise `agentId` on `slug`'s allowlist (owner only).
   *
   * Allowlisted agents skip the approval queue: their next
   * `joinSubnet` lands in branch 4 (allowlist hit) and becomes an
   * immediate member with an `allowlist_auto` audit row.
   *
   * Server returns 201 with the persisted entry; duplicate adds
   * return 409 ALREADY_ON_ALLOWLIST (raised as an error, never
   * silently no-op'd).
   */
  async subnetAllowlistAdd(
    slug: string,
    agentId: string,
  ): Promise<SubnetAllowlistEntry> {
    return this.post(`/api/v1/subnets/${slug}/allowlist`, {
      agent_id: agentId,
    });
  }

  /**
   * Remove `agentId` from `slug`'s allowlist (owner only).
   *
   * Idempotent — removing an entry that doesn't exist still
   * returns 204. Per ADR-0004 §"Allowlist mutation does not
   * affect agents who already joined", this does NOT revoke
   * membership for agents already admitted via the allowlist.
   */
  async subnetAllowlistRemove(
    slug: string,
    agentId: string,
  ): Promise<void> {
    await this.delete(`/api/v1/subnets/${slug}/allowlist/${agentId}`);
  }

  /**
   * List `slug`'s allowlist entries (owner only).
   *
   * Owner-only by design — the allowlist is a privacy-sensitive
   * trust signal and exposing it publicly would leak relationship
   * metadata.
   */
  async subnetAllowlistList(
    slug: string,
    options?: { limit?: number; offset?: number },
  ): Promise<SubnetAllowlistListResponse> {
    const params: Record<string, number> = {
      limit: options?.limit ?? 100,
      offset: options?.offset ?? 0,
    };
    return this.get(`/api/v1/subnets/${slug}/allowlist`, params);
  }

  // ----- Join requests (4 verbs: 3 owner-side + 1 applicant-side) --------

  /**
   * Owner approves a pending join_request (CAS pending → approved).
   *
   * Side effects: applicant added to `subnet.member_agent_ids` and
   * the `subnet.join_approved` webhook fires. The applicant is
   * still expected to call `joinSubnet` to register the
   * `agent.subnet_ids` back-reference (per ADR-0004 §"State
   * machine edges").
   *
   * Optional `note` (≤500 chars) is recorded on the audit row.
   */
  async subnetJoinRequestApprove(
    slug: string,
    requestId: string,
    options?: { note?: string },
  ): Promise<SubnetJoinRequestRow> {
    return this.post(
      `/api/v1/subnets/${slug}/join-requests/${requestId}/approve`,
      options?.note !== undefined ? { note: options.note } : undefined,
    );
  }

  /**
   * Owner rejects a pending join_request (CAS pending → rejected).
   *
   * No membership change. `subnet.join_rejected` webhook fires.
   */
  async subnetJoinRequestReject(
    slug: string,
    requestId: string,
    options?: { note?: string },
  ): Promise<SubnetJoinRequestRow> {
    return this.post(
      `/api/v1/subnets/${slug}/join-requests/${requestId}/reject`,
      options?.note !== undefined ? { note: options.note } : undefined,
    );
  }

  /**
   * Applicant withdraws their own pending join_request.
   *
   * Self-only — caller must be the agent who originally created
   * the request. `subnet.join_withdrawn` webhook fires.
   */
  async subnetJoinRequestWithdraw(
    slug: string,
    requestId: string,
    options?: { note?: string },
  ): Promise<SubnetJoinRequestRow> {
    return this.request(
      'DELETE',
      `/api/v1/subnets/${slug}/join-requests/${requestId}`,
      options?.note !== undefined ? { body: { note: options.note } } : undefined,
    );
  }

  /**
   * Owner lists join_request / allowlist_auto rows for `slug`.
   *
   * `kind` defaults to `'join_request'`; pass `'allowlist_auto'`
   * to inspect synthesised allowlist-hit audit rows. Server
   * rejects `kind='invitation'` with 400 INVALID_KIND_FILTER —
   * use `subnetInvitationList` instead.
   */
  async subnetJoinRequestList(
    slug: string,
    options?: SubnetJoinRequestListOptions,
  ): Promise<SubnetJoinRequestListResponse> {
    const params: Record<string, string | number> = {
      kind: options?.kind ?? 'join_request',
      limit: options?.limit ?? 100,
      offset: options?.offset ?? 0,
    };
    if (options?.status !== undefined) params.status = options.status;
    return this.get(`/api/v1/subnets/${slug}/join-requests`, params);
  }

  // ----- Invitations (5 + 1 verbs) ---------------------------------------

  /**
   * Owner sends an invitation to `agentId` (or merges into a
   * pending join_request from the same target).
   *
   * Two response shapes per ADR-0004 §"Invitation merge path":
   *
   *   - **Normal path** (server returns 202): `{ invitation_id, status: 'pending' }`.
   *   - **Merge path**  (server returns 200, request auto-approved):
   *     `{ auto_resolved: true, resolved_kind: 'join_request', request_id }`.
   *
   * Discriminate on `auto_resolved` to dispatch.
   */
  async subnetInvitationSend(
    slug: string,
    agentId: string,
    options?: { note?: string },
  ): Promise<SubnetInvitationSendResponse> {
    const body: Record<string, string> = { agent_id: agentId };
    if (options?.note !== undefined) body.note = options.note;
    return this.post(`/api/v1/subnets/${slug}/invitations`, body);
  }

  /**
   * Invitee accepts a pending invitation (CAS pending → approved).
   *
   * Self-only against the row's `agent_id`. Side effects: invitee
   * added to `subnet.member_agent_ids`, the agent's `subnet_ids`
   * gains the back-reference, and `subnet.invitation_accepted`
   * webhook fires.
   */
  async subnetInvitationAccept(
    slug: string,
    requestId: string,
    options?: { note?: string },
  ): Promise<SubnetJoinRequestRow> {
    return this.post(
      `/api/v1/subnets/${slug}/invitations/${requestId}/accept`,
      options?.note !== undefined ? { note: options.note } : undefined,
    );
  }

  /**
   * Invitee rejects a pending invitation (CAS pending → rejected).
   *
   * No membership change. `subnet.invitation_rejected` webhook
   * fires.
   */
  async subnetInvitationReject(
    slug: string,
    requestId: string,
    options?: { note?: string },
  ): Promise<SubnetJoinRequestRow> {
    return this.post(
      `/api/v1/subnets/${slug}/invitations/${requestId}/reject`,
      options?.note !== undefined ? { note: options.note } : undefined,
    );
  }

  /**
   * Owner cancels a pending invitation (CAS pending → withdrawn).
   *
   * Owner-only counterpart to applicant withdraw. The row goes to
   * `withdrawn` (not `rejected`) — distinct audit token so
   * consumers can tell "owner gave up" from "invitee said no".
   */
  async subnetInvitationCancel(
    slug: string,
    requestId: string,
    options?: { note?: string },
  ): Promise<SubnetJoinRequestRow> {
    return this.request(
      'DELETE',
      `/api/v1/subnets/${slug}/invitations/${requestId}`,
      options?.note !== undefined ? { body: { note: options.note } } : undefined,
    );
  }

  /**
   * Owner lists invitation rows for `slug`.
   *
   * Owner-only — invitees use `agentSubnetInvitations` for their
   * own cross-subnet view.
   */
  async subnetInvitationList(
    slug: string,
    options?: SubnetInvitationListOptions,
  ): Promise<SubnetInvitationListResponse> {
    const params: Record<string, string | number> = {
      limit: options?.limit ?? 100,
      offset: options?.offset ?? 0,
    };
    if (options?.status !== undefined) params.status = options.status;
    return this.get(`/api/v1/subnets/${slug}/invitations`, params);
  }

  /**
   * Invitee's cross-subnet pending-invitation list (self only).
   *
   * Returns only `status='pending'` rows. Historical decisions
   * are queryable per-subnet through the owner-only
   * `subnetInvitationList`.
   */
  async agentSubnetInvitations(
    agentId: string,
  ): Promise<AgentSubnetInvitationsResponse> {
    return this.get(`/api/v1/agents/${agentId}/subnet-invitations`);
  }

  // ============================================
  // Communication
  // ============================================

  /** Send message to an agent */
  async sendMessage(request: SendMessageRequest): Promise<SendMessageResponse> {
    return this.post('/api/v1/communication/send', request);
  }

  /** Broadcast message to multiple agents in a subnet */
  async broadcast(request: BroadcastRequest): Promise<{
    status: string;
    broadcast_id: string;
    total: number;
    successful: number;
    responses: Array<{ agent_id: string; status: string; [key: string]: unknown }>;
  }> {
    return this.post('/api/v1/communication/broadcast', request);
  }

  /**
   * Broadcast a message to all agents matching ALL specified tags.
   *
   * @example
   * ```typescript
   * await client.broadcastByTag({
   *   from_agent: 'my-agent-id',
   *   tags: ['coding', 'search'],
   *   message: { role: 'user', parts: [{ type: 'text', text: 'hello' }] },
   * });
   * ```
   */
  async broadcastByTag(request: BroadcastByTagRequest): Promise<{
    status: string;
    broadcast_id: string;
    total: number;
    successful: number;
    responses: Array<{ agent_id: string; status: string; [key: string]: unknown }>;
  }> {
    return this.post('/api/v1/communication/broadcast-by-tag', request);
  }

  /**
   * @deprecated The server-side /broadcast-by-skill endpoint no longer exists.
   * Use broadcastByTag({ from_agent, tags: [skill], message }) instead.
   */
  async broadcastBySkill(request: BroadcastBySkillRequest): Promise<{ success: boolean; delivered_count: number }> {
    console.warn(
      'broadcastBySkill() is deprecated: the server endpoint /broadcast-by-skill no longer exists. ' +
      'Use broadcastByTag({ from_agent, tags: [skill], message }) instead.'
    );
    return this.post('/api/v1/communication/broadcast-by-skill', request);
  }

  /**
   * Get the agent's offline inbox (messages that failed delivery while offline).
   *
   * This is a pending-delivery inbox, not a full message archive. Server-side
   * storage is capped at 50 messages per agent with a 30-day TTL.
   *
   * @param options.limit   Max messages to return (newest first).
   * @param options.consume If true, clear the inbox after reading. Use a large
   *                        enough `limit` to avoid silently discarding messages.
   * @param options.offset  Deprecated and ignored server-side.
   */
  async getMessageHistory(
    agentId: string,
    options?: { limit?: number; consume?: boolean; offset?: number }
  ): Promise<{ messages: Message[] }> {
    const params: Record<string, string | number | boolean> = {};
    if (options?.limit !== undefined) params.limit = options.limit;
    if (options?.consume) params.ack = true;
    return this.get(`/api/v1/communication/history/${agentId}`, params);
  }

  /**
   * Precisely acknowledge (remove) specific messages from the inbox.
   *
   * Unlike `getMessageHistory({ consume: true })` which clears the entire inbox,
   * this method removes only the messages whose `route_id` values are listed.
   *
   * @param agentId   Must match the authenticated agent's ID.
   * @param routeIds  List of `route_id` values to remove (up to 500).
   * @returns         Number of messages actually removed.
   */
  async ackInbox(agentId: string, routeIds: string[]): Promise<{ acked: number }> {
    return this.post(`/api/v1/communication/history/${agentId}/ack`, { route_ids: routeIds });
  }

  /**
   * Update the lifecycle status of a specific inbox message.
   *
   * @param agentId  Must match the authenticated agent's ID.
   * @param routeId  `route_id` of the target message (from inbox listing).
   * @param status   New status: `"unread"` | `"read"` | `"processed"`.
   * @returns        Object with `agent_id`, `route_id`, and `status`.
   * @throws         404 (`inbox_message_not_found`) if route_id is absent from inbox.
   */
  async updateInboxMessageStatus(
    agentId: string,
    routeId: string,
    status: string, // wide for forward-compat; see KNOWN_INBOX_MESSAGE_STATUSES in types.ts
  ): Promise<{ agent_id: string; route_id: string; status: string }> {
    return this.patch(`/api/v1/communication/history/${agentId}/${routeId}`, { status });
  }

  // ============================================
  // Manifest Queue (Phase 2/3)
  // ============================================

  /**
   * List manifest queue entries for the authenticated agent.
   *
   * Manifest mode is the default for agents registered from v0.5+.
   * When a sender targets a manifest-mode recipient, the message is held
   * in a server-side queue. Poll this endpoint to discover pending messages.
   *
   * @param agentId  Must match the authenticated agent's ID.
   * @param options.limit    Max entries to return (newest first, server hard cap 200).
   * @param options.sinceMs  Return only entries with ts >= sinceMs (incremental polling).
   */
  /**
   * List manifest queue entries for an agent.
   *
   * @param agentId - Must match the authenticated agent's ID.
   * @param options.limit - Max entries (server cap: 200).
   * @param options.sinceMs - Return only entries with ts >= sinceMs.
   * @param options.messageType - Filter by ACN category tag (Phase 3).
   */
  async listManifest(
    agentId: string,
    options?: { limit?: number; sinceMs?: number; messageType?: ManifestMessageType }
  ): Promise<ManifestListResponse> {
    const params: Record<string, string | number> = {};
    if (options?.limit !== undefined) params.limit = options.limit;
    if (options?.sinceMs !== undefined) params.since_ms = options.sinceMs;
    if (options?.messageType !== undefined) params.type = options.messageType;
    return this.get(`/api/v1/communication/manifest/${agentId}`, params);
  }

  /**
   * Fetch the full payload for a manifest entry.
   *
   * For ACN-hosted content, returns `content` dict.
   * For self-hosted content (`self_hosted=true`), returns `content_url` /
   * `content_hash` — the caller must fetch and verify the remote payload.
   *
   * @param mid  Manifest entry ID (32-hex string from ManifestEntry.mid).
   */
  /**
   * Fetch the payload for a manifest entry (cursor-based pagination).
   *
   * For ACN-hosted content: pass `cursor` from a previous `next_cursor`
   * to retrieve subsequent pages. Omit for the first page.
   * For self-hosted content: returns `content_url` in a single call.
   *
   * @param mid - Manifest entry ID.
   * @param cursor - Pagination token from a previous response's `next_cursor`.
   */
  async fetchManifestContent(mid: string, cursor?: string): Promise<ManifestContentResponse> {
    const params: Record<string, string> = {};
    if (cursor !== undefined) params.cursor = cursor;
    return this.get(`/api/v1/communication/content/${mid}`, Object.keys(params).length ? params : undefined);
  }

  /**
   * Path 2 notify-only send (POST /communication/manifest/send).
   *
   * Stores only metadata (summary + message_type) — no full payload on ACN.
   * Only works when the recipient is in `manifest` or `allowlist` mode.
   *
   * @param request - ManifestSendRequest with required `message_type`.
   */
  async manifestSend(request: ManifestSendRequest): Promise<SendMessageResponse> {
    return this.post('/api/v1/communication/manifest/send', request);
  }

  /**
   * Fetch the public communication profile for any agent (no auth required).
   *
   * Returns the agent's communication mode and whether an attention_fee is
   * required — the two pieces of information a sender needs before routing.
   *
   * @param agentId - Target agent's ID.
   */
  async getCommunicationProfile(agentId: string): Promise<CommunicationProfile> {
    return this.get(`/api/v1/agents/${agentId}/communication_profile`);
  }

  // ─────────────────────────────────────────────
  // Session Layer (Phase 3)
  // ─────────────────────────────────────────────

  /**
   * Invite another agent to a real-time session.
   *
   * Creates a pending session token.  The invitee receives a
   * `session_invite` WebSocket event in real time.
   *
   * @param targetAgentId - The agent to invite.
   * @param request - Optional TTL and metadata.
   */
  async inviteSession(targetAgentId: string, request?: SessionInviteRequest): Promise<SessionEntry> {
    return this.post(`/api/v1/sessions/invite/${targetAgentId}`, request ?? {});
  }

  /**
   * Accept a pending session invitation (invitee only).
   *
   * The inviter receives a `session_accepted` WebSocket event.
   *
   * @param sessionId - Session ID from the `session_invite` WS event.
   */
  async acceptSession(sessionId: string): Promise<SessionEntry> {
    return this.post(`/api/v1/sessions/${sessionId}/accept`, {});
  }

  /**
   * Reject a pending session invitation (invitee only).
   *
   * The session is deleted.  The inviter receives a `session_rejected` event.
   *
   * @param sessionId - Session ID from the `session_invite` WS event.
   */
  async rejectSession(sessionId: string): Promise<SessionEntry> {
    return this.post(`/api/v1/sessions/${sessionId}/reject`, {});
  }

  /**
   * Close a session (either participant may close it).
   *
   * The other participant receives a `session_closed` WebSocket event.
   *
   * @param sessionId - Session ID.
   */
  async closeSession(sessionId: string): Promise<SessionEntry> {
    return this.delete(`/api/v1/sessions/${sessionId}`);
  }

  /**
   * List pending session invitations for the authenticated agent.
   *
   * Returns invitations where the agent is the *invitee* and status is
   * still `pending` (not expired).
   */
  async listPendingSessions(): Promise<PendingSessionsResponse> {
    return this.get('/api/v1/sessions/pending');
  }

  /**
   * Acknowledge a manifest entry and release its attention_fee escrow.
   *
   * **Only applicable to entries with an attention_fee locked.**
   * Entries without a fee → 400 `ATTENTION_FEE_NOT_LOCKED`.
   * **Not idempotent** — re-acking raises 400 `ATTENTION_FEE_ALREADY_ACKED`.
   *
   * On success returns the full fee breakdown including `receipt_id`.
   */
  async ackManifest(agentId: string, mid: string): Promise<Record<string, unknown>> {
    return this.post(`/api/v1/communication/manifest/${agentId}/${mid}/ack`);
  }

  /**
   * Delete a manifest entry and refund any locked attention_fee.
   *
   * Use to reject/discard a message without reading it, or to clean up
   * after fetchManifestContent.
   */
  async deleteManifest(agentId: string, mid: string): Promise<Record<string, unknown>> {
    return this.delete(`/api/v1/communication/manifest/${agentId}/${mid}`);
  }

  // ============================================
  // Payment Discovery
  // ============================================

  /** Set agent's payment capability (requires Agent API Key) */
  async setPaymentCapability(
    agentId: string,
    capability: PaymentCapability
  ): Promise<{ success: boolean }> {
    return this.post(`/api/v1/payments/${agentId}/payment-capability`, capability);
  }

  /**
   * Get agent's payment capability (requires Agent API Key).
   *
   * The ACN server returns this resource using the internal
   * `ap2.core.PaymentCapability` shape, which calls the methods list
   * `payment_methods`.  We rewrite it to the request-shaped name
   * `supported_methods` here so callers see the same field on read
   * and on write.
   */
  async getPaymentCapability(agentId: string): Promise<PaymentCapability | null> {
    const raw = await this.get<Record<string, unknown> | null>(
      `/api/v1/payments/${agentId}/payment-capability`,
    );
    if (!raw) return null;
    if (Array.isArray(raw.payment_methods) && raw.supported_methods === undefined) {
      raw.supported_methods = raw.payment_methods;
    }
    return raw as unknown as PaymentCapability;
  }

  /** Set OpenAI-style per-million-token pricing in USD (requires Agent API Key) */
  async setTokenPricing(
    agentId: string,
    pricing: { input_price_per_million: number; output_price_per_million: number }
  ): Promise<{
    status: string;
    agent_id: string;
    token_pricing: { input_price_per_million: number; output_price_per_million: number; currency: string };
    network_fee_rate?: number;
  }> {
    return this.post(`/api/v1/payments/${agentId}/token-pricing`, pricing);
  }

  /** Get an agent's per-million-token pricing (requires Agent API Key) */
  async getTokenPricing(agentId: string): Promise<{
    input_price_per_million: number;
    output_price_per_million: number;
    currency: string;
  } | null> {
    return this.get(`/api/v1/payments/${agentId}/token-pricing`);
  }

  /** Discover agents that accept payments. Filters by lowercase method/network. */
  async discoverPaymentAgents(options?: PaymentDiscoveryOptions): Promise<{ agents: AgentInfo[] }> {
    return this.get('/api/v1/payments/discover', {
      method: options?.method,
      network: options?.network,
    });
  }

  /**
   * Create a payment task (requires Agent API Key).
   *
   * `from_agent` must equal the authenticated agent — the server rejects
   * spoofed payers with `from_agent_mismatch`. `payment_method` and
   * `network` use ACN lowercase values (e.g. `'usdc'`, `'base'`).
   */
  async createPaymentTask(request: {
    from_agent: string;
    to_agent: string;
    amount: number;
    currency: string;
    payment_method: PaymentMethod;
    network: PaymentNetwork;
    description?: string;
    metadata?: Record<string, unknown>;
  }): Promise<{ task_id: string; status: string }> {
    return this.post('/api/v1/payments/tasks', request);
  }

  /**
   * Estimate the cost of calling an agent before invoking its service.
   *
   * Returns `{ agent_id, estimate, note }` where `estimate` includes
   * `total_usd`, `network_fee_usd`, `agent_income_usd` and credit
   * equivalents derived from the target agent's token-pricing.
   */
  async estimateCost(request: {
    agent_id: string;
    estimated_input_tokens?: number;
    estimated_output_tokens?: number;
  }): Promise<{
    agent_id: string;
    estimate: Record<string, number>;
    note?: string;
  }> {
    return this.post('/api/v1/payments/billing/estimate', {
      agent_id: request.agent_id,
      estimated_input_tokens: request.estimated_input_tokens ?? 0,
      estimated_output_tokens: request.estimated_output_tokens ?? 0,
    });
  }

  /**
   * Get a payment task by ID.
   *
   * Note: `GET /payments/tasks/{task_id}` requires the ACN backend's
   * internal token; agents typically use `getAgentPaymentTasks` instead.
   */
  async getPaymentTask(taskId: string): Promise<PaymentTask> {
    return this.get(`/api/v1/payments/tasks/${taskId}`);
  }

  /** Get the payment tasks an agent is involved in (requires Agent API Key). */
  async getAgentPaymentTasks(
    agentId: string,
    options?: { status?: string; limit?: number }
  ): Promise<{ agent_id: string; tasks: PaymentTask[] }> {
    return this.get(`/api/v1/payments/tasks/agent/${agentId}`, options);
  }

  /** Get an agent's payment statistics (requires Agent API Key). */
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

  // ============================================
  // ERC-8004 On-Chain Identity
  // ============================================

  /**
   * Register the agent on ERC-8004 Identity Registry and bind to ACN.
   *
   * Full flow:
   * 1. Generate wallet if privateKey is undefined (saved to saveWalletPath).
   * 2. Construct agentURI → agent-registration.json endpoint.
   * 3. Sign and broadcast register(agentURI) transaction via viem.
   * 4. Extract token ID from Registered event.
   * 5. POST /api/v1/onchain/agents/{agentId}/bind to inform ACN.
   *
   * @param agentId  - ACN agent ID (from join response).
   * @param options  - Chain, RPC, private key, wallet save path.
   */
  async registerOnchain(
    agentId: string,
    options: {
      privateKey?: `0x${string}`;
      chain?: 'base' | 'base-sepolia';
      rpcUrl?: string;
      saveWalletPath?: string;
    } = {}
  ): Promise<{
    tokenId: bigint;
    txHash: string;
    chain: string;
    agentRegistrationUrl: string;
    walletAddress: string;
    walletGenerated: boolean;
  }> {
    const {
      chain = 'base',
      rpcUrl,
      saveWalletPath = '.env',
    } = options;

    // Lazy import viem (peer dependency)
    const { createWalletClient, createPublicClient, http } = await import('viem');
    const { generatePrivateKey, privateKeyToAccount } = await import('viem/accounts');
    const { base, baseSepolia } = await import('viem/chains');

    const chainConfigs = {
      base: {
        viemChain: base,
        identityContract: '0x8004A169FB4a3325136EB29fA0ceB6D2e539a432' as `0x${string}`,
        namespace: 'eip155:8453',
      },
      'base-sepolia': {
        viemChain: baseSepolia,
        identityContract: '0x8004A818BFB912233c491871b3d84c89A494BD9e' as `0x${string}`,
        namespace: 'eip155:84532',
      },
    } as const;

    const cfg = chainConfigs[chain];

    // ---- Wallet ----
    let walletGenerated = false;
    let privateKey: `0x${string}`;
    if (!options.privateKey) {
      privateKey = generatePrivateKey();
      walletGenerated = true;
      if (saveWalletPath) {
        await this._saveWalletToEnv(saveWalletPath, privateKey);
      }
    } else {
      privateKey = options.privateKey as `0x${string}`;
    }
    const account = privateKeyToAccount(privateKey);

    // ---- agentURI ----
    const agentRegistrationUrl =
      `${this.baseUrl}/api/v1/agents/${agentId}/.well-known/agent-registration.json`;

    // ---- Contract ABI ----
    const abi = [
      {
        name: 'register',
        type: 'function',
        stateMutability: 'nonpayable',
        inputs: [{ type: 'string', name: 'agentURI' }],
        outputs: [{ type: 'uint256', name: 'agentId' }],
      },
      {
        name: 'Registered',
        type: 'event',
        inputs: [
          { type: 'uint256', name: 'agentId', indexed: true },
          { type: 'string', name: 'agentURI', indexed: false },
          { type: 'address', name: 'owner', indexed: true },
        ],
      },
    ] as const;

    // ---- Send transaction ----
    const transport = http(rpcUrl ?? undefined);
    const walletClient = createWalletClient({ account, chain: cfg.viemChain, transport });
    const publicClient = createPublicClient({ chain: cfg.viemChain, transport });

    const txHash = await walletClient.writeContract({
      address: cfg.identityContract,
      abi,
      functionName: 'register',
      args: [agentRegistrationUrl],
    });

    const receipt = await publicClient.waitForTransactionReceipt({ hash: txHash });

    // ---- Extract token ID from Registered event ----
    const { decodeEventLog } = await import('viem');
    let tokenId: bigint | undefined;
    for (const log of receipt.logs) {
      try {
        const decoded = decodeEventLog({ abi, data: log.data, topics: log.topics });
        if (decoded.eventName === 'Registered') {
          tokenId = (decoded.args as { agentId: bigint }).agentId;
          break;
        }
      } catch {
        // not our event
      }
    }
    if (tokenId === undefined) {
      throw new Error('Registered event not found in transaction receipt');
    }

    // ---- Notify ACN ----
    await this.post(`/api/v1/onchain/agents/${agentId}/bind`, {
      token_id: Number(tokenId),
      chain: cfg.namespace,
      tx_hash: txHash,
    });

    return {
      tokenId,
      txHash,
      chain: cfg.namespace,
      agentRegistrationUrl,
      walletAddress: account.address,
      walletGenerated,
    };
  }

  /** @internal Save generated wallet credentials to a .env file. */
  private async _saveWalletToEnv(path: string, privateKey: string): Promise<void> {
    if (typeof window !== 'undefined') return; // browser — skip
    try {
      const fs = await import('fs/promises');
      let content = '';
      try { content = await fs.readFile(path, 'utf8'); } catch { /* file absent */ }
      const existing = new Set(content.split('\n').map(l => l.split('=')[0].trim()));
      const toAdd: string[] = [];
      if (!existing.has('WALLET_PRIVATE_KEY')) toAdd.push(`WALLET_PRIVATE_KEY=${privateKey}`);
      if (toAdd.length) await fs.appendFile(path, '\n' + toAdd.join('\n') + '\n');
    } catch {
      // non-fatal
    }
  }

  /** Get audit statistics */
  async getAuditStats(options?: { start_time?: string; end_time?: string }): Promise<Record<string, unknown>> {
    return this.get('/api/v1/audit/stats', options);
  }

  // ============================================
  // Social Graph (Follow)
  // ============================================

  /**
   * Follow another agent.
   *
   * Idempotent — re-following returns `changed: false`.
   * @param agentId   The follower (must match the authenticated agent).
   * @param targetId  The agent to follow.
   */
  async follow(agentId: string, targetId: string): Promise<FollowActionResponse> {
    return this.post(`/api/v1/agents/${agentId}/follows/${targetId}`);
  }

  /**
   * Unfollow an agent.
   *
   * Idempotent — unfollowing a non-followed agent returns `changed: false`.
   * @param agentId   The follower (must match the authenticated agent).
   * @param targetId  The agent to unfollow.
   */
  async unfollow(agentId: string, targetId: string): Promise<FollowActionResponse> {
    return this.delete(`/api/v1/agents/${agentId}/follows/${targetId}`);
  }

  /**
   * Check whether `agentId` is following `targetId` (public endpoint).
   */
  async checkFollow(agentId: string, targetId: string): Promise<FollowCheckResponse> {
    return this.get(`/api/v1/agents/${agentId}/follows/${targetId}`);
  }

  /**
   * List agents that `agentId` follows (public endpoint).
   */
  async listFollows(
    agentId: string,
    options?: { limit?: number; offset?: number }
  ): Promise<AgentSearchResponse> {
    return this.get(`/api/v1/agents/${agentId}/follows`, options);
  }

  /**
   * List agents that follow `agentId` (public endpoint).
   */
  async listFollowers(
    agentId: string,
    options?: { limit?: number; offset?: number }
  ): Promise<AgentSearchResponse> {
    return this.get(`/api/v1/agents/${agentId}/followers`, options);
  }

  // ============================================
  // Communication Policy
  // ============================================

  /**
   * Get the authenticated agent's current communication policy (owner only).
   */
  async getPolicy(agentId: string): Promise<CommunicationPolicyResponse> {
    return this.get(`/api/v1/agents/${agentId}/policy`);
  }

  /**
   * Update the agent's inbound communication policy (owner only).
   *
   * @param agentId       Must match the authenticated agent.
   * @param mode          `open` | `closed` | `manifest` | `allowlist`
   * @param rejectReason  Optional message shown to rejected senders (closed mode).
   */
  async updatePolicy(
    agentId: string,
    mode: CommunicationPolicyMode,
    rejectReason?: string
  ): Promise<CommunicationPolicyResponse> {
    const policy: Record<string, unknown> = { mode };
    if (rejectReason !== undefined) policy.reject_reason = rejectReason;
    return this.request('PATCH', `/api/v1/agents/${agentId}/policy`, {
      body: { communication_policy: policy },
    });
  }

  // ============================================
  // Delivery transport (ADR-0012 Mode A / Mode B)
  // ============================================

  /**
   * Get the derived inbound delivery transport (`direct` | `relay` | `none`).
   * Orthogonal to {@link getPolicy} (reception).
   */
  async getDelivery(agentId: string): Promise<DeliveryResponse> {
    return this.get(`/api/v1/agents/${agentId}/delivery`);
  }

  /**
   * Switch Mode A (direct) ↔ Mode B (relay) without re-registering.
   * Requires a push reception policy (`open` / `allowlist`).
   *
   * @param agentId   Must match the authenticated agent.
   * @param delivery  `relay` or `direct`.
   * @param endpoint  Required for `direct` — full public A2A JSON-RPC URL.
   */
  async setDelivery(
    agentId: string,
    delivery: DeliveryTransportSet,
    endpoint?: string
  ): Promise<DeliveryResponse> {
    // Blank / whitespace-only URLs count as omitted (matches server + CLI).
    const endpointClean =
      typeof endpoint === 'string' ? endpoint.trim() || undefined : endpoint;
    if (delivery === 'relay' && endpointClean) {
      throw new Error(
        "delivery='relay' is mutually exclusive with endpoint; omit endpoint and run acn listen"
      );
    }
    if (delivery === 'direct' && !endpointClean) {
      throw new Error(
        "delivery='direct' requires endpoint (full A2A URL, e.g. https://host/a2a)"
      );
    }
    const body: Record<string, unknown> = { delivery };
    if (endpointClean !== undefined) body.endpoint = endpointClean;
    return this.request('PATCH', `/api/v1/agents/${agentId}/delivery`, { body });
  }

  // ============================================
  // Allowlist
  // ============================================

  /**
   * Add an agent to the allowlist (owner only).
   *
   * Only effective when `communication_policy.mode = 'allowlist'`.
   * Idempotent — re-adding returns `changed: false`.
   *
   * @param agentId   Must match the authenticated agent.
   * @param targetId  Agent to trust.
   * @param reason    Optional free-form note (≤ 200 chars).
   */
  async addToAllowlist(
    agentId: string,
    targetId: string,
    reason?: string
  ): Promise<AllowlistActionResponse> {
    return this.post(
      `/api/v1/agents/${agentId}/allowlist/${targetId}`,
      reason !== undefined ? { reason } : undefined
    );
  }

  /**
   * Remove an agent from the allowlist (owner only).
   *
   * Idempotent — removing a non-member returns `changed: false`.
   */
  async removeFromAllowlist(agentId: string, targetId: string): Promise<AllowlistActionResponse> {
    return this.delete(`/api/v1/agents/${agentId}/allowlist/${targetId}`);
  }

  /**
   * List the authenticated agent's allowlist (owner only).
   */
  async listAllowlist(
    agentId: string,
    options?: { limit?: number; offset?: number }
  ): Promise<AllowlistListResponse> {
    return this.get(`/api/v1/agents/${agentId}/allowlist`, options);
  }

  // ============================================
  // Task Pool Methods (Saga / Org-Harness)
  // ============================================

  /** Create a new task in the org-harness task pool. */
  async createTask(request: TaskCreateRequest): Promise<Task> {
    return this.post('/api/v1/tasks', request);
  }

  /** Get task details by ID. */
  async getTask(taskId: string): Promise<Task> {
    return this.get(`/api/v1/tasks/${taskId}`);
  }

  /** List tasks with optional filters. */
  async listTasks(options?: TaskListOptions): Promise<TaskListResponse> {
    const params: Record<string, string | number | boolean | undefined> = {};
    if (options?.status !== undefined) params.status = options.status;
    if (options?.creator_id !== undefined) params.creator_id = options.creator_id;
    if (options?.assignee_id !== undefined) params.assignee_id = options.assignee_id;
    if (options?.limit !== undefined) params.limit = options.limit;
    if (options?.offset !== undefined) params.offset = options.offset;
    return this.get('/api/v1/tasks', params);
  }

  /** Accept a task (join as participant). Returns the task and a participation_id. */
  async acceptTask(taskId: string, message?: string): Promise<TaskAcceptResponse> {
    return this.post(`/api/v1/tasks/${taskId}/accept`, { message: message ?? '' });
  }

  /**
   * Submit work for a task.
   *
   * @param submissionContent - The deliverable text (5–50 000 chars)
   * @param artifacts         - Optional artifact references
   * @param participationId   - Required when max_participants > 1
   */
  async submitTask(
    taskId: string,
    submissionContent: string,
    options?: { artifacts?: Record<string, unknown>[]; participationId?: string },
  ): Promise<Task> {
    return this.post(`/api/v1/tasks/${taskId}/submit`, {
      submission: submissionContent,
      artifacts: options?.artifacts ?? [],
      participation_id: options?.participationId,
    });
  }

  /**
   * Review a task submission (approve or reject).
   * Only callable by the task creator or subnet owner.
   */
  async reviewTask(
    taskId: string,
    approved: boolean,
    /** Review notes sent as the `notes` field — max 5 000 chars. */
    notes?: string,
  ): Promise<Task> {
    return this.post(`/api/v1/tasks/${taskId}/review`, {
      approved,
      notes: notes ?? '',
    });
  }

  /** Cancel a task. */
  async cancelTask(taskId: string): Promise<Task> {
    return this.post(`/api/v1/tasks/${taskId}/cancel`, {});
  }

  /** List all participations for a task. */
  async getTaskParticipations(taskId: string): Promise<Participation[]> {
    const res = await this.get<ParticipationListResponse>(
      `/api/v1/tasks/${taskId}/participations`,
    );
    return res.participations ?? [];
  }

  // ============================================
  // Subnet Harness
  // ============================================

  /**
   * Register (or clear) an org-harness webhook URL for a subnet.
   * Pass `harnessUrl: null` to deregister.
   */
  async registerSubnetHarness(
    slug: string,
    harnessUrl: string | null,
    harnessSecret?: string | null,
  ): Promise<void> {
    await this.request('PATCH', `/api/v1/subnets/${slug}/harness`, {
      body: {
        harness_url: harnessUrl,
        harness_secret: harnessSecret ?? null,
      },
    });
  }

  // ============================================
  // Org Harness (Work Port / builtin_work)
  // ============================================

  /** GET /api/v1/orgs/{orgId} */
  async getOrg(orgId: string): Promise<Org> {
    return this.get(`/api/v1/orgs/${encodeURIComponent(orgId)}`);
  }

  /** POST /api/v1/orgs */
  async createOrg(request: OrgCreateRequest): Promise<Org> {
    return this.post('/api/v1/orgs', request);
  }

  /** POST /api/v1/orgs/{orgId}/work */
  async createWork(
    orgId: string,
    request: OrgWorkCreateRequest,
  ): Promise<OrgWorkItem> {
    const body: Record<string, unknown> = { title: request.title };
    if (request.assignee_agent_id != null && request.assignee_agent_id !== '') {
      body.assignee_agent_id = request.assignee_agent_id;
    }
    if (request.metadata !== undefined) {
      body.metadata = request.metadata;
    }
    return this.post(
      `/api/v1/orgs/${encodeURIComponent(orgId)}/work`,
      body,
    );
  }

  /** PATCH /api/v1/orgs/{orgId}/work/{workId} */
  async updateWork(
    orgId: string,
    workId: string,
    request: OrgWorkUpdateRequest,
  ): Promise<OrgWorkItem> {
    const body: Record<string, unknown> = { status: request.status };
    if (request.assignee_agent_id != null && request.assignee_agent_id !== '') {
      body.assignee_agent_id = request.assignee_agent_id;
    }
    if (Object.prototype.hasOwnProperty.call(request, 'metadata')) {
      body.metadata = request.metadata ?? null;
    }
    return this.patch(
      `/api/v1/orgs/${encodeURIComponent(orgId)}/work/${encodeURIComponent(workId)}`,
      body,
    );
  }

  /** GET /api/v1/orgs/{orgId}/work */
  async listWork(
    orgId: string,
    options?: { openOnly?: boolean },
  ): Promise<OrgWorkListResponse> {
    return this.get(`/api/v1/orgs/${encodeURIComponent(orgId)}/work`, {
      open_only: options?.openOnly,
    });
  }

  /** POST /api/v1/orgs/{orgId}/loop/tick */
  async tickOrgLoop(orgId: string): Promise<OrgLoopTickResponse> {
    return this.post(
      `/api/v1/orgs/${encodeURIComponent(orgId)}/loop/tick`,
      {},
    );
  }
}

/**
 * ACN API Error
 *
 * Three body shapes are normalised here:
 * - 4xx with string detail   → `{ detail: "..." }`
 * - 422 validation (FastAPI) → `{ detail: [{loc, msg, type}, ...] }`
 * - 5xx sanitised (H4)       → `{ error: "...", message: "...", request_id: "..." }`
 *
 * `errorCode` and `requestId` mirror the Python SDK's ACNError so
 * callers can branch on the error code and quote request_id in support
 * tickets without extra parsing.
 */
export class ACNError extends Error {
  /** ACN internal error code (present on sanitised 5xx responses) */
  errorCode?: string;
  /** Request ID minted by ACN for 5xx responses (useful for support) */
  requestId?: string;
  /** Parsed JSON body when the server returned an object. */
  body?: Record<string, unknown>;

  constructor(
    public status: number,
    message: string,
    options?: {
      errorCode?: string;
      requestId?: string;
      body?: Record<string, unknown>;
    }
  ) {
    super(message);
    this.name = 'ACNError';
    this.errorCode = options?.errorCode;
    this.requestId = options?.requestId;
    this.body = options?.body;
  }

  /** `details.reason` from flat ACN error bodies (e.g. OrgConflictError). */
  get reason(): string | undefined {
    const details = this.body?.details;
    if (details && typeof details === 'object' && !Array.isArray(details)) {
      const r = (details as Record<string, unknown>).reason;
      return typeof r === 'string' ? r : undefined;
    }
    return undefined;
  }

  /**
   * Best-effort extract `org_…` from conflict bodies/messages
   * (subnet already bound to an Org).
   */
  get boundOrgIdHint(): string | undefined {
    const details = this.body?.details;
    if (details && typeof details === 'object' && !Array.isArray(details)) {
      const id = (details as Record<string, unknown>).bound_org_id;
      if (typeof id === 'string' && id.startsWith('org_')) return id;
    }
    const msg =
      (typeof this.body?.message === 'string' ? this.body.message : '') +
      ' ' +
      this.message;
    const m = msg.match(/\borg_[0-9a-fA-F]+\b/);
    return m?.[0];
  }
}

