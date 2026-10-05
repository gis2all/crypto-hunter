"""身份守卫 (AIHOT 的 identity-context + `enforceIdentity`): 标题与摘要里的主体必须材料里也出现过。

一条讲 A 交易所的公告被写成 B 交易所, 数字和时间都对, 只有主体错了 —— 这种错没人会专门去核。
守卫只做两件事: 标题退回原标题 (原标题是中文) 或留空, 摘要丢掉; 另外把材料里核过的发布主体
(文档发布域 / 来源账号) 附在摘要档的提示词后面, 让模型先知道该写谁。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.feed import identity, writing
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
def cheap_only(monkeypatch):
    """这份只测守卫: 评分/结构/预筛三档按住, 分级不在表里 (不参与精选)。"""
    monkeypatch.setattr(writing, "SCORE_CALLS", 0)
    monkeypatch.setattr(writing, "STRUCTURE_CALLS", 0)
    monkeypatch.setattr(writing, "PREFILTER_CALLS", 0)
    monkeypatch.setattr(writing, "tier_of", lambda row: "")


def _row(external_id: str, *, title: str = "Nova bridge drained", body: str = ARTICLE_BODY,
         url: str | None = None) -> dict:
    return {"source_id": "rss:a", "external_id": external_id, "source_kind": "rss",
            "quoted": {}, "identity_key": "other|", "title": title,
            "url": url or ("https://a/%s" % external_id), "author": "", "body": body,
            "media": [], "published_at": (NOW - timedelta(minutes=5)).isoformat()}


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


def _written(external_id: str) -> dict:
    rows = snapshot.read_written_window(NOW, 1)
    assert ("rss:a", external_id) in rows, "这一行没有被模型写过"
    return rows[("rss:a", external_id)]


def test_a_title_naming_a_company_the_material_never_mentions_falls_back_to_the_source_title():
    """模型写到材料里没有的主体: 标题退回原标题 (原标题是中文时), 摘要留着。"""
    ctx = identity.context(title="币安上线新币", text="币安官方公告")
    title, summary, guard = ctx.guard(
        title_zh="Coinbase 上线新币", summary="币安官方公告", original_title="币安上线新币")
    assert title == "币安上线新币"
    assert summary == "币安官方公告"
    assert guard == {"outcome": "fallback", "title": ["coinbase"], "summary": []}


def test_a_non_chinese_source_title_is_replaced_with_nothing():
    """原标题不是中文时没有可退的: 留空, 页面印规则版标题, 不印一个主体错的。"""
    ctx = identity.context(title="Binance lists a new token", text="Binance blog")
    title, _summary, _guard = ctx.guard(
        title_zh="OKX 上线新币", summary="", original_title="Binance lists a new token")
    assert title == ""


def test_a_summary_naming_an_unmentioned_company_is_dropped():
    ctx = identity.context(title="Binance lists USDC", text="Binance blog")
    title, summary, guard = ctx.guard(
        title_zh="Binance 上线新币", summary="Tether 发行新稳定币",
        original_title="Binance lists USDC")
    assert title == "Binance 上线新币"
    assert summary == ""
    assert guard["outcome"] == "fallback" and guard["summary"] == ["tether"]


def test_the_publishers_own_domain_counts_as_mentioned():
    """a 公司在自家域名上发的公告, 发布方就是它 —— 正文没提也算数 (AIHOT 的 `publisherEntityId`)。"""
    ctx = identity.context(title="A new stablecoin", url="https://www.circle.com/blog/launch")
    assert ctx.publisher == "circle"
    assert ctx.facts() == "文档发布域主体=Circle"
    _title, _summary, guard = ctx.guard(
        title_zh="Circle 发布新稳定币", summary="", original_title="A new stablecoin")
    assert guard["outcome"] == "pass"


def test_a_name_written_with_a_hyphen_still_counts():
    """"USDC 5" 与 "USDC-5" 是同一个 (AIHOT 的 `surfaceVariants`)。"""
    assert identity.match_ids(identity.variants(["USDC 5 is live"])) == ["circle"]


def test_a_name_nobody_knows_is_not_touched():
    """词典外的名字不碰 —— 少列一个只是少抓到一次错, 多列一个会把对的标题也退回去。"""
    ctx = identity.context(title="Some random project ships v2")
    assert ctx.allowed == ()
    assert ctx.guard(title_zh="Some random project 发布 v2", summary="",
                     original_title="Some random project ships v2")[2]["outcome"] == "pass"


def test_strip_echo_takes_prompt_lines_off_the_end_only():
    assert writing.strip_echo("第一段。\n来源：Example Wire") == "第一段。"
    assert writing.strip_echo("第一段。\n【已核验身份上下文】某公司") == "第一段。"
    assert writing.strip_echo("第一段。\n\n") == "第一段。"
    assert writing.strip_echo("第一段。\n第二段。") == "第一段。\n第二段。"


def test_the_guard_rewrites_what_gets_stored_and_records_the_fallback(isolated_db, monkeypatch):
    """走完整条路: 模型写了材料里没有的公司, 落库的是原标题与空摘要, 账目进 `identity_guard`。"""
    _fake(monkeypatch, **{writing.PURPOSE_SUMMARIZE: {
        "title_zh": "Coinbase 上线新币", "summary": "Tether 发行新稳定币"}})
    db.feed_items_put([_row("a-9", title="币安上线新币",
                            body="<p>%s</p>" % ("币安官方今日宣布上线一个新项目。" * 12))])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    row = _written("a-9")
    assert row["title_zh"] == "币安上线新币"
    assert row["summary_llm"] == ""
    assert json.loads(row["identity_guard"]) == {
        "outcome": "fallback", "title": ["coinbase"], "summary": ["tether"]}


def test_the_identity_fragment_reaches_the_summarize_prompt(isolated_db, monkeypatch):
    """核过的发布主体要出现在系统提示词里 (AIHOT 把它填进 `{{identity}}` 那一格)。"""
    _seen, systems = _fake(monkeypatch, **{writing.PURPOSE_SUMMARIZE: {
        "title_zh": "Circle 发布新稳定币", "summary": ""}})
    db.feed_items_put([_row("a-10", title="A new stablecoin", url="https://www.circle.com/blog/x")])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    assert any("【已核验身份上下文】文档发布域主体=Circle" in s for s in systems)


def test_a_row_that_passes_keeps_no_ledger(isolated_db, monkeypatch):
    """没退过的行 `identity_guard` 是空串 —— 旧记录要被清掉, 不是留着上一轮的。"""
    _fake(monkeypatch, **{writing.PURPOSE_SUMMARIZE: {
        "title_zh": "Binance 上线新币", "summary": "Binance 官方公告上线一个新项目。"}})
    db.feed_items_put([_row("a-11", title="Binance lists a new token",
                            body="<p>%s</p>" % ("Binance announced a new listing today. " * 20))])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    assert _written("a-11")["identity_guard"] == ""
