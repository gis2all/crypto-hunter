"""RadarDEX: the widest token list on Arc, and the cheapest per-token facts.

It returns the whole chain in three requests and each row already carries holders,
market cap, reserves, 24h volume, deployer, launchpad and the social links. That made
it the one source worth reading per token as well as in bulk: for a prefilter survivor
everything except the safety verdict is already in the cached row.
"""

from __future__ import annotations

from typing import Any

from ...core.models import SafetyFacts, TokenMetrics
from ..base import social_url, web_url
from ..http import SourceError, UA_JSON, cached, json_get

BASE = "https://api.radardex.pro"
PAGE_LIMIT = 2000
#: Three orderings because one page of 2000 does not cover a 4000-token chain:
#: by market cap, by volume, and by first-seen - the last one is what catches a token
#: that is too small and too quiet to rank yet.
SORTS = ("mcap", "volume24", "firstSeen")
UNIVERSE_TTL = 900


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


class RadarDEX:
    name = "radardex"
    chains = ("arc",)

    def rows(self, chain_id: str) -> dict[str, dict]:
        """Every token this source knows, keyed by CA, first sighting wins."""

        def produce():
            merged: dict[str, dict] = {}
            for sort in SORTS:
                url = "%s/tokens?limit=%d&sort=%s&dir=desc&window=24h" % (BASE, PAGE_LIMIT, sort)
                try:
                    payload = json_get(url, UA_JSON, tries=2, sleep=3.0, timeout=45.0)
                except SourceError:
                    continue
                items = payload.get("tokens") if isinstance(payload, dict) else payload
                for row in items or []:
                    ca = str(row.get("address") or row.get("token") or "").lower()
                    if ca and ca not in merged:
                        merged[ca] = row
            return merged or None

        return cached("rd:tokens:%s" % chain_id, UNIVERSE_TTL, produce) or {}

    def fetch_universe(self, chain_id: str, limit: int) -> list[TokenMetrics]:
        out = []
        for ca, row in self.rows(chain_id).items():
            out.append(
                TokenMetrics(
                    ca=ca,
                    symbol=(row.get("symbol") or None),
                    market_cap_usd=_num(row.get("mcap")),
                    liquidity_usd=_num(row.get("liquidityUsdc")),
                    volume_24h_usd=_num(row.get("volume24")),
                    # The only bulk source that ships a pool count (measured: 2000/2000
                    # rows filled). GeckoTerminal is still believed first; this is the
                    # fallback for the tokens it does not index.
                    pools=int(row["pools"]) if row.get("pools") else None,
                    holders=int(row["holderCount"]) if row.get("holderCount") else None,
                    price_usd=_num(row.get("price")),
                    price_change_24h_pct=_num(row.get("change24h")),
                    launchpad=row.get("launchpad") or None,
                    created_at=_iso(row.get("deployTs") or row.get("firstSeen")),
                    socials=_socials(row),
                    websites=_websites(row),
                    provenance={
                        "market_cap_usd": "radardex", "liquidity_usd": "radardex",
                        "volume_24h_usd": "radardex", "holders": "radardex",
                        "pools": "radardex",
                    },
                )
            )
        return out[:limit] if limit else out

    def fetch_token(self, chain_id: str, ca: str) -> tuple[TokenMetrics, SafetyFacts]:
        row = self.rows(chain_id).get(ca.lower()) or {}
        if not row:
            return TokenMetrics(ca=ca), SafetyFacts()
        metrics = TokenMetrics(
            ca=ca,
            symbol=row.get("symbol") or None,
            holders=int(row["holderCount"]) if row.get("holderCount") else None,
            launchpad=row.get("launchpad") or None,
            socials=_socials(row),
            websites=_websites(row),
        )
        # RadarDEX publishes the deployer, which is what the LP question is asked about.
        metrics.creator = row.get("deployer") or None
        metrics.provenance["holders"] = "radardex"
        metrics.provenance["socials"] = "radardex"
        return metrics, SafetyFacts()


def _socials(row: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, kind in (("twitter", "twitter"), ("telegram", "telegram"), ("discord", "discord")):
        url = social_url(kind, row.get(key))
        if url:
            out[kind] = url
    return out


def _websites(row: dict) -> list[str]:
    url = web_url(row.get("website"))
    return [url] if url else []


def _iso(seconds: Any) -> Any:
    from datetime import datetime, timezone

    try:
        return datetime.fromtimestamp(float(seconds), tz=timezone.utc)
    except (TypeError, ValueError):
        return None
