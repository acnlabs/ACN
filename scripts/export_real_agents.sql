-- Export real agents as JSON matching ACN API response format
SELECT json_build_object(
  'total', COUNT(*),
  'agents', json_agg(
    json_build_object(
      'agent_id', agent_id,
      'name', name,
      'description', description,
      'endpoint', endpoint,
      'tags', COALESCE(skills, ARRAY[]::varchar[]),
      'status', status,
      'subnet_ids', COALESCE(to_json(subnet_ids)::jsonb, '[]'::jsonb),
      'agent_card', NULL,
      'agent_card_url', metadata->>'agent_card_url',
      'social_card_url', social_card_url,
      'erc8004_agent_id', metadata->>'erc8004_agent_id',
      'erc8004_chain', metadata->>'erc8004_chain',
      'wallet_address', wallet_address,
      'registered_at', registered_at,
      'last_heartbeat', last_heartbeat,
      'owner', owner,
      'metadata', metadata
    ) ORDER BY name
  )
)
FROM agents
WHERE metadata->'extra_metadata'->>'visibility' = 'real';
