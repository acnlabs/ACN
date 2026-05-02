-- Agent visibility backfill
-- Run via: railway connect Postgres --command "$(cat scripts/backfill_visibility.sql)"
-- Convention: agents without metadata.visibility default to "real" in the API,
-- so only test/demo/spam agents need explicit tagging.

-- ── 1. Test bots (E2E, Jury, Bnd, Adv, Debug, MP, Dsp, EdgeCase, etc.) ──────
UPDATE agents
SET metadata = COALESCE(metadata, '{}') || '{"visibility":"test"}'
WHERE name ~ '^(E2E-|Jury-|Bnd-|Adv-|Adv2-|Debug-|DBG-|MP-|Dsp-|Dsp2-|EdgeCase-
                |TestCreator|TestSolver|ClaimTestBot|VerifyBot
                |ReferrerBot|ReferredBot|TaskCreatorBot|FreshTaskBot
                |ProdInvitedBot|InvitedBot|RefBot|WalletTest
                |EscrowTest|review-creator|review-solver
                |test-agent|test-six)'
  AND (metadata->>'visibility') IS NULL;

-- ── 2. Demo agents (OpenClaw-Demo, OpenClaw-Coding, etc.) ────────────────────
UPDATE agents
SET metadata = COALESCE(metadata, '{}') || '{"visibility":"demo"}'
WHERE name ~ '^(OpenClaw-Demo|OpenClaw-Coding|OpenClaw-Analysis|OpenClawWorker
                |OpenClawDemo|ACN Reviewer|OpenClaw-Main)'
  AND (metadata->>'visibility') IS NULL;

-- ── 3. Real agents — explicit tag so future API changes stay safe ─────────────
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
  'OpenClaw-Main-Agent',
  'OpenClaw-CodingAgent',
  'OpenClaw-AnalysisBot'
)
  AND (metadata->>'visibility') IS NULL;

-- ── 4. Verify ─────────────────────────────────────────────────────────────────
SELECT
  metadata->>'visibility' AS visibility,
  COUNT(*) AS cnt
FROM agents
GROUP BY metadata->>'visibility'
ORDER BY cnt DESC;
