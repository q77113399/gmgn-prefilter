#!/usr/bin/env bash
# Commit staged-able data changes in the repo ($GMGN_GHA_REPO or cwd) and push
# with retries. Used by GitHub Actions (state only) and box-sync.sh (inputs+state).
#   INCLUDE_INPUTS=1     also stage data/inputs (box is the only writer of inputs)
# Conflicts: pull --rebase with the union JSON merge driver; if that still fails or
# leaves invalid JSON, fall back to reset onto origin + union-merge our state.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${GMGN_GHA_REPO:-$(pwd)}"
MSG="${1:-state update}"
BRANCH="${GMGN_GHA_BRANCH:-main}"
cd "$REPO" || exit 1
git config merge.gmgn-union.name "gmgn state union"
git config merge.gmgn-union.driver "python3 scripts/state_tool.py merge-driver %O %A %B %P"

stage() {
  git add -A data/state
  [[ "${INCLUDE_INPUTS:-0}" == 1 ]] && git add -A data/inputs
  return 0
}
stage
if git diff --cached --quiet; then echo "git-push-state: nothing to commit"; exit 0; fi
if ! bash "$SCRIPT_DIR/secret-check.sh" "$REPO"; then git reset -q; exit 1; fi
git commit -q -m "$MSG" || { echo "git-push-state: commit failed"; exit 1; }

for attempt in 1 2 3 4; do
  if git push -q origin "HEAD:$BRANCH"; then echo "git-push-state: pushed (attempt $attempt)"; exit 0; fi
  echo "git-push-state: push rejected, pull --rebase (attempt $attempt)"
  if git pull -q --rebase origin "$BRANCH" && python3 scripts/state_tool.py validate data; then
    continue
  fi
  git rebase --abort 2>/dev/null || true
  echo "git-push-state: rebase failed; union fallback"
  tmp=$(mktemp -d); cp -r data "$tmp/"
  git fetch -q origin "$BRANCH" && git reset -q --hard "origin/$BRANCH" || { sleep 5; continue; }
  python3 scripts/state_tool.py merge "$tmp/data/state" data/state
  [[ "${INCLUDE_INPUTS:-0}" == 1 ]] && cp "$tmp"/data/inputs/*.json data/inputs/
  rm -rf "$tmp"
  stage
  if git diff --cached --quiet; then echo "git-push-state: nothing left after merge"; exit 0; fi
  bash "$SCRIPT_DIR/secret-check.sh" "$REPO" || { git reset -q; exit 1; }
  git commit -q -m "$MSG (union merge)"
  sleep $(( attempt * 3 ))
done
echo "git-push-state: giving up after 4 attempts"; exit 1
