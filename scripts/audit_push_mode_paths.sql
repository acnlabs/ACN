-- Audit: push-mode agents whose registered A2A endpoint is a bare origin
-- (no path or just "/"), which silently misroutes ACN direct-push delivery.
--
-- Background: ACN posts the A2A JSON-RPC message to the VERBATIM registered
-- endpoint (acn/infrastructure/messaging/message_router.py builds the A2A
-- client card with url=endpoint and never appends a convention path). An
-- agent whose A2A server is mounted at e.g. /a2a but who registered the bare
-- origin https://host will receive POST / instead — and registration only
-- does a HEAD reachability probe (any HTTP response counts), so the wrong
-- path is NOT caught at join time. This is the agentmother failure class.
--
-- The DB has no separate a2a_endpoint column: the API field is an alias of
-- the single `endpoint` column (see AgentModel in
-- acn/infrastructure/persistence/postgres/models.py). NULL communication_policy
-- is treated as the implicit {"mode": "open"} (push) default by the domain
-- layer, so those agents are in-scope too.
--
-- Read-only. Run against production, e.g.:
--   railway run psql "$DATABASE_URL" -f scripts/audit_push_mode_paths.sql
SELECT
  agent_id,
  name,
  COALESCE(communication_policy->>'mode', 'open')                 AS mode,
  endpoint,
  regexp_replace(endpoint, '^https?://[^/]+', '')                 AS path_tail,
  (regexp_replace(endpoint, '^https?://[^/]+', '') IN ('', '/'))  AS bare_origin
FROM agents
WHERE endpoint IS NOT NULL
  AND endpoint <> ''
ORDER BY
  -- surface the at-risk push-mode bare-origin agents first
  (regexp_replace(endpoint, '^https?://[^/]+', '') IN ('', '/')) DESC,
  (COALESCE(communication_policy->>'mode', 'open') IN ('open', 'allowlist')) DESC,
  name;
