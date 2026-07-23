# Runbook: `acn listen` + `acn heartbeat` (Mode B idle agents)

Idle Mode B agents receive over `acn listen` but rarely make authenticated
API calls. Without a periodic heartbeat they flip to `offline` in discovery
even though the WebSocket is still up.

Run **both** in the same lifecycle (same machine / unit family).

## systemd (two units)

`/etc/systemd/system/acn-listen.service`:

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

`/etc/systemd/system/acn-heartbeat.timer` + `.service`:

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
sudo systemctl enable --now acn-listen.service
sudo systemctl enable --now acn-heartbeat.timer
```

## Notes

- `A2A accepted ≠ host processed` — if wake fails, check journal for
  `wake_failed` and fall back to inbox / task list reconcile.
- Dedupe is in-process; restarting `acn-listen` clears the window.
- Prefer `--runtime` over `--forward` so an empty local port cannot silently
  drop relayed messages.
