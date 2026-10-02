"""Deterministic rules for the macro track.

Pure functions only: no database, no network, no model. The slot in
`service.py` calls these; the tests pin them. Fingerprint dedups, prescreen
drops the obvious no without spending a token, two scores combine into one
verdict, the title heuristic clusters one story, and heat ranks events by how
many independent sources tell it.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Outlet suffixes RSS titles carry ("X - CoinDesk", "X | Decrypt"). Stripped
#: before any comparison so one story from two outlets still matches.
_OUTLET_RE = re.compile(r"\s+[-|:]\s+[^-|:]+$")

#: What counts as crypto. A feed item with none of these in its title or
#: summary is off topic; the feeds are crypto feeds, so this mostly catches
#: house ads and price-widget junk, not real news.
CRYPTO_HINTS = frozenset({
    "bitcoin", "btc", "ethereum", "eth", "crypto", "cryptocurrency",
    "blockchain", "defi", "nft", "token", "tokenize", "coin", "stablecoin",
    "cbdc", "layer", "rollup", "staking", "stake", "airdrop", "mainnet",
    "testnet", "dex", "cex", "listing", "etf", "sec", "regulation",
    "regulator", "hack", "exploit", "bridge", "wallet", "mining", "miner",
    "validator", "gas", "dao", "web3", "meme", "ordinals", "solana", "base",
    "arbitrum", "optimism", "polygon", "bnb", "xrp", "doge", "cardano",
    "avalanche", "chainlink", "uniswap", "lido", "eigenlayer", "restaking",
    "defai", "rwa", "perps", "futures", "options", "custody", "reserve",
})


def fingerprint(url: str | None, title: str) -> str:
    """The dedup key. URLs win; a missing URL falls back to the title."""
    base = (url or "").strip().split("?")[0].split("#")[0].rstrip("/").lower()
    if base:
        return hashlib.sha1(("url:" + base).encode()).hexdigest()
    return hashlib.sha1(("title:" + normalize_title(title)).encode()).hexdigest()


def normalize_title(title: str) -> str:
    """Lowercased token stream with the outlet suffix cut off."""
    text = (title or "").strip().lower()
    text = _OUTLET_RE.sub("", text)
    return " ".join(_TOKEN_RE.findall(text))


def tokens_of(title: str) -> set[str]:
    return set(normalize_title(title).split())


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def should_merge(title_a: str, title_b: str) -> bool:
    """Whether two headlines tell one story. v1 heuristic, no embedding.

    Short headlines need an exact match; longer ones merge on token overlap,
    and a headline fully contained in another merges on a lower bar. Doubtful
    pairs merge on purpose - one event with two angles reads better than two
    events with one angle each - and a hand-moved item stays moved (manual=1
    rows are never auto-attached elsewhere).
    """
    left, right = tokens_of(title_a), tokens_of(title_b)
    if not left or not right:
        return False
    if min(len(left), len(right)) < 3:
        return normalize_title(title_a) == normalize_title(title_b)
    score = jaccard(left, right)
    if score >= 0.45:
        return True
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    return short <= long and score >= 0.30


def parse_dt(raw: str | None) -> datetime | None:
    """RSS and JSON timestamps into one aware UTC datetime, or None."""
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def prescreen(title: str, summary: str | None, *,
              max_age_days: int,
              published_at: datetime | None = None,
              now: datetime | None = None) -> tuple[bool, str]:
    """The no-token filter. Returns (keep, reason)."""
    if len((title or "").strip()) < 12:
        return False, "too_short"
    words = set(re.findall(r"[a-z0-9]+", ((title or "") + " " + (summary or "")).lower()))
    if not words & CRYPTO_HINTS:
        return False, "off_topic"
    if published_at is not None:
        moment = now or datetime.now(timezone.utc)
        if moment - published_at > timedelta(days=max_age_days):
            return False, "stale"
    return True, "ok"


def combine_scores(first: float, second: float, *,
                    threshold: float, min_single: float) -> tuple[float, bool]:
    """Two independent votes into one verdict: the average must clear the bar
    and neither vote may be a veto."""
    avg = (first + second) / 2.0
    return avg, avg >= threshold and min(first, second) >= min_single


def heat(ages_hours: list[float], *, half_life_hours: float) -> float:
    """One event, one source one vote: the earliest item per source counts,
    decayed by half-life. Re-crawls add nothing - the caller passes one age
    per source, never one per item."""
    total = 0.0
    for age in ages_hours:
        if age < 0:
            continue
        total += 0.5 ** (age / half_life_hours)
    return total


def event_flags(first_seen: datetime, recent_distinct: int, *,
                now: datetime | None = None) -> tuple[bool, bool]:
    """(is_new, is_rising): new within a day; rising when at least two
    independent sources joined in the last six hours."""
    moment = now or datetime.now(timezone.utc)
    return (moment - first_seen <= timedelta(hours=24), recent_distinct >= 2)