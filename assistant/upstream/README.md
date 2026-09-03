# Upstream Odysseus policy

Our fork uses two roles:

- `upstream` — official `odysseus-dev/odysseus`
- `origin` — Jef's private fork/backup when attached

Production branch: `assistant-main`.

Policy:

1. Track curated `upstream/main` for normal updates.
2. `upstream/dev` is observation/cherry-pick territory, not automatic production input.
3. Never merge upstream straight into `assistant-main`.
4. `./upstream.sh prepare` creates an isolated temporary branch and merges with `--no-commit`.
5. Tests/review happen before `./upstream.sh accept` can fast-forward production.
6. Rebuild/deploy remains separate, so a source merge cannot silently alter the running system.
7. Keep our fork-core changes small; put owned behavior under `assistant/` or focused extension modules and use narrow hooks in upstream files.
