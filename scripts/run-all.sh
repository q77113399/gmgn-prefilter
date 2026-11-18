#!/usr/bin/env bash
# One prefilter round: strict candidates, then positions, then the bounty watch (Pano3). Shared by the box and
# GitHub Actions. Skips the whole round if ANY runner (box or GHA) started one
# within GMGN_MIN_GAP_SEC (default 720 = 12 min), per $STATE_DIR/last-run.json.
# FORCE=1 ignores the gap. DRY_RUN=1 never POSTs.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export QUOTA_SAVE_ROOT="${QUOTA_SAVE_ROOT:-/workspace/quota-save}"
STATE_DIR="${QUOTA_SAVE_STATE_DIR:-$QUOTA_SAVE_ROOT/state}"
MIN_GAP="${GMGN_MIN_GAP_SEC:-720}"
if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then RUNNER="${GMGN_RUNNER:-github-actions}"; else RUNNER="${GMGN_RUNNER:-box}"; fi
export GMGN_RUNNER="$RUNNER"
LAST="$STATE_DIR/last-run.json"

if [[ "${FORCE:-0}" != 1 ]] && python3 "$SCRIPT_DIR/state_tool.py" recent "$LAST" "$MIN_GAP"; then
  echo "$(date '+%F %T %Z') run-all skip: a run started < ${MIN_GAP}s ago ($RUNNER)"
  exit 0
fi
python3 "$SCRIPT_DIR/state_tool.py" stamp "$LAST" "$RUNNER"
echo "$(date '+%F %T %Z') run-all start runner=$RUNNER dry_run=${DRY_RUN:-0} skip_candidates=${SKIP_GMGN_CANDIDATES:-1}"
# GMGN steps never see Pano3's bounty webhook vars.
# 2026-11-19: pause new-token candidate wakes in favour of the smart-money follow
# watcher (task_id gmgn-smart-money). Default SKIP_GMGN_CANDIDATES=1 so the
# already-running box loop-gha.sh picks this up on the next tick without a
# restart, and so GitHub Actions skips after this file is pushed. Re-enable
# candidate wakes with SKIP_GMGN_CANDIDATES=0. Position wakes stay on.
if [[ "${SKIP_GMGN_CANDIDATES:-1}" != "0" ]]; then
  echo "$(date '+%F %T %Z') skip gmgn-candidates (SKIP_GMGN_CANDIDATES=${SKIP_GMGN_CANDIDATES:-1})"
else
  env -u BOUNTY_WEBHOOK_URL -u BOUNTY_WEBHOOK_BEARER GMGN_PREFILTER_MODE=strict \
    bash "$SCRIPT_DIR/gmgn-prefilter.sh" || echo "candidates wrapper exit=$?"
fi
env -u BOUNTY_WEBHOOK_URL -u BOUNTY_WEBHOOK_BEARER \
  bash "$SCRIPT_DIR/gmgn-positions.sh" || echo "positions wrapper exit=$?"
# Independent bounty step (wakes Pano3 via BOUNTY_WEBHOOK_*; never GMGN's WEBHOOK_*).
# A failure here never affects the GMGN steps above. State: $STATE_DIR/bounty.
if [[ "${BOUNTY_ENABLED:-1}" == 1 && -f "$SCRIPT_DIR/bounty-watch.sh" ]]; then
  echo "$(date '+%F %T %Z') bounty start"
  env -u WEBHOOK_URL -u WEBHOOK_BEARER BOUNTY_STATE_DIR="${BOUNTY_STATE_DIR:-$STATE_DIR/bounty}" \
    timeout "${BOUNTY_TIMEOUT_SEC:-300}" bash "$SCRIPT_DIR/bounty-watch.sh" 2>&1 || echo "bounty wrapper exit=$?"
fi
echo "$(date '+%F %T %Z') run-all done"
exit 0
