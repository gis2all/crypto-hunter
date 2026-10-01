"""The field-wise merge, and what counts as an answer.

One reading lives in two places - the bulk feed's row and the per-token verdict - and the
merge picks between them per field. A list field whose default is empty is the trap: the
first source in the order carries the field, so an empty list wins it and the source that
actually answered is never read.
"""

from app.core.models import TokenMetrics
from app.pipeline.collect import merge, share_site


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


def test_the_three_trading_links_are_stamped_by_one_function_both_paths_call():
    """A round stamps these in the collect pass; a CA search never runs one, so it calls the
    same function (app/lookup.py `read`). This is the card's link row, in order."""
    from app.pipeline.collect import stamp_trackers
    from app.report.cards import links

    row = TokenMetrics(ca="0x1")
    stamp_trackers(row, "robinhood", "0x1")
    assert links(row.model_dump(mode="json"))[:3] == [
        {"label": "GMGN", "url": "https://gmgn.ai/robinhood/token/0x1"},
        {"label": "Debot", "url": "https://debot.ai/token/robinhood/0x1"},
        {"label": "DexScreener", "url": "https://dexscreener.com/robinhood/0x1"},
    ]


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



def test_the_name_has_a_source_on_a_chain_no_launchpad_adapter_knows():
    """Symbol is listed field by field, and DexScreener now sits at its end.

    The list named only launchpad adapters, so a token that never came from one - TIDE on
    Robinhood is an ordinary Uniswap pool - had no source that could answer and its card
    went out with no name. It goes last rather than next to the other feed-wide readings:
    the launchpad the token did come from keeps its own spelling, and this fills the gap.
    """
    from app.pipeline.collect import FIELD_PRIORITY

    order = FIELD_PRIORITY["symbol"]
    assert order[-1] == "dexscreener"
    assert "pez" in order and "coinbarrel" in order

    gap = merge("0x1", {"dexscreener": TokenMetrics(ca="0x1", symbol="TIDE")}, order)
    assert gap.symbol == "TIDE" and gap.provenance["symbol"] == "dexscreener"

    both = merge(
        "0x1",
        {"dexscreener": TokenMetrics(ca="0x1", symbol="TIDE-ON-THE-FEED"),
         "pez": TokenMetrics(ca="0x1", symbol="Tide")},
        order,
    )
    assert both.symbol == "Tide" and both.provenance["symbol"] == "pez"


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


def test_the_site_a_search_proved_leads_the_list_and_names_itself():
    """The card prints one site and it is this one, so it goes first - and `search` joins the
    provenance, because it is one of the sources this union field is now built from."""
    row = TokenMetrics(ca="0x1", websites=["https://a.fun"],
                       provenance={"websites": "gmgn"})

    share_site(row, "https://b.fun/")

    assert row.websites == ["https://b.fun/", "https://a.fun"]
    assert row.provenance["websites"] == "gmgn,search"


def test_sharing_the_same_site_twice_is_one_entry():
    row = TokenMetrics(ca="0x1", websites=["https://b.fun/"])

    share_site(row, "https://b.fun/")

    assert row.websites == ["https://b.fun/"]
    assert row.provenance["websites"] == "search"


def test_sharing_nothing_leaves_the_row_alone():
    """A search that proved no site is not a search that proved an empty one."""
    row = TokenMetrics(ca="0x1", websites=["https://a.fun"])

    share_site(row, "")
    share_site(row, None)
    share_site(None, "https://b.fun/")

    assert row.websites == ["https://a.fun"]
    assert row.provenance == {}


def test_a_plain_row_is_shared_the_same_way():
    """A CA search and the research pass hand in a dict, not a built row."""
    row = {"websites": ["https://a.fun"], "provenance": {}}

    share_site(row, "https://b.fun/")

    assert row["websites"] == ["https://b.fun/", "https://a.fun"]
    assert row["provenance"] == {"websites": "search"}
