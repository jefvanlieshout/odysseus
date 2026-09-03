#!/usr/bin/env bash
set -euo pipefail

OFFICIAL_URL="https://github.com/odysseus-dev/odysseus.git"
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "$ROOT" && -f "$ROOT/ASSISTANT_VERSION" ]] || {
  echo "Run fork.sh from inside the consolidated assistant/Odysseus repository." >&2
  exit 2
}
cd "$ROOT"

normalize_url() {
  python3 - "$1" <<'PY'
import re, sys
s = sys.argv[1].strip()
s = re.sub(r'^git@github\.com:', 'https://github.com/', s)
s = re.sub(r'^ssh://git@github\.com/', 'https://github.com/', s)
s = s.rstrip('/')
if s.endswith('.git'):
    s = s[:-4]
print(s.lower())
PY
}

official_remote() {
  local url="${1:-}"
  [[ -n "$url" ]] || return 1
  [[ "$(normalize_url "$url")" == "$(normalize_url "$OFFICIAL_URL")" ]]
}

remote_url() {
  git remote get-url "$1" 2>/dev/null || true
}

ensure_no_git_operation() {
  local gitdir
  gitdir="$(git rev-parse --git-dir)"
  for marker in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD REBASE_HEAD; do
    if [[ -e "$gitdir/$marker" ]]; then
      echo "A Git merge/rebase/cherry-pick/revert is already in progress. Finish or abort it first." >&2
      exit 1
    fi
  done
  if [[ -d "$gitdir/rebase-merge" || -d "$gitdir/rebase-apply" ]]; then
    echo "A Git rebase is already in progress. Finish or abort it first." >&2
    exit 1
  fi
}

ensure_tracked_clean() {
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Tracked Git changes are present. Commit them (or use the assistant updater) before changing fork remotes." >&2
    git status --short >&2
    exit 1
  fi
}

ensure_upstream_remote() {
  local origin upstream
  origin="$(remote_url origin)"
  upstream="$(remote_url upstream)"

  if [[ -n "$upstream" ]]; then
    if ! official_remote "$upstream"; then
      echo "Refusing to overwrite existing non-official 'upstream' remote: $upstream" >&2
      exit 1
    fi
    return 0
  fi

  if [[ -n "$origin" ]] && official_remote "$origin"; then
    echo "Renaming official Odysseus remote: origin -> upstream"
    git remote rename origin upstream
  else
    echo "Adding official Odysseus remote as 'upstream'."
    git remote add upstream "$OFFICIAL_URL"
  fi
}

fetch_upstream() {
  echo "Fetching curated upstream/main and fast-moving upstream/dev..."
  if ! git fetch upstream main dev --tags --prune; then
    echo "WARNING: upstream remote is configured, but fetch failed. Your local fork is still valid; retry later with ./fork.sh fetch." >&2
    return 1
  fi
}

write_state() {
  local gitdir state head branch main dev mergebase
  gitdir="$(git rev-parse --git-dir)"
  state="$gitdir/assistant-fork-state.json"
  head="$(git rev-parse HEAD)"
  branch="$(git branch --show-current)"
  main="$(git rev-parse upstream/main 2>/dev/null || true)"
  dev="$(git rev-parse upstream/dev 2>/dev/null || true)"
  mergebase="$(git merge-base HEAD upstream/main 2>/dev/null || true)"
  python3 - "$state" "$head" "$branch" "$main" "$dev" "$mergebase" <<'PY'
from datetime import datetime, timezone
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
data = {
    "recorded_at": datetime.now(timezone.utc).isoformat(),
    "assistant_head": sys.argv[2],
    "assistant_branch": sys.argv[3],
    "upstream_main": sys.argv[4] or None,
    "upstream_dev": sys.argv[5] or None,
    "merge_base_with_upstream_main": sys.argv[6] or None,
}
p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY
}

show_status() {
  echo "Assistant fork status"
  echo "  branch: $(git branch --show-current)"
  echo "  HEAD:   $(git rev-parse --short=12 HEAD)"
  echo
  echo "Remotes:"
  git remote -v || true
  echo
  if git show-ref --verify --quiet refs/remotes/upstream/main; then
    local counts mergebase
    counts="$(git rev-list --left-right --count HEAD...upstream/main)"
    mergebase="$(git merge-base HEAD upstream/main 2>/dev/null || true)"
    echo "upstream/main: $(git rev-parse --short=12 upstream/main)"
    echo "merge-base:    ${mergebase:0:12}"
    echo "HEAD...upstream/main (ours / upstream-only): $counts"
  else
    echo "upstream/main has not been fetched yet. Run: ./fork.sh fetch"
  fi
  echo
  if git remote get-url origin >/dev/null 2>&1; then
    echo "Our fork remote (origin): $(git remote get-url origin)"
  else
    echo "Our fork remote (origin): not attached yet"
    echo "Attach later with: ./fork.sh attach-origin YOUR_PRIVATE_FORK_URL"
  fi
}

attach_origin() {
  local url="${1:-}"
  [[ -n "$url" ]] || { echo "Usage: ./fork.sh attach-origin YOUR_FORK_URL" >&2; exit 2; }
  if official_remote "$url"; then
    echo "That is the official Odysseus repository. It belongs under 'upstream', not 'origin'." >&2
    exit 1
  fi
  if git remote get-url origin >/dev/null 2>&1; then
    local current
    current="$(git remote get-url origin)"
    if [[ "$(normalize_url "$current")" == "$(normalize_url "$url")" ]]; then
      echo "origin is already attached to $current"
      return 0
    fi
    echo "Refusing to replace existing origin automatically: $current" >&2
    echo "If that remote is obsolete, remove/rename it manually after inspecting it." >&2
    exit 1
  fi
  git remote add origin "$url"
  echo "Attached our fork as origin: $url"
  echo "Nothing was pushed automatically. Use './fork.sh push' when you want the off-machine backup."
}

push_fork() {
  git remote get-url origin >/dev/null 2>&1 || {
    echo "No origin is configured. First run: ./fork.sh attach-origin YOUR_PRIVATE_FORK_URL" >&2
    exit 1
  }
  local branch
  branch="$(git branch --show-current)"
  [[ -n "$branch" ]] || { echo "Detached HEAD; refusing to push." >&2; exit 1; }
  echo "Pushing $branch and assistant tags to origin..."
  git push -u origin "$branch"
  git push origin --tags
}

self_test() {
  python3 "$ROOT/assistant/fork/selftest.py"
}

case "${1:-}" in
  prepare)
    ensure_no_git_operation
    ensure_tracked_clean
    ensure_upstream_remote
    fetch_upstream || true
    write_state
    echo
    echo "✓ Local Odysseus fork foundation is prepared."
    echo "✓ Official repository is tracked as: upstream"
    echo "✓ Your production branch remains: $(git branch --show-current)"
    echo "✓ No upstream code was merged and nothing was pushed."
    echo
    show_status
    ;;
  fetch)
    ensure_upstream_remote
    fetch_upstream
    write_state
    ;;
  status)
    show_status
    ;;
  attach-origin)
    attach_origin "${2:-}"
    ;;
  push)
    push_fork
    ;;
  self-test|selftest)
    self_test
    ;;
  *)
    cat <<'HELP'
Usage: ./fork.sh COMMAND

Commands:
  prepare                  Configure official Odysseus as upstream and fetch main/dev
  status                   Show branch/remotes/upstream divergence
  fetch                    Refresh upstream/main + upstream/dev without merging
  attach-origin URL        Attach your own private GitHub/Git remote as origin
  push                     Push current branch + tags to your origin
  self-test                Test the assistant fork/tool-broker contracts

Safety:
  - prepare never merges upstream code
  - prepare never pushes anywhere
  - a non-official existing upstream/origin is never overwritten silently
  - tracked local edits must be committed first
HELP
    exit 2
    ;;
esac
