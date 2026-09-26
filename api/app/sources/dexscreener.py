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
- **It fills links, and nothing else.** Its market cap, liquidity and volume answer the same
  questions the scanners already answer better, and a fourth opinion on a number the score is
  built from is how a round’s numbers start moving without anyone changing a rule.
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
        best = max(ours, key=lambda row: _num((row.get("liquidity") or {}).get("usd")))
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
