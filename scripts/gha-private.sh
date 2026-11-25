#!/usr/bin/env bash
# GitHub Actions: private monitors that need the encrypted state (PUBLIC repo).
#   1) early-position exit monitor  -> gmgn-4 task_id gmgn-early-positions
#   2) smart-money follow (1 poll)  -> gmgn-4 task_id gmgn-smart-money
# Plaintext lives only in $RUNNER_TEMP. Detailed logs (mints, wallets) go to files
# there and are NEVER printed; the public log gets counts + HTTP codes only.
# No PRIVATE_STATE_KEY secret -> skip quietly. DRY_RUN=1 -> no POST.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
D="${RUNNER_TEMP:?}/private"
ts() { date '+%F %T %Z'; }
HEALTH="$REPO/data/state/private-gha-health.json"
health() { # key box_ok inputs early_reason sm_rc sm_polled sm_errors
  python3 - "$HEALTH" "$@" <<'PY' || true
import json, os, sys, time
a = sys.argv[2:] + [""] * 8
json.dump({"epoch": int(time.time()), "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "run_id": os.environ.get("GITHUB_RUN_ID", ""), "key_set": a[0] == "1",
           "box_blob_decrypted": a[1] == "1", "holdings_files": a[2], "early_exit_check": a[3],
           "smart_money_rc": a[4], "smart_money_polled": a[5] == "1", "smart_money_errors": a[6]},
          open(sys.argv[1], "w"), indent=2, sort_keys=True)
PY
}
if [[ -z "${PRIVATE_STATE_KEY:-}" ]]; then
  echo "$(ts) private monitors skipped: repo secret PRIVATE_STATE_KEY not set"; health 0; exit 0
fi
python3 -c 'import cryptography' 2>/dev/null || pip install -q cryptography >/dev/null 2>&1
umask 077; mkdir -p "$D/sm" "$D/logs"
python3 "$SCRIPT_DIR/private_state.py" gha-load "$D" 2>"$D/logs/load.err"; rc=$?
if [[ $rc -ne 0 ]]; then echo "$(ts) private gha-load failed rc=$rc"; health 1 0; exit 0; fi
box_ok=0; grep -q 'box_blob=yes' "$D/logs/load.err" && box_ok=1
n_inputs=$(ls "$D"/positions.json "$D"/watch.json "$D"/watch-state.json 2>/dev/null | wc -l)
echo "$(ts) $(grep -o 'gha-load ok.*' "$D/logs/load.err" | sed -E 's/ wallets=[0-9]+//')"
export QUOTA_SAVE_SECRETS="$D/no-secrets-file"

# 1) early positions
export QUOTA_SAVE_LOG_DIR="$D/logs"
cp "$D/early-state/early.json" "$D/early.before.json"
payload="$(GMGN_EARLY_SOURCE=gha GMGN_EARLY_MODE=positions QUOTA_SAVE_STATE_DIR="$D/early-state" \
  GMGN_GHA_SEEN_PATH="$D/none.json" GMGN_REJECTED_PATH="$REPO/data/inputs/rejected.json" \
  GMGN_NARRATIVES_PATH="$REPO/data/inputs/narratives.json" \
  python3 "$SCRIPT_DIR/gmgn-early.py" 2>>"$D/logs/early-pos.err" || true)"
if printf '%s' "$payload" | jq -e '.task_id == "gmgn-early-positions"' >/dev/null 2>&1; then
  n=$(printf '%s' "$payload" | jq '.metrics.events | length')
  # shellcheck source=/dev/null
  ( export QUOTA_SAVE_STATE_DIR="$D/early-state"; source "$SCRIPT_DIR/lib_notify.sh"; notify "$payload" >/dev/null ); nrc=$?
  if [[ $nrc -eq 0 ]]; then echo "$(ts) early-positions: wake sent events=$n source=gha"
  else echo "$(ts) early-positions: wake NOT sent (rc=$nrc); events re-armed"; cp "$D/early.before.json" "$D/early-state/early.json"; fi
else
  echo "$(ts) early-positions: $(jq -r '"reason=\(.reason) open=\(.open_positions // 0)"' "$D/early-state/gmgn-early-last.json" 2>/dev/null || echo 'no stats')"
fi

early_reason=$(jq -r '.reason // "no_stats"' "$D/early-state/gmgn-early-last.json" 2>/dev/null || echo no_stats)

# 2) smart-money single poll
# Helius RPC if the repo secret is set (URL never printed; GitHub also masks the key).
if [[ -n "${HELIUS_API_KEY:-}" ]]; then export SOLANA_RPC_URL="https://mainnet.helius-rpc.com/?api-key=${HELIUS_API_KEY}"; fi
export SMART_MONEY_MIN_BUY_SOL="${SMART_MONEY_MIN_BUY_SOL:-0.5}"
SMART_MONEY_SOURCE=gha SMART_MONEY_ROOT="$D/sm" SMART_MONEY_WALLETS="$D/wallets.json" \
SMART_MONEY_STATE="$D/sm-state.json" SMART_MONEY_SKIP="$D/sm-skip.json" SMART_MONEY_EVIDENCE="$D/sm/evidence" \
GMGN_REJECTED="$REPO/data/inputs/rejected.json" GMGN_POSITIONS_FILE="$D/positions.json" \
GMGN_BOX_WATCH="$D/watch.json" \
  timeout 200 python3 "$SCRIPT_DIR/smart-money-watch.py" once --max-tx 20 >"$D/logs/sm.log" 2>&1
sm_rc=$?
sm_polled=0; grep -q 'poll wallets=[1-9]' "$D/logs/sm.log" && sm_polled=1
sm_err=$(grep -o 'errors=[0-9]*' "$D/logs/sm.log" | tail -1 | cut -d= -f2)
health 1 "$box_ok" "${n_inputs// /}/3" "$early_reason" "$sm_rc" "$sm_polled" "${sm_err:-?}"
echo "$(ts) smart-money rc=$sm_rc $(grep -o ' rpc=[a-z-]*' "$D/logs/sm.log" | tail -1) $(grep -o 'poll wallets=.*' "$D/logs/sm.log" | tail -1) $(grep -o 'notify http=[0-9]*\|wake [A-Z_ ]*ok events=[0-9]*\|already woken by the other runner\|rate-limit: [0-9]* event' "$D/logs/sm.log" | tr '\n' ' ')"

python3 "$SCRIPT_DIR/private_state.py" gha-save "$D" 2>&1 | grep -o 'gha-save.*' || true
exit 0
