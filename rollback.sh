#!/usr/bin/env bash
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$ROOT" ]]; then
  echo "Run rollback.sh from inside the assistant repository." >&2
  exit 2
fi
cd "$ROOT"
TARGET="${1:-}"
if [[ -z "$TARGET" ]]; then
  TARGET="$(git tag --list 'assistant-pre-*' --sort=-creatordate | head -n 1)"
fi
if [[ -z "$TARGET" ]]; then
  echo "No assistant-pre-* safety tag exists yet." >&2
  exit 2
fi
if ! git rev-parse --verify --quiet "$TARGET^{commit}" >/dev/null; then
  echo "Unknown rollback target: $TARGET" >&2
  exit 2
fi
CURRENT="$(git rev-parse --short HEAD)"
echo "Current commit: $CURRENT"
echo "Rollback target: $TARGET ($(git rev-parse --short "$TARGET"))"
echo "This rolls back VERSIONED CODE only. It does not rewind databases/runtime data."
if [[ "${2:-}" != "--yes" ]]; then
  read -r -p "Type ROLLBACK to continue: " answer
  [[ "$answer" == "ROLLBACK" ]] || { echo "Cancelled."; exit 1; }
fi
if [[ -n "$(git status --porcelain)" ]]; then
  git add -A
  git -c user.name="Assistant Updater" -c user.email="assistant-updater@local" commit -m "Local backup before rollback" || true
fi
git reset --hard "$TARGET"
echo "Rolled back code to $TARGET. Rebuild/restart affected services as appropriate."
