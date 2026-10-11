"""The adapter contract every source implements."""

from __future__ import annotations

from datetime import datetime
from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ..core.models import SafetyFacts, TokenMetrics


class SourceReport(BaseModel):
    """What one adapter did in one round: enough to tell "no data" from "no tokens"."""

    source: str
    ok: bool
    fetched_at: str = Field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))
    records: int = 0
    error: str | None = None


@runtime_checkable
class Source(Protocol):
    name: str
    chains: tuple[str, ...]

    def fetch_universe(self, chain_id: str, limit: int) -> list[TokenMetrics]:
        """Bulk, pool-level facts. Must be cheap: it runs against the whole chain."""

    def fetch_token(self, chain_id: str, ca: str) -> tuple[TokenMetrics, SafetyFacts]:
        """Per-token enrichment. Only ever called for prefilter survivors."""


#: The bulk registries publish a handle, not a link: RadarDEX ships `Solonlabs1` and
#: `solonpad.fun`, GMGN ships `twitter_username` the same way. Pasted into a card as they
#: arrive they are text shaped like a link - the reference implementation printed
#: `X [Solonlabs1]` beside a clickable 官网, which is one dead link and one live one in the
#: same line. One table, so the three sources that read these columns cannot each fix it
#: their own way.
SOCIAL_HOSTS = {
    "twitter": "https://x.com/",
    "telegram": "https://t.me/",
    "discord": "https://discord.gg/",
}


def social_url(kind: str, value: Any) -> str | None:
    """One social column as a URL. A value with a path is a host already (`t.me/foo`), a
    value without one is a handle, and a value that is already a URL is left alone."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("http://") or text.startswith("https://"):
        return text
    if "/" in text:
        return "https://" + text.lstrip("/")
    host = SOCIAL_HOSTS.get(kind)
    return host + text.lstrip("@") if host else text


def web_url(value: Any) -> str | None:
    """A website column as a URL. A bare domain fetched as-is is not clickable, and the
    reader assumes it is, which is how a 官网 link gets clicked once and never again."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return "https://" + text.lstrip("/")


#: The social columns the registries publish flat, in the order the card lists them.
SOCIAL_COLUMNS = (("twitter", "twitter"), ("telegram", "telegram"), ("discord", "discord"))


def socials_from(source: Any, columns: Iterable[tuple[str, str]]) -> dict[str, str]:
    """One row's social columns as `{kind: url}`, with the columns nobody filled left out.

    `columns` pairs each column name with the kind it feeds, because the same kind arrives
    under different names (`twitter_username` on GMGN) and the row is sometimes flat and
    sometimes nested under `socials` - which is the caller's to unwrap.
    """
    out: dict[str, str] = {}
    for key, kind in columns:
        url = social_url(kind, (source or {}).get(key))
        if url:
            out[kind] = url
    return out


def website_list(value: Any) -> list[str]:
    """One website column as the list the snapshot carries: the link, or no entry at all. The
    card prints whatever is in this list, so an entry holding a broken link is worse than a
    list that stays empty."""
    url = web_url(value)
    return [url] if url else []
