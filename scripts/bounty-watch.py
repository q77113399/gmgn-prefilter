#!/usr/bin/env python3
"""Mechanical bounty prefilter (no LLM).

Poll public, unauthenticated APIs and wake a bot via webhook only when
NEW qualifying tasks appear. First run seeds a silent baseline.

Stdlib only. Never prints webhook secrets or env secret values.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("BOUNTY_WATCH_ROOT", "/workspace/bounty-watch"))


# Paths are configurable so the same script runs on the box and in GitHub Actions.
#   BOUNTY_STATE_DIR    seen.json + abf-cache.json (committed to the repo under data/state/bounty)
#   BOUNTY_LOG_DIR      bounty-watch.log + rejected.jsonl (never committed)
#   BOUNTY_EVIDENCE_DIR per-wake evidence JSON (never committed; GHA keeps it inline in the payload)
def state_dir() -> Path:
    return Path(os.environ.get("BOUNTY_STATE_DIR") or (ROOT / "state"))


def log_dir() -> Path:
    return Path(os.environ.get("BOUNTY_LOG_DIR") or (ROOT / "logs"))


def evidence_dir() -> Path:
    return Path(os.environ.get("BOUNTY_EVIDENCE_DIR") or (ROOT / "evidence"))


def in_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"
USER_AGENT = "Mozilla/5.0 (compatible; bounty-watch/1.0)"
TIMEOUT = 20
MIN_DEADLINE_SECONDS = 6 * 3600
MAX_ENTRY_FEE_USD = 1.0
MAKEFUN_MAX_SUBMISSIONS = 200
ABF_MIN_INTERVAL = 3600
STABLECOINS = {"USDC", "USDT", "USDG", "USD", "DAI", "USDB", "OUSD", "CUSD"}
BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
ABF_SKIP_REQUIRES = {
    "kyc",
    "x_account",
    "public_post",
    "human_only",
    "purchase",
    "captcha",
    "stake",
    "bond",
}

# Title/summary/tag keyword denylist. Conservative phrases; avoid bare
# words like "launch" or "token launch radar".
KEYWORD_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "social",
        re.compile(
            r"\b(twitter|tweet|tweets|retweet|x\s*thread|thread on x|"
            r"instagram|tiktok|ugc|shill|shilling)\b|"
            r"\b(write|create|post|publish)\b.{0,50}\b(twitter|x thread|thread)\b|"
            r"\byoutube\s+video\b|\brecord a video\b|\bfilm yourself\b|"
            r"\bcreate content\b|\bcontent creator\b|\bcreator challenge\b",
            re.I,
        ),
    ),
    (
        "promo",
        re.compile(
            r"\b(promo|promos|promotion|promotional|affiliate|marketing|"
            r"shill|referral raid)\b",
            re.I,
        ),
    ),
    (
        "coin-launch",
        re.compile(
            r"\b(launch a (coin|token)|coin launch|memecoin|pump\.fun|"
            r"coin that sells)\b",
            re.I,
        ),
    ),
    (
        "captcha",
        re.compile(r"\b(captcha|recaptcha|hcaptcha|solve captcha)\b", re.I),
    ),
    (
        "kyc",
        re.compile(
            r"\b(kyc|know your customer|government[- ]id|government-issued|"
            r"proof of identity|identity verification)\b|\bpassport\b",
            re.I,
        ),
    ),
    (
        "audit",
        re.compile(
            r"\b(security audit|audit contest|audit competition|pentest|"
            r"penetration test|bug bounty|vulnerability disclosure|"
            r"code4rena|immunefi)\b",
            re.I,
        ),
    ),
    (
        "nsfw",
        re.compile(
            r"\b(nsfw|porn|xxx|erotic|hentai|onlyfans|adult game)\b",
            re.I,
        ),
    ),
]


def tz_now() -> datetime:
    return datetime.now().astimezone()


def iso_now() -> str:
    return tz_now().strftime("%Y-%m-%dT%H:%M:%S%z")


def log(msg: str) -> None:
    line = f"{tz_now().strftime('%Y-%m-%d %H:%M:%S %Z')} {msg}"
    log_path = log_dir() / "bounty-watch.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print(line, file=sys.stderr)


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def http_get_json(url: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/json, */*",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}") from e
    except Exception as e:
        raise RuntimeError(f"{type(e).__name__}: {e}") from e
    if status != 200:
        raise RuntimeError(f"HTTP {status}")
    if not raw:
        raise RuntimeError("empty body")
    try:
        text = raw.decode("utf-8")
        return json.loads(text)
    except Exception as e:
        raise RuntimeError(f"invalid JSON: {e}") from e


def abs_url(url: str | None, base: str) -> str:
    if not url:
        return base
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return base.rstrip("/") + url
    return base.rstrip("/") + "/" + url


def to_float(val: Any) -> float | None:
    if val is None or val is False:
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        s = val.strip().replace(",", "").replace("$", "")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def atomic_usd(val: Any, decimals: int = 6) -> float | None:
    n = to_float(val)
    if n is None:
        return None
    # If the API already gave a small display amount, don't divide.
    # Atomic USDC values are typically >= 1e5 for even $0.10.
    if isinstance(val, str) and "." in val.strip():
        return n
    return n / (10 ** decimals)


def parse_deadline(val: Any) -> datetime | None:
    if val is None or val is False or val == "":
        return None
    if isinstance(val, (int, float)):
        ts = float(val)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        if re.fullmatch(r"\d+(\.\d+)?", s):
            return parse_deadline(float(s))
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def deadline_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def truthy(val: Any) -> bool:
    if val is None or val is False:
        return False
    if val is True:
        return True
    if isinstance(val, (int, float)):
        return val != 0
    if isinstance(val, str):
        return val.strip().lower() not in {"", "0", "false", "none", "null", "no"}
    if isinstance(val, (list, dict)):
        return bool(val)
    return bool(val)


def keyword_hits(text: str) -> list[str]:
    if not text:
        return []
    hits: list[str] = []
    seen: set[str] = set()
    for label, pat in KEYWORD_RULES:
        if label in seen:
            continue
        if pat.search(text):
            hits.append(label)
            seen.add(label)
    return hits


def blob_of(*parts: Any) -> str:
    chunks: list[str] = []
    for p in parts:
        if p is None or p is False:
            continue
        if isinstance(p, list):
            chunks.extend(str(x) for x in p if x is not None)
        else:
            chunks.append(str(p))
    return "\n".join(chunks)


class Task:
    __slots__ = (
        "platform",
        "id",
        "title",
        "url",
        "reward_usd",
        "deadline",
        "submission_count",
        "flags",
        "entry_fee_usd",
        "status_open",
        "funded",
        "text",
        "extra_reasons",
    )

    def __init__(
        self,
        platform: str,
        id: str,
        title: str,
        url: str,
        reward_usd: float | None,
        deadline: datetime | None,
        submission_count: int | None,
        flags: list[str] | None = None,
        entry_fee_usd: float | None = None,
        status_open: bool = True,
        funded: bool = False,
        text: str = "",
        extra_reasons: list[str] | None = None,
    ) -> None:
        self.platform = platform
        self.id = str(id)
        self.title = title or ""
        self.url = url or ""
        self.reward_usd = reward_usd
        self.deadline = deadline
        self.submission_count = submission_count
        self.flags = list(flags or [])
        self.entry_fee_usd = entry_fee_usd
        self.status_open = status_open
        self.funded = funded
        self.text = text
        self.extra_reasons = list(extra_reasons or [])

    @property
    def seen_key(self) -> str:
        return f"{self.platform}:{self.id}"

    def to_metric(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "reward_usd": None if self.reward_usd is None else round(self.reward_usd, 4),
            "deadline": deadline_iso(self.deadline),
            "submission_count": self.submission_count,
            "flags": list(self.flags),
        }


def filter_task(task: Task, now: datetime) -> list[str]:
    reasons: list[str] = []
    if not task.status_open:
        reasons.append("not_open")
    if task.extra_reasons:
        reasons.extend(task.extra_reasons)
    if not task.funded or task.reward_usd is None or task.reward_usd <= 0:
        reasons.append("unfunded")
    if task.deadline is not None:
        if task.deadline <= now + timedelta(seconds=MIN_DEADLINE_SECONDS):
            reasons.append("deadline_too_soon")
    fee = task.entry_fee_usd if task.entry_fee_usd is not None else 0.0
    if fee > MAX_ENTRY_FEE_USD:
        reasons.append("entry_fee")
    if task.platform == "make.fun":
        if task.submission_count is None:
            reasons.append("missing_submission_count")
        elif task.submission_count >= MAKEFUN_MAX_SUBMISSIONS:
            reasons.append(f"submissions>={MAKEFUN_MAX_SUBMISSIONS}")
        task.flags.append(f"submission_count={task.submission_count}")
    hits = keyword_hits(blob_of(task.title, task.text))
    for h in hits:
        reasons.append(f"keyword:{h}")
        if h not in task.flags:
            task.flags.append(h)
    return reasons


# ----- platform fetchers -----

def fetch_makefun() -> list[Task]:
    data = http_get_json("https://make.fun/api/competitions?status=open&limit=100")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("unexpected shape")
    tasks: list[Task] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        tid = it.get("id") or it.get("slug")
        if not tid:
            continue
        slug = it.get("slug") or tid
        url = abs_url(it.get("url"), "https://make.fun")
        if "/bounty/" not in url and slug:
            url = f"https://make.fun/bounty/{slug}"
        pot = to_float(it.get("potUsd"))
        reward = to_float(it.get("rewardAmount"))
        reward_usd = pot if pot and pot > 0 else reward
        fee = to_float(it.get("entryFeeUsd"))
        if fee is None:
            fee = to_float(it.get("entryFeesUsd"))
        status = str(it.get("status") or "").lower()
        vis = str(it.get("visibility") or "").lower()
        flags: list[str] = []
        extra: list[str] = []
        if it.get("houseOnly"):
            extra.append("house_only")
        if vis and vis not in {"public", "open", ""}:
            flags.append(f"visibility={vis}")
        funded = bool(reward_usd and reward_usd > 0)
        if it.get("walletPaid") is False and not funded:
            funded = False
        subs = it.get("submissionCount")
        try:
            subs_i = int(subs) if subs is not None else None
        except (TypeError, ValueError):
            subs_i = None
        title = str(it.get("title") or "").strip()
        text = blob_of(it.get("summary"), it.get("category"), it.get("skills"))
        tasks.append(
            Task(
                platform="make.fun",
                id=str(tid),
                title=title,
                url=url,
                reward_usd=reward_usd,
                deadline=parse_deadline(it.get("deadline")),
                submission_count=subs_i,
                flags=flags,
                entry_fee_usd=fee,
                status_open=status in {"open", "active", "published"},
                funded=funded,
                text=text,
                extra_reasons=extra,
            )
        )
    return tasks


def fetch_superteam() -> list[Task]:
    data = http_get_json(
        "https://superteam.fun/api/listings?category=All&status=open&take=100"
    )
    items = data if isinstance(data, list) else (data.get("listings") if isinstance(data, dict) else None)
    if not isinstance(items, list):
        raise RuntimeError("unexpected shape")
    tasks: list[Task] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        tid = it.get("id") or it.get("slug")
        if not tid:
            continue
        slug = it.get("slug") or tid
        url = f"https://earn.superteam.fun/listing/{slug}"
        token = str(it.get("token") or "").upper()
        amount = to_float(it.get("rewardAmount"))
        extra: list[str] = []
        flags: list[str] = []
        funded = False
        reward_usd: float | None = None
        if token in STABLECOINS and amount is not None and amount > 0:
            reward_usd = amount
            funded = True
        elif amount is not None and amount > 0:
            extra.append("not_usd_equivalent")
            flags.append(f"token={token}")
        status = str(it.get("status") or "").upper()
        if it.get("isWinnersAnnounced"):
            extra.append("winners_announced")
        access = str(it.get("agentAccess") or "")
        if access and access.upper() not in {"AGENT_ALLOWED", "AGENT_ONLY"}:
            extra.append("human_only")
            flags.append(f"agentAccess={access}")
        count = None
        cnt = it.get("_count")
        if isinstance(cnt, dict) and cnt.get("Submission") is not None:
            try:
                count = int(cnt["Submission"])
            except (TypeError, ValueError):
                count = None
        title = str(it.get("title") or "").strip()
        tasks.append(
            Task(
                platform="superteam",
                id=str(tid),
                title=title,
                url=url,
                reward_usd=reward_usd,
                deadline=parse_deadline(it.get("deadline")),
                submission_count=count,
                flags=flags,
                status_open=status == "OPEN",
                funded=funded,
                text=blob_of(title, it.get("type"), it.get("sponsor")),
                extra_reasons=extra,
            )
        )
    return tasks


def _elgora_title(challenge: str, bounty_id: str) -> str:
    if not challenge:
        return f"Elgora bounty {bounty_id}"
    for line in challenge.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return f"Elgora bounty {bounty_id}"


def fetch_elgora() -> list[Task]:
    data = http_get_json("https://elgora.ai/api/bounties?status=open&first=20")
    items = data.get("bounties") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("unexpected shape")
    tasks: list[Task] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        tid = it.get("bounty_id")
        if tid is None:
            continue
        tid_s = str(tid)
        escrow = it.get("escrow") if isinstance(it.get("escrow"), dict) else {}
        token_addr = str(escrow.get("token_address") or "").lower()
        extra: list[str] = []
        flags: list[str] = []
        reward_usd = None
        funded = False
        if token_addr == BASE_USDC or not token_addr:
            reward_usd = atomic_usd(escrow.get("amount"), 6)
            funded = bool(reward_usd and reward_usd > 0)
        else:
            maybe = atomic_usd(escrow.get("amount"), 6)
            if maybe and maybe > 0:
                extra.append("not_usd_equivalent")
                flags.append("unknown_escrow_token")
        title = f"Elgora bounty {tid_s}"
        text = ""
        try:
            detail = http_get_json(f"https://elgora.ai/api/bounties/{tid_s}")
            challenge = ""
            if isinstance(detail, dict):
                challenge = detail.get("challenge") or ""
            title = _elgora_title(challenge, tid_s)
            text = challenge[:800]
        except Exception as e:
            flags.append("title_missing")
            log(f"elgora detail {tid_s} failed: {e}")
        status = str(it.get("status") or "").lower()
        subs = it.get("submission_count")
        try:
            subs_i = int(subs) if subs is not None else None
        except (TypeError, ValueError):
            subs_i = None
        tasks.append(
            Task(
                platform="elgora",
                id=tid_s,
                title=title,
                url=f"https://elgora.ai/bounties/{tid_s}",
                reward_usd=reward_usd,
                deadline=parse_deadline(it.get("submission_deadline")),
                submission_count=subs_i,
                flags=flags,
                status_open=status == "open",
                funded=funded,
                text=text,
                extra_reasons=extra,
            )
        )
    return tasks


def fetch_frantic() -> list[Task]:
    data = None
    used = "board"
    try:
        data = http_get_json("https://gofrantic.com/v1/board")
    except Exception as e:
        log(f"frantic board failed ({e}); falling back to feed.json")
        used = "feed"
        data = http_get_json("https://gofrantic.com/feed.json")
    tasks: list[Task] = []
    if used == "board":
        board = data.get("board") if isinstance(data, dict) else None
        items = board.get("open_bounties") if isinstance(board, dict) else None
        if not isinstance(items, list):
            raise RuntimeError("unexpected board shape")
        for it in items:
            if not isinstance(it, dict):
                continue
            number = it.get("number")
            tid = str(number if number is not None else it.get("url") or "")
            if not tid:
                continue
            url = abs_url(it.get("url"), "https://gofrantic.com")
            price = to_float(it.get("price_usd"))
            funded = bool(it.get("funded")) and price is not None and price > 0
            status = str(it.get("work_status") or "").lower()
            vis = str(it.get("visibility") or "")
            flags: list[str] = []
            if vis:
                flags.append(f"visibility={vis}")
            slots = it.get("claim_slots") if isinstance(it.get("claim_slots"), dict) else {}
            if slots.get("available") is not None:
                flags.append(f"slots_left={slots.get('available')}")
            title = str(it.get("title") or "").strip()
            tasks.append(
                Task(
                    platform="frantic",
                    id=tid,
                    title=title,
                    url=url,
                    reward_usd=price,
                    deadline=parse_deadline(it.get("deadline") or it.get("expires_at")),
                    submission_count=None,
                    flags=flags,
                    status_open=status == "open",
                    funded=funded,
                    text=title,
                )
            )
        return tasks

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("unexpected feed shape")
    for it in items:
        if not isinstance(it, dict):
            continue
        meta = it.get("_frantic") if isinstance(it.get("_frantic"), dict) else {}
        number = meta.get("number")
        tid = str(number if number is not None else it.get("id") or "")
        if not tid:
            continue
        price = to_float(meta.get("price_usd"))
        url = abs_url(it.get("url"), "https://gofrantic.com")
        title = str(it.get("title") or "").strip()
        tasks.append(
            Task(
                platform="frantic",
                id=tid,
                title=title,
                url=url,
                reward_usd=price,
                deadline=parse_deadline(it.get("date_published") and None),
                submission_count=None,
                flags=["source=feed"],
                status_open=True,
                funded=bool(price and price > 0),
                text=blob_of(title, it.get("content_text")),
            )
        )
    return tasks


def fetch_taskmarket() -> list[Task]:
    data = http_get_json("https://api.taskmarket.dev/api/tasks?status=open")
    items = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("unexpected shape")
    tasks: list[Task] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        ref = it.get("referenceCode") or it.get("id")
        if not ref:
            continue
        desc = str(it.get("description") or "")
        title = ""
        m = re.search(r"##\s*Title\s*\n+(.+)", desc)
        if m:
            title = m.group(1).split("\n")[0].strip()
        if not title:
            for line in desc.splitlines():
                s = line.strip()
                if s.startswith("# "):
                    title = s[2:].strip()
                    break
        if not title:
            title = str(ref)
        chain = it.get("chainId")
        reward_usd = atomic_usd(it.get("reward"), 6)
        flags: list[str] = []
        extra: list[str] = []
        if chain is not None:
            flags.append(f"chainId={chain}")
        # Base USDC and Tempo/OUSD (4217) treated as USD-like 6-decimal.
        if chain not in (8453, 4217, 1, 10, 137, 42161, None):
            if reward_usd and reward_usd > 0:
                extra.append("not_usd_equivalent")
                reward_usd = None
        vis = str(it.get("taskVisibility") or "").lower()
        if vis and vis not in {"public", "open", ""}:
            extra.append("not_public")
        if truthy(it.get("stakeRequired")):
            extra.append("stake_required")
        status = str(it.get("status") or "").lower()
        funded = bool(reward_usd and reward_usd > 0) and bool(it.get("escrowTxHash") or reward_usd)
        if not it.get("escrowTxHash"):
            # still funded if reward > 0 on a public listing; flag missing escrow tx
            flags.append("no_escrow_tx")
        subs = it.get("submissionCount")
        try:
            subs_i = int(subs) if subs is not None else None
        except (TypeError, ValueError):
            subs_i = None
        url = f"https://taskmarket.dev/tasks/{ref}"
        tags = it.get("tags") if isinstance(it.get("tags"), list) else []
        tasks.append(
            Task(
                platform="taskmarket",
                id=str(ref),
                title=title,
                url=url,
                reward_usd=reward_usd,
                deadline=parse_deadline(it.get("expiryTime")),
                submission_count=subs_i,
                flags=flags,
                status_open=status == "open",
                funded=bool(reward_usd and reward_usd > 0),
                text=blob_of(title, tags, desc[:500]),
                extra_reasons=extra,
            )
        )
    return tasks


def fetch_basedagents() -> list[Task]:
    data = http_get_json("https://api.basedagents.ai/v1/tasks?status=open")
    items = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("unexpected shape")
    tasks: list[Task] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        tid = it.get("task_id") or it.get("id")
        if not tid:
            continue
        bounty = it.get("bounty") if isinstance(it.get("bounty"), dict) else {}
        amount = None
        if bounty.get("amount_display") is not None:
            amount = to_float(bounty.get("amount_display"))
        if amount is None:
            amount = atomic_usd(it.get("bounty_amount"), 6)
        escrow = it.get("escrow")
        escrow_funded = False
        if isinstance(escrow, dict):
            amt = atomic_usd(escrow.get("amount") or escrow.get("amount_atomic"), 6)
            escrow_funded = (amt is not None and amt > 0) or truthy(escrow.get("funded")) or bool(escrow)
        elif escrow is not None and escrow is not False and escrow != "":
            escrow_funded = True
        verified = truthy(it.get("payment_verified"))
        extra: list[str] = []
        flags: list[str] = []
        if not escrow_funded:
            extra.append("unescrowed")
        if not verified:
            extra.append("payment_unverified")
        funded = bool(escrow_funded and verified and amount and amount > 0)
        status = str(it.get("status") or "").lower()
        title = str(it.get("title") or "").strip()
        url = f"https://basedagents.ai/tasks/{tid}"
        tasks.append(
            Task(
                platform="basedagents",
                id=str(tid),
                title=title,
                url=url,
                reward_usd=amount,
                deadline=parse_deadline(it.get("expires_at")),
                submission_count=None,
                flags=flags,
                status_open=status == "open",
                funded=funded,
                text=blob_of(title, it.get("category"), it.get("description", "")[:400]),
                extra_reasons=extra,
            )
        )
    return tasks


def _abf_cache_path() -> Path:
    return state_dir() / "abf-cache.json"


def fetch_agent_bounty_feed(force: bool = False) -> tuple[list[Task] | None, str]:
    """Return (tasks, status). status is ok|cached|error. tasks is None on skip/error."""
    cache_path = _abf_cache_path()
    cache: dict[str, Any] = {}
    if cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8") or "{}")
        except Exception:
            cache = {}
    now = time.time()
    last_fetch = cache.get("fetched_at_unix")
    try:
        last_fetch_f = float(last_fetch) if last_fetch is not None else 0.0
    except (TypeError, ValueError):
        last_fetch_f = 0.0
    if not force and last_fetch_f and (now - last_fetch_f) < ABF_MIN_INTERVAL:
        return None, "cached"
    try:
        data = http_get_json("https://agent-bounty-feed.dailydollar.workers.dev/feed.json")
    except Exception as e:
        return None, f"error:{e}"
    if not isinstance(data, dict):
        return None, "error:unexpected shape"
    generated = data.get("generated_at")
    if (
        not force
        and generated
        and cache.get("generated_at") == generated
        and last_fetch_f
    ):
        cache["fetched_at_unix"] = int(now)
        cache["fetched_at"] = iso_now()
        try:
            atomic_write_json(cache_path, cache)
        except OSError:
            pass
        return None, "cached"
    items = data.get("items")
    if not isinstance(items, list):
        return None, "error:unexpected shape"
    tasks: list[Task] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        tid = it.get("id")
        if not tid:
            continue
        requires = it.get("requires") if isinstance(it.get("requires"), list) else []
        req_l = [str(x).strip().lower() for x in requires if x is not None]
        extra: list[str] = []
        flags: list[str] = []
        blocked = sorted(set(req_l) & ABF_SKIP_REQUIRES)
        if blocked:
            extra.append("requires:" + ",".join(blocked))
        if req_l:
            flags.append("requires=" + ",".join(req_l))
        est = to_float(it.get("est_usd"))
        if est is None:
            cur = str(it.get("currency") or "").upper()
            amt = to_float(it.get("reward"))
            if cur in STABLECOINS and amt is not None:
                est = amt
        funded = est is not None and est > 0
        title = str(it.get("title") or "").strip()
        tasks.append(
            Task(
                platform="agent-bounty-feed",
                id=str(tid),
                title=title,
                url=str(it.get("url") or ""),
                reward_usd=est,
                deadline=parse_deadline(it.get("deadline")),
                submission_count=None,
                flags=flags,
                status_open=True,
                funded=funded,
                text=blob_of(title, it.get("eligibility"), it.get("source")),
                extra_reasons=extra,
            )
        )
    try:
        atomic_write_json(
            cache_path,
            {
                "generated_at": generated,
                "fetched_at_unix": int(now),
                "fetched_at": iso_now(),
                "count": len(tasks),
            },
        )
    except OSError as e:
        log(f"abf cache write failed: {e}")
    return tasks, "ok"


PLATFORMS = [
    ("make.fun", fetch_makefun),
    ("superteam", fetch_superteam),
    ("elgora", fetch_elgora),
    ("frantic", fetch_frantic),
    ("taskmarket", fetch_taskmarket),
    ("basedagents", fetch_basedagents),
]


def load_seen(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": 1, "ids": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {"version": 1, "ids": {}}
    if not isinstance(data, dict):
        return {"version": 1, "ids": {}}
    ids = data.get("ids")
    if not isinstance(ids, dict):
        data["ids"] = {}
    data["version"] = 1
    return data


def append_rejected(entries: list[dict[str, Any]]) -> None:
    if not entries:
        return
    path = log_dir() / "rejected.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


def post_webhook(payload: str, url: str, bearer: str) -> int:
    data = payload.encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {bearer}",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return int(getattr(resp, "status", None) or resp.getcode() or 0)
    except urllib.error.HTTPError as e:
        return int(e.code or 0)
    except Exception:
        return 0


def build_payload(tasks: list[Task], evidence_path: str) -> dict[str, Any]:
    metrics_tasks = [t.to_metric() for t in tasks]
    ids = [t.seen_key for t in tasks]
    dedupe_key = "bounty-new:" + ",".join(ids[:12])
    if len(ids) > 12:
        dedupe_key += f"+{len(ids) - 12}"
    parts = []
    for t in tasks[:5]:
        usd = f"${t.reward_usd:g}" if t.reward_usd is not None else "unknown"
        parts.append(f"{t.platform} “{t.title}” ({usd})")
    more = f" and {len(tasks) - 5} more" if len(tasks) > 5 else ""
    noun = "task" if len(tasks) == 1 else "tasks"
    summary = f"{len(tasks)} new funded {noun}: " + "; ".join(parts) + more
    return {
        "schema_version": 1,
        "task_id": "bounty-new",
        "severity": "info",
        "summary": summary,
        "evidence_path": evidence_path,
        "recommended_action": "evaluate_and_notify",
        "dedupe_key": dedupe_key,
        "changed": True,
        "metrics": {"tasks": metrics_tasks},
    }


def dedupe_batch_by_url(tasks: list[Task]) -> list[Task]:
    """Keep first occurrence of a normalized URL; prefer primary platforms."""
    priority = {
        "make.fun": 0,
        "superteam": 1,
        "elgora": 2,
        "frantic": 3,
        "taskmarket": 4,
        "basedagents": 5,
        "agent-bounty-feed": 9,
    }
    ordered = sorted(tasks, key=lambda t: (priority.get(t.platform, 5), -((t.reward_usd or 0))))
    seen_urls: set[str] = set()
    out: list[Task] = []
    for t in ordered:
        key = (t.url or "").strip().rstrip("/").lower()
        if key and key in seen_urls:
            t.flags.append("duplicate_url")
            continue
        if key:
            seen_urls.add(key)
        out.append(t)
    return out


def mark_seen(state: dict[str, Any], tasks_with_reasons: list[tuple[Task, list[str] | None]]) -> None:
    ids = state.setdefault("ids", {})
    ts = int(time.time())
    iso = iso_now()
    for task, reasons in tasks_with_reasons:
        qualified = not reasons
        ids[task.seen_key] = {
            "ts": ts,
            "at": iso,
            "qualified": qualified,
            "reason": None if qualified else ",".join(reasons),
            "title": task.title[:200],
        }
    state["updated_at"] = iso


def main(argv: list[str] | None = None) -> int:
    global ROOT
    parser = argparse.ArgumentParser(description="Mechanical bounty prefilter")
    parser.add_argument("--dry-run", action="store_true", help="Print payload, do not POST or mark new ids")
    parser.add_argument("--test", action="store_true", help="Simulate one new qualifying task (implies dry-run)")
    parser.add_argument("--force-abf", action="store_true", help="Ignore hourly Agent Bounty Feed cache")
    parser.add_argument("--root", default=None, help="Override working root")
    args = parser.parse_args(argv)

    if args.root:
        ROOT = Path(args.root)

    dry_run = args.dry_run or args.test or os.environ.get("DRY_RUN") == "1"
    # --test never sends a webhook.
    if args.test:
        dry_run = True

    state_dir().mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)
    evidence_dir().mkdir(parents=True, exist_ok=True)

    seen_path = state_dir() / "seen.json"
    state = load_seen(seen_path)
    ids: dict[str, Any] = state.get("ids") or {}
    is_baseline = len(ids) == 0

    now = datetime.now(timezone.utc)
    platform_stats: dict[str, dict[str, Any]] = {}
    all_tasks: list[Task] = []
    fetched_platforms: set[str] = set()

    for name, fn in PLATFORMS:
        try:
            tasks = fn()
            fetched_platforms.add(name)
            platform_stats[name] = {"ok": True, "open": len(tasks), "error": None}
            all_tasks.extend(tasks)
            log(f"{name}: ok open={len(tasks)}")
        except Exception as e:
            platform_stats[name] = {"ok": False, "open": 0, "error": str(e)}
            log(f"{name}: fail {e}")

    abf_tasks, abf_status = fetch_agent_bounty_feed(force=args.force_abf or is_baseline)
    if abf_status == "ok" and abf_tasks is not None:
        fetched_platforms.add("agent-bounty-feed")
        platform_stats["agent-bounty-feed"] = {
            "ok": True,
            "open": len(abf_tasks),
            "error": None,
        }
        all_tasks.extend(abf_tasks)
        log(f"agent-bounty-feed: ok open={len(abf_tasks)}")
    elif abf_status == "cached":
        platform_stats["agent-bounty-feed"] = {
            "ok": True,
            "open": None,
            "error": None,
            "skipped": "hourly_cache",
        }
        log("agent-bounty-feed: skipped (hourly cache)")
    else:
        err = abf_status.split(":", 1)[-1] if abf_status.startswith("error:") else abf_status
        platform_stats["agent-bounty-feed"] = {"ok": False, "open": 0, "error": err}
        log(f"agent-bounty-feed: fail {err}")

    qualifying: list[Task] = []
    rejected: list[tuple[Task, list[str]]] = []
    for task in all_tasks:
        reasons = filter_task(task, now)
        if reasons:
            rejected.append((task, reasons))
        else:
            qualifying.append(task)

    # If the aggregator echoes a primary-platform task we already rejected
    # this run, don't wake on the copy (ABF often lacks tags).
    rejected_keys: dict[str, list[str]] = {}
    rejected_urls: dict[str, list[str]] = {}
    for task, reasons in rejected:
        if task.platform == "agent-bounty-feed":
            continue
        rejected_keys[task.seen_key] = reasons
        urlk = (task.url or "").strip().rstrip("/").lower()
        if urlk:
            rejected_urls[urlk] = reasons
    kept: list[Task] = []
    for task in qualifying:
        if task.platform == "agent-bounty-feed":
            inherited = rejected_keys.get(task.id)
            if inherited is None:
                urlk = (task.url or "").strip().rstrip("/").lower()
                inherited = rejected_urls.get(urlk)
            if inherited:
                rejected.append((task, ["primary_rejected"] + list(inherited)))
                continue
        kept.append(task)
    qualifying = kept

    for name, st in platform_stats.items():
        if st.get("ok"):
            q = sum(1 for t in qualifying if t.platform == name)
            r = sum(1 for t, _ in rejected if t.platform == name)
            st["qualifying"] = q
            st["rejected"] = r

    # Summaries on stderr
    for name in list(dict.fromkeys([n for n, _ in PLATFORMS] + ["agent-bounty-feed"])):
        st = platform_stats.get(name) or {}
        if st.get("skipped"):
            print(f"{name}: cached (hourly)", file=sys.stderr)
        elif st.get("ok"):
            print(
                f"{name}: ok open={st.get('open')} qualifying={st.get('qualifying', 0)}",
                file=sys.stderr,
            )
        else:
            print(f"{name}: fail {st.get('error')}", file=sys.stderr)

    new_qual: list[Task] = []
    new_rej: list[tuple[Task, list[str]]] = []
    for t in qualifying:
        if t.seen_key not in ids:
            new_qual.append(t)
    for t, reasons in rejected:
        if t.seen_key not in ids:
            new_rej.append((t, reasons))

    test_payload_tasks: list[Task] | None = None
    if args.test:
        # Prefer a currently qualifying live task, treated as unseen.
        pool = qualifying or [t for t, _ in rejected]
        if qualifying:
            test_payload_tasks = [qualifying[0]]
            log(f"--test using live qualifying id {qualifying[0].seen_key}")
        elif pool:
            # Should not normally happen; don't invent unfunded work.
            log("--test: no current qualifying tasks; no payload")
        else:
            log("--test: no tasks fetched; no payload")

    if is_baseline and not args.test:
        to_mark = [(t, None) for t in qualifying] + new_rej
        mark_seen(state, to_mark)
        try:
            atomic_write_json(seen_path, state)
        except OSError as e:
            log(f"failed to write seen.json: {e}")
            print(f"baseline seed failed: {e}")
            return 1
        append_rejected(
            [
                {
                    "at": iso_now(),
                    "platform": t.platform,
                    "id": t.id,
                    "title": t.title,
                    "url": t.url,
                    "reasons": reasons,
                    "baseline": True,
                }
                for t, reasons in new_rej
            ]
        )
        n = len(qualifying) + len(rejected)
        q = len(qualifying)
        msg = f"baseline seeded: {n} ids ({q} qualifying now, not notified)"
        log(msg)
        print(msg)
        return 0

    # Normal / dry-run / test path
    notify_tasks = test_payload_tasks if args.test else new_qual
    if notify_tasks:
        notify_tasks = dedupe_batch_by_url(list(notify_tasks))

    if not notify_tasks:
        # In live mode, still mark newly rejected after a successful filter pass.
        if not dry_run and new_rej:
            mark_seen(state, new_rej)
            try:
                atomic_write_json(seen_path, state)
            except OSError as e:
                log(f"failed to write seen.json: {e}")
            append_rejected(
                [
                    {
                        "at": iso_now(),
                        "platform": t.platform,
                        "id": t.id,
                        "title": t.title,
                        "url": t.url,
                        "reasons": reasons,
                    }
                    for t, reasons in new_rej
                ]
            )
        print("no new")
        log("no new")
        return 0

    stamp = tz_now().strftime("%Y%m%d-%H%M%S")
    evidence_path = evidence_dir() / f"bounty-{stamp}.json"
    payload_obj = build_payload(
        notify_tasks,
        "inline:metrics.tasks (github-actions run)" if in_github_actions() else str(evidence_path),
    )
    if in_github_actions():
        payload_obj["runner"] = "github-actions"
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if run_id and repo:
            payload_obj["run_url"] = f"{server}/{repo}/actions/runs/{run_id}"
    else:
        payload_obj["runner"] = os.environ.get("GMGN_RUNNER") or "box"
    evidence = {
        "generated_at": iso_now(),
        "run_mode": "test" if args.test else ("dry_run" if dry_run else "live"),
        "platforms": platform_stats,
        "tasks": payload_obj["metrics"]["tasks"],
    }
    try:
        atomic_write_json(evidence_path, evidence)
    except OSError as e:
        log(f"evidence write failed: {e}")

    payload = json.dumps(payload_obj, ensure_ascii=False, separators=(",", ":"))

    if dry_run:
        print(payload)
        log(f"DRY_RUN=1 payload tasks={len(notify_tasks)} bytes={len(payload)} (not sent, not marked)")
        # DRY_RUN must not mark new ids (except baseline, already handled).
        return 0

    # Pano3's webhook only. Never fall back to the GMGN WEBHOOK_URL / WEBHOOK_BEARER.
    url = os.environ.get("BOUNTY_WEBHOOK_URL") or ""
    bearer = os.environ.get("BOUNTY_WEBHOOK_BEARER") or ""
    if not url or not bearer:
        log("notify skipped: webhook not configured")
        print("notify skipped: webhook not configured", file=sys.stderr)
        # Do not mark qualifying (retry next run). Mark rejected so they never wake.
        if new_rej:
            mark_seen(state, new_rej)
            try:
                atomic_write_json(seen_path, state)
            except OSError as e:
                log(f"failed to write seen.json: {e}")
            append_rejected(
                [
                    {
                        "at": iso_now(),
                        "platform": t.platform,
                        "id": t.id,
                        "title": t.title,
                        "url": t.url,
                        "reasons": reasons,
                    }
                    for t, reasons in new_rej
                ]
            )
        # Still print payload so an operator can inspect, but we already said not configured.
        print(payload)
        return 0

    status = post_webhook(payload, url, bearer)
    log(f"notify http={status}")
    print(f"notify http={status}")
    if 200 <= status < 300:
        mark_seen(state, [(t, None) for t in new_qual] + new_rej)
        try:
            atomic_write_json(seen_path, state)
        except OSError as e:
            log(f"failed to write seen.json: {e}")
        append_rejected(
            [
                {
                    "at": iso_now(),
                    "platform": t.platform,
                    "id": t.id,
                    "title": t.title,
                    "url": t.url,
                    "reasons": reasons,
                }
                for t, reasons in new_rej
            ]
        )
        return 0

    # Non-2xx: mark rejected only, leave qualifying unseen for retry.
    if new_rej:
        mark_seen(state, new_rej)
        try:
            atomic_write_json(seen_path, state)
        except OSError as e:
            log(f"failed to write seen.json: {e}")
        append_rejected(
            [
                {
                    "at": iso_now(),
                    "platform": t.platform,
                    "id": t.id,
                    "title": t.title,
                    "url": t.url,
                    "reasons": reasons,
                }
                for t, reasons in new_rej
            ]
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
