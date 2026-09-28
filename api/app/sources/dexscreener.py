"""DexScreener: the links no scanner on Arc publishes as links.

The registries we sweep carry a token’s X handle and its bare domain, and the ones that
carry a GitHub or a docs page carry them as browser tabs rather than API fields. DexScreener
answers one CA at a time with `info.socials` / `info.websites` already resolved to URLs,
which is what lets the writer say "this project has a site and a repository" instead of
"no public information" - the sentence our own fact sheet had been printing over a snapshot
that carried the burn transaction.

Two decisions worth stating:

- **It is not part of the pull.** There is no Arc-wide list here to sweep, and a source that
  answers nothing in the pull is a source that shows up in the credential row with a zero.
  It is asked per token, in the same enrich pass as the other per-token reads, so it costs a
  round one overlapping request per judged candidate.
- **It is also the market numbers’ source of record.** Every scanner answered 市值 /
  流动性 / 24h 成交 in its own caliber and the three do not agree: on every token checked,
  Tolly’s `liquidity` came out at 5.52x this host’s, which is a ranking key handing one
  source a five-fold head start. Since the round ranks on liquidity, the numbers now come
  from here and the scanners’ own readings only stand where this host has no pair at all.
  The 5.52 is why 14 tokens from a single launchpad took 14 of the gate’s 20 slots.
"""

from __future__ import annotations

from typing import Any

from ..core.models import SafetyFacts, TokenMetrics
from .base import social_url, web_url
from .http import SourceError, UA_JSON, json_get

BASE = "https://api.dexscreener.com/latest/dex/tokens/%s"


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _numbers(metrics: TokenMetrics, pools: list[dict], best: dict) -> None:
    """The market numbers, in the one caliber the round ranks on.

    Liquidity and 24h volume are summed over the token’s pools on this chain: a token
    spread over nine pools has the depth all nine of them hold, and the deepest single
    pool is where Tolly’s inflated number came from. Market cap and price come from that
    deepest pool alone - it is the price the page quotes and the one every pool agrees on.
    """
    liquidity = 0.0
    volume = 0.0
    quoted_volume = False
    for row in pools:
        liquidity += _num((row.get("liquidity") or {}).get("usd"))
        raw = (row.get("volume") or {}).get("h24")
        if raw is not None:
            quoted_volume = True
            volume += _num(raw)
    if liquidity > 0:
        metrics.liquidity_usd = round(liquidity, 2)
        metrics.provenance["liquidity_usd"] = "dexscreener"
    if quoted_volume:
        metrics.volume_24h_usd = round(volume, 2)
        metrics.provenance["volume_24h_usd"] = "dexscreener"
    cap = _num(best.get("marketCap")) or _num(best.get("fdv"))
    if cap > 0:
        metrics.market_cap_usd = round(cap, 2)
        metrics.provenance["market_cap_usd"] = "dexscreener"
    price = _num(best.get("priceUsd"))
    if price > 0:
        metrics.price_usd = price
        metrics.provenance["price_usd"] = "dexscreener"


class DexScreener:
    name = "dexscreener"
    chains = ("arc",)

    def fetch_universe(self, chain_id: str, limit: int) -> list[TokenMetrics]:
        """Nothing to sweep: this host is asked about a CA, not about a chain."""
        return []

    def fetch_token(self, chain_id: str, ca: str) -> tuple[TokenMetrics, SafetyFacts]:
        metrics = TokenMetrics(ca=ca)
        try:
            payload = json_get(BASE % ca, UA_JSON, tries=2, sleep=1.0, timeout=20.0)
        except SourceError:
            return metrics, SafetyFacts()
        pairs = [row for row in ((payload or {}).get("pairs") or []) if isinstance(row, dict)]
        ours = [
            row for row in pairs
            if str((row.get("baseToken") or {}).get("address") or "").lower() == ca.lower()
        ]
        if not ours:
            # A CA this host knows on exactly one chain is unambiguous even when the token
            # only ever appears as the quote side; a CA on several is ambiguous, and another
            # chain’s X handle on this card is worse than an empty 社媒 line.
            chains = {str(row.get("chainId") or "") for row in pairs}
            ours = pairs if len(chains) == 1 else []
        if not ours:
            return metrics, SafetyFacts()
        # This host indexes the same CA on several chains; only this chain’s pools are ours.
        pools = [row for row in ours if str(row.get("chainId") or "") == chain_id] or ours
        best = max(pools, key=lambda row: _num((row.get("liquidity") or {}).get("usd")))
        _numbers(metrics, pools, best)
        info = best.get("info") or {}
        socials: dict[str, str] = {}
        for entry in info.get("socials") or []:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("type") or "").strip() or "link"
            url = social_url(kind, entry.get("url"))
            if url:
                socials[kind] = url
        websites: list[str] = []
        for entry in info.get("websites") or []:
            if not isinstance(entry, dict):
                continue
            url = web_url(entry.get("url"))
            if url and url not in websites:
                websites.append(url)
        if socials:
            metrics.socials = socials
            metrics.provenance["socials"] = "dexscreener"
        if websites:
            metrics.websites = websites
            metrics.provenance["websites"] = "dexscreener"
        return metrics, SafetyFacts()
