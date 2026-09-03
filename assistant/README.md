# Assistant-owned components

This directory contains project code that belongs to Jef's assistant rather than upstream Odysseus itself.

Current layout:
- `config/` — versioned assistant identity/config (`internal_id` is stable; display name is changeable)
- `events/` — Python-authoritative event storage, state-aware notifications, persistent reminders
- `telegram/` — Telegram chat bridge
- `connectors/` — controlled integrations such as Proxmox
- `tools/` — repository/update management helpers

Root helper commands:

```bash
./events.sh start
./events.sh test
./events.sh remind 10 "test reminder"
./events.sh reminders
./events.sh status
```

Architectural rule: models reason/propose; the Python/controller layer is authoritative about permissions, external actions, schedules, and what actually happened.
