"""One pass of the feed: read every source, decide what happened, put out an issue.

The stations, in this order and no other. Each is cheaper than the one after it, and the cheap
ones exist so the expensive one never sees garbage:

    read     every source answers with raw records                      free
    dedupe   a re-read is not a second record                           free
    window   what counts as this issue's window                         free
    names    the window teaches which parties it is about               free
    group    discoveries become events; a party is counted once         free
    heat     parties, discounted by age                                 free
    read     the reading pass, on what was picked and never on the rest  the one station that costs
    write    the records, then the issue                                free

`read` sitting after ranking is the point of the design (DESIGN.md 4): the only station that can
cost money sees a window that has already been deduped, windowed, grouped and ranked, and it
never sees the same record twice. Reading the whole window and dropping most of it afterwards is
how a feed spends its budget on things nobody will read.

An issue is a *period*, not a run: `period` turns a moment into "the third of October", "week
40", "October 2026", and the id comes from that name. Which is why running the same day twice
publishes one issue - not because anything remembers, but because both runs are about the same
period and a period has one name.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Sequence

from ..store import db
from . import events as feed_events
from . import delivery, heat, materials, reports, snapshot_heat
from .models import Discovery, Edition, Event
from .sources import pipeline, read_between

log = logging.getLogger(__name__)

#: The reader's day. A Chinese-language issue about "October 3rd" is about the +8 day, and a
#: window cut at UTC midnight would put a whole evening's news on the wrong date.
ZONE = timezone(timedelta(hours=8))

#: The three issues, narrowest first. 日报 / 周报 / 月报 are one machine with three periods, not
#: three products: everything below is the same whichever one is being put out.
KINDS = ("day", "week", "month")

#: How many events an issue holds. A cut, not a target: a quiet window that produced three things
#: says something true, and padding it to eight would say nothing.
PER_EDITION = 8

def period(kind: str, moment: datetime) -> tuple[str, datetime, datetime]:
    """The issue a moment belongs to: its name, and the window it covers.

    Both ends are natural boundaries in `ZONE` and both are half-open (`materials.in_window`),
    so consecutive issues abut exactly and a record landing at midnight is published once. The
    end is the *end of the period*, not the moment of the run: an issue is a day, and a day does
    not end at whatever time somebody happened to press the button.
    """
    if kind not in KINDS:
        raise ValueError("没有这种刊: %s" % kind)
    at = moment.astimezone(ZONE)
    midnight = at.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "day":
        start, end = midnight, midnight + timedelta(days=1)
        return start.strftime("%Y-%m-%d"), start, end
    if kind == "week":
        start = midnight - timedelta(days=at.weekday())      # Monday, ISO's first day
        year, week, _ = start.isocalendar()
        return "%04d-W%02d" % (year, week), start, start + timedelta(days=7)
    start = at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.strftime("%Y-%m"), start, (start + timedelta(days=32)).replace(day=1)


def edition_id(kind: str, key: str) -> str:
    """A stable id for one issue: the same period run twice is the same issue.

    Derived rather than random on purpose - with a random id, "this day again" looks exactly
    like "another issue", and the page would end up with two rows for one morning. The id
    carries the kind as well as the key because `2026-10` and a hypothetical day keyed
    `2026-10` are two different issues, and `feed_editions.id` is the only key the table has.
    """
    return "feed-%s-%s" % (kind, key)


def _entry(event: Event) -> dict:
    """One event as the page reads it.

    Written out by hand rather than with `asdict`, and that is the whole of the fix for the
    empty `discoveries` a page used to get: `asdict` walks into `Event.items`, whose children
    are `Item`s - a discovery *plus* what was read out of it - while the page asks for
    `discoveries`, the raw records. Two names, one of which nothing produced, so the field
    came back as an empty list and the paper could say "four parties" but never say who.

    `items` is not carried alongside: the reading pass has already been applied and sits in
    `judgement`, and an issue holding both the read and the unread version of the same record
    is a page that has to be told which one to print.
    """
    return {
        "identity_key": event.identity_key,
        "kind": event.kind,
        "kind_label": event.kind_label,
        "title": event.title,
        "url": event.url,
        "first_seen_at": event.first_seen_at,
        "last_seen_at": event.last_seen_at,
        "sources": event.sources,
        "heat": event.heat,
        "judgement": event.judgement,
        "discoveries": [asdict(item.discovery) for item in event.items],
    }


def payload(edition: Edition) -> dict:
    """The issue as the row it is stored in.

    The scalars are columns and the body is `payload_json`, one fact in one place: the list
    route has to draw a row without reading a body, and the detail route has to hand back a
    body without re-deriving the scalars. A row that kept both would eventually disagree with
    itself, and the page reads whichever the route happened to pick.
    """
    return {
        "id": edition_id(edition.kind, edition.key),
        "kind": edition.kind,
        "key": edition.key,
        "window_from": edition.window_from,
        "window_to": edition.window_to,
        "seen": edition.seen,
        "entries": len(edition.entries),
        "headline": edition.headline,
        "metrics_json": json.dumps(edition.metrics, ensure_ascii=False),
        "payload_json": json.dumps(
            {"lead": edition.lead,
             "entries": [_entry(entry) for entry in edition.entries],
             # 要点与快讯是**出刊那一刻**算好的: 一期是快照, 读的时候现算会跟着行情变
             # (纸印的是定稿, 详情才是行情 —— 同一条理由).
             "highlights": reports.highlights(edition.entries),
             "flashes": reports.flashes(edition.entries)},
            ensure_ascii=False),
    }


def write(edition: Edition, items: Sequence[Discovery], names: Sequence[str]) -> dict:
    """Record what the window held, then put the issue out.

    Two writes, not one transaction: the records are true whatever becomes of the issue, and an
    issue that failed to write has to be re-runnable without those records being lost. What makes
    a re-run safe is the table and not this function - `feed_editions` refuses a second row for a
    window that already has one (store/db.py), so a pass that runs twice publishes once.
    """
    db.feed_items_put([
        {
            "source_id": item.source_id,
            "external_id": item.external_id,
            "source_kind": item.source_kind,
            "identity_key": materials.identity_key(item, names),
            "title": item.title,
            "url": item.url,
            "author": item.author,
            "body": item.body,
            "published_at": item.published_at,
        }
        for item in items
    ])
    stored = db.feed_edition_put(payload(edition))
    # 一期出炉就排一条推送 (5.8)。推送是出口, 不是这一轮的目的 ——
    # 它排不进去不该把这一期带走。
    try:
        delivery.queue_issue(stored)
    except Exception:  # pragma: no cover - 排队是出口, 不该扛下这一期
        log.exception("feed: could not queue the notification")
    return stored


def run_once(*, now: datetime | None = None, kind: str = "day",
             per_edition: int = PER_EDITION,
             names: Sequence[str] = ()) -> dict:
    """One pass, start to finish. Answers with the stored issue, which is now on file.

    `now` is a parameter for the same reason `sources.mock.read` takes one: everything here is
    measured against a clock, and a test that cannot pin it is a test that changes its mind at
    midnight.
    """
    moment = now or datetime.now(timezone.utc)
    key, start, end = period(kind, moment)

    raw: list[Discovery] = read_between(start, end)

    unique = materials.dedupe(raw)
    inside = [item for item in unique if materials.in_window(item, start, end)]
    names_all = materials.learn_aliases(inside, names)
    grouped = feed_events.group(inside, names_all)
    for event in grouped:
        event.heat = heat.score(event, moment)

    edition = reports.compose(
        kind=kind,
        key=key,
        window_from=start.isoformat(timespec="seconds"),
        window_to=end.isoformat(timespec="seconds"),
        events=grouped,
        seen=len(inside),
        per_edition=per_edition,
        # 一期的导语与报头由模型写 (5.7), 预算走写作站同一格.
        budget=pipeline.llm(),
    )
    stored = write(edition, inside, names_all)
    # 一轮跑完顺手落这一小时的热度 (AIHOT 的 `hot.snapshot` 是每小时一趟; 这里挂在这一趟上,
    # 因为信息流今天只有这一个定时入口). 它只写派生的快照, 失败不该把这一期的结果带走.
    try:
        snapshot_heat.snapshot(moment)
    except Exception:  # pragma: no cover - 快照是派生物, 不该拖垮这一轮
        log.exception("feed: heat snapshot failed")
    return stored
