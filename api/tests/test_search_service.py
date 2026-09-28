"""Several engines behind one question, and what happens when one of them is down."""

from __future__ import annotations

from app.search.base import EngineAnswer, SearchHit, TweetRow
from app.search.service import SearchService


class FakeEngine:
    def __init__(self, name, hits=(), queries=(), boom=None):
        self.name = name
        self.hits = list(hits)
        self.queries = list(queries)
        self.boom = boom
        self.asked: list[str] = []
        self.limits: list[int] = []

    def search(self, query, *, limit=20):
        self.asked.append(query)
        self.limits.append(limit)
        if self.boom:
            raise self.boom
        return EngineAnswer(hits=self.hits[:limit], queries=self.queries)


def _hit(url: str, kind: str = "x") -> SearchHit:
    return SearchHit(url=url, engine="fake", kind=kind)


def test_the_first_engine_that_answers_is_the_only_one_asked():
    """The engines are a list, and a later one only runs when every earlier one came back empty."""
    first = FakeEngine("first", [_hit("https://x.com/a")], queries=["a"])
    second = FakeEngine("second", [_hit("https://b.fun", "web")], queries=["b"])
    result = SearchService([first, second]).search("q")
    assert [hit.url for hit in result.hits] == ["https://x.com/a"]
    assert result.queries == ["a"]
    assert first.asked == ["q"] and second.asked == []


def test_a_url_seen_twice_in_one_answer_is_kept_once():
    engine = FakeEngine("one", [_hit("https://x.com/a"), _hit("https://x.com/a"),
                                _hit("https://b.fun", "web")], queries=["a"])
    result = SearchService([engine]).search("q")
    assert [hit.url for hit in result.hits] == ["https://x.com/a", "https://b.fun"]
    assert result.queries == ["a"]


def test_an_engine_that_finds_nothing_hands_the_question_to_the_next_one():
    empty = FakeEngine("empty", [])
    second = FakeEngine("second", [_hit("https://b.fun", "web")])
    result = SearchService([empty, second]).search("q")
    assert [hit.url for hit in result.hits] == ["https://b.fun"]
    assert empty.asked == ["q"] and second.asked == ["q"]


def test_an_engine_that_refuses_is_recorded_and_does_not_take_the_others_with_it():
    down = FakeEngine("down", boom=RuntimeError("HTTP 400 ModelProtocolUnsupported"))
    up = FakeEngine("up", [_hit("https://x.com/a")])
    result = SearchService([down, up]).search("q")
    assert [hit.url for hit in result.hits] == ["https://x.com/a"]
    assert len(result.errors) == 1
    assert "down" in result.errors[0] and "ModelProtocolUnsupported" in result.errors[0]


def test_the_limit_is_handed_to_the_engines_and_applied_to_the_whole_answer():
    engine = FakeEngine("one", [_hit("https://x.com/%d" % index) for index in range(30)])
    result = SearchService([engine]).search("q", limit=4)
    assert engine.limits == [4]
    assert len(result.hits) == 4


def test_one_engine_can_be_asked_on_its_own():
    first = FakeEngine("first", [_hit("https://x.com/a")])
    second = FakeEngine("second", [_hit("https://b.fun", "web")])
    result = SearchService([first, second]).search("q", only="second")
    assert [hit.url for hit in result.hits] == ["https://b.fun"]
    assert first.asked == []


def test_a_service_built_with_nothing_asks_the_x_engine():
    """X is the engine: a subprocess and no model tokens, so every candidate can be asked
    about rather than only the ones a budget could be spared for."""
    service = SearchService()
    assert [engine.name for engine in service.engines] == ["x"]


def test_a_timeline_comes_from_the_engine_that_can_bring_one():
    """The report layer asks the service, so which engine knows how stays this layer's business."""
    engine = FakeEngine("x")
    engine.timeline = lambda handle, *, limit=20: [
        TweetRow(id="1", handle=handle, name="", text="hi", created_at="")]
    rows = SearchService([engine]).timeline("Solonlabs1")
    assert [row.id for row in rows] == ["1"]


def test_an_engine_that_cannot_bring_a_timeline_is_skipped_and_then_refused_quietly():
    """A card without a timeline is a worse card, not a broken round, so the service answers
    empty rather than raising into the round that is publishing."""
    assert SearchService([FakeEngine("x")]).timeline("Solonlabs1") == []
