#!/usr/bin/env python3
"""Numeric prefilter for Pano's small-bet early-launch GMGN SOL strategy.

task_id:
  gmgn-early            buy candidates (top 1-3)
  gmgn-early-positions  TP / SL / timeout on early.json positions

Runs mechanically (urllib + stdlib only; no LLM, no trades, no webhooks).
Prints one v1 wake payload to stdout when there is something to wake.
Quiet exit 0 otherwise, or when --dry-run / DRY_RUN=1 (prints a summary
instead of a wake payload).

Discovery: DexScreener profiles/boosts/narrative search, GeckoTerminal
new_pools (cached until they age into the 5-55m wake window; strategy
is 5-60m, cut at 55m so gmgn-4 is not handed a stale coin). Token age
is the earliest DexScreener pairCreatedAt across ALL pools (pumpfun
included), not the PumpSwap graduation time. 5m price change < -30%
is rejected. pump.fun frontend APIs are 403 from this box and are skipped.

Box-only state:
  /workspace/quota-save/state/early.json          budget / positions / seen
  /workspace/quota-save/state/early-discovery.json  harvest cache
Never copy those into the public GitHub repo or the synced
positions.json / rejected.json files.

Sellability and GMGN DEV-scan / dev blacklist are left to gmgn-4.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("QUOTA_SAVE_ROOT", "/workspace/quota-save"))
STATE_DIR = Path(os.environ.get("QUOTA_SAVE_STATE_DIR", str(ROOT / "state")))
LOG_DIR = Path(os.environ.get("QUOTA_SAVE_LOG_DIR", str(ROOT / "logs")))
EVIDENCE_DIR = Path(os.environ.get("GMGN_EARLY_EVIDENCE_DIR", str(STATE_DIR / "evidence")))
NARRATIVES_PATH = Path(os.environ.get("GMGN_NARRATIVES_PATH", "/workspace/gmgn-narratives.json"))
REJECTED_PATH = Path(os.environ.get("GMGN_REJECTED_PATH", str(STATE_DIR / "rejected.json")))
EARLY_PATH = STATE_DIR / "early.json"
# A/B source tag. "box" (default) = box loop; "gha" = GitHub Actions early
# workflow. In gha mode STATE_DIR is runner temp (early.json there is a
# throwaway default: no positions, no budget), and dedupe uses the PUBLIC
# file GHA_SEEN_PATH (mints + timestamps only). In box mode GHA_SEEN_PATH is a
# local cache of that public file; mints GHA already woke are not re-woken.
EARLY_SOURCE = (os.environ.get("GMGN_EARLY_SOURCE") or "box").strip().lower()
GHA_SEEN_PATH = Path(os.environ.get("GMGN_GHA_SEEN_PATH", str(STATE_DIR / "early-seen-gha.cache.json")))
SOURCES_LOG = LOG_DIR / "early-sources.jsonl"
DISCOVERY_PATH = STATE_DIR / "early-discovery.json"
LAST_RUN_PATH = STATE_DIR / "gmgn-early-last.json"

TASK_BUY = "gmgn-early"
TASK_POS = "gmgn-early-positions"
RECOMMENDED_BUY = "verify_sellability_and_dev_on_gmgn_then_buy_0.05_sol"
RECOMMENDED_POS = "execute_early_position_rule"

DEX_BASE = "https://api.dexscreener.com"
GECKO_BASE = "https://api.geckoterminal.com/api/v2"
RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"
UA = "quota-save-gmgn-early/1.0 (box numeric filter; +https://dexscreener.com/api-docs)"

WSOL = "So11111111111111111111111111111111111111112"
QUOTE_MINTS = {
    WSOL,
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB",
}
PUMP_DEX = {"pumpfun", "pump-fun", "pumpswap", "pump-swap"}
BURN_OR_SYSTEM = {
    "11111111111111111111111111111111",
    "1nc1nerator11111111111111111111111111111111",
    "So11111111111111111111111111111111111111112",
}

AGE_MIN_MINUTES = 5.0
# Strategy window is 5-60m; cut at 55m so a candidate is not stale by the
# time gmgn-4 opens GMGN (loop interval + check time).
AGE_MAX_MINUTES = 55.0
M5_DUMP_MIN = -30.0
MC_MIN = 10_000.0
MC_MAX = 100_000.0
TOP10_MAX_PCT = 30.0
INSIDER_MAX_PCT = 20.0
MAX_OPEN_POSITIONS = 3
BET_SOL = 0.05
DEFAULT_BUDGET_SOL = 0.5
MAX_WAKE_CANDIDATES = 3
DISCOVERY_TTL_SEC = 90 * 60
SEEN_TTL_SEC = 24 * 3600
HARVEST_GECKO_PAGES = 4
TOKENS_BATCH = 30
HTTP_TIMEOUT = 20
HTTP_GAP_SEC = 0.2
MAX_RETRIES = 2
POSITION_TIMEOUT_SEC = 6 * 3600
TRAIL_FRACTION = 0.5  # after tp1, stop = max(1.0x, peak x 0.5)

# Holder-concentration / unlocked-LP danger names are expected on early
# pump.fun coins; we compute top10 ourselves and LP burn is intentionally
# relaxed. Other rugcheck danger flags still fail the prefilter.
IGNORED_DANGER_NAMES = {
    "single holder ownership",
    "high holder concentration",
    "top 10 holders",
    "lp not locked",
    "low liquidity",
    "low amount of lp providers",
    "liquidity too low",
}

# Standing priority + AI. Extra keywords are loaded from gmgn-narratives.json.
PRIORITY_KEYWORDS = [
    "jensen",
    "huang",
    "nvidia",
    "nvdia",
    "nvda",
    "jacket",
    "黃仁勳",
    "仁勳",
]
AI_WORD_RE = re.compile(
    r"(?i)(?:^|[^a-z0-9])(ai|a\.i\.|openai|chatgpt|gpt-?4|gpt-?5|\bgpt\b|"
    r"grok|claude|anthropic|llm|cuda|\bgpu\b|deepseek|gemini|chatgpt)(?:[^a-z0-9]|$)"
)
EXTRA_NARRATIVE_DEFAULT = [
    "mihao",
    "nihao",
    "pino",
    "gta6",
    "gta vi",
    "gta",
    "leonida",
    "bawsaq",
    "funders",
]

AMM_TYPES = {"AMM", "POOL", "LP", "LOCKER", "BONDING_CURVE", "BONDING CURVE"}


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
        with (LOG_DIR / "gmgn-early.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def source_log(rec: dict[str, Any]) -> None:
    """Append one A/B record (box: logs/early-sources.jsonl)."""
    rec = {"ts": now_ts(), "ts_local": time.strftime("%Y-%m-%d %H:%M:%S %Z"), "source": EARLY_SOURCE, **rec}
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with SOURCES_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def load_gha_seen() -> dict[str, Any]:
    data = load_json(GHA_SEEN_PATH, {})
    mints = data.get("mints") if isinstance(data, dict) else None
    return mints if isinstance(mints, dict) else {}


def save_gha_seen(mints: dict[str, Any]) -> None:
    now = now_ts()
    keep = {m: r for m, r in mints.items() if isinstance(r, dict) and now - int(r.get("ts") or 0) < 3 * 86400}
    atomic_write(GHA_SEEN_PATH, json.dumps({"mints": dict(sorted(keep.items()))}, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


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
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise FetchError(f"{type(e).__name__} for {url.split('?')[0]}") from e
    raise FetchError(str(last_err))


def num(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def liq_usd(pair: dict[str, Any]) -> float:
    liq = pair.get("liquidity") or {}
    if isinstance(liq, dict):
        return num(liq.get("usd"), 0.0) or 0.0
    return num(liq, 0.0) or 0.0


def market_cap(pair: dict[str, Any]) -> float | None:
    mc = num(pair.get("marketCap"))
    if mc is not None and mc > 0:
        return mc
    fdv = num(pair.get("fdv"))
    if fdv is not None and fdv > 0:
        return fdv
    return None


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


def pair_has_sol(pair: dict[str, Any]) -> bool:
    base = (pair.get("baseToken") or {}).get("address") or ""
    quote = (pair.get("quoteToken") or {}).get("address") or ""
    return base == WSOL or quote == WSOL


def is_pump_family(dex_id: str, mint: str) -> bool:
    d = (dex_id or "").lower()
    if d in PUMP_DEX:
        return True
    # Just-graduated pump.fun mint that moved to Raydium / Meteora.
    if isinstance(mint, str) and mint.endswith("pump"):
        return True
    return False


def default_early() -> dict[str, Any]:
    return {
        "budget_sol": DEFAULT_BUDGET_SOL,
        "spent_sol": 0,
        "positions": {},
        "seen": {},
        "note": (
            "Box-only early-launch state. Do not copy to the public "
            "gmgn-prefilter repo or synced positions.json/rejected.json. "
            "gmgn-4 updates positions and spent_sol after a buy."
        ),
    }


def load_early() -> dict[str, Any]:
    data = load_json(EARLY_PATH, None)
    if not isinstance(data, dict):
        data = default_early()
    base = default_early()
    for k, v in base.items():
        if k not in data:
            data[k] = v
    if not isinstance(data.get("positions"), dict):
        data["positions"] = {}
    if not isinstance(data.get("seen"), dict):
        data["seen"] = {}
    if not isinstance(data.get("position_events"), dict):
        data["position_events"] = {}
    try:
        data["budget_sol"] = float(data.get("budget_sol") or DEFAULT_BUDGET_SOL)
    except (TypeError, ValueError):
        data["budget_sol"] = DEFAULT_BUDGET_SOL
    try:
        data["spent_sol"] = float(data.get("spent_sol") or 0)
    except (TypeError, ValueError):
        data["spent_sol"] = 0.0
    return data


def save_early(data: dict[str, Any]) -> None:
    atomic_write(EARLY_PATH, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def ensure_early_file() -> dict[str, Any]:
    EARLY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not EARLY_PATH.exists():
        data = default_early()
        save_early(data)
        return data
    return load_early()


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


def prune_seen(seen: dict[str, Any], now: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for mint, rec in seen.items():
        ts: int | None = None
        if isinstance(rec, (int, float)):
            ts = int(rec)
        elif isinstance(rec, dict):
            try:
                ts = int(rec.get("ts") or 0) or None
            except (TypeError, ValueError):
                ts = None
        if ts is not None and now - ts < SEEN_TTL_SEC:
            out[mint] = rec if isinstance(rec, dict) else {"ts": ts}
    return out


def load_narrative_keywords() -> list[str]:
    extra = list(EXTRA_NARRATIVE_DEFAULT)
    data = load_json(NARRATIVES_PATH, {})
    names = []
    if isinstance(data, dict):
        items = data.get("narratives") or []
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and it.get("name"):
                    names.append(str(it["name"]))
                elif isinstance(it, str):
                    names.append(it)
    # Pull a few distinctive tokens from the narratives file; skip generic words.
    stop = {
        "the", "and", "for", "with", "from", "that", "this", "on-chain", "on",
        "to", "of", "in", "a", "an", "or", "by", "is", "are", "led", "hot",
        "list", "mostly", "weak", "market", "rotation", "tokens", "token",
        "memes", "meme", "standing", "priority", "reportedly", "fighting",
        "his", "job", "interim", "talk", "comeback", "discussed", "shilled",
        "cat", "animal", "gaming", "chinese", "solana", "stablecoins", "rwa",
    }
    for name in names:
        for word in re.split(r"[^A-Za-z0-9]+", name):
            w = word.strip().lower()
            if len(w) >= 4 and w not in stop and w not in extra:
                extra.append(w)
    # Dedup, keep order.
    seen: set[str] = set()
    out: list[str] = []
    for w in extra:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def narrative_score(
    symbol: str,
    name: str,
    extra_keywords: list[str],
    sources: list[str] | None = None,
) -> tuple[int, list[str]]:
    text = f"{symbol or ''} {name or ''}".strip()
    low = text.lower()
    score = 0
    reasons: list[str] = []
    for kw in PRIORITY_KEYWORDS:
        if kw.lower() in low:
            score += 100
            reasons.append(f"priority:{kw}")
    if AI_WORD_RE.search(text):
        score += 50
        reasons.append("ai")
    for kw in extra_keywords:
        if len(kw) >= 3 and kw.lower() in low:
            # Don't double-count priority/AI already applied.
            if kw.lower() in {k.lower() for k in PRIORITY_KEYWORDS} or kw.lower() in {
                "ai", "openai", "nvidia", "jensen", "huang"
            }:
                continue
            score += 30
            reasons.append(f"narrative:{kw}")
            if len(reasons) >= 6:
                break
    src = set(sources or [])
    if src & {"search_jensen", "search_huang", "search_nvidia"}:
        score += 20
        reasons.append("narrative_search")
    return score, reasons


def can_wake_buy(early: dict[str, Any]) -> tuple[bool, str]:
    positions = early.get("positions") or {}
    n = 0
    for mint, rec in positions.items():
        if not mint or not isinstance(rec, dict):
            continue
        n += 1
    if n >= MAX_OPEN_POSITIONS:
        return False, f"open_positions={n}>={MAX_OPEN_POSITIONS}"
    spent = float(early.get("spent_sol") or 0)
    budget = float(early.get("budget_sol") or DEFAULT_BUDGET_SOL)
    if spent + BET_SOL > budget + 1e-12:
        return False, f"spent={spent}+{BET_SOL}>budget={budget}"
    return True, f"open={n} spent={spent} budget={budget}"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def add_mint(
    store: dict[str, dict[str, Any]],
    mint: str,
    source: str,
    *,
    symbol: str = "",
    dex: str = "",
    fdv: float | None = None,
    created_ts: int | None = None,
) -> None:
    if not mint or mint in QUOTE_MINTS:
        return
    rec = store.get(mint)
    if rec is None:
        rec = {
            "first_seen": now_ts(),
            "sources": [],
            "symbol": symbol or "",
            "dex": dex or "",
            "fdv": fdv,
            "created_ts": created_ts,
        }
        store[mint] = rec
    if source and source not in rec["sources"]:
        rec["sources"].append(source)
    if symbol and not rec.get("symbol"):
        rec["symbol"] = symbol
    if dex and not rec.get("dex"):
        rec["dex"] = dex
    if fdv is not None:
        prev = num(rec.get("fdv"))
        if prev is None or fdv > prev:
            rec["fdv"] = fdv
    if created_ts:
        prev_c = rec.get("created_ts")
        if not prev_c or created_ts < int(prev_c):
            rec["created_ts"] = created_ts


def harvest_dex_lists(store: dict[str, dict[str, Any]], status: dict[str, str]) -> None:
    endpoints = [
        ("profiles_latest", f"{DEX_BASE}/token-profiles/latest/v1", "list"),
        ("boosts_latest", f"{DEX_BASE}/token-boosts/latest/v1", "list"),
        ("boosts_top", f"{DEX_BASE}/token-boosts/top/v1", "list"),
        ("search_jensen", f"{DEX_BASE}/latest/dex/search?q=jensen", "search"),
        ("search_huang", f"{DEX_BASE}/latest/dex/search?q=huang", "search"),
        ("search_nvidia", f"{DEX_BASE}/latest/dex/search?q=nvidia", "search"),
        ("search_ai", f"{DEX_BASE}/latest/dex/search?q=AI", "search"),
    ]
    for name, url, kind in endpoints:
        try:
            data = http_get_json(url)
            n = 0
            if kind == "list":
                items = data if isinstance(data, list) else []
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    if (it.get("chainId") or it.get("chain") or "").lower() != "solana":
                        continue
                    addr = it.get("tokenAddress") or it.get("token") or it.get("address")
                    if isinstance(addr, str) and addr:
                        add_mint(store, addr, name)
                        n += 1
            else:
                pairs = data.get("pairs") if isinstance(data, dict) else None
                if isinstance(pairs, list):
                    for p in pairs:
                        if not isinstance(p, dict):
                            continue
                        mint = candidate_mint_from_pair(p)
                        if not mint:
                            continue
                        created = num(p.get("pairCreatedAt"))
                        created_ts = int(created / 1000) if created and created > 1e12 else (
                            int(created) if created and created > 1e9 else None
                        )
                        add_mint(
                            store,
                            mint,
                            name,
                            symbol=((p.get("baseToken") or {}).get("symbol") or ""),
                            dex=(p.get("dexId") or ""),
                            fdv=market_cap(p),
                            created_ts=created_ts,
                        )
                        n += 1
            status[name] = f"ok:{n}"
        except FetchError as e:
            status[name] = f"fail:{e}"
            log(f"discovery {name} failed: {e}")


def parse_iso_ts(value: Any) -> int | None:
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    # Trim long fractional seconds
    try:
        # fromisoformat handles +00:00
        from datetime import datetime

        dt = datetime.fromisoformat(raw)
        return int(dt.timestamp())
    except Exception:
        return None


def harvest_gecko_new(store: dict[str, dict[str, Any]], status: dict[str, str]) -> None:
    total = 0
    fails = 0
    for page in range(1, HARVEST_GECKO_PAGES + 1):
        url = f"{GECKO_BASE}/networks/solana/new_pools?page={page}"
        try:
            data = http_get_json(url)
        except FetchError as e:
            fails += 1
            log(f"gecko new_pools page {page} failed: {e}")
            status[f"gecko_new_p{page}"] = f"fail:{e}"
            continue
        pools = data.get("data") if isinstance(data, dict) else None
        n = 0
        if isinstance(pools, list):
            for p in pools:
                if not isinstance(p, dict):
                    continue
                rel = p.get("relationships") or {}
                attrs = p.get("attributes") or {}
                base = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
                quote = ((rel.get("quote_token") or {}).get("data") or {}).get("id") or ""
                dex = ((rel.get("dex") or {}).get("data") or {}).get("id") or ""
                if not isinstance(base, str) or not base.startswith("solana_"):
                    continue
                mint = base.split("solana_", 1)[1]
                quote_mint = quote.split("solana_", 1)[1] if isinstance(quote, str) and quote.startswith("solana_") else ""
                if quote_mint != WSOL and mint != WSOL:
                    # Still cache if it's a pump mint; pair filter happens later.
                    pass
                created_ts = parse_iso_ts(attrs.get("pool_created_at"))
                fdv = num(attrs.get("fdv_usd")) or num(attrs.get("market_cap_usd"))
                name = attrs.get("name") or ""
                symbol = name.split("/")[0].strip() if isinstance(name, str) else ""
                add_mint(
                    store,
                    mint,
                    f"gecko_new_p{page}",
                    symbol=symbol,
                    dex=dex,
                    fdv=fdv,
                    created_ts=created_ts,
                )
                n += 1
        total += n
        status[f"gecko_new_p{page}"] = f"ok:{n}"
    status["gecko_new"] = f"ok:{total}" + (f",fails={fails}" if fails else "")


def harvest_gecko_trending(store: dict[str, dict[str, Any]], status: dict[str, str]) -> None:
    url = f"{GECKO_BASE}/networks/solana/trending_pools?page=1"
    try:
        data = http_get_json(url)
    except FetchError as e:
        status["gecko_trending"] = f"fail:{e}"
        log(f"gecko trending failed: {e}")
        return
    pools = data.get("data") if isinstance(data, dict) else None
    n = 0
    if isinstance(pools, list):
        for p in pools:
            if not isinstance(p, dict):
                continue
            rel = p.get("relationships") or {}
            attrs = p.get("attributes") or {}
            base = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
            if not isinstance(base, str) or not base.startswith("solana_"):
                continue
            mint = base.split("solana_", 1)[1]
            dex = ((rel.get("dex") or {}).get("data") or {}).get("id") or ""
            created_ts = parse_iso_ts(attrs.get("pool_created_at"))
            fdv = num(attrs.get("fdv_usd")) or num(attrs.get("market_cap_usd"))
            name = attrs.get("name") or ""
            symbol = name.split("/")[0].strip() if isinstance(name, str) else ""
            add_mint(
                store,
                mint,
                "gecko_trending",
                symbol=symbol,
                dex=dex,
                fdv=fdv,
                created_ts=created_ts,
            )
            n += 1
    status["gecko_trending"] = f"ok:{n}"


def harvest_rugcheck_new(store: dict[str, dict[str, Any]], status: dict[str, str]) -> None:
    try:
        data = http_get_json(f"{RUGCHECK_BASE}/stats/new_tokens")
    except FetchError as e:
        status["rugcheck_new"] = f"fail:{e}"
        log(f"rugcheck new_tokens failed: {e}")
        return
    n = 0
    items = data if isinstance(data, list) else []
    for it in items:
        if not isinstance(it, dict):
            continue
        mint = it.get("mint") or it.get("address")
        if not isinstance(mint, str) or not mint:
            continue
        created_ts = parse_iso_ts(it.get("createAt") or it.get("createdAt"))
        add_mint(
            store,
            mint,
            "rugcheck_new",
            symbol=str(it.get("symbol") or ""),
            created_ts=created_ts,
        )
        n += 1
    status["rugcheck_new"] = f"ok:{n}"


def load_discovery() -> dict[str, dict[str, Any]]:
    data = load_json(DISCOVERY_PATH, {})
    mints = data.get("mints") if isinstance(data, dict) else {}
    if not isinstance(mints, dict):
        return {}
    now = now_ts()
    out: dict[str, dict[str, Any]] = {}
    for mint, rec in mints.items():
        if not isinstance(mint, str) or not isinstance(rec, dict):
            continue
        try:
            first = int(rec.get("first_seen") or 0)
        except (TypeError, ValueError):
            first = 0
        created = rec.get("created_ts")
        try:
            created_i = int(created) if created else 0
        except (TypeError, ValueError):
            created_i = 0
        age_from = created_i or first
        if age_from and now - age_from > DISCOVERY_TTL_SEC:
            continue
        rec = dict(rec)
        rec.setdefault("sources", [])
        rec.setdefault("first_seen", first or now)
        out[mint] = rec
    return out


def save_discovery(store: dict[str, dict[str, Any]]) -> None:
    atomic_write(
        DISCOVERY_PATH,
        json.dumps({"updated": now_ts(), "mints": store}, indent=2, ensure_ascii=False) + "\n",
    )


def harvest(store: dict[str, dict[str, Any]]) -> dict[str, str]:
    status: dict[str, str] = {}
    harvest_dex_lists(store, status)
    harvest_gecko_new(store, status)
    harvest_gecko_trending(store, status)
    harvest_rugcheck_new(store, status)
    return status


# ---------------------------------------------------------------------------
# DexScreener pair fetch + window filter
# ---------------------------------------------------------------------------

def _pair_score(p: dict[str, Any]) -> tuple:
    return (1 if pair_has_sol(p) else 0, liq_usd(p))


def merge_pair(out: dict[str, dict[str, Any]], pair: dict[str, Any]) -> None:
    """Merge one DexScreener pair into the per-mint record.

    Age must use the earliest pairCreatedAt across ALL pools (pumpfun
    bonding curve, PumpSwap, Meteora, ...). /tokens/v1 often returns
    only the post-graduation PumpSwap pool, which is too young.
    """
    if not isinstance(pair, dict) or pair.get("chainId") != "solana":
        return
    mint = candidate_mint_from_pair(pair)
    if not mint:
        return
    rec = out.setdefault(
        mint,
        {
            "best": None,
            "earliest_created_ms": None,
            "earliest_dex": "",
            "has_sol": False,
            "pump_family": False,
            "dexes": [],
        },
    )
    dex = (pair.get("dexId") or "").lower()
    created = num(pair.get("pairCreatedAt"))
    if created and created > 0:
        prev = rec["earliest_created_ms"]
        if prev is None or created < prev:
            rec["earliest_created_ms"] = created
            rec["earliest_dex"] = dex
    if pair_has_sol(pair):
        rec["has_sol"] = True
    if dex and dex not in rec["dexes"]:
        rec["dexes"].append(dex)
    if is_pump_family(dex, mint):
        rec["pump_family"] = True
    best = rec["best"]
    if best is None or _pair_score(pair) > _pair_score(best):
        rec["best"] = pair


def fetch_pairs(mints: list[str]) -> dict[str, dict[str, Any]]:
    """Return mint -> {best_pair, earliest_created_ms, has_sol, pump_family, dexes}.

    First pass uses /tokens/v1 (batched). That endpoint is enough for MC /
    best-pool, but after a pump.fun graduation it often omits the original
    pumpfun pair — call enrich_all_pairs() for mints that still look young.
    """
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(mints), TOKENS_BATCH):
        batch = mints[i : i + TOKENS_BATCH]
        url = f"{DEX_BASE}/tokens/v1/solana/{','.join(batch)}"
        try:
            data = http_get_json(url)
        except FetchError as e:
            log(f"tokens/v1 batch {i // TOKENS_BATCH} failed: {e}")
            continue
        if not isinstance(data, list):
            continue
        for pair in data:
            merge_pair(out, pair)
    return out


def enrich_all_pairs(pair_info: dict[str, dict[str, Any]], mints: list[str]) -> int:
    """Pull every DexScreener pair for the given mints (incl. pumpfun).

    /latest/dex/tokens/{mints} is batchable and returns the bonding-curve
    pool that /tokens/v1 drops. Falls back to /token-pairs/v1/solana/{mint}
    per mint if a batch fails. Returns how many mints were requested.
    """
    unique: list[str] = []
    seen: set[str] = set()
    for mint in mints:
        if mint and mint not in seen:
            seen.add(mint)
            unique.append(mint)
    if not unique:
        return 0
    for i in range(0, len(unique), TOKENS_BATCH):
        batch = unique[i : i + TOKENS_BATCH]
        url = f"{DEX_BASE}/latest/dex/tokens/{','.join(batch)}"
        try:
            data = http_get_json(url)
        except FetchError as e:
            log(f"latest/dex/tokens enrich batch {i // TOKENS_BATCH} failed: {e}")
            for mint in batch:
                try:
                    one = http_get_json(f"{DEX_BASE}/token-pairs/v1/solana/{mint}")
                except FetchError as e2:
                    log(f"token-pairs/v1 {mint[:8]}... failed: {e2}")
                    continue
                if isinstance(one, list):
                    for pair in one:
                        merge_pair(pair_info, pair)
            continue
        if isinstance(data, dict):
            pairs = data.get("pairs") or []
        elif isinstance(data, list):
            pairs = data
        else:
            pairs = []
        if isinstance(pairs, list):
            for pair in pairs:
                merge_pair(pair_info, pair)
    return len(unique)


def age_minutes_from_ms(created_ms: float | None, now_ms: int) -> float | None:
    if created_ms is None or created_ms <= 0:
        return None
    return (now_ms - created_ms) / 60_000.0


def earliest_created_ms(
    mint: str,
    info: dict[str, Any],
    store: dict[str, dict[str, Any]],
) -> float | None:
    """Min of all known pair/pool creation times (DexScreener + harvest cache)."""
    created = num(info.get("earliest_created_ms")) if info else None
    harvested = (store.get(mint) or {}).get("created_ts")
    if harvested:
        try:
            h_ms = int(harvested) * 1000
        except (TypeError, ValueError):
            h_ms = 0
        if h_ms > 0 and (created is None or h_ms < created):
            created = float(h_ms)
    return created


# ---------------------------------------------------------------------------
# Rugcheck
# ---------------------------------------------------------------------------

def rugcheck_report(mint: str) -> dict[str, Any]:
    return http_get_json(f"{RUGCHECK_BASE}/tokens/{mint}/report")


def authority_off(value: Any) -> bool:
    if value is None or value == "" or value == "null":
        return True
    if isinstance(value, str) and value.strip().lower() in {"", "null", "none", "disabled"}:
        return True
    return False


def lp_exclude_addresses(report: dict[str, Any]) -> set[str]:
    out: set[str] = set(BURN_OR_SYSTEM)
    known = report.get("knownAccounts") or {}
    if isinstance(known, dict):
        for addr, info in known.items():
            typ = ""
            name = ""
            if isinstance(info, dict):
                typ = str(info.get("type") or "").upper()
                name = str(info.get("name") or "")
            if typ in AMM_TYPES or typ in {"AMM", "LOCKER"}:
                out.add(addr)
            if "pump fun" in name.lower() or name.lower() in {"raydium", "pumpswap", "meteora"}:
                out.add(addr)
    for m in report.get("markets") or []:
        if not isinstance(m, dict):
            continue
        for k in ("pubkey", "liquidityA", "liquidityB", "mintLP"):
            v = m.get(k)
            if isinstance(v, str) and v:
                out.add(v)
        for acct_key in ("liquidityAAccount", "liquidityBAccount"):
            acct = m.get(acct_key) or {}
            if isinstance(acct, dict):
                owner = acct.get("owner")
                if isinstance(owner, str) and owner:
                    out.add(owner)
    return out


def evaluate_rugcheck(mint: str, report: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
    """Return (ok, metrics, reason_if_rejected)."""
    token = report.get("token") if isinstance(report.get("token"), dict) else {}
    mint_auth = report.get("mintAuthority", token.get("mintAuthority"))
    freeze_auth = report.get("freezeAuthority", token.get("freezeAuthority"))
    if not authority_off(mint_auth):
        return False, {}, "mint_authority_on"
    if not authority_off(freeze_auth):
        return False, {}, "freeze_authority_on"
    if report.get("rugged") is True:
        return False, {}, "rugged"

    risks = report.get("risks") if isinstance(report.get("risks"), list) else []
    danger: list[str] = []
    ignored: list[str] = []
    for r in risks:
        if not isinstance(r, dict):
            continue
        level = str(r.get("level") or "").lower()
        name = str(r.get("name") or "").strip()
        if level != "danger":
            continue
        lname = name.lower()
        if any(key in lname for key in IGNORED_DANGER_NAMES):
            ignored.append(name)
            continue
        danger.append(name or "unnamed_danger")
    if danger:
        return False, {"danger": danger, "ignored_danger": ignored}, "rugcheck_danger:" + ",".join(danger)

    exclude = lp_exclude_addresses(report)
    holders = report.get("topHolders") if isinstance(report.get("topHolders"), list) else []
    cleaned: list[float] = []
    insider_pct = 0.0
    for h in holders:
        if not isinstance(h, dict):
            continue
        owner = h.get("owner") or h.get("address") or ""
        if owner in exclude:
            continue
        pct = num(h.get("pct"), 0.0) or 0.0
        if pct <= 0:
            continue
        cleaned.append(pct)
        if h.get("insider") is True:
            insider_pct += pct
    cleaned.sort(reverse=True)
    top10 = sum(cleaned[:10])
    insiders_detected = report.get("graphInsidersDetected")
    try:
        insiders_n = int(insiders_detected) if insiders_detected is not None else 0
    except (TypeError, ValueError):
        insiders_n = 0

    metrics = {
        "top10_pct": round(top10, 3),
        "insider_pct": round(insider_pct, 3),
        "insiders_detected": insiders_n,
        "holders_n": len(holders),
        "holders_ex_lp": len(cleaned),
        "mint_authority_off": True,
        "freeze_authority_off": True,
        "rugged": False,
        "danger_ignored": ignored,
        "score": report.get("score"),
        "score_normalised": report.get("score_normalised"),
        "launchpad": (report.get("launchpad") or {}).get("name")
        if isinstance(report.get("launchpad"), dict)
        else report.get("launchpad"),
    }
    if top10 >= TOP10_MAX_PCT:
        return False, metrics, f"top10={top10:.1f}>={TOP10_MAX_PCT}"
    if insider_pct >= INSIDER_MAX_PCT:
        return False, metrics, f"insider={insider_pct:.1f}>={INSIDER_MAX_PCT}"
    # High raw insider-graph count with no pct is still a bundler/insider risk.
    if insiders_n >= 8:
        return False, metrics, f"insiders_detected={insiders_n}"
    return True, metrics, "ok"


# ---------------------------------------------------------------------------
# Position monitor
# ---------------------------------------------------------------------------

def position_events(
    early: dict[str, Any],
    pair_info: dict[str, dict[str, Any]],
    now: int,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    already = early.get("position_events") or {}
    positions = early.get("positions") or {}
    for mint, rec in positions.items():
        if not isinstance(rec, dict) or not mint:
            continue
        avg_mc = num(rec.get("avg_mc"))
        bought_at = rec.get("bought_at")
        try:
            bought_at_i = int(bought_at) if bought_at is not None else 0
        except (TypeError, ValueError):
            bought_at_i = 0
        tp1_done = bool(rec.get("tp1_done"))
        symbol = rec.get("symbol") or mint[:6]
        info = pair_info.get(mint) or {}
        pair = info.get("best")
        mc = market_cap(pair) if pair else None
        if mc is None:
            mc = num(rec.get("last_mc"))
        if avg_mc is None or avg_mc <= 0:
            log(f"position {symbol} missing avg_mc; skip monitor")
            continue
        multiple = (mc / avg_mc) if mc and mc > 0 else None
        age_s = (now - bought_at_i) if bought_at_i else None
        # Trailing stop (user 2026-11-19 13:03 Taipei): track the highest multiple seen.
        peak = num(rec.get("peak_multiple")) or 0.0
        if multiple is not None and multiple > peak:
            peak = multiple
            rec["peak_multiple"] = round(peak, 4)
            early["_peak_dirty"] = True
        trail_line = max(1.0, peak * TRAIL_FRACTION)

        def maybe(kind: str, action: str, summary: str) -> None:
            key = f"{mint}:{kind}"
            if key in already:
                return
            events.append(
                {
                    "mint": mint,
                    "symbol": symbol,
                    "kind": kind,
                    "action": action,
                    "avg_mc": avg_mc,
                    "mc": mc,
                    "multiple": None if multiple is None else round(multiple, 3),
                    "tp1_done": tp1_done,
                    "bought_at": bought_at_i,
                    "age_h": None if age_s is None else round(age_s / 3600.0, 3),
                    "dedupe_key": key,
                    "summary": summary,
                }
            )

        if multiple is not None and multiple >= 3.0 and not tp1_done:
            maybe(
                "tp1",
                "sell_50_percent_tp1",
                f"{symbol} MC {multiple:.2f}x avg (>=3x); sell 50% and set tp1_done",
            )
        if multiple is not None and multiple >= 10.0:
            maybe(
                "tp10",
                "sell_half_of_remainder_tp10",
                f"{symbol} MC {multiple:.2f}x avg (>=10x); sell half of remainder",
            )
        if multiple is not None and multiple <= 0.5:
            maybe(
                "sl",
                "sell_100_percent_sl",
                f"{symbol} MC {multiple:.2f}x avg (<=0.5x); sell 100% stop-loss",
            )
        if (
            tp1_done
            and multiple is not None
            and trail_line > 1.0
            and multiple <= trail_line
        ):
            pk = int(peak * 10)  # re-arm when the peak rises by 0.1x
            maybe(
                f"trail{pk}",
                "sell_remainder_trailing_stop",
                f"{symbol} {multiple:.2f}x fell to trailing stop {trail_line:.2f}x "
                f"(peak {peak:.2f}x x {TRAIL_FRACTION}); sell remainder",
            )
        if tp1_done and multiple is not None and multiple <= 1.0:
            maybe(
                "be",
                "sell_remainder_breakeven",
                f"{symbol} back to {multiple:.2f}x after tp1; sell remainder",
            )
        if (
            bought_at_i
            and now - bought_at_i >= POSITION_TIMEOUT_SEC
            and not tp1_done
        ):
            maybe(
                "timeout",
                "sell_100_percent_timeout",
                f"{symbol} held >=6h without tp1; close position",
            )
    return events


# ---------------------------------------------------------------------------
# Payload + run
# ---------------------------------------------------------------------------

def build_buy_payload(
    selected: list[dict[str, Any]],
    evidence_path: str,
    early: dict[str, Any],
) -> dict[str, Any]:
    n = len(selected)
    syms = ", ".join(c["symbol"] for c in selected)
    n_open = len(early.get("positions") or {})
    spent = float(early.get("spent_sol") or 0)
    budget = float(early.get("budget_sol") or DEFAULT_BUDGET_SOL)
    remain = max(0.0, budget - spent)
    summary = (
        f"[{EARLY_SOURCE}] {n} early SOL candidate(s) 5-60m MC $10k-$100k; "
        f"open={n_open} spent={spent:.3f}/{budget:.3f} SOL remain={remain:.3f}; "
        f"bet={BET_SOL} SOL: {syms}"
    )
    metrics_cands = []
    for c in selected:
        metrics_cands.append(
            {
                "mint": c["mint"],
                "symbol": c["symbol"],
                "mc": c["mc"],
                "liq": c["liq"],
                "age_min": c["age_min"],
                "dex": c["dex"],
                "narrative_score": c["narrative_score"],
                "narrative_hits": c["narrative_hits"],
                "top10_pct": c.get("top10_pct"),
                "insider_pct": c.get("insider_pct"),
                "dexscreener_url": c["dexscreener_url"],
            }
        )
    dedupe_key = "gmgn-early:" + ",".join(c["mint"] for c in selected)
    return {
        "schema_version": 1,
        "task_id": TASK_BUY,
        "severity": "info",
        "summary": summary,
        "evidence_path": evidence_path,
        "recommended_action": RECOMMENDED_BUY,
        "dedupe_key": dedupe_key,
        "changed": True,
        "metrics": {
            "candidates": metrics_cands,
            "open_positions": n_open,
            "spent_sol": spent,
            "budget_sol": budget,
            "bet_sol": BET_SOL,
            "source": EARLY_SOURCE,
            "dup_note": "A/B test: box and GitHub Actions both scan. Skip any mint already in box early.json positions or seen (duplicate wake).",
        },
    }


def build_pos_payload(
    events: list[dict[str, Any]],
    evidence_path: str,
) -> dict[str, Any]:
    n = len(events)
    bits = ", ".join(f"{e['symbol']}:{e['kind']}" for e in events)
    summary = f"{n} early position event(s): {bits}"
    dedupe_key = "gmgn-early-positions:" + ",".join(e["dedupe_key"] for e in events)
    return {
        "schema_version": 1,
        "task_id": TASK_POS,
        "severity": "info",
        "summary": summary,
        "evidence_path": evidence_path,
        "recommended_action": RECOMMENDED_POS,
        "dedupe_key": dedupe_key,
        "changed": True,
        "metrics": {
            "events": [
                {
                    "mint": e["mint"],
                    "symbol": e["symbol"],
                    "kind": e["kind"],
                    "action": e["action"],
                    "avg_mc": e["avg_mc"],
                    "mc": e["mc"],
                    "multiple": e["multiple"],
                    "tp1_done": e["tp1_done"],
                    "summary": e["summary"],
                }
                for e in events
            ]
        },
    }


def is_dry_run(args: argparse.Namespace) -> bool:
    if args.dry_run:
        return True
    return os.environ.get("DRY_RUN", "0").strip() == "1"


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GMGN early-launch numeric prefilter")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print scan/candidate summary; do not emit a wake payload or mark seen",
    )
    args = parser.parse_args(argv)
    dry = is_dry_run(args)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    early = ensure_early_file()
    now = now_ts()
    now_ms = int(time.time() * 1000)
    early["seen"] = prune_seen(early.get("seen") or {}, now)
    gha_seen = load_gha_seen()
    if EARLY_SOURCE == "gha":
        # Throwaway early.json on the runner: dedupe comes from the public file.
        for m, r in gha_seen.items():
            if isinstance(r, dict) and r.get("woke"):
                early["seen"].setdefault(m, {"ts": int(r.get("ts") or now), "source": "gha"})
    extra_kw = load_narrative_keywords()
    rejected = rejected_mints()
    stats: dict[str, Any] = {
        "ts": now,
        "ts_local": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "dry_run": dry,
        "ok": False,
        "reason": "",
    }

    try:
        store = load_discovery()
        harvested_before = len(store)
        status = harvest(store)
        save_discovery(store)
        stats["endpoint_status"] = status
        stats["discovery_cached"] = len(store)
        stats["discovery_new"] = max(0, len(store) - harvested_before)
        log(
            f"harvest cached={len(store)} new~={stats['discovery_new']} "
            f"status={status}"
        )

        # Fetch DexScreener pairs for: cached mints that could be in the
        # 5-60m window, plus all open early positions (for the monitor).
        positions = early.get("positions") or {}
        pos_mints = [m for m in positions.keys() if isinstance(m, str) and m]
        window_mints: list[str] = []
        for mint, rec in store.items():
            created = rec.get("created_ts") or 0
            first = rec.get("first_seen") or 0
            # Keep if we do not yet know age, or age could still be inside 5-60m,
            # or the token was first seen recently enough that a pumpswap pair
            # age might still be in window.
            age_s = None
            if created:
                age_s = now - int(created)
            elif first:
                # first_seen is a lower bound (token may be older). Still check
                # if first_seen itself is < 90m.
                age_s = now - int(first)
            if age_s is None or age_s <= DISCOVERY_TTL_SEC:
                window_mints.append(mint)

        fetch_list: list[str] = []
        seen_f: set[str] = set()
        for m in pos_mints + window_mints:
            if m not in seen_f:
                seen_f.add(m)
                fetch_list.append(m)

        # Cap pair fetches to keep this cheap. Prefer position mints, then
        # tokens with known age already in/near the window or a near-window FDV
        # / narrative name, then the rest.
        def fetch_priority(mint: str) -> tuple:
            if mint in positions:
                return (0, 0)
            rec = store.get(mint) or {}
            created = rec.get("created_ts")
            # Do not use first_seen as birth time: a just-boosted graduated
            # coin may have first_seen=now but a hours-old pumpfun pool.
            # Unknown birth -> fetch so all-pairs enrich can date it.
            if created:
                age_m = (now - int(created)) / 60.0
                in_age = 0 if 0 <= age_m <= 90 else 1
            else:
                in_age = 0
            fdv = num(rec.get("fdv"), 0.0) or 0.0
            near_mc = 0 if fdv >= 7000 or (rec.get("symbol") and narrative_score(rec.get("symbol") or "", rec.get("symbol") or "", extra_kw)[0] > 0) else 1
            return (1, in_age, near_mc, -fdv)

        fetch_list.sort(key=fetch_priority)
        FETCH_CAP = 180  # 6 DexScreener batches
        if len(fetch_list) > FETCH_CAP:
            log(f"capping pair fetch {len(fetch_list)} -> {FETCH_CAP}")
            # Always keep position mints.
            kept = [m for m in fetch_list if m in positions]
            for m in fetch_list:
                if len(kept) >= FETCH_CAP:
                    break
                if m not in positions:
                    kept.append(m)
            fetch_list = kept

        pair_info = fetch_pairs(fetch_list) if fetch_list else {}
        stats["fetched_pairs"] = len(pair_info)
        stats["fetch_requested"] = len(fetch_list)

        # /tokens/v1 often omits the original pumpfun pool after graduation,
        # so age looks like the PumpSwap pool age. Re-fetch all pairs for
        # mints that still appear young (or have no timestamp).
        age_before: dict[str, float | None] = {}
        need_enrich: list[str] = []
        for mint, info in pair_info.items():
            cms = earliest_created_ms(mint, info, store)
            age_before[mint] = age_minutes_from_ms(cms, now_ms)
            age_m = age_before[mint]
            if age_m is None or age_m <= 90.0:
                need_enrich.append(mint)
        n_enrich = enrich_all_pairs(pair_info, need_enrich)
        age_corrections: list[dict[str, Any]] = []
        for mint, info in pair_info.items():
            cms = info.get("earliest_created_ms")
            if cms:
                rec = store.setdefault(
                    mint, {"first_seen": now, "sources": [], "symbol": ""}
                )
                created_s = int(float(cms) / 1000)
                prev = rec.get("created_ts")
                try:
                    prev_i = int(prev) if prev else 0
                except (TypeError, ValueError):
                    prev_i = 0
                if not prev_i or created_s < prev_i:
                    rec["created_ts"] = created_s
            after = age_minutes_from_ms(earliest_created_ms(mint, info, store), now_ms)
            before = age_before.get(mint)
            if (
                before is not None
                and after is not None
                and after - before >= 2.0
            ):
                sym = ""
                best = (info or {}).get("best") or {}
                if isinstance(best, dict):
                    sym = (best.get("baseToken") or {}).get("symbol") or ""
                if not sym:
                    sym = (store.get(mint) or {}).get("symbol") or mint[:6]
                age_corrections.append(
                    {
                        "symbol": sym,
                        "mint": mint,
                        "age_tokens_v1_min": round(before, 2),
                        "age_all_pairs_min": round(after, 2),
                        "earliest_dex": (info or {}).get("earliest_dex") or "",
                    }
                )
        if n_enrich or age_corrections:
            save_discovery(store)
        stats["enriched_mints"] = n_enrich
        stats["age_corrections"] = age_corrections
        if age_corrections:
            log(
                f"age corrected {len(age_corrections)} mint(s) via all-pairs "
                f"(enriched {n_enrich}): "
                + ", ".join(
                    f"{c['symbol']} {c['age_tokens_v1_min']:.0f}m->{c['age_all_pairs_min']:.0f}m"
                    for c in age_corrections[:8]
                )
            )

        # --- Position monitor (priority over new buys) ---
        evs = position_events(early, pair_info, now) if pos_mints else []
        stats["position_events"] = [e.get("dedupe_key") for e in evs]
        if early.pop("_peak_dirty", None) and not evs and not dry:
            save_early(early)
        if evs and not dry:
            run_id = time.strftime("%Y%m%d-%H%M%S")
            ev_path = EVIDENCE_DIR / f"gmgn-early-positions-{run_id}.json"
            evidence = {
                "task_id": TASK_POS,
                "run_id": run_id,
                "ts": now,
                "ts_local": stats["ts_local"],
                "events": evs,
            }
            atomic_write(ev_path, json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
            payload = build_pos_payload(evs, str(ev_path))
            # Mark events so the same trigger does not wake again.
            pe = early.setdefault("position_events", {})
            for e in evs:
                pe[e["dedupe_key"]] = {"ts": now, "kind": e["kind"], "mint": e["mint"]}
            save_early(early)
            stats["reason"] = "wake_positions"
            stats["ok"] = True
            stats["evidence_path"] = str(ev_path)
            stats["payload_summary"] = payload["summary"]
            atomic_write(LAST_RUN_PATH, json.dumps(stats, indent=2, default=str) + "\n")
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            log(f"wake {len(evs)} position event(s) evidence={ev_path}")
            return 0

        # --- Buy candidates ---
        ok_buy, buy_gate = can_wake_buy(early)
        stats["buy_gate"] = buy_gate
        stats["buy_allowed"] = ok_buy

        excluded: list[dict[str, Any]] = []
        scanned = 0
        window_pass: list[dict[str, Any]] = []
        seen_map = early.get("seen") or {}
        pos_set = set(positions.keys())

        def exclude(mint: str, symbol: str, reason: str, **extra: Any) -> None:
            rec = {"mint": mint, "symbol": symbol, "reason": reason}
            rec.update(extra)
            excluded.append(rec)

        # Evaluate every mint we have pair data for (except open positions).
        consider = list(pair_info.keys())
        # Also try discovery mints that had no pair data? They fail "no pair".
        for mint, rec in store.items():
            if mint not in pair_info:
                # Only mention those that look like they might be in window.
                created = rec.get("created_ts") or rec.get("first_seen")
                if created and AGE_MIN_MINUTES * 60 <= now - int(created) <= AGE_MAX_MINUTES * 60:
                    exclude(mint, rec.get("symbol") or "", "no_dex_pair")

        for mint in consider:
            if mint in pos_set:
                continue
            info = pair_info[mint]
            pair = info.get("best")
            symbol = ""
            name = ""
            if pair:
                symbol = (pair.get("baseToken") or {}).get("symbol") or (store.get(mint) or {}).get("symbol") or ""
                name = (pair.get("baseToken") or {}).get("name") or ""
            else:
                symbol = (store.get(mint) or {}).get("symbol") or ""

            if mint in rejected:
                exclude(mint, symbol, "rejected.json")
                continue

            created_ms = earliest_created_ms(mint, info, store)
            age_min = age_minutes_from_ms(created_ms, now_ms)
            if age_min is None:
                exclude(mint, symbol, "age_unknown")
                continue
            if age_min < AGE_MIN_MINUTES or age_min > AGE_MAX_MINUTES:
                exclude(
                    mint,
                    symbol,
                    f"age={age_min:.1f}m_out_of_5_55",
                    age_min=round(age_min, 2),
                    earliest_dex=info.get("earliest_dex") or "",
                    dexes=info.get("dexes") or [],
                )
                continue
            # already_seen after age so dry-run/logs show the true age
            # of graduated tokens that previously slipped through.
            if mint in seen_map:
                exclude(mint, symbol, "already_seen", age_min=round(age_min, 2))
                continue
            if not info.get("has_sol"):
                exclude(mint, symbol, "no_sol_pair", age_min=round(age_min, 2))
                continue
            if not info.get("pump_family"):
                exclude(mint, symbol, "not_pump_or_graduated", age_min=round(age_min, 2), dexes=info.get("dexes"))
                continue
            if not pair:
                exclude(mint, symbol, "no_best_pair", age_min=round(age_min, 2))
                continue
            mc = market_cap(pair)
            if mc is None:
                exclude(mint, symbol, "mc_unknown", age_min=round(age_min, 2))
                continue
            if mc < MC_MIN or mc > MC_MAX:
                exclude(mint, symbol, f"mc={mc:.0f}_out_of_10k_100k", age_min=round(age_min, 2), mc=mc)
                continue
            m5 = num((pair.get("priceChange") or {}).get("m5")) if isinstance(pair.get("priceChange"), dict) else None
            if m5 is not None and m5 < M5_DUMP_MIN:
                exclude(
                    mint,
                    symbol,
                    f"m5={m5:.1f}<-30",
                    age_min=round(age_min, 2),
                    mc=mc,
                    m5=m5,
                )
                continue

            scanned += 1
            nscore, nhits = narrative_score(symbol, name, extra_kw, (store.get(mint) or {}).get("sources"))
            window_pass.append(
                {
                    "mint": mint,
                    "symbol": symbol or mint[:6],
                    "name": name,
                    "mc": mc,
                    "liq": liq_usd(pair),
                    "age_min": round(age_min, 3),
                    "earliest_dex": info.get("earliest_dex") or "",
                    "dex": (pair.get("dexId") or ""),
                    "dexes": info.get("dexes") or [],
                    "sources": (store.get(mint) or {}).get("sources") or [],
                    "narrative_score": nscore,
                    "narrative_hits": nhits,
                    "pair_address": pair.get("pairAddress") or "",
                    "dexscreener_url": f"https://dexscreener.com/solana/{mint}",
                    "h1": num((pair.get("priceChange") or {}).get("h1")) if isinstance(pair.get("priceChange"), dict) else None,
                    "m5": m5,
                    "vol_h1": num((pair.get("volume") or {}).get("h1")) if isinstance(pair.get("volume"), dict) else None,
                }
            )

        stats["window_pass"] = len(window_pass)
        stats["excluded"] = len(excluded)
        stats["scanned_in_window_mc"] = scanned
        log(
            f"window+mc pass={len(window_pass)} excluded={len(excluded)} "
            f"(age/m5/mc/sol/pump/seen/rejected)"
        )

        # Rugcheck only the window+MC passers, cheapest last filter.
        passed: list[dict[str, Any]] = []
        for c in window_pass:
            try:
                report = rugcheck_report(c["mint"])
            except FetchError as e:
                exclude(c["mint"], c["symbol"], f"rugcheck_fetch:{e}", age_min=c["age_min"], mc=c["mc"])
                log(f"rugcheck fail {c['symbol']}: {e}")
                continue
            ok, rmetrics, reason = evaluate_rugcheck(c["mint"], report)
            c.update({k: v for k, v in rmetrics.items() if k in ("top10_pct", "insider_pct", "insiders_detected", "score_normalised", "launchpad")})
            c["rugcheck"] = rmetrics
            if not ok:
                exclude(c["mint"], c["symbol"], reason, age_min=c["age_min"], mc=c["mc"], **{k: rmetrics[k] for k in ("top10_pct", "insider_pct") if k in rmetrics})
                continue
            # Safety net: rugcheck detectedAt is the mint's first sighting and
            # should match the pumpfun pool even if a DexScreener call missed it.
            detected = parse_iso_ts(report.get("detectedAt"))
            if detected:
                det_age = (now - detected) / 60.0
                c["rugcheck_detected_age_min"] = round(det_age, 2)
                if det_age > AGE_MAX_MINUTES or det_age < AGE_MIN_MINUTES:
                    exclude(
                        c["mint"],
                        c["symbol"],
                        f"age={det_age:.1f}m_out_of_5_55",
                        age_min=round(det_age, 2),
                        mc=c["mc"],
                        age_source="rugcheck_detectedAt",
                    )
                    continue
            passed.append(c)

        # Rank: narrative first, then closer to mid-window MC as a weak tiebreak, then liq.
        def rank_key(c: dict[str, Any]) -> tuple:
            return (-int(c.get("narrative_score") or 0), -(c.get("liq") or 0), -abs((c.get("mc") or 0) - 40_000))

        passed.sort(key=rank_key)
        if not dry:
            for c in passed:
                g = gha_seen.get(c["mint"]) if EARLY_SOURCE == "box" else None
                source_log({"event": "candidate", "mint": c["mint"], "symbol": c["symbol"],
                            "mc": c["mc"], "age_min": c["age_min"],
                            "gha_woke_ts": (g or {}).get("ts") if isinstance(g, dict) and g.get("woke") else None})
                if EARLY_SOURCE == "gha":
                    rec = gha_seen.setdefault(c["mint"], {"first_ts": now})
                    rec.update({"ts": rec.get("ts") or now, "symbol": c["symbol"], "first_ts": rec.get("first_ts") or now,
                                "mc_first": rec.get("mc_first") or round(c["mc"]), "age_min_first": rec.get("age_min_first") or c["age_min"],
                                "run_id": rec.get("run_id") or os.environ.get("GITHUB_RUN_ID", ""), "woke": bool(rec.get("woke"))})
        if EARLY_SOURCE == "box":
            dups = [c for c in passed if isinstance(gha_seen.get(c["mint"]), dict) and gha_seen[c["mint"]].get("woke")]
            if dups:
                for c in dups:
                    exclude(c["mint"], c["symbol"], "dup_of_gha_wake", age_min=c["age_min"], mc=c["mc"])
                    if not dry:
                        early.setdefault("seen", {})[c["mint"]] = {"ts": now, "symbol": c["symbol"], "dup_of_gha": True}
                if not dry:
                    save_early(early)
                log("skip (GHA already woke): " + ", ".join(c["symbol"] for c in dups))
                passed = [c for c in passed if c not in dups]
        selected = passed[:MAX_WAKE_CANDIDATES]
        stats["passed"] = [
            {"symbol": c["symbol"], "mint": c["mint"], "mc": c["mc"], "age_min": c["age_min"], "score": c["narrative_score"]}
            for c in passed
        ]

        if dry:
            summary = {
                "dry_run": True,
                "discovery_cached": len(store),
                "fetch_requested": len(fetch_list),
                "fetched_pairs": len(pair_info),
                "in_age_mc_sol_pump": len(window_pass),
                "passed_rugcheck": len(passed),
                "selected": [
                    {
                        "symbol": c["symbol"],
                        "mint": c["mint"],
                        "mc": c["mc"],
                        "age_min": c["age_min"],
                        "dex": c["dex"],
                        "liq": c["liq"],
                        "narrative_score": c["narrative_score"],
                        "narrative_hits": c["narrative_hits"],
                        "top10_pct": c.get("top10_pct"),
                        "insider_pct": c.get("insider_pct"),
                    }
                    for c in selected
                ],
                "window_examples": [
                    {"symbol": c["symbol"], "mc": c["mc"], "age_min": c["age_min"], "dex": c["dex"], "score": c["narrative_score"]}
                    for c in window_pass[:8]
                ],
                "exclude_reasons": _reason_counts(excluded),
                "age_corrections": age_corrections,
                "enriched_mints": n_enrich,
                "buy_allowed": ok_buy,
                "buy_gate": buy_gate,
                "position_events": [e.get("dedupe_key") for e in evs],
            }
            stats["reason"] = "dry_run"
            stats["ok"] = True
            stats["exclude_reasons"] = summary["exclude_reasons"]
            atomic_write(LAST_RUN_PATH, json.dumps(stats, indent=2, default=str) + "\n")
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            log(
                f"dry-run scanned_pairs={len(pair_info)} in_window_mc={len(window_pass)} "
                f"passed={len(passed)} selected={len(selected)}"
            )
            return 0

        if not ok_buy:
            stats["reason"] = "buy_gate_closed"
            stats["ok"] = True
            atomic_write(LAST_RUN_PATH, json.dumps(stats, indent=2, default=str) + "\n")
            log(f"quiet: buy gate closed ({buy_gate})")
            return 0

        if EARLY_SOURCE == "gha" and passed and not dry and not (ok_buy and selected):
            save_gha_seen(gha_seen)
        if not selected:
            stats["reason"] = "no_candidates"
            stats["ok"] = True
            stats["exclude_reasons"] = _reason_counts(excluded)
            atomic_write(LAST_RUN_PATH, json.dumps(stats, indent=2, default=str) + "\n")
            log("quiet: no early candidates passed")
            return 0

        run_id = time.strftime("%Y%m%d-%H%M%S")
        ev_path = EVIDENCE_DIR / f"gmgn-early-{run_id}.json"
        evidence = {
            "task_id": TASK_BUY,
            "run_id": run_id,
            "ts": now,
            "ts_local": stats["ts_local"],
            "buy_gate": buy_gate,
            "endpoint_status": status,
            "discovery_cached": len(store),
            "fetched_pairs": len(pair_info),
            "age_corrections": age_corrections,
            "window_pass": window_pass,
            "passed": passed,
            "selected": selected,
            "excluded": excluded[-80:],  # cap size
            "exclude_reasons": _reason_counts(excluded),
            "notes": [
                "Sellability and GMGN DEV-scan are left to gmgn-4.",
                "early.json is box-only; do not copy to the public repo.",
                "If you buy, write the position into early.json (avg_mc, bought_at, buy_sol, tp1_done) and add buy_sol to spent_sol. Do not put early positions into the main 2-slot positions.json.",
            ],
        }
        atomic_write(ev_path, json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
        payload = build_buy_payload(selected, str(ev_path), early)
        if EARLY_SOURCE == "gha":
            payload["evidence_path"] = "github-actions (runner temp, not kept)"
            for k in ("open_positions", "spent_sol", "budget_sol"):
                payload["metrics"][k] = None
            payload["summary"] = (
                f"[gha] {len(selected)} early SOL candidate(s) 5-60m MC $10k-$100k; "
                f"budget/open: check box early.json; bet={BET_SOL} SOL: "
                + ", ".join(c["symbol"] for c in selected)
            )
        # Mark seen even if the wrapper later fails to POST? Mark after we
        # print the payload; the wrapper notifies. If we mark here we will
        # not re-wake the same mints, matching gmgn-prefilter.py.
        for c in selected:
            early.setdefault("seen", {})[c["mint"]] = {
                "ts": now,
                "symbol": c["symbol"],
                "run_id": run_id,
                "mc": c["mc"],
                "age_min": c["age_min"],
            }
        save_early(early)
        if EARLY_SOURCE == "gha":
            for c in selected:
                gha_seen.setdefault(c["mint"], {"first_ts": now})
                gha_seen[c["mint"]].update({"woke": True, "ts": now, "woke_ts": now})
            save_gha_seen(gha_seen)
        source_log({"event": "wake", "mints": [c["mint"] for c in selected]})
        stats["reason"] = "wake"
        stats["ok"] = True
        stats["evidence_path"] = str(ev_path)
        stats["payload_summary"] = payload["summary"]
        atomic_write(LAST_RUN_PATH, json.dumps(stats, indent=2, default=str) + "\n")
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        log(f"wake {len(selected)} early candidate(s) evidence={ev_path}")
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


def _reason_counts(excluded: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rec in excluded:
        reason = str(rec.get("reason") or "unknown")
        # Group rugcheck danger and age/mc prefixes.
        key = reason
        if reason.startswith("age=") and (
            reason.endswith("_out_of_5_55") or reason.endswith("_out_of_5_60")
        ):
            key = "age_out_of_window"
        elif reason.startswith("m5=") and "<-30" in reason:
            key = "m5_dump"
        elif reason.startswith("mc=") and "out_of_10k_100k" in reason:
            key = "mc_out_of_range"
        elif reason.startswith("rugcheck_danger:"):
            key = "rugcheck_danger"
        elif reason.startswith("rugcheck_fetch:"):
            key = "rugcheck_fetch"
        elif reason.startswith("top10="):
            key = "top10"
        elif reason.startswith("insider="):
            key = "insider"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


if __name__ == "__main__":
    sys.exit(run())
