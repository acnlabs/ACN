# Runbook: Agent JWT signing-key rotation (overlapping kids)

Operational guide for rotating the RS256 key that ACN uses to mint agent
JWTs (`POST /oauth/token`), with **zero downtime** and **no forged-token
window**. Implements the "short TTL + overlapping kids" half of
[ADR-0007 D2](../adr/0007-unified-agent-identity-and-credential-issuance.md)
and closes issue #154.

For the immediate-revocation companion (denylist) see issue #155 — that is a
separate mechanism and is not part of key rotation.

---

## 1) Background

- ACN mints agent JWTs signed RS256 with a single **active** private key,
  tagged by its `kid` in the JWT header.
- Relying parties (the AgentPlanet backend) verify **offline** against the
  public keys published at `GET /.well-known/jwks.json`, selecting the key by
  `kid`. They cache the JWKS.
- To rotate the key without rejecting in-flight tokens, both the old and new
  public keys must be **published simultaneously** (overlapping kids) for at
  least one max-token-TTL window.

### What ships (code)

- `Settings.agent_jwt_private_key` + `agent_jwt_kid` — the **primary**
  (signing) key. This is the only key used to mint.
- `Settings.agent_jwt_private_key_secondary` + `agent_jwt_kid_secondary` — a
  **verification-only** key published in JWKS but never used to mint.
- `AgentTokenIssuer.jwks()` returns **both** public keys when a secondary is
  configured; `mint()` uses only the primary.
- Verification (`acn/auth/middleware.py`) now does a **strict `kid` match** —
  the old "fall back to the first key" behaviour is removed, so a token with
  an unknown `kid` is rejected (401 `Unknown token signing key.`).

### Config / env vars

| Env var | Meaning |
|---------|---------|
| `AGENT_JWT_PRIVATE_KEY` | PEM of the **active** signing key (mints) |
| `AGENT_JWT_KID` | `kid` of the active key (default `acn-agent-key-1`) |
| `AGENT_JWT_PRIVATE_KEY_SECONDARY` | PEM of an **extra verification-only** key (rotation window) |
| `AGENT_JWT_KID_SECONDARY` | `kid` of the secondary key (default `acn-agent-key-2`) |

> The two kids **must differ**. A colliding secondary kid is logged
> (`agent_jwt_secondary_kid_collision`) and ignored. An invalid secondary PEM
> is logged (`agent_jwt_secondary_key_invalid`) and skipped — it can never
> disable the valid primary.

---

## 2) Pre-flight

- Know the current max token TTL: `AGENT_JWT_TTL_SECONDS` (default **1800 s /
  30 min**). The overlap window must be **≥ this value** plus the relying
  party's JWKS cache TTL.
- Confirm the relying party (AgentPlanet backend) refreshes JWKS by `kid` miss
  — it does: `try_resolve_acn_agent` refreshes once on an unknown `kid` before
  failing (`backend/app/auth/acn_jwt.py`).
- Generate the new RSA key (2048-bit, PKCS8 PEM):

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out new-agent-jwt.pem
# keep this secret; it goes into a Railway env var, never into git
```

Pick the new `kid` by incrementing the suffix, e.g. current `acn-agent-key-1`
→ new `acn-agent-key-2`.

---

## 3) Rotation procedure (zero-downtime)

Three deploys. **Do not skip the wait between steps 2 and 3** — that wait is
what guarantees no valid token is rejected.

### Step 1 — Pre-publish the NEW key (verification-only)

Keep minting with the old key; add the new key to JWKS so relying parties
learn it **before** any token is signed with it.

```bash
# Railway (ACN service)
railway variables \
  --set "AGENT_JWT_PRIVATE_KEY_SECONDARY=$(cat new-agent-jwt.pem)" \
  --set "AGENT_JWT_KID_SECONDARY=acn-agent-key-2"
# AGENT_JWT_PRIVATE_KEY / AGENT_JWT_KID unchanged (old key still mints)
```

Verify JWKS now serves both kids:

```bash
curl -s https://api.acnlabs.dev/.well-known/jwks.json | jq '.keys[].kid'
# → "acn-agent-key-1"   (primary, minting)
# → "acn-agent-key-2"   (secondary, verify-only)
```

Wait for relying-party JWKS caches to refresh (a few minutes; the backend also
force-refreshes on `kid` miss, so this is belt-and-suspenders).

### Step 2 — Promote: NEW key mints, OLD key demoted to verification-only

Swap the slots: the new key becomes primary (starts minting), the old key
moves into the secondary slot so tokens already minted with it still verify.

```bash
railway variables \
  --set "AGENT_JWT_PRIVATE_KEY=$(cat new-agent-jwt.pem)" \
  --set "AGENT_JWT_KID=acn-agent-key-2" \
  --set "AGENT_JWT_PRIVATE_KEY_SECONDARY=$(cat old-agent-jwt.pem)" \
  --set "AGENT_JWT_KID_SECONDARY=acn-agent-key-1"
```

Verify new tokens carry the new `kid`:

```bash
TOKEN=$(curl -s -X POST https://api.acnlabs.dev/oauth/token \
  -d grant_type=client_credentials -d client_id=$AGENT_ID -d client_secret=$ACN_KEY \
  -d audience=https://api.agentplanet.org | jq -r .access_token)
echo "$TOKEN" | cut -d. -f1 | base64 -d 2>/dev/null | jq .kid   # → "acn-agent-key-2"
```

**Wait ≥ one max TTL window (≥ 30 min, recommend 60 min for safety).** During
this window: new tokens verify against the new key; old tokens (kid
`acn-agent-key-1`) still verify against the demoted secondary. No rejections.

### Step 3 — Retire the OLD key

Once no token signed with the old key can still be live (elapsed ≥ TTL), drop
the secondary slot.

```bash
# Railway CLI has no "delete var"; set secondary equal to primary so the
# issuer dedupes it away (collision → ignored), or blank it via the dashboard.
railway variables \
  --set "AGENT_JWT_PRIVATE_KEY_SECONDARY=$(cat new-agent-jwt.pem)" \
  --set "AGENT_JWT_KID_SECONDARY=acn-agent-key-2"
```

> Cleaner: remove `AGENT_JWT_PRIVATE_KEY_SECONDARY` / `AGENT_JWT_KID_SECONDARY`
> entirely from the Railway dashboard. Setting the secondary equal to the
> primary kid triggers the documented collision-ignore path, which is safe but
> leaves dead env vars around.

Verify JWKS is back to a single key:

```bash
curl -s https://api.acnlabs.dev/.well-known/jwks.json | jq '.keys[].kid'
# → "acn-agent-key-2"
```

Securely destroy `old-agent-jwt.pem`.

---

## 4) Emergency rotation (key suspected compromised)

A compromised signing key means an attacker can mint valid tokens for **any**
agent. You cannot wait out a TTL window. Procedure:

1. **Immediately** run Step 2 (promote a fresh key to primary) **without** the
   overlap — i.e. do **not** put the compromised key in the secondary slot.
   This makes every token signed by the compromised key fail verification at
   once (unknown/!matching kid → 401).
2. Accept the blast radius: all currently-live legitimate tokens minted with
   the old key are also invalidated. Agents re-mint via `/oauth/token`
   (client_credentials) on their next call — the long-lived `acn_*` API key is
   unaffected, so this is automatic for well-behaved clients.
3. If you only need to cut off **specific** agents (not the signing key
   itself), use the denylist instead (issue #155,
   `POST /internal/agents/revoke`) — that does not require key rotation.

---

## 5) Rollback

- **Before Step 2:** harmless — just remove the secondary env vars; nothing
  was minted with the new key.
- **After Step 2, within the overlap window:** re-swap the slots back (old key
  → primary, new key → secondary). Both kids are still in JWKS so no token is
  rejected during the swap-back.
- **After Step 3:** the old key is retired; to roll back you must run a fresh
  rotation back to it (treat the old key as a new key).

---

## 6) Verification checklist

- [ ] `GET /.well-known/jwks.json` serves the expected set of kids at each step
- [ ] A freshly minted token's header `kid` matches the intended primary
- [ ] An old token (minted before Step 2) still verifies during the overlap
- [ ] After Step 3, a token with the retired `kid` is rejected with 401
      `Unknown token signing key.`
- [ ] AgentPlanet backend store calls keep working throughout (smoke: mint →
      `GET /api/store/orders/fulfillment-queue`)

---

## 7) Drill cadence

Run a **non-emergency** rotation drill on staging at least once per quarter so
the procedure stays muscle-memory and the overlap timing is validated against
the live JWKS cache behaviour. Record the drill date and any deviations in the
ops log.
