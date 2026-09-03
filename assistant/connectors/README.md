# Connectors

External-system connectors live here. They expose controlled Python/controller APIs; models do not receive raw credentials or direct unrestricted access.

Planned examples:
- Proxmox
- Home Assistant
- calendar/email adapters

Future agents should call the same controller-owned connectors via an execution context; connectors must not be hard-coded to one assistant name.
