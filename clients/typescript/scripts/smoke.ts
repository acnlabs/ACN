/**
 * Smoke test for v0.12.0 SDK against a running ACN backend.
 *
 * Covers every contract that the 6-round audit touched:
 *  1. Auth header (Authorization: Bearer) — must succeed on a protected route
 *  2. Agent self-registration (/agents/join)
 *  3. Subnet create
 *  4. registerSubnetHarness (PATCH /subnets/:id/harness with secret)
 *  5. createTask  — `reward: string`, `deadline_hours: number`, required fields
 *  6. getTask     — response shape matches Task interface
 *  7. listTasks   — `status: "open" | "in_progress" | "submitted"` filter
 *  8. acceptTask  — returns { task, participation_id }
 *  9. submitTask  — sends `submission` (not `submissionContent`)
 * 10. reviewTask  — sends `notes` (not `feedback`)
 * 11. TaskStatus  — assert backend emits one of the documented values
 * 12. joinSubnet  — POST /api/v1/agents/{id}/subnets/{subnet_id} (0.11.2 canonical)
 * 13. getAgentSubnets — GET /api/v1/agents/{id}/subnets (returns {agent_id, subnets[]})
 * 14. leaveSubnet — DELETE /api/v1/agents/{id}/subnets/{subnet_id}
 * 15. rotateApiKey (H1) — new key works; old key returns 401; participant
 *     identity (agent_id) survives the rotation (subnet membership intact).
 *
 * Usage:
 *   ACN_URL=http://127.0.0.1:9000 npx tsx scripts/smoke.ts
 */

import { ACNClient } from "../src/index.js";

const ACN_URL = process.env.ACN_URL ?? "http://127.0.0.1:9000";
// ACN backend rejects names ending with 8+ digits (auto-generated guard),
// so use a short alphanumeric suffix instead of Date.now().
const STAMP = Math.random().toString(36).slice(2, 8);

function log(stage: string, detail?: unknown) {
  if (detail !== undefined) {
    console.log(`\n[${stage}]`, typeof detail === "string" ? detail : JSON.stringify(detail, null, 2));
  } else {
    console.log(`\n[${stage}]`);
  }
}

async function main() {
  // ── 1. Register a fresh creator agent (gets its own acn_xxx api key) ────
  log("register creator agent");
  const anon = new ACNClient({ baseUrl: ACN_URL });
  const creator = await anon.joinACN({
    name: `smoke-creator-${STAMP}`,
    description: "v0.11.0 SDK smoke test creator (auto-generated, safe to delete)",
    a2a_endpoint: "http://127.0.0.1:0/jsonrpc",
    tags: ["smoke", "creator"],
  });
  log("creator.agent_id", creator.agent_id);

  const creatorClient = new ACNClient({ baseUrl: ACN_URL, apiKey: creator.api_key });

  // ── 2. Register a solver agent in a second subnet identity ──────────────
  log("register solver agent");
  const solver = await anon.joinACN({
    name: `smoke-solver-${STAMP}`,
    description: "v0.11.0 SDK smoke test solver (auto-generated, safe to delete)",
    a2a_endpoint: "http://127.0.0.1:0/jsonrpc",
    tags: ["smoke", "solver"],
  });
  log("solver.agent_id", solver.agent_id);
  const solverClient = new ACNClient({ baseUrl: ACN_URL, apiKey: solver.api_key });

  // ── 3. Create a subnet (creator owns it) ─────────────────────────────────
  log("create subnet");
  const subnet = await creatorClient.createSubnet({
    name: `smoke-${STAMP}`,
    description: "v0.11.0 SDK smoke test subnet",
  });
  log("subnet.subnet_id", subnet.subnet_id);

  // ── 4. Register a harness webhook (with secret) — proves PATCH /harness ─
  log("registerSubnetHarness");
  await creatorClient.registerSubnetHarness(
    subnet.subnet_id,
    "http://127.0.0.1:0/webhook",
    "smoke-secret-32-bytes-of-entropy-yes",
  );

  // ── 5. createTask using new schema (reward: string, deadline_hours) ─────
  log("createTask");
  const created = await creatorClient.createTask({
    title: `smoke task ${STAMP}`,
    description: "v0.11.0 SDK smoke task — verifies createTask field set.",
    reward: "0",
    deadline_hours: 24,
    reward_currency: "credits",
    max_participants: 1,
  });
  log("created.task_id", created.task_id);
  log("created.status (should be 'open')", created.status);
  log("created.reward type", typeof created.reward);
  if (created.status !== "open") throw new Error(`expected status 'open', got '${created.status}'`);
  if (typeof created.reward !== "string") throw new Error(`expected reward string, got ${typeof created.reward}`);

  // ── 6. getTask ──────────────────────────────────────────────────────────
  log("getTask");
  const fetched = await creatorClient.getTask(created.task_id);
  if (fetched.task_id !== created.task_id) throw new Error("getTask mismatch");

  // ── 7. listTasks with new TaskStatus filter ─────────────────────────────
  log("listTasks status=open");
  const open = await creatorClient.listTasks({ status: "open", limit: 50 });
  if (!open.tasks.some((t) => t.task_id === created.task_id)) {
    throw new Error("freshly created task not present in listTasks(open)");
  }

  // ── 8. acceptTask (solver) — returns TaskAcceptResponse ─────────────────
  log("acceptTask (solver)");
  const accept = await solverClient.acceptTask(created.task_id, "I'll take this");
  log("accept.participation_id", accept.participation_id ?? "<null/single-participant>");
  log("accept.task.status (should be in_progress)", accept.task.status);
  if (accept.task.status !== "in_progress") throw new Error(`expected in_progress after accept, got ${accept.task.status}`);

  // ── 9. submitTask — body field name must be `submission` ────────────────
  log("submitTask (solver)");
  const submitted = await solverClient.submitTask(
    created.task_id,
    "Smoke submission content (≥5 chars).",
    { participationId: accept.participation_id ?? undefined },
  );
  log("submitted.status (should be 'submitted')", submitted.status);
  if (submitted.status !== "submitted") throw new Error(`expected 'submitted', got ${submitted.status}`);

  // ── 10. reviewTask — body field name must be `notes` ───────────────────
  log("reviewTask approve (creator)");
  const reviewed = await creatorClient.reviewTask(created.task_id, true, "LGTM via smoke");
  log("reviewed.status (should be 'completed')", reviewed.status);
  if (reviewed.status !== "completed") throw new Error(`expected 'completed', got ${reviewed.status}`);

  // ── 12-14. Canonical subnet membership paths (0.11.2). Uses a *third*
  //   agent so we don't touch creator/solver state from steps above. ──────
  log("register joiner agent for subnet-membership checks");
  const joiner = await anon.joinACN({
    name: `smoke-joiner-${STAMP}`,
    description: "v0.11.2 SDK smoke joiner (auto-generated, safe to delete)",
    a2a_endpoint: "http://127.0.0.1:0/jsonrpc",
    tags: ["smoke", "joiner"],
  });
  const joinerClient = new ACNClient({ baseUrl: ACN_URL, apiKey: joiner.api_key });

  log("joinSubnet (canonical /api/v1/agents/{id}/subnets/{subnet_id})");
  const joinResp = await joinerClient.joinSubnet(joiner.agent_id, subnet.subnet_id);
  if ((joinResp as { status?: string }).status !== "joined") {
    throw new Error(`expected joinSubnet status='joined', got ${JSON.stringify(joinResp)}`);
  }

  log("getAgentSubnets (canonical /api/v1/agents/{id}/subnets)");
  const subs = await joinerClient.getAgentSubnets(joiner.agent_id);
  if (!subs.subnets.includes(subnet.subnet_id)) {
    throw new Error(`getAgentSubnets did not list ${subnet.subnet_id}: ${JSON.stringify(subs)}`);
  }

  log("leaveSubnet (canonical DELETE /api/v1/agents/{id}/subnets/{subnet_id})");
  const leaveResp = await joinerClient.leaveSubnet(joiner.agent_id, subnet.subnet_id);
  if ((leaveResp as { status?: string }).status !== "left") {
    throw new Error(`expected leaveSubnet status='left', got ${JSON.stringify(leaveResp)}`);
  }
  const subsAfter = await joinerClient.getAgentSubnets(joiner.agent_id);
  if (subsAfter.subnets.includes(subnet.subnet_id)) {
    throw new Error(`getAgentSubnets still has ${subnet.subnet_id} after leave`);
  }

  // ── 15. rotateApiKey (H1) — agent self-rotation; old key invalidates ───
  //   Uses the joiner agent because it's already in a clean post-leave
  //   state and won't perturb any in-flight task we exercised earlier.
  //   We re-join the subnet on the joiner so we can prove that rotation
  //   does NOT lose subnet membership (the whole point of H1 vs a
  //   re-register: agent_id and bindings must survive).
  log("rotateApiKey — re-join subnet first so we can prove identity survives");
  await joinerClient.joinSubnet(joiner.agent_id, subnet.subnet_id);

  log("rotateApiKey (H1) — agent self-rotation");
  const rotated = await joinerClient.rotateApiKey(joiner.agent_id);
  log("rotated.api_key (first 12 chars)", rotated.api_key.slice(0, 12) + "…");
  if (!rotated.api_key.startsWith("acn_")) {
    throw new Error(`rotateApiKey returned non-acn_* key: ${rotated.api_key.slice(0, 16)}…`);
  }
  if (rotated.api_key === joiner.api_key) {
    throw new Error("rotateApiKey echoed the OLD key back — server didn't actually rotate");
  }

  // New client built from the rotated key must work.
  const rotatedClient = new ACNClient({ baseUrl: ACN_URL, apiKey: rotated.api_key });
  const meAfter = await rotatedClient.getMyAgent();
  if (meAfter.agent_id !== joiner.agent_id) {
    throw new Error(
      `agent identity changed after rotation: ${joiner.agent_id} → ${meAfter.agent_id}`,
    );
  }

  // Subnet membership must be preserved across the rotation — that's
  // the H1 win over "delete + re-register".
  const subsAfterRotate = await rotatedClient.getAgentSubnets(joiner.agent_id);
  if (!subsAfterRotate.subnets.includes(subnet.subnet_id)) {
    throw new Error(
      `subnet membership lost after rotateApiKey: ${JSON.stringify(subsAfterRotate)}`,
    );
  }

  // Old key must now return 401. We bypass the SDK's exception path so
  // we can assert on the exact HTTP status — the SDK throws on non-2xx
  // and any 4xx code would satisfy a try/catch, masking a silent
  // accept-old-key regression.
  log("rotateApiKey — verifying OLD key now 401s");
  const oldKeyResp = await fetch(`${ACN_URL}/api/v1/agents/me`, {
    headers: { Authorization: `Bearer ${joiner.api_key}` },
  });
  if (oldKeyResp.status !== 401) {
    throw new Error(
      `OLD key still accepted after rotation — expected 401, got ${oldKeyResp.status}`,
    );
  }

  // ── Final sanity ────────────────────────────────────────────────────
  log("DONE — all 15 contract checks passed");
}

main().catch((err) => {
  console.error("\n❌ SMOKE FAILED:", err);
  process.exit(1);
});
