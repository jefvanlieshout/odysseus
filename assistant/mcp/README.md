# Assistant MCP tools

This directory contains thin MCP adapters that let Odysseus/Qwen request
controller-owned capabilities without giving the model direct access to the
underlying service.

## Reminder bridge

`reminders_server.py` exposes three tools:

- `assistant_create_reminder`
- `assistant_list_reminders`
- `assistant_cancel_reminder`

The MCP process does **not** schedule timers and does not send Telegram messages.
It forwards validated structured requests to `assistant-events`, which owns the
SQLite reminder record, due-time processing, conditional state check, and final
delivery.

The installed runtime copy lives under Odysseus's persistent `/app/data` mount
so the MCP subprocess can be started by Odysseus without patching its core
source tree. `./reminders.sh install` refreshes that runtime copy and MCP config.
