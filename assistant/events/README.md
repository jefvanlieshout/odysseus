# Assistant Events v0.1.0

Small, Python-authoritative event + notification sidecar for the local assistant stack.

## Design rules

- Event producers report facts; they do not send Telegram messages directly.
- The event service stores the event **before** attempting delivery.
- Qwen is not required for V0.1 and is not trusted as the source of truth.
- Notifications are sinks. Telegram is only the first sink.
- Assistant display name is configuration (`ASSISTANT_NAME`), not an architectural identity.
- `agent_id` is optional metadata now so future sub-agents do not require an event-schema rewrite.
- Repeated events are deduplicated by fingerprint + cooldown.
- Recovery notifications only fire after a matching active condition.

## Event flow

```text
monitor / controller / future agent
             |
             | POST /events
             v
      assistant-events
        |          |
        |          +--> SQLite event history
        |
        +--> notification policy
                  |
                  +--> Telegram sink (V0.1)
                  +--> Discord sink  (future)
                  +--> desktop sink  (future)
```

## Install next to Odysseus

Expected layout:

```text
~/odysseus/
├── docker-compose.yml
├── .env
├── jarvis-brain/
└── assistant-events/
    ├── app.py
    ├── Dockerfile
    ├── requirements.txt
    ├── docker-compose.events.yml
    ├── .env
    └── test_event.sh
```

Copy `.env.example` to `.env`:

```bash
cd ~/odysseus/assistant-events
cp .env.example .env
nano .env
```

Generate the service API key:

```bash
openssl rand -hex 32
```

Put that result in `EVENTS_API_KEY`.

For `TELEGRAM_CHAT_ID`, send `/whoami` to the existing Telegram bridge and copy the **Telegram chat ID** it reports. Reuse the same `TELEGRAM_BOT_TOKEN` that the bridge already uses. This service sends outbound Bot API calls only; it does not call `getUpdates` and therefore does not become a second Telegram poller.

## Start

From `~/odysseus`:

```bash
docker compose \
  -f docker-compose.yml \
  -f jarvis-brain/docker-compose.brain.yml \
  -f assistant-events/docker-compose.events.yml \
  up -d --build assistant-events
```

Health check:

```bash
curl http://127.0.0.1:8780/health
```

Expected shape:

```json
{
  "ok": true,
  "service": "assistant-events",
  "assistant_name": "Jarvis",
  "schema_version": 1,
  "enabled_channels": ["telegram"],
  "database": "healthy"
}
```

## First real notification test

Load the API key into your shell without printing it:

```bash
cd ~/odysseus/assistant-events
set -a
source .env
set +a
./test_event.sh
```

Telegram should receive:

```text
ℹ️ Jarvis · INFO · INFO

Outbound notifications are working
Hello Jef. The new event system delivered this without asking Qwen to do anything.

Source: manual-test · Target: telegram · Agent: main
```

## Test deduplication

This example becomes an active fault and uses a 10-minute cooldown:

```bash
curl --fail-with-body -sS \
  -H "Authorization: Bearer ${EVENTS_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{
    "source": "proxmox",
    "event_type": "guest_down",
    "severity": "warning",
    "state": "active",
    "title": "Immich guest is down",
    "message": "Proxmox reports that guest 104 is stopped.",
    "target": "vm:104",
    "actor_id": "proxmox-monitor",
    "fingerprint": "proxmox:guest_down:104",
    "cooldown_seconds": 600
  }' \
  http://127.0.0.1:8780/events
```

Run it twice. The first event should be delivered. The second should still be stored in SQLite but return `notification_status: "suppressed"` until the cooldown expires.

## Recovery event

```bash
curl --fail-with-body -sS \
  -H "Authorization: Bearer ${EVENTS_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{
    "source": "proxmox",
    "event_type": "guest_down",
    "severity": "info",
    "state": "recovered",
    "title": "Immich guest recovered",
    "message": "Proxmox reports that guest 104 is running again.",
    "target": "vm:104",
    "actor_id": "proxmox-monitor",
    "fingerprint": "proxmox:guest_down:104"
  }' \
  http://127.0.0.1:8780/events
```

A recovery notification is only emitted when the same fingerprint was previously `active`.

## Inspect stored events

```bash
curl --fail-with-body -sS \
  -H "Authorization: Bearer ${EVENTS_API_KEY}" \
  'http://127.0.0.1:8780/events?limit=20'
```

## Event contract

Minimal event:

```json
{
  "source": "proxmox",
  "event_type": "guest_down",
  "title": "Guest is down",
  "message": "VM 104 is stopped."
}
```

Useful future-ready fields:

```json
{
  "actor_id": "proxmox-monitor",
  "agent_id": "homelab",
  "correlation_id": "diagnostic-run-123",
  "target": "vm:104",
  "metadata": {
    "node": "pve",
    "vmid": 104
  }
}
```

`agent_id` is informational only. It grants **zero permissions**. When sub-agents arrive later, controller permissions should remain authoritative.

## Why this does not live in Telegram

Telegram already serves as a chat bridge to Odysseus. Keeping events separate prevents this architecture:

```text
Proxmox -> Telegram-specific code
Calendar -> Telegram-specific code
Email -> Telegram-specific code
```

Instead every producer speaks one event contract and the notification layer decides where messages go.

## Why Qwen is not in V0.1

The first job is reliable plumbing:

```text
real condition -> Python event -> stored fact -> notification
```

Later, Qwen can classify or summarize events *after* Python has captured the real source data. It should never be able to invent that a VM stopped and make that event authoritative.

## Next step

The natural V0.2 is a read-only Proxmox monitor/connector that emits events such as:

- guest `running -> stopped`
- guest `stopped -> running` recovery
- node offline/online
- storage threshold warnings

That connector should use the same event API; no changes to Telegram should be needed.
