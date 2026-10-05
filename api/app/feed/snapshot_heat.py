"""热度快照与榜单快照 (AIHOT 的 `events/hot.ts` 的落库那一半).

页面上那条线和一个名次读的是**写下来的结果**, 不是每个请求各算一遍: 两个地方各算一次, 迟早
会算出两个数, 而读者看到的是一张图和一句话互相矛盾 (DESIGN 20).

三件事:
- `snapshot(at)`: 每小时给每一件事记一行热度. 当时有源在落后就记成 `complete = 0`, 等源追上
  来再用 `grace = false` 重算那一格 —— 证据带着源自己的时间, 所以过去的一格是确定的.
- `rank(at)`: 算一遍热榜并落一份快照, 页面读最新那一份.
- `series(story_id)` / `sparkline(ids, at)`: 读库画线, 不用重算.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..store import db
from . import heat, materials

#: 榜单快照的保鲜时间. 与 AIHOT 的 `hot.rank` 每 5 分钟一趟同一个数: 5 分钟内的请求读的是
#: 同一份榜, 所以同一个页面的名次不会因为刷新而跳.
RANK_TTL_MINUTES = 5

#: 榜单只留这么多天 (AIHOT 的 `DELETE FROM hot_rankings WHERE computed_at < now() - interval '30 days'`).
RANK_KEEP_DAYS = 30

#: 曲线画多久 (AIHOT 的 `heatSeries` 默认 7 天).
SERIES_DAYS = 7


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _floor_hour(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _drawable(line: list[float | None] | None) -> list[float | None] | None:
    """少于两个真实点就不换 —— 一条只有一个点的线画不出来, 换了等于把图抹掉.

    快照是攒出来的: 刚上线的库里每个故事只有当前那一格. 那时候页面退回现算的 24 小时线,
    等快照攒够两格再读库 (AIHOT 那边从头就是空的, 因为它的历史是回填出来的).
    """
    if not line or sum(1 for point in line if point is not None) < 2:
        return None
    return line


def _story_number(story_id: str) -> int | None:
    """`s:<id>` -> id. 规则桶的键是哈希, 库里没有它的行, 所以答 None (没有证据可读)."""
    raw = story_id.split(":", 1)[1] if ":" in story_id else story_id
    return int(raw) if raw.isdigit() else None


def _rows(at: datetime, *, behind: set[str], story_since: dict[str, datetime],
          story_ids: set[str] | None = None) -> list[tuple]:
    """每一件事在 `at` 的热度行. `story_ids` 只在修补过去那一格时给 (AIHOT 也是按 id 限)."""
    from . import views  # 延迟导入: views 在模块层读 heat, 这里再读 views 会成环.
    _items, stories = views.collect(at, heat.WINDOW_HOURS)
    out = []
    for event in stories:
        if story_ids is not None and event.identity_key not in story_ids:
            continue
        row = heat.row_at(event, at, behind=behind, source_since=story_since)
        out.append((event, row))
    return out


def _cell(event, row, hour: datetime) -> dict:
    """一行快照. `heat` 存的是**页面那个单位** (AIHOT 的 `saveHeat` 也存 `heatIndex`)."""
    return {
        "story_id": event.identity_key,
        "heat": heat.index(row.heat),
        "participants": int(row.participants),
        "cohort": int(row.editorial),
        "complete": int(row.behind) == 0,
    }


def snapshot(at: datetime) -> dict:
    """落这一小时的热度, 再把之前没补完的那几格补一次 (AIHOT 的 `snapshotHeat`).

    `complete = 0` 的意思是"那一刻有源没把该说的说完". 它的值当时是对的, 只是可能不全;
    源追上来之后, 用 `grace = false` 重算那一格 —— 一个源只要在那之后成功抓过, 就说明它
    该说的都说了, 那一格也就定了.
    """
    hour = _floor_hour(at)
    clocks = heat.source_clocks(db.feed_source_clocks())
    story_since = {c["id"]: c["since"] for c in clocks if c.get("since")}
    behind = heat.clocks_behind(clocks, hour, grace=True)
    rows = [_cell(event, row, hour) for event, row in _rows(hour, behind=behind,
                                                             story_since=story_since)]
    stored = db.feed_heat_snapshot_set(_iso(hour), rows)
    # 曲线只画 7 天 (SERIES_DAYS), 更早的格子不会被读 —— 让它们留在库里只是让 SQLite 长大.
    db.feed_heat_prune(_iso(hour - timedelta(days=SERIES_DAYS)))

    repaired = 0
    window_from = hour - timedelta(hours=heat.WINDOW_HOURS)
    for stale in db.feed_heat_hourly_stale(_iso(window_from), _iso(hour)):
        moment = materials.moment(stale["hour"])
        ids = set(stale["story_ids"])
        late_behind = heat.clocks_behind(clocks, moment, grace=False)
        rows = [_cell(event, row, moment)
                for event, row in _rows(moment, behind=late_behind,
                                        story_since=story_since, story_ids=ids)
                if row.behind == 0]
        repaired += db.feed_heat_snapshot_set(stale["hour"], rows)
    return {"stories": stored, "repaired": repaired}


def rank(at: datetime, *, limit: int = 10) -> dict:
    """算一遍热榜并落一份快照 (AIHOT 的 `computeHotRanking`). 返回那一行的 id 与条数."""
    from . import views
    ranked = views.ranked_stories(at)
    entries = [views.story_payload(event, at) for event in ranked[:limit]]
    evidence = {
        "windowHours": heat.WINDOW_HOURS,
        "halfLifeHours": heat.HALF_LIFE_HOURS,
        "minParticipants": heat.MIN_PARTICIPANTS,
        "candidates": len(ranked),
        "total": len(ranked),
    }
    ranking_id = db.feed_hot_ranking_new({
        "computed_at": _iso(at),
        "rule_version": heat.RULE_VERSION,
        "entries_json": json.dumps(entries, ensure_ascii=False),
        "evidence_json": json.dumps(evidence, ensure_ascii=False),
    })
    db.feed_hot_ranking_prune(_iso(at - timedelta(days=RANK_KEEP_DAYS)))
    return {"id": ranking_id, "entries": len(entries), "total": len(ranked)}


def latest(at: datetime, *, ttl_minutes: int = RANK_TTL_MINUTES) -> dict | None:
    """最新一份还新鲜的榜单, 换成页面的形状; 没有、过期或换了规则版本就答 None.

    规则版本要对得上: 一份按旧规则算的榜, 名次用的还是旧的判据, 拿它当今天的结果读, 页面
    就会印出一份没人认领的名次.
    """
    stored = db.feed_hot_ranking_latest()
    if not stored or stored["rule_version"] != heat.RULE_VERSION:
        return None
    computed = materials.moment(stored["computed_at"])
    if computed is None or (at - computed) > timedelta(minutes=ttl_minutes):
        return None
    entries = json.loads(stored["entries_json"] or "[]")
    evidence = json.loads(stored["evidence_json"] or "{}")
    marks = sparkline([entry["id"] for entry in entries], computed)
    for entry in entries:
        line = _drawable(marks.get(entry["id"]))
        if line:
            entry["series"] = line
    return {
        "window_hours": heat.WINDOW_HOURS,
        "generated_at": stored["computed_at"],
        "total": int(evidence.get("total") or len(entries)),
        "stories": entries,
    }


def hot_payload(at: datetime, *, limit: int = 10) -> dict:
    """页面读的那一份: 先读库, 库里没有 (或太旧) 就算一份新的再读. 两条路都走不通才交给调用方
    退回现算 —— 榜单是一次写下来的结果, 不是每次请求的副产品."""
    stored = latest(at)
    if stored is not None:
        return stored
    rank(at, limit=limit)
    stored = latest(at, ttl_minutes=24 * 60)
    if stored is not None:
        return stored
    from . import views
    return views.hot_view(at, limit=limit)


def sparkline(story_ids: list[str], at: datetime) -> dict[str, list[float | None]]:
    """榜单卡片上那条线 (AIHOT 的 `sparklines`): 25 个**小时**格子, 缺的补 `null`.

    `at` 是榜单算出来的那一刻取整到小时 —— 线画的是"这份榜"的来路, 不是请求进来的那一刻.
    """
    end = _floor_hour(at)
    start = end - timedelta(hours=heat.SPARK_HOURS)
    slots = heat.SPARK_HOURS + 1
    out: dict[str, list[float | None]] = {sid: [None] * slots for sid in story_ids}
    rows = db.feed_heat_hourly(story_ids, _iso(start), _iso(end))
    for row in rows:
        moment = materials.moment(row["hour"])
        if moment is None:
            continue
        slot = int(round((moment - start).total_seconds() / 3600.0))
        line = out.get(row["story_id"])
        if line is not None and 0 <= slot < slots:
            line[slot] = float(row["heat"])
    return out


def series(story_id: str, at: datetime, *, days: int = SERIES_DAYS) -> list[dict]:
    """一件事逐小时的热度, 只画观察完整的小时 (AIHOT 的 `heatSeries`).

    只数**从头看到尾**的那批参与者: 一个源是后来才接入的, 它在更早的小时里当然什么都没说,
    把它算进去等于把一条本来平的线画成一条上升的线 —— 那不是"这件事在涨", 是"我们刚开始看".
    """
    raw = _story_number(story_id)
    rows = db.feed_heat_hourly([story_id], _iso(at - timedelta(days=days)), _iso(at))
    if raw is None or not rows:
        return []
    first = materials.moment(rows[0]["hour"])
    if first is None:
        return []
    window_from = first - timedelta(hours=heat.WINDOW_HOURS)
    since = {clock["id"]: clock["since"] for clock in heat.source_clocks(db.feed_source_clocks())}
    times: dict[str, list[datetime]] = {}
    for row in db.feed_story_evidence_times(raw, _iso(window_from), _iso(at)):
        moment = materials.moment(row["observed_at"])
        if moment is None:
            continue
        if (since.get(row["source_id"]) or window_from) > window_from:
            continue
        times.setdefault(row["source_id"], []).append(moment)
    out: list[dict] = []
    for row in rows:
        hour = materials.moment(row["hour"])
        if hour is None:
            continue
        total = 0.0
        participants = 0
        for marks in times.values():
            last = max((mark for mark in marks
                        if hour - timedelta(hours=heat.WINDOW_HOURS) < mark <= hour),
                       default=None)
            if last is None:
                continue
            total += 0.5 ** (heat.age_hours(last, hour) / heat.HALF_LIFE_HOURS)
            participants += 1
        out.append({"hour": row["hour"], "heat": heat.index(total), "participants": participants})
    return out if any(point["heat"] > 0 for point in out) else []


def backfill(story_id: str, *, hours: int = SERIES_DAYS * 24,
             at: datetime | None = None) -> int:
    """给一件事重算它的历史格子 (AIHOT 的 `backfillStoryHeat`): 导入或重新分组之后用.

    从它第一条证据所在的那个小时开始, 到"最后一条证据 + 48 小时"与"现在"的较早者为止 ——
    更晚的小时它已经不在窗口里了, 算了也是零.
    """
    now = at or datetime.now(timezone.utc)
    raw = _story_number(story_id)
    if raw is None:
        return 0
    span = db.feed_story_evidence_span(raw)
    if not span or not span.get("first_at"):
        return 0
    first = materials.moment(span["first_at"])
    last = materials.moment(span.get("last_at") or span["first_at"])
    if first is None:
        return 0
    end = _floor_hour(min(now, (last or now) + timedelta(hours=heat.WINDOW_HOURS)))
    start = max(_floor_hour(first), end - timedelta(hours=hours))
    clocks = heat.source_clocks(db.feed_source_clocks())
    story_since = {c["id"]: c["since"] for c in clocks if c.get("since")}
    written = 0
    moment = start
    while moment <= end:
        behind = heat.clocks_behind(clocks, moment, grace=False)
        rows = [_cell(event, row, moment)
                for event, row in _rows(moment, behind=behind, story_since=story_since,
                                        story_ids={story_id})]
        written += db.feed_heat_snapshot_set(_iso(moment), rows)
        moment += timedelta(hours=1)
    return written
