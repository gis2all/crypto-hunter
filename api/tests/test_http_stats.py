"""The per-host tallies. They exist to answer "where did the round go" with numbers."""

import pytest

from app.sources import http


@pytest.fixture(autouse=True)
def _fast_pacer(monkeypatch):
    monkeypatch.setitem(http.MIN_INTERVAL, "t.example", 0.0)
    http.stats(reset=True)


def _lane(status: int):
    calls = []

    def fake(url, headers, timeout):
        calls.append(url)
        return status, "{}"

    return fake, calls


def test_a_200_is_one_ok_and_no_failure(monkeypatch):
    lane, calls = _lane(200)
    monkeypatch.setitem(http.LANES, "http", lane)

    assert http.get("https://t.example/a") == "{}"

    row = http.stats()["t.example"]
    assert len(calls) == 1
    assert row["ok"] == 1 and row["fail"] == 0 and row["retry"] == 0


def test_every_attempt_of_a_lost_cause_is_counted(monkeypatch):
    """A retried request is where a round's minutes go, so the retries are counted
    separately from the failures: three tries is three waits, not one."""
    lane, calls = _lane(502)
    monkeypatch.setitem(http.LANES, "http", lane)

    with pytest.raises(http.SourceError):
        http.get("https://t.example/b", tries=3, sleep=0.0)

    row = http.stats()["t.example"]
    assert len(calls) == 3
    assert row["retry"] == 3 and row["fail"] == 1 and row["ok"] == 0


def test_reset_clears_the_tallies_at_the_top_of_a_round(monkeypatch):
    lane, _calls = _lane(200)
    monkeypatch.setitem(http.LANES, "http", lane)
    http.get("https://t.example/c")

    http.stats(reset=True)

    assert http.stats() == {}


def test_the_lines_name_the_host_that_ate_the_round_first(monkeypatch):
    lane, _calls = _lane(502)
    monkeypatch.setitem(http.LANES, "http", lane)
    monkeypatch.setitem(http.MIN_INTERVAL, "busy.example", 0.0)

    for host in ("t.example", "busy.example"):
        with pytest.raises(http.SourceError):
            http.get("https://%s/d" % host, tries=1)

    lines = http.stats_lines()
    assert lines
    assert "fail=1" in lines[0]
    assert {line.split(" ")[0] for line in lines} == {"t.example", "busy.example"}


def test_a_404_is_still_a_failure_and_is_not_dressed_up(monkeypatch):
    """The caller decides what a 404 means - `blockscout_checked` reads it as an answer.
    The transport only reports that it did not get a 200."""
    lane, _calls = _lane(404)
    monkeypatch.setitem(http.LANES, "http", lane)

    with pytest.raises(http.SourceError) as excinfo:
        http.get("https://t.example/e", tries=1)

    assert "HTTP 404" in str(excinfo.value)
    assert http.stats()["t.example"]["fail"] == 1

