# Assistant-owned components

This directory contains project code that belongs to Jef's assistant rather than upstream Odysseus itself.

Current layout:
- `config/` — assistant identity/config that is safe to version
- `events/` — event/notification service (not automatically enabled by bootstrap)
- `telegram/` — imported Telegram bridge when detected during migration
- `connectors/` — controlled integrations such as Proxmox
- `tools/` — repository/update management helpers

Architectural rule: models reason/propose; the Python/controller layer is authoritative about permissions, external actions, and what actually happened.
