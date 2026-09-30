"""The field-wise merge, and what counts as an answer.

One reading lives in two places - the bulk feed's row and the per-token verdict - and the
merge picks between them per field. A list field whose default is empty is the trap: the
first source in the order carries the field, so an empty list wins it and the source that
actually answered is never read.
"""

from app.core.models import TokenMetrics
from app.pipeline.collect import merge


def test_an_empty_list_does_not_win_a_field():
    bulk = TokenMetrics(ca="0x1", symbol="A")
    detail = TokenMetrics(
        ca="0x1",
        pool_list=[{"pair": "0xaa"}],
        top_holders=[{"address": "0xbb"}],
    )
    out = merge("0x1", {"gmgn": bulk, "goplus": detail}, ("gmgn", "goplus"))
    assert out.symbol == "A"
    assert out.pool_list == [{"pair": "0xaa"}]
    assert out.top_holders == [{"address": "0xbb"}]
    assert out.provenance["pool_list"] == "goplus"
    assert out.provenance["top_holders"] == "goplus"


def test_every_tracker_the_cards_link_to_writes_a_url_the_row_can_hold():
    """The footer prints one link per tracker in `TOKEN_URLS`, and `pull` fills them by name
    (`<tracker>_url`). A tracker whose field does not exist would raise at the very end of a
    real round - after the scan, not before it - so the pairing is checked here. The shape
    rides along: DexScreener spells a token page `/chain/address`, the same way it spells a pool.
    """
    from app.pipeline.collect import TOKEN_URLS, token_url

    row = TokenMetrics(ca="0x1")
    for tracker in TOKEN_URLS:
        setattr(row, tracker + "_url", token_url(tracker, "arc", "0x1"))

    assert row.dexscreener_url == "https://dexscreener.com/arc/0x1"
    assert row.gmgn_url == "https://gmgn.ai/arc/token/0x1"

def test_a_measured_zero_still_wins_its_field():
    """The rule above is about lists only: 0 is a reading and keeps its field."""
    bulk = TokenMetrics(ca="0x1", top10_pct=0.0)
    detail = TokenMetrics(ca="0x1", top10_pct=22.1)
    out = merge("0x1", {"gmgn": bulk, "goplus": detail}, ("gmgn", "goplus"))
    assert out.top10_pct == 0.0
    assert out.provenance["top10_pct"] == "gmgn"

def test_the_tax_comes_from_the_source_that_measures_it():
    """GoPlus answers "0" for every Arc token (24/24, 2026-09-25), so it is not in the
    tax priority at all - a placeholder must not be able to fill the field, and a token
    only the bulk row covers shows no tax rather than a false zero."""
    from app.pipeline.collect import FIELD_PRIORITY

    assert FIELD_PRIORITY["buy_tax_pct"] == ("gmgn",)
    assert FIELD_PRIORITY["sell_tax_pct"] == ("gmgn",)
    bulk = TokenMetrics(ca="0x1", symbol="A")
    detail = TokenMetrics(ca="0x1", buy_tax_pct=0.0, sell_tax_pct=0.0)
    out = merge("0x1", {"gmgn": bulk, "goplus": detail}, ("gmgn", "goplus"))
    assert out.buy_tax_pct is None
    assert out.sell_tax_pct is None
    assert "buy_tax_pct" not in out.provenance



def test_the_market_caliber_leads_the_numbers_the_rules_read():
    """DexScreener is the round's market caliber, so it has to outrank the bulk feeds on
    the four numbers the floors and the sort read.

    It was missing from all four lists, so a per-token answer was bought, merged in and
    then dropped on the floor: 0 of 17,573 stored snapshots had ever credited it with a
    market number (measured 2026-09-29). It has no bulk sweep, so leading a list cannot
    override a feed's own row - it only decides the enrich merge, which is the point.
    """
    from app.pipeline.collect import FIELD_PRIORITY

    for field in ("market_cap_usd", "liquidity_usd", "volume_24h_usd", "price_usd"):
        assert FIELD_PRIORITY[field][0] == "dexscreener", field
    # GMGN answers 24h change in the ranking window's units, so it is listed - and the
    # two feeds that ship a real change24h sit right behind it.
    assert FIELD_PRIORITY["price_change_24h_pct"][:3] == ("gmgn", "radardex", "tolly")
