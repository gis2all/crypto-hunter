"""分组: 四类关系, 到线才合并, 只挂故事的根。

这里测的是判定那一半 (grouping.py)。读写库那一半在采集站, 所以这一份可以拿假模型整段测。
"""

from __future__ import annotations

import pytest

from app.feed import grouping


def _candidate(key: str, title: str, *, story: str = "", root: bool = True,
               summary: str = "", score: float = 0.0) -> grouping.Candidate:
    return grouping.Candidate(key, story or key, title, summary, "CoinDesk", "2026-10-05T00:00:00Z",
                              root, score)


def test_the_similarity_is_shared_character_bigrams():
    assert grouping.lexical("Nova bridge drained", "Nova bridge drained") == 1.0
    # 偶发的字面重合是有的（"drained"/"lists" 共享 ai/ne 这种二元组），但要落在召回线之下。
    assert grouping.lexical("Nova bridge drained", "Binance lists Pulse") < grouping.LEXICAL_MIN
    assert 0 < grouping.lexical("Nova bridge drained for 12M", "Nova bridge drained for $12M") < 1


def test_recall_keeps_only_what_is_close_enough_and_the_best_ten():
    query = _candidate("q", "Nova bridge drained for 12M")
    near = _candidate("near", "Nova bridge drained for 12M")
    far = _candidate("far", "Binance lists Pulse")
    assert [c.key for c in grouping.recall(query, [near, far])] == ["near"]

    many = [_candidate("f%d" % index, "Nova bridge drained for 12M") for index in range(12)]
    assert len(grouping.recall(query, many)) == grouping.RECALL_TOP


def test_a_known_reference_is_not_something_the_model_has_to_guess():
    """推文回复/引用的那一条是材料里写着的事实, 直接进候选 (AIHOT 的 boost)。"""
    query = _candidate("q", "I agree with this")
    quoted = _candidate("quoted", "Something completely different")
    assert [c.key for c in grouping.recall(query, [], boost=[quoted])] == ["quoted"]


def test_verdicts_are_handed_back_by_candidate_number():
    candidates = [_candidate("f1", "one"), _candidate("f2", "two"), _candidate("f3", "three")]
    draft = grouping.GroupDraft.model_validate({
        "query": "something happened",
        "decisions": [{"id": "C2", "relation": "same_occurrence", "confidence": 0.9},
                      {"id": "banana", "relation": "same_story", "confidence": 1.0}]})
    verdicts = grouping.verdicts_of(draft, candidates)
    # C2 是第二个候选, 不是第二个决定; 认不出的 id 不是候选, 落不到任何人身上.
    assert verdicts["f2"].relation == "same_occurrence"
    assert verdicts["f2"].confidence == 0.9
    # 模型漏掉的候选算 unrelated —— 沉默不是同意.
    assert verdicts["f1"].relation == "unrelated"
    assert verdicts["f3"].relation == "unrelated"


def test_the_relation_may_come_back_in_any_of_the_spellings_we_accept():
    assert grouping._relation("SAME_OCCURRENCE") == "same_occurrence"
    assert grouping._relation("同一次发生") == "same_occurrence"
    assert grouping._relation("something else entirely") == "unrelated"


def test_same_occurrence_is_sorted_by_confidence_then_by_recall():
    a = _candidate("a", "one", score=0.99)
    b = _candidate("b", "two", score=0.5)
    verdicts = {"a": grouping.Verdict("same_occurrence", 0.7),
                "b": grouping.Verdict("same_occurrence", 0.7)}
    assert [c.key for c in grouping.same_occurrence([a, b], verdicts)] == ["a", "b"]
    verdicts["b"] = grouping.Verdict("same_occurrence", 0.9)
    assert [c.key for c in grouping.same_occurrence([a, b], verdicts)] == ["b", "a"]


def test_a_development_only_hangs_off_the_root_of_a_story():
    """一条已经有后续的事实不再收新的后续: 链式挂会把两年前的事挂到今天 (AIHOT 的 storyRoot)。"""
    root = _candidate("root", "an exploit", root=True)
    later = _candidate("later", "the response to it", root=False)
    verdicts = {"root": grouping.Verdict("same_story", 0.9),
                "later": grouping.Verdict("same_story", 0.9)}
    assert grouping.story_for_development([later], verdicts) is None
    assert grouping.story_for_development([root], verdicts).key == "root"


def test_a_roundup_never_merges():
    query = _candidate("q", "Daily roundup: ten things")
    one = _candidate("f1", "an exploit")
    verdicts = {"f1": grouping.Verdict("roundup", 0.9)}
    assert grouping.looks_like_roundup([one], verdicts) is True
    assert grouping.decide(query, [one], verdicts).story == ""


def test_a_confident_same_occurrence_reports_into_that_fact():
    query = _candidate("q", "Nova bridge drained")
    fact = _candidate("f1", "Nova bridge drained", story="s1", score=0.9)
    decision = grouping.decide(query, [fact],
                               {"f1": grouping.Verdict("same_occurrence", 0.95)})
    assert (decision.story, decision.fact, decision.role) == ("s1", "f1", "report")


def test_a_weak_match_is_reviewed_before_it_is_trusted():
    """召回相似度不高、模型却说是同一次发生: 值得再问一遍 (AIHOT 的 confirmMerge)。"""
    query = _candidate("q", "Nova bridge drained")
    fact = _candidate("f1", "Nova Protocol halts bridge", story="s1", score=0.5)
    verdicts = {"f1": grouping.Verdict("same_occurrence", 0.9)}

    kept = grouping.decide(query, [fact], verdicts,
                           confirm=lambda a, b: grouping.Verdict("same_occurrence", 0.9))
    assert (kept.story, kept.fact, kept.role) == ("s1", "f1", "report")

    # 复核说是"后续进展"而它又是故事的根 => 这件事的新事实 (不合并进那个事实).
    story = grouping.decide(query, [fact], verdicts,
                            confirm=lambda a, b: grouping.Verdict("same_story", 0.8))
    assert (story.story, story.fact, story.role) == ("s1", "", "primary")

    # 复核说无关 => 自己起一件事.
    alone = grouping.decide(query, [fact], verdicts,
                            confirm=lambda a, b: grouping.Verdict("unrelated", 0.9))
    assert (alone.story, alone.fact) == ("", "")


def test_a_clear_development_starts_a_new_fact_inside_that_story():
    query = _candidate("q", "Nova publishes its post-mortem")
    root = _candidate("f1", "Nova bridge drained", story="s1", score=0.7)
    decision = grouping.decide(query, [root], {"f1": grouping.Verdict("same_story", 0.85)})
    assert (decision.story, decision.fact, decision.role, decision.relation) == (
        "s1", "", "primary", "same_story")


def test_nothing_close_means_a_story_of_its_own():
    query = _candidate("q", "A quiet launch nobody covered")
    assert grouping.decide(query, [], {}).story == ""
    assert grouping.decide(query, [], {}).relation == "unrelated"


def test_the_judge_numbers_the_candidates_and_maps_them_back(monkeypatch):
    """编排本身也要测: 材料里候选编号与答案编号是同一件事写在两处。"""
    seen: list[tuple[str, str]] = []

    def call(messages, **kw):
        seen.append((kw["purpose"], messages[1]["content"]))
        return {"query": "x", "decisions": [{"id": "C2", "relation": "same_story",
                                             "confidence": 0.8}]}

    monkeypatch.setattr(grouping.llm, "complete_json", call)
    query = _candidate("q", "Nova publishes its post-mortem")
    first = _candidate("f1", "Nova bridge drained")
    second = _candidate("f2", "Nova bridge TVL tops 900M")
    verdicts = grouping.judge(query, [first, second])

    assert seen == [(grouping.PURPOSE_GROUP, grouping.user_block(query, [first, second]))]
    assert "C2" in seen[0][1]
    assert verdicts["f2"].relation == "same_story"
    assert verdicts["f1"].relation == "unrelated"


def test_the_material_is_a_labelled_block_per_report():
    """AIHOT `describeReport` 的形状: 一栏一行、带字段名; 候选那侧多一格编号与篇数.

    形状本身是判据的一部分 —— 一边有「事实要素」一边没有, 判出来的关系就会跟着变, 所以两边
    走的是同一个 `describe`。
    """
    query = grouping.Candidate("q", "", "Nova bridge drained", "一句话摘要", "CoinDesk",
                               "2026-10-05T00:00:00Z", True, 0.0, first_party=True,
                               subject="Nova Bridge", action="drained",
                               object="1200 万美元", occurred_at="2026-10-04")
    candidate = grouping.Candidate("f:1", "s:1", "Old story", "", "The Block",
                                   "2026-10-03T00:00:00Z", True, 0.0, subject="Other",
                                   members=3, fact_title="Old story")

    block = grouping.user_block(query, [candidate])

    lines = block.split("\n")
    assert lines[0] == "【新报道】"
    assert lines[1] == "标题：Nova bridge drained"
    assert lines[2] == "来源：CoinDesk（当事方/官方）｜发布时间：2026-10-05T00:00:00Z"
    assert lines[3] == "摘要：一句话摘要"
    assert lines[4] == "事实要素：主体=Nova Bridge；动作=drained；对象=1200 万美元；日期=2026-10-04"
    assert "【候选 C1】（该事实已有 3 篇报道；事实标题：Old story）" in block
    # 候选没有日期就写「未知」, 没有摘要就写「（无）」—— 空着的格子要印出来, 缺格会改变判定.
    assert "来源：The Block｜发布时间：2026-10-03T00:00:00Z" in block
    assert "摘要：（无）" in block
    assert "日期=未知" in block
    assert block.rstrip().endswith("这篇新报道与每个候选的关系是什么？")


def test_the_summary_keeps_three_hundred_and_sixty_characters():
    """160 字常常刚好切在这件事到底怎么了之前, 而判定要的正是那半句 (AIHOT 的 `slice(0, 360)`)。"""
    long_summary = "甲" * 400
    text = grouping.describe(grouping.Candidate("f:1", "s:1", "T", long_summary,
                                                "CoinDesk", "2026-10-05T00:00:00Z"))
    assert "摘要：" + "甲" * grouping.SUMMARY_CHARS in text
    assert "甲" * (grouping.SUMMARY_CHARS + 1) not in text


def test_the_group_station_calls_the_model_twice_when_a_match_is_weak(monkeypatch):
    calls: list[str] = []

    def call(messages, **kw):
        calls.append(kw["purpose"])
        if kw["purpose"] == grouping.PURPOSE_GROUP:
            return {"query": "x", "decisions": [{"id": "C1", "relation": "same_occurrence",
                                                 "confidence": 0.9}]}
        return {"a": "x", "b": "y", "relation": "same_story", "difference": "later",
                "confidence": 0.8}

    monkeypatch.setattr(grouping.llm, "complete_json", call)
    query = _candidate("q", "Nova publishes its post-mortem")
    root = _candidate("f1", "Nova bridge draining halted", story="s1")
    # 把召回分钉在 0.5: 过得了召回线、够不到"不必复核"的 0.85, 所以第二趟该发生。
    decisions = grouping.group(query, [root], similarity=lambda a, b: 0.5)
    assert calls == [grouping.PURPOSE_GROUP, grouping.PURPOSE_GROUP_REVIEW]
    assert decisions[0].story == "s1"


def test_a_pool_with_nothing_close_never_asks_the_model(monkeypatch):
    def call(messages, **kw):   # pragma: no cover - 走到这里就是失败
        raise AssertionError("没有候选还去问模型")

    monkeypatch.setattr(grouping.llm, "complete_json", call)
    decisions = grouping.group(_candidate("q", "A quiet launch nobody covered"),
                               [_candidate("f1", "Binance lists Pulse")])
    assert decisions[0].relation == "unrelated"
