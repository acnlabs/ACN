-- Move visibility from top-level metadata to extra_metadata sub-key
-- (entity.metadata maps to metadata->'extra_metadata' in the repository)
UPDATE agents
SET metadata =
  jsonb_set(
    metadata - 'visibility',
    '{extra_metadata}',
    COALESCE(metadata->'extra_metadata', '{}') ||
    jsonb_build_object('visibility', metadata->>'visibility')
  )
WHERE metadata ? 'visibility';

-- Verify
SELECT
  metadata->'extra_metadata'->>'visibility' AS em_visibility,
  COUNT(*) AS cnt
FROM agents
GROUP BY metadata->'extra_metadata'->>'visibility'
ORDER BY cnt DESC;
