# Assistant Events

Python-authoritative event storage + notification delivery for the assistant stack.

## Why it exists

Event producers report facts to one service instead of speaking Telegram directly:

```text
Proxmox / calendar / email / future agents
                  |
                  v
          assistant-events
           |           |
           v           v
        SQLite     notification policy
                        |
                        v
                     Telegram
```

Qwen is not the source of truth for system events. A real connector/controller captures the fact first; the model may later summarize or reason about that captured data.

## Start

From the repository root:

```bash
./events.sh start
```

On first start, `events.sh` creates `assistant/events/.env` with a random `EVENTS_API_KEY`. The real `.env` stays outside Git.

The service reuses `TELEGRAM_BOT_TOKEN` from `assistant/telegram/.env`; the token is not copied into another file. If exactly one Telegram user is allowlisted, that user's ID is also used as the private Telegram chat ID. For groups or multiple allowlisted users, set `TELEGRAM_CHAT_ID` explicitly in `assistant/events/.env`.

The human-facing assistant name comes from `assistant/config/identity.toml`, not from a hard-coded service name.

## Test

```bash
./events.sh test
```

That creates a real event and should send a Telegram message similar to:

```text
ℹ️ Jarvis · INFO · INFO

Outbound notifications are working
Hello Jef. The event system delivered this without asking Qwen to do anything.

Source: manual-test · Target: telegram · Agent: main
```

## Useful commands

```bash
./events.sh status
./events.sh logs
./events.sh stop
```

## API

The service binds to localhost only:

```text
http://127.0.0.1:8780
```

`GET /health` is unauthenticated and localhost-bound. Event/history endpoints require:

```text
Authorization: Bearer <EVENTS_API_KEY>
```

### Minimal event

```json
{
  "source": "proxmox",
  "event_type": "guest_down",
  "title": "Guest is down",
  "message": "VM 104 is stopped."
}
```

Optional future-agent provenance is already supported:

```json
{
  "actor_id": "proxmox-monitor",
  "agent_id": "homelab",
  "correlation_id": "diagnostic-run-123"
}
```

`agent_id` is metadata only. It grants no permissions.
