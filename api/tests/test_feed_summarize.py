"""便宜档分档 (AIHOT 的 summarize-{article,short-post,long-post,*quoted,empty}):
文章写摘要、短帖直接译、长帖写摘要，引用与空正文各带一段附则。

这一档按材料挑提示词, 但"要不要再问一遍模型"只有一个答案 —— 六个文件共用一个版本。另外两件事
在本地做完: 本来全是中文的短帖照抄即可 (verbatim), 摘要太长就地压短 (compact)。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.feed import writing
from app.feed.sources import snapshot
from app.feed.sources import store as source_store
from app.store import db

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
BUDGET = {"maxItems": 40, "concurrency": 2, "timeoutSeconds": 5,
          "retryHours": 24, "maxFailures": 3}
NAMES = {"rss:a": "Example Wire"}

ARTICLE_BODY = "<p>%s</p>" % ("The protocol shipped a new release with a longer body. " * 20)


@pytest.fixture(autouse=True)
def roster(isolated_db):
    source_store.import_roster()


@pytest.fixture(autouse=True)
def one_model_only(monkeypatch):
    """这一份只测便宜档的分派: 评分与结构两档按住, 门槛按住 (分级不在表里 = 不参与精选)."""
    monkeypatch.setattr(writing, "SCORE_CALLS", 0)
    monkeypatch.setattr(writing, "STRUCTURE_CALLS", 0)
    # 预筛档与这一份要测的东西无关: 按掉它, 免得每条夹具都要补一个预筛答案 (预筛自己见 test_feed_prefilter.py)。
    monkeypatch.setattr(writing, "PREFILTER_CALLS", 0)
    monkeypatch.setattr(writing, "tier_of", lambda row: "")


def _row(external_id: str, *, kind: str = "rss", source_id: str = "rss:a",
         title: str = "Nova bridge drained", body: str = ARTICLE_BODY,
         quoted: dict | None = None) -> dict:
    return {"source_id": source_id, "external_id": external_id, "source_kind": kind,
            "quoted": quoted or {}, "identity_key": "other|", "title": title,
            "url": "https://a/%s" % external_id, "author": "", "body": body,
            "media": [], "published_at": (NOW - timedelta(minutes=5)).isoformat()}


def _put(row: dict) -> None:
    """存一条夹具, 并把"原文页读过了、没读到"记上。

    写作站不打断还在等原文的那些 (AIHOT 的 `waitsForPage`): 一条只有标题、链接可以去读、
    又还没去读过的记录, 现在问它就是在标题上写摘要 —— 原文一到这一份就过期了。所以
    这些"只有标题"的夹具, 得先把"读过了、没读到"这一格 (`detail_at`) 记上, 否则它们要测的那一档
    根本不会被问到。实际跑起来时这一格由 `collect._fill_details` 写。
    """
    db.feed_items_put([row])
    db.feed_items_set_detail([{"source_id": row["source_id"], "external_id": row["external_id"]}],
                             tried_at=NOW.isoformat())


def _fake(monkeypatch, **answers):
    seen: list[str] = []
    systems: list[str] = []

    def call(messages, **kw):
        seen.append(kw["purpose"])
        systems.append(messages[0]["content"])
        answer = answers[kw["purpose"]]
        return answer(messages) if callable(answer) else answer

    monkeypatch.setattr(writing.llm, "complete_json", call)
    return seen, systems


def _written(external_id: str, source_id: str = "rss:a") -> dict:
    rows = snapshot.read_written_window(NOW, 1)
    assert (source_id, external_id) in rows, "这一行没有被模型写过"
    return rows[(source_id, external_id)]


def test_the_two_short_tweet_lines_are_two_different_rulers():
    """100/500 那把尺决定"走哪一份提示词", 60 那把决定"正文还值不值得译" —— 两件事。"""
    assert writing.is_short_tweet("gm") is True                     # 英文按 500 字算
    assert writing.short_post("gm") is True
    english = "word " * 20                                          # 100 字: 是短帖, 但够 60
    assert writing.is_short_tweet(english) is True
    assert writing.short_post(english) is False
    assert writing.is_short_tweet("字" * 150) is False              # 汉字多, 按 100 字算


def test_a_pure_chinese_short_tweet_needs_no_translation():
    assert writing.needs_short_translation("大家早上好，今天上线主网") is False
    assert writing.needs_short_translation("gm to everyone building") is True
    assert writing.needs_short_translation("这只是半句 Chinese mixed in") is True
    assert writing.looks_zh("中文") is True and writing.looks_zh("日本語のテキスト") is False


def test_the_prompt_is_picked_by_the_material_and_the_appendix_by_the_special_case():
    quoted = {"quoted_json": json.dumps({"handle": "someone", "text": "the real news"})}
    row = _row("s", kind="x", body="gm builders")
    row.update(quoted)
    short, system = writing.summarize_system(row, post=True, text="gm builders")
    assert short == "summarize-short-post.md" and "短推文" in system
    assert "引用式 teaser 例外" in system          # 有引用 → 附则跟上
    assert "不要扩写" in system

    long, system = writing.summarize_system(_row("l"), post=True, text="word " * 120)
    assert long == "summarize-long-post.md" and "长推文" in system
    assert "引用式 teaser 例外" not in system

    article, system = writing.summarize_system(_row("a"), post=False, text=ARTICLE_BODY)
    assert article == "summarize-article.md" and "没到理解档" in system
    assert "不要扩写" not in system

    _name, system = writing.summarize_system(_row("e"), post=False, text="")
    assert "写空字符串" in system


def test_a_long_summary_is_compacted_locally_not_asked_again(isolated_db, monkeypatch):
    pieces = ["Nova 跨链桥被攻击者一次性掏空，损失约 1200 万美元，这是今年第三起同类事件。",
              "攻击者利用了一个没有校验的跨链消息处理函数，把伪造的提款请求当成了合法请求。",
              "官方随后暂停了桥的出入金，并表示正在与审计方和交易所合作追踪资金流向。",
              "目前还没有给出赔付方案，也没有说明用户资产什么时候可以取回。",
              "社区里有用户开始怀疑项目方在上线前的审计报告里漏掉了这一条路径。",
              "链上数据显示攻击者的资金已经分批转入了两个混币服务地址。"]
    long_summary = "".join(pieces)
    seen, _ = _fake(monkeypatch, **{writing.PURPOSE_SUMMARIZE: {"title_zh": "Nova 桥被掏空",
                                                               "summary": long_summary}})
    db.feed_items_put([_row("a-1")])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    assert seen.count(writing.PURPOSE_SUMMARIZE) == 1     # 不再问一次模型
    row = _written("a-1")
    assert len(row["summary_llm"]) < len(long_summary)
    assert writing.answer_first_ok(row["summary_llm"], len(ARTICLE_BODY))

    short_ok = "第一句说明发生了什么，并且把关键结果写清楚让它可以被独立引用。第二句补一个可验证的数字，整段就超过五十个汉字了。"
    assert len(short_ok) >= 50 and writing.answer_first_ok(short_ok, 100) is True
    assert writing.answer_first_ok(long_summary, len(ARTICLE_BODY)) is False


def test_a_chinese_short_tweet_is_written_without_a_model_call(isolated_db, monkeypatch):
    """它自己就是稿子 (AIHOT 的 verbatim): 一个字都不用模型写, 也照样记版本, 不然每轮都还要问。"""
    tweet = "大家早上好，今天上线主网"
    seen, _ = _fake(monkeypatch, **{writing.PURPOSE_SUMMARIZE: {"title_zh": "不该被问到",
                                                               "summary": "不该被问到"}})
    db.feed_items_put([_row("t-1", kind="x", source_id="x:a", body=tweet)])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    assert writing.PURPOSE_SUMMARIZE not in seen
    row = _written("t-1", "x:a")
    assert row["title_zh"] == tweet and row["summary_llm"] == tweet
    assert writing.summarize_version() in (row["prompt_version"] or "")


def test_an_empty_body_is_not_written_up_from_the_title(isolated_db, monkeypatch):
    """正文短到没有可写的: 中文原标题照留, 不拿标题脑补一篇 (AIHOT 的 none)。"""
    seen, _ = _fake(monkeypatch, **{writing.PURPOSE_SUMMARIZE: {"title_zh": "不该被问到",
                                                               "summary": "不该被问到"}})
    _put(_row("a-2", body="", title="A quiet week"))
    writing.run(NOW, names=NAMES, budget=BUDGET)

    assert writing.PURPOSE_SUMMARIZE not in seen
    row = _written("a-2")
    assert row["title_zh"] == "" and row["summary_llm"] == ""
    assert row["llm_stage"] == "full"


def test_editing_a_summarize_prompt_or_the_identity_fragment_changes_the_version(tmp_path, monkeypatch):
    """一个版本说清这一档走的是哪一套字, 身份附则也算在内 (AIHOT 的 `PROMPT_VERSIONS.summarize`)。"""
    for name in writing.SUMMARIZE_PROMPTS + (writing.IDENTITY_PROMPT,):
        (tmp_path / name).write_text("body\n", encoding="utf-8")
    monkeypatch.setattr(writing, "PROMPT_DIR", tmp_path)
    before = writing.summarize_version()

    (tmp_path / "summarize-short-post-quoted.md").write_text("edited\n", encoding="utf-8")
    assert writing.summarize_version() != before
    assert writing.summarize_version().startswith("summarize@")

    (tmp_path / "summarize-short-post-quoted.md").write_text("body\n", encoding="utf-8")
    (tmp_path / writing.IDENTITY_PROMPT).write_text("edited\n", encoding="utf-8")
    assert writing.summarize_version() != before
