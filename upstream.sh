#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "$ROOT" && -f "$ROOT/ASSISTANT_VERSION" ]] || {
  echo "Run upstream.sh from inside the consolidated assistant/Odysseus repository." >&2
  exit 2
}
cd "$ROOT"
PROD_BRANCH="assistant-main"

require_upstream() {
  git remote get-url upstream >/dev/null 2>&1 || {
    echo "No upstream remote. Run: ./fork.sh prepare" >&2
    exit 1
  }
}

require_clean() {
  if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    echo "Tracked tree is not clean. Commit/update it before preparing an upstream sync." >&2
    git status --short >&2
    exit 1
  fi
}

sync_branch_name() {
  date -u '+assistant-upstream-sync-%Y%m%dT%H%M%SZ'
}

status() {
  require_upstream
  "$ROOT/fork.sh" status
}

prepare_sync() {
  require_upstream
  require_clean
  [[ "$(git branch --show-current)" == "$PROD_BRANCH" ]] || {
    echo "Start upstream preparation from $PROD_BRANCH, not $(git branch --show-current)." >&2
    exit 1
  }
  git fetch upstream main --tags --prune
  local branch
  branch="$(sync_branch_name)"
  git switch -c "$branch"
  echo "Created isolated sync branch: $branch"
  echo "Merging upstream/main WITHOUT committing or touching production..."
  if ! git merge --no-ff --no-commit upstream/main; then
    echo
    echo "Merge conflicts found. Production branch '$PROD_BRANCH' is untouched." >&2
    echo "Inspect/resolve on '$branch', or run: ./upstream.sh abort" >&2
    exit 1
  fi
  echo
  python3 "$ROOT/assistant/fork/selftest.py"
  echo
  echo "✓ Upstream/main merged cleanly into the temporary sync branch."
  echo "✓ Basic assistant fork tests passed."
  echo "Nothing has been committed or promoted yet. Review with:"
  echo "  git status"
  echo "  git diff --cached --stat"
  echo "Then use: ./upstream.sh accept"
}

accept_sync() {
  local branch
  branch="$(git branch --show-current)"
  [[ "$branch" == assistant-upstream-sync-* ]] || {
    echo "accept must run from an assistant-upstream-sync-* branch." >&2
    exit 1
  }
  if [[ ! -f "$(git rev-parse --git-dir)/MERGE_HEAD" ]]; then
    echo "No pending upstream merge found on this sync branch." >&2
    exit 1
  fi
  if git ls-files -u | grep -q .; then
    echo "Unresolved merge conflicts remain." >&2
    exit 1
  fi
  python3 "$ROOT/assistant/fork/selftest.py"
  git commit -m "Merge upstream Odysseus main into assistant fork"
  local merged_commit
  merged_commit="$(git rev-parse HEAD)"
  git switch "$PROD_BRANCH"
  git merge --ff-only "$merged_commit"
  echo "✓ Upstream sync promoted to $PROD_BRANCH after tests."
  echo "Rebuild/deploy is intentionally a separate explicit step."
}

abort_sync() {
  local branch
  branch="$(git branch --show-current)"
  [[ "$branch" == assistant-upstream-sync-* ]] || {
    echo "abort must run from an assistant-upstream-sync-* branch." >&2
    exit 1
  }
  if [[ -f "$(git rev-parse --git-dir)/MERGE_HEAD" ]]; then
    git merge --abort
  fi
  git switch "$PROD_BRANCH"
  git branch -D "$branch"
  echo "Aborted upstream sync; production branch was left untouched."
}

case "${1:-}" in
  status) status ;;
  fetch) require_upstream; git fetch upstream main dev --tags --prune ;;
  prepare) prepare_sync ;;
  accept) accept_sync ;;
  abort) abort_sync ;;
  *)
    cat <<'HELP'
Usage: ./upstream.sh COMMAND

Commands:
  status     Show upstream divergence
  fetch      Fetch upstream/main and upstream/dev only
  prepare    Create an isolated sync branch and merge upstream/main --no-commit
  accept     Test, commit, and fast-forward assistant-main to the reviewed sync
  abort      Abort/delete the current temporary sync branch

This intentionally never auto-deploys an upstream merge.
HELP
    exit 2
    ;;
esac
