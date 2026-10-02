"""`http.cached`: what is worth remembering, and what has to be asked again.

The distinction is money. "GeckoTerminal does not have this token" is an answer, and on Arc
it is the answer for most of the pool - so a miss that is never cached is a miss that is
re-asked every round, three retries at a time.
"""

import pytest

from app.sources import http


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(http, "_cache", None)
    monkeypatch.setattr(http, "_cache_path", lambda: tmp_path / "cache.json")


def _counter(value):
    calls = []

    def produce():
        calls.append(1)
        return value

    return produce, calls


def test_a_source_that_answered_nothing_is_not_asked_twice():
    produce, calls = _counter(http.NEGATIVE)

    assert http.cached("gt:pools:0x1", 60, produce) is None
    assert http.cached("gt:pools:0x1", 60, produce) is None

    assert len(calls) == 1


def test_silence_is_never_cached():
    """`None` means nobody answered: caching it would turn one bad minute into a round
    that believes a token has no data."""
    produce, calls = _counter(None)

    assert http.cached("gt:pools:0x2", 60, produce) is None
    assert http.cached("gt:pools:0x2", 60, produce) is None

    assert len(calls) == 2


def test_the_caller_never_sees_the_marker():
    """Callers get `None` either way - "nothing" and "nothing, and we remember" are the
    same thing to every source adapter."""
    produce, _calls = _counter(http.NEGATIVE)
    assert not http.is_negative(http.cached("gt:ohlcv:0x3", 60, produce))


def test_a_real_answer_still_round_trips():
    produce, calls = _counter([{"pool": "0xabc"}])

    assert http.cached("gt:pools:0x4", 60, produce) == [{"pool": "0xabc"}]
    assert http.cached("gt:pools:0x4", 60, produce) == [{"pool": "0xabc"}]
    assert len(calls) == 1


def test_a_remembered_negative_expires_like_anything_else(monkeypatch):
    produce, calls = _counter(http.NEGATIVE)
    http.cached("gt:pools:0x5", 60, produce)

    http.cached("gt:pools:0x5", 0, produce)

    assert len(calls) == 2


def test_geckoterminal_remembers_a_token_it_does_not_have(monkeypatch):
    """The wiring, not just the mechanism: a 404 from GeckoTerminal is the answer this
    whole path exists for."""
    from app.sources import geckoterminal

    calls = []

    def four_oh_four(url, *args, **kwargs):
        calls.append(url)
        raise http.SourceError("%s -> HTTP 404" % url)

    monkeypatch.setattr(geckoterminal, "json_get", four_oh_four)
    source = geckoterminal.GeckoTerminal()

    assert source._pools("arc", "0xdead") == (None, None)
    assert source._pools("arc", "0xdead") == (None, None)

    # One request, then never again: the second call is answered from the cache. The
    # transport's own retries live under `json_get` and are counted in `test_http_stats`.
    assert len(calls) == 1, calls


def test_geckoterminal_does_not_remember_a_timeout(monkeypatch):
    from app.sources import geckoterminal

    calls = []

    def timeout(url, *args, **kwargs):
        calls.append(url)
        raise http.SourceError("%s -> ConnectTimeout" % url)

    monkeypatch.setattr(geckoterminal, "json_get", timeout)
    source = geckoterminal.GeckoTerminal()

    source._pools("arc", "0xdead")
    source._pools("arc", "0xdead")

    # A timeout is not an answer, so the next caller asks again.
    assert len(calls) == 2, calls

