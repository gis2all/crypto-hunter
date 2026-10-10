"""The mark on the pools payload: a property of the token, read from the resource
GeckoTerminal includes beside it instead of from a second request.

The trap is that `base_token` is the base side of *each* pool, and the pools endpoint answers
pools where the CA is either side. A mark read without checking the address is another
token's picture on this card - so the address check is what the first two tests are about.
"""

import pytest

from app.sources import geckoterminal


POOLS = {
    "data": [{
        "id": "robinhood_0xpool",
        "type": "pool",
        "attributes": {"name": "CASINO / USDG", "address": "0xpool",
                       "reserve_in_usd": "37802.84", "volume_usd": {"h24": "1234.5"}},
    }],
    "included": [
        {"id": "robinhood_0xother", "type": "token",
         "attributes": {"address": "0xother", "image_url": "https://assets.geckoterminal.com/other"}},
        {"id": "robinhood_0xabc", "type": "token",
         "attributes": {"address": "0xabc", "image_url": "https://assets.geckoterminal.com/ours"}},
    ],
}


def _pools(monkeypatch, payload):
    monkeypatch.setattr(geckoterminal, "json_get", lambda *args, **kwargs: payload)
    monkeypatch.setattr(geckoterminal, "cached", lambda _key, _ttl, produce: produce())
    return geckoterminal.GeckoTerminal()._pools("robinhood", "0xabc")


def test_the_mark_comes_from_the_resource_that_matches_the_ca(monkeypatch):
    rows, mark = _pools(monkeypatch, POOLS)
    assert mark == "https://assets.geckoterminal.com/ours"
    assert rows and rows[0]["pool"] == "0xpool", "the pools still read as pools"


def test_the_other_side_of_the_pool_is_not_this_card_mark(monkeypatch):
    only_other = {"data": POOLS["data"], "included": POOLS["included"][:1]}
    rows, mark = _pools(monkeypatch, only_other)
    assert rows, "the pools are still the token's own"
    assert mark is None, "another token's picture is worse than an empty box"


def test_nothing_to_read_is_no_mark(monkeypatch):
    assert _pools(monkeypatch, {"data": []}) == (None, None)


def test_fetch_token_stamps_the_mark_and_where_it_came_from(monkeypatch):
    source = geckoterminal.GeckoTerminal()
    monkeypatch.setattr(source, "_pools", lambda _chain, _ca: (
        [{"name": "CASINO / USDG", "pool": "0xpool", "liq": 37802.84, "vol24": 1234.5}],
        "https://assets.geckoterminal.com/ours",
    ))
    monkeypatch.setattr(source, "journey", lambda *_args, **_kwargs: None)

    metrics, _facts = source.fetch_token("robinhood", "0xabc")

    assert metrics.logo_url == "https://assets.geckoterminal.com/ours"
    assert metrics.provenance["logo_url"] == "geckoterminal"


def test_a_pool_page_that_never_answered_is_not_an_empty_chain(monkeypatch):
    """This is the enumerator for Solana, so a page that failed answered as `[]` would file
    throttling as a quiet market - the report reads `ok=True, n=0`, exactly like a chain with
    no pools - and the round would carry on with an empty universe.

    Nothing readable at all raises. One page read, even an empty one, is what tells the two
    apart, which is why the second half is not an error.
    """
    from app.sources.http import SourceError

    source = geckoterminal.GeckoTerminal()

    def dead(*_args, **_kwargs):
        raise SourceError("HTTP 429")

    monkeypatch.setattr(geckoterminal, "json_get", dead)
    with pytest.raises(SourceError):
        source._pages("solana", [1], ["new_pools"])

    monkeypatch.setattr(geckoterminal, "json_get", lambda *_args, **_kwargs: {"data": []})
    assert source._pages("solana", [1], ["new_pools"]) == []
