"""The BSC launchpad feed: the half of a row that is a measurement and the half that is not.

four.meme publishes `cap`, `volume` and a ladder of window volumes on every row, and none
of them is read here. The rows below are real captured answers, trimmed, and they are the
evidence for that:

- `cap` is not DexScreener market cap. Measured 2026-10-11, AKE carried 2,703,401,221 in
  this column against 616,653,496 there (x4.4) while the row beside it agreed to five
  digits (560,303,067 against 560,082,849).
- `day1Vol` is usually the day's trade and sometimes is not: AKE 595,445.97 against a real
  621,626.82, `$BANANA` 360.33 against 1,652,020.09, Tag 55,976,851.46 against 208,838.20.
  `volume` disagrees with `day1Vol` on the same row, and the captured rows show it in both
  directions (`volume` 859.69 beside `day1Vol` 599,093.06).
- `hold` was 0 on all 20 rows measured, including rows with seven-figure caps.
- `symbol` is the currency the curve prices in ("BNB"), not the ticker. The ticker is
  `shortName`.

What is left is what only this feed has: ticker, deployer, creation instant, artwork, and
the fact that the token came from four.meme. Nothing here reaches the network.
"""

import pytest

from app import config
from app.pipeline.collect import FIELD_PRIORITY
from app.sources import registry
from app.sources.bsc import fourmeme
from app.sources.http import SourceError

#: A graduated row off the market-cap order (captured 2026-10-11, trimmed).
CAP_ROW = {
    "tokenAddress": "0x0e920821464625cf661a4dcedeb6406e1b48ffff",
    "shortName": "SPX001",
    "name": "SPACEBOX",
    "symbol": "BNB",
    "cap": "871887.66577971856",
    "volume": "3435.658160264497606605",
    "day1Vol": "2583.59",
    "hold": 0,
    "progress": "1",
    "status": "TRADE",
    "dexType": 0,
    "img": "/market/614lxn34nDDjZ.jpg",
    "userAddress": "0x023568d23eb48fb062de2255c2373f96ce0728c1",
    "createDate": "1781407514000",
    "tag": "AI",
    "version": 9,
}

#: A just-graduated row off the newest-first order (captured the same day, trimmed).
NEW_ROW = {
    "tokenAddress": "0x7d6d4b711fb7c4aad9f162d93e8e51034ab5ffff",
    "shortName": "4cat",
    "name": "4cat",
    "symbol": "QQQB",
    "cap": "47554.44531205748",
    "volume": "859.691512534692928331",
    "day1Vol": "599093.06",
    "hold": 0,
    "progress": "1",
    "status": "TRADE",
    "dexType": 0,
    "img": "/market/1011LPrgWWrMEk.png",
    "userAddress": "0x97087c8a602fdaf783c592af742619ab02efdd63",
    "createDate": "1791649771000",
    "tag": "Meme",
    "version": 9,
}


@pytest.fixture()
def no_cache(monkeypatch):
    """`cached` is a disk cache; a test wants the producer to run."""
    monkeypatch.setattr(fourmeme, "cached", lambda key, ttl, produce: produce())


def test_the_launchpad_only_claims_the_chain_it_indexes():
    assert fourmeme.FourMeme().chains == ("bsc",)
    assert registry.ALL["fourmeme"] is not None


def test_the_ticker_is_the_short_name_and_not_the_currency_column():
    """`symbol` is the currency the curve prices in - "BNB" on every row measured - so a
    card titled from it would call every token on this chain BNB."""
    metrics = fourmeme.FourMeme()._metrics("0xaa", CAP_ROW)
    assert metrics.symbol == "SPX001"
    assert fourmeme.FourMeme()._metrics("0xaa", {**CAP_ROW, "shortName": ""}).symbol is None


def test_the_row_market_numbers_are_not_read_even_though_the_columns_are_filled():
    """Every one of these columns carries a number on this row, and not one of them is the
    number the rule reads - see the module docstring for the measurements."""
    metrics = fourmeme.FourMeme()._metrics("0xaa", CAP_ROW)
    assert metrics.market_cap_usd is None
    assert metrics.volume_24h_usd is None
    assert metrics.liquidity_usd is None
    assert metrics.holders is None
    for field in ("market_cap_usd", "volume_24h_usd", "liquidity_usd", "holders"):
        assert field not in metrics.provenance


def test_there_is_no_band_knob_to_hand_this_source():
    """`set_scan_band` tells a source where the round's box is by setting attributes it
    already has. This endpoint ignores `minCap`/`maxCap` (sending them changes nothing,
    measured), so the adapter declares none rather than accepting a box it cannot apply."""
    for attribute in ("market_cap_floor", "market_cap_ceiling", "liquidity_floor"):
        assert not hasattr(fourmeme.FourMeme(), attribute)


def test_the_creation_instant_is_read_as_milliseconds():
    metrics = fourmeme.FourMeme()._metrics("0xaa", CAP_ROW)
    assert metrics.created_at.timestamp() == pytest.approx(1781407514.0)
    bad = fourmeme.FourMeme()._metrics("0xaa", {**CAP_ROW, "createDate": None})
    assert bad.created_at is None


def test_the_artwork_is_a_path_on_the_site_own_asset_host():
    """`img` arrives as `/market/<id>`, and the host is the one the site publishes at
    `/v1/public/file/host` - measured 2026-10-11: `https://static.four.meme`, 200
    `image/png`."""
    metrics = fourmeme.FourMeme()._metrics("0xaa", CAP_ROW)
    assert metrics.logo_url == "https://static.four.meme/market/614lxn34nDDjZ.jpg"
    assert fourmeme.FourMeme()._metrics(
        "0xaa", {**CAP_ROW, "img": "614.jpg"}).logo_url is None
    absolute = fourmeme.FourMeme()._metrics(
        "0xaa", {**CAP_ROW, "img": "https://cdn.example/x.png"})
    assert absolute.logo_url == "https://cdn.example/x.png"


def test_the_deployer_is_folded_like_every_other_evm_address():
    metrics = fourmeme.FourMeme()._metrics("0xaa", CAP_ROW)
    assert metrics.creator == "0x023568d23eb48fb062de2255c2373f96ce0728c1"
    assert metrics.launchpad == "fourmeme"
    assert fourmeme.FourMeme()._metrics(
        "0xaa", {**CAP_ROW, "userAddress": ""}).creator is None


def test_the_sweep_asks_the_graduated_shape_and_bounds_each_order(monkeypatch, no_cache):
    """`status: TRADE` with `listType: NOR_DEX` is the payload the site own PancakeSwap tab
    builds. Curve rows have no pool - measured caps of 4 to 593 dollars - and a token with
    no pool cannot be ranked beside one that has."""
    asked: list[tuple[str, int]] = []

    def full_page(order, index):
        # The order is half of the address so that the two orders cannot collide and the
        # short-page rule is the only thing that ever ends a sweep.
        head = 1 if order == "CAP" else 5
        return {"code": 0, "data": [
            {**CAP_ROW, "tokenAddress": "0x%040x" % (head * 100000 + index * 100 + n)}
            for n in range(fourmeme.PAGE_SIZE)]}

    def fake(url, body, headers, **kwargs):
        asked.append((body["type"], body["pageIndex"]))
        assert body["status"] == "TRADE" and body["listType"] == "NOR_DEX"
        # A full page keeps an order going and a short one ends it. The cap order runs out
        # on its third page - measured - and the newest order never runs dry, so the page
        # ceiling is what ends that one.
        if body["type"] == "CAP" and body["pageIndex"] > 2:
            return {"code": 0, "data": []}
        return full_page(body["type"], body["pageIndex"])

    monkeypatch.setattr(fourmeme, "json_post", fake)
    rows = fourmeme.FourMeme().rows("bsc")
    assert asked == [("CAP", page) for page in (1, 2, 3)] + [
        ("NEW", page) for page in (1, 2, 3, 4)]
    assert len(rows) == 6 * fourmeme.PAGE_SIZE, "two cap pages and four newest ones"


def test_the_sweep_unions_the_two_orders_and_keeps_one_row_per_ca(monkeypatch, no_cache):
    def fake(url, body, headers, **kwargs):
        if body["type"] == "CAP":
            return {"code": 0, "data": [CAP_ROW] if body["pageIndex"] == 1 else []}
        return {"code": 0, "data": [NEW_ROW, CAP_ROW]}

    monkeypatch.setattr(fourmeme, "json_post", fake)
    rows = fourmeme.FourMeme().rows("bsc")
    assert set(rows) == {"0x0e920821464625cf661a4dcedeb6406e1b48ffff",
                         "0x7d6d4b711fb7c4aad9f162d93e8e51034ab5ffff"}
    assert fourmeme.FourMeme().fetch_universe("bsc", 0)[0].symbol == "SPX001"


def test_a_page_that_fails_ends_that_order_and_not_the_round(monkeypatch, no_cache):
    def fake(url, body, headers, **kwargs):
        if body["type"] == "CAP":
            raise SourceError("HTTP 502")
        return {"code": 0, "data": [NEW_ROW]}

    monkeypatch.setattr(fourmeme, "json_post", fake)
    rows = fourmeme.FourMeme().rows("bsc")
    assert list(rows) == ["0x7d6d4b711fb7c4aad9f162d93e8e51034ab5ffff"]


def test_the_per_token_read_sends_the_lower_case_address_and_matches_on_it(
        monkeypatch, no_cache):
    """Measured: `0X...` answers with nothing while the same address behind a lower-case
    `0x` answers with the row, and this endpoint is a search - the answer is a list."""
    sent: list[dict] = []

    def fake(url, body, headers, **kwargs):
        sent.append(body)
        return {"code": 0, "data": [CAP_ROW, {"tokenAddress": "0xother", "shortName": "NO"}]}

    monkeypatch.setattr(fourmeme, "json_post", fake)
    upper = "0X0E920821464625CF661A4DCEDEB6406E1B48FFFF"
    metrics, facts = fourmeme.FourMeme().fetch_token("bsc", upper)
    assert sent[0]["keyword"] == "0x0e920821464625cf661a4dcedeb6406e1b48ffff"
    assert metrics.symbol == "SPX001" and metrics.ca == sent[0]["keyword"]
    assert facts.creator is None


def test_a_token_this_launchpad_never_had_is_an_empty_answer(monkeypatch, no_cache):
    monkeypatch.setattr(fourmeme, "json_post",
                        lambda url, body, headers, **kw: {"code": 0, "data": []})
    metrics, facts = fourmeme.FourMeme().fetch_token("bsc", "0xmissing")
    assert metrics.ca == "0xmissing" and metrics.symbol is None
    assert facts.creator is None


def test_bsc_declares_this_source_and_scans_it():
    chain = config.chain("bsc")
    assert chain["enabled"] is True
    assert "fourmeme" in chain["sources"]
    assert chain["pipeline"]["scan_sources"] == [
        "gmgn", "fourmeme", "geckoterminal", "dexscreener_rank"]
    pipeline = chain["pipeline"]
    assert pipeline["scan_limit"] > 0 and pipeline["prefilter"]["require_market_data"] is True


def test_the_generic_sources_all_answer_about_bsc():
    from app.sources import dexscreener, goplus, sourcify

    for source in (registry.ALL["gmgn"], registry.ALL["geckoterminal"],
                   registry.ALL["dexscreener_rank"], registry.ALL["goplus"],
                   registry.ALL["sourcify"]):
        assert "bsc" in source.chains, source.name
    # The per-token market read is ENRICH_ONLY, so it is asserted on the class rather than
    # through `ALL` - and its BSC pairs are what the market numbers come from.
    assert "bsc" in dexscreener.DexScreener().chains

    #: BSC is 56 - the id the rest of the world uses for it, and the one both verifiers
    #: answer on (measured 2026-10-11: GoPlus `code: 1`, Sourcify `exact_match`).
    assert goplus.CHAIN_IDS["bsc"] == 56
    assert sourcify.CHAIN_IDS["bsc"] == 56


def test_this_source_is_kept_out_of_the_market_numbers_it_cannot_read():
    add = fourmeme.FourMeme().name
    for field in ("symbol", "creator", "created_at", "launchpad", "logo_url"):
        assert add in FIELD_PRIORITY[field], field
    for field in ("market_cap_usd", "volume_24h_usd", "liquidity_usd", "price_usd"):
        assert add not in FIELD_PRIORITY[field], field
    assert FIELD_PRIORITY["symbol"][-2:] == ("dexscreener", "dexscreener_rank")
