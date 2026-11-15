#!/usr/bin/env bash
# PROPOSED replacement for /workspace/quota-save/scripts/loop.sh (not active).
# Same catch-up loop, but each due tick: box-sync pre -> run-all (shared 12-min
# guard with GitHub Actions via last-run.json) -> box-sync post.
# Uses the SAME lock file as loop.sh, so it cannot run alongside it: stop the
# old loop first. Start (from a block_until_ms:0 Shell):
#   cd /workspace/quota-save && exec env -u GMGN_POSITIONS_DEDUPE -u GMGN_POSITIONS_FILE setsid bash /workspace/gmgn-gha/scripts/loop-gha.sh >/dev/null 2>&1 </dev/null
unset GMGN_POSITIONS_DEDUPE GMGN_POSITIONS_FILE QUOTA_SAVE_STATE_DIR GMGN_EVIDENCE_DIR QUOTA_SAVE_LOG_DIR DRY_RUN \
      GMGN_INPUTS_DIR GMGN_RUNSTATS_DIR GITHUB_ACTIONS FORCE
export PATH=/home/box/.local/bin:/usr/local/bin:/usr/bin:/bin QUOTA_SAVE_ROOT=/workspace/quota-save GMGN_RUNNER=box
R=/workspace/quota-save
G="${GMGN_GHA_REPO:-/workspace/gmgn-gha}"
INTERVAL=${LOOP_INTERVAL_SEC:-900}
exec 9>$R/state/loop.lock; flock -n 9 || { echo "loop already running"; exit 0; }
LAST_FILE=$R/state/loop-last-run
HB=$R/logs/loop-heartbeat.log
while true; do
  now=$(date +%s)
  last=$(cat "$LAST_FILE" 2>/dev/null || echo 0)
  [[ "$last" =~ ^[0-9]+$ ]] || last=0
  gap=$(( now - last ))
  if (( gap >= INTERVAL )); then
    echo "$now" > "$LAST_FILE"
    echo "$(date '+%F %T') run gap=${gap}s (gha-sync)" >> "$HB"
    bash "$G/scripts/box-sync.sh" pre
    bash "$G/scripts/run-all.sh" >> $R/logs/gmgn-run-all.cron.log 2>&1
    bash "$G/scripts/box-sync.sh" post
  fi
  sleep 20
done
