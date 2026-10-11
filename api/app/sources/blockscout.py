"""Blockscout: the chain's own index, and the only complete token list Arc has.

The other sources answer "what is trading": GMGN's rank, GeckoTerminal's pools,
RadarDEX's single page of 2,000. None of them can answer "what exists", and that is the
question a universe starts from. Arc holds ~140,000 ERC-20 contracts, RadarDEX lists
4,147 of them, and not one of the fifty tokens with the most holders is in that list - a
radar built on the narrow list cannot see the tokens an operator would name first.

The explorer's token endpoint is cursor-paged and sortable by holder count, and both of
those matter here. Sorted by holders descending, the sweep stops at the prefilter's own
holder floor: a token below it cannot pass that rule whatever else is true about it, so
fetching it would only cost seconds. That turns "the whole chain" into the few thousand
tokens that could ever be shortlisted.

The explorer is reached on the browser lane - see `onchain.BLOCKSCOUT_TRANSPORT` for why
a plain httpx client gets 403 on the very same URL.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from ..core.models import SafetyFacts, TokenMetrics, ca_key
from ..core.parse import positive_float as _num
from .http import SourceError, cached, explorer_headers, json_get
from .onchain import BLOCKSCOUT_TRANSPORT, explorer_url

#: The endpoint's own page size. A different one (`items_count`) is a 422, not a bigger
#: page, so this is a constant rather than a knob.
PAGE_SIZE = 50
#: Sweep floor for a chain whose config does not name one. It sits below the prefilter's
#: holder floor on purpose: the sweep must be able to answer a threshold that is lowered.
HOLDERS_FLOOR = 50
#: A cap, so a listing that stops advancing ends the sweep instead of looping.
MAX_PAGES = 400
UNIVERSE_TTL = 1800
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _holders(row: dict) -> int:
    try:
        return int(row.get("holders_count") or 0)
    except (TypeError, ValueError):
        return 0


def _scalar(value: Any) -> Any:
    """A cursor field as the query string spells it: null as "", bool as `false`."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _cursor(next_page: dict) -> dict:
    """The parameters for the next page.

    The endpoint hands back nulls for the fields its cursor does not need and rejects a
    null with a 422, so they go back as empty strings. Booleans have to be spelled
    `false` rather than `False` - the parameter is parsed, not coerced. `sort`/`order`
    are dropped: the cursor already carries the position, and sending the ordering again
    answers with the first page.
    """
    kept = {key: _scalar(value) for key, value in next_page.items()}
    return {key: value for key, value in kept.items() if key not in ("sort", "order")}


def _metrics(ca: str, row: dict) -> dict:
    """One listing row as a `TokenMetrics`, as a plain dict so the cache can hold it."""
    return {
        "ca": ca,
        "symbol": row.get("symbol") or None,
        "market_cap_usd": _num(row.get("circulating_market_cap")),
        "volume_24h_usd": _num(row.get("volume_24h")),
        "price_usd": _num(row.get("exchange_rate")),
        "holders": _holders(row) or None,
        "provenance": {
            "holders": "blockscout", "market_cap_usd": "blockscout",
            "volume_24h_usd": "blockscout", "price_usd": "blockscout",
        },
    }


class Blockscout:
    name = "blockscout"
    chains = ("arc", "robinhood")

    def fetch_universe(self, chain_id: str, limit: int) -> list[TokenMetrics]:
        rows = cached("bs:tokens:%s" % chain_id, UNIVERSE_TTL, lambda: self._sweep(chain_id))
        out = [TokenMetrics(**row) for row in (rows or [])]
        return out[:limit] if limit else out

    def _sweep(self, chain_id: str) -> list[dict] | None:
        """Every token at or above the holder floor, newest listing state first."""
        from .. import config

        chain = config.chain(chain_id)
        floor = int((chain.get("pipeline") or {}).get("universe_holders_floor") or HOLDERS_FLOOR)
        url = "%s/api/v2/tokens" % explorer_url(chain).rstrip("/")
        params: dict[str, Any] = {"type": "ERC-20", "sort": "holders_count", "order": "desc"}
        out: list[dict] = []
        seen: set[str] = set()
        for _attempt in range(MAX_PAGES):
            try:
                body = json_get(
                    "%s?%s" % (url, urlencode(params)), explorer_headers(explorer_url(chain)),
                    transport=BLOCKSCOUT_TRANSPORT, tries=2, sleep=2.0, timeout=30.0,
                )
            except SourceError:
                break
            items = (body or {}).get("items") or []
            if not items:
                break
            under = False
            for row in items:
                if _holders(row) < floor:
                    # Ordered by holder count: the first row under the floor means every
                    # row after it is under it too.
                    under = True
                    break
                ca = ca_key(row.get("address_hash"))
                if not ca or ca in seen:
                    continue
                seen.add(ca)
                out.append(_metrics(ca, row))
            next_page = (body or {}).get("next_page_params")
            if under or not next_page:
                break
            params = _cursor(next_page)
            params["type"] = "ERC-20"
        return out or None

    def fetch_token(self, chain_id: str, ca: str) -> tuple[TokenMetrics, SafetyFacts]:
        """One token, from the chain's own index. Sparse fields are left empty, never zero."""
        if not ADDRESS_RE.match(str(ca or "")):
            return TokenMetrics(ca=ca), SafetyFacts()
        from .. import config

        chain = config.chain(chain_id)
        url = "%s/api/v2/tokens/%s" % (explorer_url(chain).rstrip("/"), ca)
        try:
            body = json_get(url, explorer_headers(explorer_url(chain)),
                            transport=BLOCKSCOUT_TRANSPORT,
                            tries=2, sleep=2.0, timeout=20.0)
        except SourceError:
            return TokenMetrics(ca=ca), SafetyFacts()
        if not isinstance(body, dict):
            return TokenMetrics(ca=ca), SafetyFacts()
        row = _metrics(ca_key(ca), body)
        row.pop("ca", None)
        return TokenMetrics(ca=ca, **row), SafetyFacts()
