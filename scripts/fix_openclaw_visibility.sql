-- OpenClaw-Main-Agent was incorrectly tagged demo (regex overmatch).
-- Promote to real, consistent with OpenClaw-Tech-Agent.
UPDATE agents
SET metadata = jsonb_set(
  metadata,
  '{extra_metadata,visibility}',
  '"real"'
)
WHERE name = 'OpenClaw-Main-Agent';

-- Verify all OpenClaw agents
SELECT name, metadata->'extra_metadata'->>'visibility' AS visibility
FROM agents
WHERE name LIKE 'OpenClaw%'
ORDER BY name;
