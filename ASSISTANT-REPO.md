# Assistant repository workflow

This Odysseus checkout is also the single versioned repository for Jef's assistant stack.

## Why this layout

Odysseus already has Git history. Wrapping it in another Git repository would create nested-repository problems, so custom assistant components are versioned in this checkout instead.

Current custom areas:

- `jarvis-brain/` — memory Brain integration (kept in its working location for compatibility)
- `assistant/telegram/` — Telegram bridge when imported by bootstrap
- `assistant/events/` — event/notification service; bootstrap installs the code but does **not** enable it automatically
- `assistant/connectors/` — future controlled integrations such as Proxmox
- `assistant/config/identity.toml` — stable internal ID + configurable display name
- `assistant/tools/` — updater/package tooling

## Updating

Future ChatGPT-produced updates are self-contained `assistant-update-*.tar.gz` packages.

From anywhere inside this repository:

```bash
./update.sh ~/Downloads/assistant-update-X.Y.Z.tar.gz
```

The updater:

1. validates archive paths and SHA-256 hashes;
2. refuses to overwrite `.env`, databases, private keys, or runtime data;
3. stores any current code edits as a local Git backup commit;
4. creates a pre-update Git safety tag;
5. applies only the files listed by the manifest;
6. syntax-checks touched Python/shell/JSON/TOML files;
7. commits the update and creates an `assistant-vX.Y.Z` tag;
8. can run only a small allowlist of declared deployment actions (Docker Compose / localhost health check), never arbitrary update-package shell commands.

Use `--no-deploy` to apply code without declared deployment actions.

## Rollback

```bash
./rollback.sh
```

Rollback affects versioned code only. Databases and runtime data are deliberately not rewound implicitly.

## Git backup remote

The bootstrap does not guess where your private backup repository lives. Add one later, for example:

```bash
git remote add assistant-backup YOUR_PRIVATE_REPO_URL
git push -u assistant-backup assistant-main --tags
```

Your existing Odysseus upstream remote is left untouched.

## Secrets

Real `.env` files, SQLite databases, logs, caches, Brain data and private-key formats are ignored/rejected. Keep `.env.example` files in Git; keep actual credentials local.

## Upstream Odysseus

Do not blindly `git pull` an upstream development branch into `assistant-main`. Treat upstream updates as code changes that should be inspected/merged and then tested against the Brain integration. The assistant updater does not pull upstream automatically.
