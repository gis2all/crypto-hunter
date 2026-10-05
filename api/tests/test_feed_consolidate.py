"""合并与相关: 一条报道绑住两件事时比两个根, 两次都说是同一件才合。

判定站只回答"这条报道和每个候选是什么关系"; 这一站补上的是"那两件事本身是不是一件"。测的是
顺序与底线: 早的当锚点、反着复核一次、盘点永不合并、模型不可用就收手、人工决定不被推翻。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.feed import consolidate, grouping, writing
from app.store import db

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)

BUDGET = {"maxItems": 10, "concurrency": 1, "timeoutSeconds": 5}

FRAME = {"subject": "Nova", "action": "bridge drained", "object": "$12M"}


@pytest.fixture(autouse=True)
def station_on(isolated_db, monkeypatch):
    monkeypatch.setattr(writing, "enabled", lambda: True)


def _report(source_id: str, external_id: str, *, minutes: float) -> None:
    at = (NOW - timedelta(minutes=minutes)).isoformat()
    db.feed_items_put([{
        "source_id": source_id, "external_id": external_id, "source_kind": "rss",
        "identity_key": "other|", "title": "Nova bridge drained",
        "url": "https://a/%s" % external_id, "body": "<p>x</p>", "published_at": at,
    }])
    db.feed_items_write_llm([{
        "source_id": source_id, "external_id": external_id, "title_zh": "Nova 桥被盗",
        "summary_llm": "一句话摘要", "fact_json": json.dumps(FRAME, ensure_ascii=False),
        "llm_stage": "full", "llm_at": NOW.isoformat(),
    }])


def _story(source_id: str, external_id: str, *, minutes: float) -> int:
    """一件事 + 它的根事实 + 一条报道。"""
    _report(source_id, external_id, minutes=minutes)
    story_id = db.feed_story_new({"title": "Nova 桥被盗", "at": NOW.isoformat()})
    fact_id = db.feed_fact_new({"story_id": story_id, "title": "Nova 桥被盗",
                                "at": NOW.isoformat(), "root": True, **FRAME})
    db.feed_item_facts_set([{"source_id": source_id, "external_id": external_id,
                             "fact_id": fact_id, "role": "primary"}])
    db.feed_story_touch(story_id, (NOW - timedelta(minutes=minutes)).isoformat())
    return story_id


def _tie(rows) -> None:
    db.feed_story_ties_set([
        {"source_id": source_id, "external_id": external_id, "story_id": story_id,
         "relation": relation, "confidence": confidence, "at": NOW.isoformat()}
        for source_id, external_id, story_id, relation, confidence in rows])


def _pair(monkeypatch, answers) -> list[tuple[str, str, str]]:
    """模型对每一对关系的回答 (按调用顺序), 记下每次的 purpose 与材料。"""
    seen: list[tuple[str, str, str]] = []
    queue = list(answers)

    def answer(messages, **kw):
        seen.append((kw.get("purpose", ""), messages[0]["content"][:12],
                     messages[1]["content"][:40]))
        return queue.pop(0)

    monkeypatch.setattr(grouping.llm, "complete_json", answer)
    return seen


def _same(confidence: float = 0.95) -> dict:
    return {"a": "同一件事", "b": "同一件事", "relation": "same_occurrence",
            "difference": "", "confidence": confidence}


def _unrelated() -> dict:
    return {"a": "一件事", "b": "另一件事", "relation": "unrelated",
            "difference": "不是同一件事", "confidence": 0.9}


def test_both_rounds_must_say_one_story_before_merging(monkeypatch):
    early = _story("alpha", "a1", minutes=60)
    late = _story("beta", "b1", minutes=30)
    _tie([("alpha", "a1", early, "same_occurrence", 0.95),
          ("alpha", "a1", late, "same_story", 0.9)])
    calls = _pair(monkeypatch, [_same(), _same(0.8)])

    report = consolidate.run(NOW, budget=BUDGET)

    assert report == {"asked": 1, "merged": 1, "kept": 0, "failed": 0}
    # 第一次判定走 group 档, 反着复核走 group_review 档.
    assert [purpose for purpose, _, _ in calls] == [grouping.PURPOSE_GROUP,
                                                    grouping.PURPOSE_GROUP_REVIEW]
    # 早的当锚点: 晚的那件并进去, 旧 id 还落得回来.
    assert db.feed_story_leader(late) == early
    assert [row["id"] for row in db.feed_stories_all() if row["merged_into"] is None] == [early]


def test_the_review_has_the_last_word(monkeypatch):
    early = _story("alpha", "a1", minutes=60)
    late = _story("beta", "b1", minutes=30)
    _tie([("alpha", "a1", early, "same_occurrence", 0.95),
          ("alpha", "a1", late, "same_story", 0.9)])
    _pair(monkeypatch, [_same(), _unrelated()])

    report = consolidate.run(NOW, budget=BUDGET)

    assert report == {"asked": 1, "merged": 0, "kept": 1, "failed": 0}
    assert db.feed_story_leader(late) == late


def test_the_anchor_keeps_its_name_and_loses_its_digest(monkeypatch):
    early = _story("alpha", "a1", minutes=60)
    late = _story("beta", "b1", minutes=30)
    db.feed_story_digest_set(early, {"title": "上一版标题", "digest": "上一版综述",
                                     "latest": "上一版进展", "digest_inputs": "x", "digest_ids": "y"})
    _tie([("alpha", "a1", early, "same_occurrence", 0.95),
          ("alpha", "a1", late, "same_story", 0.9)])
    _pair(monkeypatch, [_same(), _same(0.8)])

    consolidate.run(NOW, budget=BUDGET)

    row = [r for r in db.feed_stories_all() if r["id"] == early][0]
    # 成员变了, 上一版结论就作废: 下一轮综述站按新输入重写, 标题退回代表报道的.
    assert row["digest"] == "" and row["latest"] == "" and row["digest_inputs"] == ""
    assert row["title"] == "Nova 桥被盗"


def test_a_roundup_story_is_never_merged(monkeypatch):
    early = _story("alpha", "a1", minutes=60)
    late = _story("beta", "b1", minutes=30)
    db.feed_item_facts_set([{"source_id": "alpha", "external_id": "a1",
                             "fact_id": db.feed_story_root_report(early)["fact_id"],
                             "role": "primary", "relation": "roundup"}])
    _tie([("alpha", "a1", early, "same_occurrence", 0.95),
          ("alpha", "a1", late, "same_story", 0.9)])
    calls = _pair(monkeypatch, [_same(), _same(0.8)])

    report = consolidate.run(NOW, budget=BUDGET)

    assert report["asked"] == 0 and calls == []
    assert db.feed_story_leader(late) == late


def test_the_station_stops_when_the_model_is_unavailable(monkeypatch):
    early = _story("alpha", "a1", minutes=60)
    late = _story("beta", "b1", minutes=30)
    _tie([("alpha", "a1", early, "same_occurrence", 0.95),
          ("alpha", "a1", late, "same_story", 0.9)])

    def refuse(messages, **kw):
        raise grouping.llm.LLMUnavailable("no model")

    monkeypatch.setattr(grouping.llm, "complete_json", refuse)

    report = consolidate.run(NOW, budget=BUDGET)

    assert report == {"asked": 0, "merged": 0, "kept": 0, "failed": 1}
    assert db.feed_story_leader(late) == late


def test_two_reports_link_two_stories_that_stay_apart():
    first = _story("alpha", "a1", minutes=60)
    second = _story("beta", "b1", minutes=30)
    _tie([("gamma", "g1", first, "same_occurrence", 0.9),
          ("gamma", "g1", second, "same_story", 0.85),
          ("delta", "d1", first, "same_story", 0.9),
          ("delta", "d1", second, "same_occurrence", 0.9)])

    report = consolidate.links(NOW)

    assert report == {"added": 2}
    rows = db.feed_story_links((NOW - timedelta(days=1)).isoformat())
    assert {(row["story_id"], row["other_id"]) for row in rows} == {(first, second),
                                                                   (second, first)}


def test_one_report_is_not_enough_to_link():
    first = _story("alpha", "a1", minutes=60)
    second = _story("beta", "b1", minutes=30)
    _tie([("gamma", "g1", first, "same_occurrence", 0.9),
          ("gamma", "g1", second, "same_story", 0.85)])

    assert consolidate.links(NOW) == {"added": 0}
    assert db.feed_story_links((NOW - timedelta(days=1)).isoformat()) == []


def test_a_decision_made_while_the_model_runs_wins(monkeypatch):
    """判定跑完之前人点了「放在这里」, 那一票不能被算完的模型答案盖掉 (AIHOT 的 manualDecision)。"""
    from app.feed import stories

    _report("alpha", "a1", minutes=10)
    _story("beta", "b1", minutes=40)
    marked = {"done": False}

    def answer(messages, **kw):
        if not marked["done"]:
            marked["done"] = True
            db.feed_override_set({"source_id": "alpha", "external_id": "a1",
                                  "reason": "人在判定途中点的"})
        return {"query": "x", "decisions": []}

    monkeypatch.setattr(grouping.llm, "complete_json", answer)

    report = stories.run(NOW, budget=BUDGET)

    assert report["manual"] == 1
    assert [row for row in db.feed_item_facts_all()
            if row["external_id"] == "a1"] == []


def test_a_manual_decision_takes_the_item_out_of_the_queue():
    _report("alpha", "a1", minutes=10)
    assert len(db.feed_items_for_grouping(10)) == 1

    db.feed_override_set({"source_id": "alpha", "external_id": "a1",
                          "reason": "人工放在这件事下面"})
    assert db.feed_items_for_grouping(10) == []

    db.feed_override_clear("alpha", "a1")
    assert len(db.feed_items_for_grouping(10)) == 1
