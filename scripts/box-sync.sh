#!/usr/bin/env bash
# Box <-> GitHub repo sync for the GMGN prefilters. NOT wired into loop.sh yet
# (see loop-gha.sh). Usage:  box-sync.sh pre | post
#   pre : git pull --rebase the repo clone, then union-merge data/state into the
#         box state dir (so the box sees dedupe marks / last-run from GitHub runs).
#   post: copy the 5 box inputs into data/inputs, union-merge box state into
#         data/state, secret-check, commit + push if anything changed.
# No remote configured -> logs one line and exits 0 (no-op).
# Never prints secrets; secret-check.sh aborts the push on any secret-looking string.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${GMGN_GHA_REPO:-$(cd "$SCRIPT_DIR/.." && pwd)}"
BOX_ROOT="${QUOTA_SAVE_ROOT:-/workspace/quota-save}"
BOX_STATE="${GMGN_BOX_STATE_DIR:-$BOX_ROOT/state}"
LOG="${GMGN_SYNC_LOG:-$BOX_ROOT/logs/box-sync.log}"
BRANCH="${GMGN_GHA_BRANCH:-main}"
# box source -> repo name (Pano edits these on the box; the box is their only writer)
SRC_NARRATIVES="${GMGN_BOX_NARRATIVES:-/workspace/gmgn-narratives.json}"
SRC_WATCH="${GMGN_BOX_WATCH:-/workspace/watch-gmgn-exit/watch.json}"
SRC_WATCH_STATE="${GMGN_BOX_WATCH_STATE:-/workspace/watch-gmgn-exit/state.json}"
SRC_REJECTED="${GMGN_BOX_REJECTED:-$BOX_STATE/rejected.json}"
SRC_POSITIONS="${GMGN_BOX_POSITIONS:-$BOX_STATE/positions.json}"

mkdir -p "$(dirname "$LOG")"
log() { printf '%s box-sync %s\n' "$(date '+%F %T %Z')" "$*" >> "$LOG"; }

mode="${1:-}"
[[ "$mode" == pre || "$mode" == post ]] || { echo "usage: $0 pre|post" >&2; exit 2; }
if ! git -C "$REPO" remote get-url origin >/dev/null 2>&1; then
  log "$mode: no remote configured in $REPO; no-op"
  exit 0
fi
cd "$REPO" || exit 1
# Serialize git ops with private_state.py box-sync (encrypted blob pushes).
exec 8>"$BOX_ROOT/state/gha-git.lock"; flock -w 120 8 || log "$mode: git lock wait timed out; continuing"
git config merge.gmgn-union.name "gmgn state union"
git config merge.gmgn-union.driver "python3 scripts/state_tool.py merge-driver %O %A %B %P"

if [[ "$mode" == pre ]]; then
  if timeout 60 git pull -q --rebase --autostash origin "$BRANCH" >/dev/null 2>&1; then
    log "pre: pulled $(git rev-parse --short HEAD)"
  else
    git rebase --abort >/dev/null 2>&1 || true
    log "pre: pull failed; using local copy"
  fi
  python3 scripts/state_tool.py merge data/state "$BOX_STATE" && log "pre: state merged into $BOX_STATE"
  exit 0
fi

# post
copy_in() { # src dst
  [[ -f "$1" ]] || { log "post: missing input $1"; return 0; }
  python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$1" 2>/dev/null || { log "post: invalid json $1 (skipped)"; return 0; }
  cmp -s "$1" "$2" || cp "$1" "$2"
}
mkdir -p data/inputs data/state
copy_in "$SRC_NARRATIVES"   data/inputs/narratives.json
copy_in "$SRC_WATCH"        data/inputs/watch.json
copy_in "$SRC_WATCH_STATE"  data/inputs/watch-state.json
copy_in "$SRC_REJECTED"     data/inputs/rejected.json
copy_in "$SRC_POSITIONS"    data/inputs/positions.json
python3 scripts/state_tool.py merge "$BOX_STATE" data/state
git config user.name  >/dev/null || git config user.name  "gmgn-box-sync"
git config user.email >/dev/null || git config user.email "gmgn-box-sync@users.noreply.github.com"
out="$(INCLUDE_INPUTS=1 GMGN_GHA_REPO="$REPO" GMGN_GHA_BRANCH="$BRANCH" timeout 120 \
  bash scripts/git-push-state.sh "box: sync $(date '+%F %T %Z')" 2>&1)"; rc=$?
log "post: rc=$rc $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-300)"
exit 0
