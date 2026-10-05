"""讨论帖那一档: 证据挂靠、挂不上记一笔, 以及"证据算热度、但不印成正文".

判定那一半在 `grouping.py` (`judge_signal` / `signal_target`), 编排与落库在 `stories._signal`.
这一份测三件事: 挂靠的三条路 (指向 / 直挂 / 问一次), 问过就不再问第二遍, 以及读取侧
「数它一家、但不把它印成报道」。

对照 AIHOT: `events/group.ts` 的 `groupSignal` 与 `events/relate.ts` 的 `signalTarget`。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.feed import embeddings, grouping, heat, stories, views, writing
from app.feed.sources import store as source_store
from app.store import db

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)

BUDGET = {"maxItems": 10, "concurrency": 1, "timeoutSeconds": 5}

SINCE = (NOW - timedelta(days=30)).isoformat(timespec="seconds")

FRAME = {"subject": "Nova", "action": "bridge drained", "object": "$12M"}

#: 清单里写着 `participation_mode: hot_signal` 的那一类源 (config/feed/sources.yaml): 监测账号.
POST = "x:lookonchain"
POST_TITLE = "Nova bridge drained for $12M, per on-chain flows"


@pytest.fixture(autouse=True)
def roster(isolated_db):
    """名录要在库里: 一个源算"报道"还是"讨论证据"读的是 `feed_sources.participation_mode`."""
    source_store.import_roster()


@pytest.fixture(autouse=True)
def station_on(monkeypatch):
    """讨论帖也要写作站写过中文标题才排队; 这一份测的是编排, 把它按住. """
    monkeypatch.setattr(writing, "enabled", lambda: True)


def _row(external_id: str, *, title: str, source_id: str = "rss:a", source_kind: str = "rss",
         minutes: float = 5, url: str | None = None, quoted: dict | None = None) -> dict:
    return {
        "source_id": source_id,
        "external_id": external_id,
        "source_kind": source_kind,
        "identity_key": "other|",
        "title": title,
        "url": url or "https://a/%s" % external_id,
        "author": "",
        "body": "<p>%s</p>" % title,
        "media": [],
        "quoted": quoted or {},
        "published_at": (NOW - timedelta(minutes=minutes)).isoformat(),
    }


def _stored(external_id: str, *, title: str, summary: str = "同一次发生", source_id: str = "rss:a",
            source_kind: str = "rss", minutes: float = 5, url: str | None = None,
            quoted: dict | None = None, frame: dict | None = None) -> None:
    """一条「写作站已经写过」的条目: 有中文标题、有摘要、有骨架, 就欠分组. """
    db.feed_items_put([_row(external_id, title=title, source_id=source_id,
                            source_kind=source_kind, minutes=minutes, url=url, quoted=quoted)])
    db.feed_items_write_llm([{
        "source_id": source_id,
        "external_id": external_id,
        "title_zh": title,
        "summary_llm": summary,
        "fact_json": json.dumps(frame if frame is not None else FRAME, ensure_ascii=False),
        "llm_stage": "full",
        "llm_at": NOW.isoformat(),
    }])


def _seed_story(external_id: str, title: str, *, url: str = "https://a/root") -> tuple[int, int]:
    """种一件事 (一条根事实 + 一条报道), 绕过分组站. """
    at = (NOW - timedelta(minutes=90)).isoformat()
    _stored(external_id, title=title, url=url, minutes=90)
    story_id = db.feed_story_new({"title": title, "at": at})
    fact_id = db.feed_fact_new({"story_id": story_id, "title": title, "at": at, "root": True,
                                **FRAME})
    db.feed_item_facts_set([{"source_id": "rss:a", "external_id": external_id,
                             "fact_id": fact_id, "role": "primary"}])
    return story_id, fact_id


def _fake(monkeypatch, answer) -> list[str]:
    seen: list[str] = []

    def call(messages, **kw):
        seen.append(kw["purpose"])
        got = answer(kw["purpose"]) if callable(answer) else answer[kw["purpose"]]
        if isinstance(got, Exception):
            raise got
        return got

    monkeypatch.setattr(grouping.llm, "complete_json", call)
    return seen


def _vectors(monkeypatch, mapping: dict[str, list[float]]) -> None:
    """把向量端点换成按关键词给方向的假人; 没命中的文本不进表 (余弦按 0 算). """
    def call(texts):
        out = {}
        for text in texts:
            for needle, vector in mapping.items():
                if needle in text:
                    out[text] = vector
                    break
        return out

    monkeypatch.setattr(embeddings, "warm", call)


# --- 挂靠的三条路 ---------------------------------------------------------------

def test_a_post_pointing_at_a_report_attaches_without_a_model_call(isolated_db, monkeypatch):
    """转发/回复了库里那一条: 它自己就带着答案, 不必花一次调用 (AIHOT 的 signal-native). """
    story_id, _fact = _seed_story("a-1", "Nova bridge drained for $12M")
    monkeypatch.setattr(grouping.llm, "complete_json",
                        lambda *a, **k: pytest.fail("指向的那条不该问模型"))
    _stored("p-1", title=POST_TITLE, source_id=POST, source_kind="x", minutes=10,
            quoted={"url": "https://a/root", "handle": "someone"})

    report = stories.run(NOW, budget=BUDGET)

    assert (report["signal"], report["unmatched"]) == (1, 0)
    rows = db.feed_story_signal_members(SINCE)
    assert [(row["source_id"], row["story_id"]) for row in rows] == [(POST, story_id)]
    assert len(db.feed_stories_all()) == 1        # 没有为它单开一件事


def test_a_post_close_enough_attaches_without_a_model_call(isolated_db, monkeypatch):
    """召回分到 0.92: 几乎肯定是同一件事, 也不必问 (AIHOT 的 SIGNAL_AUTO_COSINE). """
    story_id, _fact = _seed_story("a-1", "Nova bridge drained for $12M")
    _vectors(monkeypatch, {"Nova": [1.0, 0.0]})
    monkeypatch.setattr(grouping.llm, "complete_json",
                        lambda *a, **k: pytest.fail("0.92 以上不该问模型"))
    _stored("p-1", title=POST_TITLE, source_id=POST, source_kind="x", minutes=10)

    report = stories.run(NOW, budget=BUDGET)

    assert report["signal"] == 1
    assert db.feed_story_signal_members(SINCE)[0]["story_id"] == story_id


def test_a_post_that_is_close_but_not_enough_is_judged_once(isolated_db, monkeypatch):
    """0.8 的余弦: 过 0.72 的召回线, 不到 0.92 的直挂线 —— 只有这一档该花一次调用. """
    story_id, _fact = _seed_story("a-1", "Nova bridge drained for $12M")
    _vectors(monkeypatch, {POST_TITLE: [1.0, 0.0], "Nova bridge drained": [0.8, 0.6]})
    seen = _fake(monkeypatch, {grouping.PURPOSE_GROUP_SIGNAL: {
        "decisions": [{"id": "C1", "relation": "same_story", "confidence": 0.9}]}})
    _stored("p-1", title=POST_TITLE, source_id=POST, source_kind="x", minutes=10)

    report = stories.run(NOW, budget=BUDGET)

    assert seen == [grouping.PURPOSE_GROUP_SIGNAL]
    assert report["signal"] == 1
    assert db.feed_story_signal_members(SINCE)[0]["story_id"] == story_id


def test_a_post_that_matches_nothing_is_recorded_and_not_asked_twice(isolated_db, monkeypatch):
    """挂不上也要记一笔: 不记, 这一条帖子每一轮都会重新排队, 把预算占满. """
    _seed_story("a-1", "Nova bridge drained for $12M")
    _vectors(monkeypatch, {POST_TITLE: [0.0, 1.0], "Nova": [1.0, 0.0]})
    monkeypatch.setattr(grouping.llm, "complete_json",
                        lambda *a, **k: pytest.fail("没有候选就不该问模型"))
    _stored("p-1", title=POST_TITLE, source_id=POST, source_kind="x", minutes=10)

    report = stories.run(NOW, budget=BUDGET)

    assert (report["unmatched"], report["signal"]) == (1, 0)
    assert db.feed_story_signal_members(SINCE) == []      # 挂不上的不进任何一件事
    assert db.feed_items_for_grouping(10) == []           # 也不再排在队里
    assert stories.run(NOW, budget=BUDGET)["asked"] == 0


def test_a_post_with_no_vector_endpoint_is_left_unmatched(isolated_db, monkeypatch):
    """没配向量端点就只记一笔 (AIHOT 同一条): 这一档的判据是召回分, 而字面相似度在
    几十字的推文上量不出可信的值 —— 挂错了比不挂坏得多, 它会把两件事连成一件. """
    _seed_story("a-1", "Nova bridge drained for $12M")
    monkeypatch.setattr(embeddings, "warm", lambda texts: None)
    monkeypatch.setattr(grouping.llm, "complete_json",
                        lambda *a, **k: pytest.fail("没有向量线就不该问模型"))
    _stored("p-1", title=POST_TITLE, source_id=POST, source_kind="x", minutes=10)

    report = stories.run(NOW, budget=BUDGET)

    assert report["unmatched"] == 1
    assert db.feed_story_signal_members(SINCE) == []


# --- 回头捞: 帖子先到, 原帖后到 (AIHOT 的 rematchSignals + reclaimWaiting) --------------

def test_a_post_that_arrived_early_is_rechecked_when_the_story_appears(isolated_db, monkeypatch):
    """帖子先到、原帖后到: 判定站只判一次, 早就记了 unmatched —— 没有回头捞就永远掉在外面
    (AIHOT 的 `rematchSignals`)。这一趟只在**立了事件**之后跑, 判据是这条帖子与新报道的相似度。"""
    _vectors(monkeypatch, {"Nova": [1.0, 0.0]})
    monkeypatch.setattr(grouping.llm, "complete_json",
                        lambda *a, **k: pytest.fail("够 0.92 就不是问模型那一档"))
    _stored("p-1", title=POST_TITLE, source_id=POST, source_kind="x", minutes=30)

    first = stories.run(NOW, budget=BUDGET)          # 池子空: 没有候选可挂
    assert (first["unmatched"], first["rematched"]) == (1, 0)
    assert db.feed_story_signal_members(SINCE) == []

    _stored("a-1", title="Nova bridge drained for $12M", minutes=5)
    second = stories.run(NOW, budget=BUDGET)         # 这条立了事件 → 回头捞那条帖子

    assert second["rematched"] == 1
    story_id = db.feed_stories_all()[0]["id"]
    assert [(row["source_id"], row["story_id"]) for row in db.feed_story_signal_members(SINCE)] \
        == [(POST, story_id)]


def test_a_post_that_quotes_the_article_is_rechecked_when_the_article_appears(isolated_db, monkeypatch):
    """帖子引用了还没入库的那条推: 指向那一路 (`boosts`) 要等原帖入库才认得出来 —— 这也是
    回头捞里"等原帖"那条线 (AIHOT 的 `reclaimWaiting`)。它不靠向量: 没配端点也照样补。"""
    monkeypatch.setattr(embeddings, "warm", lambda texts: None)
    monkeypatch.setattr(grouping.llm, "complete_json",
                        lambda *a, **k: pytest.fail("指向的那条不该问模型"))
    _stored("p-1", title=POST_TITLE, source_id=POST, source_kind="x", minutes=30,
            quoted={"url": "https://x.com/binance/status/9", "handle": "binance"})

    first = stories.run(NOW, budget=BUDGET)
    assert (first["unmatched"], first["reclaimed"]) == (1, 0)

    _stored("9", title="Nova bridge drained for $12M", source_id="x:binance",
            source_kind="x", url="https://x.com/binance/status/9", minutes=5)
    second = stories.run(NOW, budget=BUDGET)

    assert second["reclaimed"] == 1
    story_id = db.feed_stories_all()[0]["id"]
    assert [(row["source_id"], row["story_id"]) for row in db.feed_story_signal_members(SINCE)] \
        == [(POST, story_id)]


def test_only_the_article_that_was_quoted_is_rechecked(isolated_db, monkeypatch):
    """引用的是**别人**那条: 原帖入库不该把这条帖子拖过来重问一遍 (AIHOT 按 `/status/<id>` 对)."""
    monkeypatch.setattr(embeddings, "warm", lambda texts: None)
    monkeypatch.setattr(grouping.llm, "complete_json",
                        lambda *a, **k: pytest.fail("没有指向、没有候选, 不该问模型"))
    _stored("p-1", title=POST_TITLE, source_id=POST, source_kind="x", minutes=30,
            quoted={"url": "https://x.com/binance/status/910", "handle": "binance"})

    assert stories.run(NOW, budget=BUDGET)["unmatched"] == 1

    _stored("9", title="Nova bridge drained for $12M", source_id="x:binance",
            source_kind="x", url="https://x.com/binance/status/9", minutes=5)
    second = stories.run(NOW, budget=BUDGET)

    assert (second["reclaimed"], second["rematched"]) == (0, 0)
    assert db.feed_story_signal_members(SINCE) == []


# --- 判定那一半 (纯函数) ---------------------------------------------------------

def _candidate(key: str, title: str, *, story: str = "", root: bool = True,
               score: float = 0.0) -> grouping.Candidate:
    return grouping.Candidate(key, story or key, title, "", "CoinDesk",
                              "2026-10-05T00:00:00Z", root, score)


def test_signal_target_prefers_the_same_occurrence():
    candidates = [_candidate("f:1", "A"), _candidate("f:2", "B")]
    verdicts = {"f:1": grouping.Verdict("same_story", 0.95),
                "f:2": grouping.Verdict("same_occurrence", 0.7)}
    assert grouping.signal_target(candidates, verdicts).key == "f:2"


def test_signal_target_drops_a_weak_reaction():
    """顺手评论一句 (0.5) 不算"在反应这件事": 收了它就会把两件事连成一件. """
    candidates = [_candidate("f:1", "A")]
    assert grouping.signal_target(candidates, {"f:1": grouping.Verdict("same_story", 0.5)}) is None
    picked = grouping.signal_target(candidates, {"f:1": grouping.Verdict("same_story", 0.9)})
    assert picked is not None and picked.key == "f:1"


def test_a_post_is_described_as_a_post_not_a_report():
    """同一份材料叫成"新报道", 模型会按报道的严法去读它 —— 措辞是判据的一部分. """
    block = grouping.user_block(
        _candidate("f:0", "P"), [_candidate("f:1", "A")],
        label="帖子", ask="这条帖子与每个候选的关系是什么？")
    assert block.startswith("【帖子】")
    assert "这条帖子与每个候选的关系是什么？" in block


# --- 读取侧: 数它一家, 但不印它 --------------------------------------------------

def test_evidence_counts_for_heat_but_is_not_printed(isolated_db, monkeypatch):
    """热点榜问的是"几家在说**这一次发生**", 转发一句也算一家在说; 但正文里没有它 ——
    一条几十字的推文当报道印出来, 读者看到的就是一条没有内容的新闻. """
    monkeypatch.setattr(writing, "enabled", lambda: False)
    db.feed_items_put([
        _row("a-1", title="Nova bridge drained for $12M", url="https://a/root"),
        _row("b-2", title="Nova bridge drained, $12M gone", source_id="rss:b",
             url="https://b/root", minutes=20),
        _row("p-1", title=POST_TITLE, source_id=POST, source_kind="x",
             url="https://x/p/1", minutes=15),
    ])
    at = (NOW - timedelta(minutes=90)).isoformat()
    story_id = db.feed_story_new({"title": "Nova bridge drained", "at": at})
    fact_id = db.feed_fact_new({"story_id": story_id, "title": "Nova bridge drained",
                                "at": at, "root": True, **FRAME})
    db.feed_item_facts_set([
        {"source_id": "rss:a", "external_id": "a-1", "fact_id": fact_id, "role": "primary"},
        {"source_id": "rss:b", "external_id": "b-2", "fact_id": fact_id, "role": "report"},
    ])
    db.feed_story_signal_set([{"source_id": POST, "external_id": "p-1",
                               "story_id": story_id, "kind": "signal",
                               "observed_at": NOW.isoformat()}])

    items, grouped = views.collect(NOW, views.HOT_HOURS)

    assert [item.discovery.external_id for item in items] == ["a-1", "b-2"]
    assert len(grouped) == 1
    assert heat.participants(grouped[0], NOW) == 3        # 两篇报道 + 一条讨论
    assert len(views.story_payload(grouped[0], NOW)["items"]) == 2


def test_a_story_with_nothing_but_evidence_is_not_a_story(isolated_db, monkeypatch):
    """一条帖子挂到了一件事上, 而那件事的报道都不在窗口里: 剩下的空壳不该印成一张空卡. """
    monkeypatch.setattr(writing, "enabled", lambda: False)
    db.feed_items_put([_row("p-1", title=POST_TITLE, source_id=POST, source_kind="x")])
    at = (NOW - timedelta(minutes=90)).isoformat()
    story_id = db.feed_story_new({"title": "Nova bridge drained", "at": at})
    db.feed_story_signal_set([{"source_id": POST, "external_id": "p-1",
                               "story_id": story_id, "kind": "signal",
                               "observed_at": NOW.isoformat()}])

    items, grouped = views.collect(NOW, views.HOT_HOURS)

    assert items == []
    assert grouped == []
