"""RadarDEX: the Arc registry that ships a pool count and a deployer."""

from app.sources.arc import radardex

CA_A = "0x" + "aa" * 20
CA_B = "0x" + "bb" * 20


def _row(ca, mcap=50000, liq=12000, **kw):
    row = {"address": ca, "symbol": "T", "mcap": mcap, "liquidityUsdc": liq,
           "volume24": 900, "pools": 2, "holderCount": 40, "price": 0.001,
           "change24h": 3.0}
    row.update(kw)
    return row


def test_a_row_with_no_market_data_is_not_handed_over(monkeypatch):
    """Same shape Tolly ships as placeholders: an address, no market cap, no reserve. The
    free half's 空壳淘汰 rule drops it a moment later, so the adapter skips it on the same
    predicate - and the kept row still carries every field it carried before."""
    rows = {CA_A: _row(CA_A), CA_B: _row(CA_B, mcap=None, liq=None)}
    monkeypatch.setattr(radardex.RadarDEX, "rows", lambda self, chain_id: rows)
    kept = radardex.RadarDEX().fetch_universe("arc", 0)
    assert [m.ca for m in kept] == [CA_A]
    assert (kept[0].market_cap_usd, kept[0].liquidity_usd, kept[0].pools) == (50000, 12000, 2)
    assert len(radardex.RadarDEX().fetch_universe("arc", 1)) == 1
