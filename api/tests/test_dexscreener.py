"""DexScreener: the links, read one CA at a time.

It is the only host that answers with a social list and a website list already resolved to
URLs. Its numbers are deliberately not read - see the module docstring - and what is
asserted here is that the CA we asked about is the CA we get links for.
"""

from app.sources import dexscreener

CA = "0x" + "a" * 40


def _pair(ca, chain="arc", liq=1000, socials=None, websites=None):
    return {
        "chainId": chain,
        "baseToken": {"address": ca, "symbol": "T"},
        "liquidity": {"usd": liq},
        "info": {"socials": socials or [], "websites": websites or []},
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


def test_it_fills_links_and_nothing_else(monkeypatch):
    """A fourth opinion on a number the score is built from is how a round’s numbers start
    moving without anyone changing a rule."""
    pair = _pair(CA, liq=999_999)
    pair["priceUsd"] = "0.01"
    pair["marketCap"] = 500_000
    pair["volume"] = {"h24": 42_000}
    _answers(monkeypatch, {"pairs": [pair]})
    metrics, _facts = dexscreener.DexScreener().fetch_token("arc", CA)
    assert metrics.market_cap_usd is None and metrics.liquidity_usd is None
    assert metrics.volume_24h_usd is None and metrics.price_usd is None


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
