# Assistant-owned components

This directory contains project code that belongs to Jef's assistant/fork rather than generic upstream Odysseus.

Current layout:

- `config/` — versioned assistant identity/config
- `events/` — Python-authoritative event storage, state-aware notifications, persistent reminders
- `telegram/` — Telegram chat gateway
- `connectors/` — controlled integrations such as Proxmox
- `fork/` — fork-owned execution context + ToolBroker contracts
- `docs/` — audit and design documents
- `upstream/` — safe upstream maintenance policy
- `tools/` — repository/update management helpers

Root helpers:

```bash
./events.sh start
./events.sh test
./fork.sh prepare
./fork.sh status
./fork.sh self-test
./upstream.sh status
```

Architectural rule: models reason/propose; the Python/controller layer is authoritative about permissions, external actions, schedules, and what actually happened.

## v0.2.0 fork foundation

v0.2.0 deliberately does not rewrite the live Odysseus agent loop yet. It establishes a maintainable fork and a tested ToolBroker contract first. The current reminder MCP bridge remains available as an experimental adapter, but native controller reminders are planned for the first core fork patch.

Read:

- `docs/ODYSSEUS-AUDIT-2026-09-03.md`
- `docs/TOOL-BROKER-DESIGN.md`
