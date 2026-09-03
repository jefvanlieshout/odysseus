# Assistant repository workflow

This Odysseus checkout is also the single versioned repository for Jef's assistant stack.

## Current custom areas

- `jarvis-brain/` — memory Brain integration
- `assistant/telegram/` — Telegram bridge
- `assistant/events/` — event/notification service
- `assistant/connectors/` — controlled integrations such as Proxmox
- `assistant/config/identity.toml` — stable internal ID + configurable display name
- `assistant/tools/` — updater/package tooling
- `events.sh` — event service setup/start/test/status/log helper
- `docker-compose.assistant.yml` — assistant-owned services overlay

## Updating

Future ChatGPT-produced updates are self-contained `assistant-update-*.tar.gz` packages.

```bash
./update.sh ~/Downloads/assistant-update-X.Y.Z.tar.gz
```

The updater validates package hashes/paths, protects local secrets/runtime data, preserves tracked local edits in Git, creates a pre-update safety tag, applies only manifest-listed files, checks syntax, commits the update, and creates an `assistant-vX.Y.Z` tag.

## Rollback

```bash
./rollback.sh
```

Rollback affects versioned code only. Databases and runtime data are deliberately not rewound implicitly.

## Events / notifications

```bash
./events.sh start
./events.sh test
```

`assistant-events` binds to `127.0.0.1:8780`. It stores events before notification attempts, applies cooldown/recovery policy, and currently uses Telegram as its first notification sink. The Telegram bot token is reused from the existing Telegram bridge `.env`; it is not duplicated into Git or another tracked config file.

## Git backup remote

Add a private remote when desired:

```bash
git remote add assistant-backup YOUR_PRIVATE_REPO_URL
git push -u assistant-backup assistant-main --tags
```

The existing Odysseus upstream remote is left untouched.

## Secrets

Real `.env` files, SQLite databases, logs, caches, Brain data and private-key formats are ignored/rejected. `.env.example` files are safe to version; actual credentials stay local.

## Upstream Odysseus

Do not blindly pull an upstream development branch into `assistant-main`. Treat upstream changes as code changes that should be inspected/merged and tested against the Brain/assistant integration.

## Notification policy (v0.1.3)

Notifications use state-aware duplicate suppression rather than a blanket time cooldown. Persistent reminders may be unconditional or tied to an active condition fingerprint.
