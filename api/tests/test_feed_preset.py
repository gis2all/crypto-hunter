"""档位: 一个下拉决定四个站跑哪几档.

用户定的三档 (2026-10-05): 节省 / 标准 / 完整.
这一份测的是那张表的三件事 —— 键缺席时是不是老行为、三档是不是单调的、以及那条最容易配错的
关系: 分组站的入场券是写作站写的中文标题, 所以「不开写作站却开分组站」是一张空头支票.
"""

from __future__ import annotations

import pytest

from app.feed import writing
from app.feed.sources import pipeline


def test_the_three_presets_are_the_ones_on_the_page():
    assert set(pipeline.PRESETS) == {"saver", "standard", "full"}


def test_a_missing_key_is_the_old_behaviour():
    """老的调用点自己传预算, 没有 preset 这一格 —— 它拿到的必须是全开, 不是新的默认."""
    assert pipeline.stages({"maxItems": 40}) == pipeline.PRESETS["full"]


def test_an_unknown_preset_is_refused():
    """一个拼错的档位要在读到它的那一刻炸掉, 而不是安静地按某一档跑一整轮."""
    with pytest.raises(pipeline.PipelineRefused):
        pipeline.stages({"preset": "frugal"})


def test_each_preset_only_turns_things_off():
    """三档是单调的: 越省的那一档能做的越少. 反过来的那一格就是配错了."""
    order = ["saver", "standard", "full"]
    for cheap, rich in zip(order, order[1:]):
        fewer, more = pipeline.PRESETS[cheap], pipeline.PRESETS[rich]
        for knob in ("prefilter", "structure", "score", "summarize"):
            assert fewer[knob] <= more[knob], (cheap, rich, knob)
        for knob in ("understand", "group", "digest"):
            assert (not fewer[knob]) or more[knob], (cheap, rich, knob)


def test_only_the_presets_that_write_titles_turn_grouping_on():
    """分组站只排「有中文标题」的行 (db.feed_items_for_grouping 的 WHERE), 而中文标题是写作站
    写的 —— 一个关掉写作站却开着分组站的档位, 每一轮都是空跑."""
    for name, stages in pipeline.PRESETS.items():
        if stages["group"] or stages["digest"]:
            assert stages["summarize"] > 0, name


def test_the_saver_preset_writes_no_titles_at_all(monkeypatch):
    """节省档一个字都不写, 所以页面那一侧的门槛 (writes) 必须跟着关 —— 否则整页会安静地空掉."""
    monkeypatch.setattr(writing, "enabled", lambda: True)
    monkeypatch.setattr(writing.pipeline, "stages",
                        lambda *a, **k: dict(pipeline.PRESETS["saver"]))
    assert writing.writes() is False


def test_the_standard_preset_still_writes(monkeypatch):
    monkeypatch.setattr(writing, "enabled", lambda: True)
    monkeypatch.setattr(writing.pipeline, "stages",
                        lambda *a, **k: dict(pipeline.PRESETS["standard"]))
    assert writing.writes() is True
