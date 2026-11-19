#!/usr/bin/env bash
# GitHub Actions wrapper for the early-launch CANDIDATE scan (task_id gmgn-early).
# Positions / early.json stay box-only: here early.json is a throwaway default in
# runner temp. Public dedupe: data/state/early-seen-gha.json (mints + timestamps).
# Webhook URL/bearer come from repo secrets via env and are never printed; only the
# HTTP status code is logged. DRY_RUN=1: print the summary, never POST.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
export GMGN_EARLY_SOURCE=gha
export GMGN_GHA_SEEN_PATH="$REPO/data/state/early-seen-gha.json"
export GMGN_NARRATIVES_PATH="$REPO/data/inputs/narratives.json"
export GMGN_REJECTED_PATH="$REPO/data/inputs/rejected.json"
: "${QUOTA_SAVE_STATE_DIR:?set to runner temp}"
: "${QUOTA_SAVE_LOG_DIR:?set to runner temp}"
export QUOTA_SAVE_ROOT="${QUOTA_SAVE_ROOT:-$REPO}"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/lib_notify.sh"
mkdir -p "$QUOTA_SAVE_STATE_DIR" "$QUOTA_SAVE_LOG_DIR"
[[ -f "$GMGN_GHA_SEEN_PATH" ]] || echo '{"mints": {}}' > "$GMGN_GHA_SEEN_PATH"
cp "$GMGN_GHA_SEEN_PATH" "$QUOTA_SAVE_STATE_DIR/early-seen-gha.before.json"

payload="$(python3 "$SCRIPT_DIR/gmgn-early.py" || true)"
ts=$(date '+%F %T %Z')
if [[ -z "${payload//[$'\t\r\n ']}" ]]; then echo "$ts gha-early: no wake"; exit 0; fi
if printf '%s' "$payload" | jq -e '.dry_run == true' >/dev/null 2>&1; then
  printf '%s\n' "$payload"; exit 0
fi
if ! printf '%s' "$payload" | jq -e '.task_id == "gmgn-early"' >/dev/null 2>&1; then
  echo "$ts gha-early: unexpected payload task_id; not sent"
  cp "$QUOTA_SAVE_STATE_DIR/early-seen-gha.before.json" "$GMGN_GHA_SEEN_PATH"; exit 0
fi
echo "$ts gha-early WAKE source=gha: $(printf '%s' "$payload" | jq -c '[.metrics.candidates[] | {symbol, mint, mc: (.mc|floor), age_min}]')"
if notify "$payload"; then
  echo "$ts gha-early: wake sent"
else
  echo "$ts gha-early: wake NOT sent; restoring dedupe so next run retries"
  cp "$QUOTA_SAVE_STATE_DIR/early-seen-gha.before.json" "$GMGN_GHA_SEEN_PATH"
fi
exit 0
