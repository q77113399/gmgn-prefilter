#!/usr/bin/env python3
"""Smart-money follow watcher for Pano's GMGN SOL trading.

Mechanical only (stdlib + public Solana RPC + DexScreener). Never trades.
Polls followed wallets for new token buys and for sells of tokens we hold,
pre-filters on-box, and POSTs a v1 wake (task_id gmgn-smart-money) to gmgn-4.

Never prints webhook URLs, keys, bearer tokens, or env values.
Box-only state; do not sync this directory to the public repo.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("SMART_MONEY_ROOT", "/workspace/smart-money"))
WALLETS_PATH = Path(os.environ.get("SMART_MONEY_WALLETS", str(ROOT / "wallets.json")))
STATE_PATH = Path(os.environ.get("SMART_MONEY_STATE", str(ROOT / "state.json")))
EVIDENCE_DIR = Path(os.environ.get("SMART_MONEY_EVIDENCE", str(ROOT / "evidence")))
LOCK_PATH = Path(os.environ.get("SMART_MONEY_LOCK", str(ROOT / "watch.lock")))
PID_PATH = Path(os.environ.get("SMART_MONEY_PID", str(ROOT / "watch.pid")))
REJECTED_PATH = Path(os.environ.get("GMGN_REJECTED", "/workspace/quota-save/state/rejected.json"))
POSITIONS_PATH = Path(os.environ.get("GMGN_POSITIONS_FILE", "/workspace/quota-save/state/positions.json"))
WATCH_PATH = Path(os.environ.get("GMGN_BOX_WATCH", "/workspace/watch-gmgn-exit/watch.json"))
SECRETS_FILE = Path(os.environ.get("QUOTA_SAVE_SECRETS", "/workspace/quota-save/secrets/webhook.env"))

RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
DEX_BASE = "https://api.dexscreener.com"
TASK_ID = "gmgn-smart-money"
UA = "pano-smart-money-watch/1.0"
TZ = timezone(timedelta(hours=8))
POLL_SEC = int(os.environ.get("SMART_MONEY_INTERVAL_SEC", "90"))
MIN_BUY_SOL = float(os.environ.get("SMART_MONEY_MIN_BUY_SOL", "0.5"))
MIN_LIQ_USD = float(os.environ.get("SMART_MONEY_MIN_LIQ_USD", "20000"))
WAKE_GAP_SEC = int(os.environ.get("SMART_MONEY_WAKE_GAP_SEC", "600"))
SIG_LIMIT = 40
# A/B source tag: "box" (default, daemon) or "gha" (GitHub Actions single poll).
SOURCE = (os.environ.get("SMART_MONEY_SOURCE") or "box").strip().lower()
# Keys (sig:mint:side) the OTHER runner already woke gmgn-4 for -> never re-wake.
SKIP_PATH = Path(os.environ.get("SMART_MONEY_SKIP", str(ROOT / "gha-woken.json")))
SOURCES_LOG = Path(os.environ.get("SMART_MONEY_SOURCES_LOG", "/workspace/quota-save/logs/sm-sources.jsonl"))
PRIVATE_SYNC = Path(os.environ.get("PRIVATE_STATE_PY", "/workspace/gmgn-gha/scripts/private_state.py"))
GHA_LOOKBACK_SEC = int(os.environ.get("SMART_MONEY_GHA_LOOKBACK_SEC", "900"))    # no cursor yet
GHA_MAX_AGE_SEC = int(os.environ.get("SMART_MONEY_GHA_MAX_AGE_SEC", "3600"))    # never wake older
HTTP_TIMEOUT = 25

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
# Stablecoins, LSTs, wrapped majors — never treat as follow-buy targets.
MAJORS = {
    WSOL, USDC, USDT,
    "USDSwr9ApdHk5bvJKMjz82sDXSNjtoxqAeqKrsPE1FiC",  # USDS
    "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB",  # USD1
    "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",  # PYUSD
    "J1toso1uCk3RLmjor3oaW8d2oJX5dMXdtNxWEmZ2xPk",  # JitoSOL (common)
    "J1toso1uCk3RLmjjf3tsqHgQ6gqEQQeSM3U6pGEJ",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",
    "bSo13r4TkiE4KumL71LsHTPpL2euSHQxkqzhk5Rh59o",
    "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v",
    "7Q2afV64in6N6FeZgowsCNE2XkPXo55kmsuBfaWAjah9",  # MSOL variants ignored if wrong
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",  # WBTC
    "cbbtcf3aa214zXHbiAZQwf4122FBYbeRk9sZBErEsgHs",  # cbBTC
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",  # WETH
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "4k3Dyjzvzp8eT9Y3NPHDUI4qCtctX1YTNq2yrqECCedk",  # RAY (legacy)
    "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",
    "HZ1JovNiVvGrGNiiYvEozEVgZ58xaEu9VYn3oSWNqN2",  # PYTH
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",  # BONK
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",  # WIF
    "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN",  # TRUMP
}

# Cached DexScreener pair info: mint -> dict or None
_dex_cache: dict[str, dict[str, Any] | None] = {}
_sol_price: float | None = None
_sol_price_ts = 0.0


def now_ts() -> int:
    return int(time.time())


def taipei(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def log(msg: str) -> None:
    print(f"{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S %Z')} {msg}", flush=True)


def atomic_write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text() or "null")
    except Exception:
        return default


def load_secrets() -> tuple[str, str]:
    """Return (url, bearer). Never logs or prints the values."""
    url = os.environ.get("WEBHOOK_URL") or ""
    bearer = os.environ.get("WEBHOOK_BEARER") or ""
    if path_is := SECRETS_FILE.is_file():
        for raw in SECRETS_FILE.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                val = val[1:-1]
            if key == "WEBHOOK_URL" and not url:
                url = val
            elif key == "WEBHOOK_BEARER" and not bearer:
                bearer = val
    return url, bearer


def ev_key(e: dict[str, Any]) -> str:
    return f"{e.get('sig')}:{e.get('mint')}:{e.get('side')}"


def skip_keys() -> set[str]:
    data = load_json(SKIP_PATH, {})
    keys = data.get("keys") if isinstance(data, dict) else None
    return set(keys) if isinstance(keys, dict) else set()


def source_log(rec: dict[str, Any]) -> None:
    if SOURCE != "box":
        return
    try:
        SOURCES_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SOURCES_LOG.open("a") as f:
            f.write(json.dumps({"ts": now_ts(), "source": SOURCE, **rec}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def private_sync(push: bool, wait: bool) -> None:
    """Box only: refresh GHA-woken keys / push the encrypted box blob. Best effort."""
    if SOURCE != "box" or not PRIVATE_SYNC.is_file():
        return
    cmd = ["python3", str(PRIVATE_SYNC), "box-sync"] + ([] if push else ["--no-push"])
    try:
        if wait:
            import subprocess
            subprocess.run(cmd, timeout=60, capture_output=True)
        else:
            import subprocess
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        log(f"private sync failed: {type(e).__name__}")


def rpc(method: str, params: list[Any], retries: int = 6) -> Any:
    last: Any = None
    for attempt in range(retries):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(
            RPC_URL, data=body, headers={"Content-Type": "application/json", "User-Agent": UA}
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                data = json.loads(r.read().decode())
            if "error" in data:
                last = data["error"]
                time.sleep(0.6 + attempt * 0.5)
                continue
            return data.get("result")
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            time.sleep(1.0 + attempt * 0.8)
        except Exception as e:
            last = str(e)
            time.sleep(0.6 + attempt * 0.5)
    raise RuntimeError(f"{method} failed: {last}")


def http_json(url: str, timeout: int = 15) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def load_wallets() -> list[dict[str, Any]]:
    data = load_json(WALLETS_PATH, {})
    wallets = data.get("wallets") if isinstance(data, dict) else data
    out = []
    for w in wallets or []:
        if isinstance(w, dict) and w.get("address") and w.get("enabled", True):
            out.append(w)
        elif isinstance(w, str):
            out.append({"address": w})
    return out


def rejected_mints() -> set[str]:
    data = load_json(REJECTED_PATH, {})
    out: set[str] = set()
    if isinstance(data, list):
        out.update(x for x in data if isinstance(x, str))
    elif isinstance(data, dict):
        mints = data.get("mints")
        if isinstance(mints, list):
            out.update(x for x in mints if isinstance(x, str))
        reasons = data.get("reasons")
        if isinstance(reasons, dict):
            out.update(k for k in reasons if isinstance(k, str))
        for k in data:
            if isinstance(k, str) and 32 <= len(k) <= 44 and k not in ("mints", "note", "reasons"):
                out.add(k)
    return out


def held_mints() -> set[str]:
    out: set[str] = set()
    pos = load_json(POSITIONS_PATH, {})
    if isinstance(pos, dict):
        inner = pos.get("positions")
        if isinstance(inner, dict):
            out.update(k for k in inner if isinstance(k, str))
        elif isinstance(inner, list):
            for rec in inner:
                if isinstance(rec, dict):
                    m = rec.get("mint") or rec.get("contract")
                    if isinstance(m, str):
                        out.add(m)
    watch = load_json(WATCH_PATH, {})
    if isinstance(watch, dict):
        tokens = watch.get("tokens")
        if isinstance(tokens, dict):
            out.update(k for k in tokens if isinstance(k, str))
    return out


def load_state() -> dict[str, Any]:
    st = load_json(STATE_PATH, {})
    if not isinstance(st, dict):
        st = {}
    st.setdefault("cursors", {})          # wallet -> last processed signature
    st.setdefault("initialized", {})      # wallet -> bool
    st.setdefault("seen_sigs", {})        # sig -> ts
    st.setdefault("pending", [])          # queued events waiting for wake gap
    st.setdefault("last_wake_ts", 0)
    return st


def prune_seen(st: dict[str, Any], ttl: int = 14 * 86400) -> None:
    now = now_ts()
    for k in ("woken_keys", "detected_keys"):
        st[k] = {a: int(b) for a, b in (st.get(k) or {}).items() if isinstance(b, (int, float)) and now - int(b) < 3 * 86400}
    seen = st.get("seen_sigs") or {}
    st["seen_sigs"] = {k: int(v) for k, v in seen.items() if isinstance(v, (int, float)) and now - int(v) < ttl}


def acquire_lock() -> int:
    ROOT.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise SystemExit("watch already running (lock held)")
    return fd


def token_map(lst: Any) -> dict[tuple[str, str], float]:
    d: dict[tuple[str, str], float] = {}
    for b in lst or []:
        if not isinstance(b, dict):
            continue
        owner = b.get("owner")
        mint = b.get("mint")
        ui = (b.get("uiTokenAmount") or {}).get("uiAmount") or 0
        if isinstance(owner, str) and isinstance(mint, str):
            d[(owner, mint)] = float(ui or 0)
    return d


def account_keys(tx: dict[str, Any]) -> list[str]:
    msg = ((tx.get("transaction") or {}).get("message") or {})
    keys = []
    for k in msg.get("accountKeys") or []:
        keys.append(k.get("pubkey") if isinstance(k, dict) else k)
    return [k for k in keys if isinstance(k, str)]


def parse_wallet_changes(wallet: str, tx: dict[str, Any]) -> dict[str, Any]:
    meta = tx.get("meta") or {}
    keys = account_keys(tx)
    pre = token_map(meta.get("preTokenBalances"))
    post = token_map(meta.get("postTokenBalances"))
    changes: list[tuple[str, float]] = []
    for owner, mint in set(pre) | set(post):
        if owner != wallet:
            continue
        delta = post.get((owner, mint), 0) - pre.get((owner, mint), 0)
        if abs(delta) > 1e-12:
            changes.append((mint, delta))
    sol_delta = 0.0
    if wallet in keys:
        i = keys.index(wallet)
        pre_b = meta.get("preBalances") or []
        post_b = meta.get("postBalances") or []
        if i < len(pre_b) and i < len(post_b):
            sol_delta = (post_b[i] - pre_b[i]) / 1e9
    wsol = sum(d for m, d in changes if m == WSOL)
    usdc = sum(d for m, d in changes if m == USDC)
    usdt = sum(d for m, d in changes if m == USDT)
    bought = [(m, d) for m, d in changes if d > 0 and m not in MAJORS]
    sold = [(m, d) for m, d in changes if d < 0 and m not in MAJORS]
    quote_sol = 0.0
    if sol_delta < 0:
        quote_sol += -sol_delta
    if wsol < 0:
        quote_sol += -wsol
    return {
        "sol_delta": sol_delta,
        "wsol_delta": wsol,
        "usdc_delta": usdc,
        "usdt_delta": usdt,
        "quote_sol": quote_sol,
        "bought": bought,
        "sold": sold,
    }


def sol_usd() -> float | None:
    global _sol_price, _sol_price_ts
    if _sol_price and time.time() - _sol_price_ts < 300:
        return _sol_price
    try:
        data = http_json(f"{DEX_BASE}/tokens/v1/solana/{WSOL}")
        pairs = data if isinstance(data, list) else []
        best = None
        for p in pairs:
            if not isinstance(p, dict):
                continue
            if p.get("quoteToken", {}).get("address") in (USDC, USDT):
                px = p.get("priceUsd")
                liq = ((p.get("liquidity") or {}).get("usd")) or 0
                if px and (best is None or liq > best[0]):
                    best = (float(liq), float(px))
        if best:
            _sol_price, _sol_price_ts = best[1], time.time()
            return _sol_price
    except Exception as e:
        log(f"sol price fetch failed: {e}")
    return _sol_price


def dex_pair(mint: str) -> dict[str, Any] | None:
    if mint in _dex_cache:
        return _dex_cache[mint]
    try:
        data = http_json(f"{DEX_BASE}/tokens/v1/solana/{mint}")
        pairs = [p for p in (data if isinstance(data, list) else []) if isinstance(p, dict) and p.get("chainId") == "solana"]
        best = None
        best_liq = -1.0
        for p in pairs:
            liq = float(((p.get("liquidity") or {}).get("usd")) or 0)
            # Prefer the pair where mint is the base token
            base = ((p.get("baseToken") or {}).get("address")) or ""
            score = liq + (1e12 if base == mint else 0)
            if score > best_liq:
                best_liq = score
                best = p
        _dex_cache[mint] = best
        return best
    except Exception as e:
        log(f"dexscreener {mint[:8]} failed: {e}")
        _dex_cache[mint] = None
        return None


def pair_metrics(mint: str, token_amount: float | None = None) -> dict[str, Any]:
    p = dex_pair(mint) or {}
    liq = float(((p.get("liquidity") or {}).get("usd")) or 0)
    mc = p.get("marketCap") or p.get("fdv") or 0
    try:
        mc = float(mc or 0)
    except (TypeError, ValueError):
        mc = 0.0
    created = p.get("pairCreatedAt") or 0
    age_h = None
    if created:
        try:
            age_h = round((time.time() * 1000 - float(created)) / 3600000, 2)
        except (TypeError, ValueError):
            age_h = None
    base = p.get("baseToken") or {}
    symbol = base.get("symbol") or mint[:8]
    price_native = p.get("priceNative")
    price_usd = p.get("priceUsd")
    size_sol = None
    if token_amount and price_native:
        try:
            # priceNative is usually quote-per-base; if base is the mint and quote is SOL, this is SOL
            quote = ((p.get("quoteToken") or {}).get("address")) or ""
            if quote == WSOL:
                size_sol = abs(float(token_amount) * float(price_native))
        except (TypeError, ValueError):
            size_sol = None
    if size_sol is None and token_amount and price_usd:
        sp = sol_usd()
        try:
            if sp:
                size_sol = abs(float(token_amount) * float(price_usd) / sp)
        except (TypeError, ValueError):
            pass
    return {
        "symbol": symbol,
        "liq": round(liq, 2),
        "mc": round(mc, 2),
        "age_h": age_h,
        "dexscreener_url": p.get("url") or f"https://dexscreener.com/solana/{mint}",
        "size_sol_from_price": round(size_sol, 4) if size_sol else None,
        "price_usd": float(price_usd) if price_usd else None,
    }


def get_new_sigs(wallet: str, cursor: str | None, lookback_ts: int | None, max_pages: int = 2) -> list[dict[str, Any]]:
    """Newest-first signatures until cursor, lookback, or page cap.

    Official RPC only. An empty first page is retried once (some public RPCs
    lie with empty lists; we still do not trust an empty result blindly).
    """
    out: list[dict[str, Any]] = []
    before = None
    for page in range(max_pages):
        opts: dict[str, Any] = {"limit": 1000}
        if before:
            opts["before"] = before
        batch = rpc("getSignaturesForAddress", [wallet, opts])
        if not batch:
            if page == 0:
                time.sleep(0.8)
                batch = rpc("getSignaturesForAddress", [wallet, opts]) or []
            if not batch:
                break
        stop = False
        for s in batch:
            sig = s.get("signature")
            ts = s.get("blockTime") or 0
            if cursor and sig == cursor:
                stop = True
                break
            if lookback_ts and ts and ts < lookback_ts:
                stop = True
                break
            out.append(s)
        if stop or len(batch) < 1000:
            break
        before = batch[-1]["signature"]
        time.sleep(0.15)
    return out


def classify_tx(
    wallet: str,
    tx: dict[str, Any],
    sig: str,
    ts: int | None,
    rejected: set[str],
    held: set[str],
) -> list[dict[str, Any]]:
    """Return zero or more candidate events (not yet rate-limited)."""
    changes = parse_wallet_changes(wallet, tx)
    events: list[dict[str, Any]] = []
    tlabel = taipei(ts) or "unknown"

    # Sells of tokens we currently hold — always interesting, no size/liq gate.
    for mint, delta in changes["sold"]:
        if mint in held:
            ev = {
                "side": "sell",
                "mint": mint,
                "wallets": [wallet],
                "sig": sig,
                "time": tlabel,
                "ts": ts or now_ts(),
                "token_amount": abs(delta),
                "sol_size": round(changes["quote_sol"], 4) if changes["quote_sol"] else None,
                "skip": None,
            }
            ev.update(pair_metrics(mint, abs(delta)))
            if ev.get("sol_size") is None and ev.get("size_sol_from_price"):
                ev["sol_size"] = ev["size_sol_from_price"]
            events.append(ev)

    # Buys of non-major tokens
    n_bought = len(changes["bought"])
    for mint, delta in changes["bought"]:
        ev = {
            "side": "buy",
            "mint": mint,
            "wallets": [wallet],
            "sig": sig,
            "time": tlabel,
            "ts": ts or now_ts(),
            "token_amount": delta,
            "sol_size": None,
            "skip": None,
        }
        if mint in rejected:
            ev["skip"] = "rejected.json"
            events.append(ev)
            continue
        metrics = pair_metrics(mint, delta)
        ev.update(metrics)
        # Size: prefer on-chain quote spend when this looks like a single-token buy
        quote = changes["quote_sol"]
        px = metrics.get("size_sol_from_price")
        if n_bought == 1 and quote and quote >= MIN_BUY_SOL * 0.5:
            ev["sol_size"] = round(quote, 4)
        elif px:
            ev["sol_size"] = px
        elif n_bought == 1 and quote:
            ev["sol_size"] = round(quote, 4)
        if ev["sol_size"] is None:
            ev["skip"] = "size_unknown"
        elif ev["sol_size"] < MIN_BUY_SOL:
            ev["skip"] = f"size {ev['sol_size']} < {MIN_BUY_SOL} SOL"
        elif (ev.get("liq") or 0) < MIN_LIQ_USD:
            ev["skip"] = f"liq ${ev.get('liq')} < ${MIN_LIQ_USD:.0f}"
        events.append(ev)
    return events


def build_payload(events: list[dict[str, Any]], evidence: Path) -> dict[str, Any]:
    buys = [e for e in events if e["side"] == "buy"]
    sells = [e for e in events if e["side"] == "sell"]
    parts = []
    if buys:
        bits = []
        for e in buys:
            bits.append(
                f"{e.get('symbol') or e['mint'][:8]} mint={e['mint']} "
                f"wallets={','.join(a[:6]+'…' for a in e['wallets'])} "
                f"size={e.get('sol_size')} SOL liq=${e.get('liq')} mc=${e.get('mc')} age_h={e.get('age_h')}"
            )
        parts.append(f"{len(buys)} smart-money buy(s): " + "; ".join(bits))
    if sells:
        bits = []
        for e in sells:
            bits.append(
                f"{e.get('symbol') or e['mint'][:8]} mint={e['mint']} "
                f"wallets={','.join(a[:6]+'…' for a in e['wallets'])} "
                f"size={e.get('sol_size')} SOL"
            )
        parts.append(f"{len(sells)} followed-wallet sell(s) of a token we hold: " + "; ".join(bits))
    summary = " | ".join(parts) if parts else "smart-money event"
    mints = [e["mint"] for e in events]
    action = "verify_then_follow_buy_0.1_to_0.2_sol" if buys else "review_sell_of_held_token"
    metrics_events = []
    for e in events:
        metrics_events.append({
            "side": e["side"],
            "mint": e["mint"],
            "symbol": e.get("symbol"),
            "wallets": e["wallets"],
            "sol_size": e.get("sol_size"),
            "time": e.get("time"),
            "ts": e.get("ts"),
            "sig": e.get("sig"),
            "mc": e.get("mc"),
            "liq": e.get("liq"),
            "age_h": e.get("age_h"),
            "dexscreener_url": e.get("dexscreener_url"),
        })
    return {
        "schema_version": 1,
        "task_id": TASK_ID,
        "severity": "warning" if sells and not buys else "info",
        "summary": summary,
        "evidence_path": str(evidence),
        "recommended_action": action,
        "dedupe_key": f"{TASK_ID}:{','.join(sorted(set(mints)))}:{int(events[0]['ts']) // 600}",
        "changed": True,
        "metrics": {
            "events": metrics_events,
            "runner": f"{SOURCE}-smart-money",
            "source": SOURCE,
        },
    }


def notify(payload: dict[str, Any], dry_run: bool) -> bool:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if dry_run:
        log("DRY_RUN would-be wake payload:")
        print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
        return True
    url, bearer = load_secrets()
    if not url or not bearer:
        log("notify skipped: webhook not configured")
        return False
    req = urllib.request.Request(
        url,
        data=raw.encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {bearer}",
            "User-Agent": UA,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:
        log(f"notify failed: {type(e).__name__}")
        return False
    log(f"notify http={code}")
    return 200 <= int(code) < 300


def merge_pending(pending: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe by sig+mint+side; merge wallets if same mint/side arrives twice."""
    out = list(pending)
    index = {(e.get("sig"), e.get("mint"), e.get("side")): i for i, e in enumerate(out)}
    for e in new:
        key = (e.get("sig"), e.get("mint"), e.get("side"))
        if key in index:
            prev = out[index[key]]
            for w in e.get("wallets") or []:
                if w not in prev["wallets"]:
                    prev["wallets"].append(w)
            continue
        # also merge same mint/side within the batch window (no sig required)
        merged = False
        for prev in out:
            if prev.get("mint") == e.get("mint") and prev.get("side") == e.get("side"):
                for w in e.get("wallets") or []:
                    if w not in prev["wallets"]:
                        prev["wallets"].append(w)
                merged = True
                break
        if not merged:
            index[key] = len(out)
            out.append(e)
    return out


def flush_wakes(st: dict[str, Any], dry_run: bool) -> None:
    pending = [e for e in (st.get("pending") or []) if not e.get("skip")]
    if not pending:
        return
    # Cross-runner dedupe: drop events the other runner (box <-> GHA) already woke for.
    if not dry_run:
        private_sync(push=False, wait=True)
    other = skip_keys()
    dups = [e for e in pending if ev_key(e) in other]
    if dups:
        log(f"skip {len(dups)} event(s) already woken by the other runner")
        pending = [e for e in pending if ev_key(e) not in other]
        st["pending"] = [e for e in (st.get("pending") or []) if ev_key(e) not in other]
        if not pending:
            return
    last = int(st.get("last_wake_ts") or 0)
    gap = now_ts() - last
    if last and gap < WAKE_GAP_SEC:
        log(f"rate-limit: {len(pending)} event(s) queued, next wake in {WAKE_GAP_SEC - gap}s")
        return
    ev_id = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    evidence = EVIDENCE_DIR / f"gmgn-smart-money-{ev_id}.json"
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    payload = build_payload(pending, evidence)
    evidence.write_text(json.dumps({"payload": payload, "events": pending}, indent=2, ensure_ascii=False) + "\n")
    ok = notify(payload, dry_run)
    if ok:
        st["last_wake_ts"] = now_ts()
        st["pending"] = []
        if not dry_run:
            wk = st.setdefault("woken_keys", {})
            for e in pending:
                wk[ev_key(e)] = now_ts()
            source_log({"event": "wake", "keys": [ev_key(e) for e in pending]})
            private_sync(push=True, wait=False)
        log(f"wake {'DRY_RUN ' if dry_run else ''}ok events={len(pending)} evidence={evidence}")
    else:
        log("wake POST failed; pending kept for retry")


def poll_once(st: dict[str, Any], dry_run: bool, lookback_ts: int | None, max_tx: int) -> dict[str, Any]:
    wallets = load_wallets()
    if not wallets:
        log("no enabled wallets in wallets.json")
        return st
    rejected = rejected_mints()
    held = held_mints()
    stats = {"wallets": 0, "new_sigs": 0, "txs": 0, "buys": 0, "sells": 0, "skipped": 0, "errors": 0}
    new_events: list[dict[str, Any]] = []

    for w in wallets:
        addr = w["address"]
        stats["wallets"] += 1
        initialized = bool((st.get("initialized") or {}).get(addr))
        cursor = (st.get("cursors") or {}).get(addr)
        try:
            # First live run: record the tip and do not backfill historical buys.
            # Dry-run with lookback may inspect history without moving the live cursor
            # unless we are also initializing.
            if SOURCE == "gha" and not lookback_ts:
                # Single GHA poll: since cursor, never older than GHA_MAX_AGE; no cursor -> 15 min.
                lb = now_ts() - (GHA_MAX_AGE_SEC if initialized and cursor else GHA_LOOKBACK_SEC)
                sigs = get_new_sigs(addr, cursor if initialized else None, lb)
                if not sigs and not cursor:
                    tip = rpc("getSignaturesForAddress", [addr, {"limit": 1}]) or []
                    if tip:
                        st.setdefault("cursors", {})[addr] = tip[0]["signature"]
                        st.setdefault("initialized", {})[addr] = True
            elif not initialized and not lookback_ts:
                tip = rpc("getSignaturesForAddress", [addr, {"limit": 1}]) or []
                if tip:
                    st.setdefault("cursors", {})[addr] = tip[0]["signature"]
                st.setdefault("initialized", {})[addr] = True
                log(f"{addr[:8]}… cursor initialized (no backfill)")
                time.sleep(0.1)
                continue
            else:
                sigs = get_new_sigs(addr, cursor if initialized else None, lookback_ts)
        except Exception as e:
            log(f"{addr[:8]}… sigs error: {e}")
            stats["errors"] += 1
            continue
        # process oldest-first so cursors move forward cleanly
        sigs = list(reversed(sigs))
        stats["new_sigs"] += len(sigs)
        processed = 0
        last_ok = cursor
        for s in sigs:
            sig = s.get("signature")
            if not sig:
                continue
            if (st.get("seen_sigs") or {}).get(sig):
                last_ok = sig
                continue
            if s.get("err"):
                st.setdefault("seen_sigs", {})[sig] = now_ts()
                last_ok = sig
                continue
            if processed >= max_tx:
                log(f"{addr[:8]}… hit max_tx={max_tx}, remaining sigs deferred")
                break
            try:
                tx = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
            except Exception as e:
                log(f"{addr[:8]}… tx error: {e}")
                stats["errors"] += 1
                time.sleep(0.6)
                break  # retry this sig next poll; do not advance cursor past it
            processed += 1
            stats["txs"] += 1
            st.setdefault("seen_sigs", {})[sig] = now_ts()
            last_ok = sig
            if tx:
                evs = classify_tx(addr, tx, sig, s.get("blockTime"), rejected, held)
                for ev in evs:
                    if ev.get("skip"):
                        stats["skipped"] += 1
                        log(
                            f"skip {ev['side']} {ev.get('symbol') or ev['mint'][:8]} "
                            f"{ev['mint']} {addr[:8]}… reason={ev['skip']}"
                        )
                        continue
                    if ev["side"] == "buy":
                        stats["buys"] += 1
                    else:
                        stats["sells"] += 1
                    log(
                        f"qualify {ev['side']} {ev.get('symbol')} mint={ev['mint']} "
                        f"wallet={addr[:8]}… size={ev.get('sol_size')} SOL "
                        f"liq=${ev.get('liq')} mc=${ev.get('mc')} t={ev.get('time')}"
                    )
                    new_events.append(ev)
                    dk = st.setdefault("detected_keys", {})
                    if ev_key(ev) not in dk:
                        dk[ev_key(ev)] = now_ts()
                        source_log({"event": "detect", "key": ev_key(ev), "side": ev["side"],
                                    "mint": ev["mint"], "symbol": ev.get("symbol"), "event_ts": ev.get("ts")})
            time.sleep(0.2)
        if last_ok and not lookback_ts:
            st.setdefault("cursors", {})[addr] = last_ok
            st.setdefault("initialized", {})[addr] = True
        time.sleep(0.1)

    if new_events:
        st["pending"] = merge_pending(st.get("pending") or [], new_events)
    flush_wakes(st, dry_run)
    log(
        f"poll wallets={stats['wallets']} new_sigs={stats['new_sigs']} txs={stats['txs']} "
        f"buys={stats['buys']} sells={stats['sells']} skipped={stats['skipped']} errors={stats['errors']} "
        f"pending={len(st.get('pending') or [])}"
    )
    return st


def write_pid() -> None:
    PID_PATH.write_text(str(os.getpid()) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Smart-money follow watcher")
    ap.add_argument("mode", choices=["once", "loop"], help="one poll or daemon loop")
    ap.add_argument("--dry-run", action="store_true", help="print would-be wakes, do not POST")
    ap.add_argument("--lookback-hours", type=float, default=0, help="inspect this many hours of history (dry-run / test)")
    ap.add_argument("--max-tx", type=int, default=30, help="max getTransaction calls per wallet per poll")
    ap.add_argument("--interval", type=int, default=POLL_SEC, help="loop interval seconds")
    args = ap.parse_args()
    dry = args.dry_run or os.environ.get("DRY_RUN") == "1"
    lookback_ts = int(time.time() - args.lookback_hours * 3600) if args.lookback_hours > 0 else None

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = acquire_lock()
    write_pid()
    log(
        f"start mode={args.mode} dry_run={int(dry)} interval={args.interval}s "
        f"lookback_h={args.lookback_hours} min_buy={MIN_BUY_SOL} SOL min_liq=${MIN_LIQ_USD:.0f} "
        f"wake_gap={WAKE_GAP_SEC}s rpc=mainnet-beta"
    )
    st = load_state()
    try:
        if args.mode == "once":
            st = poll_once(st, dry, lookback_ts, args.max_tx)
            prune_seen(st)
            if not dry or not lookback_ts:
                atomic_write(STATE_PATH, st)
            elif dry:
                # Keep pending/cursors from a pure historical dry-run out of the live state
                log("dry-run lookback: live state.json not overwritten")
            return 0
        while True:
            try:
                st = load_state()
                st = poll_once(st, dry, None, args.max_tx)
                prune_seen(st)
                atomic_write(STATE_PATH, st)
            except Exception:
                log("poll error:\n" + traceback.format_exc())
            time.sleep(max(15, args.interval))
    finally:
        try:
            os.close(lock_fd)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
