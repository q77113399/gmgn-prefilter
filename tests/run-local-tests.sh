#!/usr/bin/env bash
# Local DRY_RUN tests. Uses a throwaway copy of data/ so neither the repo's
# committed state nor the live box state is touched. Never sends a webhook.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d /tmp/gmgn-gha-test.XXXX)"
echo "scratch=$T"
setup() { # $1 = name, $2 = keep_dedupe(1)/empty(0)
  local d="$T/$1"; mkdir -p "$d"; cp -r "$REPO/data" "$d/"
  if [[ "$2" == 0 ]]; then rm -f "$d"/data/state/*dedupe.json "$d"/data/state/last-run.json; fi
  echo "$d"
}
common_env() { # $1 = case dir
  export GMGN_INPUTS_DIR="$1/data/inputs" QUOTA_SAVE_STATE_DIR="$1/data/state" \
    QUOTA_SAVE_LOG_DIR="$1/logs" GMGN_EVIDENCE_DIR="$1/evidence" GMGN_RUNSTATS_DIR="$1/stats" \
    QUOTA_SAVE_SECRETS="$1/no-secrets" DRY_RUN=1 FORCE=1
  unset WEBHOOK_URL WEBHOOK_BEARER GMGN_POSITIONS_FILE GMGN_POSITIONS_DEDUPE
}
pos_fixture() { # positions.json: one real mint with TP below current MC + pending_cleanup
  python3 - "$1/data/inputs/positions.json" "$2" <<'PY'
import json,sys
json.dump({"positions":{sys.argv[2]:{"name":"TESTPOS","tp_mc":1,"sl_mc":0,"entry_liq":1,"bought_at":"2026-11-15T00:00:00+08:00"}},
           "pending_cleanup":[{"name":"TESTCLEAN","mint":sys.argv[2],"note":"fixture"}]},open(sys.argv[1],"w"))
PY
}

# 1) box mode, snapshot dedupe (realistic), candidates + positions(real file)
d=$(setup box-snapshot 1); ( common_env "$d"; bash "$REPO/scripts/run-all.sh" ) > "$d/out.txt" 2>"$d/err.txt"
# 2) box mode, empty dedupe (forces a candidates payload if any strict passer exists) + positions fixture
d=$(setup box-empty 0); pos_fixture "$d" "${TEST_MINT:-}"; ( common_env "$d"; bash "$REPO/scripts/run-all.sh" ) > "$d/out.txt" 2>"$d/err.txt"
# 3) simulated GitHub Actions mode, empty dedupe + positions fixture
d=$(setup gha-empty 0); pos_fixture "$d" "${TEST_MINT:-}"
( common_env "$d"; export GITHUB_ACTIONS=true GITHUB_RUN_ID=4242 GITHUB_REPOSITORY=example/gmgn-gha GITHUB_SERVER_URL=https://github.com
  bash "$REPO/scripts/run-all.sh" ) > "$d/out.txt" 2>"$d/err.txt"
# 4) skip guard: last-run just stamped by case 3 -> run without FORCE must skip
( common_env "$d"; unset FORCE; export GITHUB_ACTIONS=true; bash "$REPO/scripts/run-all.sh" ) > "$d/out-guard.txt" 2>&1
echo "$T"
