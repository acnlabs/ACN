# ADR-0008: Unified API Audience (`https://api.agentplanet.org`)

**Status:** Accepted — rollout pending (phased dual-accept)
**Date:** 2026-05-31
**Deciders:** AgentPlanet platform owner + ACN core team + backend
**Related:** ADR-0007 (Unified Agent Identity — settles D7 "Audience model"); API-host canonicalization to `api.agentplanet.org`; acnlabs/Agentplanet-backend#13

> **Decision:** the AgentPlanet backend is **one resource** with **one audience**:
> `https://api.agentplanet.org`. Both human tokens (Auth0) and agent tokens
> (ACN) carry that single `aud`. Human vs agent is distinguished at the
> **issuer** layer (Auth0 vs ACN), never at the audience layer. ADR-0007 left D7
> as "a single platform `aud`, revisit later" — this ADR fixes the actual
> string and folds the human side in too.

---

## Context

The API **host** was canonicalized to `https://api.agentplanet.org` (frontend
`agentplanet.org`, backend `api.agentplanet.org`). The **audience** identifiers
were not, and three unrelated strings are in play — none of them the canonical
host:

| Path | Where | `aud` value | State |
|---|---|---|---|
| Agent (ACN JWT) — issue | ACN config | `https://api.agenticplanet.space` | consistent with backend |
| Agent (ACN JWT) — verify | backend `ACN_JWT_AUDIENCE` | `https://api.agenticplanet.space` | ✅ works (AgentMother E2E) |
| Human (Auth0) — request | Vercel `NEXT_PUBLIC_AUTH0_AUDIENCE` | `https://api.agenticplanet.space` | |
| Human (Auth0) — verify | backend `AUTH0_AUDIENCE` | `https://acn.acnlabs.dev` | ❌ **mismatch** |

**Verified 2026-05-31** (via `vercel env pull` + Railway `railway variables`):
the human path is a **latent production bug** — the frontend requests `aud =
api.agenticplanet.space`, the backend strictly verifies `aud = acn.acnlabs.dev`
(`backend/app/auth/auth0.py` passes `audience=` to `jwt.decode`, so a mismatch
is a hard 401). Human login → backend call would 401 across the board; it is
unnoticed only because the platform is not yet promoted.

### Three layers people conflate

The fragmentation comes from treating these as one thing. They are independent:

| Layer | Meaning | Must be reachable? | Correct target |
|---|---|---|---|
| **Service hostname** | where the request actually goes | ✅ yes | `agentplanet.org` / `api.agentplanet.org` |
| **Issuer (`iss`)** | who vouches for the principal | should serve JWKS | human → Auth0; agent → ACN |
| **Audience (`aud`)** | which *resource* the token targets | ❌ opaque string | the AgentPlanet backend = one value |

`aud` is an opaque identifier (it does **not** have to resolve), so the only
real requirements are: issuer and verifier agree on the string, and it is
registered as an API in the IdP that mints it (Auth0).

---

## Decision

1. **One audience for the AgentPlanet backend:** `https://api.agentplanet.org`.
   - Auth0 (human tokens): the API Identifier the frontend requests and the
     backend verifies is `https://api.agentplanet.org`.
   - ACN (agent JWTs): minted with `aud = https://api.agentplanet.org`; backend
     verifies the same.
   - Backend verifies a **single** audience for both token types; it
     distinguishes human vs agent by **`iss`** (Auth0 JWKS vs ACN JWKS) and by
     claims (`sub` shape, `permissions`/`scope`), **not** by `aud`.
2. **Issuer-level human/agent split is retained** (ADR-0007): humans → Auth0
   (interactive login), agents → ACN (`client_credentials`, ACN registry).
   Merging principals is explicitly rejected.
3. **No dedicated `auth.*` subdomain as an audience.** `api.agentplanet.org`
   *is* the resource identifier. A separate auth audience would re-introduce the
   "which token for which service" confusion ADR-0007 removed.
4. **ACN keeps `acnlabs.dev` as its own issuer host.** ACN is a cross-platform
   network; AgentPlanet is one consumer. The agent token's `iss` stays ACN
   (`https://api.acnlabs.dev`); only its `aud` (the AgentPlanet resource) becomes
   `api.agentplanet.org`. No conflict.
5. **Auth0 Custom Login Domain (e.g. `login.agentplanet.org`) is deferred to a
   later, optional phase.** It only brands the login page / hides the
   `dev-*.us.auth0.com` tenant and changes the human `iss`; it is not required to
   fix the audience and adds Auth0 plan + cert cost. Until then humans keep
   `iss = dev-ypufda63738rkary.us.auth0.com`.

### Why `api.agentplanet.org` and not `acn.acnlabs.dev` or `api.agenticplanet.space`

The audience names the *resource being called*. That resource is the
**AgentPlanet backend**, so the AgentPlanet-branded canonical host is the
semantically correct identifier. `acn.acnlabs.dev` wrongly implies "ACN's API"
(ACN is the issuer, not this resource); `api.agenticplanet.space` is the
deprecated, Cloudflare-blocked host we are migrating off. Unifying on
`api.agentplanet.org` also matches the operator's mental model ("everything
AgentPlanet lives under agentplanet.org").

---

## Considered Options

| | A: unify on `api.agentplanet.org` (chosen) | B: unify on existing `acn.acnlabs.dev` | C: keep human/agent audiences separate |
|---|---|---|---|
| Matches canonical host | ✅ | ❌ | ❌ |
| Drops deprecated `agenticplanet.space` | ✅ | ✅ | partial |
| Semantically correct (resource = AgentPlanet) | ✅ | ❌ (reads as ACN's API) | mixed |
| Verifier config | one audience | one audience | two audiences to keep in sync |
| Migration cost | new Auth0 API + dual-accept window | rename frontend only | none, but debt/confusion stays |

**B** rejected: keeps an ACN-branded string for an AgentPlanet resource and
loses the host alignment. **C** rejected: two audiences for one backend is the
exact fragmentation we are removing; issuer + claims already separate the
principals. **A** is the only option that is both correct and aligned with the
canonical host.

---

## Consequences

### Positive
- One opaque audience for the whole AgentPlanet API; verifier config is a single
  value; the deprecated `agenticplanet.space` and the misleading
  `acn.acnlabs.dev`-for-humans both disappear.
- Fixes the latent human-login 401.
- Audience now matches the canonical host — no more "three domains" confusion.

### Negative / costs
- Requires a **new Auth0 API** with identifier `https://api.agentplanet.org`
  (Auth0 API identifiers are immutable — you create a new one, you cannot rename
  the existing). The old API stays registered until cutover completes.
- A **dual-accept migration window** on the backend (verify old **and** new
  audiences) to avoid locking out in-flight tokens during the swap.
- Production auth change — must be sequenced (Auth0 → backend dual-accept →
  clients → drop old), or humans/agents 401 mid-flight.

### Neutral
- Issuers, JWKS, and the human/agent split are unchanged.
- `aud` being opaque means no DNS/cert work is needed for the audience itself.

---

## Migration Plan (decided once, phased dual-accept, reversible)

Ordering matters: **widen the verifier before changing any client**, then
**narrow the verifier only after all clients moved**.

- **M1 — Auth0:** create a new API with identifier `https://api.agentplanet.org`
  (copy scopes/permissions from the current API). Old API left registered.
- **M2 — backend dual-accept (deploy first):** `auth0_audience` and
  `acn_jwt_audience` each accept a **set** of audiences (old + new). At this
  point the human path also stops 401-ing for whichever audience the frontend
  sends.
- **M3 — clients switch to new `aud`:**
  - Frontend Vercel `NEXT_PUBLIC_AUTH0_AUDIENCE = https://api.agentplanet.org`
    (all envs), redeploy.
  - ACN agent-JWT issue `aud = https://api.agentplanet.org`.
  - Store `SKILL.md` `token_audience` → `https://api.agentplanet.org`.
- **M4 — narrow + clean:** after old tokens have expired (≥ max TTL, agent JWT
  TTL is 1h), backend drops the old audiences and verifies only
  `https://api.agentplanet.org`. Align hardcoded code defaults
  (`backend/app/config.py` `auth0_audience`/`acn_jwt_audience`, frontend
  `auth0.ts` fallback). Remove the now-unused old Auth0 API and the stale
  `https://api.agentplanet.com` in `env.template`.

> Blast radius: backend (verify config) + frontend (Vercel env) + ACN (issue
> config) + Auth0 dashboard. No third party trusts these audiences.

### Ownership / handoff
- **Code (agent):** backend dual-accept + final narrowing; frontend `auth0.ts`
  default; `SKILL.md` `token_audience`; ACN issue-audience config.
- **Dashboard (human):** Auth0 new API (M1); Vercel env (M3); Railway env (M2/M4).

---

## Open Sub-Decisions

| # | Decision | Default / status |
|---|---|---|
| A1 | Auth0 Custom Login Domain (`login.agentplanet.org`) | **Deferred** — optional later phase; keep `dev-*.us.auth0.com` for now |
| A2 | Should ACN's *own* API (acnlabs.dev) audience also be touched | **No** — out of scope; this ADR is only the AgentPlanet backend resource |
| A3 | Keep old Auth0 API registered after cutover | Drop in M4 once no tokens reference it |
