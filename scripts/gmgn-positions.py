#!/usr/bin/env python3
"""Numeric prefilter for Pano's GMGN open-position / leftover-order checks
(task_id: gmgn-positions).

Mechanical only (stdlib + DexScreener; no LLM, no trades, no webhooks).
Reads state/positions.json (Pano-owned; this script never writes it).
Prints one v1 wake payload to stdout when a fresh event exists.

Quiet exit 0 (empty stdout) when:
  - the positions file is missing
  - positions is empty AND pending_cleanup is empty
  - no wake condition holds, or every event was already woken within 6h
  - a required DexScreener fetch fails (when there are positions to check)
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("QUOTA_SAVE_ROOT", "/workspace/quota-save"))
STATE_DIR = Path(os.environ.get("QUOTA_SAVE_STATE_DIR", str(ROOT / "state")))
LOG_DIR = Path(os.environ.get("QUOTA_SAVE_LOG_DIR", str(ROOT / "logs")))
EVIDENCE_DIR = Path(os.environ.get("GMGN_EVIDENCE_DIR", str(STATE_DIR / "evidence")))
RUNSTATS_DIR = Path(os.environ.get("GMGN_RUNSTATS_DIR", str(STATE_DIR)))
_INPUTS_DIR = os.environ.get("GMGN_INPUTS_DIR", "").strip()
# positions.json is Pano-owned input: GMGN_POSITIONS_FILE > GMGN_INPUTS_DIR > box state dir.
POSITIONS_PATH = Path(
    os.environ.get("GMGN_POSITIONS_FILE")
    or (str(Path(_INPUTS_DIR) / "positions.json") if _INPUTS_DIR else str(STATE_DIR / "positions.json"))
)
DEDUPE_PATH = Path(
    os.environ.get("GMGN_POSITIONS_DEDUPE", str(STATE_DIR / "positions-dedupe.json"))
)
LAST_RUN_PATH = RUNSTATS_DIR / "gmgn-positions-last.json"

# GitHub Actions mode: evidence stays on the ephemeral runner; payload is inline.
IN_GHA = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"


def evidence_ref(local_path: Path) -> str:
    if IN_GHA:
        return f"github-actions run {os.environ.get('GITHUB_RUN_ID', 'local')}"
    return str(local_path)


def runner_metrics() -> dict[str, Any]:
    out: dict[str, Any] = {"runner": os.environ.get("GMGN_RUNNER") or ("github-actions" if IN_GHA else "box")}
    if IN_GHA:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        if repo and run_id:
            out["run_url"] = f"{server}/{repo}/actions/runs/{run_id}"
        out["details_inline"] = True
    return out

TASK_ID = "gmgn-positions"
DEX_BASE = "https://api.dexscreener.com"
UA = "quota-save-gmgn-positions/1.0 (box numeric filter)"
HTTP_TIMEOUT = 20
HTTP_GAP_SEC = 0.2
MAX_RETRIES = 2
TOKENS_BATCH = 30
DEDUPE_TTL = 6 * 3600
LIQ_ABS_FLOOR = 10_000.0
LIQ_DROP_FRAC = 0.5

QUOTE_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB",
}


class FetchError(Exception):
    pass


def now_ts() -> int:
    return int(time.time())


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    line = f"{ts} {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / "gmgn-positions.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_last(stats: dict[str, Any]) -> None:
    try:
        atomic_write(LAST_RUN_PATH, json.dumps(stats, indent=2, default=str) + "\n")
    except OSError:
        pass


def http_get_json(url: str, *, retries: int = MAX_RETRIES) -> Any:
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": UA}
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                raw = resp.read()
            time.sleep(HTTP_GAP_SEC)
            return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            try:
                e.read()
            except Exception:
                pass
            if e.code == 429 and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise FetchError(f"HTTP {e.code} for {url.split('?')[0]}") from e
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise FetchError(f"{type(e).__name__} for {url.split('?')[0]}") from e
    raise FetchError(str(last_err))


def num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def liq_usd(pair: dict[str, Any]) -> float:
    liq = pair.get("liquidity") or {}
    if isinstance(liq, dict):
        v = num(liq.get("usd"))
        return v if v is not None else 0.0
    v = num(liq)
    return v if v is not None else 0.0


def market_cap(pair: dict[str, Any]) -> float | None:
    mc = num(pair.get("marketCap"))
    if mc is not None and mc > 0:
        return mc
    fdv = num(pair.get("fdv"))
    if fdv is not None and fdv > 0:
        return fdv
    return None


def pair_mint(pair: dict[str, Any]) -> str | None:
    base = ((pair.get("baseToken") or {}).get("address")) or ""
    quote = ((pair.get("quoteToken") or {}).get("address")) or ""
    if base in QUOTE_MINTS and quote and quote not in QUOTE_MINTS:
        return quote
    if base and base not in QUOTE_MINTS:
        return base
    if quote and quote not in QUOTE_MINTS:
        return quote
    return None


def fetch_best_pairs(mints: list[str]) -> dict[str, dict[str, Any]]:
    wanted = set(mints)
    best: dict[str, dict[str, Any]] = {}
    failures = 0
    batches = 0
    for i in range(0, len(mints), TOKENS_BATCH):
        batch = mints[i : i + TOKENS_BATCH]
        batches += 1
        url = f"{DEX_BASE}/tokens/v1/solana/{','.join(batch)}"
        try:
            data = http_get_json(url)
        except FetchError as e:
            failures += 1
            log(f"tokens/v1 batch failed: {e}")
            continue
        if not isinstance(data, list):
            continue
        for pair in data:
            if not isinstance(pair, dict) or pair.get("chainId") != "solana":
                continue
            mint = pair_mint(pair)
            if not mint or mint not in wanted:
                # Also match if this mint was requested as base/quote exactly.
                base = ((pair.get("baseToken") or {}).get("address")) or ""
                quote = ((pair.get("quoteToken") or {}).get("address")) or ""
                if base in wanted:
                    mint = base
                elif quote in wanted:
                    mint = quote
                else:
                    continue
            prev = best.get(mint)
            if prev is None or liq_usd(pair) > liq_usd(prev):
                best[mint] = pair
    if not best and mints:
        raise FetchError("no pair data from tokens/v1/solana")
    if failures == batches and mints:
        raise FetchError("all tokens/v1 batches failed")
    return best


def load_positions(path: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}, []
    raw = data.get("positions") or {}
    positions: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for mint, rec in raw.items():
            if isinstance(mint, str) and mint and isinstance(rec, dict):
                positions[mint] = rec
    elif isinstance(raw, list):
        for rec in raw:
            if not isinstance(rec, dict):
                continue
            mint = rec.get("mint") or rec.get("contract")
            if isinstance(mint, str) and mint:
                positions[mint] = rec
    pending = data.get("pending_cleanup") or []
    if not isinstance(pending, list):
        pending = []
    pending = [x for x in pending if isinstance(x, dict)]
    return positions, pending


def cleanup_key(pending: list[dict[str, Any]]) -> str:
    canonical = json.dumps(pending, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"cleanup:{digest}"


def load_dedupe() -> dict[str, int]:
    try:
        data = json.loads(DEDUPE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    keys = data.get("keys") if isinstance(data, dict) else {}
    if not isinstance(keys, dict):
        return {}
    now = now_ts()
    out: dict[str, int] = {}
    for k, rec in keys.items():
        ts: int | None = None
        if isinstance(rec, (int, float)):
            ts = int(rec)
        elif isinstance(rec, dict) and rec.get("ts") is not None:
            try:
                ts = int(rec["ts"])
            except (TypeError, ValueError):
                ts = None
        if ts is not None and now - ts < DEDUPE_TTL:
            out[str(k)] = ts
    return out


def save_dedupe(new_entries: dict[str, dict[str, Any]]) -> None:
    current = load_dedupe()
    merged: dict[str, Any] = {k: {"ts": ts} for k, ts in current.items()}
    merged.update(new_entries)
    atomic_write(DEDUPE_PATH, json.dumps({"keys": merged}, indent=2) + "\n")


def position_events(mint: str, rec: dict[str, Any], pair: dict[str, Any]) -> list[dict[str, Any]]:
    name = str(rec.get("name") or mint[:8])
    mc = market_cap(pair)
    liq = liq_usd(pair)
    tp = num(rec.get("tp_mc"))
    sl = num(rec.get("sl_mc"))
    entry_liq = num(rec.get("entry_liq"))
    events: list[dict[str, Any]] = []
    base = {
        "mint": mint,
        "name": name,
        "mc": mc,
        "liq": liq,
        "tp_mc": tp,
        "sl_mc": sl,
        "entry_liq": entry_liq,
        "bought_at": rec.get("bought_at"),
        "dexscreener_url": pair.get("url") or f"https://dexscreener.com/solana/{mint}",
    }
    if mc is not None and tp is not None and mc >= tp:
        events.append(
            {
                **base,
                "type": "tp",
                "severity": "medium",
                "action": "review_tp_sl_on_gmgn",
                "dedupe_key": f"{mint}:tp",
                "detail": f"MC {mc:.0f} >= TP {tp:.0f}",
            }
        )
    if mc is not None and sl is not None and mc <= sl:
        events.append(
            {
                **base,
                "type": "sl",
                "severity": "medium",
                "action": "review_tp_sl_on_gmgn",
                "dedupe_key": f"{mint}:sl",
                "detail": f"MC {mc:.0f} <= SL {sl:.0f}",
            }
        )
    liq_hit = False
    reasons = []
    if entry_liq is not None and liq < LIQ_DROP_FRAC * entry_liq:
        liq_hit = True
        reasons.append(f"liq {liq:.0f} < 50% of entry {entry_liq:.0f}")
    if liq < LIQ_ABS_FLOOR:
        liq_hit = True
        reasons.append(f"liq {liq:.0f} < $10K")
    if liq_hit:
        events.append(
            {
                **base,
                "type": "liq",
                "severity": "high",
                "action": "notify_user_possible_rug",
                "dedupe_key": f"{mint}:liq",
                "detail": "; ".join(reasons),
            }
        )
    return events


def recommended_action(events: list[dict[str, Any]]) -> str:
    actions: list[str] = []
    for ev in events:
        a = ev.get("action")
        if a and a not in actions:
            actions.append(str(a))
    return "; ".join(actions) if actions else "review_positions_on_gmgn"


def severity_of(events: list[dict[str, Any]]) -> str:
    if any(ev.get("severity") == "high" for ev in events):
        return "high"
    if any(ev.get("type") in ("tp", "sl") for ev in events):
        return "medium"
    return "info"


def build_summary(events: list[dict[str, Any]]) -> str:
    bits: list[str] = []
    for ev in events:
        typ = ev.get("type")
        name = ev.get("name") or ev.get("mint", "")[:8]
        if typ == "cleanup":
            n = ev.get("count") or len(ev.get("items") or [])
            bits.append(f"{name} pending cleanup ({n} item(s))" if name else f"pending cleanup ({n} item(s))")
        elif typ == "tp":
            bits.append(f"{name} TP crossed ({ev.get('detail')})")
        elif typ == "sl":
            bits.append(f"{name} SL crossed ({ev.get('detail')})")
        elif typ == "liq":
            bits.append(f"{name} liquidity drop ({ev.get('detail')})")
        else:
            bits.append(f"{name} {typ}")
    return "; ".join(bits) if bits else "position events"


def cleanup_event(pending: list[dict[str, Any]]) -> dict[str, Any]:
    names = [str(x.get("name") or x.get("mint") or "item") for x in pending]
    label = ", ".join(names[:5])
    if len(names) > 5:
        label += f" +{len(names) - 5}"
    return {
        "type": "cleanup",
        "name": label,
        "mint": pending[0].get("mint") if pending else None,
        "count": len(pending),
        "items": pending,
        "severity": "info",
        "action": "clean_orders_on_gmgn",
        "dedupe_key": cleanup_key(pending),
        "detail": f"{len(pending)} leftover order(s) to clean on GMGN",
    }


def run() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    RUNSTATS_DIR.mkdir(parents=True, exist_ok=True)

    run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    stats: dict[str, Any] = {"run_id": run_id, "ok": False, "positions_file": str(POSITIONS_PATH)}

    if not POSITIONS_PATH.is_file():
        log("quiet: positions file missing")
        stats["reason"] = "missing_file"
        write_last(stats)
        return 0

    try:
        positions, pending = load_positions(POSITIONS_PATH)
    except Exception as e:
        log(f"quiet: cannot read positions file ({type(e).__name__})")
        stats["reason"] = "unreadable_file"
        write_last(stats)
        return 0

    stats["n_positions"] = len(positions)
    stats["n_pending_cleanup"] = len(pending)
    if not positions and not pending:
        log("quiet: no positions and no pending_cleanup")
        stats["reason"] = "empty"
        write_last(stats)
        return 0

    try:
        pairs: dict[str, dict[str, Any]] = {}
        if positions:
            pairs = fetch_best_pairs(list(positions.keys()))
    except FetchError as e:
        log(f"quiet: fetch failure ({e})")
        stats["reason"] = "fetch_failure"
        stats["error"] = str(e)
        write_last(stats)
        return 0

    events: list[dict[str, Any]] = []
    per_position: list[dict[str, Any]] = []
    missing_pairs = 0
    for mint, rec in positions.items():
        pair = pairs.get(mint)
        if not pair:
            missing_pairs += 1
            per_position.append(
                {
                    "mint": mint,
                    "name": rec.get("name"),
                    "mc": None,
                    "liq": None,
                    "tp_mc": num(rec.get("tp_mc")),
                    "sl_mc": num(rec.get("sl_mc")),
                    "entry_liq": num(rec.get("entry_liq")),
                    "pair_missing": True,
                }
            )
            continue
        evs = position_events(mint, rec, pair)
        events.extend(evs)
        per_position.append(
            {
                "mint": mint,
                "name": rec.get("name") or evs[0]["name"] if evs else rec.get("name"),
                "mc": market_cap(pair),
                "liq": liq_usd(pair),
                "tp_mc": num(rec.get("tp_mc")),
                "sl_mc": num(rec.get("sl_mc")),
                "entry_liq": num(rec.get("entry_liq")),
                "bought_at": rec.get("bought_at"),
                "dexscreener_url": pair.get("url") or f"https://dexscreener.com/solana/{mint}",
            }
        )

    if pending:
        events.append(cleanup_event(pending))

    stats["missing_pairs"] = missing_pairs
    stats["events_raw"] = [e.get("dedupe_key") for e in events]

    dedupe = load_dedupe()
    fresh = [e for e in events if e.get("dedupe_key") not in dedupe]
    skipped = [e.get("dedupe_key") for e in events if e.get("dedupe_key") in dedupe]
    stats["skipped_dedupe"] = skipped
    stats["events_fresh"] = [e.get("dedupe_key") for e in fresh]
    log(
        f"positions={len(positions)} pending_cleanup={len(pending)} "
        f"events={len(events)} fresh={len(fresh)} skipped_dedupe={len(skipped)} "
        f"missing_pairs={missing_pairs}"
    )

    evidence = {
        "task_id": TASK_ID,
        "run_id": run_id,
        "ts": now_ts(),
        "ts_local": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "positions_file": str(POSITIONS_PATH),
        "n_positions": len(positions),
        "n_pending_cleanup": len(pending),
        "missing_pairs": missing_pairs,
        "events_all": events,
        "events_fresh": fresh,
        "skipped_dedupe": skipped,
        "positions": per_position,
        "pending_cleanup": pending,
        "notes": [
            "This script never writes positions.json (Pano-owned).",
            "Market cap uses DexScreener marketCap, falling back to fdv.",
            "Largest-liquidity Solana pair per mint (tokens/v1/solana).",
            "Liquidity wake: liq < 50% of entry_liq or liq < $10K.",
            "Same condition per mint (or cleanup hash) wakes at most once per 6h.",
        ],
    }

    if not fresh:
        log("quiet: no fresh position events")
        stats["reason"] = "no_fresh_events" if events else "no_events"
        stats["ok"] = True
        ev_path = EVIDENCE_DIR / f"gmgn-positions-{run_id}-quiet.json"
        atomic_write(ev_path, json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
        stats["evidence_path"] = str(ev_path)
        write_last(stats)
        return 0

    ev_path = EVIDENCE_DIR / f"gmgn-positions-{run_id}.json"
    atomic_write(ev_path, json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")

    metrics_events = []
    for e in fresh:
        slim = {
            "type": e.get("type"),
            "mint": e.get("mint"),
            "name": e.get("name"),
            "detail": e.get("detail"),
            "severity": e.get("severity"),
            "action": e.get("action"),
        }
        if e.get("type") == "cleanup":
            slim["count"] = e.get("count")
            slim["items"] = e.get("items")
        else:
            slim.update({k: e.get(k) for k in ("mc", "liq", "tp_mc", "sl_mc")})
        metrics_events.append(slim)

    payload = {
        "schema_version": 1,
        "task_id": TASK_ID,
        "severity": severity_of(fresh),
        "summary": build_summary(fresh),
        "evidence_path": evidence_ref(ev_path),
        "recommended_action": recommended_action(fresh),
        "dedupe_key": "gmgn-positions:" + ",".join(str(e["dedupe_key"]) for e in fresh),
        "changed": True,
        "metrics": {
            "events": metrics_events,
            "positions": [
                {k: p.get(k) for k in ("mint", "name", "mc", "liq", "tp_mc", "sl_mc", "entry_liq")}
                for p in per_position
            ],
            **runner_metrics(),
        },
    }
    save_dedupe(
        {
            str(e["dedupe_key"]): {
                "ts": now_ts(),
                "run_id": run_id,
                "type": e.get("type"),
            }
            for e in fresh
        }
    )
    stats["reason"] = "wake"
    stats["ok"] = True
    stats["evidence_path"] = str(ev_path)
    stats["payload_summary"] = payload["summary"]
    write_last(stats)
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    log(f"wake {len(fresh)} event(s) evidence={ev_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception as e:
        log(f"quiet: unexpected {type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stderr)
        write_last({"reason": "unexpected_error", "error": f"{type(e).__name__}: {e}"})
        sys.exit(0)
