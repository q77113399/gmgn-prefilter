#!/usr/bin/env bash
# Bounty prefilter wrapper (Pano3 wake). Runs on the box and in GitHub Actions.
# Uses ONLY its own webhook: BOUNTY_WEBHOOK_URL / BOUNTY_WEBHOOK_BEARER.
#   GitHub Actions: from repo secrets via env.
#   Box: parsed from $BOUNTY_WEBHOOK_SECRETS (default quota-save/secrets/bounty-webhook.env,
#        whose keys are WEBHOOK_URL / WEBHOOK_BEARER) when the env vars are empty.
# The GMGN WEBHOOK_URL / WEBHOOK_BEARER are unset so they can never reach this script.
# Never prints secret values or the secrets file. DRY_RUN=1 never POSTs or marks ids.
# Paths (defaults are box paths):
#   BOUNTY_STATE_DIR    default $QUOTA_SAVE_STATE_DIR/bounty or quota-save/state/bounty
#   BOUNTY_LOG_DIR      default $QUOTA_SAVE_LOG_DIR/bounty or /workspace/bounty-watch/logs
#   BOUNTY_EVIDENCE_DIR default $GMGN_EVIDENCE_DIR/bounty or /workspace/bounty-watch/evidence
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$PATH:/home/box/.local/bin:/usr/local/bin:/usr/bin:/bin"
QS_ROOT="${QUOTA_SAVE_ROOT:-/workspace/quota-save}"
if [[ -n "${QUOTA_SAVE_STATE_DIR:-}" ]]; then def_state="$QUOTA_SAVE_STATE_DIR/bounty"; else def_state="$QS_ROOT/state/bounty"; fi
if [[ -n "${QUOTA_SAVE_LOG_DIR:-}" ]]; then def_log="$QUOTA_SAVE_LOG_DIR/bounty"; else def_log="/workspace/bounty-watch/logs"; fi
if [[ -n "${GMGN_EVIDENCE_DIR:-}" ]]; then def_ev="$GMGN_EVIDENCE_DIR/bounty"; else def_ev="/workspace/bounty-watch/evidence"; fi
export BOUNTY_STATE_DIR="${BOUNTY_STATE_DIR:-$def_state}"
export BOUNTY_LOG_DIR="${BOUNTY_LOG_DIR:-$def_log}"
export BOUNTY_EVIDENCE_DIR="${BOUNTY_EVIDENCE_DIR:-$def_ev}"

SECRETS_FILE="${BOUNTY_WEBHOOK_SECRETS:-$QS_ROOT/secrets/bounty-webhook.env}"

# Parse KEY=VAL only; do not source the file. Do not echo values.
load_bounty_secrets() {
  BOUNTY_WEBHOOK_URL="${BOUNTY_WEBHOOK_URL:-}"
  BOUNTY_WEBHOOK_BEARER="${BOUNTY_WEBHOOK_BEARER:-}"
  [[ -n "$BOUNTY_WEBHOOK_URL" && -n "$BOUNTY_WEBHOOK_BEARER" ]] && return 0
  [[ -f "$SECRETS_FILE" ]] || return 0
  local line key val
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" != *=* ]] && continue
    key="${line%%=*}"
    val="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    key="${key#"${key%%[![:space:]]*}"}"
    val="${val%"${val##*[![:space:]]}"}"
    val="${val#"${val%%[![:space:]]*}"}"
    if [[ "$val" == \"*\" && "$val" == *\" ]]; then
      val="${val#\"}"; val="${val%\"}"
    elif [[ "$val" == \'*\' && "$val" == *\' ]]; then
      val="${val#\'}"; val="${val%\'}"
    fi
    case "$key" in
      WEBHOOK_URL|BOUNTY_WEBHOOK_URL) BOUNTY_WEBHOOK_URL="$val" ;;
      WEBHOOK_BEARER|BOUNTY_WEBHOOK_BEARER) BOUNTY_WEBHOOK_BEARER="$val" ;;
    esac
  done < "$SECRETS_FILE"
}

unset WEBHOOK_URL WEBHOOK_BEARER
load_bounty_secrets
export BOUNTY_WEBHOOK_URL BOUNTY_WEBHOOK_BEARER
export DRY_RUN="${DRY_RUN:-0}"

exec python3 "$SCRIPT_DIR/bounty-watch.py" "$@"
