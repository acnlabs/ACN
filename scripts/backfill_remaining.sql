UPDATE agents
SET metadata = COALESCE(metadata, '{}') || '{"visibility":"test"}'
WHERE (metadata->>'visibility') IS NULL;

SELECT metadata->>'visibility' AS visibility, COUNT(*) AS cnt
FROM agents
GROUP BY metadata->>'visibility'
ORDER BY cnt DESC;
