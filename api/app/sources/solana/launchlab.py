"""Raydium LaunchLab: the launchpad behind bonk.fun, and this chain's first enumeration
of tokens that do not have a pool yet.

`launch-mint-v1.raydium.io/get/list` is the only public list endpoint for the LaunchLab
programs. The published API (`api-v3.raydium.io`) has no LaunchLab binding in its Swagger
at all, and `launch-mint-v1` answers without a key or a referer check - the site's own
origin is sent anyway, because a header a page always sends is not worth guessing about.

**bonk.fun is not a second source.** It is a `platformId` of this program: its frontend
calls these same endpoints with `platformId=FfYek5vEz23cMkWsdJwG2oa6EphsvXSHrGpdALN4g6W1`
(or `82NMHVCKwehXgbXMyzL41mvv3sdkypaMCtTxvJ4CtTzm`). Asking without a `platformId`
returns every platform at once - the first page of the probe came back named `letsbonk.fun`
- so one sweep covers bonk.fun, Raydium's own launches and the rest, and the row's own
`platformInfo.name` is what the card prints as the launchpad.

**`sort` accepts three values**, measured: `hotToken`, `new` and `marketCap`;
anything else is a 500 `sort type error`. `size` is capped at 100 (500 above it) and the
next page comes from the envelope's `nextPageId` rather than an offset.

**`volumeU` is deliberately not read.** It looks like a day's trade and is not: measured
against DexScreener on 2026-10-11 the same tokens carried `volumeU` 0 where their pools had
traded 62,759 / 21,138 / 9,331 over 24h - the column stops moving once a token migrates, so
it is the curve's own total. Reading it as `volume_24h_usd` would hand the volume floor a
number from the wrong window, which is the one mistake `collect.py`'s field table exists to
prevent. `marketCap` *is* dollars (measured equal to DexScreener's `marketCap` to the
dollar on the same token), and the row's own high and day change are not published here.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ...core.models import SafetyFacts, TokenMetrics, ca_key
from ...core.parse import positive_float as _num, utc_from_unix
from ..base import socials_from
from ..http import SourceError, UA_JSON, cached, json_get

BASE = "https://launch-mint-v1.raydium.io"
HEADERS = {**UA_JSON, "Referer": "https://raydium.io/"}
#: Measured: 100 is the ceiling `size` accepts, and the envelope carries the next cursor.
PAGE_SIZE = 100
#: The band is crowded into the head of the order: measured on 2026-10-11, `hotToken` page 1
#: held 31 of the 32 rows in this chain's market-cap band and the two pages behind it added 1
#: and 1. Three pages, so a busy hour cannot push a whole page out from under the sweep.
MAX_PAGES = 3
SORT = "hotToken"
UNIVERSE_TTL = 900

#: The artwork the launchpad publishes is already an absolute URL on its own image host.
HTTP = re.compile(r"^https?://")


def _when(value: Any) -> datetime | None:
    """`createAt` is milliseconds. A value that will not read as one is a gap, and a gap
    must not become a timestamp of the epoch."""
    return utc_from_unix(value, unit="ms", require_positive=True)


class LaunchLab:
    name = "launchlab"
    chains = ("solana",)

    def rows(self, chain_id: str) -> dict[str, dict]:
        """Every launch this source lists, keyed by mint."""

        def produce():
            merged: dict[str, dict] = {}
            cursor = None
            for _ in range(MAX_PAGES):
                url = "%s/get/list?sort=%s&size=%d" % (BASE, SORT, PAGE_SIZE)
                if cursor:
                    url += "&nextPageId=%s" % cursor
                try:
                    payload = json_get(url, HEADERS, tries=2, sleep=2.0, timeout=45.0)
                except SourceError:
                    break
                data = payload.get("data") if isinstance(payload, dict) else None
                items = data.get("rows") if isinstance(data, dict) else None
                if not items:
                    break
                for row in items:
                    ca = ca_key(row.get("mint"))
                    if ca and ca not in merged:
                        merged[ca] = row
                cursor = data.get("nextPageId")
                if not cursor or len(items) < PAGE_SIZE:
                    break
            return merged or None

        return cached("launchlab:list:%s" % chain_id, UNIVERSE_TTL, produce) or {}

    def fetch_universe(self, chain_id: str, limit: int) -> list[TokenMetrics]:
        out = [self._metrics(ca, row) for ca, row in self.rows(chain_id).items()]
        return out[:limit] if limit else out

    def fetch_token(self, chain_id: str, ca: str) -> tuple[TokenMetrics, SafetyFacts]:
        row = self.rows(chain_id).get(ca_key(ca))
        if not row:
            return TokenMetrics(ca=ca), SafetyFacts()
        return self._metrics(ca_key(ca), row), SafetyFacts()

    def _metrics(self, ca: str, row: dict) -> TokenMetrics:
        platform = (row.get("platformInfo") or {}).get("name")
        metrics = TokenMetrics(
            ca=ca,
            symbol=row.get("symbol") or None,
            description=str(row.get("description") or "").strip() or None,
            market_cap_usd=_num(row.get("marketCap")),
            launchpad=str(platform).strip() if platform else "launchlab",
            created_at=_when(row.get("createAt")),
            logo_url=_image(row.get("imgUrl")),
            socials=_socials(row),
        )
        metrics.creator = row.get("creator") or None
        metrics.provenance = {
            "market_cap_usd": "launchlab", "created_at": "launchlab",
            "launchpad": "launchlab", "creator": "launchlab", "logo_url": "launchlab",
            "description": "launchlab", "socials": "launchlab",
        }
        return metrics


def _image(value: Any) -> str | None:
    """`imgUrl` is an absolute URL on the launchpad's own image host; anything that is not
    one is no mark, rather than a link the card would print and the reader would click."""
    text = str(value or "").strip()
    return text if HTTP.match(text) else None


def _socials(row: dict) -> dict[str, str]:
    return socials_from(row, (("twitter", "twitter"),))
