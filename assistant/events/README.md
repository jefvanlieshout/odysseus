# Assistant Events

Python-authoritative event storage, state-aware notification delivery, and reminders.

## Event path

```text
verified producer (later: Proxmox / calendar / email)
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

Qwen is not the source of truth for system events. A connector/controller captures a real fact first. Qwen may later explain it or request a reminder through a validated tool.

## State-aware anti-spam

There is no blanket time cooldown anymore.

- first/new condition -> notify
- unchanged repeated condition -> store, suppress duplicate notification
- severity increase -> notify
- producer-supplied `notification_key` changes -> notify
- recovery from an active condition -> notify
- every occurrence is still stored in SQLite

`fingerprint` identifies the underlying condition. `notification_key` identifies a meaningful sub-state of that condition. Dynamic metrics/duration should go in message/metadata without changing the key.

## Reminders

Reminders are persistent in SQLite. They can be unconditional or tied to an active condition fingerprint.

A conditional reminder fires only when that condition is still `active` when it becomes due. If the monitor has already reported recovery, the reminder is skipped.

The API accepts either `delay_seconds` or an absolute ISO-8601 `due_at`. This is intentionally ready for a Qwen tool later: Qwen interprets natural language, Python validates and persists the concrete schedule.

## Start / test

```bash
./events.sh start
./events.sh test
./events.sh test       # repeat: stored, but Telegram should stay quiet
./events.sh remind 10 "This is a ten-second reminder test"
./events.sh reminders
```

## API

Localhost only: `http://127.0.0.1:8780`

Authenticated endpoints require `Authorization: Bearer <EVENTS_API_KEY>`.

- `POST /events`
- `GET /events`
- `POST /reminders`
- `GET /reminders`
- `POST /reminders/{id}/cancel`

`agent_id` is provenance only; it grants zero permissions.
