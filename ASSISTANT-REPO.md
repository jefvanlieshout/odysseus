# Assistant repository workflow

This Odysseus checkout is the single versioned repository for Jef's assistant stack **and, from v0.2.0, the base of our maintained Odysseus fork**.

## Current custom areas

- `jarvis-brain/` — authoritative memory service/integration
- `assistant/telegram/` — Telegram gateway
- `assistant/events/` — event/notification/reminder service
- `assistant/connectors/` — controlled integrations such as Proxmox
- `assistant/config/identity.toml` — stable internal ID + configurable display name
- `assistant/fork/` — fork-owned execution-context/tool-broker contracts
- `assistant/docs/` — architecture audit/design notes
- `assistant/upstream/` — upstream merge policy
- `assistant/tools/` — updater/package tooling
- `events.sh` — event service helper
- `fork.sh` — safe fork/remotes/status helper
- `upstream.sh` — isolated upstream sync helper
- `docker-compose.assistant.yml` — assistant-owned services overlay

## Updating our assistant

ChatGPT-produced updates remain self-contained packages:

```bash
./update.sh ~/Downloads/assistant-update-X.Y.Z.tar.gz
```

The updater validates hashes/paths, protects local secrets/runtime data, preserves tracked local edits in Git, creates a pre-update safety tag, applies only manifest-listed files, checks syntax, commits the update, and creates an `assistant-vX.Y.Z` tag.

## Fork preparation

After installing v0.2.0 once:

```bash
./fork.sh prepare
./fork.sh status
```

`prepare` makes the official Odysseus repository the `upstream` remote. It never merges and never pushes. If the current `origin` is the official repository, it is safely renamed to `upstream`.

When a private GitHub/Git fork exists:

```bash
./fork.sh attach-origin YOUR_PRIVATE_FORK_URL
./fork.sh push
```

## Upstream updates

Normal upstream base is curated `upstream/main`, not fast-moving `dev`.

```bash
./upstream.sh fetch
./upstream.sh prepare
```

The prepare command creates a temporary `assistant-upstream-sync-*` branch and merges upstream with `--no-commit`. Production remains untouched until review/tests and an explicit:

```bash
./upstream.sh accept
```

Use `./upstream.sh abort` to discard a sync attempt.

## Rollback

```bash
./rollback.sh
```

Rollback affects versioned code only. Databases/runtime state are deliberately not rewound implicitly.

## Secrets

Real `.env` files, SQLite databases, logs, caches, Brain data and private-key formats are ignored/rejected. Before publishing any fork, also follow upstream `SECURITY.md` fork-publishing checks.

## Architecture rule

**Models reason/propose. Python/controller decides permissions, performs external actions, owns schedules/persistence, and records what actually happened.**

Tool visibility is not authority. Agent identity is provenance, not permission.
