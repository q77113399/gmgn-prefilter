# gmgn-gha — GitHub Actions backstop for the GMGN box prefilters

PUBLIC repo. Contains only code and non-secret JSON inputs/state. Webhook URL and
bearer live ONLY in repo secrets (`WEBHOOK_URL`, `WEBHOOK_BEARER`) and on the box
in `/workspace/quota-save/secrets/webhook.env` (never committed).

- `scripts/` — box scripts, path-configurable via env (defaults = box layout):
  `gmgn-prefilter.py/.sh` (task gmgn-candidates, run strict), `gmgn-positions.py/.sh`
  (task gmgn-positions), `lib_notify.sh` (payload v1 + notify + dedupe),
  `run-all.sh` (one round, 12-min shared skip guard), `state_tool.py` (gap check,
  stamp, union merge, git merge driver), `secret-check.sh` (public-repo guard),
  `git-push-state.sh` (commit + push with retries), `box-sync.sh` (box <-> repo),
  `loop-gha.sh` (proposed box loop; not active).
- `data/inputs/` — written only by the box (Pano-owned): narratives.json, watch.json,
  watch-state.json, rejected.json, positions.json.
- `data/state/` — dedupe files + last-run.json, written by both runners, union-merged.
- `.github/workflows/prefilter.yml` — every 15 min + manual dispatch.

In GitHub mode (`GITHUB_ACTIONS=true`) payloads carry details inline in `metrics`
and `evidence_path` is `github-actions run <run_id>`.

Local tests (DRY_RUN, scratch copies only): `bash tests/run-local-tests.sh`.
