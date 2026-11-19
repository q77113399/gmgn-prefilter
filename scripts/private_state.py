#!/usr/bin/env python3
"""Encrypted private state shared between the box and GitHub Actions (PUBLIC repo).

Plaintext never touches the repo. Two single-writer blobs (Fernet = AES-128-CBC + HMAC):
  data/state/private-box.enc.json  written by the box: enabled smart-money wallets,
                                   early.json positions + fired position_events,
                                   smart-money woken event keys
  data/state/private-gha.enc.json  written by GHA: smart-money cursors / seen sigs /
                                   woken + detected keys, early peak_multiple per mint,
                                   fired early position_events
Merge rules: peak_multiple = max(box, gha); position_events / woken keys = union.
Key: env PRIVATE_STATE_KEY (repo secret) or box file
/workspace/quota-save/secrets/private-state.key (chmod 600). Never printed.

Commands
  box-sync [--no-push]   box: pull GHA blob, merge into early.json + smart-money skip
                         file; without --no-push also re-encrypt the box blob if its
                         plaintext changed and push it.
  box-sync --no-git      box: same, write the blob but let the caller (box-sync.sh) commit.
  gha-inputs <dir>       GHA prefilter: decrypt positions.json / watch.json / watch-state.json
                         into <dir> (gitignored data/inputs); exit 4 if missing.
  gha-load <dir>         GHA: decrypt both blobs, write wallets.json, early.json,
                         sm-state.json, sm-skip.json into <dir> (runner temp).
  gha-save <dir>         GHA: build the GHA blob from <dir> and re-encrypt if changed.
Exit 3 = no key configured (callers skip quietly).
"""
from __future__ import annotations

import hashlib, json, os, subprocess, sys, time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
BOX_BLOB = REPO / "data/state/private-box.enc.json"
GHA_BLOB = REPO / "data/state/private-gha.enc.json"
KEY_FILE = Path(os.environ.get("PRIVATE_STATE_KEY_FILE", "/workspace/quota-save/secrets/private-state.key"))
QS = Path(os.environ.get("QUOTA_SAVE_ROOT", "/workspace/quota-save"))
EARLY = QS / "state/early.json"
BOX_HASH = QS / "state/private-box.sha256"          # box-only, never committed
SM_ROOT = Path(os.environ.get("SMART_MONEY_ROOT", "/workspace/smart-money"))
SM_WALLETS = SM_ROOT / "wallets.json"
SM_STATE = SM_ROOT / "state.json"
SM_SKIP = SM_ROOT / "gha-woken.json"                 # box-only cache of GHA-woken keys
KEEP_SEC = 3 * 86400
# Holdings inputs: plaintext only on the box; published only inside the box blob.
# blob key -> (box source path, file name GHA writes into GMGN_INPUTS_DIR)
INPUTS = {
    "positions": (Path(os.environ.get("GMGN_BOX_POSITIONS", str(QS / "state/positions.json"))), "positions.json"),
    "watch": (Path(os.environ.get("GMGN_BOX_WATCH", "/workspace/watch-gmgn-exit/watch.json")), "watch.json"),
    "watch_state": (Path(os.environ.get("GMGN_BOX_WATCH_STATE", "/workspace/watch-gmgn-exit/state.json")), "watch-state.json"),
}


def key() -> bytes | None:
    k = os.environ.get("PRIVATE_STATE_KEY", "").strip()
    if not k and KEY_FILE.is_file():
        k = KEY_FILE.read_text().strip()
    return k.encode() if k else None


def fernet():
    from cryptography.fernet import Fernet
    k = key()
    if not k:
        print("private_state: no key configured; skipping", file=sys.stderr)
        sys.exit(3)
    return Fernet(k)


def canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def load_json(p: Path, default: Any) -> Any:
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def write_json(p: Path, obj: Any, mode: int | None = None) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, p)


def decrypt_blob(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        ct = json.loads(text).get("ct")
        if not ct:
            return {}
        return json.loads(fernet().decrypt(ct.encode()))
    except SystemExit:
        raise
    except Exception as e:
        print(f"private_state: decrypt failed ({type(e).__name__})", file=sys.stderr)
        return {}


def encrypt_to(path: Path, plain: dict[str, Any]) -> None:
    ct = fernet().encrypt(canon(plain)).decode()
    write_json(path, {"v": 1, "alg": "fernet", "note": "encrypted; see scripts/private_state.py", "ct": ct})


def prune(d: dict[str, Any], now: int) -> dict[str, Any]:
    out = {}
    for k, v in (d or {}).items():
        ts = v if isinstance(v, (int, float)) else (v or {}).get("ts", now) if isinstance(v, dict) else now
        if now - int(ts or now) < KEEP_SEC:
            out[k] = v
    return out


def merge_early(early: dict[str, Any], other: dict[str, Any]) -> bool:
    """peak = max, position_events = union. Returns True if early changed."""
    changed = False
    pos = early.get("positions") or {}
    for mint, pk in (other.get("early_peaks") or {}).items():
        rec = pos.get(mint)
        if isinstance(rec, dict):
            try:
                cur = float(rec.get("peak_multiple") or 0)
                pk = float(pk or 0)
            except (TypeError, ValueError):
                continue
            if pk > cur:
                rec["peak_multiple"] = round(pk, 4)
                changed = True
    pe = early.setdefault("position_events", {})
    for k, v in (other.get("early_events") or {}).items():
        if k not in pe:
            pe[k] = v
            changed = True
    return changed


# ---------------------------------------------------------------- box side
def git(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=timeout)


def write_inputs(box: dict[str, Any], d: Path) -> list[str]:
    """Write decrypted holdings inputs into d (runner temp / gitignored). Returns names written."""
    got = []
    inputs = box.get("inputs") or {}
    for k, (_src, name) in INPUTS.items():
        if isinstance(inputs.get(k), (dict, list)):
            write_json(d / name, inputs[k], 0o600)
            got.append(name)
    return got


def gha_inputs(d: Path) -> int:
    """GHA prefilter: decrypt holdings into d. Exit 0 only if positions+watch are present."""
    fernet()
    box = decrypt_blob(BOX_BLOB.read_text() if BOX_BLOB.is_file() else "")
    got = write_inputs(box, d)
    print(f"private_state gha-inputs: {len(got)}/3 holdings files", file=sys.stderr)
    return 0 if {"positions.json", "watch.json"} <= set(got) else 4


def box_sync(push: bool = True, git_push: bool = True) -> int:
    now = int(time.time())
    fernet()  # exits 3 without key
    try:
        git("fetch", "-q", "origin", "main", timeout=30)
    except Exception:
        pass
    gha_text = git("show", "origin/main:data/state/private-gha.enc.json").stdout
    gha = decrypt_blob(gha_text)
    # merge GHA -> early.json
    early = load_json(EARLY, None)
    if isinstance(early, dict) and gha:
        if merge_early(early, gha):
            write_json(EARLY, early)
    # GHA-woken smart-money keys -> box skip cache
    if gha:
        write_json(SM_SKIP, {"updated": now, "keys": gha.get("sm_woken") or {}}, 0o600)
    if not push:
        return 0  # pull/merge only; the pushing call owns the box blob + hash
    # build box plaintext
    w = load_json(SM_WALLETS, {})
    wl = w.get("wallets") if isinstance(w, dict) else w
    wallets = sorted({x["address"] for x in wl or [] if isinstance(x, dict) and x.get("address") and x.get("enabled", True)}
                     | {x for x in wl or [] if isinstance(x, str)})
    early = load_json(EARLY, {}) or {}
    sm = load_json(SM_STATE, {}) or {}
    plain = {
        "wallets": wallets,
        "early_positions": early.get("positions") or {},
        "early_events": prune(early.get("position_events") or {}, now),
        "sm_woken": prune(sm.get("woken_keys") or {}, now),
        "inputs": {k: v for k, (src, _n) in INPUTS.items() if (v := load_json(src, None)) is not None},
    }
    h = hashlib.sha256(canon(plain)).hexdigest()
    if BOX_HASH.is_file() and BOX_HASH.read_text().strip() == h and BOX_BLOB.is_file():
        return 0
    plain["updated"] = now
    encrypt_to(BOX_BLOB, plain)
    BOX_HASH.write_text(h + "\n")
    if git_push:
        r = subprocess.run(["flock", "-w", "90", str(QS / "state/gha-git.lock"),
                            "bash", str(REPO / "scripts/git-push-state.sh"), f"box: private state {time.strftime('%F %T %Z')}"],
                           cwd=str(REPO), capture_output=True, text=True, timeout=150,
                           env={**os.environ, "GMGN_GHA_REPO": str(REPO), "PRIVATE_BLOB_OWN": BOX_BLOB.name})
        if r.returncode != 0:
            BOX_HASH.unlink(missing_ok=True)  # retry next time
        print(f"private_state box-sync push rc={r.returncode}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- GHA side
def gha_load(d: Path) -> int:
    fernet()
    d.mkdir(parents=True, exist_ok=True)
    box = decrypt_blob(BOX_BLOB.read_text() if BOX_BLOB.is_file() else "")
    gha = decrypt_blob(GHA_BLOB.read_text() if GHA_BLOB.is_file() else "")
    write_json(d / "gha-prev.json", gha, 0o600)
    write_inputs(box, d)  # positions.json / watch.json for the smart-money held-sell check
    write_json(d / "wallets.json", {"wallets": [{"address": a, "enabled": True} for a in box.get("wallets") or []]}, 0o600)
    early = {"budget_sol": 0.5, "spent_sol": 0, "seen": {}, "positions": box.get("early_positions") or {},
             "position_events": dict(box.get("early_events") or {})}
    merge_early(early, gha)
    write_json(d / "early-state" / "early.json", early, 0o600)
    st = {"cursors": gha.get("sm_cursors") or {}, "initialized": {a: True for a in (gha.get("sm_cursors") or {})},
          "seen_sigs": gha.get("sm_seen") or {}, "pending": gha.get("sm_pending") or [],
          "last_wake_ts": gha.get("sm_last_wake") or 0, "woken_keys": gha.get("sm_woken") or {},
          "detected_keys": gha.get("sm_detect") or {}}
    write_json(d / "sm-state.json", st, 0o600)
    write_json(d / "sm-skip.json", {"keys": box.get("sm_woken") or {}}, 0o600)
    print(f"private_state gha-load ok wallets={len(box.get('wallets') or [])} "
          f"early_positions={len(early['positions'])} box_blob={'yes' if box else 'no'}", file=sys.stderr)
    return 0


def gha_save(d: Path) -> int:
    now = int(time.time())
    fernet()
    prev = load_json(d / "gha-prev.json", {}) or {}
    early = load_json(d / "early-state" / "early.json", {}) or {}
    st = load_json(d / "sm-state.json", {}) or {}
    peaks = dict(prev.get("early_peaks") or {})
    for m, rec in (early.get("positions") or {}).items():
        if isinstance(rec, dict) and rec.get("peak_multiple") is not None:
            peaks[m] = max(float(rec["peak_multiple"]), float(peaks.get(m) or 0))
    live = set((early.get("positions") or {}).keys())
    peaks = {m: v for m, v in peaks.items() if m in live}
    plain = {
        "sm_cursors": st.get("cursors") or {},
        "sm_seen": prune(st.get("seen_sigs") or {}, now),
        "sm_pending": st.get("pending") or [],
        "sm_last_wake": st.get("last_wake_ts") or 0,
        "sm_woken": prune(st.get("woken_keys") or {}, now),
        "sm_detect": prune(st.get("detected_keys") or {}, now),
        "early_peaks": peaks,
        "early_events": prune(early.get("position_events") or {}, now),
    }
    old = {k: prev.get(k) for k in plain}
    if canon(old) == canon(plain) and GHA_BLOB.is_file():
        print("private_state gha-save: unchanged", file=sys.stderr)
        return 0
    plain["updated"] = now
    encrypt_to(GHA_BLOB, plain)
    print("private_state gha-save: blob updated", file=sys.stderr)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "box-sync":
        return box_sync(push="--no-push" not in argv, git_push="--no-git" not in argv)
    if cmd == "gha-inputs":
        return gha_inputs(Path(argv[2]))
    if cmd == "gha-load":
        return gha_load(Path(argv[2]))
    if cmd == "gha-save":
        return gha_save(Path(argv[2]))
    if cmd == "has-key":
        return 0 if key() else 3
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
