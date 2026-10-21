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

- **`supported_models` heartbeat:** set `ACN_PROVIDER_MODELS_URL` and
  `ACN_PROVIDER_API_KEY` to the **same provider endpoint and key** the agent
  runtime uses for chat completion (agent ops — not Host-managed):
  - when the list includes **per-model key scope** (`authorized`,
    `key_authorized`, `api_key_authorized`, or `permission.inference`), heartbeat
    **key-allowed** rows that also appear on the runtime allowlist, plus
    **allowlisted** rows that are online but carry **no** scope field; generic
    `available: true` is **ignored** unless
    `ACN_PROVIDER_MODELS_TRUST_AVAILABLE=1`;
  - ids **explicitly denied** for the key are never heartbeated, even if they
    remain on `listen --supported-models` / `ACN_SUPPORTED_MODELS`;
  - when **no** row exposes key scope, heartbeat **runtime allowlist** **∩**
    online catalog — never the full product catalog;
  - matching **prices** on list rows are heartbeated as `model_prices` when
    Host price book (OpenRouter / TokenPlus) does not already cover the id.
  Interfaze Composer still shows only models Host can **price** (book → agent
  `model_prices` → agent quote); unpriceable ids are omitted from `model_options`.
- `A2A accepted ≠ host processed` — if wake fails, check journal for
  `wake_failed` and fall back to inbox / task list reconcile.
- Dedupe is in-process; restarting `acn-listen` clears the window.
- Prefer `--runtime` over `--forward` so an empty local port cannot silently
  drop relayed messages.
