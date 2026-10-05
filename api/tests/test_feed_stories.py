"""分组站: 定夺落库、只挂根、盘点不合并、模型不可用就收手, 以及页面读库里那件事.

判定那一半在 `test_feed_grouping.py`（拿假模型整段测）。这一份测的是另一半：一次定夺写进
`feed_stories` / `feed_facts` / `feed_item_facts` 之后长什么样，以及读取侧怎么把规则桶按库里
的分组重新并起来 —— 要点是「库里没有的条目留在原来的桶里」，一个窗口里总会有几条还没轮到。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.feed import events as feed_events
from app.feed import grouping, stories, views, writing
from app.feed.models import Event
from app.feed.sources import snapshot
from app.feed.sources import store as source_store
from app.store import db

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)

BUDGET = {"maxItems": 10, "concurrency": 1, "timeoutSeconds": 5}

#: 召回池那一头：30 天比 14 天的窗口宽，读回来的一定是全部。
SINCE = (NOW - timedelta(days=30)).isoformat(timespec="seconds")

FRAME = {"subject": "Nova", "action": "bridge drained", "object": "$12M"}

UNRELATED = {"query": "x", "decisions": [{"id": "C1", "relation": "unrelated", "confidence": 0.9}]}


@pytest.fixture(autouse=True)
def roster(isolated_db):
    source_store.import_roster()


@pytest.fixture(autouse=True)
def station_on(monkeypatch):
    """这一站开不开由「旋钮 + 有没有模型」两件事决定；这个文件测的是编排，两件都按住。"""
    monkeypatch.setattr(writing, "enabled", lambda: True)


@pytest.fixture()
def recall_wide(monkeypatch):
    """把召回线放到 0：池子里那条要能被召回，否则测的是召回而不是定夺。"""
    monkeypatch.setattr(grouping, "LEXICAL_MIN", 0.0)


def _row(external_id: str, *, title: str, url: str | None = None,
         source_id: str = "rss:a", minutes: float = 5, quoted: dict | None = None) -> dict:
    return {
        "source_id": source_id,
        "external_id": external_id,
        "source_kind": "rss",
        "identity_key": "other|",
        "title": title,
        "url": url or "https://a/%s" % external_id,
        "author": "",
        "body": "<p>%s</p>" % title,
        "media": [],
        "quoted": quoted or {},
        "published_at": (NOW - timedelta(minutes=minutes)).isoformat(),
    }


def _stored(external_id: str, *, title: str, summary: str = "", source_id: str = "rss:a",
            minutes: float = 5, url: str | None = None,
            frame: dict | None = None, quoted: dict | None = None) -> None:
    """一条「写作站已经写过」的条目：有中文标题、有摘要、有骨架，就欠分组。"""
    db.feed_items_put([_row(external_id, title=title, url=url, source_id=source_id,
                            minutes=minutes, quoted=quoted)])
    db.feed_items_write_llm([{
        "source_id": source_id,
        "external_id": external_id,
        "title_zh": title,
        "summary_llm": summary,
        "fact_json": json.dumps(frame if frame is not None else FRAME, ensure_ascii=False),
        "llm_stage": "full",
        "llm_at": NOW.isoformat(),
    }])


def _seed_story(first: str, first_title: str, *, minutes: float = 90,
                second: tuple[str, str] | None = None) -> tuple[int, int]:
    """直接种一件事（一条根事实 + 一到两条报道），绕过分组站。

    要测的是「分组站遇到既有事实时怎么办」，把既有事实也交给它去造，测的就成了它自己。
    """
    at = (NOW - timedelta(minutes=minutes)).isoformat()
    _stored(first, title=first_title, minutes=minutes)
    story_id = db.feed_story_new({"title": first_title, "at": at})
    fact_id = db.feed_fact_new({"story_id": story_id, "title": first_title, "at": at,
                                "root": True, **FRAME})
    db.feed_item_facts_set([{"source_id": "rss:a", "external_id": first,
                             "fact_id": fact_id, "role": "primary"}])
    if second is not None:
        external_id, title = second
        _stored(external_id, title=title, source_id="rss:b", minutes=minutes - 30)
        db.feed_item_facts_set([{"source_id": "rss:b", "external_id": external_id,
                                 "fact_id": fact_id, "role": "report"}])
        db.feed_story_touch(story_id, (NOW - timedelta(minutes=minutes - 30)).isoformat())
    return story_id, fact_id


def _fake(monkeypatch, answer) -> list[str]:
    """把模型换成按 purpose 翻牌子的假人；`answer` 是 purpose -> dict，或一个函数。"""
    seen: list[str] = []

    def call(messages, **kw):
        seen.append(kw["purpose"])
        got = answer(kw["purpose"]) if callable(answer) else answer[kw["purpose"]]
        if isinstance(got, Exception):
            raise got
        return got

    monkeypatch.setattr(grouping.llm, "complete_json", call)
    return seen


# ── 落库 ──────────────────────────────────────────────────────────────────────────

def test_an_item_with_no_close_candidate_starts_its_own_story(isolated_db, monkeypatch):
    _fake(monkeypatch, {grouping.PURPOSE_GROUP: UNRELATED})
    _stored("a-1", title="Nova bridge drained for $12M")

    report = stories.run(NOW, budget=BUDGET)

    assert report == {"asked": 1, "report": 0, "development": 0, "story": 1, "signal": 0,
                      "unmatched": 0, "manual": 0, "failed": 0, "regrouped": 0,
                      "rematched": 0, "reclaimed": 0}
    facts, members = db.feed_fact_pool(SINCE)
    assert [fact["root"] for fact in facts] == [1]
    assert db.feed_stories_all()[0]["title"] == "Nova bridge drained for $12M"
    assert members[0]["role"] == "primary"


def test_a_report_of_the_same_occurrence_joins_that_fact(isolated_db, monkeypatch, recall_wide):
    _seed_story("a-1", "Nova bridge drained for $12M")
    _stored("b-2", title="Nova bridge drained for $12M", source_id="rss:b", minutes=10)
    _fake(monkeypatch, {grouping.PURPOSE_GROUP: {
        "query": "x",
        "decisions": [{"id": "C1", "relation": "same_occurrence", "confidence": 0.95}]}})

    report = stories.run(NOW, budget=BUDGET)

    assert (report["report"], report["story"], report["development"]) == (1, 0, 0)
    assert len(db.feed_stories_all()) == 1
    assert len(db.feed_fact_pool(SINCE)[0]) == 1
    roles = {row["role"] for row in db.feed_item_facts_all()}
    assert roles == {"primary", "report"}


def test_a_development_starts_a_new_fact_under_the_same_story(isolated_db, monkeypatch, recall_wide):
    story_id, _fact = _seed_story("a-1", "Nova bridge drained for $12M", minutes=180)
    _stored("b-2", title="Nova publishes its post-mortem", source_id="rss:b", minutes=10)
    _fake(monkeypatch, {grouping.PURPOSE_GROUP: {
        "query": "x", "decisions": [{"id": "C1", "relation": "same_story", "confidence": 0.9}]}})

    report = stories.run(NOW, budget=BUDGET)

    assert report["development"] == 1
    facts = db.feed_fact_pool(SINCE)[0]
    assert sorted(fact["root"] for fact in facts) == [0, 1]
    assert {fact["story_id"] for fact in facts} == {story_id}
    assert len(db.feed_stories_all()) == 1


def test_a_roundup_never_merges(isolated_db, monkeypatch, recall_wide):
    """盘点把几件事装进一篇, 所以它**不立事件** (4.9b): 它只提及那些事, 不是它们的证据.

    期望的那一份: 还是那一件事 (没有第二条), 而这一篇记成 mention + `composite` ——
    所以它不进材料、不进热度, 但照常出现在全部动态里.
    """
    _story, fact = _seed_story("a-1", "Nova bridge drained for $12M")
    _stored("b-2", title="Daily roundup: ten things that happened", source_id="rss:b", minutes=10)
    _fake(monkeypatch, {grouping.PURPOSE_GROUP: {
        "query": "x", "decisions": [{"id": "C1", "relation": "roundup", "confidence": 0.9}]}})

    report = stories.run(NOW, budget=BUDGET)

    assert (report["story"], report["roundup"]) == (0, 1)
    assert len(db.feed_stories_all()) == 1
    assert [row["fact_id"] for row in db.feed_item_mentions_of("rss:b", "b-2")] == [fact]
    assert db.feed_item_get("rss:b", "b-2")["analysis_scope"] == "composite"


def test_a_model_that_is_unavailable_stops_the_pass(isolated_db, monkeypatch, recall_wide):
    """模型不可用（没配 / 被限流 / 答案不合格式）就收手：写一半会把这一批切成两半。"""
    _seed_story("z-0", "Nova bridge drained for $12M")     # 池子里有一条，第一条就会去问模型
    _stored("a-1", title="Nova bridge drained for $12M")
    _stored("b-2", title="Nova bridge drained for $12M", source_id="rss:b")
    _fake(monkeypatch, {grouping.PURPOSE_GROUP: grouping.llm.LLMUnavailable("no model")})

    report = stories.run(NOW, budget=BUDGET)

    assert (report["asked"], report["failed"]) == (2, 1)
    assert (report["story"], report["development"], report["report"]) == (0, 0, 0)
    assert len(db.feed_stories_all()) == 1        # 先前种下的那一件，两条新报道一条都没进去
    assert len(db.feed_item_facts_all()) == 1


def test_an_item_the_model_has_not_written_is_not_asked_about(isolated_db, monkeypatch):
    """分组读的是模型写的中文标题与摘要，没有它就无从判起 —— 队列只收写作站写过的行。"""
    monkeypatch.setattr(grouping.llm, "complete_json",
                        lambda *a, **k: pytest.fail("不该问模型"))
    db.feed_items_put([_row("b-2", title="Untouched by the model")])

    assert stories.run(NOW, budget=BUDGET)["asked"] == 0


def test_an_empty_frame_still_gets_grouped(isolated_db, monkeypatch):
    """骨架只是这条新事实的名字（观点、盘点类常常给不出骨架），它不该成为进不进队列的门槛 ——

    拿它当门槛，那些条目会永远排在队里，把每一轮的预算占满。"""
    _fake(monkeypatch, {grouping.PURPOSE_GROUP: UNRELATED})
    _stored("a-1", title="Nova bridge drained", frame={})

    report = stories.run(NOW, budget=BUDGET)

    assert report["story"] == 1
    facts, _members = db.feed_fact_pool(SINCE)
    assert (facts[0]["subject"], facts[0]["action"], facts[0]["object"]) == ("", "", "")


def test_an_empty_budget_turns_the_station_off(isolated_db, monkeypatch):
    monkeypatch.setattr(grouping.llm, "complete_json",
                        lambda *a, **k: pytest.fail("不该问模型"))
    _stored("a-1", title="Nova bridge drained")
    assert stories.run(NOW, budget={"maxItems": 0})["asked"] == 0


def test_the_same_url_goes_into_the_recall_pool_as_a_known_reference(isolated_db):
    """「这条回复/转述了那一条」是材料里写着的事实，不该让模型去猜（grouping.recall 的 boost）。"""
    _seed_story("a-1", "Nova bridge drained", minutes=90)
    pool, urls = stories.pool(NOW)
    # 读回来的行是库的形状（`quoted_json`），不是入库时那一份（`quoted`）。
    row = dict(_row("b-2", title="I agree with this", url="https://x/2"),
               quoted_json=json.dumps({"url": "https://a/a-1"}))
    assert [candidate.key for candidate in stories.boosts(row, pool, urls)] == [pool[0].key]


# ── 读取侧 ────────────────────────────────────────────────────────────────────────

def test_the_page_reads_the_story_the_model_decided(isolated_db):
    """两条报道归到一件事上，页面上就是一条事件。"""
    _seed_story("a-1", "Nova bridge drained for $12M", minutes=90,
                second=("b-2", "Nova Protocol halts bridge"))

    _items, grouped = views.collect(NOW, views.HOT_HOURS)

    assert len(grouped) == 1
    assert len(grouped[0].items) == 2


def test_the_items_the_model_has_not_reached_stay_in_their_rule_bucket(isolated_db):
    """库里没有的条目留在原来的桶里：让它们掉出去等于页面忽然少几条，而它们没有做错什么。"""
    _stored("a-1", title="Nova bridge drained for $12M")
    rule = feed_events.group([snapshot.row_to_discovery(_row("a-1", title="Nova bridge drained"))])
    kept = stories.rebucket(rule, {("rss:b", "b-2"): "s:9"})
    assert len(kept) == 1
    assert kept[0].identity_key == rule[0].identity_key


def test_two_stories_are_not_merged_by_the_reader(isolated_db):
    _seed_story("a-1", "Nova bridge drained for $12M", minutes=90)
    _seed_story("c-3", "Binance lists Pulse", minutes=60)
    membership = stories.membership(NOW - timedelta(hours=72))
    assert len(membership) == 2
    _items, grouped = views.collect(NOW, views.HOT_HOURS)
    assert len(grouped) == 2


def test_the_rule_version_stands_in_when_nothing_has_been_grouped(isolated_db):
    _stored("a-1", title="Nova bridge drained for $12M")
    assert stories.membership(NOW - timedelta(hours=72)) == {}


def _event(key: str, title: str) -> Event:
    at = NOW.isoformat(timespec="seconds")
    return Event(identity_key=key, kind="fact", kind_label="事件", title=title, url="",
                 first_seen_at=at, last_seen_at=at)


def _story_with_report(title: str, source_id: str, external_id: str) -> int:
    """一件有报道的事: 开一件事 + 记一次发生 + 把一条报道钉上去."""
    at = NOW.isoformat(timespec="seconds")
    story_id = db.feed_story_new({"title": title, "at": at})
    fact_id = db.feed_fact_new({"story_id": story_id, "title": title, "root": True, "at": at})
    db.feed_item_facts_set([{"source_id": source_id, "external_id": external_id,
                             "fact_id": fact_id}])
    return story_id


def test_the_detail_page_lists_the_related_stories(isolated_db, monkeypatch):
    """「关联事件」读的是 `feed_story_links` (AIHOT 的 `relatedStories`): 两头都活着才印."""
    monkeypatch.setattr(db, "now_iso", lambda: NOW.isoformat(timespec="seconds"))
    one = _story_with_report("Nova bridge drained", "rss:a", "a-1")
    two = _story_with_report("Kite audit published", "rss:b", "b-1")
    db.feed_story_links_add([(one, two)])

    refs = views.related_stories(_event("s:%d" % one, "Nova bridge drained"), NOW)

    assert [ref["id"] for ref in refs] == ["s:%d" % two]
    assert refs[0]["title"] == "Kite audit published"
    assert refs[0]["relation"] == "related"
    assert refs[0]["relation_label"] == "相关事件"
    assert refs[0]["last_seen_ago"]


def test_a_related_story_nobody_reports_is_not_listed(isolated_db, monkeypatch):
    """对面那一头没人报过: 点进去是一页空的, 所以不印 (AIHOT 也要求对面有公开报道)."""
    monkeypatch.setattr(db, "now_iso", lambda: NOW.isoformat(timespec="seconds"))
    one = _story_with_report("Nova bridge drained", "rss:a", "a-1")
    quiet = db.feed_story_new({"title": "Kite audit published",
                               "at": NOW.isoformat(timespec="seconds")})
    db.feed_story_links_add([(one, quiet)])

    assert views.related_stories(_event("s:%d" % one, "Nova bridge drained"), NOW) == []


def test_a_rule_bucket_has_no_related_stories(isolated_db):
    """规则桶还没有库里的边: 那一栏是空的, 不是报错."""
    assert views.related_stories(_event("s:ab12cd", "Nova"), NOW) == []


def test_a_prompt_change_decides_the_old_rows_again(monkeypatch, recall_wide):
    """提示词换版: 按旧版规则定过的行要重新定夺一次 (AIHOT 的 `regroup_pending`)。

    不重判的话, 库里那些行会一直用旧口径冒充新口径, 而且完全静默 —— 与写作侧 `prompt_version`
    那串 token 治的是同一类病。
    """
    assert grouping.station_version() == grouping.station_version()   # 同一版之间必须是稳定的
    _fake(monkeypatch, {grouping.PURPOSE_GROUP: UNRELATED})
    _stored("a-1", title="Nova bridge drained for $12M")
    assert stories.run(NOW, budget=BUDGET)["story"] == 1
    assert _decision("a-1")["prompt_version"] == grouping.station_version()

    # 提示词改一个字就是一个新版本
    changed = "group-batch.md@changed"
    monkeypatch.setattr(grouping, "station_version", lambda: changed)
    _fake(monkeypatch, {grouping.PURPOSE_GROUP: UNRELATED})
    report = stories.run(NOW, budget=BUDGET)

    assert (report["regrouped"], report["asked"]) == (1, 1)
    assert _decision("a-1")["prompt_version"] == changed
    # 重判之后这一条记的是新版本, 下一轮不再挑它出来
    _fake(monkeypatch, {grouping.PURPOSE_GROUP: UNRELATED})
    assert stories.run(NOW, budget=BUDGET)["regrouped"] == 0


def test_a_report_older_than_the_recall_window_keeps_its_old_decision(monkeypatch, recall_wide):
    """窗口外的行不重判: 池子里已经没有它那件事了, 重判会把一条老报道变成一个**新**事件。"""
    _fake(monkeypatch, {grouping.PURPOSE_GROUP: UNRELATED})
    _stored("old-1", title="Old news nobody recalls", minutes=60 * 24 * 30)
    assert stories.run(NOW, budget=BUDGET)["story"] == 1

    monkeypatch.setattr(grouping, "station_version", lambda: "group-batch.md@changed")
    _fake(monkeypatch, {grouping.PURPOSE_GROUP: UNRELATED})
    report = stories.run(NOW, budget=BUDGET)

    assert (report["regrouped"], report["asked"]) == (0, 0)


def test_a_manual_decision_is_never_regrouped(monkeypatch, recall_wide):
    """人工放过的条目不在重判之列 —— 换提示词不该把人的决定重新掷一次骰子。"""
    _fake(monkeypatch, {grouping.PURPOSE_GROUP: UNRELATED})
    _stored("a-1", title="Nova bridge drained for $12M")
    db.feed_override_set({"source_id": "rss:a", "external_id": "a-1", "reason": "operator",
                          "actor": "web"})

    monkeypatch.setattr(grouping, "station_version", lambda: "group-batch.md@changed")
    _fake(monkeypatch, {grouping.PURPOSE_GROUP: UNRELATED})
    assert stories.run(NOW, budget=BUDGET)["regrouped"] == 0


def _decision(external_id: str) -> dict:
    """这一条落在 `feed_item_facts` 上的那一笔。"""
    conn = db.connect()
    try:
        row = conn.execute("SELECT fact_id, prompt_version FROM feed_item_facts"
                           " WHERE source_id = 'rss:a' AND external_id = ?",
                           (external_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else {}
