#!/usr/bin/env python3
"""Numeric prefilter for Pano's "GMGN 找新機會" (task_id: gmgn-candidates).

Runs mechanically (curl/urllib + stdlib only; no LLM, no trades, no webhooks).
Discovers Solana token mints from DexScreener public endpoints, applies
numeric pool A/B/D/E thresholds, and prints one v1 wake payload to stdout
when at least one mint passes and is not 24h-deduped.

Quiet exit 0 (empty stdout) when:
  - holdings are already 2, or buy budget < 0.1 SOL
  - no token passes, or all passers were recently woken
  - a required fetch (RPC holdings/budget, or all pair data) fails

DexScreener from this box works; GMGN API and pump.fun APIs return 403.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Paths: every path can be overridden by env so the same code runs on the box
# (defaults = current box layout) and in GitHub Actions (repo-relative data/).
#   GMGN_INPUTS_DIR  -> narratives.json, watch.json, watch-state.json, rejected.json
#   per-file overrides: GMGN_NARRATIVES_FILE, GMGN_WATCH_FILE, GMGN_WATCH_STATE_FILE,
#                       GMGN_REJECTED_FILE
#   QUOTA_SAVE_STATE_DIR -> mint-dedupe.json (and notify-dedupe.json via lib_notify.sh)
#   GMGN_EVIDENCE_DIR, GMGN_RUNSTATS_DIR, QUOTA_SAVE_LOG_DIR -> scratch output
ROOT = Path(os.environ.get("QUOTA_SAVE_ROOT", "/workspace/quota-save"))
STATE_DIR = Path(os.environ.get("QUOTA_SAVE_STATE_DIR", str(ROOT / "state")))
LOG_DIR = Path(os.environ.get("QUOTA_SAVE_LOG_DIR", str(ROOT / "logs")))
EVIDENCE_DIR = Path(os.environ.get("GMGN_EVIDENCE_DIR", str(STATE_DIR / "evidence")))
RUNSTATS_DIR = Path(os.environ.get("GMGN_RUNSTATS_DIR", str(STATE_DIR)))
_INPUTS_DIR = os.environ.get("GMGN_INPUTS_DIR", "").strip()


def _input_path(env_key: str, inputs_name: str, box_default: str) -> Path:
    if os.environ.get(env_key):
        return Path(os.environ[env_key])
    if _INPUTS_DIR:
        return Path(_INPUTS_DIR) / inputs_name
    return Path(box_default)


NARRATIVES_PATH = _input_path("GMGN_NARRATIVES_FILE", "narratives.json", "/workspace/gmgn-narratives.json")
WATCH_PATH = _input_path("GMGN_WATCH_FILE", "watch.json", "/workspace/watch-gmgn-exit/watch.json")
WATCH_STATE_PATH = _input_path("GMGN_WATCH_STATE_FILE", "watch-state.json", "/workspace/watch-gmgn-exit/state.json")
REJECTED_PATH = _input_path("GMGN_REJECTED_FILE", "rejected.json", str(STATE_DIR / "rejected.json"))
MINT_DEDUPE_PATH = STATE_DIR / "mint-dedupe.json"
LAST_RUN_PATH = RUNSTATS_DIR / "gmgn-prefilter-last.json"

# GitHub Actions mode: evidence files stay on the ephemeral runner, so the
# payload must be self-contained (details already live in metrics.candidates).
IN_GHA = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"


def evidence_ref(local_path: Path) -> str:
    if IN_GHA:
        run_id = os.environ.get("GITHUB_RUN_ID", "local")
        return f"github-actions run {run_id}"
    return str(local_path)


def runner_name() -> str:
    return os.environ.get("GMGN_RUNNER") or ("github-actions" if IN_GHA else "box")

TASK_ID = "gmgn-candidates"
RECOMMENDED_ACTION = "verify_R1_R2_R4_R5_on_gmgn_then_buy"
WALLET1 = "6gDeDQtmuYYjoRH4GJ8PpeSMxowZ9aZsEqLFktNMgs7z"
RPC_URL = os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
DEX_BASE = "https://api.dexscreener.com"
UA = "quota-save-gmgn-prefilter/1.0 (box numeric filter; +https://dexscreener.com/api-docs)"

# Known quote-side mints on Solana; never treat these as candidate tokens.
QUOTE_MINTS = {
    "So11111111111111111111111111111111111111112",  # WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB",
}
HOLDINGS_EXCLUDE_NAMES = {"SWJ", "NIHAO"}
# If buy/peak amount is unknown, uiAmount <= this is treated as dust (near zero).
# Documented heuristic — can under-count leftover meme-coin dust with huge supplies.
# Prefer Pano's watch.json to drop closed positions; if watch-gmgn-exit/state.json
# has a peak for the mint, use peak * 1% instead (same rule as the exit watcher).
DUST_UI_THRESHOLD = 1e-6
MINT_DEDUPE_TTL = 24 * 3600
MAX_WAKE_CANDIDATES = 5
# "spec" = thresholds exactly as specified (default).
# "strict" = recommended tightening so wakes stay rare; see apply_strict_pools().
PREFILTER_MODE = os.environ.get("GMGN_PREFILTER_MODE", "spec").strip().lower()
TOKENS_BATCH = 30
HTTP_TIMEOUT = 20
HTTP_GAP_SEC = 0.2
MAX_RETRIES = 2

# Quietly ignored names that must never be counted as open holdings.
# NIHAO is dust; SWJ is cancelled.


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
        with (LOG_DIR / "gmgn-prefilter.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def http_get_json(url: str, *, retries: int = MAX_RETRIES) -> Any:
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                raw = resp.read()
            time.sleep(HTTP_GAP_SEC)
            return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            code = e.code
            try:
                e.read()
            except Exception:
                pass
            if code == 429 and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise FetchError(f"HTTP {code} for {url.split('?')[0]}") from e
        except Exception as e:  # timeout, DNS, JSON
            last_err = e
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise FetchError(f"{type(e).__name__} for {url.split('?')[0]}") from e
    raise FetchError(str(last_err))


def rpc(method: str, params: list[Any]) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        RPC_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": UA,
        },
    )
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if "error" in data:
                raise FetchError(f"RPC {method} error")
            return data["result"]
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise FetchError(f"RPC {method} failed: {type(e).__name__}") from e
    raise FetchError(str(last_err))


def sol_balance(wallet: str) -> float:
    return rpc("getBalance", [wallet])["value"] / 1e9


def token_ui_amount(wallet: str, mint: str) -> float:
    res = rpc(
        "getTokenAccountsByOwner",
        [wallet, {"mint": mint}, {"encoding": "jsonParsed"}],
    )
    total = 0.0
    for acc in res.get("value") or []:
        try:
            amt = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"]
            total += float(amt or 0)
        except (KeyError, TypeError, ValueError):
            continue
    return total


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def bought_mints() -> set[str]:
    data = load_json(NARRATIVES_PATH, {})
    out: set[str] = set()
    bought = data.get("bought") if isinstance(data, dict) else None
    if not isinstance(bought, list):
        return out
    for item in bought:
        if isinstance(item, dict):
            c = item.get("contract") or item.get("mint") or item.get("tokenAddress")
            if isinstance(c, str) and c.strip():
                out.add(c.strip())
        elif isinstance(item, str) and item.strip():
            out.add(item.strip())
    return out


def rejected_mints() -> set[str]:
    data = load_json(REJECTED_PATH, {"mints": []})
    out: set[str] = set()
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("mints") or data.get("rejected") or []
    else:
        items = []
    for item in items:
        if isinstance(item, str) and item.strip():
            out.add(item.strip())
        elif isinstance(item, dict):
            c = item.get("mint") or item.get("contract") or item.get("tokenAddress")
            if isinstance(c, str) and c.strip():
                out.add(c.strip())
    return out


def load_watch() -> dict[str, Any]:
    data = load_json(WATCH_PATH, {})
    return data if isinstance(data, dict) else {}


def load_watch_peaks() -> dict[str, float]:
    """Read-only peek at watch-gmgn-exit/state.json peaks. Never writes there."""
    data = load_json(WATCH_STATE_PATH, {})
    peaks: dict[str, float] = set_peaks(data)
    return peaks


def set_peaks(data: Any) -> dict[str, float]:
    peaks: dict[str, float] = {}
    if not isinstance(data, dict):
        return peaks
    for mint, rec in data.items():
        if mint.startswith("_") or not isinstance(rec, dict):
            continue
        try:
            peak = float(rec.get("peak") or 0)
        except (TypeError, ValueError):
            continue
        if peak > 0:
            peaks[mint] = peak
    return peaks


def is_meaningful_holding(ui_amount: float, peak: float | None) -> bool:
    if peak is not None and peak > 0:
        return ui_amount > peak * 0.01
    # Buy amount unknown: treat dust as UI amount near zero.
    return ui_amount > DUST_UI_THRESHOLD


def holdings_and_budget() -> tuple[int, float, float, list[dict[str, Any]]]:
    """Return (n_holdings, sol, budget_sol, details). Quiet-exit gates applied by caller."""
    watch = load_watch()
    wallet = watch.get("wallet") or WALLET1
    tokens = watch.get("tokens") if isinstance(watch.get("tokens"), dict) else {}
    peaks = load_watch_peaks()
    sol = sol_balance(wallet)
    details = []
    n = 0
    for name, mint in tokens.items():
        if str(name).upper() in HOLDINGS_EXCLUDE_NAMES:
            details.append({"name": name, "mint": mint, "excluded": True, "reason": "SWJ_or_NIHAO"})
            continue
        if not isinstance(mint, str) or not mint:
            continue
        ui = token_ui_amount(wallet, mint)
        peak = peaks.get(mint)
        meaningful = is_meaningful_holding(ui, peak)
        rec = {
            "name": name,
            "mint": mint,
            "ui_amount": ui,
            "peak": peak,
            "meaningful": meaningful,
            "heuristic": "peak_1pct" if peak else "near_zero_ui",
        }
        details.append(rec)
        if meaningful:
            n += 1
    if n == 0:
        budget = (sol - 0.02) / 2.0
    elif n == 1:
        budget = sol - 0.02
    else:
        budget = 0.0
    return n, sol, budget, details


def candidate_mint_from_pair(pair: dict[str, Any]) -> str | None:
    if pair.get("chainId") != "solana":
        return None
    base = (pair.get("baseToken") or {}).get("address") or ""
    quote = (pair.get("quoteToken") or {}).get("address") or ""
    if base in QUOTE_MINTS and quote and quote not in QUOTE_MINTS:
        return quote
    if base and base not in QUOTE_MINTS:
        return base
    if quote and quote not in QUOTE_MINTS:
        return quote
    return None


def liq_usd(pair: dict[str, Any]) -> float:
    liq = pair.get("liquidity") or {}
    if isinstance(liq, dict):
        try:
            return float(liq.get("usd") or 0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(liq or 0)
    except (TypeError, ValueError):
        return 0.0


def num(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def age_hours(pair: dict[str, Any], now_ms: int) -> float | None:
    created = pair.get("pairCreatedAt")
    created_f = num(created)
    if created_f is None or created_f <= 0:
        return None
    # DexScreener pairCreatedAt is milliseconds since epoch.
    return (now_ms - created_f) / 3_600_000.0


def market_cap(pair: dict[str, Any]) -> float | None:
    mc = num(pair.get("marketCap"))
    if mc is not None and mc > 0:
        return mc
    fdv = num(pair.get("fdv"))
    if fdv is not None and fdv > 0:
        return fdv
    return None


def keep_best_pair(store: dict[str, dict[str, Any]], mint: str, pair: dict[str, Any]) -> None:
    prev = store.get(mint)
    if prev is None or liq_usd(pair) > liq_usd(prev):
        store[mint] = pair


def collect_discovery() -> tuple[set[str], dict[str, list[str]], dict[str, str], list[str]]:
    """Return (mints, sources_by_mint, endpoint_status, notes)."""
    mints: set[str] = set()
    sources: dict[str, list[str]] = {}
    status: dict[str, str] = {}
    notes: list[str] = []

    def add_mint(mint: str, source: str) -> None:
        if not mint or mint in QUOTE_MINTS:
            return
        mints.add(mint)
        sources.setdefault(mint, [])
        if source not in sources[mint]:
            sources[mint].append(source)

    def add_from_token_list(items: Any, source: str) -> int:
        n = 0
        if not isinstance(items, list):
            return 0
        for it in items:
            if not isinstance(it, dict):
                continue
            if (it.get("chainId") or it.get("chain") or "").lower() != "solana":
                continue
            addr = it.get("tokenAddress") or it.get("token") or it.get("address")
            if isinstance(addr, str) and addr:
                add_mint(addr, source)
                n += 1
        return n

    endpoints = [
        ("profiles_latest", f"{DEX_BASE}/token-profiles/latest/v1", "list"),
        ("boosts_latest", f"{DEX_BASE}/token-boosts/latest/v1", "list"),
        ("boosts_top", f"{DEX_BASE}/token-boosts/top/v1", "list"),
        ("search_jensen", f"{DEX_BASE}/latest/dex/search?q=jensen", "search"),
        ("search_huang", f"{DEX_BASE}/latest/dex/search?q=huang", "search"),
        # pump.fun graduated is 403 from this box; pumpswap search is a modest
        # stand-in for recently migrated PumpSwap pairs.
        ("search_pumpswap", f"{DEX_BASE}/latest/dex/search?q=pumpswap", "search"),
    ]

    for name, url, kind in endpoints:
        try:
            data = http_get_json(url)
            if kind == "list":
                n = add_from_token_list(data, name)
                status[name] = f"ok:{n}"
            else:
                pairs = data.get("pairs") if isinstance(data, dict) else None
                n = 0
                if isinstance(pairs, list):
                    for p in pairs:
                        if not isinstance(p, dict):
                            continue
                        mint = candidate_mint_from_pair(p)
                        if mint:
                            add_mint(mint, name)
                            n += 1
                status[name] = f"ok:{n}"
        except FetchError as e:
            status[name] = f"fail:{e}"
            log(f"discovery {name} failed: {e}")

    # Document pump.fun unavailability once (no extra request in production after a probe flag).
    # We still try a single graduated-style URL only if explicitly enabled.
    if os.environ.get("GMGN_TRY_PUMPFUN", "0") == "1":
        pump_url = (
            "https://frontend-api-v3.pump.fun/coins"
            "?offset=0&limit=50&sort=market_cap&order=DESC&includeNsfw=false&complete=true"
        )
        try:
            data = http_get_json(pump_url)
            n = 0
            items = data if isinstance(data, list) else (data.get("data") or data.get("coins") or [])
            for it in items or []:
                if not isinstance(it, dict):
                    continue
                addr = it.get("mint") or it.get("address")
                if isinstance(addr, str) and addr:
                    add_mint(addr, "pumpfun_graduated")
                    n += 1
            status["pumpfun_graduated"] = f"ok:{n}"
        except FetchError as e:
            status["pumpfun_graduated"] = f"fail:{e}"
            notes.append("pump.fun graduated API not reachable from this box")
    else:
        status["pumpfun_graduated"] = "skipped:known_403_cloudflare"
        notes.append("pump.fun APIs return 403 (Cloudflare) from this box; skipped")

    return mints, sources, status, notes


def fetch_pairs(mints: list[str]) -> dict[str, dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    failures = 0
    for i in range(0, len(mints), TOKENS_BATCH):
        batch = mints[i : i + TOKENS_BATCH]
        url = f"{DEX_BASE}/tokens/v1/solana/{','.join(batch)}"
        try:
            data = http_get_json(url)
        except FetchError as e:
            failures += 1
            log(f"tokens/v1 batch {i // TOKENS_BATCH} failed: {e}")
            continue
        if not isinstance(data, list):
            continue
        for pair in data:
            if not isinstance(pair, dict) or pair.get("chainId") != "solana":
                continue
            mint = candidate_mint_from_pair(pair)
            if not mint:
                continue
            keep_best_pair(best, mint, pair)
    if not best and mints:
        raise FetchError("no pair data from tokens/v1/solana")
    if failures and not best:
        raise FetchError("all tokens/v1 batches failed")
    return best


def classify_pools(pair: dict[str, Any], now_ms: int) -> tuple[list[str], list[str], dict[str, Any]]:
    """Return (matched_pools, needs_check, metrics). Common gates apply to every pool."""
    liq = liq_usd(pair)
    mc = market_cap(pair)
    age_h = age_hours(pair, now_ms)
    pc = pair.get("priceChange") or {}
    vol = pair.get("volume") or {}
    m5 = num(pc.get("m5")) if isinstance(pc, dict) else None
    h1 = num(pc.get("h1")) if isinstance(pc, dict) else None
    h24 = num(pc.get("h24")) if isinstance(pc, dict) else None
    vol_h1 = num(vol.get("h1")) if isinstance(vol, dict) else None
    vol_h24 = num(vol.get("h24")) if isinstance(vol, dict) else None

    metrics = {
        "mc": mc,
        "liq": liq,
        "age_h": None if age_h is None else round(age_h, 3),
        "m5": m5,
        "h1": h1,
        "h24": h24,
        "vol_h1": vol_h1,
        "vol_h24": vol_h24,
    }

    # Missing m5: cannot confirm a 5m dump, so R6/B-m5 are treated as pass
    # (unknown ≠ observed drop). Documented in evidence.notes.
    m5_r6 = m5 if m5 is not None else 0.0
    m5_b = m5 if m5 is not None else 0.0

    common = (
        liq >= 50_000
        and age_h is not None
        and age_h >= 2.0
        and m5_r6 > -20.0
    )
    pools: list[str] = []
    needs_check: list[str] = []
    if not common or mc is None:
        return pools, needs_check, metrics

    # A old: MC>$1M, liq>$100K, age>=3d, h1>-15%, -30%<=h24<=+150%
    if (
        mc > 1_000_000
        and liq > 100_000
        and age_h >= 72.0
        and h1 is not None
        and h1 > -15.0
        and h24 is not None
        and -30.0 <= h24 <= 150.0
    ):
        pools.append("A")

    # B new: age>=2h, MC>=$150K, liq>=$50K, -20%<=h1<=+80%, m5>-10
    if (
        age_h >= 2.0
        and mc >= 150_000
        and liq >= 50_000
        and h1 is not None
        and -20.0 <= h1 <= 80.0
        and m5_b > -10.0
    ):
        pools.append("B")

    # D dip: age>=12h, MC>=$150K, h24 volume>=$100K, h1>-5%
    # ATH-based checks (ATH MC >= $500K, now MC 50–80% below ATH) are not
    # available from DexScreener → mark needs_check but still numeric-pass.
    if (
        age_h >= 12.0
        and mc >= 150_000
        and vol_h24 is not None
        and vol_h24 >= 100_000
        and h1 is not None
        and h1 > -5.0
    ):
        pools.append("D")
        needs_check.extend(
            [
                "ath_mc_gte_500k_unavailable",
                "ath_drawdown_50_80_unavailable",
                "kline_bottoming_is_judgment",
            ]
        )

    # E dormant breakout: age 3–21d, MC $200K–$2M, h1 0..+50%,
    # h1 volume >= 5x average hourly volume over h24 (volume.h24/24).
    avg_hourly = (vol_h24 / 24.0) if (vol_h24 is not None and vol_h24 > 0) else None
    if (
        72.0 <= age_h <= 21 * 24.0
        and 200_000 <= mc <= 2_000_000
        and h1 is not None
        and 0.0 <= h1 <= 50.0
        and vol_h1 is not None
        and avg_hourly is not None
        and vol_h1 >= 5.0 * avg_hourly
    ):
        pools.append("E")

    return pools, needs_check, metrics



def apply_strict_pools(
    pools: list[str],
    sources: list[str],
    age_h: float | None,
) -> list[str]:
    """Tighten pool membership so established DexScreener large-caps do not wake every run.

    - B (新幣): also require age < 3 days (A already starts at >= 3d; stop the overlap).
    - A (老幣): only if the mint came from narrative search (jensen/huang).
    - D: do not qualify on its own (ATH fields are unavailable / needs_check).
    - E: unchanged.
    - search_pumpswap is only a stand-in for new migrated pairs: if a token's
      only source is search_pumpswap and age >= 3d, drop it in strict mode.
    """
    if not pools:
        return []
    src = set(sources or [])
    narrative = bool(src & {"search_jensen", "search_huang"})
    only_pumpswap = bool(src) and src <= {"search_pumpswap"}
    out: list[str] = []
    if "B" in pools and age_h is not None and age_h < 72.0:
        out.append("B")
    if "E" in pools:
        out.append("E")
    if "A" in pools and narrative:
        out.append("A")
    if "D" in pools and out:
        out.append("D")
    if only_pumpswap and (age_h is None or age_h >= 72.0):
        return []
    return out


def load_mint_dedupe() -> dict[str, int]:
    data = load_json(MINT_DEDUPE_PATH, {})
    keys = data.get("mints") if isinstance(data, dict) else {}
    if not isinstance(keys, dict):
        return {}
    now = now_ts()
    out: dict[str, int] = {}
    for mint, rec in keys.items():
        ts: int | None = None
        if isinstance(rec, (int, float)):
            ts = int(rec)
        elif isinstance(rec, dict) and rec.get("ts") is not None:
            try:
                ts = int(rec["ts"])
            except (TypeError, ValueError):
                ts = None
        if ts is not None and now - ts < MINT_DEDUPE_TTL:
            out[mint] = ts
    return out


def save_mint_dedupe(entries: dict[str, dict[str, Any]]) -> None:
    # Merge with existing non-expired.
    current = load_mint_dedupe()
    merged: dict[str, Any] = {
        mint: {"ts": ts} for mint, ts in current.items()
    }
    merged.update(entries)
    atomic_write(MINT_DEDUPE_PATH, json.dumps({"mints": merged}, indent=2) + "\n")


def symbol_of(pair: dict[str, Any], mint: str) -> str:
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    if base.get("address") == mint:
        return str(base.get("symbol") or "?")
    if quote.get("address") == mint:
        return str(quote.get("symbol") or "?")
    return str(base.get("symbol") or "?")


def name_of(pair: dict[str, Any], mint: str) -> str:
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    if base.get("address") == mint:
        return str(base.get("name") or "")
    if quote.get("address") == mint:
        return str(quote.get("name") or "")
    return str(base.get("name") or "")


def build_payload(
    selected: list[dict[str, Any]],
    evidence_path: str,
    holdings: int,
    budget: float,
    pool_counts: dict[str, int],
) -> dict[str, Any]:
    n = len(selected)
    parts = [f"{k}:{pool_counts.get(k, 0)}" for k in ("A", "B", "D", "E") if pool_counts.get(k, 0)]
    pools_s = ",".join(parts) if parts else "none"
    syms = ", ".join(c["symbol"] for c in selected)
    summary = (
        f"{n} SOL candidate(s) passed numeric prefilter ({pools_s}); "
        f"holdings={holdings} budget={budget:.3f} SOL: {syms}"
    )
    metrics_cands = []
    for c in selected:
        metrics_cands.append(
            {
                "mint": c["mint"],
                "symbol": c["symbol"],
                "pool": c["pool"],
                "mc": c["mc"],
                "liq": c["liq"],
                "age_h": c["age_h"],
                "h1": c["h1"],
                "m5": c["m5"],
                "dexscreener_url": c["dexscreener_url"],
            }
        )
    dedupe_key = "gmgn-candidates:" + ",".join(c["mint"] for c in selected)
    return {
        "schema_version": 1,
        "task_id": TASK_ID,
        "severity": "info",
        "summary": summary,
        "evidence_path": evidence_path,
        "recommended_action": RECOMMENDED_ACTION,
        "dedupe_key": dedupe_key,
        "changed": True,
        "metrics": {
            "candidates": metrics_cands,
            "holdings": holdings,
            "budget_sol": round(budget, 6),
            **runner_metrics(),
        },
    }


def runner_metrics() -> dict[str, Any]:
    """Small runner tag; in GitHub mode also a link to the run (details are inline)."""
    out: dict[str, Any] = {"runner": runner_name()}
    if IN_GHA:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        if repo and run_id:
            out["run_url"] = f"{server}/{repo}/actions/runs/{run_id}"
        out["details_inline"] = True
    return out


def run() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    RUNSTATS_DIR.mkdir(parents=True, exist_ok=True)

    run_id = time.strftime("%Y%m%d-%H%M%S")
    now_ms = int(time.time() * 1000)
    stats: dict[str, Any] = {"run_id": run_id, "ok": False}

    try:
        n_hold, sol, budget, hold_details = holdings_and_budget()
        stats["holdings"] = n_hold
        stats["sol"] = sol
        stats["budget_sol"] = budget
        log(f"holdings={n_hold} sol={sol:.6f} budget={budget:.6f}")
        if n_hold >= 2:
            log("quiet: already 2 holdings")
            stats["reason"] = "holdings_full"
            atomic_write(LAST_RUN_PATH, json.dumps(stats, indent=2) + "\n")
            return 0
        if budget < 0.1:
            log("quiet: buy budget < 0.1 SOL")
            stats["reason"] = "budget_below_0.1"
            atomic_write(LAST_RUN_PATH, json.dumps(stats, indent=2) + "\n")
            return 0

        bought = bought_mints()
        rejected = rejected_mints()
        stats["bought_excluded"] = len(bought)
        stats["rejected_excluded"] = len(rejected)

        mints, sources, endpoint_status, notes = collect_discovery()
        stats["endpoint_status"] = endpoint_status
        stats["discovered"] = len(mints)
        log(f"discovered {len(mints)} unique SOL mints; endpoints={endpoint_status}")

        # Exclude bought / rejected / quote mints before spending tokens/v1 calls.
        excluded = []
        filtered = []
        for m in sorted(mints):
            if m in bought:
                excluded.append({"mint": m, "reason": "bought"})
            elif m in rejected:
                excluded.append({"mint": m, "reason": "rejected"})
            else:
                filtered.append(m)
        stats["after_exclusions"] = len(filtered)

        if not filtered:
            log("quiet: no mints left after exclusions (or discovery empty)")
            stats["reason"] = "no_mints"
            # If every discovery endpoint failed, this is a fetch failure — still exit 0.
            atomic_write(LAST_RUN_PATH, json.dumps(stats, indent=2) + "\n")
            return 0

        pairs = fetch_pairs(filtered)
        scanned = []
        classified: list[dict[str, Any]] = []
        passed: list[dict[str, Any]] = []
        pool_counts = {"A": 0, "B": 0, "D": 0, "E": 0}
        missing_pair = 0
        for mint in filtered:
            pair = pairs.get(mint)
            if not pair:
                missing_pair += 1
                continue
            pools_spec, needs_check, metrics = classify_pools(pair, now_ms)
            srcs = sources.get(mint, [])
            pools_strict = apply_strict_pools(pools_spec, srcs, metrics.get("age_h"))
            pools = pools_strict if PREFILTER_MODE == "strict" else pools_spec
            rec = {
                "mint": mint,
                "symbol": symbol_of(pair, mint),
                "name": name_of(pair, mint),
                "pools_spec": pools_spec,
                "pools_strict": pools_strict,
                "pools": pools,
                "pool": ",".join(pools) if pools else "",
                "needs_check": needs_check,
                "sources": srcs,
                "dexscreener_url": pair.get("url") or f"https://dexscreener.com/solana/{mint}",
                "dex_id": pair.get("dexId"),
                "pair_address": pair.get("pairAddress"),
                **metrics,
            }
            classified.append(rec)
            scanned.append({k: rec[k] for k in ("mint", "symbol", "pools", "pools_spec", "pools_strict", "mc", "liq", "age_h", "h1", "m5")})
            if pools:
                passed.append(rec)
                for p in pools:
                    if p in pool_counts:
                        pool_counts[p] += 1

        stats["mode"] = PREFILTER_MODE
        stats["scanned_with_pairs"] = len(scanned)
        stats["missing_pair"] = missing_pair
        stats["passed"] = len(passed)
        stats["pool_counts"] = pool_counts
        passed_spec = [c for c in classified if c.get("pools_spec")]
        passed_strict = [c for c in classified if c.get("pools_strict")]
        stats["passed_spec"] = len(passed_spec)
        stats["passed_strict"] = len(passed_strict)
        stats["passed_strict_symbols"] = [c["symbol"] for c in passed_strict]
        log(
            f"mode={PREFILTER_MODE} scanned={len(scanned)} passed={len(passed)} "
            f"pools={pool_counts} missing_pair={missing_pair} "
            f"passed_spec={len(passed_spec)} passed_strict={len(passed_strict)}"
        )

        dedupe = load_mint_dedupe()
        fresh = [c for c in passed if c["mint"] not in dedupe]
        skipped_dedupe = [c["mint"] for c in passed if c["mint"] in dedupe]
        stats["passed_fresh"] = len(fresh)
        stats["skipped_dedupe"] = len(skipped_dedupe)

        # Prefer more specific / new-opportunity pools when sorting is tied.
        pool_rank = {"B": 0, "E": 1, "D": 2, "A": 3}

        def sort_key(c: dict[str, Any]) -> tuple:
            ranks = [pool_rank.get(p, 9) for p in c["pools"]]
            best_rank = min(ranks) if ranks else 9
            return (best_rank, -(c["liq"] or 0))

        # Task: sorted by liquidity. Use liquidity as the primary sort;
        # pool rank only as a tie-break so "top 5 by liquidity" holds.
        fresh.sort(key=lambda c: (-(c["liq"] or 0), sort_key(c)[0]))
        selected = fresh[:MAX_WAKE_CANDIDATES]
        stats["selected"] = [c["mint"] for c in selected]

        evidence = {
            "task_id": TASK_ID,
            "run_id": run_id,
            "ts": now_ts(),
            "ts_local": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "holdings": n_hold,
            "sol": sol,
            "budget_sol": budget,
            "holdings_details": hold_details,
            "holdings_heuristic": {
                "exclude_names": sorted(HOLDINGS_EXCLUDE_NAMES),
                "if_peak_known": "uiAmount > peak * 0.01 (same as watch-gmgn-exit)",
                "if_buy_unknown": f"uiAmount > {DUST_UI_THRESHOLD:g} counts as a holding; at or below is dust (near zero)",
            },
            "endpoint_status": endpoint_status,
            "notes": notes
            + [
                "GMGN API is not used (403 from this box).",
                "Missing priceChange.m5 is treated as 0 (unknown ≠ a observed dump).",
                "Market cap uses DexScreener marketCap, falling back to fdv.",
                "Largest-liquidity Solana pair per token (tokens/v1/solana batch).",
                "Pool D ATH conditions cannot be checked from DexScreener; marked needs_check.",
                "Pool C is judgment-based and is not applied here.",
                "GMGN_PREFILTER_MODE=spec (default) uses the specified thresholds as-is.",
                "GMGN_PREFILTER_MODE=strict caps B at age<3d, limits A to jensen/huang hits, "
                "drops D-only, and ignores search_pumpswap tokens aged >=3d.",
            ],
            "mode": PREFILTER_MODE,
            "discovered_mints": len(mints),
            "after_exclusions": len(filtered),
            "scanned": len(scanned),
            "pool_counts": pool_counts,
            "passed_spec_count": len(passed_spec),
            "passed_strict_count": len(passed_strict),
            "passed_strict_symbols": [c["symbol"] for c in passed_strict],
            "excluded": excluded,
            "skipped_dedupe_mints": skipped_dedupe,
            "passed_all": passed,
            "selected": selected,
        }

        if not selected:
            log("quiet: no fresh candidates after pool filter / 24h mint dedupe")
            stats["reason"] = "no_fresh_candidates"
            stats["ok"] = True
            ev_path = EVIDENCE_DIR / f"gmgn-candidates-{run_id}-quiet.json"
            atomic_write(ev_path, json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
            stats["evidence_path"] = str(ev_path)
            atomic_write(LAST_RUN_PATH, json.dumps(stats, indent=2, default=str) + "\n")
            return 0

        ev_path = EVIDENCE_DIR / f"gmgn-candidates-{run_id}.json"
        atomic_write(ev_path, json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
        payload = build_payload(selected, evidence_ref(ev_path), n_hold, budget, {
            k: sum(1 for c in selected if k in c["pools"]) for k in ("A", "B", "D", "E")
        })
        # Mark only the mints we are actually waking about, even in DRY_RUN
        # (the shell never POSTs when DRY_RUN=1, but we must not re-wake them).
        save_mint_dedupe(
            {
                c["mint"]: {"ts": now_ts(), "symbol": c["symbol"], "run_id": run_id}
                for c in selected
            }
        )
        stats["reason"] = "wake"
        stats["ok"] = True
        stats["evidence_path"] = str(ev_path)
        stats["payload_summary"] = payload["summary"]
        atomic_write(LAST_RUN_PATH, json.dumps(stats, indent=2, default=str) + "\n")
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        log(f"wake {len(selected)} candidate(s) evidence={ev_path}")
        return 0

    except FetchError as e:
        log(f"quiet: fetch failure ({e})")
        stats["reason"] = "fetch_failure"
        stats["error"] = str(e)
        atomic_write(LAST_RUN_PATH, json.dumps(stats, indent=2, default=str) + "\n")
        return 0
    except Exception as e:
        log(f"quiet: unexpected {type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stderr)
        stats["reason"] = "unexpected_error"
        stats["error"] = f"{type(e).__name__}: {e}"
        atomic_write(LAST_RUN_PATH, json.dumps(stats, indent=2, default=str) + "\n")
        return 0


if __name__ == "__main__":
    sys.exit(run())
