# ACN Error Schema — WebSocket Protocol Migration (Sprint #11b RFC)

**Status**: ✅ Implemented (2026-05-03; behind `WEBSOCKET_CLOSE_REASON_FORMAT=compact` flag during 30-day SDK 0.6.0 bake window)
**Sprint**: Phase 2 review v2 P1 #11 row #11b
**Companion to**: [`acn-error-schema.md`](./acn-error-schema.md) (HTTP error contract, sprint rows #1–#11a; §5 → "WebSocket protocol close-frame contract" for the SDK consumer view)
**Approved on**: 2026-05-03
**Implemented on**: 2026-05-03
**Resolved decisions**: D1 = ~~D1c (hybrid)~~ → revised to **D1b (uniform application error-frame + close)** during implementation — see "Post-implementation revision" below · D2 = D2b (RFC-mapped buckets) · D3 = D3a (catalog reuse) · D4 = as-drafted · D5 = as-drafted · Q1 = yes · Q2 = defer to separate ticket · Q3 = distinct close codes (4401 / 4403) · Q4 = ship with SDK 0.6.0 (major break) · Q5 = yes

## Post-implementation revision (2026-05-03)

The original RFC §4.1 proposed including `details` (the `d` key) inline in the close-frame compact JSON. Implementation discovered that `api_key_agent_mismatch`'s `{path_agent, key_agent}` payload (two UUIDs + the JSON envelope) overflows the 123-byte RFC 6455 close-reason budget by ~60 bytes — the original RFC's 90-byte estimate undercounted by ~50%.

Rather than amputate or compress the shape per-code (which would re-introduce the cross-channel drift that union-schema codes already cause), the implementation moves `details` exclusively onto the application error-frame channel. Every site emits BOTH:

1. An application error-frame (`{type:"error", error_code, message, details, request_id}`) on the WebSocket data channel — no size cap, full payload.
2. A close with the RFC-mapped code from D2b + a compact `{c, r}` close-reason fallback for close-only SDK clients that miss the frame.

The application error-frame channel was already the post-handshake recommendation in §4.2; the revision applies it uniformly to all 4 (now 5 — see Q3 split) sites. The close-reason becomes a typed-but-minimal fallback rather than the primary payload channel. See `acn-error-schema.md` §5 → "WebSocket protocol close-frame contract" for the SDK parsing template.

§4.1 below preserves the original D1c proposal for historical traceability; the actual implementation follows the post-revision design.

---

## 0. TL;DR

The WebSocket endpoint `WEBSOCKET /ws/{agent_id}` is the last unmigrated surface in the ACN error-schema convergence. Unlike the 10 HTTP routers migrated in sprints #1–#11a, the WS endpoint cannot adopt `ACNHTTPError` directly because:

* WebSocket close frames carry an RFC 6455 `code` (uint16) + `reason` (≤123 bytes UTF-8), **not** an HTTP body.
* The ws.close event in browsers exposes only the close code + reason text; clients have no second channel to receive structured error bodies after the socket is gone.
* The current implementation collapses 4 distinct auth-failure causes into a single close code (4401) with hand-written `reason` strings — clients must regex the prose to disambiguate.

This RFC proposes **route 3 (hybrid)**: emit a typed application *error frame* (JSON, same four-field flat shape as `ACNErrorResponse`) **before** closing, and use a small RFC-6455-mapped close-code dictionary so reverse proxies and metrics keep working without parsing the reason. The recommended target is below; alternatives + open questions follow.

---

## 1. Scope

### In scope (this RFC)

* Auth-failure paths during the WebSocket handshake (today's 4 `_safe_close(code=4401, reason=…)` sites in `acn/routes/websocket.py`).
* Application-level errors after the handshake completes (currently routed through `WebSocketManager._send_message` with `{type: "error", error: <safe_string>}` — only used by *server-initiated* failures during message dispatch; no client-rejected-message path yet).
* The contract SDK clients depend on for distinguishing error classes without parsing `reason` text.
* Migration mechanics: ErrorCode catalog additions (if any), test format, doc updates, backward-compat.

### Out of scope (deferred)

* Per-message rate limiting on the WS channel — currently absent; any future limiter would emit its own close code (4429) governed by the same dictionary defined here, but the policy itself belongs to a separate ticket.
* Server-pushed business errors (manifest delivery failures, billing rejections, etc.) — those use `MessageType.ERROR` application frames today; aligning the *content* of those frames with `ACNErrorResponse` is straightforward and is folded into §5 below as an opportunistic cleanup, NOT a hard precondition for #11b.
* Realigning slowapi 429 (HTTP) — covered by the existing `WALLET_RATE_LIMIT_EXCEEDED` ticket in [`docs/BACKLOG.md`](../BACKLOG.md), unrelated.

---

## 2. Status quo — error sites today

`acn/routes/websocket.py` (post-#11a) emits errors at 4 sites, all via `_safe_close(code=4401, reason=…)`:

| # | Line | Trigger                                            | Reason text                                           |
|---|------|----------------------------------------------------|-------------------------------------------------------|
| 1 | L118 | Query-string token used while the operator-controlled flag `WEBSOCKET_ALLOW_QUERY_TOKEN` is False | `"Unauthorized: query-string token disabled"`         |
| 2 | L144 | First-message JSON's `type` ≠ `"auth"` or `token` missing | `"Unauthorized: expected auth message"`               |
| 3 | L149 | First-message JSON parse failure / disconnect mid-handshake | `"Unauthorized: invalid auth message"`                |
| 4 | L157 | Bearer / first-message / query-string token resolves to wrong agent | `"Unauthorized: invalid API key"`                     |

There is also a runtime-loop catch-all (L197) that re-raises after disconnecting from the manager — the surrounding starlette layer translates this to a 1011 close frame, with no `reason` body.

### What's wrong with this

1. **Single close code for 4 distinct causes.** Clients cannot programmatically tell "you must use first-message auth in production" from "your key is wrong" — both are 4401 + free-form text. Browsers have no other channel to learn which.
2. **Reason text is not stable contract.** Wording can change at any time without notice (matches `acn-error-schema.md` §5 unstable-surface convention for `message`), so SDKs that string-match are guaranteed to break.
3. **No `request_id` on the wire.** Operators cannot correlate a "WS connection refused" client-side report with server-side audit logs. Every other migrated route has `request_id` in the body; the WS path doesn't have a body.
4. **No `details` channel for context.** Site #4 (key-vs-path mismatch) cannot tell the SDK *which* agent the key did belong to (would help debugging) — there is nowhere to put the `{path_agent, key_agent}` shape we use everywhere else.
5. **Drift surface from HTTP.** The same auth-failure on `GET /api/v1/websocket/agent/{agent_id}/status` (HTTP, migrated in #11a) emits `error_code = "api_key_agent_mismatch"` + `details = {path_agent, key_agent}`. The WS path has no equivalent typed shape, so SDKs need two parsers for the same semantic failure depending on transport.

---

## 3. Design alternatives

The decision space splits along five axes (the D1–D5 numbering is for cross-reference in review):

### D1. Where does the structured error payload live?

| Option | Description                                                                                          | Pros                                                                                          | Cons                                                                                             |
|--------|------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------|
| **D1a — close-reason JSON** | Encode `{error_code, details, request_id}` as a JSON string in the close-frame `reason` field.       | Single event for clients (`ws.onclose` carries everything). No protocol changes.              | RFC 6455 caps `reason` at 123 bytes — too tight for nested `details`. Browsers truncate silently. |
| **D1b — pre-close error frame** | Send a JSON application frame `{type:"error", error_code, message, details, request_id}` then close. | No size limit. Same shape as `MessageType.ERROR` already in use post-handshake. Mirrors HTTP `ACNErrorResponse` 1:1. | Browser SDKs must coordinate `ws.onmessage` (frame) and `ws.onclose` (code) to reconstruct the error. Two events per failure. |
| **D1c — hybrid (recommended)** | Pre-handshake (steps 1–4 above): use D1a (compact, fits in reason because client has no message-receive loop yet). Post-handshake runtime errors: use D1b (free of size limit, message loop is alive). | Each path uses the cheapest channel for its phase. Browser SDK only needs the dual-event coordination for post-handshake errors, which are rarer. | Two contracts to document (one per phase). Slight client-SDK complexity. |

**Decision: D1c (accepted 2026-05-03).** The size budget for handshake-phase errors is comfortable (≤123 bytes is enough for `{"c":"api_key_agent_mismatch","r":"<uuid>"}` ≈ 60 bytes; we drop verbose keys and rely on the close-code dictionary in D2 to carry semantic class). Post-handshake errors get the full flat schema via the existing `MessageType.ERROR` channel.

### D2. RFC 6455 close-code dictionary

| Option | Codes used | SDK implication |
|--------|------------|------------------|
| **D2a — collapse to 4401** | 4401 for every auth failure (status quo). Clients ignore code, parse reason JSON. | One code → SDKs that only see the code can't tell auth from rate-limit from server fault. Reverse proxies / metrics dashboards lose granularity. |
| **D2b — RFC-mapped buckets (recommended)** | Map to a small dictionary mirroring HTTP status classes: 4400 (bad request), 4401 (unauthorised), 4403 (forbidden), 4429 (rate-limited; reserved for future), 1011 (internal error, RFC 6455 standard). | Existing HTTP-aware tooling works without protocol-specific config. Clients that key on close code alone get correct semantic class. Reason JSON refines within the class. |

**Decision: D2b (accepted 2026-05-03).** The mapping table:

| Close code | HTTP analogue | Used for                                                                  |
|------------|---------------|---------------------------------------------------------------------------|
| 4400       | 400           | Malformed first-message auth JSON (site #2, #3 above)                     |
| 4401       | 401           | Auth required / invalid key (sites #1, #4 above)                          |
| 4403       | 403           | API-key-resolves-to-different-agent (refinement of #4, see D3 below)      |
| 4429       | 429           | Reserved — future per-WS rate limiting                                    |
| 1011       | 500           | Internal server error (RFC 6455 standard, no body — central sanitisation) |

The 1011 code is RFC 6455 native (not a 4xxx custom code) so reverse proxies don't double-treat it as application-level. ACN never emits 1011 with a JSON reason — it stays opaque, mirroring the §1 5xx sanitisation contract.

### D3. ErrorCode reuse vs. new `WS_*` prefix

| Option | Description                                                                                  | Pros                                                                          | Cons                                                                       |
|--------|----------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------|----------------------------------------------------------------------------|
| **D3a — reuse cross-module catalog** | `AUTHENTICATION_REQUIRED` + new `details.reason` values; `API_KEY_AGENT_MISMATCH` for site #4. | One ErrorCode vocabulary across HTTP and WS. SDK clients write one switch. | `AUTHENTICATION_REQUIRED` is already in `UNION_SCHEMA_CODES`; adding more reasons keeps it union-bucket. |
| **D3b — new WS prefix** | `WS_HANDSHAKE_AUTH_REQUIRED`, `WS_INVALID_AUTH_MESSAGE`, `WS_KEY_AGENT_MISMATCH`.            | Transport-specific so SDKs can render WS-specific UX (e.g., reconnect prompt). | Two parallel catalogs (HTTP + WS) double the SDK switch cardinality. Same semantic failure (key/path mismatch) gets two codes. |

**Decision: D3a (accepted 2026-05-03).** Justifications:

1. The cross-module catalog explicitly designed for this — adding emitters does not break any consumer.
2. Site #4 (`api_key_agent_mismatch`) is *exactly* the same failure as the HTTP route migrated in #11a. Forcing two codes for the same failure breaks the "one switch" contract `acn-error-schema.md` §5 promises.
3. SDK clients that need WS-vs-HTTP differentiation can branch on transport (the call-site already knows which they used) rather than on `error_code`. This is what the analytics-vs-onchain split looks like and it works.

Concrete reason vocabulary for `AUTHENTICATION_REQUIRED` (additions for #11b):

| `details.reason`                            | Site | Trigger                                        |
|---------------------------------------------|------|------------------------------------------------|
| `ws_query_token_disabled`                   | #1   | Query-string token sent but flag is False       |
| `ws_invalid_auth_message`                   | #2   | First-message JSON shape wrong                  |
| `ws_invalid_auth_message_format`            | #3   | First-message JSON parse error                  |
| (no new reason; reuse `invalid_api_key`)    | #4 (auth half) | Bearer / query / first-message token does not resolve |

Site #4's "key resolves to different agent" half goes through `API_KEY_AGENT_MISMATCH` with the strict `{path_agent, key_agent}` shape — same as #11a's HTTP emitter.

### D4. Schema documentation + test format

* **Doc surface**: extend `acn-error-schema.md` with §6 **WebSocket close-frame contract**, mirroring §1 (response schema) but for the close-frame channel. The handshake-phase compact JSON shape gets pinned there with the close-code dictionary.
* **Tests**: add `tests/routes/test_websocket_error_schema_protocol.py` using `TestClient.websocket_connect(...)` (already in use by `test_websocket_auth_m14.py`). Each test asserts:
  1. `WebSocketDisconnect.code == <expected>` from D2b dictionary.
  2. `json.loads(WebSocketDisconnect.reason)` matches the compact handshake schema or, post-handshake, the next `receive_text()` carries the error frame.
  3. `request_id` is present and looks like UUID v4 (format-only, not value).
* **Consistency check**: extend `tests/test_error_code_details_consistency.py` to also AST-walk `_safe_close(...)` calls if they carry an `ErrorCode` annotation — TBD, see open question §7-Q1.

### D5. 5xx (1011) equivalent

Status quo: the L197 catch-all triggers starlette's default 1011 close with no body. We **keep** this behaviour:

* `ACNHTTPError` rejects 5xx by construction (`acn/core/errors.py` L427); the WS path inherits the same separation.
* No `error_code` / `details` is sent on 1011 — same opaque sanitisation as HTTP 5xx. The connection logger writes the structured error server-side; the wire is opaque.
* Browsers that see 1011 should treat it as transient and reconnect with backoff (same posture as HTTP 503).

---

## 4. Recommended target spec (compact summary)

### 4.1 Handshake-phase failure (close-frame channel)

Wire format on close:

```text
WebSocket close frame
  code:   <D2b dictionary entry>
  reason: <compact JSON>
```

Compact JSON shape (≤123 bytes target):

```json
{"c":"<error_code>","r":"<uuid-request-id>","d":{"<key>":"<value>"}}
```

* `c` = error_code (string, ASCII snake_case; same vocabulary as HTTP)
* `r` = request_id (UUID v4 string; same format as HTTP)
* `d` = details (object; OMITTED when empty to save bytes)

Field name compression is mandatory (single-letter keys) given the 123-byte budget. Full `error_code` / `details` / `request_id` keys appear only in D1b error frames where size is unbounded.

### 4.2 Post-handshake failure (application frame channel)

Wire format on send-error-then-close:

```json
{"type":"error","error_code":"<>","message":"<>","details":{},"request_id":"<>"}
```

Followed by a close frame with code from the D2b dictionary, reason = empty string (the body is on the previous frame).

This matches `ACNErrorResponse` 1:1, modulo the `type` discriminator that `MessageType.ERROR` has been using since day one.

### 4.3 ErrorCode catalog deltas

* `AUTHENTICATION_REQUIRED` — extend the *Used by* column in `acn-error-schema.md` §2 cross-module catalog to include the websocket protocol; extend the `details.reason` enum with three new values (`ws_query_token_disabled`, `ws_invalid_auth_message`, `ws_invalid_auth_message_format`). Stays in `UNION_SCHEMA_CODES`; no new code.
* `API_KEY_AGENT_MISMATCH` — extend the *Used by* column to include the websocket protocol. Strict shape unchanged (`{path_agent, key_agent}`).
* **0 new ErrorCode members.**

---

## 5. Migration plan (if RFC accepted as-is)

1. Implement a thin helper on `_safe_close` that takes `(error_code: ErrorCode, *, status_class: int, request_id: str, details: dict | None = None)` and serializes the compact reason. Keep the existing `_safe_close(code, reason)` overload as deprecated until call sites move.
2. Convert the 4 sites in `websocket.py` to the new helper, mapping:
   * site #1 → 4401 + `AUTHENTICATION_REQUIRED` reason `ws_query_token_disabled`
   * site #2 → 4400 + `AUTHENTICATION_REQUIRED` reason `ws_invalid_auth_message`
   * site #3 → 4400 + `AUTHENTICATION_REQUIRED` reason `ws_invalid_auth_message_format`
   * site #4 → 4401 + `AUTHENTICATION_REQUIRED` reason `invalid_api_key` (token-not-found half) **or** 4403 + `API_KEY_AGENT_MISMATCH` (token-found-but-wrong-agent half) — distinct close codes per D2b
3. Land the §6 addition in `acn-error-schema.md` describing the close-frame shape.
4. Add `tests/routes/test_websocket_error_schema_protocol.py` with 5 tests (one per site, plus one `request_id` format pin).
5. Update SDK release notes with the new wire shape (the parsing template in §4 of `acn-error-schema.md` extends naturally).
6. Update `tests/test_websocket_auth_m14.py`'s assertions to read the structured close reason (currently asserts on `code == 4401` only — extend to assert the JSON `c` field too, so M14 invariants strengthen alongside the schema migration).

### Backward compatibility

* **Wire-shape break**: SDKs that string-matched on the old `reason` text (`"Unauthorized: invalid API key"`) will see `{"c":"...","r":"..."}` instead. This is a hard break by design — the prose text was explicitly *not* a stable contract, but production SDKs may rely on it.
* **Mitigation**: announce in the same SDK release notes channel as the 5xx `error` field deprecation (see [`docs/BACKLOG.md`](../BACKLOG.md) "5xx field deprecation ticket"). Recommended bake window ≥ 30 days, gated on dashboards showing zero string-match references in client SDKs.
* **Discovery hint**: ship a deprecation warning helper that, when an SDK calls `parse_legacy_close_reason(...)`, logs a one-shot `acn.deprecated.legacy_close_reason_parsing` warning. SDK 0.5.x already has the infrastructure; 0.6.0 can flip the wire shape with the warning loud.

---

## 6. Risk assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| 123-byte close reason ceiling exceeded for some site | Medium | Pre-flight size check in the helper raises in dev; CI test asserts every emit is ≤123 bytes. The compact field-name encoding gives ~3× headroom for foreseeable details shapes (max we ship today: `{path_agent, key_agent}` ≈ 90 bytes including UUID). |
| Browser truncation if the helper bug ships | Medium | Helper raises, never silently truncates. CI gate. |
| SDK clients that string-match on legacy reason break overnight | High → Low (with announce) | 30-day deprecation window per BACKLOG schedule; deprecation warning helper. |
| Post-handshake error frame race (close arrives before client reads frame) | Low | Standard WebSocket pattern — Starlette/aiohttp/websockets all flush pending sends before close. Unit test pins ordering. |
| Future per-WS rate-limit ticket emits 4429 with a different reason vocabulary, fragmenting the dictionary | Low | The reason vocabulary is owned by `AUTHENTICATION_REQUIRED` (or future `WALLET_RATE_LIMIT_EXCEEDED`) which is already in `UNION_SCHEMA_CODES`; adding emitters is the existing pattern. |

---

## 7. Resolved review questions

All five resolved on 2026-05-03; no override of defaults requested.

* **Q1 (resolved: yes)** — The AST consistency check (`tests/test_error_code_details_consistency.py`) is extended to walk `_safe_close` calls. The cross-channel invariant ("same `ErrorCode` → same `details` keys regardless of HTTP or WS emitter") is exactly what the test was built to enforce, and the helper signature is stable enough to make the AST walker low-brittleness. Implementation note: the walker will treat `_safe_close(error_code=…, details=…)` keyword arguments the same way it treats `ACNHTTPError(...)`, with the helper's positional signature pinned by a unit test so future helper-API drift fails loud.
* **Q2 (resolved: defer)** — AsyncAPI schema generation is deferred to a separate ticket. ACN ships no AsyncAPI infrastructure today; coupling its bootstrap to #11b would balloon scope. The compact-shape contract is pinned in the new contract test file (`tests/routes/test_websocket_error_schema_protocol.py`), which is sufficient for SDK type-gen consumers in the interim.
* **Q3 (resolved: distinct codes 4401 / 4403)** — Site #4 splits cleanly: 4401 + `AUTHENTICATION_REQUIRED reason=invalid_api_key` for the token-not-found half (caller's key did not resolve to any agent), 4403 + `API_KEY_AGENT_MISMATCH` for the token-resolves-to-wrong-agent half. This matches the HTTP `/status` endpoint behaviour migrated under #11a and prevents an attacker from using a transport switch (HTTP vs WS) as a side-channel oracle to differentiate "key bad" from "key for wrong agent".
* **Q4 (resolved: SDK 0.6.0 major)** — The wire-shape change ships with SDK 0.6.0 (major break). 30-day bake window with deprecation-warning helper announced via the same channel as the 5xx `error` field deprecation (see [`docs/BACKLOG.md`](../BACKLOG.md)). Server implementation can land before the SDK release; legacy SDK 0.5.x clients continue to receive the old close-frame shape via a route-level feature flag for the bake window — see §5 migration plan for the flag mechanic.
* **Q5 (resolved: yes)** — The L197 runtime catch-all is extended to write a `websocket_error` log line tagged with the `request_id` (already in `request.state` for HTTP requests; the WS path needs a one-line addition to assign one at connection time). Operators correlate opaque 1011 closes with internal stack traces by `request_id`, mirroring the same pattern HTTP 5xx already uses.

---

## 8. Acceptance criteria (resolved 2026-05-03)

- [x] Reviewers ack §3 recommendations (D1c, D2b, D3a) — all accepted as-drafted.
- [x] Open questions §7-Q1..Q5 each resolved (defaults accepted, no overrides).
- [x] SDK release-note plan agreed — Q4 → SDK 0.6.0 (major), 30-day bake with deprecation warning helper.
- [x] BACKLOG.md row #11b flipped from "RFC required" to "ready for implementation" with the agreed scope.

Implementation lands as a single sprint #11b PR (estimated ≈ 2-4 days including SDK coordination and the deprecation-warning shipment). Implementation entry point: `acn/routes/websocket.py` `_safe_close` helper signature change, then the 4 site migrations + new contract test file.

---

## 9. Out of scope, recapped

* Per-WS rate limiting (separate ticket).
* AsyncAPI schema generation (separate ticket).
* Server-pushed business errors content alignment (opportunistic — folded into #11b only if low-cost).
* slowapi 429 realignment (covered by `WALLET_RATE_LIMIT_EXCEEDED` BACKLOG entry).

---

## 10. Cross-references

* HTTP error contract — [`acn-error-schema.md`](./acn-error-schema.md)
* Sprint #11a (websocket HTTP routes) — `acn-error-schema.md` §2 *Websocket HTTP routes*, footnote `[^11a]`
* M14 security audit (WS auth paths) — `acn/routes/websocket.py` docstring + `tests/routes/test_websocket_auth_m14.py`
* Existing application-frame error type — `acn/infrastructure/messaging/websocket_manager.py` `MessageType.ERROR`
