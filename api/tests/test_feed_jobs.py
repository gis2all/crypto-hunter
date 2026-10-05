"""定时活儿 (feed/jobs.py) 与来源图标 (feed/sources/icons.py)。

这两件事共用一个问法: **一次该发生的副作用, 什么时候发生、发生几次、有没有留下账**。
所以这里测的不是"函数跑通了", 而是那几条容易被悄悄改坏的规矩:

  * 一个槽只跑一次, 下一槽再跑 —— 判据是槽, 不是"上次跑完的时刻";
  * 停了三个槽再起来, `once` 的活儿补一次而不是三次 (`skip` 的干脆不补);
  * 一个活儿坏掉, 别的活儿照跑, 而它自己留下一行 failed;
  * 图标找过就记账 (哪怕是空手), 于是不会每一轮都去敲同一家。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.feed import jobs, service
from app.feed.sources import icons, pipeline, registry
from app.store import db

NOW = datetime(2026, 10, 5, 12, 0, 30, tzinfo=timezone.utc)

#: conftest 的 `no_live_feed` 会把时钟掐掉 (别的测试不该真的去抓), 而这一整个模块测的正是它。
#: 在模块导入时抓住真的那一个 —— 那时 autouse 的 patch 还没生效 —— 再在每条测试里装回去。
REAL_RUN_DUE = jobs.run_due


@pytest.fixture(autouse=True)
def real_clock(monkeypatch):
    monkeypatch.setattr(jobs, "run_due", REAL_RUN_DUE)


def marks() -> dict[str, dict]:
    return {row["name"]: row for row in db.feed_jobs_all()}


@pytest.fixture()
def probe(monkeypatch):
    """一个只有它自己的活儿注册表 —— 真那几个会去敲网络。"""
    calls: list[datetime] = []

    def run(moment: datetime) -> dict:
        calls.append(moment)
        return {"seen": moment.isoformat()}

    job = jobs.Job("probe.tick", 60, run)
    monkeypatch.setattr(jobs, "registry", lambda: [job])
    jobs.register()
    return calls


# ── 槽与补跑 ────────────────────────────────────────────────────────────────────

def test_a_slot_runs_exactly_once(isolated_db, probe):
    jobs.run_due(NOW)
    assert len(probe) == 1
    jobs.run_due(NOW + timedelta(seconds=20))
    assert len(probe) == 1


def test_the_next_slot_runs_again(isolated_db, probe):
    jobs.run_due(NOW)
    jobs.run_due(NOW + timedelta(minutes=61))
    assert len(probe) == 2


def test_a_missed_slot_is_caught_up_once(isolated_db, probe):
    """停了三小时再起来: 补一次, 不是三次 —— 补三次没有意义, 而一次不补就看不出它坏过。"""
    jobs.run_due(NOW)
    jobs.run_due(NOW + timedelta(hours=3))
    assert len(probe) == 2


def test_a_skip_job_does_not_catch_up(isolated_db, monkeypatch):
    calls: list[datetime] = []
    job = jobs.Job("skip.me", 60, lambda moment: calls.append(moment) or {}, missed="skip")
    monkeypatch.setattr(jobs, "registry", lambda: [job])
    jobs.register()

    jobs.run_due(NOW)
    assert len(calls) == 1
    # 一个小时后正好是下一槽: 照跑。
    jobs.run_due(NOW + timedelta(minutes=61))
    assert len(calls) == 2
    # 一次停了一小时以上: 不补, 但要把这一槽记成跳过, 否则下一秒还会问它一遍。
    jobs.run_due(NOW + timedelta(minutes=61 + 180))
    assert len(calls) == 2
    assert marks()["skip.me"]["last_status"] == "skipped"
    # 再往后一槽: 到点就跑, 不会因为前面跳过而永久停住。
    jobs.run_due(NOW + timedelta(minutes=61 + 240))
    assert len(calls) == 3


def test_slot_boundaries_align_to_the_cadence():
    assert jobs.slot_of(NOW, 60) == "2026-10-05T12:00:00+00:00"
    assert jobs.slot_of(NOW, 1440) == "2026-10-05T00:00:00+00:00"
    assert jobs.slot_of(NOW, 5) == "2026-10-05T12:00:00+00:00"


# ── 账 ──────────────────────────────────────────────────────────────────────────

def test_every_run_leaves_a_row_with_its_detail(isolated_db, probe):
    jobs.run_due(NOW)
    runs = db.feed_job_runs(20)
    assert len(runs) == 1
    assert runs[0]["name"] == "probe.tick"
    assert runs[0]["status"] == "ok"
    assert runs[0]["detail"]["seen"] == NOW.isoformat()


def test_a_failing_job_is_recorded_and_the_others_still_run(isolated_db, monkeypatch):
    calls: list[str] = []

    def boom(moment):
        raise RuntimeError("boom")

    monkeypatch.setattr(jobs, "registry", lambda: [
        jobs.Job("bad.one", 60, boom),
        jobs.Job("good.one", 60, lambda moment: calls.append("good") or {}),
    ])
    jobs.register()

    outcome = jobs.run_due(NOW)
    assert list(outcome["failed"]) == ["bad.one"]
    assert calls == ["good"]
    held = marks()
    assert held["bad.one"]["last_status"] == "failed"
    assert held["good.one"]["last_status"] == "ok"
    history = {row["name"]: row["status"] for row in db.feed_job_runs(20)}
    assert history == {"bad.one": "failed", "good.one": "ok"}


def test_the_shipped_registry_registers_every_job(isolated_db):
    """登记是"进程起来时对一遍"的那一半: 表里少了谁, 下一次问 due 就会露出来。"""
    jobs.register()
    held = marks()
    assert set(held) == {job.name for job in jobs.registry()}
    assert held["sources.poll"]["cadence_minutes"] == 1


def test_retention_prunes_the_old_rows(isolated_db, probe):
    jobs.run_due(NOW)
    assert len(db.feed_job_runs(20)) == 1
    # 清理的口径是"槽比保留期还老", 所以四天之后再清, 这一趟就该走了。
    jobs._retention(NOW + timedelta(days=400))
    assert db.feed_job_runs(20) == []


# ── 来源图标 ────────────────────────────────────────────────────────────────────

HTML = """<html><head>
<link rel="icon" href="/favicon.ico" sizes="16x16">
<link rel="apple-touch-icon" href="/touch.png" sizes="180x180">
<link rel="icon" href="/logo.svg" sizes="any">
</head><body></body></html>"""


def test_candidates_put_the_touch_icon_first_and_keep_a_favicon_under_it():
    found = icons.candidates(HTML, "https://example.com/post")
    assert found[0] == "https://example.com/touch.png"
    assert "https://example.com/logo.svg" in found
    assert found[-1] == "https://example.com/favicon.ico"


def test_candidates_ignore_data_urls():
    found = icons.candidates('<link rel="icon" href="data:image/png;base64,AAAA">',
                             "https://example.com/")
    assert found == ["https://example.com/favicon.ico"]


@pytest.fixture()
def one_site(isolated_db):
    db.feed_sources_put([{"id": "rss:example", "name": "Example", "kind": "rss",
                          "tier": "T2", "interval_minutes": 30,
                          "config": {"feedUrl": "https://example.com/rss.xml"}}])
    registry.forget()
    return db


def test_refresh_finds_the_icon_and_stores_it(one_site, monkeypatch):
    monkeypatch.setattr(icons.http, "get", lambda url, **kw: HTML)
    assert icons.refresh(NOW) == {"checked": 1, "found": 1}
    assert registry.of("rss:example").icon == "https://example.com/touch.png"


def test_a_second_refresh_does_not_probe_again(one_site, monkeypatch):
    """找过就记账 —— 空手也是结论。一个没有图标的站点不该每一轮都被敲一遍。"""
    def refused(url, **kw):
        raise RuntimeError("no")

    monkeypatch.setattr(icons.http, "get", refused)
    assert icons.refresh(NOW) == {"checked": 1, "found": 0}
    assert icons.refresh(NOW + timedelta(days=1)) == {"checked": 0, "found": 0}


def test_a_source_without_a_home_is_recorded_as_checked(isolated_db):
    db.feed_sources_put([{"id": "x:someone", "name": "Someone", "kind": "x_search",
                          "tier": "T2", "interval_minutes": 120,
                          "config": {"url": "https://x.com/someone"}}])
    registry.forget()
    assert icons.refresh(NOW)["checked"] == 1
    # X 那一家的头像不在首页上, 跳过它也算看过一次。
    assert registry.of("x:someone").icon == ""
    assert icons.refresh(NOW + timedelta(days=1))["checked"] == 0


def test_an_explicit_icon_in_the_roster_wins(isolated_db, monkeypatch):
    db.feed_sources_put([{"id": "rss:pinned", "name": "Pinned", "kind": "rss",
                          "tier": "T2", "interval_minutes": 30,
                          "icon": "https://cdn.example.com/ours.png",
                          "config": {"feedUrl": "https://pinned.example.com/feed"}}])
    registry.forget()
    assert registry.of("rss:pinned").icon == "https://cdn.example.com/ours.png"

    def refused(url, **kw):
        raise AssertionError("should not fetch")

    monkeypatch.setattr(icons.http, "get", refused)
    # 清单写了就不去猜, 也不去敲对方。
    assert icons.refresh(NOW) == {"checked": 0, "found": 0}
    # 再导一次清单也不该把找到的图标抹掉 (那是导入最容易被写坏的一格)。
    db.feed_sources_put([{"id": "rss:pinned", "name": "Pinned", "kind": "rss",
                          "tier": "T2", "interval_minutes": 30,
                          "config": {"feedUrl": "https://pinned.example.com/feed"}}])
    registry.forget()
    assert registry.of("rss:pinned").icon == "https://cdn.example.com/ours.png"


def test_faces_carry_the_source_icon(one_site, monkeypatch):
    """图标最后要走到卡片上: `faces()` 就是热点榜那一叠头像的来源。"""
    from app.feed import views
    from app.feed.models import Discovery, Event, Item

    monkeypatch.setattr(icons.http, "get", lambda url, **kw: HTML)
    icons.refresh(NOW)

    item = Item(discovery=Discovery(
        source_kind="rss", source_id="rss:example", external_id="1", title="t",
        url="https://example.com/1", published_at="2026-10-05T10:00:00+00:00", body="b"))
    event = Event(identity_key="k", kind="story", kind_label="事件", title="t",
                  url="https://example.com/1", first_seen_at="2026-10-05T10:00:00+00:00",
                  last_seen_at="2026-10-05T10:00:00+00:00", items=[item])
    faces = views.faces(event)
    assert faces and faces[0]["icon"] == "https://example.com/touch.png"


# ── 出刊 ────────────────────────────────────────────────────────────────────────

BEIJING = timezone(timedelta(hours=8))


def _at(hour: int, minute: int = 0, day: int = 5) -> datetime:
    """北京时间的一个时刻 —— 出刊那一格就是按这个时区念的。"""
    return datetime(2026, 10, day, hour, minute, tzinfo=BEIJING)


@pytest.fixture()
def press(monkeypatch):
    """出刊那一趟: 出的那一期记下来, 不真的编排一整轮 (那一轮要读库、要请模型)。"""
    ran: list[tuple[str, datetime]] = []
    monkeypatch.setattr(service, "run_once",
                        lambda *, now=None, kind="day", **kw: ran.append((kind, now)))
    return ran


def _clocks(monkeypatch, **times) -> None:
    monkeypatch.setattr(pipeline, "schedule", lambda path=None: dict(times))


def test_a_clock_that_has_not_come_round_yet_puts_out_nothing(isolated_db, monkeypatch, press):
    _clocks(monkeypatch, day="08:00", week="", month="")
    assert jobs._publish_due(_at(7, 59)) is None
    assert press == []


def test_the_clock_puts_out_the_issue_that_just_ended(isolated_db, monkeypatch, press):
    """08:00 的日报讲昨天 —— 窗口是一个完整的日子, 不是「从今早到现在」。"""
    _clocks(monkeypatch, day="08:00")
    assert jobs._publish_due(_at(8, 0)) == {"day": "2026-10-04"}
    assert press == [("day", datetime(2026, 10, 4, 23, 59, 59, tzinfo=BEIJING))]


def test_a_blank_clock_is_how_that_issue_stops_coming_out(isolated_db, monkeypatch, press):
    _clocks(monkeypatch, day="08:00", week="", month="08:00")
    assert jobs._publish_due(_at(9, 0)) == {"day": "2026-10-04", "month": "2026-09"}
    assert [kind for kind, _now in press] == ["day", "month"]


def test_an_issue_that_already_came_out_is_not_printed_again(isolated_db, monkeypatch, press):
    """一期是出刊那一刻的定稿: 重出会把它换成「现在这一秒的行情」, 纸和详情之间那个差就没了。"""
    _clocks(monkeypatch, day="08:00")
    db.feed_edition_put({"id": service.edition_id("day", "2026-10-04"), "kind": "day",
                         "key": "2026-10-04", "window_from": "2026-10-04T00:00:00+08:00",
                         "window_to": "2026-10-05T00:00:00+08:00", "payload_json": "[]"})
    assert jobs._publish_due(_at(8, 0)) is None
    assert press == []


def test_a_clock_nobody_can_read_does_not_take_the_clock_down(isolated_db, monkeypatch, press):
    """一个手写的时刻不该让整台定时器停下来 —— 这一期不出, 别的照出。"""
    _clocks(monkeypatch, day="八点", week="08:00")
    assert jobs._publish_due(_at(9, 0)) == {"week": "2026-W40"}
    assert [kind for kind, _now in press] == ["week"]
