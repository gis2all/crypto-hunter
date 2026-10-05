"""`config/feed/pipeline.yaml` 和 信息流设定页: 一个文件, 两张脸。

这个文件是每一轮 tick 实际读的那一份 (出刊的时刻、补正文的预算、写作站的闸门), 页面只是它的
一张表。这些测试测的是那条缝: 页面画得出什么、一次保存写进去什么、写完那一刻什么在生效。
"""

from __future__ import annotations

import pytest

from app.api.v1 import forms
from app.feed.sources import pipeline

#: 一份操作员写的文件 —— 有注释, 有引号, 也有一格空着。
FILE = """# 信息流的全局旋钮。
detail:
  maxFetches: 12
schedule:
  # 日报几点出。
  day: "09:30"
  week: ""
"""


@pytest.fixture()
def feed_file(tmp_path, monkeypatch):
    """一份属于它自己的 `pipeline.yaml`: 页面和流水线都对着它, 真的那份不动。"""
    path = tmp_path / "pipeline.yaml"
    path.write_text(FILE, encoding="utf-8")
    monkeypatch.setattr(pipeline, "PATH", path)
    monkeypatch.setattr(forms, "FEED_PATH", path)
    return path


def _rows(blocks: list[dict]) -> dict[str, dict]:
    """页面上那几行, 按 key —— SPA 收到的东西, 摊平一层。"""
    return {f["key"]: f for block in blocks for group in block["groups"]
            for f in group["fields"]}


def _page(client) -> dict:
    answer = client.get("/api/v1/feed/settings")
    assert answer.status_code == 200, answer.text
    return answer.json()


def _save(client, patch: dict):
    return client.put("/api/v1/feed/settings", json={"patch": patch})


# ── 画的是文件 ──────────────────────────────────────────────────────────────────

def test_every_label_the_page_draws_names_a_key_the_pipeline_reads(feed_file):
    """一行写进文件、却没有一轮会读它, 就是一行假装设过了。"""
    assert set(forms.FEED_LABELS) == {key for key, _label, _kind, _unit in forms.FEED_FIELDS}
    rows = _rows(forms.feed_blocks())
    assert set(rows) == set(forms.FEED_LABELS)


def test_the_page_shows_the_file_and_falls_back_to_the_factory_value(feed_file):
    rows = _rows(forms.feed_blocks())
    assert rows["schedule.day"]["value"] == "09:30"       # 文件里写着的
    assert rows["schedule.week"]["value"] == ""           # 文件里空着的那一格
    assert rows["detail.maxFetches"]["value"] == "12"     # 文件盖过出厂值
    assert rows["detail.minBodyChars"]["value"] == "200"  # 文件没写的, 出厂值
    assert rows["llm.maxItems"]["value"] == "40"


def test_the_page_is_two_blocks_over_three_groups(feed_file):
    blocks = forms.feed_blocks()
    assert [b["key"] for b in blocks] == ["schedule", "budget"]
    assert [b["title"] for b in blocks] == ["出刊", "闸门"]
    assert [g["key"] for b in blocks for g in b["groups"]] == [
        "feed-schedule", "feed-preset", "feed-detail", "feed-llm"]


def test_a_clock_is_a_clock_and_a_count_is_a_count(feed_file):
    """写法决定页面上画成什么, 也决定保存时怎么校验 —— 两件事在同一个字段上。"""
    rows = _rows(forms.feed_blocks())
    assert rows["schedule.day"]["widget"] == "text"
    assert rows["llm.maxItems"]["widget"] == "num"
    assert rows["llm.maxItems"]["unit"] == "条"
    assert rows["schedule.day"].get("unit") is None


# ── 存的是文件 ──────────────────────────────────────────────────────────────────

def test_a_save_writes_the_file_and_answers_with_the_file(client, feed_file):
    answer = _save(client, {"schedule.day": "07:15", "llm.maxItems": "0"})
    assert answer.status_code == 200, answer.text
    rows = _rows(answer.json()["blocks"])
    assert rows["schedule.day"]["value"] == "07:15"
    assert rows["llm.maxItems"]["value"] == "0"
    # 写的是文件本身, 而文件还是那句注释的文件。
    text = feed_file.read_text(encoding="utf-8")
    assert "07:15" in text and "maxItems: 0" in text
    assert "# 日报几点出。" in text
    assert pipeline.schedule()["day"] == "07:15"
    assert pipeline.llm()["maxItems"] == 0


def test_clearing_a_clock_is_how_that_issue_stops_coming_out(client, feed_file):
    """出刊的开关就是那个时刻有没有 —— 没有第二个布尔, 也就不会互相矛盾。"""
    assert _save(client, {"schedule.week": "  "}).status_code == 200
    assert pipeline.schedule()["week"] == ""


def test_a_clock_written_loosely_is_written_back_as_a_clock(client, feed_file):
    assert _save(client, {"schedule.month": "9:05"}).status_code == 200
    assert pipeline.schedule()["month"] == "09:05"


def test_a_row_the_page_never_draws_is_refused_and_the_file_stands(client, feed_file):
    before = feed_file.read_text(encoding="utf-8")
    answer = _save(client, {"detail.nope": "1"})
    assert answer.status_code == 422
    assert feed_file.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("patch", [
    {"schedule.day": "8"},        # 少了一半的时刻
    {"schedule.day": "25:00"},    # 不是时刻
    {"llm.maxItems": "-1"},       # 不是一个数
    {"llm.maxItems": ""},         # 也不是一个数
])
def test_a_value_that_does_not_fit_its_row_is_refused(client, feed_file, patch):
    before = feed_file.read_text(encoding="utf-8")
    assert _save(client, patch).status_code == 422
    assert feed_file.read_text(encoding="utf-8") == before


def test_every_save_leaves_an_account(client, feed_file):
    from app.feed import audit

    assert _save(client, {"schedule.day": "07:15"}).status_code == 200
    rows = audit.recent(limit=10)
    assert any(row["action"] == "settings.save" and row["subject"] == "feed:schedule.day"
               for row in rows), rows


def test_the_preset_row_is_a_select_over_the_three_presets(feed_file):
    """档位那一行画成下拉, 三个选项与 pipeline.PRESETS 同一个词汇表 —— 页面只负责念名字."""
    row = _rows(forms.feed_blocks())["llm.preset"]
    assert row["widget"] == "select"
    assert [o["value"] for o in row["options"]] == ["saver", "standard", "full"]
    assert [o["label"] for o in row["options"]] == ["节省", "标准", "完整"]


def test_a_preset_outside_the_table_is_refused(client, feed_file):
    """表外的档位在保存那一步就被拒, 文件一个字不动 —— 先写坏再报错就是把文件留在没人验过的状态."""
    answer = _save(client, {"llm.preset": "frugal"})
    assert answer.status_code != 200
    assert "frugal" not in feed_file.read_text(encoding="utf-8")

