"""Sourcify: the second opinion on "the source is published".

Independent of GoPlus on purpose - the two disagree often enough on Arc that a single
yes would be a guess, and the settings page shows both. A contract that is absent from
Sourcify answers 404, which is not an error: it is the answer "not verified here".
"""

from __future__ import annotations

from ..core.models import SafetyFacts, TokenMetrics
from .http import SourceError, UA_JSON, cached, json_get

BASE = "https://sourcify.dev/server/v2"
#: Sourcify identifies chains by EVM chain id too: Arc 5042, Robinhood 4663.
CHAIN_IDS = {"arc": 5042, "robinhood": 4663}
MATCHED = ("exact_match", "match")
TTL = 24 * 3600


class Sourcify:
    name = "sourcify"
    chains = ("arc", "robinhood")

    def fetch_universe(self, chain_id: str, limit: int) -> list[TokenMetrics]:
        """Sourcify is asked about a CA, one at a time; there is no chain listing."""
        return []

    def contract(self, chain_id: str, ca: str) -> dict:
        chain_number = CHAIN_IDS.get(chain_id)
        if chain_number is None:
            return {}

        def produce():
            url = "%s/contract/%d/%s?fields=compilation" % (BASE, chain_number, ca)
            try:
                # One try on purpose: a 404 is Sourcify answering "no record here", which
                # is an answer, not a failure - and a retry costs every token two seconds of
                # round time to hear the same sentence again.
                payload = json_get(url, UA_JSON, tries=1, sleep=2.0, timeout=25.0)
            except SourceError:
                # 404 is "no record", and the transport cannot tell it from a failure, so
                # remember the no-record answer explicitly instead of re-asking every round.
                return {"match": "none"}
            return payload if isinstance(payload, dict) else {"match": "none"}

        return cached("sourcify:%s:%s" % (chain_id, ca), TTL, produce) or {}

    def fetch_token(self, chain_id: str, ca: str) -> tuple[TokenMetrics, SafetyFacts]:
        row = self.contract(chain_id, ca)
        facts = SafetyFacts()
        if not row:
            return TokenMetrics(ca=ca), facts  # the verifier did not answer
        facts.source_checked_by = ["Sourcify"]
        match = row.get("match") or "none"
        verified = match in MATCHED
        facts.source_match = match
        facts.source_verified = verified
        if verified:
            facts.source_verified_by = ["Sourcify"]
        name = ((row.get("compilation") or {}).get("name")) if isinstance(row.get("compilation"), dict) else None
        if name:
            facts.source_name = name
        return TokenMetrics(ca=ca), facts
