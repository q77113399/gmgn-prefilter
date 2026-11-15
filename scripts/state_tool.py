#!/usr/bin/env python3
"""Small state helper shared by the box and GitHub Actions runners (stdlib only).

  state_tool.py recent <last-run.json> <seconds>   exit 0 if a run started within <seconds>, else 1
  state_tool.py stamp  <last-run.json> <runner>     write {"epoch","iso","runner","run_id"}
  state_tool.py merge  <src_dir> <dst_dir>          union-merge state files from src into dst
  state_tool.py merge-driver <O> <A> <B> <path>     git merge driver: union of A and B into A
  state_tool.py validate <dir>...                   exit 1 if any *.json under dir fails to parse

Union rules (never lose a dedupe mark, so neither runner re-wakes the bot):
  - last-run.json: keep the record with the larger epoch
  - {"keys": {...}} / {"mints": {...}}: per-entry keep the larger ts (int or {"ts": int})
  - entries older than PRUNE_SEC are dropped (longest dedupe TTL is 24h)
  - bounty/seen.json: union of "ids", NEVER pruned (a dropped id would re-wake Pano3)
  - bounty/abf-cache.json: keep the record with the larger fetched_at_unix
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

STATE_FILES = ("mint-dedupe.json", "notify-dedupe.json", "positions-dedupe.json", "last-run.json")
BOUNTY_FILES = ("bounty/seen.json", "bounty/abf-cache.json")
PRUNE_SEC = 3 * 86400


def load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}


def dump(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def ts_of(rec: Any) -> int:
    if isinstance(rec, (int, float)):
        return int(rec)
    if isinstance(rec, dict):
        try:
            return int(rec.get("ts") or rec.get("epoch") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def union(a: Any, b: Any, name: str) -> Any:
    a = a if isinstance(a, dict) else {}
    b = b if isinstance(b, dict) else {}
    if name == "last-run.json" or "epoch" in a or "epoch" in b:
        return a if ts_of(a) >= ts_of(b) else b
    now = int(time.time())
    out: dict[str, Any] = {}
    for top in sorted(set(a) | set(b)):
        va, vb = a.get(top), b.get(top)
        if isinstance(va, dict) or isinstance(vb, dict):
            va = va if isinstance(va, dict) else {}
            vb = vb if isinstance(vb, dict) else {}
            merged: dict[str, Any] = {}
            for k in set(va) | set(vb):
                ra, rb = va.get(k), vb.get(k)
                win = ra if rb is None else rb if ra is None else (ra if ts_of(ra) >= ts_of(rb) else rb)
                if now - ts_of(win) < PRUNE_SEC:
                    merged[k] = win
            out[top] = merged
        else:
            out[top] = va if va is not None else vb
    return out


def bounty_union(a: Any, b: Any, name: str) -> Any:
    a = a if isinstance(a, dict) else {}
    b = b if isinstance(b, dict) else {}
    if name == "abf-cache.json":
        def fa(x: dict) -> float:
            try:
                return float(x.get("fetched_at_unix") or 0)
            except (TypeError, ValueError):
                return 0.0
        return a if fa(a) >= fa(b) else b
    ida = a.get("ids") if isinstance(a.get("ids"), dict) else {}
    idb = b.get("ids") if isinstance(b.get("ids"), dict) else {}
    ids: dict[str, Any] = {}
    for k in set(ida) | set(idb):
        ra, rb = ida.get(k), idb.get(k)
        # keep the first sighting (smaller ts); never drop an id
        ids[k] = ra if rb is None else rb if ra is None else (ra if ts_of(ra) <= ts_of(rb) else rb)
    out = {**b, **a}
    out["version"] = 1
    out["ids"] = dict(sorted(ids.items()))
    ua, ub = str(a.get("updated_at") or ""), str(b.get("updated_at") or "")
    if ua or ub:
        out["updated_at"] = max(ua, ub)
    return out


def is_bounty_path(path: str) -> bool:
    return "bounty/" in path.replace("\\", "/")


def cmd_recent(path: str, seconds: str) -> int:
    ep = ts_of(load(Path(path)))
    age = int(time.time()) - ep
    recent = ep > 0 and 0 <= age < int(seconds)
    print(f"last-run age={age if ep else 'none'}s recent={recent}", file=sys.stderr)
    return 0 if recent else 1


def cmd_stamp(path: str, runner: str) -> int:
    now = int(time.time())
    dump(Path(path), {
        "epoch": now,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
        "runner": runner,
        "run_id": os.environ.get("GITHUB_RUN_ID") or time.strftime("box-%Y%m%d-%H%M%S", time.localtime(now)),
    })
    return 0


def cmd_merge(src: str, dst: str) -> int:
    s, d = Path(src), Path(dst)
    for name in STATE_FILES:
        sp, dp = s / name, d / name
        if not sp.is_file():
            continue
        merged = union(load(dp), load(sp), name)
        if not dp.is_file() or load(dp) != merged:
            dump(dp, merged)
    for rel in BOUNTY_FILES:
        sp, dp = s / rel, d / rel
        if not sp.is_file():
            continue
        merged = bounty_union(load(dp), load(sp), Path(rel).name)
        if not dp.is_file() or load(dp) != merged:
            dump(dp, merged)
    return 0


def cmd_merge_driver(_o: str, a: str, b: str, path: str = "") -> int:
    pa, pb = Path(a), Path(b)
    try:
        ja = json.loads(pa.read_text(encoding="utf-8") or "{}")
        jb = json.loads(pb.read_text(encoding="utf-8") or "{}")
    except Exception:
        return 1  # let git report a real conflict
    name = Path(path).name if path else pa.name
    if path and is_bounty_path(path):
        dump(pa, bounty_union(ja, jb, name))
    else:
        dump(pa, union(ja, jb, name))
    return 0


def cmd_validate(*dirs: str) -> int:
    bad = 0
    for d in dirs:
        for f in Path(d).rglob("*.json"):
            try:
                json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                print(f"invalid json: {f}", file=sys.stderr)
                bad += 1
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, args = argv[1], argv[2:]
    fn = {"recent": cmd_recent, "stamp": cmd_stamp, "merge": cmd_merge,
          "merge-driver": cmd_merge_driver, "validate": cmd_validate}.get(cmd)
    if not fn:
        print(__doc__, file=sys.stderr)
        return 2
    return fn(*args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
