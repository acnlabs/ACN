# ADR-0007: Unified Agent Identity & Credential Issuance

**Status:** Proposed
**Date:** 2026-05-30
**Deciders:** ACN core team + AgentPlanet backend
**Related:** ADR-0006 (V6 ACL contract), Store marketplace integration (`backend/docs/STORE_MARKETPLACE_INTEGRATION.md`)

---

## Context

When an agent (e.g. AgentMother) wants to call an AgentPlanet resource server — the Store marketplace, the wallet, etc. — it must authenticate. Today an agent ends up carrying **two unrelated credentials**, issued by two different parties, validated two different ways:

| Credential | Issued by | Validated by | Mechanism |
|---|---|---|---|
| `acn_<...>` API key | **ACN** (`agent_service.generate_api_key`) | ACN only | Opaque string; SHA-256 hash stored, looked up in Redis/PG on every request (`get_agent_by_api_key`) |
| Auth0 M2M (`client_id`/`secret` → JWT) | **AgentPlanet backend** (via Auth0 Management API in `auth0_agent_service.py`) | AgentPlanet backend | Standard RS256 JWT verified against Auth0 JWKS |

### How it works today (code evidence)

- **ACN is already the agent identity authority.** `agent_service.py` mints `acn_{token_urlsafe(32)}`, stores only the hash, returns plaintext once, and supports `rotate_api_key`. But the token is **opaque** — `auth/middleware.py::verify_token` dispatches on the `acn_` prefix and resolves identity via a **Redis/PG lookup**, not signature verification.
- **ACN already verifies Auth0 JWTs too** — but only for *human users* (`type: "user"`). So ACN's middleware is dual-protocol: `acn_*` → agent (DB lookup), JWT → human (JWKS).
- **The AgentPlanet backend mints agent M2M credentials**, even though it is a resource server. `agent_auth.py` exposes `POST /api/agent-auth/credentials`, which ACN calls during registration; `auth0_agent_service.py` holds the **Auth0 Management** credential and creates a real M2M application per agent, persisting an `agent_id ↔ auth0_client_id` row.
- **Every agent request to the backend pays a DB lookup.** Both resolution paths — `auth/auth0.py::get_subject` and `auth/internal.py::verify_internal_or_agent` (the dependency the Store endpoints actually use) — take the JWT `sub` (`{client_id}@clients`) and call `_resolve_agent_client_id(client_id)`, which queries `agent_auth0_credentials` to recover `agent_id`. The agent's real identity is **not** in the token; it lives in a shared mapping table on the hot path of every protected call.

### Why it ended up like this

There is **no prior ADR** for agent credential issuance — this design was never deliberately chosen. The most likely history: the AgentPlanet backend predated the agent-network split, already had Auth0 (for humans) and the Auth0 Management integration wired, so when agents needed to call it, the team reused the backend's existing Auth0 plumbing and had ACN trigger it. ACN, being an open-source standalone network layer, kept its own native opaque key. The M2M bridge was bolted on after the fact. **Path-of-least-resistance accretion, not architecture.**

---

## Problem

1. **Two credentials per principal.** One agent, one identity, two tokens, two lifecycles, two issuers. Integrators must reason about "which token for which service."
2. **Resource server holds authorization-server privileges.** The money-handling backend holds an Auth0 Management credential that can create/delete arbitrary M2M clients — over-privileged, and the wrong place for issuance.
3. **Issuance is split from identity ownership.** ACN owns the agent's identity and `acn_*` lifecycle; the backend independently owns the agent's Auth0 M2M lifecycle. Rotation/revocation are not unified.
4. **Hot-path coupling via a shared table.** Every resource server that wants `agent_id` must read `agent_auth0_credentials`. The identity is not self-contained in the token.
5. **Interop is asymmetric.** The `acn_*` key is *proprietary* — any other system that wants to accept it must call back to ACN to introspect. The Auth0 JWT is standards-based but its "universality" is illusory: it is only accepted by services configured to trust *that one Auth0 tenant + audience* — a single-tenant walled garden, which does not generalize to an open agent network.

---

## Decision

**ACN becomes the agent identity authority *and* a standards-compliant OIDC token issuer. All resource servers (AgentPlanet backend, future consensus/feed, third parties) only *verify* ACN-issued JWTs by signature + scope. Auth0 is retained for human users only.**

Concretely:

1. **ACN issues standard JWTs**, not opaque keys, for agent→resource-server calls:
   - Asymmetric signing key (RS256/EdDSA), published JWKS at `/.well-known/jwks.json` and OIDC discovery at `/.well-known/openid-configuration`.
   - Token carries first-class claims: `iss` (ACN), `sub = agent_id`, `aud` (target API/platform), `scope` (capabilities, e.g. `store:sell wallet:read wallet:write`), `exp` (short TTL).
   - The existing long-lived `acn_*` key is **retained as the client credential** used to obtain short-lived JWTs (token-mint endpoint), analogous to a refresh/client-credential. This preserves ACN's offline/standalone posture and the issue/rotate machinery already in `agent_service.py`.
2. **`agent_id` travels inside the signed token.** Resource servers read it from the verified JWT — **no DB lookup, no shared mapping table** on the hot path.
3. **Authorization is scope-based.** Resource servers gate on `scope` claims rather than ad-hoc per-service logic; this scales cleanly to consensus/feed/etc.
4. **The AgentPlanet backend stops issuing.** It removes the Auth0 Management dependency for agents and becomes a pure relying party: the agent-resolution dependencies (`get_subject`, `verify_internal_or_agent`) verify ACN JWTs and read `agent_id` straight from the `sub`/claim instead of `_resolve_agent_client_id`. `agent_auth0_credentials` and `/api/agent-auth/credentials` are decommissioned (or reduced to a thin no-op/proxy during transition).
5. **ACN stays IdP-agnostic / open-source-friendly.** Token issuance sits behind a pluggable issuer interface. Self-hosted ACN signs with its own key and needs no external IdP. The hosted product may optionally federate/broker to Auth0, but resource servers always trust **one** issuer: ACN.

### Why this resolves "do we even need two?"

We do **not** need two agent credentials. The two existed only because ACN wanted an IdP-free native credential and the backend wanted standards-based JWTs. Making ACN a *standards-based JWT issuer* satisfies both with one credential: ACN-issued JWTs are as interoperable as any Auth0 JWT (both are "JWT + trust an issuer's JWKS"), while ACN remains the issuer of record and stays self-contained. The only separation we keep is **humans → Auth0, agents → ACN**, which is correct because they are genuinely different principals.

---

## Considered Options

| | A: Auth0 everywhere | **B: ACN issues standard JWTs (chosen)** | C: Keep both, fix smells only |
|---|---|---|---|
| Agent credentials | 1 | **1** | 2 (debt persists) |
| Issuance owner | Auth0 / backend | **ACN (identity authority)** | Split |
| Backend holds Auth0 Management | Yes | **No** | Yes |
| Hot-path DB mapping lookup | removable | **removed (claim in token)** | remains |
| ACN can run standalone without an external IdP | ❌ deployers must run Auth0 | **✅ ACN is the IdP** | ✅ |
| Interop for a new third-party verifier | config-only (must trust that tenant) | **config-only (trust ACN issuer)** | proprietary introspection for `acn_*` |
| Main cost | vendor lock-in; breaks ACN standalone | **ACN must implement JWT signing + JWKS + rotation/revocation** | tech debt never paid down |

**A** is rejected: it forces an external IdP dependency on an open network and locks ACN to Auth0. **C** is rejected: it leaves the debt in place permanently. **B** is the only option that both removes the debt and preserves ACN's open-network identity role.

---

## Consequences

### Positive
- One agent credential; unified issue/rotate/revoke at the identity authority.
- Backend loses Auth0 Management privilege → least privilege; money service no longer an authorization server.
- Resource servers verify offline; no shared mapping table; `agent_id` is a signed claim.
- Scope-based authz generalizes to all future services.
- ACN remains self-hostable with no external IdP requirement.

### Negative / costs
- **ACN must become a real OIDC issuer**: signing key management (generation, rotation, kid), JWKS + discovery endpoints, short-TTL tokens + a mint/refresh path, and a revocation strategy (short TTL + denylist, or introspection for the rare immediate-revoke case).
- A migration window where resource servers must dual-accept old and new credentials.
- Operational ownership of issuer security shifts fully onto ACN (previously partly leaned on Auth0's managed M2M).

### Neutral
- Human auth is unchanged (Auth0).
- ACN's existing `acn_*` issue/rotate/hashed-storage machinery is reused as the client credential, so this is an *extension* of ACN, not a rewrite.

---

## Migration Plan (decision once, rollout phased — dual-accept, reversible)

The target (B) is decided now so we do not build a throwaway intermediate. Rollout is incremental because an identity cutover across live services plus in-the-wild agents (AgentMother already holds Auth0 M2M) must never lock everyone out at once. The transition scaffolding below is deleted at the end and is **not** debt.

- **Phase 0 — do not block current integration.** Keep today's path; just configure backend `AUTH0_MGMT_CLIENT_ID/SECRET` + ACN wiring and provision AgentMother so Store goes live. No refactor.
- **Phase 1 — ACN becomes issuer (additive).** Add signing key, JWKS + OIDC discovery, a token-mint endpoint (`acn_*` client credential → short-lived JWT with `sub=agent_id` + `scope`). Old `acn_*` direct usage keeps working.
- **Phase 2 — resource servers dual-accept.** AgentPlanet backend accepts ACN-issued JWTs (verify ACN JWKS, read `agent_id`/`scope` from claims) *in addition to* the current Auth0 M2M + table lookup. New agent calls migrate to ACN JWTs. (Optional bridge: also have Auth0 inject an `agent_id` custom claim so the legacy path drops its DB lookup during the window.)
- **Phase 3 — decommission.** Backend removes the Auth0 Management dependency and `_resolve_agent_client_id` table lookup; `/api/agent-auth/credentials` is retired or proxied. Backend is a pure relying party. Delete dual-accept scaffolding.

> **Decommission blast radius is bounded.** The only consumer of the backend's `/api/agent-auth/credentials` (POST/GET/DELETE) is ACN's `acn/services/auth0_client.py`. No frontend, no other backend service, and no third party calls it. Phase 3 therefore touches exactly two repos (ACN + backend).

---

## Affected Components (implementation checklist)

Beyond the AgentPlanet backend, the following must be updated so the agent identity is read consistently from the new JWT rather than the opaque key / mapping table:

- **ACN must accept its own issued JWTs.** `acn/auth/middleware.py::verify_token` today branches `acn_*` → DB lookup vs JWT → Auth0. After Phase 1 it should also accept ACN-issued JWTs (read `sub=agent_id` + `scope`) for agent→ACN calls. The `acn_*` key becomes the *mint* credential (one hash lookup per token refresh) rather than per-request auth.
- **A2A `from_agent` middleware** (`acn/protocols/a2a/auth_middleware.py`) resolves `Authorization: Bearer <api_key>` → `agent_id` via `get_agent_by_api_key`. It must also resolve an ACN-issued JWT to `agent_id` (read `sub`) so the impersonation check keeps working when agents present JWTs.
- **`get_subject` callers** in the backend (wallets, billing_history, ai_chat, payments — anything currently relying on `_resolve_agent_client_id`) inherit the change for free once the resolver reads the claim, but each should be smoke-tested.

## Decisions Still Needed (with recommended defaults)

The Decision above fixes the *direction* (B). These sub-decisions must be ratified before/while building; each has a recommended default so the team can approve quickly.

| # | Decision | Recommended default | Why it matters |
|---|---|---|---|
| D1 | **Mint protocol** — bespoke `/token` exchange vs standard OAuth2 `client_credentials` | **Standard `client_credentials`** at an ACN `/oauth/token`; `acn_*` plays the client-secret role | Makes AgentMother's migration a *URL + credential swap* (same grant it already uses against Auth0); gives off-the-shelf SDK/tooling interop. **Single biggest lever on migration cost.** |
| D2 | **Signing alg + key custody + rotation** | EdDSA (Ed25519) or RS256; private key in secret store (Railway/KMS); publish overlapping `kid`s during rotation | Security-critical; determines JWKS shape and rotation ops |
| D3 | **Scope-granting policy** — does every agent get `store:sell`/`wallet:*` by default, or opt-in? | Minimal default (`acn:read acn:write`); capability scopes (`store:sell`, `wallet:write`) granted explicitly | Default-granting wallet/sell to every agent is a money-movement risk |
| D4 | **`sub` format** | `sub = agent_id` (UUID); drop `{client_id}@clients` parsing post-migration (keep compat only during dual-accept) | Resource-server parsing + what "identity" means in logs/audits |
| D5 | **TTL & refresh model** | Short TTL (15–60 min), **no separate refresh token** — re-mint with the long-lived `acn_*` | `acn_*` already *is* the durable credential; avoids a second token type |
| D6 | **End-state of opaque `acn_*` on ACN's own request path** | Accept directly during transition; target end-state = **mint-only** (closes the proprietary-introspection smell inside ACN too) | Decides whether the introspection coupling fully disappears or persists for back-compat |
| D7 | **Audience model** | Per-capability `scope` + a single platform `aud`; revisit if services need hard isolation | Token validation config across services |
| D8 | **Revocation** | Short TTL + denylist for the rare immediate-revoke (compromised agent); no full introspection endpoint | Balances offline verification against revoke latency |
| D9 | **Federation** | ACN is a fully independent issuer; hosted product *may* broker to Auth0 later, self-host never requires it | Preserves open-network posture |
| D10 | **Human ownership-bridge stays on Auth0** | Humans = Auth0 only; never granted agent scopes on the backend (buyers pay as `auth0|…`, sellers are always agents). Cross-ref ADR-0006 for the ACN-side bridge | Confirms humans are out of scope for this change |
| D11 | **Phase ownership + Phase 0 go/no-go** | ACN team owns Phase 1; backend team owns Phase 2–3; Phase 0 (configure + provision AgentMother) approved to start now | Operational sequencing |

**D1 is the one to settle first** — it directly determines how cheap the eventual AgentMother (and every future seller) migration is, and it should shape how Phase 0's skill documents the "get a token" step.
