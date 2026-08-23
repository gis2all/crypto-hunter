"""一次完整正文的 LLM 分组：一轮覆盖全部帖子，失败不写半张表。"""

from __future__ import annotations

import pytest

from app.llm import client as llm
from app.opportunity import group, job, settings, topic
from app.store import db


@pytest.fixture(autouse=True)
def own_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LIVE_PATH", tmp_path / "opportunity.yaml")


def _post(tweet_id: str, text: str = "测试网已开放，去官网参与", *, handle: str = "spoutfi",
          score: int = 100) -> None:
    db.opportunity_post_put({
        "tweet_id": tweet_id, "handle": handle, "text": text,
        "posted_at": "2026-10-18T00:00:00Z",
        "url": "https://x.com/%s/status/%s" % (handle, tweet_id),
        "source_target": handle, "score": score, "first_seen_scan": "test",
    })


def _answer(groups=None, irrelevant=None, uncertain=None) -> dict:
    return {"groups": groups or [], "irrelevant": irrelevant or [],
            "uncertain": uncertain or []}


def _marks() -> dict[str, tuple[str | None, str | None]]:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT tweet_id, classified, project_name"
                            " FROM opportunity_posts").fetchall()
    finally:
        conn.close()
    return {row["tweet_id"]: (row["classified"], row["project_name"]) for row in rows}


def test_the_prompt_carries_the_whole_post_without_truncation(isolated_db, monkeypatch):
    marker = "END-OF-POST-9f3a"
    text = "开头 " + ("x" * 1200) + marker
    _post("t1", text)
    seen: list[str] = []

    def fake(messages, **kw):
        seen.append(str(messages[-1].get("content") or ""))
        return _answer([{"name": "Spout Finance", "handle": "spoutfi",
                         "kind": "activity", "ids": ["p1"]}])

    monkeypatch.setattr(llm, "complete_json", fake)
    out = group.run(limit=10)

    assert out["grouped"] == 1
    assert text in seen[0], "1200 字符的正文必须完整出现在 prompt 里"
    assert marker in seen[0]


def test_groups_merge_by_handle_and_invalid_or_empty_groups_are_dropped(isolated_db,
                                                                         monkeypatch):
    for ref in ("t1", "t2", "t3", "t4"):
        _post(ref)

    monkeypatch.setattr(llm, "complete_json", lambda messages, **kw: _answer([
        {"name": "Spout Finance", "handle": "spoutfi", "kind": "activity", "ids": ["p1"]},
        {"name": "Spout", "handle": "spoutfi", "kind": "activity", "ids": ["p2", "p1"]},
        {"name": "Other", "handle": "other", "kind": "project", "ids": ["p3"]},
        {"name": "", "kind": "activity", "ids": []},
        {"name": "Bad", "kind": "nonsense", "ids": []},
    ], ["p4", "p999"]))

    out = group.run(limit=10)

    assert out["groups"] == 2, "同 handle 的两个组合成一个"
    assert out["grouped"] == 3
    assert out["irrelevant"] == 1
    assert out["dropped"] == 2
    assert out["unknown"] == 1
    assert _marks() == {
        "t1": ("activity", "Spout Finance"),
        "t2": ("activity", "Spout Finance"),
        "t3": ("project", "Other"),
        "t4": ("other", None),
    }


def test_an_id_in_two_groups_goes_to_the_first_group(isolated_db, monkeypatch):
    _post("t1")
    _post("t2")
    monkeypatch.setattr(llm, "complete_json", lambda messages, **kw: _answer([
        {"name": "First", "handle": "first", "kind": "activity", "ids": ["p1"]},
        {"name": "Second", "handle": "second", "kind": "activity", "ids": ["p1", "p2"]},
    ]))

    out = group.run(limit=10)

    assert out["duplicates"] == 1
    assert _marks() == {
        "t1": ("activity", "First"),
        "t2": ("activity", "Second"),
    }


def test_a_named_group_without_handle_joins_the_handle_group(isolated_db, monkeypatch):
    _post("t1")
    _post("t2")
    monkeypatch.setattr(llm, "complete_json", lambda messages, **kw: _answer([
        {"name": "Spout Finance", "kind": "activity", "ids": ["p2"]},
        {"name": "Spout Finance", "handle": "spoutfi", "kind": "activity", "ids": ["p1"]},
    ]))

    out = group.run(limit=10)

    assert out["groups"] == 1
    assert out["grouped"] == 2
    assert _marks() == {
        "t1": ("activity", "Spout Finance"),
        "t2": ("activity", "Spout Finance"),
    }


def test_missing_and_uncertain_ids_get_one_repair_call(isolated_db, monkeypatch):
    for ref in ("t1", "t2", "t3"):
        _post(ref)
    prompts: list[tuple[str, str]] = []

    def fake(messages, **kw):
        purpose = str(kw.get("purpose") or "")
        body = str(messages[-1].get("content") or "")
        prompts.append((purpose, body))
        if purpose == "opportunity_group_repair":
            assert "p2" in body and "p3" in body and "p1" not in body
            return _answer([{"name": "Repaired", "handle": "repair",
                             "kind": "project", "ids": ["p2", "p3"]}])
        return _answer([{"name": "First", "handle": "first",
                         "kind": "activity", "ids": ["p1"]}], [], ["p2"])

    monkeypatch.setattr(llm, "complete_json", fake)
    out = group.run(limit=10)

    assert [purpose for purpose, _ in prompts] == ["opportunity_group",
                                                   "opportunity_group_repair"]
    assert out["repair_calls"] == 1
    assert _marks()["t1"] == ("activity", "First")
    assert _marks()["t2"] == ("project", "Repaired")
    assert _marks()["t3"] == ("project", "Repaired")


def test_a_second_repair_can_finish_the_remaining_ids(isolated_db, monkeypatch):
    _post("t1")
    _post("t2")
    repairs = 0


    def fake(messages, **kw):
        nonlocal repairs
        if kw.get("purpose") == "opportunity_group_repair":
            repairs += 1
            if repairs == 1:
                return _answer([], [], ["p2"])
            return _answer([{"name": "Repaired", "handle": "repair",
                             "kind": "project", "ids": ["p2"]}])
        return _answer([{"name": "First", "handle": "first",
                         "kind": "activity", "ids": ["p1"]}])


    monkeypatch.setattr(llm, "complete_json", fake)
    out = group.run(limit=10)


    assert out["repair_calls"] == 2
    assert _marks()["t1"] == ("activity", "First")
    assert _marks()["t2"] == ("project", "Repaired")
def test_unresolved_after_repairs_are_folded_into_other(isolated_db, monkeypatch):
    _post("t1")
    _post("t2")
    _post("t3")

    def fake(messages, **kw):
        if kw.get("purpose") == "opportunity_group_repair":
            return _answer([], [], ["p2"])
        return _answer([{"name": "First", "handle": "first",
                         "kind": "activity", "ids": ["p1"]}])

    monkeypatch.setattr(llm, "complete_json", fake)
    out = group.run(limit=10)

    assert out["repair_calls"] == 2
    assert out["uncertain"] == 2, "p2 待定、p3 漏掉，都计入待定归其他"
    assert _marks() == {
        "t1": ("activity", "First"),
        "t2": ("other", None),
        "t3": ("other", None),
    }


def test_more_pending_posts_than_the_cap_fails_before_any_call(isolated_db, monkeypatch):
    _post("t1")
    _post("t2")
    called = []
    monkeypatch.setattr(llm, "complete_json",
                        lambda messages, **kw: called.append("called") or _answer())

    with pytest.raises(RuntimeError, match="超过"):
        group.run(limit=1)

    assert called == []
    assert _marks() == {"t1": (None, None), "t2": (None, None)}


def test_a_malformed_batch_answer_fails_without_writing(isolated_db, monkeypatch):
    _post("t1")
    monkeypatch.setattr(llm, "complete_json", lambda messages, **kw: {"groups": "not-a-list"})

    with pytest.raises(RuntimeError, match="缺少 groups"):
        group.run(limit=10)

    assert _marks() == {"t1": (None, None)}


def test_group_put_rolls_back_the_whole_batch_on_a_bad_row(isolated_db):
    _post("t1")
    _post("t2")

    with pytest.raises(KeyError):
        db.opportunity_group_put([
            {"ref": "t1", "kind": "activity", "name": "First"},
            {"ref": "t2"},
        ], [])

    assert _marks() == {"t1": (None, None), "t2": (None, None)}


def test_topic_consumes_every_post_in_the_group_not_only_the_prompt_ten():
    materials = [{
        "ref": "t%d" % index, "project_name": "Same Project", "source": "someone",
        "score": 100 - index, "text": "post %d" % index, "at": "2026-10-18",
    } for index in range(10)]

    topics = topic.merge(materials)

    assert len(topics) == 1
    assert topics[0]["posts"] == 10, "提示词仍只读前 10 条"
    assert len(topics[0]["consumed"]) == 10, "整组帖子都要标记已读，否则下一轮再出卡"


def test_the_topic_limit_is_not_blocked_by_the_post_scan_floor(isolated_db):
    for index in range(2):
        _post("topic-%d" % index, handle="h%d" % index)
        db.opportunity_mark("topic-%d" % index, "post", classified="activity")
    conn = db.connect()
    try:
        for index in range(2):
            conn.execute("UPDATE opportunity_posts SET project_name = ? WHERE tweet_id = ?",
                         ("Project %d" % index, "topic-%d" % index))
    finally:
        conn.close()


    got = topic.materials(limit=2, scan=1)


    assert len(got) == 2, "scan 应至少扩展到 limit × 每主题帖子数"
def test_the_scan_job_calls_group_instead_of_the_old_classifier(isolated_db, monkeypatch):
    from app.opportunity import classify, extract, lifecycle, scan as scanner, trace, watchlist

    called: list[str] = []
    monkeypatch.setattr(job, "submit", lambda _job: None)
    monkeypatch.setattr(watchlist, "ensure_seeded", lambda: None)
    monkeypatch.setattr(watchlist, "enabled", lambda: [])
    monkeypatch.setattr(scanner, "collect", lambda *args, **kw: {})
    monkeypatch.setattr(llm, "configured", lambda: True)
    monkeypatch.setattr(group, "run", lambda **kw: called.append("group") or {
        "asked": 0, "groups": 0, "grouped": 0, "irrelevant": 0, "uncertain": 0,
        "missing": 0, "duplicates": 0, "dropped": 0, "unknown": 0, "repair_calls": 0,
        "activity": 0, "project": 0, "unreadable": 0})
    monkeypatch.setattr(classify, "run",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("classify")))
    monkeypatch.setattr(trace, "run", lambda **kw: {})
    monkeypatch.setattr(extract, "run", lambda **kw: {})
    monkeypatch.setattr(extract, "refresh_heat", lambda: 0)
    monkeypatch.setattr(lifecycle, "sweep", lambda: {})

    started = job.start()
    job.drive(started)
    stored = db.opportunity_scan_get(started["id"])

    assert called == ["group"]
    assert stored["status"] == "done"
    assert stored["counters"]["classify"]["groups"] == 0


def test_the_setting_is_named_as_a_grouping_limit_with_a_thousand_default():
    field = next(item for item in settings.FIELDS if item["key"] == "classify_per_scan")
    assert field["label"] == "分组帖子上限"
    assert field["default"] == 1000
