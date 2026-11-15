#!/usr/bin/env bash
# Thin wrapper: run the GMGN positions prefilter and notify only when it
# prints a payload. Never trades. DRY_RUN=1 prints the payload and skips POST.
# Runs on the box (defaults = box paths) or in GitHub Actions (paths via env).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$PATH:/home/box/.local/bin:/usr/local/bin:/usr/bin:/bin"
export QUOTA_SAVE_ROOT="${QUOTA_SAVE_ROOT:-/workspace/quota-save}"

# shellcheck source=lib_notify.sh
source "$SCRIPT_DIR/lib_notify.sh"
mkdir -p "$STATE_DIR" "$LOG_DIR"

PY="$SCRIPT_DIR/gmgn-positions.py"
# Back up dedupe state so a failed/skipped POST does not suppress the event for 6h.
DEDUPE_P="${GMGN_POSITIONS_DEDUPE:-$STATE_DIR/positions-dedupe.json}"
bk=$(mktemp -d)
[[ -f "$STATE_DIR/notify-dedupe.json" ]] && cp "$STATE_DIR/notify-dedupe.json" "$bk/notify-dedupe.json"
[[ -f "$DEDUPE_P" ]] && cp "$DEDUPE_P" "$bk/positions-dedupe.json"
restore_dedupe() {
  if [[ -f "$bk/notify-dedupe.json" ]]; then cp "$bk/notify-dedupe.json" "$STATE_DIR/notify-dedupe.json"; else rm -f "$STATE_DIR/notify-dedupe.json"; fi
  if [[ -f "$bk/positions-dedupe.json" ]]; then cp "$bk/positions-dedupe.json" "$DEDUPE_P"; else rm -f "$DEDUPE_P"; fi
}
payload="$(python3 "$PY" || true)"

if [[ -z "${payload//[$'\t\r\n ']}" ]]; then
  exit 0
fi

dedupe_key="$(printf '%s' "$payload" | jq -r '.dedupe_key // empty' 2>/dev/null || true)"
# Match the script's 6h per-condition window at the notify layer too.
if [[ -n "$dedupe_key" ]] && should_skip_dedupe "$dedupe_key" 21600; then
  ts=$(date '+%Y-%m-%d %H:%M:%S %Z')
  echo "$ts wrapper skip: notify-level 6h dedupe" >> "$LOG_DIR/gmgn-positions.log"
  exit 0
fi

if [[ "${DRY_RUN:-0}" == 1 ]]; then notify "$payload"; restore_dedupe; exit 0; fi
status=0; notify "$payload" || status=$?
if [[ "$status" -ne 0 ]]; then restore_dedupe; echo "$(date '+%F %T %Z') notify failed; dedupe restored" >> "$LOG_DIR/gmgn-positions.log"; exit 0; fi
if [[ -n "$dedupe_key" ]]; then
  mark_dedupe "$dedupe_key"
fi
exit 0
