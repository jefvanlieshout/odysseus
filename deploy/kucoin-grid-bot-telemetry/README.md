# KuCoin grid-bot telemetry sidecar

Read-only telemetry bridge for Atlas/Odysseus.

- Reads `/etc/kucoin-grid-bot/config.yaml` and the configured `state_file`.
- Never talks to KuCoin.
- Never writes the bot's state or config.
- Writes only its own derived history database under `/var/lib/kucoin-grid-bot-telemetry/history.sqlite3`.
- Reads fixed `kucoin-grid-bot.service` systemd status for liveness (no arbitrary command execution).
- Exposes GET-only endpoints: `/status`, `/performance`, `/activity?window=day|week|month|all`, `/diagnostics`.
- Requires `Authorization: Bearer <token>`.
- Uses the bot's persisted anchor price; no live market price is fetched.

The deployment commands in the v0.4.6 handoff install this inside the bot LXC and create a random read-only bearer token.

`state_fresh` describes the accounting file modification time only; bot liveness is reported separately from systemd.

## Install through the Proxmox host

From the Odysseus repository on your workstation:

```bash
./deploy/kucoin-grid-bot-telemetry/install-proxmox.sh root@PROXMOX_HOST 109
```

The installer is idempotent: it preserves an existing telemetry token, replaces only the sidecar files/service, creates a separate writable history directory, and never edits the trading bot's config or state.
