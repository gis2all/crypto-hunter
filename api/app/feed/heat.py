"""热度: 过去 48 小时里, 有几个独立来源在说这件事, 按 24 小时半衰期折算.

口径整条照 AIHOT 的 `events/hot.ts`:

    窗口   48 小时 —— 只有落在这个窗口里的发言算数. 没有这一条, 一件陈年旧事会一直累加热度,
           最后挤掉今天真正在发生的事, 页面上就是"热点榜混进了别的事".
    去重   每家一次 —— 一家发了三条是一条, 不是三条; 转载十遍的通讯社稿是一个人说话.
    取值   每家取窗口内**最新**那一条: 问的是"这家现在还在说吗", 不是"它最早什么时候说".
    衰减   24 小时半衰期 —— 过一天折一半, 把"今天说的"和"前天说的"分开.
    入榜   至少两家**而且其中至少一家是发布源** (AIHOT 的 `MIN_PARTICIPANTS` +
           `editorial_participants >= 1`): 全是讨论帖凑出来的两家, 是有人在聊, 不是有人在报.

这个文件只放"怎么算", 不碰库 —— 时钟 (`sourceClocks`) 与快照落库在 `db.py` 和采集站.
排行与门槛在 `views.ranked_stories`, 页面字段在 `views.story_payload`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import materials
from .models import Event
from .sources import registry

#: 只有落在这段时间里的发言算数 (AIHOT 的 `WINDOW_HOURS`). 一件旧事会因此自然掉出榜, 不用额外
#: 的截止规则 —— 窗口和半衰期是两件事: 窗口决定"谁还在场", 半衰期决定"在场的值多少".
WINDOW_HOURS = 48.0

#: 过多少小时折一半 (AIHOT 的 `HALF_LIFE_HOURS`). 24 对 48 小时窗口, 最老的一条在最年轻的一条
#: 面前值 1/4 —— 一天前的报道还在榜上, 但压不过今天早上的.
HALF_LIFE_HOURS = 24.0

#: 入榜的两条门槛 (AIHOT 的 `MIN_PARTICIPANTS` 与 `editorial_participants >= 1`).
MIN_PARTICIPANTS = 2
MIN_EDITORIAL_PARTICIPANTS = 1

#: "比 6 小时前热了多少" 的那个 6 小时 (AIHOT 的 `heat_prev` 也取 6 小时前).
TREND_BACK_HOURS = 6.0

#: 小标签的阈值 (AIHOT 的 `computeHotRanking` 那三行). 三条**各自成立各自印** —— 一件刚发生
#: 又被三家同时跟进的事, 该同时带着 `surge` 和 `new`, 不是二选一.
NEW_HOURS = 6.0                 # 这件事第一次出现还不到 6 小时
SURGE_MIN_NEW = 3               # 至少 3 家是这 6 小时才加入的
SURGE_SHARE = 0.5               # 而且他们占了全部来源的一半 —— 半路起量, 不是慢慢热起来
RISING_PCT = 0.15               # 涨得超过 15% 才叫 "rising"
TREND_FLAT = 0.1                # 涨跌不到 10% 就是 "flat", 不是每个小数都值得箭头

#: 页面上印的那个数: AIHOT 的 `heatIndex` = 四舍五入到**一位小数**的 10 倍值.
#: heat 1.0 印成 10.0 —— 单位是"折合几个满血来源", 乘 10 只是让读者看到小数点后一位.
INDEX_SCALE = 10.0

#: 走势画多久、多久一个点 (AIHOT 的 `SPARK_HOURS`): 24 个点, 每小时一个.
SPARK_HOURS = 24
SPARK_STEP_HOURS = 1.0

#: 榜单与名单的上限 (AIHOT 的条目上限 10 / 参与者上限 40 / 来源名 8 个).
ENTRIES_MAX = 10
PARTICIPANTS_MAX = 40
SOURCE_NAMES_MAX = 8

#: 规则的版本号. 落库的每一份榜单都带着它 —— 改了算法而版本没变, 旧榜就会被当成本次结果读.
RULE_VERSION = "heat-v1-48h-halflife24h"


def age_hours(moment: datetime, now: datetime) -> float:
    """Hours between two aware datetimes, never negative.

    A source whose clock runs ahead hands over a timestamp in the future, and reading that as a
    negative age would make a story hotter than one published right now - a bug that pays out to
    whoever sets their clock forward. So it is clamped.
    """
    return max((now - moment).total_seconds() / 3600.0, 0.0)


def editorial(source_id: str) -> bool:
    """这家是发布源还是讨论源 (AIHOT 的 `participation_mode = 'editorial'`).

    入榜要有至少一家发布源: 两家都在转述同一张图, 热度是真的, 但它还没有一个"谁在报"。
    """
    return registry.of(source_id).participation == "editorial"


def source_clocks(rows: list[dict]) -> list[dict]:
    """Turn `feed_sources` rows into the clock shape the heat readers want (AIHOT 的 `sourceClocks`).

    `last_ok` 是这一路最后一次**成功**的时刻, 不是最后一次尝试 —— 失败的那次什么都没读到, 拿它
    当时钟, 一个正在失败的源会被当成"刚刚读过". `created_at` 是这家什么时候接入的.
    """
    out: list[dict] = []
    for row in rows:
        if not row.get("enabled"):
            continue
        if row.get("kind") not in ("rss", "json_list", "web_list", "x_search"):
            continue
        last = row.get("last_ok_at")
        since = row.get("created_at")
        out.append({
            "id": row["id"],
            "kind": row["kind"],
            "enabled": True,
            "last_ok": materials.moment(last) if last else None,
            "interval_minutes": int(row.get("interval_minutes") or 0),
            "since": materials.moment(since) if since else None,
        })
    return out


def clocks_behind(clocks: list[dict], at: datetime, grace: bool) -> set[str]:
    """Which sources may not have delivered everything up to `at` (AIHOT 的 `behindSources`).

    Looking at the present (`grace=True`) a source that has not succeeded in three intervals (at
    least 90 minutes) is behind. For an hour already past (`grace=False`) it has caught up as soon
    as any fetch succeeded after that hour, because evidence carries the source's own timestamp.
    """
    behind: set[str] = set()
    for clock in clocks:
        last_ok = clock.get("last_ok")
        width = max(int(clock.get("interval_minutes") or 0) * 3, 90) * 60
        if last_ok is None or last_ok < at - timedelta(seconds=width if grace else 0):
            behind.add(clock["id"])
    return behind


def observations(event: Event, when: datetime,
                 hours: float = WINDOW_HOURS) -> dict[str, datetime]:
    """每家在窗口内的**最新**一次发言: `source_id -> published`.

    窗口 = `when` 往前 48 小时, 而且不晚于 `when` —— 后者是"把一条线画成一条线"的全部:
    还没发生的报道不能算进一个过去的时刻.
    """
    floor = when - timedelta(hours=hours)
    latest: dict[str, datetime] = {}
    for item in event.items:
        published = materials.moment(item.discovery.published_at)
        if published > when or published <= floor:
            continue
        source_id = item.discovery.source_id
        if source_id not in latest or published > latest[source_id]:
            latest[source_id] = published
    return latest


@dataclass
class Row:
    """One story's heat, with the hidden columns AIHOT's `heatRows` also computes.

    `heat` is what the ranking sorts on. `heat_prev` is the same over the 48-hour window that ended
    six hours ago; the two `_obs` numbers are the same pair restricted to the participants that can
    be compared (AIHOT's `heat_obs` / `heat_prev_obs`). `uncomparable` is how many participants fall
    outside that comparison, and `behind` how many of today's have a source that is behind.
    """

    heat: float = 0.0
    participants: int = 0
    heat_prev: float = 0.0
    heat_obs: float = 0.0
    heat_prev_obs: float = 0.0
    comparable: int = 0
    uncomparable: int = 0
    behind: int = 0
    recent: int = 0
    editorial: int = 0
    signal: int = 0
    first_at: datetime | None = None
    latest_at: datetime | None = None
    names: list[str] = field(default_factory=list)
    entries: list[dict] = field(default_factory=list)


def row_at(event: Event, now: datetime, *,
           behind: set[str] | None = None,
           source_since: dict[str, datetime] | None = None) -> Row:
    """Everything `heatRows` knows about one story at `now`.

    `behind` names the sources whose newest evidence may be missing; `source_since` says when each
    source was added, so a source younger than the earlier window can be left out of the change
    (it widens what is seen; it adds to the heat, not to its growth).
    """
    behind = behind or set()
    source_since = source_since or {}
    moment = now - timedelta(hours=TREND_BACK_HOURS)
    now_from = now - timedelta(hours=WINDOW_HOURS)
    prev_from = moment - timedelta(hours=WINDOW_HOURS)

    seen: dict[str, list[datetime]] = {}
    for item in event.items:
        published = materials.moment(item.discovery.published_at)
        if published > now or published <= prev_from:
            continue
        seen.setdefault(item.discovery.source_id, []).append(published)

    row = Row()
    for source_id, times in seen.items():
        latest = max(times)
        first = min(times)
        old = [time for time in times if time <= moment]
        old_latest = max(old) if old else None
        was_behind = source_id in behind
        late = bool(source_since.get(source_id) and
                    source_since[source_id] > prev_from)
        if latest > now_from:
            row.participants += 1
            row.heat += 0.5 ** (age_hours(latest, now) / HALF_LIFE_HOURS)
            if was_behind:
                row.behind += 1
            if editorial(source_id):
                row.editorial += 1
            else:
                row.signal += 1
            if first > moment:
                row.recent += 1
            if row.first_at is None or first < row.first_at:
                row.first_at = first
            if row.latest_at is None or latest > row.latest_at:
                row.latest_at = latest
        if old_latest is not None and old_latest > prev_from:
            row.heat_prev += 0.5 ** (age_hours(old_latest, moment) / HALF_LIFE_HOURS)
        if latest > now_from or old_latest is not None:
            if was_behind or late:
                row.uncomparable += 1
            else:
                row.comparable += 1
                if latest > now_from:
                    row.heat_obs += 0.5 ** (age_hours(latest, now) / HALF_LIFE_HOURS)
                if old_latest is not None and old_latest > prev_from:
                    row.heat_prev_obs += 0.5 ** (age_hours(old_latest, moment)
                                                 / HALF_LIFE_HOURS)
    return row


def value_at(event: Event, when: datetime) -> float:
    """The event's heat measured at `when`: one party, one vote, discounted by then-age."""
    return sum(0.5 ** (age_hours(published, when) / HALF_LIFE_HOURS)
               for published in observations(event, when).values())


def participants(event: Event, now: datetime) -> int:
    """这一轮里还在场的独立来源数. 入榜门槛与 surge 的分子都是它."""
    return len(observations(event, now))


def eligible(event: Event, now: datetime) -> bool:
    """够不够进热点榜: 至少两家, 而且至少一家是发布源 (AIHOT 的两条入榜条件)."""
    latest = observations(event, now)
    if len(latest) < MIN_PARTICIPANTS:
        return False
    return sum(1 for source_id in latest if editorial(source_id)) >= MIN_EDITORIAL_PARTICIPANTS


def score(event: Event, now: datetime) -> float:
    """The heat the views rank on. Kept as the old name because an issue sorts on it too."""
    return value_at(event, now)


def index(heat: float) -> float:
    """The 热度指数 the page prints, from the same number the ranking used.

    AIHOT 的 `heatIndex`: 10 倍值, 一位小数. 与排行同源而不同单位 —— 排序用原值, 印出来的是它.
    """
    return round(heat * INDEX_SCALE, 1)


def series(event: Event, now: datetime,
           points: int = SPARK_HOURS, step_hours: float = SPARK_STEP_HOURS) -> list[float]:
    """The last 24 hours of this event's heat, oldest point first, one point per hour."""
    step = timedelta(hours=step_hours)
    return [index(value_at(event, now - step * (points - 1 - i))) for i in range(points)]


def trend_pct(row: Row) -> float | None:
    """Percent change against six hours ago, one decimal (AIHOT 的 `trendPct`).

    `None` = 那时它还不在, 页面写「新」. 比的是**可比**那一组 (AIHOT: 有人不可比时改用
    `heat_obs` / `heat_prev_obs`), 否则今天刚接入的一家会把涨幅算成几百个点.
    """
    current, past = ((row.heat_obs, row.heat_prev_obs) if row.uncomparable
                     else (row.heat, row.heat_prev))
    if past <= 0:
        return None
    return round((current - past) / past * 1000) / 10


def trend_state(row: Row, pct: float | None) -> str:
    """五态走势 (AIHOT 的 `trend`): new / unknown / up / down / flat."""
    if row.heat_prev <= 0:
        return "new"
    if pct is None:
        return "unknown"
    if pct > TREND_FLAT * 100:
        return "up"
    if pct < -TREND_FLAT * 100:
        return "down"
    return "flat"


def recent(event: Event, now: datetime,
           back_hours: float = TREND_BACK_HOURS) -> tuple[int, int]:
    """最近 `back_hours` 小时里的 (第一次出现的独立来源数, 新增条目数).

    "第一次出现"看的是这一家在这件事上最早的那条, 不是任意一条的发布时间: 一家昨天说过、今天又
    说了一次, 今天这次不算新面孔, 否则一条被反复转载的旧闻会一直显示"刚刚有人加入".
    """
    since = now - timedelta(hours=back_hours)
    earliest: dict[str, datetime] = {}
    fresh_items = 0
    for item in event.items:
        published = materials.moment(item.discovery.published_at)
        source_id = item.discovery.source_id
        if source_id not in earliest or published < earliest[source_id]:
            earliest[source_id] = published
        if published >= since:
            fresh_items += 1
    return sum(1 for first in earliest.values() if first >= since), fresh_items


def source_names(event: Event, now: datetime,
                 cap: int = SOURCE_NAMES_MAX) -> list[str]:
    """The publishing sources of the window, most recently active first (AIHOT 的 `sourceNames`).

    Only 发布源: 讨论帖的名字排在名单里会让人以为那也是个"在报道的媒体".
    """
    latest = observations(event, now)
    ordered = sorted((published, source_id) for source_id, published in latest.items()
                     if editorial(source_id))
    names: list[str] = []
    for _published, source_id in reversed(ordered):
        name = registry.of(source_id).name
        if name not in names:
            names.append(name)
    return names[:cap]


def badges(event: Event, now: datetime, pct: float | None = None) -> list[str]:
    """标题上那几枚小标签 (AIHOT 的 hot badges): `surge` / `new` / `rising`.

    三条按 AIHOT 的写法各自判、各自印, 前两条可以同时成立 (`surge` + `new`: 刚发生又被三家
    同时跟进); `rising` 只在不是 surge 的时候印 —— 半路起量已经说过一遍"在涨"。

    最后一条读涨跌幅, 阈值 15%: 更小的波动不值得标.
    """
    count = participants(event, now)
    fresh, _ = recent(event, now)
    out: list[str] = []
    if fresh >= SURGE_MIN_NEW and count and fresh / count >= SURGE_SHARE:
        out.append("surge")
    if age_hours(materials.moment(event.first_seen_at), now) < NEW_HOURS:
        out.append("new")
    if "surge" not in out:
        if pct is None:
            pct = trend_pct(row_at(event, now))
        if pct is not None and pct > RISING_PCT * 100:
            out.append("rising")
    return out


def annotate(events: list[Event], now: datetime, *,
             behind: set[str] | None = None,
             source_since: dict[str, datetime] | None = None) -> list[Event]:
    """Fill the display numbers on every event, in place."""
    for event in events:
        row = row_at(event, now, behind=behind, source_since=source_since)
        pct = trend_pct(row)
        event.heat = row.heat
        event.heat_index = index(row.heat)
        event.series = series(event, now)
        event.trend = trend_state(row, pct)
        event.trend_pct = pct
        event.recent_sources, event.recent_reports = recent(event, now)
        event.participants_editorial = row.editorial
        event.participants_signal = row.signal
        event.badges = badges(event, now, pct)
    return events
