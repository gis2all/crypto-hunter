"""Tolly: the second full-chain registry, and the long tail no ranking reaches.

The endpoint clamps `limit` to 120 whatever you ask for, answers a page in about four
seconds, and reports the chain total on page one. It takes no range filter - the market
cap and floor parameters it ignores were measured, not assumed - but it does take
`sort=mcap`, and that is what changed the pull: the chain is a spike, so everything above
the round floor sits in the first pages and the walk stops there instead of reading all 41.

Two reads make up a pull: the default first page, which is what catches a token too new to
have been ranked, and the capped walk. Paging is `offset` over a live ordering, so the same
CA can arrive twice: keyed by CA, a repeat costs nothing, and a refused page costs its own
120 rows rather than the rest of the walk.
"""

import re

from app.pipeline import collect
from app.sources import tolly

CA_A = "0x" + "aa" * 20
CA_B = "0x" + "bb" * 20
CA_C = "0x" + "cc" * 20


def _row(ca, symbol="T", mcap=1000, liq=500, vol=0, **kw):
    row = {"address": ca, "symbol": symbol, "marketCap": mcap, "liquidity": liq,
           "volume24h": vol, "price": 0.001, "created_ts": 1790270268, "pool": "0xpool"}
    row.update(kw)
    return row


def _offset(url):
    return int(re.search(r"offset=(\d+)", url).group(1))


def _order(url):
    return "mcap" if "sort=" in url else "default"


def _serve(monkeypatch, total, pages=None, walk=None, fails=()):
    """One call per (order, offset): `pages` answers the default order, `walk` the sorted
    one, and an offset in `fails` is refused whichever order asks for it."""
    calls = []

    def fake(url, headers=None, **_kw):
        calls.append(url)
        offset = _offset(url)
        if offset in fails:
            raise tolly.SourceError("503")
        table = walk if _order(url) == "mcap" else pages
        return {"tokens": (table or {}).get(offset, []), "total": total}

    monkeypatch.setattr(tolly, "json_get", fake)
    # The cache would make two tests share one pull, which is not what any of these is
    # about.
    monkeypatch.setattr(tolly, "cached", lambda key, ttl, produce: produce())
    return calls


def _asked(calls, order=None):
    """The offsets one order asked for, in request order."""
    return [_offset(url) for url in calls if order is None or _order(url) == order]


def _source(floor=0.0):
    source = tolly.Tolly()
    source.market_cap_floor = floor
    return source


def test_a_pull_is_the_default_page_plus_the_market_cap_walk(monkeypatch):
    """Two reads, and the default order is read exactly once: it is there for the token
    that is too new to have been ranked, not for the long tail."""
    calls = _serve(monkeypatch, total=240, pages={0: [_row(CA_A)]},
                   walk={0: [_row(CA_B)], 120: [_row(CA_C)]})
    rows = _source().rows("arc")
    assert sorted(rows) == [CA_A, CA_B, CA_C]
    assert _asked(calls, "default") == [0]
    assert _asked(calls, "mcap") == [0, 120]
    assert any("sort=mcap&dir=desc" in url for url in calls)
    assert not any("sort=" in url for url in calls if _order(url) == "default")


def test_the_walk_stops_on_the_page_that_holds_nothing_above_the_floor(monkeypatch):
    """1200 tokens is ten pages. With a floor under page two, the other eight are never
    asked for - that is the whole point of the sorted ordering."""
    calls = _serve(monkeypatch, total=1200,
                   walk={0: [_row(CA_A, mcap=90000)], 120: [_row(CA_B, mcap=9000)]})
    assert sorted(_source(10_000).rows("arc")) == [CA_A, CA_B]
    assert _asked(calls, "mcap") == [0, 120]


def test_a_page_without_prices_ends_the_walk(monkeypatch):
    """Rows with no market cap sort to the end, so a page of them means the priced part of
    the chain is behind us. Reading on would be walking the launchpad placeholders."""
    calls = _serve(monkeypatch, total=1200,
                   walk={0: [_row(CA_A, mcap=50000)], 120: [_row(CA_B, mcap=None)]})
    assert sorted(_source(10_000).rows("arc")) == [CA_A, CA_B]
    assert _asked(calls, "mcap") == [0, 120]


def test_without_a_floor_the_walk_reads_every_page(monkeypatch):
    """No floor means no early stop, which is the safe default: a scanner that has not been
    told where the line is must not guess one."""
    calls = _serve(monkeypatch, total=360,
                   walk={0: [_row(CA_A)], 120: [_row(CA_B)], 240: [_row(CA_C)]})
    assert sorted(_source().rows("arc")) == [CA_A, CA_B, CA_C]
    assert _asked(calls, "mcap") == [0, 120, 240]


def test_the_floor_never_drops_a_row_that_arrived(monkeypatch):
    """The stop is a shortcut, not a filter. A row under the floor and a row with no price
    at all are both returned; the prefilter is the only thing that drops a token."""
    _serve(monkeypatch, total=240,
           walk={0: [_row(CA_A, mcap=50000)], 120: [_row(CA_B, mcap=10), _row(CA_C, mcap=None)]})
    assert sorted(_source(10_000).rows("arc")) == [CA_A, CA_B, CA_C]


def test_a_ca_seen_in_both_orders_keeps_the_first_sighting(monkeypatch):
    """The ordering shifts while the pull runs, so about 11% of the slots repeat a token,
    and the two orders overlap by construction. Keyed by CA, a repeat costs nothing."""
    _serve(monkeypatch, total=120, pages={0: [_row(CA_A, symbol="FIRST")]},
           walk={0: [_row(CA_A, symbol="SECOND")]})
    rows = _source().rows("arc")
    assert list(rows) == [CA_A]
    assert rows[CA_A]["symbol"] == "FIRST"


def test_a_refused_page_costs_only_its_own_rows(monkeypatch):
    """One 503 is 120 tokens, not the round: the other pages are already in flight."""
    _serve(monkeypatch, total=360, pages={0: [_row(CA_A)]},
           walk={0: [_row(CA_B)], 120: [_row(CA_C)], 240: [_row(CA_A, symbol="DUP")]},
           fails=(120,))
    assert sorted(_source().rows("arc")) == [CA_A, CA_B]


def test_a_dead_host_yields_nothing_rather_than_an_exception(monkeypatch):
    """Both reads failing means there is no total and no walk. The caller records a failed
    source; it must not take the round down."""
    calls = _serve(monkeypatch, total=0, fails=(0,))
    assert _source().rows("arc") == {}
    assert _asked(calls) == [0, 0]


def test_an_empty_chain_reads_one_page_in_each_order(monkeypatch):
    calls = _serve(monkeypatch, total=0, pages={0: []}, walk={0: []})
    assert _source().rows("arc") == {}
    assert _asked(calls) == [0, 0]


def test_the_round_tells_the_walk_where_to_stop():
    """`collect.set_scan_floor` is the one way a round hands its own floor to a scanner.
    An unusable value has to mean read everything, never read nothing."""

    class Scanner:
        market_cap_floor = 0.0

    scanner = Scanner()
    collect.set_scan_floor([scanner, object()], "15000")
    assert scanner.market_cap_floor == 15000.0
    collect.set_scan_floor([scanner], None)
    assert scanner.market_cap_floor == 0.0
    collect.set_scan_floor([scanner], "not a number")
    assert scanner.market_cap_floor == 0.0


def test_a_zero_volume_is_a_number_and_a_zero_market_cap_is_a_gap():
    """`volume24h == 0` means nobody traded it, and the volume floor has to be able to
    say no. A zero market cap means the source has no price at all, which is a gap."""
    row = tolly.Tolly()._metrics(CA_A, _row(CA_A, mcap=0, liq=0, vol=0))
    assert row.volume_24h_usd == 0
    assert row.market_cap_usd is None and row.liquidity_usd is None


def test_the_row_carries_market_facts_and_never_a_holder_count():
    """Holder counts belong to RadarDEX. A source that does not publish one must not
    appear to."""
    row = tolly.Tolly()._metrics(CA_A, _row(CA_A, mcap=52000, liq=12000, vol=900,
                                              creator="0xdev", holders=4242))
    assert row.market_cap_usd == 52000 and row.liquidity_usd == 12000
    assert row.volume_24h_usd == 900
    assert row.holders is None
    assert row.creator == "0xdev"
    assert row.provenance["market_cap_usd"] == "tolly"
    assert "holders" not in row.provenance


def test_the_launch_flag_becomes_a_launchpad_name():
    assert tolly.Tolly()._metrics(CA_A, _row(CA_A, tolly=True)).launchpad == "tolly"
    assert tolly.Tolly()._metrics(CA_A, _row(CA_A)).launchpad is None
