"""The feed: what is the same thing, what counts as today, and what got printed.

The two questions `materials` answers are the two everything else stands on, so most of this file
is about them. The rest is the one claim the design makes that is worth testing rather than
asserting: the reading pass sees only what was picked (service.py, DESIGN.md 4).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.feed import (analysis, editorial, events, heat, htmlbody, materials, reports, service,
                     taxonomy, views)
from app.feed.models import Discovery
from app.feed.sources import mock, registry
from app.feed.sources import collect as source_collect
from app.feed.sources import store as source_store
from app.store import db

#: A fixed instant. Every offset in the fixture is relative to whatever reads it, so a test that
#: could not pin the clock would pass before lunch and fail after it.
NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)

#: 前端那几张 mock 图 (web/public/feed-media). 它们随页面一起发出去, 所以"引用块旁边那张图在不在"
#: 是这一侧能测的: 路径打错一个字符, 页面上只是安静地少一张图, 没有任何一处会报错.
PUBLIC = Path(__file__).resolve().parents[2] / "web" / "public"

#: The operator's own list. `zephyr` is here because the fixture writes `$ZEPH` in one place and
#: "Zephyr" in the other - a ticker and a name that share no spelling, which is the case a
#: window's own alias table cannot learn (materials.learn_aliases).
NAMES = ("zephyr",)


def _seeded(records, suffix: str = ""):
    """一条记录在 `feed_items` 里的那一行。

    调用方先 `dedupe`: 同一个来源把同一篇重发时, `materials.dedupe` 留的是**第一次**, 而
    `feed_items_put` 的 upsert 留的是**最后一次**。不先去重, 表里的东西与 `mock.read` 的期望
    说的就不是同一件事 (窗口里 61 条对 79 条)。

    `suffix` 留给真时钟那一份: 主键是 (来源, 该来源自己的 id), 两份要并排待在表里, id 就得
    分得开。
    """
    return [
        {
            "source_id": item.source_id,
            "external_id": item.external_id + suffix,
            "source_kind": item.source_kind,
            "identity_key": materials.identity_key(item, ()),
            "title": item.title,
            "url": item.url,
            "author": item.author,
            "body": item.body,
            "media": item.media,
            "published_at": item.published_at,
        }
        for item in records
    ]


@pytest.fixture(autouse=True)
def fed(isolated_db):
    """把夹具写进快照表, 再让页面去读它。

    视图读的是 `feed_items` 而不是源 (DESIGN.md 8.1), 所以"预置一份内容"这件事现在是往那张
    表里写。这样测的正是生产走的那条路 —— 抓取与阅读之间那一层, 而不是它旁边的一条。

    出厂清单也一并导入: 名录 ("谁在说") 现在从 `feed_sources` 读, 而夹具里用到的
    `blog:base` / `x:funding-watch` 那几个正是清单里 `enabled: false` 的那些 —— 采集跳过它们,
    但它们仍然要在名录里答得出名字。
    """
    source_store.import_roster()
    records = materials.dedupe(mock.read(NOW))
    db.feed_items_put(_seeded(records))
    return records


@pytest.fixture
def live(isolated_db):
    """此刻那一份: 走真时钟的那几条 (`POST /feed/run` 与几个页面) 要读的内容。

    `fed` 把夹具钉在 `NOW` 上, 而一期是**读者的一天** —— 两个时钟差一天, 同一批记录落不到
    两边的同一个窗口里。这一份带着 `-now` 后缀铺进来, 与 `fed` 那一份并排, 谁也不盖谁。
    """
    db.feed_items_put(_seeded(mock.read(datetime.now(timezone.utc)), suffix="-now"))


def _discovery(**kw) -> Discovery:
    base = dict(source_kind="rss", source_id="rss:a", external_id="a-1",
                title="Nova bridge drained", url="https://a/1",
                published_at=(NOW - timedelta(minutes=10)).isoformat())
    base.update(kw)
    return Discovery(**base)


def test_the_fixture_holds_every_record():
    # 数出来而不是写死: 这条测的是"一条都没被丢掉、顺序也没变", 不是"fixture 恰好有 18 条".
    rows = json.loads(mock.FIXTURE.read_text(encoding="utf-8"))
    read = mock.read(NOW)
    assert len(read) == len(rows)
    assert [item.external_id for item in read] == [row["external_id"] for row in rows]


def test_the_window_is_half_open():
    start = NOW - timedelta(hours=12)
    assert materials.in_window(_discovery(published_at=start.isoformat()), start, NOW) is True
    assert materials.in_window(_discovery(published_at=NOW.isoformat()), start, NOW) is False


def test_a_reader_that_wrote_no_zone_is_read_as_utc():
    assert materials.moment("2026-10-03T09:00:00") == NOW
    assert materials.moment("2026-10-03T09:00:00Z") == NOW


def test_identity_ignores_the_wording():
    first = _discovery(title="Nova bridge drained for $12M")
    second = _discovery(title="Nova Protocol halts bridge after exploit", external_id="a-2")
    # The name is what has to match, not the sentence: one outlet calls it a drain, the next
    # calls it an exploit, and both are the same event.
    assert materials.identity_key(first, ("nova",)) == "hack|nova"
    assert materials.identity_key(second, ("nova",)) == "hack|nova"


def test_a_ticker_teaches_the_window_its_name():
    items = [
        _discovery(source_kind="x", source_id="x:a", external_id="x-1",
                   title="$NOVA 跨链桥被 exploit"),
        _discovery(source_id="rss:b", external_id="b-1", title="Nova bridge drained"),
    ]
    learned = materials.learn_aliases(items)
    assert "nova" in learned
    assert len(events.group(items, learned)) == 1


def test_a_party_is_matched_as_a_whole_word():
    assert materials.parties_of("Novation raises", ("nova",)) == ()
    assert materials.parties_of("Nova raises", ("nova",)) == ("nova",)


def test_one_party_written_two_ways_is_one_party():
    assert materials.parties_of("$ZEPH - Zephyr raises $40M", ("zeph", "zephyr")) == ("zephyr",)


def test_a_discovery_that_names_nobody_falls_back_to_its_wording():
    key = materials.identity_key(_discovery(title="Stablecoin bill advances in committee"))
    assert key == "text|stablecoin bill advances in committee"


def test_a_reread_is_not_a_second_record():
    again = _discovery(external_id="a-1", title="a different wording")
    assert len(materials.dedupe([_discovery(), again])) == 1


def test_a_wire_reprinted_counts_one_party():
    grouped = events.group([
        _discovery(source_id="rss:wire", external_id="w-1"),
        _discovery(source_id="rss:wire", external_id="w-2"),
        _discovery(source_id="rss:other", external_id="o-1"),
    ])
    assert len(grouped) == 1
    assert grouped[0].sources == 2


def test_heat_discounts_by_age():
    """过一天折一半: `heat.HALF_LIFE_HOURS = 24`, 与 AIHOT 的 hot.ts 同值."""
    event = events.group([_discovery()])[0]
    aged = heat.score(event, NOW + timedelta(hours=heat.HALF_LIFE_HOURS))
    assert heat.score(event, NOW) == pytest.approx(aged * 2, rel=1e-6)


def test_a_story_older_than_the_window_carries_no_heat():
    """窗口外的一条不算数 —— 没有这一条, 一件旧事会一直累加热度, 挤掉今天真在发生的事."""
    event = events.group([_discovery()])[0]
    assert heat.score(event, NOW + timedelta(hours=heat.WINDOW_HOURS)) == 0.0
    assert heat.score(event, NOW + timedelta(hours=heat.WINDOW_HOURS - 1)) > 0.0


def test_a_source_is_measured_by_its_latest_word_in_the_window():
    """问的是"这家现在还在说吗": 同一家在窗口里说过两次, 取新的那一次算热度."""
    event = events.group([
        _discovery(published_at=NOW.isoformat()),
        _discovery(external_id="a-2", published_at=(NOW + timedelta(hours=20)).isoformat()),
    ])[0]
    assert heat.value_at(event, NOW + timedelta(hours=20)) == pytest.approx(1.0)


def test_one_party_is_not_hot():
    """一家之言是通稿, 不是热点 (heat.MIN_PARTICIPANTS)."""
    one = events.group([_discovery()])[0]
    two = events.group([_discovery(), _discovery(source_id="rss:b", external_id="b-1")])[0]
    assert heat.eligible(one, NOW) is False
    assert heat.eligible(two, NOW) is True


def test_the_badges_read_the_same_numbers_the_ranking_does():
    fresh = events.group([_discovery()])[0]
    assert heat.badges(fresh, NOW) == ["new"]

    # 刚发生 (三家都在 6 小时内) 又被三家同时跟进: 三枚里前两枚各自成立, 一起印 ——
    # AIHOT 的 `if (surge) ... if (isNew) ...` 是两条独立的判断, 不是二选一.
    surge = events.group([_discovery(source_id="rss:a", external_id="a-1"),
                          _discovery(source_id="rss:b", external_id="b-1"),
                          _discovery(source_id="rss:c", external_id="c-1")])[0]
    assert heat.badges(surge, NOW) == ["surge", "new"]

    # 一条 30 小时前开的头、今天才被两家跟上来的事: 不算 new, 也不是 surge, 是升温.
    warming = events.group([
        _discovery(source_id="rss:a", external_id="a-1",
                   published_at=(NOW - timedelta(hours=30)).isoformat()),
        _discovery(source_id="rss:b", external_id="b-1",
                   published_at=(NOW - timedelta(hours=10)).isoformat()),
        _discovery(source_id="rss:c", external_id="c-1", published_at=NOW.isoformat()),
    ])[0]
    assert heat.badges(warming, NOW) == ["rising"]


def test_a_timestamp_from_the_future_is_not_extra_heat():
    # 未来时间戳不是"额外热度", 是**还没发生**: 一条时标跑到前面的记录在那一刻还没说过话,
    # 所以它一分也不加 (不是夹到 0 岁那样给满分 —— 夹出来的满分会让"把时钟拨快"变成一门生意,
    # 也会让 24 小时走势线上每一个历史点都变成今天的数).
    event = events.group([
        _discovery(published_at=(NOW + timedelta(hours=3)).isoformat())])[0]
    assert heat.score(event, NOW) == 0.0
    # 等那一刻真的到了, 它照常算一家来源 (那一瞬间还没开始衰减, 所以正好是 1).
    assert heat.value_at(event, NOW + timedelta(hours=3)) == 1.0


def test_a_clock_that_runs_ahead_is_not_believed():
    """源说的时间比发现还晚得多, 那条时间没有意义 —— 换成发现时刻 (AIHOT 的 FUTURE_TOLERANCE).

    一小时以内照实留着: 时区边界和发布延迟都会让源钟快一点, 那不是错, 是常态.
    """
    ahead = (NOW + timedelta(hours=3)).isoformat()
    assert (materials.decide_timeline(ahead, NOW).published_at
            == NOW.isoformat(timespec="seconds"))
    soon = (NOW + timedelta(minutes=20)).isoformat()
    assert materials.decide_timeline(soon, NOW).published_at == soon


def test_the_index_is_the_ten_times_value_with_one_decimal():
    """页面印的是 10 倍值、一位小数 (AIHOT 的 `heatIndex`: `round(heat*100)/10`).

    heat 1.0 = 一家满血来源, 印成 10.0. 写整数的 20 倍是另一套单位 —— 同一个热度两边不一样大.
    """
    assert heat.index(1.0) == 10.0
    assert heat.index(0.0) == 0.0
    assert heat.index(1.234) == 12.3
    event = events.group([_discovery()])[0]
    assert heat.index(heat.score(event, NOW)) == 10.0


def test_two_talking_sources_are_not_yet_a_hot_story():
    """两家都在聊、一家都没报: 有热度, 但还够不上热点榜 (AIHOT 的 `editorial>=1`).

    全是讨论帖凑出来的"两家", 是有人在聊, 不是有人在报 —— 页面上那栏叫"谁在说".
    """
    posts = events.group([_discovery(source_id="x:lookonchain", external_id="xa-1"),
                          _discovery(source_id="x:defillama", external_id="xb-1")])[0]
    assert heat.participants(posts, NOW) == 2
    assert heat.eligible(posts, NOW) is False

    with_report = events.group([_discovery(source_id="x:lookonchain", external_id="xa-1"),
                                _discovery(source_id="rss:theblock", external_id="w-1")])[0]
    assert heat.eligible(with_report, NOW) is True


def test_the_line_is_one_point_per_hour():
    """走势线 24 个点、每小时一个 (AIHOT 的 `SPARK_HOURS`), 最后一个点就是印的那个数."""
    event = events.group([_discovery()])[0]
    line = heat.series(event, NOW)
    assert len(line) == heat.SPARK_HOURS == 24
    assert line[-1] == heat.index(heat.score(event, NOW))


def test_the_trend_is_a_five_state_word_not_a_number():
    """五态 (AIHOT 的 `trend`): new / unknown / up / down / flat —— 页面翻成词, 不算阈值."""
    fresh = heat.row_at(events.group([_discovery()])[0], NOW)
    assert heat.trend_state(fresh, heat.trend_pct(fresh)) == "new"

    # 四态由两个数说清: 这个 6 小时涨跌多少. 直接摆数, 不经材料 —— 阈值改了一个人都看得见.
    assert heat.trend_state(heat.Row(heat=1.0, heat_prev=1.0), 0.0) == "flat"
    assert heat.trend_state(heat.Row(heat=2.0, heat_prev=1.0), 100.0) == "up"
    assert heat.trend_state(heat.Row(heat=0.5, heat_prev=1.0), -50.0) == "down"
    # 有旧的、可比的却是空的 (那一家刚接入): 算不出涨幅, 不是"没涨".
    assert heat.trend_state(heat.Row(heat=1.0, heat_prev=1.0, uncomparable=1), None) == "unknown"


def test_a_source_that_is_behind_leaves_the_comparison():
    """落后源的发言算进热度, 但不进涨幅那一比 (AIHOT 的 `heat_obs` / `behind_participants`).

    一个还没抓完的源, 它的"现在"本来就少几条 —— 拿它比今天和昨天, 冷暖是它自己造成的.
    """
    event = events.group([_discovery(source_id="rss:a", external_id="a-1"),
                          _discovery(source_id="rss:b", external_id="b-1")])[0]
    row = heat.row_at(event, NOW, behind={"rss:b"})
    assert row.participants == 2 and row.behind == 1
    assert row.uncomparable == 1
    # 只有一家可比: 涨幅比的是它自己, 不是把落后那家也算进来.
    assert row.heat_obs < row.heat
    caught_up = heat.row_at(event, NOW, behind=set())
    assert caught_up.uncomparable == 0 and caught_up.heat_obs == caught_up.heat


def test_a_record_found_two_days_after_it_was_written_is_history():
    """被翻出来的旧闻照常列出, 但不算今天的事 (AIHOT 的 `isHistorical`)."""
    assert materials.decide_timeline((NOW - timedelta(hours=60)).isoformat(), NOW).historical is True
    assert materials.decide_timeline((NOW - timedelta(hours=20)).isoformat(), NOW).historical is False
    # 钟快了一点的不是历史, 是钟错了.
    assert materials.decide_timeline((NOW + timedelta(days=3)).isoformat(), NOW).historical is False


def test_the_entrance_rewrites_an_unbelievable_timestamp():
    """入口只此一处: 采集站给每一条盖时间戳时顺手把不信的那些换掉 (collect._stamped)."""
    record = _discovery(published_at=(NOW + timedelta(days=2)).isoformat())
    assert (source_collect._stamped(record, NOW).published_at
            == NOW.isoformat(timespec="seconds"))
    # 没被改写的那条原样返回 (同一个对象, 不是白拷一份).
    kept = _discovery(published_at=(NOW - timedelta(hours=2)).isoformat())
    assert source_collect._stamped(kept, NOW) is kept


def test_a_reread_does_not_move_the_discovery_time(isolated_db, monkeypatch):
    """`seen_at` 是**第一次**读到它的时刻: 重读不该把它变成"刚发现".

    每次 upsert 都刷新的话, 一条老记录会越读越"新", `is_historical` 于是永远判不出历史.
    """
    row = {"source_id": "rss:a", "external_id": "x-1", "source_kind": "rss",
           "identity_key": "k", "title": "t", "url": "u",
           "published_at": (NOW - timedelta(hours=60)).isoformat()}
    monkeypatch.setattr(db, "now_iso", lambda: "2026-10-03T09:00:00+00:00")
    db.feed_items_put([row])
    monkeypatch.setattr(db, "now_iso", lambda: "2026-10-03T11:00:00+00:00")
    db.feed_items_put([row])
    assert db.feed_items_for("k")[0]["seen_at"] == "2026-10-03T09:00:00+00:00"


def test_history_never_joins_today_event(isolated_db, monkeypatch):
    """一条 60 小时前写的记录今天才被发现: 它照常列出来, 但不并进今天那件事.

    并进去了它就有了今天这件事的名分 —— 一家旧报道能把一件事顶成"两家在说".
    """
    monkeypatch.setattr(db, "now_iso", lambda: NOW.isoformat(timespec="seconds"))
    db.feed_items_put([
        {"source_id": "rss:a", "external_id": "old-1", "source_kind": "rss",
         "identity_key": "other|", "title": "Nova bridge drained", "url": "https://a/old",
         "published_at": (NOW - timedelta(hours=60)).isoformat()},
        {"source_id": "rss:b", "external_id": "new-1", "source_kind": "rss",
         "identity_key": "other|", "title": "Nova bridge drained", "url": "https://b/new",
         "published_at": (NOW - timedelta(hours=2)).isoformat()},
    ])
    items, stories = views.collect(NOW, 72.0)
    by_id = {item.discovery.external_id: item for item in items}
    assert {"old-1", "new-1"} <= set(by_id)
    story_of = {id(item): story for story in stories for item in story.items}
    # 旧闻照常列出来 (有它自己的一件事), 但绝不是今天那件事.
    assert id(by_id["old-1"]) in story_of
    assert story_of[id(by_id["new-1"])] is not story_of[id(by_id["old-1"])]


def test_two_parties_make_a_fact_and_one_makes_a_discussion():
    one = events.group([_discovery()])[0]
    two = events.group([_discovery(), _discovery(source_id="rss:b", external_id="b-1")])[0]
    assert editorial.certify(one) == "discussion"
    assert editorial.certify(two) == "fact"


def test_a_venue_speaking_for_itself_is_a_fact():
    assert editorial.certify(events.group([_discovery(source_kind="exchange")])[0]) == "fact"


def test_the_issue_is_cut_by_heat_and_only_what_it_prints_is_read():
    grouped = events.group([
        _discovery(source_id="rss:a", external_id="a-1", title="Nova bridge drained"),
        _discovery(source_id="rss:b", external_id="b-1", title="Kite partners with Visa"),
        _discovery(source_id="rss:c", external_id="c-1", title="Tether mints 1B on Tron"),
    ])
    grouped[0].heat, grouped[1].heat, grouped[2].heat = 1.0, 5.0, 2.0

    edition = reports.compose(kind="day", key="2026-10-03", window_from="f", window_to="t",
                              events=grouped, seen=3, per_edition=2)

    assert [entry.title for entry in edition.entries] == [
        "Kite partners with Visa", "Tether mints 1B on Tron"]
    assert edition.seen == 3
    # The one that was cut was never read. This is the whole of the design's fourth rule: the
    # only station that can cost money is handed a window that has already been ranked.
    assert grouped[0].judgement is None
    assert edition.entries[0].judgement["provider"] == "rules"
    # 一期的标题是排第一的那条, 报头的数是这一期自己的. 两者都在 compose 里算好, 因为档案栏
    # 只拿得到最近几十期, 从那里既读不出"这一期讲了什么", 也数不出"这是第几期".
    assert edition.headline == "Kite partners with Visa"
    assert [metric["key"] for metric in edition.metrics] == ["events", "sources", "seen", "minutes"]
    assert edition.metrics[0]["value"] == 2
    assert edition.metrics[2]["value"] == 3


def test_a_party_speaking_for_itself_is_counted_on_the_paper():
    """「件一手发布」在出刊那条路上也算得出来.

    一手是来源的属性, 所以它在建事件的时候就从名录填上; 只在阅读 pass 里填的话, 报头那个
    数永远是 0, 而 0 是不印的 —— 一个永远不出现的数, 比一个错的数更难发现.
    """
    grouped = events.group([_discovery(source_id="blog:base", external_id="b-1",
                                       title="Base mainnet upgrade is live")])
    assert grouped[0].items[0].first_party
    edition = reports.compose(kind="day", key="2026-10-03", window_from="f", window_to="t",
                              events=grouped, seen=1, per_edition=8)
    assert "first_party" in [metric["key"] for metric in edition.metrics]


def test_a_period_is_a_name_and_a_window_not_a_run():
    """Same day, different minutes, one name - and the three periods nest."""
    morning = datetime(2026, 10, 3, 1, 0, tzinfo=timezone.utc)
    evening = datetime(2026, 10, 3, 14, 48, tzinfo=timezone.utc)
    assert service.period("day", morning)[0] == service.period("day", evening)[0] == "2026-10-03"
    assert service.edition_id("day", service.period("day", evening)[0]) == "feed-day-2026-10-03"
    assert service.period("week", evening)[0] == "2026-W40"
    assert service.period("month", evening)[0] == "2026-10"
    # 半开区间首尾相接: 一天的窗口是 00:00 到次日 00:00 (+08), 不是"从此刻起 24 小时".
    key, start, end = service.period("day", evening)
    assert start.isoformat() == "2026-10-03T00:00:00+08:00"
    assert end.isoformat() == "2026-10-04T00:00:00+08:00"
    assert (end - start) == timedelta(days=1)
    # 周一是一周的第一天, 所以 10 月 3 日 (周六) 属于 9 月 28 日开始的那一周.
    assert service.period("week", evening)[1].isoformat() == "2026-09-28T00:00:00+08:00"


def test_an_unknown_period_is_refused_rather_than_guessed():
    with pytest.raises(ValueError):
        service.period("fortnight", NOW)


def test_one_pass_records_the_window_and_publishes_it(isolated_db):
    row = service.run_once(now=NOW, names=NAMES)
    # 窗口里的一切都写下来了, 而窗口外的一条也没写 —— 数出来, 不写死 (见 7.1 的缓存口径).
    _, start, end = service.period("day", NOW)
    inside = [item for item in materials.dedupe(mock.read(NOW))
              if materials.in_window(item, start, end)]
    assert row["seen"] == len(inside)
    assert row["entries"] == 8
    assert row["issue_number"] == 1
    assert len(db.feed_editions()) == 1


def test_the_same_window_run_twice_publishes_once(isolated_db):
    first = service.run_once(now=NOW, names=NAMES)
    second = service.run_once(now=NOW, names=NAMES)
    assert first["id"] == second["id"]
    # 同一期的期号也一样: 重跑还回库里那一行, 不是这次算出来的新版.
    assert first["issue_number"] == second["issue_number"] == 1
    assert len(db.feed_editions()) == 1


def test_each_period_is_its_own_issue_and_they_are_numbered_from_the_oldest(isolated_db):
    """三种刊各自成档, 期号从最早的那期数起 —— 而"第几期"是后端数出来的, 不是页面数的."""
    today = service.run_once(now=NOW, names=NAMES)
    yesterday = service.run_once(now=NOW - timedelta(days=1), names=NAMES)
    assert (today["id"], yesterday["id"]) == ("feed-day-2026-10-03", "feed-day-2026-10-02")
    # 期号是数出来的, 所以写进去的次序不影响它: 先写今天再补昨天, 读回来仍然是 1 / 2.
    assert [(row["key"], row["issue_number"]) for row in db.feed_editions(10, "day")] == [
        ("2026-10-03", 2), ("2026-10-02", 1)]
    # 另一份档: 周报的第一期就是第一期, 不会接着日报往下数.
    weekly = service.run_once(now=NOW, kind="week", names=NAMES)
    assert weekly["id"] == "feed-week-2026-W40"
    assert weekly["issue_number"] == 1
    assert [row["id"] for row in db.feed_editions(10, "week")] == ["feed-week-2026-W40"]


def test_an_issue_carries_the_raw_records_it_was_read_from(isolated_db):
    """`discoveries` 不是空的: 一期要印得出"四家独立来源"**是哪四家**."""
    row = service.run_once(now=NOW, names=NAMES)
    payload = json.loads(row["payload_json"])
    top = payload["entries"][0]
    assert [record["source_id"] for record in top["discoveries"]] == [
        "x:chain-watcher", "rss:theblock", "rss:thedefiant", "rss:coindesk"]
    # 只有正文在 payload 里: 标量都是列, 一件事只有一个地方记.
    # 导语 (5.7) 也在: 它与条目一样是版面的一部分, 而不是一个标量.
    # 要点与快讯 (2026-10-05) 同理 —— 它们是出刊那一刻对同一批条目的挑选, 跟着快照存.
    assert set(payload) == {"entries", "lead", "highlights", "flashes"}
    assert isinstance(payload["lead"], str)
    assert isinstance(payload["highlights"], list) and isinstance(payload["flashes"], list)


def test_the_window_cut_is_the_reader_s_job(isolated_db):
    """窗口外的记录在表里, 但不在这一期里。

    抓取那一层把看到的都写下来 —— 它不认识「日报」这回事, 切窗口发生在读书的那一站
    (DESIGN.md 8.1)。以前这条测的是「run_once 只写下窗口内的记录」, 那个动作现在归采集
    (sources/collect.py), 所以它守的边界跟着搬到这里: 表可以比窗口大, 一期不可以。
    """
    _, start, end = service.period("day", NOW)
    old = _discovery(source_id="rss:theblock", external_id="old-1",
                     title="A story from three days ago nobody reprinted",
                     published_at=(NOW - timedelta(days=3)).isoformat())
    db.feed_items_put(_seeded([old]))
    row = service.run_once(now=NOW, names=NAMES)
    assert db.feed_items_for(materials.identity_key(old, ())), "旧闻该在表里"
    inside = [item for item in materials.dedupe(mock.read(NOW) + [old])
              if materials.in_window(item, start, end)]
    assert row["seen"] == len(inside), "它不该被数进这一期"


def test_the_issue_points_back_at_the_records_it_was_written_from(isolated_db):
    service.run_once(now=NOW, names=NAMES)
    written = db.feed_items_for("hack|nova")
    assert [row["source_id"] for row in written] == [
        "x:chain-watcher", "rss:theblock", "rss:thedefiant", "rss:coindesk"]


def test_the_hottest_thing_in_the_window_is_the_hack(isolated_db):
    payload = json.loads(service.run_once(now=NOW, names=NAMES)["payload_json"])
    top = payload["entries"][0]
    assert top["identity_key"] == "hack|nova"
    assert top["sources"] == 4
    assert top["judgement"]["certainty"] == "fact"


def test_a_pass_can_be_run_from_the_page(client, live):
    created = client.post("/api/v1/feed/run")
    assert created.status_code == 201
    body = created.json()
    # 用的是真时钟, 所以只认形状, 不认今天是哪天.
    assert body["id"].startswith("feed-day-")
    assert body["kind"] == "day"
    assert body["key"]
    assert body["headline"]
    assert body["issue_number"] == 1
    assert body["entries"]
    assert client.get("/api/v1/feed/editions/latest").json()["id"] == body["id"]
    assert [row["id"] for row in client.get("/api/v1/feed/editions").json()] == [body["id"]]


def test_the_archive_lists_one_kind_at_a_time(client, live):
    """三份档案互不串门: 日报那一列里出现一段周报, 读者会以为周报也是一天一期.

    要 `live`: 夹具钉在 `NOW` 那一天, 而"这一周"是读者的一周 —— 周一换了一周之后, 钉住的那一批
    就不在这一期的窗口里了, 于是这一期没有头条可印 (archive 读的是真时钟).
    """
    client.post("/api/v1/feed/run")
    client.post("/api/v1/feed/run?kind=week")
    days = client.get("/api/v1/feed/editions").json()
    assert [row["kind"] for row in days] == ["day"]
    weeks = client.get("/api/v1/feed/editions?kind=week").json()
    # 真时钟: 认的是"这一期就是这一周", 不认写这条用例的那天是第几周 —— 写死的那一串会周一过期.
    assert [row["kind"] for row in weeks] == ["week"]
    assert weeks[0]["id"] == "feed-week-%s" % service.period("week", datetime.now().astimezone())[0]
    # 档案行画的是马克 + 标题, 所以这两个字段得在列表里而不只是详情里.
    assert weeks[0]["headline"]
    assert weeks[0]["issue_number"] == 1


def test_the_issue_list_carries_no_bodies(client, live):
    client.post("/api/v1/feed/run")
    row = client.get("/api/v1/feed/editions").json()[0]
    assert "payload_json" not in row
    assert row["entries"] >= 1


def test_there_is_no_latest_issue_before_anything_ran(client):
    assert client.get("/api/v1/feed/editions/latest").json() is None


def test_an_unknown_issue_is_404(client):
    assert client.get("/api/v1/feed/editions/nope").status_code == 404

def test_every_kind_the_vocabulary_knows_has_a_label():
    """A kind with no label is an event the page cannot name; a label with no kind is a word
    nothing will ever print. Both are caught here rather than on the page."""
    assert set(materials.KIND_LABELS) == {kind for kind, _words in materials.KINDS} | {""}


def test_an_event_carries_the_words_the_page_prints():
    """The page draws labels, it does not know them (CLAUDE.md 2.3.3) - so both travel in the
    payload: the kind with the event, the certainty with the reading."""
    event = events.group([_discovery()])[0]
    assert event.kind_label == materials.KIND_LABELS["hack"]
    assert editorial.read_one(event)["certainty_label"] == "讨论"


# ── 采集: 补时刻、认旧账


def _only(sid: str) -> None:
    """把这一轮的源收成一个。璇谁由表说，而测试要挑得准 —— 所以收在导入之后。"""
    conn = db.connect()
    try:
        conn.execute("UPDATE feed_sources SET enabled = 0")
        conn.execute("UPDATE feed_sources SET enabled = 1 WHERE id = ?", (sid,))
        conn.commit()
    finally:
        conn.close()


def test_editing_the_roster_makes_that_source_due_again(tmp_path, isolated_db):
    """清单里改过的那一行立刻到点，而账不动。

    改了配置却还要等上一次失败的退避走完，操作者只能看到旧的失败 —— 而一个「明明修好了」
    的源在页面上挂着一小时，比坏掉更让人不信任。
    """
    import yaml

    source_store.import_roster()
    later = (NOW + timedelta(minutes=90)).isoformat(timespec="seconds")
    db.feed_source_claim("cn:chaincatcher", later)
    db.feed_source_fail("cn:chaincatcher", "boom", later)

    source_store.import_roster()
    same = {row["id"]: row for row in db.feed_sources_all()}["cn:chaincatcher"]
    assert same["next_fetch_at"] == later        # 清单没动，退避照旧
    assert same["fail_count"] == 1

    before = {row["id"]: row for row in db.feed_sources_all()}
    document = yaml.safe_load(source_store.ROSTER_PATH.read_text(encoding="utf-8"))
    for row in document["sources"]:
        if row["id"] == "cn:chaincatcher":
            row["interval_minutes"] = 45
    edited = tmp_path / "sources.yaml"
    edited.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
                      encoding="utf-8")
    source_store.import_roster(edited)

    after = {row["id"]: row for row in db.feed_sources_all()}
    assert after["cn:chaincatcher"]["next_fetch_at"] is None     # 改了就立刻到点
    assert after["cn:chaincatcher"]["fail_count"] == 1           # 账还在
    assert after["rss:coindesk"]["next_fetch_at"] == before["rss:coindesk"]["next_fetch_at"]


def test_a_record_the_source_left_undated_is_stamped_when_first_seen(monkeypatch, isolated_db):
    """列表页天生不带日期，所以补时刻是采集这一层的活 (weblist.py 的整段说明)。

    `Discovery` 是 frozen 的，而每一个 `web_list` 源的每一条记录都走这条路 —— 直接给
    `published_at` 赋值会抛 FrozenInstanceError，于是四个中文源一条也进不来。
    """
    from app.feed.models import Discovery
    from app.feed.sources import collect

    records = [
        Discovery(source_kind="web_list", source_id="cn:chaincatcher", external_id="u-1",
                  title="一条没有日期的记录", url="https://example.com/a",
                  published_at=""),
        Discovery(source_kind="web_list", source_id="cn:chaincatcher", external_id="u-2",
                  title="一条有日期的记录", url="https://example.com/b",
                  published_at="2026-10-03T10:00:00+08:00"),
    ]
    monkeypatch.setattr(collect, "fetcher_for", lambda source: (lambda row, first: (records, {})))
    source_store.import_roster()
    _only("cn:chaincatcher")

    report = collect.poll(now=NOW, limit=1)
    assert report["ok"] == 1 and report["new"] == 2
    rows = {row["external_id"]: row
            for row in db.feed_items_since("1970-01-01T00:00:00+00:00")
            if row["source_id"] == "cn:chaincatcher"}
    # 没日期的按第一次见到它的时刻；有日期的原样留下，只是归一成 UTC。
    assert rows["u-1"]["published_at"] == NOW.isoformat(timespec="seconds")
    assert rows["u-2"]["published_at"] == "2026-10-03T02:00:00+00:00"


def test_the_same_list_read_twice_is_written_once(monkeypatch, isolated_db):
    """列表页每轮都把同一批文章送回来。不认这一点，一篇两周前的文章每轮都会盖上今天的日期，
    而它将永远看起来像刚发的 (DESIGN.md 8.5)。"""
    from app.feed.models import Discovery
    from app.feed.sources import collect

    records = [Discovery(source_kind="web_list", source_id="cn:chaincatcher", external_id="u-1",
                         title="同一条", url="https://example.com/a", published_at="")]
    monkeypatch.setattr(collect, "fetcher_for", lambda source: (lambda row, first: (records, {})))
    source_store.import_roster()
    _only("cn:chaincatcher")

    first = collect.poll(now=NOW, limit=1)
    again = collect.poll(now=NOW + timedelta(minutes=30), limit=1)
    assert first["new"] == 1 and again["new"] == 0
    written = [row for row in db.feed_items_since("1970-01-01T00:00:00+00:00")
               if row["source_id"] == "cn:chaincatcher"]
    assert len(written) == 1


# ── 采集的时钟: 一分钟一班, 坏了也不停


def test_a_broken_pass_does_not_take_the_clock_down(monkeypatch):
    """一次抓取炸了只记一条日志 (tick._once)。

    采集停在那里是这一层最坏的失败方式: 页面不会报错, 只是从此不再有新东西 —— 换来
    的是一个没有任何地方说明原因的「安静的旧」。所以这条要求的是「不抛」。
    """
    from app.feed import jobs
    from app.feed.sources import tick

    def boom(moment=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(jobs, "run_due", boom)
    asyncio.run(tick._once())


def test_the_clock_starts_once_and_stops_cleanly(isolated_db, monkeypatch):
    """起两次也只有一个时钟; 停得下来。重启不丢位置靠的是表, 不是这一层。"""
    from app.feed import jobs
    from app.feed.sources import tick

    def quiet(moment=None):
        return {"ran": {}, "failed": {}}

    monkeypatch.setattr(jobs, "run_due", quiet)
    monkeypatch.setattr(tick, "TICK_SECONDS", 3600)

    async def run() -> None:
        tick.start()
        first = tick._task
        assert first is not None and not first.done()
        tick.start()
        assert tick._task is first
        await tick.stop()
        assert tick._task is None

    asyncio.run(run())


# ── 分析层: 一条记录读成一个条目 ────────────────────────────────────────────────


def test_the_taxonomy_has_exactly_one_fallback():
    # 兜底那一类必须存在且只有一个: 归不到别处的东西要有地方去, 而两处兜底就是两套口径.
    for entry in taxonomy.CATEGORIES:
        assert entry["label"] and entry["section"]
    assert sum(1 for entry in taxonomy.CATEGORIES if entry.get("fallback")) == 1


def test_a_bridge_exploit_is_a_security_event_and_not_infrastructure():
    # "bridge" 同时出现在 infra 的词表里, 但一条讲桥被攻击的东西不是基建新闻 ——
    # 所以关键词表的顺序就是优先级, 这条测的就是那个顺序还在.
    read = taxonomy.classify("Nova bridge drained for $12M after an exploit")
    assert read["category"] == "security"


def test_a_record_nobody_can_place_falls_back_rather_than_guessing():
    assert taxonomy.classify("A quiet week for the newsletter")["category"] == "industry"


def test_every_source_in_the_fixture_is_in_the_registry():
    # 名录里查不到就是采集层写了个没人登记的名字: 那是数据错误, 不该靠猜一个默认值盖过去.
    for item in mock.read(NOW):
        assert item.source_id in registry.SOURCES, item.source_id


def test_the_score_stays_in_range_and_reads_who_is_speaking():
    title = "Base mainnet upgrade is live"
    official = analysis.read(_discovery(source_id="blog:base", title=title), NOW)
    wire = analysis.read(_discovery(source_id="rss:theblock", title=title), NOW)
    assert 0 <= official.score <= 100 and 0 <= wire.score <= 100
    assert official.first_party and not wire.first_party
    # 同样一句话, 当事方自己说和媒体转述不是一个分量.
    assert official.score > wire.score


def test_a_fresh_record_outweighs_the_same_record_two_days_old():
    fresh = analysis.read(_discovery(
        source_id="rss:theblock",
        published_at=(NOW - timedelta(hours=1)).isoformat()), NOW)
    stale = analysis.read(_discovery(
        source_id="rss:theblock",
        published_at=(NOW - timedelta(hours=40)).isoformat()), NOW)
    assert fresh.score > stale.score


# ── 三个视图: 同一批条目的三种问法 ─────────────────────────────────────────────


def test_curated_is_a_subset_of_all_and_every_row_cleared_the_bar():
    everything = views.items_view(NOW, scope="all")
    picked = views.items_view(NOW, scope="curated")
    assert 0 < picked["shown"] < everything["shown"]
    assert all(row["score"] >= views.CURATED_MIN_SCORE
               for day in picked["days"] for row in day["items"])
    ids = {row["id"] for day in everything["days"] for row in day["items"]}
    assert {row["id"] for day in picked["days"] for row in day["items"]} <= ids


def test_a_list_row_carries_its_pictures_and_the_other_reports():
    """图和"另有 N 家信源报道"随行下来 (items_view), 不留给页面去查.

    两样都是**可以没有的** —— 大多数条目两样都没有, 而那正是页面上什么都不画的意思. 所以这里
    先要求"两样都在字段里", 再要求"窗口里确实各有一条有", 否则一个永远返回空列表的实现也能过.
    折叠行里那几样缺一不可: 那一条自己的 id、来源名、它自己的标题、去哪看 (views.other_reports).
    """
    rows = [row for day in views.items_view(NOW, scope="all")["days"] for row in day["items"]]
    assert rows
    assert all(isinstance(row["media"], list) and isinstance(row["other_reports"], list)
               for row in rows)

    with_media = [row for row in rows if row["media"]]
    with_others = [row for row in rows if row["other_reports"]]
    assert with_media, "窗口里该有带图的条目 (mock 里那几条 X)"
    assert with_others, "窗口里该有同一件事被别家也说过的条目"
    for row in with_others:
        # 计数是**信源家数**, 不是列表长度: 列表截断到 5 条, 拿长度去印会在截断之后撒谎.
        assert row["other_report_count"] == min(len(row["other_reports"]),
                                                views.OTHER_REPORTS_LIMIT) <= views.OTHER_REPORTS_LIMIT
        # 自己不算"别家": 这一条自己的来源不该出现在折叠行里
        assert all(other["source_name"] != row["source_name"] for other in row["other_reports"])
        for other in row["other_reports"]:
            assert other["id"] and other["source_name"] and other["title"] and other["url"]

    shipped = {"/" + str(path.relative_to(PUBLIC)).replace("\\", "/")
               for path in PUBLIC.rglob("*") if path.is_file()}
    for row in with_media:
        assert set(row["media"]) <= shipped, "图得真的在 web/public 里"


def test_the_list_is_grouped_by_day_newest_first():
    view = views.items_view(NOW, scope="all")
    dates = [day["date"] for day in view["days"]]
    assert dates == sorted(dates, reverse=True)
    for day in view["days"]:
        assert day["count"] == len(day["items"])
        assert day["date_label"] and day["weekday_label"]


def test_a_category_filter_narrows_the_list_without_blanking_the_counts():
    full = views.items_view(NOW, scope="all")
    one = views.items_view(NOW, scope="all", category="security")
    assert 0 < one["shown"] < full["shown"]
    assert all(row["category"] == "security"
               for day in one["days"] for row in day["items"])
    # 计数数在筛选之前: 点进一个类别以后, 其它类别的计数还在, 读者才有回去的路.
    assert [row["count"] for row in one["categories"]] == \
           [row["count"] for row in full["categories"]]


def test_the_hot_list_is_ranked_by_heat_and_carries_its_own_line():
    hot = views.hot_view(NOW)
    heats = [story["heat_index"] for story in hot["stories"]]
    assert heats == sorted(heats, reverse=True)
    for story in hot["stories"]:
        assert story["sources"] >= 1
        assert story["items"], story["id"]
        assert len(story["series"]) == heat.SPARK_HOURS
        # 走势线的最新一个点就是印在上面的那个数: 两处不一致的图比没有图更糟.
        assert story["series"][-1] == story["heat_index"]


def test_a_story_is_its_items_and_the_line_points_back_at_who_said_it():
    hot = views.hot_view(NOW)
    top = hot["stories"][0]
    # 「谁在说」那一列印的是**发布来源** (views.py 的 `source_count` = `participants_editorial`):
    # 讨论帖只投热度那一票、不印成正文 (`evidence`), 所以它们算在 `sources` 里却不在这列里 ——
    # 要对的是 `source_count` 那一格, 不是 `sources`.
    assert top["source_count"] == len({row["source_name"] for row in top["items"]})
    assert top["sources"] >= top["source_count"]
    assert top["latest"]


def test_every_entity_group_names_an_entity_the_vocabulary_knows():
    """分组是"摆放", 不是第二份名录: 名单里打错一个名字, 那个实体会静默地不上墙 —— 而
    ENTITY_TAGS 那边也不会知道少了一个人. 两个词表的一致性只能在这儿盯."""
    known = [name for name, _words in taxonomy.ENTITY_TAGS]
    grouped = [name for group in taxonomy.ENTITY_GROUPS for name in group["members"]]
    assert len(grouped) == len(set(grouped)), "一个实体只能出现在一组里"
    assert set(grouped) == set(known)
    for group in taxonomy.ENTITY_GROUPS:
        assert group["key"] and group["name"] and group["members"]


def test_the_topic_window_is_longer_than_the_stream_and_the_board():
    """主题要的是"不动的那批也算数", 与热点榜那个短窗口正好相反 —— 所以它是自己的一个数,
    不是从上面两个里挑一个. 窗口的长短还随回答一起发下去, 页面不写死 30."""
    assert views.TOPIC_HOURS > views.STREAM_HOURS > views.HOT_HOURS
    assert views.topics_view(NOW)["window_days"] == views.TOPIC_HOURS / 24


def test_a_topic_row_counts_the_same_items_the_line_lists():
    """左栏那个数与中栏那一批必须是同一个窗口、同一次读 —— 两处都走 TOPIC_HOURS.
    左栏写 10、中栏只列出 9 条, 读者会以为丢了一条, 而其实是他看的那两处口径不一样."""
    directory = views.topics_view(NOW)
    named = [row for group in directory["groups"] for row in group["rows"] if row["count"] > 0]
    assert named, "窗口里应该至少有一条实体命中"
    for row in named:
        line = views.topic_items_view(NOW, row["id"])
        assert line["shown"] == row["count"], row["id"]
        assert line["shown"] == sum(day["count"] for day in line["days"])
        for day in line["days"]:
            assert day["count"] == len(day["items"])


def test_the_topic_directory_answers_from_the_page(client):
    body = client.get("/api/v1/feed/topics").json()
    assert body["window_days"] == views.TOPIC_HOURS / 24
    assert [group["key"] for group in body["groups"]] == \
           [group["key"] for group in taxonomy.ENTITY_GROUPS]
    assert all(row["name"] and "count" in row
               for group in body["groups"] for row in group["rows"])


def test_a_line_the_vocabulary_does_not_know_reads_empty_rather_than_missing(client):
    """名册之外的名字回一个空窗口而不是 404: 读者手上那个连接可能是在更早一份词表上点出来的,
    而"这条线现在没东西"与"这条线不存在"在页面上长得一样 (都是一句空的提示)."""
    answer = client.get("/api/v1/feed/topics/items", params={"topic": "No Such Entity"})
    assert answer.status_code == 200
    assert answer.json()["shown"] == 0 and answer.json()["days"] == []


# ── 路由 ───────────────────────────────────────────────────────────────────────


def test_the_three_views_answer_from_the_page(client):
    bar = client.get("/api/v1/feed/taxonomy").json()
    assert bar["categories"] and bar["categories"][0]["key"] == "protocol"

    everything = client.get("/api/v1/feed/items", params={"scope": "all"}).json()
    assert everything["scope"] == "all" and everything["shown"] > 0

    picked = client.get("/api/v1/feed/items", params={"scope": "curated"}).json()
    assert 0 < picked["shown"] < everything["shown"]

    hot = client.get("/api/v1/feed/hot").json()
    assert hot["stories"] and hot["total"] >= len(hot["stories"])


def test_a_view_that_does_not_exist_is_refused(client):
    assert client.get("/api/v1/feed/items", params={"scope": "whatever"}).status_code == 422


def test_a_detail_page_is_the_list_row_deepened(client):
    """详情页按 id 取, 不是让列表把那一行原样传下来: 它印的东西列表里没有 (api/v1/feed.py).

    所以这一条测的是那个承诺本身: 同一个 id 拿到的标题与分数与列表那一行一致 (它是那一行的
    加深, 不是另一条), 而正文那两样只有详情页才有 —— 有一笔记录带着正文, 就有一页带着块
    (`body_html`); 没有的就没有, 不编 (analysis.article).
    """
    everything = client.get("/api/v1/feed/items", params={"scope": "all"}).json()
    rows = [row for day in everything["days"] for row in day["items"]]
    assert rows
    deepened = 0
    for row in rows:
        page = client.get("/api/v1/feed/item", params={"id": row["id"]}).json()
        assert page["id"] == row["id"]
        assert page["title"] == row["title"]
        assert page["score"] == row["score"]
        assert page["summary"]
        assert page["domain"] and page["published_label"] and page["published_ago"]
        assert page["story_id"] == row["story_id"]
        # 写作站关掉的那一档 (测试里 maxItems 是 0): 没有译文, 页面上只有原文那一份.
        assert page["body_zh"] == "" and page["body_complete"] is False
        if page["body_html"]:
            deepened += 1
            assert htmlbody.blocks(page["body_html"])
    assert deepened > 0, "窗口里应该至少有一条带着正文"


def test_a_detail_page_carries_the_same_pictures_and_other_reports(client):
    """详情页是从列表那一行长出来的 (item_detail), 所以图和"别家报道"是同一份, 不是各算一遍.

    两处各写一套的话, 迟早有一边多一个字段, 而读的人不知道哪边是对的.
    """
    everything = client.get("/api/v1/feed/items", params={"scope": "all"}).json()
    rows = {row["id"]: row for day in everything["days"] for row in day["items"]}
    for row_id, row in rows.items():
        page = client.get("/api/v1/feed/item", params={"id": row_id}).json()
        assert page["media"] == row["media"]
        assert page["other_reports"] == row["other_reports"]
        assert page["other_report_count"] == row["other_report_count"]


def test_a_lone_report_prints_no_event_box():
    """一件事只有它自己那一条, 详情页不画「所属事件」(views.item_detail)。

    桶里只有它自己的时候, 那个方块印的就是本文标题重印一遍 —— 读到的是一件「只属于自己」
    的事。AIHOT 的详情页同理: 后续列表先把自己滤掉, 空了整块不画。别家报道那一行是另一条
    规矩, 由 other_report_count 管 (DESIGN.md 4.9)。
    """
    solo = events.group([_discovery()])[0]
    assert views.item_detail(solo.items[0], solo, NOW)["story_title"] == ""
    # 没所属事件那一份本来就是空串, 不因为这条改动变样.
    assert views.item_detail(solo.items[0], None, NOW)["story_title"] == ""

    # 同一件事被重发一次 (同一个桶里两条) -> 事件方块回来, 印的是这件事的标题.
    pair = events.group([_discovery(), _discovery(external_id="a-2")])[0]
    assert len(pair.items) == 2
    assert views.item_detail(pair.items[0], pair, NOW)["story_title"] == "Nova bridge drained"


def test_a_record_without_a_body_pages_short_rather_than_invented():
    """一条只有一句话的记录得到的是一页短的, 不是三段空话 (analysis.article).

    规则版的摘要是"正文的第一段"这件事只剩下兜底一个用处: 写出来的那一条不印它.
    """
    page = analysis.article(_discovery(body="First line.\n\nSecond line."))
    assert page["summary"] == "First line."
    assert page["body_html"] == "<p>First line.</p><p>Second line.</p>"

    empty = analysis.article(_discovery(body=""))
    assert empty["body_html"] == ""
    assert empty["summary"] == "Nova bridge drained"   # 没有正文就拿标题兜底, 不编一段

    # 库里早先落下的纯文本正文 (采集层改成块之前的那一批) 也画得出来: 空行分段补成块.
    old = analysis.article(_discovery(body="一。\n\n二。"))
    assert old["body_html"] == "<p>一。</p><p>二。</p>"


def test_a_story_detail_carries_its_rank_and_its_own_timeline(client):
    """一件事那一页: 名次由榜单给, 时间线是同一批报道按天的分组 —— 两处条数必须对得上."""
    hot = client.get("/api/v1/feed/hot").json()["stories"]
    top = hot[0]
    page = client.get("/api/v1/feed/story", params={"id": top["id"]}).json()
    assert page["rank"] == 1
    assert page["badges"] and page["summary"]
    assert page["first_seen_ago"] and page["last_seen_ago"]
    assert page["days"], page["id"]
    rows = [row for day in page["days"] for row in day["items"]]
    assert len(rows) == sum(day["count"] for day in page["days"])
    # 卡上那份"谁在说"与时间线上那批报道是同一批人, 只是投影不同.
    assert {row["source_name"] for row in rows} == {row["source_name"] for row in top["items"]}
    # 时间线上每一行都点得进自己的详情页.
    one = client.get("/api/v1/feed/item", params={"id": rows[0]["id"]}).json()
    assert one["title"] == rows[0]["title"]


def test_a_story_off_the_board_is_not_found_rather_than_ranked():
    """名次是榜单给的: 不在榜上就没有名次, 所以是 None (路由答案是 404), 不是 0 名."""
    assert views.find_story(NOW, "not-a-story") is None
    assert views.find_item(NOW, "rss:nowhere:x") is None


def test_an_unknown_detail_id_is_404(client):
    assert client.get("/api/v1/feed/item", params={"id": "rss:nowhere:x"}).status_code == 404
    assert client.get("/api/v1/feed/story", params={"id": "nowhere"}).status_code == 404

# ── 补正文: 列表页只给链接, 正文要另外去读 (DESIGN.md 8.5)

#: 一段够长的正文。`detail._MIN_BODY` 是两百字 (AIHOT 的 `MIN_BODY_CHARS`), 少于那个数就
#: 认输 —— 所以测试要的是一张**像文章**的页面, 不是一张像菜单的。三段各写一百来字, 加起来
#: 明显过线 (留出余量: 判的是正文文字数, 不是这一段字面长度)。
_LONG = (
    "这一段话说得足够长，长到能被当成一段正经的正文来读，而不是一个菜单项或者一个按钮，也不是页脚里的一行版权说明，更不是侧边那一列推荐链接里的标题，它就该是一篇文章的开头那一段。",
    "第二段接着说下去，同样写得足够长，两段时间加起来的字数已经明显超过了「这不像一篇文章」的那条线，于是这一整页看起来确实是在讲一件事，而不是把好几件不相干的事摆在同一个列表里。",
    "第三段收尾，也足够长；它继续把同一件事说完，这样整页的文字量就稳稳地落在文章那一档里，而不是停在摘要与菜单之间，也不至于让读的人以为正文只有前面那两句话。",
)


def _page(paragraphs, *, og_title: str = "", og_description: str = "") -> str:
    """一张合成出来的详情页。段落以外的东西一律给上: 导航、脚本、页脚。"""
    body = "".join("<p>%s</p>" % part for part in paragraphs)
    meta = ""
    if og_title:
        meta += '<meta property="og:title" content="%s">' % og_title
    if og_description:
        meta += '<meta property="og:description" content="%s">' % og_description
    return ("<html><head><title>%s</title>%s</head><body>"
            "<nav><p>首页</p><p>行情</p></nav><script>var a=1;</script>"
            "<article>%s</article>"
            "<footer><p>版权所有</p></footer></body></html>" % (og_title, meta, body))


def _clear_items() -> None:
    """这几条采集测试只想看自己写进去的记录 —— `fed` 夹具预置的那批也在同一张表里, 而补正文的
    欠账查询是整个表的。"""
    conn = db.connect()
    try:
        conn.execute("DELETE FROM feed_items")
        conn.commit()
    finally:
        conn.close()


def _serve(pages: dict, monkeypatch) -> list:
    """把 `http.get` 换成本地那几张页面, 回一个「谁被读了几次」的列表。"""
    from app.feed.sources import collect
    from app.sources import http

    calls: list = []

    def fake_get(url, headers=None, *, transport="http", timeout=30.0, tries=3, sleep=0.8):
        calls.append(url)
        if url not in pages:
            raise http.SourceError("%s -> HTTP 500" % url)
        return pages[url]

    monkeypatch.setattr(collect.http, "get", fake_get)
    return calls


def _budget(collect, monkeypatch, **overrides) -> None:
    """这一轮用哪几个数。文件里的出厂值不进测试 —— 那会让「预算是一」这条测试跟着文件走。"""
    numbers = {"maxFetches": 10, "minBodyChars": 200, "timeoutSeconds": 5, "retryHours": 24}
    numbers.update(overrides)
    monkeypatch.setattr(collect.pipeline, "detail", lambda path=None: dict(numbers))


def _web_source(monkeypatch, records, sid: str = "cn:chaincatcher"):
    from app.feed.sources import collect

    monkeypatch.setattr(collect, "fetcher_for", lambda source: (lambda row, first: (records, {})))
    source_store.import_roster()
    _only(sid)
    return collect


def test_a_long_article_keeps_the_sources_own_headings():
    """一篇文章四十段, 我们**不给它编小标题** —— 小标题是源自己的 `<h2>`, 有就留, 没有就没有.

    按位置命名 (摘要 / 发生了什么 / 调查与响应) 只说得清前三段, 第四段起就会印出三十七个「后续」,
    那是把一个编出来的名字重复三十七遍, 比没有名字更假. 段落一条都不许丢.
    """
    from app.feed.models import Discovery

    body = "<p>开头。</p>" + "".join(
        "<h2>第 %d 节</h2><p>第 %d 段: 这里是一段正文, 写得够长到能被当成一段话读。</p>" % (index, index)
        for index in range(1, 41))
    page = analysis.article(Discovery(source_kind="web_list", source_id="cn:chaincatcher",
                                      external_id="u-1", title="一篇长文",
                                      url="https://example.com/long", published_at="",
                                      body=body))
    blocks = htmlbody.blocks(page["body_html"])
    assert len(blocks) == 81 and blocks[1] == "第 1 节"
    assert "<h2>第 40 节</h2>" in page["body_html"]


def test_a_detail_page_is_read_into_its_paragraphs():
    """段落级正文: 导航、脚本、页脚里的 `<p>` 也是 `<p>`, 不认这几样, 它们会变成文章的开头。"""
    from app.feed.sources import detail

    found = detail.parse(_page(_LONG, og_title="某交易所今天上线了一个新币 - 某站点"))
    assert _LONG[0] in found["body"] and _LONG[2] in found["body"]
    assert "首页" not in found["body"] and "版权所有" not in found["body"]
    assert "var a=1" not in found["body"]
    # 尾巴上那截站点名去掉, 而正文里的连字符不该被砍。
    assert found["title"] == "某交易所今天上线了一个新币"


def test_a_detail_page_keeps_its_pictures_apart_from_the_text():
    """正文 HTML 只留文字, 图另存 (`media`) —— 卡片媒体视图与理解档的首图都读它。"""
    from app.feed.sources import detail

    page = _page(["。".join(_LONG), '<img data-src="https://a/chart.png" width="12">'])
    found = detail.parse(page)
    assert _LONG[0] in found["body"]
    assert "<img" not in found["body"]
    # 懒加载的真地址在 `data-src`; 没有高度的那一格是 None, 不是 0.
    assert found["media"] == [{"kind": "image", "url": "https://a/chart.png",
                               "width": 12, "height": None}]


def test_a_page_that_says_nothing_gives_back_nothing():
    """只有菜单与页脚的页面交回空串, 而不是把那堆字当成文章 —— 后者会被印在页面上, 而且没人
    看得出来。"""
    from app.feed.sources import detail

    assert detail.parse(_page(["短", "也短"]))["body"] == ""
    assert detail.parse("")["body"] == ""
    assert detail.parse("<html><body><p>没有</p></body></html>")["body"] == ""
    # 没有段落时, 描述是第二好的; 但描述也不够长就还是空。
    short = _page(["短"], og_description="一句话")
    assert detail.parse(short)["body"] == ""
    # 描述够长时它就是第二好的正文 —— 但它也得过 `_MIN_BODY` 那条线, 一句摘要不算。
    described = "。".join(_LONG)
    found = detail.parse(_page(["短"], og_description=described))["body"]
    assert htmlbody.to_text(found) == described


def test_a_placeholder_title_is_not_a_title():
    """8.5 的那条: 「阅读全文」不是标题, 一百多字的一整段也不是。"""
    from app.feed.sources import detail

    assert detail.looks_like_title("某交易所上线新币")
    assert not detail.looks_like_title("阅读全文")
    assert not detail.looks_like_title("Read more")
    assert not detail.looks_like_title("字" * 120)
    assert not detail.looks_like_title("")
    # 站点名只在尾巴短、剩下的还像标题时才去掉。
    assert detail.strip_site_suffix("文章标题写在这里 - 某站点") == "文章标题写在这里"
    assert detail.strip_site_suffix("Bitcoin - Ethereum") == "Bitcoin - Ethereum"


def test_a_bodyless_record_gets_its_paragraphs_the_same_round(monkeypatch, isolated_db):
    """列表页交出来的条目正文是空的, 而它在同一条 tick 里就该有正文 —— 这是这一站存在的理由。"""
    _clear_items()
    from app.feed.models import Discovery

    records = [Discovery(source_kind="web_list", source_id="cn:chaincatcher", external_id="u-1",
                         title="一条只有标题的记录", url="https://example.com/a",
                         published_at="")]
    collect = _web_source(monkeypatch, records)
    calls = _serve({"https://example.com/a": _page(_LONG)}, monkeypatch)
    _budget(collect, monkeypatch)

    report = collect.poll(now=NOW, limit=1)
    assert report["ok"] == 1 and report["new"] == 1 and report["filled"] == 1
    assert calls == ["https://example.com/a"]
    row = [row for row in db.feed_items_since("1970-01-01T00:00:00+00:00")
           if row["external_id"] == "u-1"][0]
    assert _LONG[0] in row["body"]
    assert row["detail_at"]


def test_a_placeholder_title_is_replaced_by_the_real_one(monkeypatch, isolated_db):
    """列表页给的「阅读全文」不是标题, 详情页里有真的那一个 (DESIGN.md 8.5)。"""
    _clear_items()
    from app.feed.models import Discovery

    records = [Discovery(source_kind="web_list", source_id="cn:chaincatcher", external_id="u-1",
                         title="阅读全文", url="https://example.com/a", published_at="")]
    collect = _web_source(monkeypatch, records)
    _serve({"https://example.com/a": _page(_LONG, og_title="某交易所今天上线了一个新币 - 某站点")},
           monkeypatch)
    _budget(collect, monkeypatch)

    collect.poll(now=NOW, limit=1)
    row = [row for row in db.feed_items_since("1970-01-01T00:00:00+00:00")
           if row["external_id"] == "u-1"][0]
    assert row["title"] == "某交易所今天上线了一个新币"


def test_the_detail_budget_is_per_round_and_the_leftovers_wait(monkeypatch, isolated_db):
    """一轮最多读 `maxFetches` 篇: 一轮 tick 会一次带进几十条, 而这是这一层最贵的一步。

    没读完的那些不能就这么算了 —— 下一轮只看新条目, 不接着补, 它们永远等不到。
    """
    _clear_items()
    from app.feed.models import Discovery

    records = [Discovery(source_kind="web_list", source_id="cn:chaincatcher",
                         external_id="u-%d" % index, title="第 %d 条" % index,
                         url="https://example.com/%d" % index, published_at="")
               for index in range(3)]
    collect = _web_source(monkeypatch, records)
    pages = {url: _page(_LONG) for url in
             ("https://example.com/0", "https://example.com/1", "https://example.com/2")}
    calls = _serve(pages, monkeypatch)
    _budget(collect, monkeypatch, maxFetches=1)

    first = collect.poll(now=NOW, limit=1)
    assert first["new"] == 3 and first["filled"] == 1 and len(calls) == 1

    second = collect.poll(now=NOW + timedelta(minutes=30), limit=1)
    assert second["new"] == 0 and second["filled"] == 1
    assert len(calls) == 2 and calls[1] != calls[0]

    third = collect.poll(now=NOW + timedelta(minutes=60), limit=1)
    assert third["filled"] == 1 and len(calls) == 3

    # 一条都没欠下了: 再跑一轮不再读任何页面。
    fourth = collect.poll(now=NOW + timedelta(minutes=90), limit=1)
    assert fourth["filled"] == 0 and len(calls) == 3


def test_a_detail_page_that_will_not_open_is_left_for_later(monkeypatch, isolated_db):
    """一条详情页打不开: 这一轮照旧算成, 条目留着, 而它 `retryHours` 之内不会再排进预算 ——
    坏链接每轮都敲, 会把好条目的位置占掉。"""
    _clear_items()
    from app.feed.models import Discovery

    records = [Discovery(source_kind="web_list", source_id="cn:chaincatcher", external_id="u-1",
                         title="一条只有标题的记录", url="https://example.com/dead",
                         published_at="")]
    collect = _web_source(monkeypatch, records)
    calls = _serve({}, monkeypatch)
    _budget(collect, monkeypatch)

    report = collect.poll(now=NOW, limit=1)
    assert report["ok"] == 1 and report["filled"] == 0       # 源没红, 只是这条没补上
    assert calls == ["https://example.com/dead"]

    again = collect.poll(now=NOW + timedelta(minutes=30), limit=1)
    assert again["due"] == 0 and len(calls) == 1             # 还在退避里, 再敲就是白敲

    later = collect.poll(now=NOW + timedelta(hours=25), limit=1)
    assert len(calls) == 2 and later["filled"] == 0


def test_a_tweet_is_not_worth_a_detail_page(monkeypatch, isolated_db):
    """推文的正文就是它自己, 而它的 url 是推文页 —— 没有登录态时抓回来是一层 JS 壳。"""
    _clear_items()
    from app.feed.models import Discovery

    records = [Discovery(source_kind="x_search", source_id="x:coindesk", external_id="t-1",
                         title="一条没有正文的推文", url="https://x.com/coindesk/status/1",
                         published_at="")]
    collect = _web_source(monkeypatch, records, sid="x:coindesk")
    calls = _serve({}, monkeypatch)
    _budget(collect, monkeypatch)

    report = collect.poll(now=NOW, limit=1)
    assert report["new"] == 1 and report["filled"] == 0 and calls == []


def test_the_detail_knobs_refuse_a_key_nobody_reads(tmp_path):
    """写在文件里、却被忽略的键, 比一个拼错的键更坏 —— 它看起来设过了。"""
    from app.feed.sources import pipeline

    path = tmp_path / "pipeline.yaml"
    path.write_text("detail:\n  maxFetch: 10\n", encoding="utf-8")
    with pytest.raises(pipeline.PipelineRefused):
        pipeline.load(path)
    # 文件不在也有出厂值: 一台刚拉下来的机器不该因为少一个旋钮文件就不补正文。
    assert pipeline.load(tmp_path / "gone.yaml")["detail"]["maxFetches"] == 40
    # 好好的那份就该读得出来。
    good = tmp_path / "good.yaml"
    good.write_text("detail:\n  maxFetches: 3\n", encoding="utf-8")
    assert pipeline.load(good)["detail"]["maxFetches"] == 3
