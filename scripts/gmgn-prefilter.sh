#!/usr/bin/env bash
# Thin wrapper: run the numeric prefilter and notify only when it prints a payload.
# Never trades. Set DRY_RUN=1 to print the payload and skip the webhook POST.
# Runs on the box (defaults = box paths) or in GitHub Actions (paths via env).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$PATH:/home/box/.local/bin:/usr/local/bin:/usr/bin:/bin"
export QUOTA_SAVE_ROOT="${QUOTA_SAVE_ROOT:-/workspace/quota-save}"

# shellcheck source=lib_notify.sh
source "$SCRIPT_DIR/lib_notify.sh"
mkdir -p "$STATE_DIR" "$LOG_DIR"

PY="$SCRIPT_DIR/gmgn-prefilter.py"
# The .py marks mint-dedupe before we POST; back it up so a failed POST is retried next run.
bk=$(mktemp -d)
for f in notify-dedupe.json mint-dedupe.json; do [[ -f "$STATE_DIR/$f" ]] && cp "$STATE_DIR/$f" "$bk/"; done
restore_dedupe() { for f in notify-dedupe.json mint-dedupe.json; do if [[ -f "$bk/$f" ]]; then cp "$bk/$f" "$STATE_DIR/$f"; else rm -f "$STATE_DIR/$f"; fi; done; }
payload="$(python3 "$PY" || true)"

if [[ -z "${payload//[$'\t\r\n ']}" ]]; then
  exit 0
fi

dedupe_key="$(printf '%s' "$payload" | jq -r '.dedupe_key // empty' 2>/dev/null || true)"
if [[ -n "$dedupe_key" ]] && should_skip_dedupe "$dedupe_key" 86400; then
  ts=$(date '+%Y-%m-%d %H:%M:%S %Z')
  echo "$ts wrapper skip: notify-level 24h dedupe" >> "$LOG_DIR/gmgn-prefilter.log"
  exit 0
fi

status=0; notify "$payload" || status=$?
if [[ "$status" -ne 0 ]]; then
  restore_dedupe
  echo "$(date '+%F %T %Z') notify failed; dedupe restored" >> "$LOG_DIR/gmgn-prefilter.log"
  exit 0
fi
if [[ "${DRY_RUN:-0}" != 1 && -n "$dedupe_key" ]]; then
  mark_dedupe "$dedupe_key"
fi
exit 0
