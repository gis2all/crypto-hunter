"""结构档 (AIHOT 的 structure): 类别、标签、主体、事实骨架.

它不写任何读者看得见的字, 所以单独一档跑 (与评分并列) —— 改一次标签词表不该把最贵的理解档整库
重跑。这一份盯着三件事: 白名单之外的标签进不来 (筛选栏筛不到的词等于一个静默的洞); 类别一定
落在词表里 (第一格反推不出来就用规则版兜底); 事实骨架与 subjects 只由它写, 理解档不再输出。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.feed import taxonomy, writing
from app.feed.sources import snapshot
from app.feed.sources import store as source_store
from app.store import db

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)

BUDGET = {"maxItems": 40, "concurrency": 2, "timeoutSeconds": 5,
          "retryHours": 24, "maxFailures": 3}

NAMES = {"rss:a": "Example Wire"}

#: 模型的答案: 分类标签写成同义词, 混进大小写不同的实体写法, 再夹一个词表里没有的.
STRUCTURED = {"category": "security",
              "tags": ["安全事件", "跨链桥", "ethereum", "\u4ee5\u592a\u574a", "\u4e0d\u5b58\u5728\u7684\u6807\u7b7e"],
              "subjects": ["ethereum", "made-up"],
              "fact": {"subject": "Nova Bridge", "action": "drained", "object": "$12M"}}
UNDERSTOOD = {"title_zh": "Nova 桥被掏空 1200 万美元", "summary": "四千多个钱包受影响.",
              "reason": "同类跨链桥通常会被一起重新定价."}


@pytest.fixture(autouse=True)
def roster(isolated_db):
    source_store.import_roster()


@pytest.fixture(autouse=True)
def no_scoring(monkeypatch):
    """评分档由 test_feed_selection.py 测; 这一份只测结构抽取."""
    monkeypatch.setattr(writing, "SCORE_CALLS", 0)
    # 预筛档与这一份要测的东西无关: 按掉它, 免得每条夹具都要补一个预筛答案 (预筛自己见 test_feed_prefilter.py)。
    monkeypatch.setattr(writing, "PREFILTER_CALLS", 0)


def _row(external_id: str = "a-1") -> dict:
    return {
        "source_id": "rss:a",
        "external_id": external_id,
        "source_kind": "rss",
        "quoted": {},
        "identity_key": "other|",
        "title": "Nova bridge drained",
        "url": "https://a/%s" % external_id,
        "author": "",
        "body": "",
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


def _fake(monkeypatch, **answers):
    seen: list[str] = []
    systems: list[str] = []

    def call(messages, **kw):
        seen.append(kw["purpose"])
        if kw["purpose"] == writing.PURPOSE_STRUCTURE:
            systems.append(messages[0]["content"])
        answer = answers[kw["purpose"]]
        return answer(messages) if callable(answer) else answer

    monkeypatch.setattr(writing.llm, "complete_json", call)
    return seen, systems


def _written(external_id: str = "a-1") -> dict:
    rows = snapshot.read_written_window(NOW, 1)
    assert ("rss:a", external_id) in rows, "这一行没有被模型写过"
    return rows[("rss:a", external_id)]


def test_normalizing_tags_keeps_only_the_vocabulary():
    """同义词先归一, 白名单外的丢掉, 大小写不敏感, 去重; 分类标签反推出类别 key."""
    norm = taxonomy.normalize_tags(["安全事件", "跨链桥", "ethereum", "\u4ee5\u592a\u574a",
                                    "\u4e0d\u5b58\u5728\u7684\u6807\u7b7e"])
    assert norm["category"] == "security"
    assert norm["tags"] == ["\u8de8\u94fe"]
    assert norm["entities"] == ["Ethereum"]


def test_the_category_falls_back_when_the_model_gives_none():
    """第一格不是分类标签时用规则版读出来的类别兜底 —— 不为这一格再问一次模型."""
    assert taxonomy.normalize_tags(["\u8de8\u94fe"], fallback="infra")["category"] == "infra"
    assert taxonomy.normalize_tags([], fallback="\u4e0d\u5b58\u5728")["category"] == taxonomy._FALLBACK


def test_the_structure_prompt_carries_the_vocabulary():
    """提示词里的类别、标签、实体三张名单来自 taxonomy.py, 不在提示词里再抄一份."""
    prompt = writing.structure_prompt()
    assert "- security\uff08\u5b89\u5168\uff09\uff1a" in prompt
    assert "ethereum\uff08ethereum/ether/\u4ee5\u592a\u574a\uff09" in prompt
    assert "{{" not in prompt


def test_the_structure_version_moves_with_the_vocabulary(monkeypatch):
    """版本 = 提示词 + 词表: 只哈希文件的话, 改了词表那些行不会重跑."""
    before = writing.structure_version()
    monkeypatch.setattr(taxonomy, "CATEGORY_TAGS",
                        list(taxonomy.CATEGORY_TAGS) + ["\u4e34\u65f6"])
    assert writing.structure_version() != before


def test_the_structure_tier_writes_category_tags_and_fact(isolated_db, monkeypatch):
    seen, systems = _fake(monkeypatch, **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD,
                                          writing.PURPOSE_STRUCTURE: STRUCTURED})
    _put(_row("a-1"))
    writing.run(NOW, names=NAMES, budget=BUDGET)

    row = _written()
    assert row["category_llm"] == "security"
    assert json.loads(row["tags_json"]) == {"tags": ["\u8de8\u94fe"], "entities": ["Ethereum"]}
    assert json.loads(row["subjects_json"]) == ["ethereum"]
    assert json.loads(row["fact_json"]) == {"subject": "Nova Bridge", "action": "drained",
                                            "object": "$12M"}
    assert writing.PURPOSE_STRUCTURE in seen and writing.PURPOSE_UNDERSTAND in seen
    # 请求带的是**渲染过**的那一份: 词表替身必须换掉, 否则模型看到的是一串 {{...}}.
    assert systems and "{{" not in systems[0] and "- security\uff08\u5b89\u5168\uff09" in systems[0]


def test_the_understanding_tier_no_longer_writes_the_fact(isolated_db, monkeypatch):
    """骨架与 subjects 只由结构档写: 两档都写会让"这一格按哪一档写的"没有答案."""
    monkeypatch.setattr(writing, "STRUCTURE_CALLS", 0)
    _fake(monkeypatch, **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD})
    _put(_row("a-1"))
    writing.run(NOW, names=NAMES, budget=BUDGET)

    row = _written()
    assert row["title_zh"] == UNDERSTOOD["title_zh"]
    assert not row["fact_json"] and not row["subjects_json"] and not row["category_llm"]
