-- Backfill visibility for CI/Saga/Demo fixtures left over in production.
--
-- Context
-- -------
-- ACN registry exposes ``GET /api/v1/agents?visibility=real``. The route
-- treats an agent with no ``metadata.visibility`` as ``"real"`` (open-world
-- assumption for legacy registrations — see ``routes/registry.py``).
--
-- CI/demo fixtures registered via ``smoke_backend_integration.py``, saga
-- integration tests, chaos/dlq/t7v test suites, and old demo runs were
-- created **without** setting ``metadata.visibility``, so they leak into
-- the public ``visibility=real`` list and show up on agentplanet.org/world.
--
-- Storage path
-- ------------
-- ``Agent.metadata`` in the domain entity corresponds to the JSONB sub-key
-- ``metadata['extra_metadata']`` in the database (see
-- ``infrastructure/persistence/postgres/agent_repository.py:64``). All reads
-- and writes must go through that sub-path. The older scripts
-- ``backfill_visibility.sql`` / ``backfill_visibility_clean.sql`` /
-- ``backfill_remaining.sql`` wrote to the wrong top-level path and therefore
-- never took effect; ``rename_to_hidden.sql`` is the only script with the
-- correct path.
--
-- Idempotency
-- -----------
-- Each UPDATE is gated by ``... IS NULL`` so re-runs are no-ops.
--
-- How to run
-- ----------
--   railway connect Postgres --command "$(cat scripts/backfill_visibility_fixtures.sql)"
-- or pipe through ``psql $DATABASE_URL -f scripts/backfill_visibility_fixtures.sql``.

-- ── 0. Pre-flight: how many rows are currently unlabelled? ──────────────────
SELECT
  COUNT(*) FILTER (WHERE metadata->'extra_metadata'->>'visibility' IS NULL) AS null_visibility,
  COUNT(*) FILTER (WHERE metadata->'extra_metadata'->>'visibility' = 'real') AS real_count,
  COUNT(*) FILTER (WHERE metadata->'extra_metadata'->>'visibility' = 'hidden') AS hidden_count,
  COUNT(*) AS total
FROM agents;

-- ── 1. Tag known CI / saga / chaos / dlq / t7v fixture prefixes ─────────────
-- These all originate from automated test suites that register agents but
-- never tear them down. ``visibility=test`` keeps them out of the public
-- ``visibility=real`` filter while preserving the rows for audit.
UPDATE agents
SET metadata = jsonb_set(
  COALESCE(metadata, '{}'::jsonb),
  '{extra_metadata}',
  COALESCE(metadata->'extra_metadata', '{}'::jsonb) || '{"visibility":"test"}'::jsonb
)
WHERE name ~ '^(smoke-|saga[0-9]?-|chaos-|dlq-|t7v-)'
  AND metadata->'extra_metadata'->>'visibility' IS NULL;

-- ── 2. Tag long-lived demo fixtures ─────────────────────────────────────────
-- ``DemoCoordinator`` and ``DemoWorker`` are duplicated many times across
-- old demo runs (10+ copies each). They were never claimed and never had a
-- real endpoint — they are fixtures, not real agents.
UPDATE agents
SET metadata = jsonb_set(
  COALESCE(metadata, '{}'::jsonb),
  '{extra_metadata}',
  COALESCE(metadata->'extra_metadata', '{}'::jsonb) || '{"visibility":"test"}'::jsonb
)
WHERE name IN ('DemoCoordinator', 'DemoWorker')
  AND metadata->'extra_metadata'->>'visibility' IS NULL;

-- ── 3. Tag manual one-off test agents ───────────────────────────────────────
UPDATE agents
SET metadata = jsonb_set(
  COALESCE(metadata, '{}'::jsonb),
  '{extra_metadata}',
  COALESCE(metadata->'extra_metadata', '{}'::jsonb) || '{"visibility":"test"}'::jsonb
)
WHERE name ~ '^TestAgent'
  AND metadata->'extra_metadata'->>'visibility' IS NULL;

-- ── 4. Verify final distribution ────────────────────────────────────────────
SELECT
  COALESCE(metadata->'extra_metadata'->>'visibility', '(none)') AS visibility,
  COUNT(*) AS cnt
FROM agents
GROUP BY visibility
ORDER BY cnt DESC;

-- ── 5. Spot-check: any remaining NULL rows (these will still show as real) ──
SELECT agent_id, name, registered_at
FROM agents
WHERE metadata->'extra_metadata'->>'visibility' IS NULL
ORDER BY registered_at DESC NULLS LAST
LIMIT 20;
