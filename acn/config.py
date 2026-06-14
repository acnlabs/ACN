"""
ACN Configuration

Settings for ACN service
"""

import json
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_pkg_version() -> str:
    try:
        return _pkg_version("acn")
    except PackageNotFoundError:
        return "unknown"

# Hosts allowed when ``dev_mode=True``. Deliberately *excludes* ``0.0.0.0``
# even though dev-mode docker-compose often binds it — letting dev_mode
# accept all-interfaces would let an operator who flipped DEV_MODE=true on
# a prod box quietly expose the auth-bypassed service to the public network.
# Forcing the bind host to also change before that's possible is a
# defence-in-depth on top of the dev_mode security checks below.
_DEV_MODE_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class Settings(BaseSettings):
    """ACN Settings"""

    # Service
    service_name: str = "ACN"
    service_version: str = _read_pkg_version()
    host: str = "0.0.0.0"
    port: int = 8000

    # Redis
    redis_url: str = "redis://localhost:6379"
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str | None = None

    # A2A Protocol
    a2a_protocol_version: str = "0.3.0"

    # Phase 2 review v2 P1 #10 — mode-switch SDK version warning.
    # When ``PATCH /agents/{id}/policy`` resolves to ``manifest`` or
    # ``allowlist`` mode, the response carries
    # ``X-ACN-SDK-Min-Version: <this value>``. Old SDKs without a
    # ``manifest_notification`` WS handler will silently miss every
    # subsequent inbound message — the header is the explicit warning
    # gate so operators / clients catch the implicit breaking change
    # before agents go "deaf".
    #
    # Default is forward-looking — bumped each time the ACN python
    # client adds a contractually-required handler. Must match (or
    # be ≤) the lowest published client version that implements both
    # ``manifest_notification`` and ``policy_changed`` events.
    # Ops can override per-deployment via ``POLICY_MANIFEST_MIN_SDK_VERSION``
    # without a code rebuild — useful for staging fleets pinned to
    # an older client during a phased rollout.
    policy_manifest_min_sdk_version: str = "0.5.0"

    # Gateway
    gateway_base_url: str = "https://api.acnlabs.dev"

    # Frontend base URL — used for human-facing links (e.g. claim pages)
    # Defaults to gateway_base_url if not set
    frontend_base_url: str | None = None

    # Backend URL (for escrow and other integrations)
    backend_url: str = "http://localhost:8000"

    # Set to False to run ACN without payment settlement (e.g. self-hosted deployments
    # that do not connect to Agent Planet's Backend). When disabled, tasks still work
    # but Escrow lock/release calls are skipped entirely and a warning is logged.
    escrow_enabled: bool = True

    # ACN revenue wallet ID in Backend (wallet_type=PLATFORM, label=acn_revenue).
    # Backend's release/release_partial endpoints split fees and credit this wallet.
    # Must be pre-created in Backend's wallets table before enabling fee collection.
    acn_revenue_wallet_id: str | None = None

    # Internal API Token (shared with Backend for service-to-service auth).
    # MUST be set via env var (INTERNAL_API_TOKEN). No code default is
    # provided — the validator below rejects empty/short tokens regardless
    # of dev_mode, eliminating the historical "open-source default password"
    # foot-gun.
    internal_api_token: str | None = None

    # Webhooks (for backend integration)
    webhook_url: str | None = None  # e.g., "https://your-backend.com/api/acn/webhook"
    webhook_secret: str | None = None  # For HMAC signature verification
    webhook_timeout: int = 30  # seconds
    webhook_retry_count: int = 3
    webhook_retry_delay: int = 5  # seconds

    # Durable webhook outbox (ADR-0009 C7 / ACN #162): after in-process retries
    # are exhausted, the delivery is parked in Redis and re-driven by a background
    # worker until delivered or aged out — giving at-least-once delivery across
    # process restarts. P0 reconciliation (seller fulfillment-queue poll) remains
    # the correctness backstop; this only shortens the window the seller relies on it.
    webhook_outbox_enabled: bool = True
    webhook_outbox_poll_interval: int = 5  # seconds between worker sweeps
    webhook_outbox_max_age_seconds: int = 86400  # give up (dead-letter) after 24h
    webhook_outbox_max_backoff: int = 600  # cap per-attempt backoff at 10 min

    # Billing webhook
    billing_webhook_url: str | None = None  # e.g., "https://your-backend.com/api/billing/webhook"

    # Auth0 (for JWT verification and Agent Card security scheme)
    auth0_domain: str | None = (
        None  # e.g., "your-tenant.auth0.com" or "https://your-tenant.auth0.com"
    )
    auth0_audience: str | None = None  # e.g., "https://api.agentplanet.com"

    @field_validator("auth0_domain", mode="before")
    @classmethod
    def normalize_auth0_domain(cls, v: str | None) -> str | None:
        """Ensure auth0_domain always has https:// prefix when set."""
        if v is None:
            return v
        v = v.strip()
        if v and not v.startswith(("https://", "http://")):
            v = f"https://{v}"
        return v.rstrip("/")

    # Additional human identity providers, beyond Auth0 (issue: pluggable
    # OIDC issuer registry). Lets a region (e.g. China) verify human users
    # against a domestic, self-issued OIDC IdP instead of Auth0 — without
    # giving up the offline RS256 JWKS model. Auth0 (``auth0_domain`` /
    # ``auth0_audience``) remains the implicit default provider and is
    # verified exactly as before; this registry is purely *additive*.
    #
    # JSON array of objects (parsed in ``auth/middleware.py``):
    #   [
    #     {
    #       "name": "cn-wechat",                 # audit label
    #       "issuer": "https://api.acnlabs.cn/u",# exact ``iss`` claim value
    #       "audience": "https://api.acnlabs.cn",# expected ``aud``
    #       "jwks_url": "https://api.acnlabs.cn/u/.well-known/jwks.json",
    #       "jwks": "{...}",                     # OR pre-seeded JWKS JSON (offline)
    #       "sub_prefix": "wechat|"              # optional: pin sub namespace
    #     }
    #   ]
    human_oidc_providers_json: str | None = None

    # ------------------------------------------------------------------
    # Agent JWT issuance (ADR-0007) — ACN as the agent identity authority.
    # ------------------------------------------------------------------
    # ACN mints short-lived RS256 JWTs that resource servers (AgentPlanet
    # backend, future consensus/feed) verify offline against the JWKS
    # published at ``/.well-known/jwks.json``. The agent's long-lived
    # ``acn_*`` API key is the client credential exchanged at
    # ``POST /oauth/token`` (OAuth2 client_credentials) for these JWTs.
    #
    # ``AGENT_JWT_PRIVATE_KEY`` is a PEM-encoded RSA private key. When it
    # is unset the issuer is disabled: ``/oauth/token`` returns 503 and
    # the JWKS is empty. The public half (for JWKS) is derived from the
    # private key at runtime — no second env var to keep in sync.
    agent_jwt_private_key: str | None = None
    agent_jwt_kid: str = "acn-agent-key-1"
    # Key rotation window (#154 / ADR-0007 D2 "overlapping kids"). A second
    # PEM key that is published in JWKS for *verification only* — it is never
    # used to mint. This enables zero-downtime rotation:
    #   1. Pre-publish the NEW key here (JWKS now serves both kids); wait for
    #      relying-party JWKS caches to pick it up.
    #   2. Promote: make the new key the primary above (it now mints), move the
    #      OLD key into this secondary slot (old tokens still verify); wait one
    #      max-TTL window for old tokens to drain.
    #   3. Clear this secondary slot — rotation complete.
    # The two kids MUST differ. See docs/operations-agent-jwt-key-rotation.md.
    agent_jwt_private_key_secondary: str | None = None
    agent_jwt_kid_secondary: str = "acn-agent-key-2"
    # Issuer (``iss`` claim). Defaults to ``gateway_base_url`` when unset.
    agent_jwt_issuer: str | None = None
    # Default audience (``aud`` claim) when the token request does not
    # specify one. Canonical AgentPlanet backend audience (ADR-0008). The
    # backend dual-accepts the legacy https://api.agenticplanet.space during
    # the migration window, so clients still requesting it keep working.
    agent_jwt_audience: str = "https://api.agentplanet.org"
    agent_jwt_ttl_seconds: int = 1800  # 30 min; short TTL limits no-denylist exposure (ADR-0007 D8)
    # Scopes granted by default to every agent (ADR-0007 D3). Capability
    # scopes that move money for others (e.g. ``wallet:write``) are kept
    # OUT of the default and must be granted explicitly. ``store:sell`` is
    # safe to default: it only lets an agent collect to its OWN wallet.
    agent_jwt_default_scope: str = "acn:read acn:write store:sell"

    # PostgreSQL (for future persistent storage)
    database_url: str | None = None

    # CORS
    cors_origins: list[str] = ["*"]

    # Reverse-proxy IPs whose X-Forwarded-For / X-Real-IP headers we trust.
    # Empty list means "untrusted environment": ignore forwarded headers,
    # rate-limit on the immediate peer IP. Set to e.g.
    # TRUSTED_PROXIES=["10.0.0.1","10.0.0.2"] when ACN sits behind a known
    # set of L7 proxies/load-balancers. Mis-trusting allows clients to
    # spoof XFF and bypass per-IP rate limits — see C1a security audit.
    trusted_proxies: list[str] = []

    # Observability
    log_level: str = "INFO"
    otel_enabled: bool = False  # Enable OpenTelemetry (requires opentelemetry-sdk)

    # Development mode (disables Auth0 requirement for some endpoints)
    dev_mode: bool = False  # Set to True for local development (DEV_MODE=true)

    # API docs (Swagger UI / ReDoc / openapi.json)
    # Independent of dev_mode — operators can expose docs on staging while using prod auth
    enable_docs: bool = False  # Set to True for local development (ENABLE_DOCS=true)

    # WebSocket limits
    max_websocket_connections: int = 10_000

    # WebSocket auth: allow API key in the URL query string?
    # Security audit M14: an API key in ``?token=...`` ends up in:
    #   - server access logs (Nginx, Cloudflare, ALB),
    #   - Referer headers when the WS handshake is initiated from a page,
    #   - browser history / shoulder-surfable URL bars.
    # The recommended path is a one-shot first-message auth handshake or
    # the Authorization header. We keep the query-string path available
    # but make it OFF by default in production. ``dev_mode=True`` flips
    # this to True automatically (see validator below) so dev rigs that
    # paste a token in the URL keep working.
    websocket_allow_query_token: bool = False

    # Request body size cap (security audit H6).
    # Hard ceiling enforced by BodySizeLimitMiddleware before the request
    # reaches Pydantic. 1 MiB is enough for any legitimate JSON payload we
    # currently accept (the largest is task submission ~50 KB); raise this
    # only after auditing every endpoint that consumes large dict fields
    # (message, metadata, ui_spec, agent_card).
    max_request_body_size: int = 1_048_576  # 1 MiB

    # Anti-spam / join controls
    # Max registrations from one IP per day (endpoint-less agents are cheaper to spam)
    join_daily_limit_no_endpoint: int = 5  # IP/day — agents without an A2A endpoint
    join_daily_limit_with_endpoint: int = 20  # IP/day — agents with a real endpoint

    # Labs features (experimental)
    labs_onboarding_enabled: bool = True  # Agent self-onboarding experiment

    # ERC-8004 On-Chain Identity
    erc8004_enabled: bool = True
    erc8004_rpc_url: str = "https://mainnet.base.org"
    erc8004_chain_id: int = 8453  # Base mainnet; use 84532 for Base Sepolia
    # Mainnet contracts (same address on Base / Ethereum / Arbitrum / Polygon / etc.)
    erc8004_identity_contract: str = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
    erc8004_reputation_contract: str = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"
    # Testnet contracts (Base Sepolia / Arbitrum Sepolia / etc.)
    erc8004_identity_contract_testnet: str = "0x8004A818BFB912233c491871b3d84c89A494BD9e"
    erc8004_reputation_contract_testnet: str = "0x8004B663056A597Dffe9eCcC1965A193B7388713"
    # Validation Registry — experimental, addresses not yet published in ERC-8004 README.
    # Set via ERC8004_VALIDATION_CONTRACT env var when the address becomes available.
    erc8004_validation_contract: str | None = None
    erc8004_validation_contract_testnet: str | None = None

    # ------------------------------------------------------------------
    # Settlement Saga (v0.1) — see acn/docs/_drafts/settlement-saga-design.md
    # ------------------------------------------------------------------
    # Env var: OUTBOX_ENQUEUE_REQUIRED  (Pydantic upper-cases the field;
    # ``case_sensitive=False`` in ``model_config`` accepts either case).
    #
    # When True, ``task_service.complete_task`` runs in saga mode:
    # CAS save on ``tasks`` and INSERT on ``settlement_outbox`` execute
    # in a single ACID transaction. An enqueue failure rolls back the
    # state transition and bubbles up as HTTP 500.
    #
    # When False, ``complete_task`` short-circuits to the legacy
    # non-atomic path: CAS save commits independently in its own
    # transaction, NO outbox row is enqueued, and payment + reward
    # release runs synchronously exactly like pre-v0.1 production.
    # Use this ONLY as an emergency lever — production should always
    # run with True. The flag exists so an on-call operator can disarm
    # the new write path without a redeploy if a regression slips
    # through:
    #
    #     # Rollback drill (no source change):
    #     railway variables set OUTBOX_ENQUEUE_REQUIRED=false
    #     # ACN picks the new value up on next instance restart;
    #     # railway redeploy --no-cache forces it within ~30s.
    #
    # NB: SettlementWorker (below) is governed by its own flag
    # (``settlement_worker_enabled``); together they form the v0.1
    # rollout control. See plan §2 + §6 for the matrix.
    outbox_enqueue_required: bool = True

    # ------------------------------------------------------------------
    # SettlementWorker (v0.1) — async consumer of ``settlement_outbox``.
    # ------------------------------------------------------------------
    # Env var: SETTLEMENT_WORKER_ENABLED
    #
    # ``False`` in the code default: even if everything else is wired
    # up (PG mode, outbox table present, ``OUTBOX_ENQUEUE_REQUIRED=true``)
    # the worker DOES NOT start unless an operator flips this ON.
    # This is the "safe by default" posture: a deploy that runs the
    # Alembic migration but isn't yet ready to process events will
    # accumulate ``state='pending'`` rows visibly rather than silently
    # firing side effects.
    #
    # Production is expected to set this to ``true`` alongside the
    # outbox migration; staging / smoke envs that need to verify the
    # plumbing should set ``SETTLEMENT_WORKER_ENABLED=true`` explicitly.
    # Flipping back to ``false`` is a soft emergency stop — events
    # keep landing in the outbox but no settlement happens until the
    # worker comes back up. For a *full* emergency disarm that also
    # routes ``complete_task`` through the legacy synchronous path,
    # set ``OUTBOX_ENQUEUE_REQUIRED=false`` as well.
    settlement_worker_enabled: bool = False

    # Env var: SETTLEMENT_POLL_INTERVAL_SEC
    # How often the worker polls ``claim_batch`` when the previous
    # batch was empty. Low enough that completions feel "instant" to
    # the user (1 s); high enough that idle workers don't hammer the
    # DB. Bump to e.g. 5 s if the deployment is connection-pool
    # constrained.
    settlement_poll_interval_sec: float = 1.0

    # Env var: SETTLEMENT_BATCH_SIZE
    # Max rows claimed in one round of the polling loop. 10 keeps the
    # worker latency-bounded under load — a single slow step can hold
    # a batch but not the whole table. Raise if you see ``pending``
    # building up faster than the worker can drain.
    settlement_batch_size: int = 10

    # Env var: SETTLEMENT_MAX_ATTEMPTS
    # After this many failed retries (each with exponential backoff),
    # an event is moved to ``state='dead'`` and the DLQ alert fires.
    # 12 attempts × backoff 2..900 s ≈ ~80 minutes of self-healing
    # before human eyes are needed.
    settlement_max_attempts: int = 12

    # Env var: SETTLEMENT_BACKOFF_BASE_SEC / SETTLEMENT_BACKOFF_MAX_SEC
    # Exponential backoff: ``min(base * 2^attempts, max)`` for each
    # retry. Defaults: 2 → 4 → 8 → 16 → ... → 900 s (15 min, the cap).
    # The cap exists so a transient outage doesn't drag retries out
    # to multi-hour intervals — once a single retry waits ≥ 15 min,
    # we're already in "alert engineer" territory.
    settlement_backoff_base_sec: float = 2.0
    settlement_backoff_max_sec: float = 900.0

    # Env var: SETTLEMENT_JANITOR_INTERVAL_SEC / SETTLEMENT_JANITOR_STUCK_THRESHOLD_SEC
    # Janitor loop that resurrects rows stuck in ``state='paying'``
    # (worker crashed mid-step). Default: every 30 s, reset rows
    # whose ``updated_at`` is older than 5 min. The 5 min threshold
    # is intentionally well above the expected step latency (a few
    # hundred ms each) so we never racewith a healthy worker that's
    # just slow on one IO call.
    settlement_janitor_interval_sec: float = 30.0
    settlement_janitor_stuck_threshold_sec: float = 300.0

    # Env var: SETTLEMENT_DLQ_ALERT_WEBHOOK
    # Optional generic HTTP POST endpoint receiving ``{event_id,
    # task_id, last_error, attempts}`` as JSON whenever an event
    # transitions to ``state='dead'``. Slack / Discord / PagerDuty
    # incoming-webhook URLs are all valid shapes. ``None`` (default)
    # disables the alert path — operators must monitor
    # ``acn_settlement_outbox_dead_count`` via Prometheus instead.
    settlement_dlq_alert_webhook: str | None = None

    # ------------------------------------------------------------------
    # SettlementReconciler (v0.1) — daily cross-check between
    # ``settlement_outbox`` and ``reputation_events``.
    # ------------------------------------------------------------------
    # Env var: SETTLEMENT_RECONCILER_ENABLED
    #
    # When True (default), the API lifespan starts a background loop
    # that periodically runs ``SettlementReconciler.run_once`` and
    # publishes ``acn_settlement_reconcile_delta``. It's safe to
    # leave on in production because the loop performs only two
    # SELECT COUNT queries — far less load than the saga worker.
    # The flag exists so chaos / smoke envs can disable the reconciler
    # while exercising deliberately broken saga paths that would
    # otherwise spam the divergence log.
    settlement_reconciler_enabled: bool = True

    # Env var: SETTLEMENT_RECONCILE_INTERVAL_SEC
    # How often the reconciler loop wakes up. Default: 24h (one
    # window per day). The interval doubles as the trailing window
    # width — each run covers ``[now - interval, now]`` so they
    # tile back-to-back with zero gap and no double-counting.
    settlement_reconcile_interval_sec: int = 86_400

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @model_validator(mode="after")
    def validate_security_settings(self) -> "Settings":
        """Fail fast on unsafe configuration.

        Defenses are deliberately decoupled from ``dev_mode`` so that a
        single misconfigured flag cannot turn off everything at once
        (this was the pre-launch "dev_mode single point of failure"
        finding from the security audit):

        - Internal API token: required and >= 32 chars *always*. Removes
          the open-source default-password foot-gun.
        - CORS ``["*"]``: only tolerated when ``dev_mode=True``.
        - Auth0: required when ``dev_mode=False``.
        - ``dev_mode=True`` must bind to a loopback interface (``localhost``
          / ``127.0.0.1`` / ``::1``). Refusing to bind on a public interface
          while dev-mode auth bypasses are active makes "I left DEV_MODE=true
          on the prod box" physically impossible.
        """
        errors: list[str] = []

        if not self.internal_api_token:
            errors.append(
                "INTERNAL_API_TOKEN must be set. Generate one with "
                '`python -c "import secrets; print(secrets.token_urlsafe(32))"` '
                "and put it in your .env (no code default is provided)."
            )
        elif len(self.internal_api_token) < 32:
            errors.append(
                f"INTERNAL_API_TOKEN must be at least 32 characters "
                f"(current length: {len(self.internal_api_token)})."
            )

        if not self.dev_mode and self.cors_origins == ["*"]:
            errors.append(
                "CORS_ORIGINS must not be ['*'] when DEV_MODE=false. "
                "Set it to the list of allowed origins."
            )

        if (
            not self.dev_mode
            and (not self.auth0_domain or not self.auth0_audience)
            and not self.human_oidc_providers_json
        ):
            errors.append(
                "Configure human identity verification when DEV_MODE=false: set "
                "AUTH0_DOMAIN + AUTH0_AUDIENCE, and/or HUMAN_OIDC_PROVIDERS_JSON "
                "(a region may run with only a domestic OIDC provider)."
            )

        # Validate the human OIDC provider registry. A non-empty but
        # unparseable / all-invalid value would otherwise leave the gateway
        # "configured" yet 503-ing every human login — fail loud at boot
        # instead (security review hardening, below-threshold but cheap).
        if self.human_oidc_providers_json:
            valid_providers: list[dict] = []
            try:
                items = json.loads(self.human_oidc_providers_json)
                if not isinstance(items, list):
                    raise ValueError("must be a JSON array")
            except Exception as exc:  # noqa: BLE001
                items = None
                errors.append(f"HUMAN_OIDC_PROVIDERS_JSON is not valid JSON: {exc}")
            if items is not None:
                for idx, item in enumerate(items):
                    if (
                        not isinstance(item, dict)
                        or not item.get("issuer")
                        or not item.get("audience")
                        or not (item.get("jwks_url") or item.get("jwks"))
                    ):
                        errors.append(
                            f"HUMAN_OIDC_PROVIDERS_JSON[{idx}] needs issuer, audience "
                            "and jwks_url or jwks."
                        )
                    else:
                        valid_providers.append(item)
                if not valid_providers:
                    errors.append(
                        "HUMAN_OIDC_PROVIDERS_JSON is set but has no valid provider entries."
                    )
                auth0_iss = self.auth0_domain.rstrip("/") if self.auth0_domain else None
                # ACN-issued agent JWTs are routed by ``iss`` *before* the human
                # path; a registry provider sharing that issuer would be dead
                # config (agent path wins) — reject so misconfig fails loud.
                agent_iss = (self.agent_jwt_issuer or self.gateway_base_url or "").rstrip("/") or None
                for item in valid_providers:
                    iss = str(item["issuer"]).rstrip("/")
                    # A registry issuer that shadows Auth0 would intercept Auth0
                    # traffic with no fallback — reject it outright.
                    if auth0_iss and iss == auth0_iss:
                        errors.append(
                            f"HUMAN_OIDC_PROVIDERS_JSON issuer {iss!r} collides with "
                            "AUTH0_DOMAIN; a registry provider must not shadow Auth0."
                        )
                    if agent_iss and iss == agent_iss:
                        errors.append(
                            f"HUMAN_OIDC_PROVIDERS_JSON issuer {iss!r} collides with the "
                            "ACN agent JWT issuer; a human provider must not shadow it."
                        )
                    # When Auth0 users share the owner namespace, a registry
                    # provider without sub_prefix could mint colliding subs.
                    if auth0_iss and not item.get("sub_prefix"):
                        errors.append(
                            f"HUMAN_OIDC_PROVIDERS_JSON[{item.get('name', iss)}] must set "
                            "sub_prefix when AUTH0 is also configured (owner namespace isolation)."
                        )

        # M14: in dev mode we always allow the query-token WS auth path,
        # regardless of any explicit env setting. Reasoning: the only
        # reason to *disable* query tokens is "production access logs
        # are leaking the key" — irrelevant on a developer laptop.
        # Pinning ``True`` here means existing dev clients that paste
        # tokens in URLs keep working without one more env var to set,
        # and it cannot accidentally tighten in dev.
        if self.dev_mode:
            self.websocket_allow_query_token = True

        if self.dev_mode and self.host not in _DEV_MODE_ALLOWED_HOSTS:
            errors.append(
                f"DEV_MODE=true refuses to bind to non-loopback host {self.host!r}. "
                f"Allowed: {sorted(_DEV_MODE_ALLOWED_HOSTS)}. "
                "If you need to expose the service on a public interface, "
                "set DEV_MODE=false (and configure Auth0 + a non-'*' CORS origin)."
            )

        if errors:
            raise ValueError(
                "Security configuration errors detected:\n" + "\n".join(f"  - {e}" for e in errors)
            )

        return self


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance"""
    return Settings()
