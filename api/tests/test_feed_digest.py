"""事件综述: 按时间重写一整段话、矛盾要说明白、输入没变就不重写。

判定之外的编排也在这里测: 模型不可用就整轮收手 (半截写下去, 页面上会有的模型写、有的规则版),
以及读回来的时候模型写的字确实盖在规则版上面 (views.collect).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.feed import digest, stories, views, writing
from app.feed.sources import store as source_store
from app.store import db

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)

BUDGET = {"maxItems": 10, "concurrency": 1, "timeoutSeconds": 5}

FRAME = {"subject": "Nova", "action": "bridge drained", "object": "$12M"}

DRAFT = {"title": "Nova 跨链桥被盗 1200 万美元", "digest": "10 月 5 日早, Nova 的跨链桥被转走 1200 万美元。",
         "latest": "官方确认被盗，桥已暂停。"}


@pytest.fixture(autouse=True)
def roster(isolated_db):
    source_store.import_roster()


@pytest.fixture(autouse=True)
def station_on(monkeypatch):
    monkeypatch.setattr(writing, "enabled", lambda: True)


def _report(source_id: str, external_id: str, *, minutes: float, title: str = "Nova bridge drained") -> None:
    """一条「写作站已经写过」的报道: 有中文标题与摘要, 也已经归到某件事上。"""
    at = (NOW - timedelta(minutes=minutes)).isoformat()
    db.feed_items_put([{
        "source_id": source_id, "external_id": external_id, "source_kind": "rss",
        "identity_key": "other|", "title": title, "url": "https://a/%s" % external_id,
        "body": "<p>x</p>", "published_at": at,
    }])
    db.feed_items_write_llm([{
        "source_id": source_id, "external_id": external_id, "title_zh": title,
        "summary_llm": "一句话摘要", "fact_json": json.dumps(FRAME, ensure_ascii=False),
        "llm_stage": "full", "llm_at": NOW.isoformat(),
    }])


def _story(*reports: tuple[str, str, float]) -> int:
    """一件事 + 它的报道。报道用 (source_id, external_id, 几分钟前) 给。"""
    for source_id, external_id, minutes in reports:
        _report(source_id, external_id, minutes=minutes)
    story_id = db.feed_story_new({"title": "Nova bridge drained", "at": NOW.isoformat()})
    fact_id = db.feed_fact_new({"story_id": story_id, "title": "Nova bridge drained",
                                "at": NOW.isoformat(), "root": True, **FRAME})
    db.feed_item_facts_set([
        {"source_id": source_id, "external_id": external_id, "fact_id": fact_id,
         "role": "primary" if index == 0 else "report"}
        for index, (source_id, external_id, _) in enumerate(reports)
    ])
    db.feed_story_touch(story_id, (NOW - timedelta(minutes=5)).isoformat())
    return story_id


def _fake(monkeypatch, answer) -> list[str]:
    seen: list[str] = []
    prompts: list[str] = []

    def call(messages, **kw):
        seen.append(kw["purpose"])
        prompts.append(messages[1]["content"])
        got = answer(kw["purpose"]) if callable(answer) else answer[kw["purpose"]]
        if isinstance(got, Exception):
            raise got
        return got

    monkeypatch.setattr(digest.llm, "complete_json", call)
    return seen, prompts


def test_the_status_follows_how_long_it_has_been_quiet():
    assert digest.status_for((NOW - timedelta(hours=2)).isoformat(), NOW) == "active"
    assert digest.status_for((NOW - timedelta(hours=48)).isoformat(), NOW) == "watching"
    assert digest.status_for((NOW - timedelta(days=9)).isoformat(), NOW) == "settled"
    assert digest.status_for("", NOW) == "settled"


def test_a_story_gets_a_written_digest_and_keeps_its_input_fingerprint(isolated_db, monkeypatch):
    story_id = _story(("rss:a", "a-1", 120), ("rss:b", "b-1", 30))
    seen, _ = _fake(monkeypatch, {digest.PURPOSE_DIGEST: DRAFT})

    report = digest.run(NOW, budget=BUDGET)

    assert report == {"asked": 1, "written": 1, "failed": 0, "changed": 0}
    assert seen == [digest.PURPOSE_DIGEST]
    rows = db.feed_story_digests((NOW - timedelta(days=30)).isoformat(timespec="seconds"))
    assert len(rows) == 1
    assert rows[0]["title"] == DRAFT["title"]
    assert rows[0]["digest"] == DRAFT["digest"]
    assert rows[0]["latest"] == DRAFT["latest"]
    # 输入指纹跟文字一起落笔, 而且记得住那一次覆盖的是哪些报道.
    stored = db.feed_stories_for_digest((NOW - timedelta(days=30)).isoformat(timespec="seconds"))[0]
    assert stored["digest_inputs"] and json.loads(stored["digest_ids"]) == ["rss:a|a-1", "rss:b|b-1"]
    assert db.feed_story_reports(story_id, 40)[0]["source_id"] == "rss:a"
    assert db.feed_story_reports(story_id)[0]["external_id"] == "a-1"


def test_nothing_to_rewrite_costs_nothing(isolated_db, monkeypatch):
    _story(("rss:a", "a-1", 30))
    _fake(monkeypatch, {digest.PURPOSE_DIGEST: DRAFT})
    digest.run(NOW, budget=BUDGET)
    seen, _ = _fake(monkeypatch, {digest.PURPOSE_DIGEST: DRAFT})
    assert digest.run(NOW, budget=BUDGET) == {"asked": 0, "written": 0, "failed": 0, "changed": 0}
    assert seen == []


def test_a_new_report_makes_the_digest_stale_and_the_round_is_incremental(isolated_db, monkeypatch):
    _story(("rss:a", "a-1", 120))
    _fake(monkeypatch, {digest.PURPOSE_DIGEST: DRAFT})
    digest.run(NOW, budget=BUDGET)
    _report("rss:b", "b-1", minutes=10)
    fact_id = db.feed_fact_pool((NOW - timedelta(days=30)).isoformat(timespec="seconds"))[0][0]["id"]
    db.feed_item_facts_set([{"source_id": "rss:b", "external_id": "b-1", "fact_id": fact_id,
                             "role": "report"}])
    seen, prompts = _fake(monkeypatch, {digest.PURPOSE_DIGEST: DRAFT})
    assert digest.run(NOW, budget=BUDGET) == {"asked": 1, "written": 1, "failed": 0, "changed": 0}

    # 上一版覆盖的报道一条没少、内容也没变 → 这一趟是增量: 上一版一起给它, 新的那条标【新】.
    assert seen == [digest.PURPOSE_DIGEST]
    assert "已核对输入的上一版综述：" in prompts[0] and DRAFT["digest"] in prompts[0]
    in_block = prompts[0].split("标【新】的是新增报道）：", 1)[1]
    marked = [line for line in in_block.split("\n") if line.startswith("【新】")]
    assert len(marked) == 1 and "rss:b" in marked[0]
    assert in_block.index("rss:a") < in_block.index(marked[0])   # 早的在前, 【新】压在后面


def test_a_changed_summary_with_the_same_report_count_makes_the_digest_stale(isolated_db, monkeypatch):
    """条数一样而指纹不一样: 换的是内容. 条数那个老口径看不见这一种 (AIHOT 的 inputsHash)."""
    _story(("rss:a", "a-1", 120), ("rss:b", "b-1", 30))
    _fake(monkeypatch, {digest.PURPOSE_DIGEST: DRAFT})
    digest.run(NOW, budget=BUDGET)

    db.feed_items_write_llm([{"source_id": "rss:b", "external_id": "b-1",
                              "summary_llm": "换了一句摘要"}])
    seen, _ = _fake(monkeypatch, {digest.PURPOSE_DIGEST: DRAFT})
    assert digest.run(NOW, budget=BUDGET) == {"asked": 1, "written": 1, "failed": 0, "changed": 0}
    assert seen == [digest.PURPOSE_DIGEST]


def test_a_digest_whose_inputs_move_while_the_model_writes_is_dropped(isolated_db, monkeypatch):
    """AIHOT `composeStoryDigest`: 答案回来之后把输入重新核一遍, 对不上就丢掉这次答案.

    模型想答案的这段时间里, 这件事可能又落进来一条报道 (合并搬事实、纠错作废、下一轮重分组).
    一段描述「另一批报道」的话写上去, 比这一轮没写更坏 —— 它还会带着一份对不上的指纹落库,
    让下一次「输入没变」判在一份错的底稿上.
    """
    story_id = _story(("rss:a", "a-1", 120), ("rss:b", "b-1", 30))

    def answer(purpose):
        _report("rss:c", "c-1", minutes=5, title="Nova bridge drained, again")
        fact_id = db.feed_fact_new({"story_id": story_id, "title": "Nova bridge drained, again",
                                    "at": NOW.isoformat(), "root": False, **FRAME})
        db.feed_item_facts_set([{"source_id": "rss:c", "external_id": "c-1",
                                 "fact_id": fact_id, "role": "report"}])
        return DRAFT

    _fake(monkeypatch, answer)
    report = digest.run(NOW, budget=BUDGET)
    assert report == {"asked": 1, "written": 0, "failed": 0, "changed": 1}
    assert db.feed_story_digests((NOW - timedelta(days=30)).isoformat(timespec="seconds")) == []
    assert db.feed_story_reports(story_id)[-1]["external_id"] == "c-1"


def test_a_dead_model_stops_the_round_instead_of_half_writing(isolated_db, monkeypatch):
    _story(("rss:a", "a-1", 120), ("rss:b", "b-1", 30))
    _fake(monkeypatch, {digest.PURPOSE_DIGEST: digest.llm.LLMUnavailable("503")})
    assert digest.run(NOW, budget=BUDGET) == {"asked": 1, "written": 0, "failed": 1, "changed": 0}
    assert db.feed_story_digests((NOW - timedelta(days=30)).isoformat(timespec="seconds")) == []


def test_the_written_digest_is_what_the_page_prints(isolated_db, monkeypatch):
    """读回来的时候, 模型写的标题 / 综述 / 最新进展盖住规则版 —— 而且排在写作站那一步**之后**."""
    _story(("rss:a", "a-1", 120), ("rss:b", "b-1", 30))
    _fake(monkeypatch, {digest.PURPOSE_DIGEST: DRAFT})
    digest.run(NOW, budget=BUDGET)

    _, events = views.collect(NOW, 72.0)
    story = [event for event in events if event.identity_key.startswith("s:")][0]
    assert story.title == DRAFT["title"]
    assert story.summary == [DRAFT["digest"]]
    assert story.latest == DRAFT["latest"]
    assert story.status == "active"
    # 事件的成员报道还是从写作站那一层读的 (中文标题), 综述只换事件自己的那几格.
    assert {item.display_title for item in story.items} == {"Nova bridge drained"}


def test_the_prompt_file_is_a_part_of_the_version():
    assert writing.version(digest.DIGEST_PROMPT).startswith("story-digest.md@")


def test_the_input_fingerprint_sees_a_swapped_report_and_ignores_block_edits():
    """指纹只认进得了综述的字段: 换一条报道看得出来, 正文里某一块重译不算. """
    first = [{"source_id": "rss:a", "external_id": "a-1", "published_at": "2026-10-05T08:00:00+00:00",
              "title_zh": "标题", "summary_llm": "摘要"}]
    swapped = [{"source_id": "rss:c", "external_id": "c-1", "published_at": "2026-10-05T08:00:00+00:00",
                "title_zh": "标题", "summary_llm": "摘要"}]
    assert digest.inputs_hash(first) != digest.inputs_hash(swapped)
    assert digest.inputs_hash(first) == digest.inputs_hash([dict(first[0], body_zh="<p>换了译文</p>")])
    assert digest.report_id(first[0]) == "rss:a|a-1"
    assert digest._known_ids("坏掉的 JSON") == []
    assert digest._known_ids('["rss:a|a-1"]') == ["rss:a|a-1"]
