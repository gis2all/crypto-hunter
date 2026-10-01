"""DexScreener: the links and the market numbers, read one CA at a time.

It is the only host that answers with a social list and a website list already resolved to
URLs, and it is where 市值 / 流动性 / 24h 成交 come from - one caliber for the whole round,
because the scanners disagree with each other by 5.5x on liquidity. What is asserted here is
the CA we asked about is the CA we got an answer for, and that the numbers are summed over
that CA’s pools on this chain only.
"""

from app.sources import dexscreener

CA = "0x" + "a" * 40


def _pair(ca, chain="arc", liq=1000, socials=None, websites=None, symbol="T", image=None):
    return {
        "chainId": chain,
        "baseToken": {"address": ca, "symbol": symbol},
        "liquidity": {"usd": liq},
        "info": {"socials": socials or [], "websites": websites or [], "imageUrl": image},
    }


def _answers(monkeypatch, payload):
    monkeypatch.setattr(dexscreener, "json_get", lambda *args, **kwargs: payload)


def test_the_pairs_are_read_for_the_ca_we_asked_about(monkeypatch):
    _answers(monkeypatch, {"pairs": [
        _pair("0x" + "b" * 40, chain="bsc", liq=9_000_000,
              socials=[{"type": "twitter", "url": "https://x.com/other"}]),
        _pair(CA, socials=[{"type": "twitter", "url": "https://x.com/solon"}],
              websites=[{"url": "https://solonpad.fun"}]),
    ]})
    metrics, _facts = dexscreener.DexScreener().fetch_token("arc", CA)
    assert metrics.socials == {"twitter": "https://x.com/solon"}
    assert metrics.websites == ["https://solonpad.fun"]
    assert metrics.provenance["socials"] == "dexscreener"


def test_the_mark_rides_along_with_the_links(monkeypatch):
    """The token's image is one more field of the `info` this host already answered with, so
    carrying it costs no second request and no second host - and what lands on the card is a
    URL, which is all a browser needs."""
    _answers(monkeypatch, {"pairs": [
        _pair(CA, image="https://cdn.dexscreener.com/cms/images/abc?width=800")]})
    metrics, _facts = dexscreener.DexScreener().fetch_token("arc", CA)
    assert metrics.logo_url == "https://cdn.dexscreener.com/cms/images/abc?width=800"
    assert metrics.provenance["logo_url"] == "dexscreener"


def test_a_listing_with_no_mark_says_nothing(monkeypatch):
    """Absent is absent: not an empty string the card would have to explain, and not a claim
    that a mark was checked for. The card draws the empty box either way."""
    _answers(monkeypatch, {"pairs": [_pair(CA)]})
    metrics, _facts = dexscreener.DexScreener().fetch_token("arc", CA)
    assert metrics.logo_url is None
    assert "logo_url" not in metrics.provenance


def test_a_ca_that_answers_on_several_chains_is_left_alone(monkeypatch):
    """Another chain’s X handle on this card is worse than an empty 社媒 line."""
    _answers(monkeypatch, {"pairs": [
        _pair("0x" + "b" * 40, chain="bsc", socials=[{"type": "twitter", "url": "https://x.com/other"}]),
        _pair("0x" + "c" * 40, chain="eth", socials=[{"type": "twitter", "url": "https://x.com/third"}]),
    ]})
    metrics, _facts = dexscreener.DexScreener().fetch_token("arc", CA)
    assert metrics.socials == {} and metrics.websites == []


def test_one_chain_in_the_answer_is_unambiguous(monkeypatch):
    """A token that only ever appears as the quote side still gets its own links, because
    there is no second chain in the answer to confuse it with."""
    _answers(monkeypatch, {"pairs": [
        _pair("0x" + "b" * 40, chain="arc", socials=[{"type": "twitter", "url": "https://x.com/solon"}]),
    ]})
    metrics, _facts = dexscreener.DexScreener().fetch_token("arc", CA)
    assert metrics.socials["twitter"] == "https://x.com/solon"


def test_it_reports_the_numbers_in_the_round_caliber(monkeypatch):
    """Liquidity and volume are the sum over this chain’s pools; cap and price are the
    deepest pool’s. One caliber for the ranking key, decided here rather than by whichever
    scanner happened to answer first."""
    main = _pair(CA, liq=999_999)
    main["priceUsd"] = "0.01"
    main["marketCap"] = 500_000
    main["volume"] = {"h24": 42_000}
    side = _pair(CA, liq=1_000)
    side["volume"] = {"h24": 8_000}
    other = _pair(CA, chain="bsc", liq=5_000_000)
    other["volume"] = {"h24": 900_000}
    _answers(monkeypatch, {"pairs": [main, side, other]})
    metrics, _facts = dexscreener.DexScreener().fetch_token("arc", CA)
    assert metrics.liquidity_usd == 1_000_999.0
    assert metrics.volume_24h_usd == 50_000.0
    assert metrics.market_cap_usd == 500_000.0 and metrics.price_usd == 0.01
    assert metrics.provenance["liquidity_usd"] == "dexscreener"


def test_the_name_comes_from_the_ca_we_asked_about(monkeypatch):
    """The card is titled with this host's spelling of the symbol, read off the side of the
    pair this CA is on.

    It is the only source that answers for a token no launchpad adapter scrapes - TIDE on
    Robinhood is an ordinary Uniswap pool, not a launchpad launch - so without it those
    cards went out with no name at all (a real search, 2026-10-01).
    """
    deep = _pair(CA, chain="robinhood", liq=17_992, symbol="TIDE")
    thin = _pair(CA, chain="robinhood", liq=2, symbol="TIDE")
    _answers(monkeypatch, {"pairs": [thin, deep]})
    metrics, _facts = dexscreener.DexScreener().fetch_token("robinhood", CA)
    assert metrics.symbol == "TIDE"
    assert metrics.provenance["symbol"] == "dexscreener"


def test_a_ca_that_only_trades_as_the_quote_side_is_still_named(monkeypatch):
    """Both sides of a pool carry a symbol and this CA is not always the base token.

    Reading baseToken alone would have titled the card with the other token's name.
    """
    pair = _pair("0x" + "d" * 40, chain="robinhood", liq=17_992, symbol="OTHER")
    pair["quoteToken"] = {"address": CA, "symbol": "TIDE"}
    _answers(monkeypatch, {"pairs": [pair]})
    metrics, _facts = dexscreener.DexScreener().fetch_token("robinhood", CA)
    assert metrics.symbol == "TIDE"


def test_a_host_that_is_down_says_nothing_rather_than_guessing(monkeypatch):
    def boom(*args, **kwargs):
        raise dexscreener.SourceError("HTTP 500")
    monkeypatch.setattr(dexscreener, "json_get", boom)
    metrics, facts = dexscreener.DexScreener().fetch_token("arc", CA)
    assert metrics.socials == {} and facts.source_verified is None


def test_there_is_no_chain_wide_list_to_sweep():
    """It is asked per token on purpose: a sweep it cannot answer would show up in the
    credential row as a source with a zero next to it."""
    assert dexscreener.DexScreener().fetch_universe("arc", 100) == []
