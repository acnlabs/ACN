-- Step 1: tag test bots
UPDATE agents
SET metadata = COALESCE(metadata, '{}') || '{"visibility":"test"}'
WHERE name ~ '^(E2E-|Jury-|Bnd-|Adv-|Adv2-|Debug-|DBG-|MP-|Dsp-|Dsp2-|EdgeCase-|TestCreator|TestSolver|ClaimTestBot|VerifyBot|ReferrerBot|ReferredBot|TaskCreatorBot|FreshTaskBot|ProdInvitedBot|InvitedBot|RefBot|WalletTest|EscrowTest|review-creator|review-solver|test-agent|test-six)'
AND (metadata->>'visibility') IS NULL;

-- Step 2: tag demo bots
UPDATE agents
SET metadata = COALESCE(metadata, '{}') || '{"visibility":"demo"}'
WHERE name ~ '^(OpenClaw-Demo|OpenClaw-Coding|OpenClaw-Analysis|OpenClawWorker|OpenClawDemo|ACN Reviewer|OpenClaw-Main)'
AND (metadata->>'visibility') IS NULL;

-- Step 3: explicitly tag real agents
UPDATE agents
SET metadata = COALESCE(metadata, '{}') || '{"visibility":"real"}'
WHERE name IN (
  'Aria',
  'blue-intel',
  '小宁-架构师',
  '小思-翻译协作',
  'Samantha',
  'Karna',
  'Ziling',
  'xingchen',
  'ChillClaw',
  '代码助手',
  'slop-farm',
  'erisa-copy',
  'athena-visual',
  'WHW888_bot',
  'OpenClaw-Tech-Agent',
  'OpenClaw-Main-Agent'
)
AND (metadata->>'visibility') IS NULL;

-- Verify
SELECT metadata->>'visibility' AS visibility, COUNT(*) AS cnt
FROM agents
GROUP BY metadata->>'visibility'
ORDER BY cnt DESC;
