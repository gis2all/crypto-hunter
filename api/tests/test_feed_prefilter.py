"""预筛档 (AIHOT 的 `runSelectionPrefilter`): 三态标签、BLOCK 短路、UNKNOWN 放行。

这一档存在的理由是钱: 明显不属于本行业的材料不该走到评分与写作那几档去。所以这一份盯着三件 ——
BLOCK 之后一次调用都不再打; 材料只有标题时 BLOCK 不算数 (只当"等材料"); 判过的行不再排进预算.

模型是假的.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.feed import writing
from app.feed.sources import store as source_store
from app.store import db

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)

BUDGET = {"maxItems": 40, "concurrency": 2, "timeoutSeconds": 5,
          "retryHours": 24, "maxFailures": 3}

NAMES = {"rss:a": "Example Wire"}

BODY = "<p>Nova bridge drained for $12M.</p><p>More than 4,000 wallets were affected.</p>"

SUMMARIZED = {"title_zh": "Nova 桥被掏空", "summary": "Nova 桥被掏空 1200 万美元, 四千多个钱包受影响."}
PASSED = {"label": "pass", "reason": "跨链桥被盗"}
BLOCKED = {"label": "BLOCK", "reason": "只有普通科技"}


@pytest.fixture(autouse=True)
def roster(isolated_db):
    source_store.import_roster()


@pytest.fixture
def tier(monkeypatch):
    """把这条的源分级按住; 表外的分级不评分, 于是"分没拿到就走便宜档"这条路才走得到."""
    def use(name: str):
        monkeypatch.setattr(writing, "tier_of", lambda row: name)
    return use


@pytest.fixture(autouse=True)
def only_prefilter(monkeypatch):
    """这一份只测预筛: 评分与结构两档按掉, 免得每条夹具都要补它们的答案."""
    monkeypatch.setattr(writing, "SCORE_CALLS", 0)
    monkeypatch.setattr(writing, "STRUCTURE_CALLS", 0)


def _row(external_id: str = "a-1", *, body: str = BODY) -> dict:
    return {
        "source_id": "rss:a",
        "external_id": external_id,
        "source_kind": "rss",
        "quoted": {},
        "identity_key": "other|",
        "title": "Nova bridge drained",
        "url": "https://a/%s" % external_id,
        "author": "",
        "body": body,
        "media": [],
        "published_at": (NOW - timedelta(minutes=5)).isoformat(),
    }


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


def _fake(monkeypatch, answer, **others):
    """假模型: 记下每一次调用的用途与参数; 预筛那一问回 `answer`, 其余按用途翻牌子."""
    seen: list[tuple[str, dict]] = []

    def call(messages, **kw):
        seen.append((kw["purpose"], kw))
        value = others.get(kw["purpose"], answer)
        if isinstance(value, Exception):
            raise value
        return value(messages) if callable(value) else value

    monkeypatch.setattr(writing.llm, "complete_json", call)
    return seen


def _stored(external_id: str = "a-1") -> dict:
    rows = db.feed_items_for("other|")
    for row in rows:
        if row["external_id"] == external_id:
            return row
    raise AssertionError("这一行不在库里: %s" % external_id)


def test_a_block_stops_every_paid_stage(isolated_db, monkeypatch):
    """BLOCK 直接短路: 后面几档一次都不打, 而这一条停在终态 `blocked` —— 不再排进预算,
    页面上也不会出现 (没有中文标题摘要 = 不发布)."""
    seen = _fake(monkeypatch, BLOCKED)
    db.feed_items_put([_row()])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    assert [purpose for purpose, _ in seen] == [writing.PURPOSE_PREFILTER]
    row = _stored()
    assert row["relevance"] == "block"
    assert row["llm_stage"] == writing.STAGE_BLOCKED
    assert not (row["title_zh"] or "").strip()


def test_unknown_is_let_through(isolated_db, monkeypatch, tier):
    """UNKNOWN 放行 (它的材料已经到头了, 不该因为"判不出来"就丢掉)."""
    tier("T9")
    seen = _fake(monkeypatch, {"label": "UNKNOWN", "reason": "只有代词"},
                 **{writing.PURPOSE_SUMMARIZE: SUMMARIZED})
    db.feed_items_put([_row()])
    writing.run(NOW, names=NAMES, budget=BUDGET)
    assert _stored()["relevance"] == "unknown"


def test_a_block_without_evidence_only_waits(isolated_db, monkeypatch):
    """材料只有标题时 BLOCK 不算数: 那是"等材料"而不是"无关" (AIHOT 的 `missingEvidence`)."""
    _fake(monkeypatch, BLOCKED)
    _put(_row(body=""))
    writing.run(NOW, names=NAMES, budget=BUDGET)
    assert _stored()["relevance"] == "unknown"
    assert _stored()["llm_stage"] != writing.STAGE_BLOCKED


def test_a_pass_goes_on_to_the_cheap_tier(isolated_db, monkeypatch, tier):
    """PASS 之后照旧走便宜档: 预筛放行不等于精选, 而是"继续判"."""
    tier("T9")
    seen = _fake(monkeypatch, PASSED, **{writing.PURPOSE_SUMMARIZE: SUMMARIZED})
    db.feed_items_put([_row()])
    writing.run(NOW, names=NAMES, budget=BUDGET)
    purposes = [purpose for purpose, _ in seen]
    assert purposes[0] == writing.PURPOSE_PREFILTER
    assert writing.PURPOSE_SUMMARIZE in purposes
    assert _stored()["relevance"] == "pass"
    assert _stored()["title_zh"] == SUMMARIZED["title_zh"]


def test_the_prefilter_asks_at_temperature_zero_and_at_its_own_length(isolated_db, monkeypatch):
    """同一份材料两次预筛必须给同一个标签, 所以温度 0; 它只回一个词, 所以 512 上限够."""
    seen = _fake(monkeypatch, PASSED)
    db.feed_items_put([_row()])
    writing.run(NOW, names=NAMES, budget=BUDGET)
    purpose, kw = seen[0]
    assert purpose == writing.PURPOSE_PREFILTER
    assert kw["temperature"] == 0.0
    assert kw["max_tokens"] == writing.PREFILTER_MAX_TOKENS == 512


def test_a_label_outside_the_three_is_a_failed_stage(isolated_db, monkeypatch):
    """模型回一个表外的标签是格式错, 不是"大概相关": 这一档算失败, 下一轮再问."""
    _fake(monkeypatch, {"label": "MAYBE", "reason": "?"})
    db.feed_items_put([_row()])
    report = writing.run(NOW, names=NAMES, budget=BUDGET)
    row = _stored()
    assert row["llm_stage"] is None or row["llm_stage"] != writing.STAGE_BLOCKED
    assert not (row["relevance"] or "").strip()
    assert writing.version(writing.PREFILTER_PROMPT) not in (row["prompt_version"] or "")
    assert report.get("failed"), "这一条该被记成失败"


def test_a_blocked_row_is_never_queued_again(isolated_db, monkeypatch):
    """`blocked` 是终态: 预筛判过之后它不再排进预算 (不然每过 retryHours 就白问一次)."""
    _fake(monkeypatch, BLOCKED)
    db.feed_items_put([_row()])
    writing.run(NOW, names=NAMES, budget=BUDGET)
    pending = db.feed_items_pending_llm(
        (NOW - timedelta(hours=48)).isoformat(),
        versions=[writing.understand_version(), writing.summarize_version()])
    assert [row["external_id"] for row in pending] == []
