"""DexScreener's board as a source: the document's rows, and the union over orders.

Two things here fail silently if they are wrong, which is why they carry tests. The payload
is protobuf-JSON rather than JSON (`BigInt("33")`, `void 0`, `undefined`), and the board is
one page per order rather than a paged list - a sweep is a union. Read either one wrong and
the answer is a shorter list, not an error. The third is the band: `collect.set_scan_band`
hands this source the round's own prefilter box, and a value that will not read as a number
has to mean "read everything", never "read nothing".
"""

import pytest

from app.pipeline import collect as collector
from app.sources import dexscreener_rank
from app.sources.dexscreener_rank import DexScreenerRank, pairs
from app.sources.http import SourceError

SOL = "So11111111111111111111111111111111111111112"
EVM = "0xAbC0000000000000000000000000000000000001"

#: One real row, in the shape the page carries it: a big integer as `BigInt(...)`, an absent
#: field as `void 0` and as `undefined`, and a symbol with a bracket in it.
ROW = (
    '{"chainId":"solana","pairAddress":"p1",'
    '"baseToken":{"address":"' + SOL + '","symbol":"AAA [x]"},'
    '"marketCap":BigInt("12345"),"fdv":void 0,'
    '"liquidity":{"usd":9000.5,"base":undefined},'
    '"volume":{"h24":1234.5},"priceUSD":"0.00123","priceChange":{"h24":-4.5},'
    '"cmsProfile":{"iconId":"abc","links":['
    '{"type":0,"url":"example.com"},{"type":2,"url":"xhandle"},'
    '{"type":3,"url":"https://t.me/xyz"}]}}'
)


def _document(*rows: str) -> str:
    return '<html><script>window.__d={"pairs":[%s],"next":1}</script></html>' % ",".join(rows)


def _pair(address: str, depth: float, symbol: str = "AAA") -> str:
    return ('{"chainId":"solana","pairAddress":"p%s",'
            '"baseToken":{"address":"%s","symbol":"%s"},"liquidity":{"usd":%s}}'
            % (symbol, address, symbol, depth))


def _fetch(monkeypatch, getter):
    source = DexScreenerRank()
    monkeypatch.setattr(dexscreener_rank, "cached", lambda _key, _ttl, produce: produce())
    monkeypatch.setattr(dexscreener_rank, "get", getter)
    return source.fetch_universe("solana", 0)


# ------------------------------------------------------------------ the document

def test_a_bracket_inside_a_string_does_not_end_the_board():
    rows = pairs(_document(ROW))
    assert len(rows) == 1, "the array is matched by brackets, and the string holds one"
    assert rows[0]["baseToken"]["symbol"] == "AAA [x]"


def test_a_document_without_a_board_answers_nothing():
    assert pairs("<html><body>just a moment</body></html>") == []


def test_a_board_that_will_not_parse_is_no_rows_rather_than_an_exception():
    assert pairs('{"pairs":[{,,}]}') == []


def test_one_row_becomes_one_token_row():
    metrics = dexscreener_rank._metrics(pairs(_document(ROW))[0], "solana")
    assert metrics.ca == SOL, "base58 is case-sensitive: the address is the address"
    assert metrics.symbol == "AAA [x]"
    assert metrics.market_cap_usd == 12345.0, "a BigInt is a number"
    assert metrics.liquidity_usd == 9000.5
    assert metrics.volume_24h_usd == 1234.5
    assert metrics.price_usd == 0.00123
    assert metrics.price_change_24h_pct == -4.5
    assert metrics.logo_url == "https://cdn.dexscreener.com/cms/images/abc"
    assert metrics.websites == ["https://example.com"], "type 0 is the site-shaped link"
    assert metrics.socials == {"twitter": "https://x.com/xhandle",
                               "telegram": "https://t.me/xyz"}


def test_an_evm_address_folds_and_a_base58_one_does_not():
    row = {"chainId": "solana", "baseToken": {"address": EVM, "symbol": "BBB"}}
    assert dexscreener_rank._metrics(row, "solana").ca == EVM.lower()


def test_a_row_from_another_chain_is_not_this_chain_row():
    other = {"chainId": "robinhood", "baseToken": {"address": SOL}}
    assert dexscreener_rank._metrics(other, "solana") is None
    assert dexscreener_rank._metrics({"chainId": "solana", "baseToken": {}}, "solana") is None


def test_a_pair_with_no_price_carries_no_market_numbers():
    """A market cap of nothing and a pool with no reserves are "this source has no reading",
    not "the reading is nil" - while a zero for volume is an answer: the pair did not move."""
    row = {"chainId": "solana", "baseToken": {"address": SOL, "symbol": "Z"},
           "marketCap": 0, "liquidity": {"usd": 0}, "volume": {"h24": 0}}
    metrics = dexscreener_rank._metrics(row, "solana")
    assert metrics.market_cap_usd is None
    assert metrics.liquidity_usd is None
    assert metrics.volume_24h_usd == 0.0


# ------------------------------------------------------------------ the sweep

def test_the_sweep_unions_the_orders_and_keeps_the_deepest_pair(monkeypatch):
    boards = {"trendingScoreH6": _document(_pair(SOL, 100)),
              "volume": _document(_pair(SOL, 9000), _pair(EVM, 50))}

    def getter(url, *_args, **_kwargs):
        for order, document in boards.items():
            if "rankBy=" + order in url:
                return document
        raise SourceError("no board")

    rows = {row.ca: row for row in _fetch(monkeypatch, getter)}
    assert sorted(rows) == sorted([SOL, EVM.lower()]), "the union, not the first page"
    assert rows[SOL].liquidity_usd == 9000.0, "the deepest of a token's pairs is its row"


def test_a_sweep_where_no_order_answers_is_a_dead_source(monkeypatch):
    def dead(_url, *_args, **_kwargs):
        raise SourceError("blocked")

    with pytest.raises(SourceError):
        _fetch(monkeypatch, dead)


def test_one_order_answering_is_enough(monkeypatch):
    def getter(url, *_args, **_kwargs):
        if "rankBy=txns" in url:
            return _document(_pair(SOL, 100))
        raise SourceError("blocked")

    assert [row.ca for row in _fetch(monkeypatch, getter)] == [SOL]


# ------------------------------------------------------------------ the band

def test_the_band_is_the_round_own_box():
    source = DexScreenerRank()
    collector.set_scan_band([source], {"market_cap_min_usd": 10000,
                                       "market_cap_max_usd": 200000,
                                       "liquidity_min_usd": 5000})
    assert source._query() == "minMarketCap=10000&maxMarketCap=200000&minLiq=5000"


def test_a_band_that_will_not_read_reads_the_whole_chain():
    source = DexScreenerRank()
    collector.set_scan_band([source], {"market_cap_min_usd": "nonsense",
                                       "market_cap_max_usd": None,
                                       "liquidity_min_usd": -5})
    assert (source.market_cap_floor, source.market_cap_ceiling,
            source.liquidity_floor) == (0.0, 0.0, 0.0)
    assert source._query() == "", "no line: the page is asked for the whole chain"


def test_a_source_without_the_knob_is_left_alone():
    collector.set_scan_band([object()], {"market_cap_min_usd": 1})


# ------------------------------------------------------------------ the wiring

def test_the_registry_knows_the_source_and_which_chains_it_answers():
    from app.sources import registry

    assert registry.ALL["dexscreener_rank"].chains == ("bsc", "robinhood", "solana")


def test_a_board_row_keeps_its_numbers_through_the_merge():
    """A source missing from the priority table still returns rows - they just arrive at the
    pool as a CA and nothing else, which reads as "this chain answered nothing" rather than as
    a missing entry. This is the one line that keeps the five bulk fields on the card."""
    row = dexscreener_rank._metrics(pairs(_document(ROW))[0], "solana")
    merged = collector.merge(SOL, {"dexscreener_rank": row}, collector.DEFAULT_PRIORITY)

    assert merged.symbol == "AAA [x]"
    assert merged.market_cap_usd == 12345.0 and merged.liquidity_usd == 9000.5
    assert merged.volume_24h_usd == 1234.5 and merged.price_usd == 0.00123
    assert merged.price_change_24h_pct == -4.5
    assert merged.logo_url and merged.socials["twitter"].endswith("xhandle")
    assert merged.websites == ["https://example.com"]
    assert merged.provenance["market_cap_usd"] == "dexscreener_rank"


def test_the_per_token_read_is_empty_on_purpose():
    """The sweep already read every pair this source knows about; the per-token questions are
    `sources/dexscreener.py`, on the same host family."""
    metrics, facts = DexScreenerRank().fetch_token("solana", SOL)
    assert metrics.ca == SOL
    assert not metrics.websites and not metrics.liquidity_usd
    assert facts.dangerous_perms in (None, [])
