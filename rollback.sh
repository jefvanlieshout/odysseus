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

pre_staged_new="$(git diff --cached --name-only --diff-filter=A)"
if [[ -n "$pre_staged_new" ]]; then
  echo "Refusing to auto-commit newly staged files before rollback." >&2
  echo "Commit or unstage these first:" >&2
  printf '%s\n' "$pre_staged_new" >&2
  exit 1
fi

git add -u
if ! git diff --cached --quiet; then
  git -c user.name="Assistant Updater" -c user.email="assistant-updater@local" \
    commit -m "Local backup before rollback"
fi

untracked_count="$(git ls-files --others --exclude-standard | wc -l)"
if [[ "$untracked_count" -gt 0 ]]; then
  echo "Leaving $untracked_count unrelated untracked file(s) untouched."
fi

git reset --hard "$TARGET"
echo "Rolled back code to $TARGET. Rebuild/restart affected services as appropriate."
