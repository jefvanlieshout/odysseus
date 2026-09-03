#!/usr/bin/env bash
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$ROOT" ]]; then
  echo "Run update.sh from inside the assistant/Odysseus Git repository." >&2
  exit 2
fi
exec python3 "$ROOT/assistant/tools/updater" "$@"
