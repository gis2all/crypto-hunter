"""2026-10-05 那一批的三件: 事件页的官方报道分组 / 详情页的正文目录 / 报刊的要点与快讯.

三件各自只有一条会坏的承诺, 所以测的就是那一条:

* 官方报道 = **当事方自己**说的那些, 别家说的不在这里 (那边是 `other_reports`);
* 目录 = 锚点真的注进了正文, 而且译文那一边用的是同一套号 (对不上就整块不给);
* 报刊 = 一期的四块互斥 (要点不重印头条, 快讯不混进看点), 以及相邻的两期是后端查出来的
  —— 左栏一次只读 60 期, 读到边界时列表里没有下一期, 而后端知道还有.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.feed import htmlbody, reports, views
from app.feed.models import Discovery, Event, Item
from app.store import db

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _item(external_id: str = "a-1", *, hours: float = 2.0, first_party: bool = False,
          body_html: str = "", body_zh: str = "") -> Item:
    return Item(
        discovery=Discovery(source_kind="rss", source_id="rss:a", external_id=external_id,
                            title="Nova bridge drained", url="https://a/%s" % external_id,
                            published_at=_stamp(NOW - timedelta(hours=hours))),
        first_party=first_party, body_html=body_html, body_zh=body_zh)


def _event(items: list[Item] | None = None, *, key: str = "s:1", sources: int = 1,
           judgement: dict | None = None) -> Event:
    return Event(identity_key=key, kind="news", kind_label="新闻", title="Nova 桥被抽干",
                 url="https://a/1", first_seen_at=_stamp(NOW - timedelta(hours=3)),
                 last_seen_at=_stamp(NOW - timedelta(hours=1)), items=list(items or []),
                 sources=sources, judgement=judgement)


# ── 详情页的正文目录 (AIHOT 的 outline) ─────────────────────────────────────────

def test_a_heading_gets_an_anchor_and_a_line_in_the_outline() -> None:
    html, outline = htmlbody.with_outline("<h2>是什么</h2><p>甲</p><h3>怎么算</h3>")
    assert html == '<h2 id="sec-1">是什么</h2><p>甲</p><h3 id="sec-2">怎么算</h3>'
    assert outline == [{"id": "sec-1", "text": "是什么", "level": 2},
                       {"id": "sec-2", "text": "怎么算", "level": 3}]


def test_an_empty_heading_keeps_its_number_and_loses_its_line() -> None:
    """空标题不占目录, 号码却要照走 —— 号码错开一位, 目录点下去就落到别的节上."""
    html, outline = htmlbody.with_outline("<h2></h2><h2>有字</h2>")
    assert outline == [{"id": "sec-2", "text": "有字", "level": 2}]
    assert 'id="sec-1"' in html


def test_a_heading_below_h4_is_not_a_line_in_the_outline() -> None:
    _, outline = htmlbody.with_outline("<h2>甲</h2><h5>太深了</h5>")
    assert [row["id"] for row in outline] == ["sec-1"]


def test_the_outline_is_kept_off_the_payload_when_it_is_only_one_line() -> None:
    assert views.item_detail(_item(body_html="<h2>只有一节</h2>"), None, NOW)["outline"] == []


def test_a_detail_hands_back_an_outline_whose_anchors_are_in_the_body() -> None:
    payload = views.item_detail(_item(body_html="<h2>甲</h2><p>x</p><h2>乙</h2>"), None, NOW)
    assert [row["id"] for row in payload["outline"]] == ["sec-1", "sec-2"]
    assert 'id="sec-1"' in payload["body_html"] and 'id="sec-2"' in payload["body_html"]


def test_the_translated_body_carries_the_same_anchors() -> None:
    """译文换了字, `sec-2` 还是同一节 —— 切语言时目录不该跳错地方."""
    payload = views.item_detail(
        _item(body_html="<h2>What</h2><h2>How</h2>", body_zh="<h2>是什么</h2><h2>怎么算</h2>"),
        None, NOW)
    assert [row["text"] for row in payload["outline"]] == ["What", "How"]
    assert 'id="sec-2"' in payload["body_zh"]


def test_a_translation_that_lost_a_heading_drops_the_whole_outline() -> None:
    """指向不存在那一节的锚点, 比没有目录更糟 —— 对不上就整块不给."""
    payload = views.item_detail(
        _item(body_html="<h2>What</h2><h2>How</h2>", body_zh="<h2>是什么</h2>"), None, NOW)
    assert payload["outline"] == []


# ── 事件页的官方报道 (AIHOT 的 officialReports) ─────────────────────────────────

def test_official_reports_are_the_parties_own_words_newest_first() -> None:
    event = _event([_item("a-1", hours=3, first_party=True),
                    _item("a-2", hours=2),
                    _item("a-3", hours=1, first_party=True)])
    assert [row["id"] for row in views.official_reports(event)] == ["rss:a:a-3", "rss:a:a-1"]


def test_a_story_page_hands_the_official_ones_along_with_everything_else() -> None:
    event = _event([_item("a-1", hours=3, first_party=True), _item("a-2", hours=2)])
    payload = views.story_detail(event, 1, NOW)
    assert [row["id"] for row in payload["official_reports"]] == ["rss:a:a-1"]
    # 两边互补: 别家说的那条在"谁在说"那一列里, 不在官方那一列.
    assert [row["url"] for row in payload["items"]] == ["https://a/a-2", "https://a/a-1"]
    assert [row["first_party"] for row in payload["items"]] == [False, True]


# ── 报刊的要点与快讯 (AIHOT 的 highlights / flashes) ───────────────────────────

def test_highlights_are_the_fact_grade_entries_that_have_a_summary() -> None:
    entries = [_event(key="s:1", sources=2, judgement={"summary": "甲", "why": "…"}),
               _event(key="s:2", sources=1, judgement={"summary": "乙", "why": "…"}),
               _event(key="s:3", sources=2, judgement={"why": "…"})]
    assert [row["identity_key"] for row in reports.highlights(entries)] == ["s:1"]


def test_flashes_are_the_entries_with_nothing_to_expand() -> None:
    entries = [_event(key="s:1", sources=2, judgement={"summary": "甲", "why": "…"}),
               _event(key="s:2", sources=2, judgement={"summary": "乙"}),
               _event(key="s:3"),
               _event(key="s:4", judgement={"certainty": "fact"})]
    rows = reports.flashes(entries)
    assert [row["identity_key"] for row in rows] == ["s:3", "s:4"]
    assert rows[0]["last_seen_at"] == _stamp(NOW - timedelta(hours=1))


# ── 报刊的相邻两期 ────────────────────────────────────────────────────────────

def _edition_row(key: str, *, highlights: list[dict] | None = None,
                 flashes: list[dict] | None = None) -> dict:
    return {"id": "feed-day-%s" % key, "kind": "day", "key": key,
            "window_from": "%sT00:00:00+08:00" % key, "window_to": "%sT23:59:59+08:00" % key,
            "seen": 3, "entries": 0, "headline": "一期", "metrics_json": "[]",
            "payload_json": json.dumps({"lead": "导语", "entries": [],
                                        "highlights": highlights or [],
                                        "flashes": flashes or []}, ensure_ascii=False),
            "created_at": _stamp(NOW)}


def test_an_issue_points_at_the_ones_around_it(isolated_db) -> None:
    for key in ("2026-10-01", "2026-10-02", "2026-10-03"):
        db.feed_edition_put(_edition_row(key))
    assert db.feed_edition_neighbors("day", "2026-10-02") == {
        "prev": "feed-day-2026-10-01", "next": "feed-day-2026-10-03"}
    # 到头了就回空, 页面那一侧的按钮跟着不画.
    assert db.feed_edition_neighbors("day", "2026-10-03")["next"] is None


def test_the_edition_route_hands_back_highlights_flashes_and_neighbors(client) -> None:
    db.feed_edition_put(_edition_row("2026-10-01"))
    db.feed_edition_put(_edition_row(
        "2026-10-02",
        highlights=[{"identity_key": "s:1", "title": "甲", "summary": "一句话",
                     "sources": 3, "kind_label": "新闻"}],
        flashes=[{"identity_key": "s:2", "title": "乙", "last_seen_at": _stamp(NOW)}]))

    body = client.get("/api/v1/feed/editions/feed-day-2026-10-02").json()
    assert body["prev"] == "feed-day-2026-10-01" and body["next"] == ""
    assert [row["identity_key"] for row in body["highlights"]] == ["s:1"]
    assert [row["identity_key"] for row in body["flashes"]] == ["s:2"]


def test_an_older_issue_without_the_two_keys_still_reads(isolated_db) -> None:
    """老库里的一期没有这两格 —— 读出来是空表, 不是 KeyError."""
    row = _edition_row("2026-09-30")
    row["payload_json"] = json.dumps({"lead": "", "entries": []}, ensure_ascii=False)
    db.feed_edition_put(row)
    from app.api.v1.feed import _issue
    seen = _issue(db.feed_edition("feed-day-2026-09-30"))
    assert seen["highlights"] == [] and seen["flashes"] == [] and seen["prev"] == ""
