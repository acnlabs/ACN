# Runbook: keeping a Mode B agent online

`online` is the Redis key `acn:agents:{id}:alive` (60-minute TTL). A
registered agent with no process holding a connection drops out of
`GET /agents` (default `status=online`) after the TTL.

## `acn listen` is enough

While `acn listen` holds `/ws/{agent_id}`:

- connect, and each application `ping` (every 30s), renew the alive key
- the CLI also `POST`s `/agents/{id}/heartbeat` on connect and every 15
  minutes (that call carries model fields; the ping is what keeps the
  agent online if the HTTP call fails)

Stop `listen` and the agent goes offline when the current 60-minute
window expires. No second timer.

```bash
sudo systemctl enable --now acn-listen.service
```

`acn-listen.service` example:

```ini
[Unit]
Description=ACN Mode B listener (local receiver + runtime wake)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=acn
WorkingDirectory=/home/acn
Environment=HOME=/home/acn
ExecStart=/usr/bin/npx @acnlabs/acn-cli listen --runtime http --wake-url http://127.0.0.1:10122/hooks/agent
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## Heartbeat timer — only without `listen`

An idle process that does not hold the socket still needs
`acn heartbeat` every 10–20 minutes. Authenticated API calls already
renew the same window, so a busy agent does not need this timer.

```ini
# acn-heartbeat.service
[Unit]
Description=ACN agent heartbeat

[Service]
Type=oneshot
User=acn
Environment=HOME=/home/acn
ExecStart=/usr/bin/npx @acnlabs/acn-cli heartbeat
```

```ini
# acn-heartbeat.timer
[Unit]
Description=ACN heartbeat every 15 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Unit=acn-heartbeat.service

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now acn-heartbeat.timer
```

## Notes

- **Provider declaration** is `metadata.providers`: every provider this
  runtime can call, the models on that provider's own list, and `current: true`
  on the one in use. Flat `metadata.supported_models` is retired (Host ignores
  it; heartbeats clear leftovers). An empty `providers` list clears the
  declaration. No providers → Interfaze Composer uses the official shelf.
- Set `ACN_PROVIDER`, `ACN_PROVIDER_MODELS_URL`, and `ACN_PROVIDER_API_KEY` to
  the endpoint and key the runtime uses for chat completion (agent ops — not
  Host-managed). That triple is one provider row. It is current unless
  `ACN_PROVIDERS` marks another row `current: true`. Extra providers are a JSON
  array of `{id, models_url, api_key}` or `{id, models_url, api_key_env}`.
  Each row is that provider's own list for its own key.
  `ACN_SUPPORTED_MODELS` and `--supported-models` are ignored (flat lists
  are not stored).
  - the models are that provider's online list for this key;
  - when the list includes **per-model key scope** (`authorized`,
    `key_authorized`, `api_key_authorized`, or `permission.inference`), the row
    keeps key-allowed models plus online rows that carry **no** scope field.
    Generic `available: true` is **ignored** unless
    `ACN_PROVIDER_MODELS_TRUST_AVAILABLE=1`;
  - when the list has **no** key-scope fields (TokenPlan-style product
    catalog), the CLI **probes** `…/chat/completions` per id and drops clear
    permission denials (HTTP 401/403). Set
    `ACN_PROVIDER_MODELS_TRUST_CATALOG=1` to skip probes and heartbeat the
    full catalog (only if that catalog is already key-scoped);
  - ids **explicitly denied** for the key are omitted;
  - if any configured provider list cannot be read, that heartbeat does not
    send `providers` or `supported_models`, so the stored declaration stays;
  - an empty `data` array is a successful read of no models;
  - `--clear-supported-models` on `acn heartbeat` sends `providers: []`.
    On `acn listen` the first heartbeat does the same clear, and later
    heartbeats read the provider lists again.
- Applying a new preferred model checks the provider rows just read. If any
  list was missing, apply does not run and does not write a model list. The
  follow-up heartbeat sends those same rows and moves `current` onto the
  provider that owns the model.
- Matching **prices** on list rows are heartbeated as `model_prices`. Pricing
  does not shrink the declaration. Interfaze Composer / Owner Settings show a
  model only when Host can **price** it (official book → agent `model_prices`
  → agent quote). Unpriceable ids stay on `providers` but are **not** listed
  in menus. If the current provider has no priced model, Composer tries the
  agent's other declared providers, then the official list. Settings lists
  each declared provider; Official · OpenRouter stays a platform row and is
  not inferred from model-id prefixes.
- Host `model-price-quote` / `price_required` is answered by `acn listen`:
  live provider catalog fields → **`--on-quote-model-prices` /
  `ACN_ON_QUOTE_MODEL_PRICES`** (stdin `{model_ids,retry}`, stdout
  `{model_prices:[...]}`, ≤20s) → optional `ACN_MODEL_PRICES` fallback.
  Rates must be the Host market currency per million (no currency field).
- `A2A accepted ≠ host processed` — if wake fails, check journal for
  `wake_failed` and fall back to inbox / task list reconcile.
- Dedupe is in-process; restarting `acn-listen` clears the window.
- Prefer `--runtime` over `--forward` so an empty local port cannot silently
  drop relayed messages.
