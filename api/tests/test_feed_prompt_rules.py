"""提示词片段: `{{> name}}` 展开、片段进版本、环与缺文件都报错.

共用规则只写一份 (AIHOT 的 `industry/prompts/rules-*.md` 与 `{{> file}}`): 几档都要的「不可信
数据」「防幻觉」「标题自洽」各写各的, 改一条就要记得改五处, 迟早有两处不一样 —— 而那一处不一样
是静默的, 它只体现在模型写出来的字上.
"""

from __future__ import annotations

import hashlib

import pytest

from app.feed import writing

PROMPT_NAMES = ("understand.md", "structure.md", "selection-score.md",
                "story-digest.md", "translate-body.md", "translate-post.md",
                "group-batch.md", "group-pair.md", "group-signal.md",
                "summarize-article.md", "summarize-short-post.md", "summarize-long-post.md")


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_every_prompt_expands_its_fragments_and_takes_the_safety_rule(name):
    # 词表替身对不带这些占位符的档是多余的, 对结构档与理解档是必需的 —— 缺值现在报错
    # (`read_prompt` 的契约), 所以统一把替身喂进去.
    text = writing.read_prompt(name, **writing._structure_fillings())
    assert "{{>" not in text          # 没有漏展开的片段
    assert "不可信数据" in text        # 每一档都收得到安全规则


def test_a_placeholder_without_a_value_is_an_error_not_a_blank():
    """缺值静默留白是最坏的一种: 模型照着 `{{topicTags}}` 答, 页面上谁也看不出这一档少了词表."""
    with pytest.raises(ValueError):
        writing.read_prompt("understand.md")


def test_editing_a_fragment_changes_the_version_of_every_prompt_that_uses_it(tmp_path, monkeypatch):
    """改共用规则一个字, 用到它的每一档都要重跑 —— 只哈希主文件的话, 那些按旧规则写过的行
    不会重跑, 而规则已经换了, 完全静默. """
    (tmp_path / "main.md").write_text("body\n\n{{> shared}}\n\nout\n", encoding="utf-8")
    (tmp_path / "shared.md").write_text("rule one\n", encoding="utf-8")
    (tmp_path / "plain.md").write_text("just text\n", encoding="utf-8")
    monkeypatch.setattr(writing, "PROMPT_DIR", tmp_path)

    before = writing.version("main.md")
    plain = writing.version("plain.md")
    assert "rule one" in writing.read_prompt("main.md")

    (tmp_path / "shared.md").write_text("rule two\n", encoding="utf-8")

    assert writing.version("main.md") != before
    assert writing.version("plain.md") == plain     # 没用到片段的, 版本逐字不变


def test_a_prompt_with_no_fragment_keeps_the_version_it_had_before(tmp_path, monkeypatch):
    """片段是加法: 一份没有片段的提示词不该因为换了写法就白花一轮模型钱. """
    (tmp_path / "plain.md").write_text("just text\n", encoding="utf-8")
    monkeypatch.setattr(writing, "PROMPT_DIR", tmp_path)
    want = "plain.md@" + hashlib.sha256("just text\n".encode("utf-8")).hexdigest()[:8]
    assert writing.version("plain.md") == want


def test_a_cycle_is_an_error_not_a_silent_blank(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("{{> b}}\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("{{> a}}\n", encoding="utf-8")
    monkeypatch.setattr(writing, "PROMPT_DIR", tmp_path)
    with pytest.raises(ValueError):
        writing.read_prompt("a.md")


def test_a_missing_fragment_is_an_error(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("{{> nowhere}}\n", encoding="utf-8")
    monkeypatch.setattr(writing, "PROMPT_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        writing.read_prompt("a.md")
