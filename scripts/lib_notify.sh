#!/usr/bin/env bash
# Shared notify / dedupe helpers for box-side quota-save prefilters.
# Mechanical only (no LLM). Never prints secret values or webhook.env contents.
# Expected secrets file keys: WEBHOOK_URL, WEBHOOK_BEARER
# In GitHub Actions there is no secrets file: WEBHOOK_URL / WEBHOOK_BEARER come
# from repo secrets via env (load_secrets keeps env values when the file is absent).

ROOT="${QUOTA_SAVE_ROOT:-/workspace/quota-save}"
STATE_DIR="${QUOTA_SAVE_STATE_DIR:-$ROOT/state}"
LOG_DIR="${QUOTA_SAVE_LOG_DIR:-$ROOT/logs}"
SECRETS_FILE="${QUOTA_SAVE_SECRETS:-$ROOT/secrets/webhook.env}"
DEDUPE_FILE="${STATE_DIR}/notify-dedupe.json"

mkdir -p "$STATE_DIR" "$LOG_DIR"

_notify_log() {
  local ts
  ts=$(date '+%Y-%m-%d %H:%M:%S %Z')
  printf '%s %s\n' "$ts" "$*" >> "$LOG_DIR/notify.log"
}

# Load WEBHOOK_URL and WEBHOOK_BEARER from secrets/webhook.env if present.
# Does not echo the file or the values.
load_secrets() {
  WEBHOOK_URL="${WEBHOOK_URL:-}"
  WEBHOOK_BEARER="${WEBHOOK_BEARER:-}"
  if [[ -f "$SECRETS_FILE" ]]; then
    # Parse KEY=VAL lines only; ignore comments / blanks. Do not `source`
    # (avoids executing unexpected content). Values may be quoted.
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
        WEBHOOK_URL) WEBHOOK_URL="$val" ;;
        WEBHOOK_BEARER) WEBHOOK_BEARER="$val" ;;
      esac
    done < "$SECRETS_FILE"
  fi
}

_dedupe_get_ts() {
  local key="$1"
  python3 - "$DEDUPE_FILE" "$key" << 'PY'
import json, sys, pathlib
path, key = pathlib.Path(sys.argv[1]), sys.argv[2]
if not path.is_file():
    sys.exit(0)
try:
    data = json.loads(path.read_text() or "{}")
except Exception:
    sys.exit(0)
keys = data.get("keys") if isinstance(data, dict) else None
if not isinstance(keys, dict):
    sys.exit(0)
ts = keys.get(key)
if isinstance(ts, (int, float)):
    print(int(ts))
PY
}

# Usage: should_skip_dedupe <key> <ttl_seconds>
# Return 0 if this key was marked within ttl (caller should skip).
# Return 1 if it is new / expired (caller may notify).
should_skip_dedupe() {
  local key="${1:-}" ttl="${2:-0}"
  [[ -n "$key" ]] || return 1
  local last now
  last=$(_dedupe_get_ts "$key")
  [[ -n "$last" ]] || return 1
  now=$(date +%s)
  if (( now - last < ttl )); then
    return 0
  fi
  return 1
}

# Usage: mark_dedupe <key>
mark_dedupe() {
  local key="${1:-}"
  [[ -n "$key" ]] || return 0
  python3 - "$DEDUPE_FILE" "$key" << 'PY'
import json, sys, time, pathlib, os
path = pathlib.Path(sys.argv[1])
key = sys.argv[2]
path.parent.mkdir(parents=True, exist_ok=True)
data = {}
if path.is_file():
    try:
        data = json.loads(path.read_text() or "{}")
    except Exception:
        data = {}
if not isinstance(data, dict):
    data = {}
keys = data.get("keys")
if not isinstance(keys, dict):
    keys = {}
now = int(time.time())
# Drop entries older than 30 days to keep the file small.
fresh = {}
for k, ts in keys.items():
    try:
        if now - int(ts) < 30 * 86400:
            fresh[k] = int(ts)
    except (TypeError, ValueError):
        continue
fresh[key] = now
data["keys"] = fresh
tmp = path.with_suffix(".tmp")
tmp.write_text(json.dumps(data, indent=2) + "\n")
os.replace(tmp, path)
PY
}

# Usage: payload <task_id> <severity> <summary> <evidence_path> \
#                <recommended_action> <dedupe_key> [metrics_json]
# Prints a v1 webhook JSON object to stdout.
payload() {
  local task_id="$1" severity="$2" summary="$3" evidence_path="$4"
  local recommended_action="$5" dedupe_key="$6" metrics="${7:-{}}"
  METRICS_JSON="$metrics" python3 -c '
import json, os, sys
raw = os.environ.get("METRICS_JSON") or "{}"
try:
    metrics = json.loads(raw)
except json.JSONDecodeError:
    metrics = {}
if not isinstance(metrics, dict):
    metrics = {}
obj = {
    "schema_version": 1,
    "task_id": sys.argv[1],
    "severity": sys.argv[2],
    "summary": sys.argv[3],
    "evidence_path": sys.argv[4],
    "recommended_action": sys.argv[5],
    "dedupe_key": sys.argv[6],
    "changed": True,
    "metrics": metrics,
}
print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
' "$task_id" "$severity" "$summary" "$evidence_path" "$recommended_action" "$dedupe_key"
}

# Usage: notify '<v1 json payload>'
# DRY_RUN=1 (default if unset in interactive use is not assumed; caller sets it)
# only prints the payload and does not POST. Otherwise curl POSTs with
# Authorization: Bearer, and logs the HTTP status code only (never the URL,
# bearer, or body).
notify() {
  local json="${1:-}"
  local ts http
  ts=$(date '+%Y-%m-%d %H:%M:%S %Z')
  if [[ -z "$json" ]]; then
    _notify_log "notify skipped: empty payload"
    return 0
  fi
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    printf '%s\n' "$json"
    _notify_log "notify DRY_RUN=1 (not sent)"
    return 0
  fi
  load_secrets
  if [[ -z "${WEBHOOK_URL:-}" || -z "${WEBHOOK_BEARER:-}" ]]; then
    _notify_log "notify skipped: webhook not configured"
    # Non-zero so wrappers restore dedupe (do not suppress an unsent event).
    return 1
  fi
  http=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 20 \
    -X POST "$WEBHOOK_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${WEBHOOK_BEARER}" \
    --data-binary "$json" || echo "000")
  _notify_log "notify http=${http}"
  printf 'notify http=%s\n' "$http"
  # Non-2xx (or curl failure -> 000) returns 1 so wrappers can restore dedupe
  # state and retry next run instead of suppressing the event.
  [[ "$http" =~ ^2[0-9][0-9]$ ]] || return 1
  return 0
}
