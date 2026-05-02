-- Rename test + demo → hidden (simpler taxonomy)
UPDATE agents
SET metadata = jsonb_set(
  metadata,
  '{extra_metadata,visibility}',
  '"hidden"'
)
WHERE metadata->'extra_metadata'->>'visibility' IN ('test', 'demo');

-- Verify final distribution
SELECT
  metadata->'extra_metadata'->>'visibility' AS visibility,
  COUNT(*) AS cnt
FROM agents
GROUP BY metadata->'extra_metadata'->>'visibility'
ORDER BY cnt DESC;
