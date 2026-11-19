#!/usr/bin/env bash
# Pre-commit guard for the PUBLIC repo. Scans the STAGED files of the repo in $1
# (default: cwd). Exit 1 (and print only file names, never matches) if:
#   - a staged path is outside the allow-list (scripts/, data/inputs|state/*.json,
#     .github/, tests/, README.md, .gitignore, .gitattributes)
#   - a staged file contains the actual WEBHOOK_URL / WEBHOOK_BEARER value (GMGN) or
#     BOUNTY_WEBHOOK_URL / BOUNTY_WEBHOOK_BEARER value (Pano3), from env or from the box
#     secrets files if present; compared via stdin, not argv
#   - a staged file contains a webhook-URL / bearer-token / KEY=value-looking string
set -uo pipefail
REPO="${1:-.}"
cd "$REPO" || exit 1
SECRETS_FILE="${QUOTA_SAVE_SECRETS:-/workspace/quota-save/secrets/webhook.env}"
URLV="${WEBHOOK_URL:-}"; BEARV="${WEBHOOK_BEARER:-}"
if [[ -f "$SECRETS_FILE" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      WEBHOOK_URL=*) v="${line#*=}"; v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"; URLV="$v" ;;
      WEBHOOK_BEARER=*) v="${line#*=}"; v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"; BEARV="$v" ;;
    esac
  done < "$SECRETS_FILE"
fi
BURLV="${BOUNTY_WEBHOOK_URL:-}"; BBEARV="${BOUNTY_WEBHOOK_BEARER:-}"
BOUNTY_SECRETS_FILE="${BOUNTY_WEBHOOK_SECRETS:-/workspace/quota-save/secrets/bounty-webhook.env}"
if [[ -f "$BOUNTY_SECRETS_FILE" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      WEBHOOK_URL=*|BOUNTY_WEBHOOK_URL=*) v="${line#*=}"; v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"; BURLV="$v" ;;
      WEBHOOK_BEARER=*|BOUNTY_WEBHOOK_BEARER=*) v="${line#*=}"; v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"; BBEARV="$v" ;;
    esac
  done < "$BOUNTY_SECRETS_FILE"
fi
# Encrypted-state key (repo secret PRIVATE_STATE_KEY / box key file): must never be committed.
PKEYV="${PRIVATE_STATE_KEY:-}"
PKEY_FILE="${PRIVATE_STATE_KEY_FILE:-/workspace/quota-save/secrets/private-state.key}"
[[ -z "$PKEYV" && -r "$PKEY_FILE" ]] && PKEYV="$(tr -d '[:space:]' < "$PKEY_FILE")"
fail=0
mapfile -t files < <(git diff --cached --name-only --diff-filter=ACMR)
for f in "${files[@]}"; do
  case "$f" in
    scripts/*|tests/*|.github/*|README.md|.gitignore|.gitattributes) ;;
    data/inputs/*.json|data/state/*.json) ;;
    *) echo "secret-check: path not allowed in public repo: $f" >&2; fail=1; continue ;;
  esac
  case "$f" in *secrets*|*.env|*.log|*evidence*) echo "secret-check: forbidden path: $f" >&2; fail=1; continue ;; esac
  blob="$(git show ":$f" 2>/dev/null)" || continue
  for v in "$URLV" "$BEARV" "$BURLV" "$BBEARV" "$PKEYV"; do
    if [[ ${#v} -ge 8 ]] && grep -qF -f <(printf '%s\n' "$v") <<<"$blob"; then
      echo "secret-check: webhook secret value found in $f" >&2; fail=1
    fi
  done
  if grep -qiE 'https?://[^"[:space:]]*(webhook|hooks?/)[^"[:space:]]*' <<<"$blob" \
     || grep -qE 'Bearer[[:space:]]+[A-Za-z0-9._~+/=-]{20,}' <<<"$blob" \
     || grep -qE '(WEBHOOK_URL|WEBHOOK_BEARER|API_KEY|SECRET|TOKEN)[[:space:]]*[=:][[:space:]]*["'"'"']?[A-Za-z0-9._~+/:=-]{12,}' <<<"$blob"; then
    echo "secret-check: secret-looking string in $f" >&2; fail=1
  fi
done
(( fail )) && echo "secret-check: FAILED (nothing will be committed/pushed)" >&2
exit $fail
