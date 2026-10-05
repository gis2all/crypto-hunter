"""三个页面要的东西, 从同一份条目层切出来.

One ingest, three views - and the views are pure functions of (sources, now). Nothing here
writes anything and nothing here is stored: 精选, 全部动态 and 热点榜 are three questions asked
of the same items, which is exactly why they cannot drift from each other.

  全部动态  窗口里的每一条, 按天分组        scope=all
  精选      同样的一批, 分数过线的那些       scope=curated
  热点榜    同样的一批, 先聚成事件再按热度排  /feed/hot

落库的那条路 (service.run_once / editions) 不在这里: 一条刊是"选了哪几条", 是这三个视图
的下游, 见 DESIGN.md 8.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from urllib.parse import urlparse

from ..store import db
from . import analysis, eligibility, heat, htmlbody, materials, media, rules, scope, site, stories, taxonomy, writing
from .lib import calendar as cal
from .lib import cursor as cursorlib
from .lib import text as textlib
from . import events as feed_events
from .models import Discovery, Event, Item
from .sources import read_window, registry
from .sources.snapshot import read_written_window

#: 全部动态与精选读多久. 三天足够"从今天往回翻", 又不至于把陈年旧闻混进精选.
STREAM_HOURS = 72.0
#: 热点榜读多久. 比上面短: 一个还挂在榜上的东西, 不该靠四天前的旧流量撑着.
HOT_HOURS = 48.0

#: 主题页读多久. 比上面两个都长: 一条线是"这个机构/这条赛道的轨迹", 72 小时的流水看不出轨迹.
#: 与热点榜相反的是: 它短是为了只留下还在动的东西, 而主题要的正是"不动的那批也算数".
#: 两边要的是相反的东西, 所以不共用一个数.
TOPIC_HOURS = 720.0
#: 精选的下限. 这个数定义在 `analysis.py` (分数在那里算), 这里只是把它读出来 —— 入库时的
#: 写作站按同一条线决定要不要译正文, 两处共用一个数才不会分家.
CURATED_MIN_SCORE = analysis.CURATED_MIN_SCORE
#: 热点榜给几条. 页面自己再切 1 / 2-3 / 4-10.
HOT_LIMIT = 10

def time_label(value: str) -> str:
    """HH:MM 北京. 一条列表里的东西只需要"几点", 日期在分组头上."""
    return cal.beijing_time(value)


def stamp_label(value: str) -> str:
    """2026-10-03 11:37 北京. 详情页左栏印绝对时刻, 事件头上印相对时间 —— 两个问题."""
    return textlib.full_datetime(value)


def ago_label(value: str, now: datetime) -> str:
    """多久以前. 未来时间戳读成"刚刚": 源钟快了一点和它真的在未来, 页面上不该有两种样子."""
    minutes = max(int((now - materials.moment(value)).total_seconds() // 60), 0)
    if minutes < 1:
        return "刚刚"
    if minutes < 60:
        return "%d 分钟前" % minutes
    hours = minutes // 60
    if hours < 24:
        return "%d 小时前" % hours
    return "%d 天前" % (hours // 24)


def evidence(item: Item) -> bool:
    """这一条是**讨论证据**, 不是报道 (AIHOT 的 `participation_mode = hot_signal`)。

    X 上的监测账号这类源只往别人立好的那件事上挂一条证据、给热度投一票, 它们不立事件、也不该
    印成正文: 一条几十字的推文当"报道"印出来, 读者看到的就是一条没有内容的新闻。热度那一半
    (`heat.observations` 读 `event.items`) 照样数它 —— 过滤只发生在印出来的那几个列表上。
    """
    return registry.of(item.discovery.source_id).participation == "hot_signal"


def domain_of(url: str) -> str:
    """来源域名, 例如 coindesk.com. 拆 URL 是后端的活, 页面不该为了印四个字母去 parse 一遍."""
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def date_label(value: str) -> str:
    """10月3日. 中文的日期不写成 10/03: 那是另一套排版."""
    stamp = cal.beijing_date(value)
    _, month, date = (int(part) for part in stamp.split("-"))
    return "%d月%d日" % (month, date)


def weekday_label(value: str) -> str:
    return "星期%s" % cal.beijing_weekday(value)


def collect(now: datetime, hours: float) -> tuple[list[Item], list[Event]]:
    """Read every source once and hand back (窗口里的条目, 这些条目组成的事件).

    The window is applied to raw records before grouping for the reason service.run_once does
    the same thing in the same order: an event whose first word is inside the window but whose
    follow-ups are not is still one event, and cutting after grouping would have to decide
    which half of it to keep.
    """
    raw: list[Discovery] = read_window(now, hours)

    unique = materials.dedupe(raw)
    inside = [item for item in unique
              if materials.in_window(item, now - timedelta(hours=hours), now)]
    # 还没写出中文标题与摘要的条目**先不印** —— 这是 AIHOT 的发布门槛: 一条要么有中文标题与
    # 摘要, 要么不出现在页面上, 页面不该混着中英两种半成品. 写作站关掉时 (没配模型, maxItems 是
    # 0, 或者这一档预设根本不写条目 —— 节省档) 门槛跟着关掉, 否则开发机上信息流会安静地变成
    # 空页 (writing.writes)。
    written = read_written_window(now, hours)
    if writing.writes():
        inside = [item for item in inside
                  if (item.source_id, item.external_id) in written]
    names = materials.learn_aliases(inside, ())
    # 历史不是新闻 (AIHOT 的 `isHistorical`): 一条被翻出来的旧闻照常列出来、照常点得开, 但它不
    # 进今天的事件图 —— 进去了它就会以"今天第一次出现"的身份和今天的事抢位置, 还能凑够两家把
    # 一件旧事顶进热点榜. 判据是"发现时间比源自己说的时间晚了两天" (materials.is_historical).
    live = [item for item in inside if not materials.is_historical(item)]
    history = [item for item in inside if materials.is_historical(item)]
    grouped = feed_events.group(live, names)
    if history:
        grouped = grouped + feed_events.group(history, names)
    story_digests = stories.digests(now - timedelta(hours=hours))
    # 已经由分组站归好的条目，按库里那件事重新并桶；模型没跑过（或还没轮到）的留在规则桶里 ——
    # 一个窗口里总会有几条没轮到的，让它们掉出去等于页面忽然少几条，而它们并没有做错什么
    # (feed/stories.rebucket)。
    grouped = stories.rebucket(grouped, stories.membership(now - timedelta(hours=hours)))
    analysis.annotate(grouped, now)
    # 落后源与"这家什么时候接入的"由库里的源表说 (AIHOT 的 `sourceClocks`): 一个刚刚才接入的
    # 来源会把今天的涨幅算成几百个点, 一个正在落后的来源会让今天看着比昨天冷 —— 两者都不该进入
    # 涨幅那一比. 与后端那趟是同一个判据, 所以页面上的箭头和入库的曲线不会分家.
    clocks = heat.source_clocks(db.feed_source_clocks())
    heat.annotate(grouped, now,
                  behind=heat.clocks_behind(clocks, now, grace=True),
                  source_since={c["id"]: c["since"] for c in clocks if c.get("since")})
    # 规则版先填, 模型写过的再盖上去. 顺序不能反: 盖完再跑 `annotate`, 规则版会把模型写的字
    # 整个盖掉 —— 而那一趟是每个请求都要走的 (feed/writing.py 的 `overlay`).
    writing.overlay(grouped, written)

    # 综述站写过的那些事排在**最后**: 写作站的 overlay 会把事件标题换成"第一个说它的人的中文
    # 标题", 而一件事的名字该是模型写的那条综述标题 —— 顺序反了, 综述的标题永远盖不上去, 而且
    # 完全静默。标题只在模型真给了的时候才换: 一个空标题的事件在页面上没有名字。
    # 综述站写过的那些事: 模型写的标题 / 综述 / 最新进展盖在规则版上面 (与写作站的 overlay 同
    # 一条理由 —— 规则版每个请求都要跑一遍, 顺序反了它会把模型写的字整个盖掉)。标题只在模型真
    # 给了的时候才换: 一个空标题的事件在页面上没有名字。
    for event in grouped:
        row = story_digests.get(event.identity_key)
        if not row:
            continue
        if row["title"]:
            event.title = row["title"]
        event.summary = [row["digest"]]
        if row["latest"]:
            event.latest = row["latest"]
        event.status = row["status"]

    # 复合分析 (4.9b): 盘点 / 日报这一类把几件事装在一起的报道**不立事件** —— 它对那些事
    # 只是"提及", 不是任何一件的证据 (AIHOT 的 `evidenceCondition` 把 composite 排除在外)。
    # 它照常出现在**全部动态**里: 那是一篇真被发表过的东西, 只是它不是一件事的发生。
    # 抽取放在 overlay 之后: 档位是入库那一刻定的, 读的正是 overlay 盖进来的那一格。
    composite = [item for event in grouped for item in event.items
                 if item.analysis_scope == "composite"]
    if composite:
        grouped = [event for event in grouped
                   if not all(item.analysis_scope == "composite" for item in event.items)]
    # 只留下真报道的那些事: 一件只剩讨论证据的事不是"事件", 它的成员一条都没被印出来, 卡片
    # 会是一张没有正文的空卡。
    grouped = [event for event in grouped if any(not evidence(item) for item in event.items)]
    items = sorted((item for event in grouped for item in event.items if not evidence(item)),
                   key=lambda item: materials.moment(item.discovery.published_at),
                   reverse=True)
    if composite:
        items = sorted(items + [item for item in composite if not evidence(item)],
                       key=lambda item: materials.moment(item.discovery.published_at),
                       reverse=True)
    return items, grouped


#: 卡片上那行折叠能列出来的上限. 折叠行印的是**总数**, 列表只是给个去处, 所以截断不撒谎.
OTHER_REPORTS_LIMIT = 5


def in_channel(item: Item, channel: str) -> bool:
    """这一条属不属于读者选的那一路 (AIHOT 的 `CHANNEL_KEYS`)。

    `all` 是“不过滤”, `firstParty` 问的是“谁在说”, `news`/`x` 问的是“走哪一路” ——
    三个问题共用一个筛选键, 判据只在这里写一遍。
    """
    if channel in ("", "all"):
        return True
    if channel == "firstParty":
        return item.first_party
    source = registry.of(item.discovery.source_id)
    return rules.channel_of(source.kind, bool(item.discovery.quoted)) == channel


def other_reports(event: Event | None, item: Item) -> list[dict]:
    """同一件事里, 别家还怎么说过它 —— 卡片上「另有 N 家信源报道」展开后的那一列.

    按发布时间新到旧, 每**家**只留最新的一条: 折叠行说的是「另有 N 家信源」, 同一家写两遍
    会让那个数对不上. 这一条自己的来源不算 —— 它已经印在卡片顶上了. 没有所属事件就是空表.
    """
    if event is None:
        return []
    others = [other for other in event.items
              if other is not item and other.discovery.source_id != item.discovery.source_id
              and not evidence(other)]
    others.sort(key=lambda other: materials.moment(other.discovery.published_at), reverse=True)
    seen: set[str] = set()
    rows: list[dict] = []
    for other in others:
        if other.discovery.source_id in seen:
            continue
        seen.add(other.discovery.source_id)
        rows.append({
            # id 和列表行同一个拼法 (source:external), 折叠行点标题就能进那一条自己的页.
            "id": "%s:%s" % (other.discovery.source_id, other.discovery.external_id),
            "source_name": registry.of(other.discovery.source_id).name,
            "title": other.discovery.title.strip(),
            "url": other.discovery.url,
            "time_label": time_label(other.discovery.published_at),
        })
    return rows


def display_tags(owner) -> list[dict]:
    """卡片角标那一串, 每一个都带"点不点得动" (AIHOT 的 `displayTags`).

    实体与分类标签原先是两串 (`entities` / `tags`), 但卡片印的是一行角标而不是两行 ——
    这里把两者并成一串, 顺序是"先说牵了谁, 再说是什么". 实体点得动 (去主题页),
    分类标签点不动, 所以每一个都带一个布尔, 页面据此决定要不要给链接。
    """
    out: list[dict] = []
    for name in owner.entities:
        out.append({"text": name, "entity": True})
    for name in owner.tags:
        if name not in owner.entities:
            out.append({"text": name, "entity": False})
    return out


def first_media(item: Item) -> list[str]:
    """卡片上的图 (AIHOT 的 `firstImagePart`): 源自带的优先, 源没给就从正文里抽第一张.

    正文里那张图是入库时抽的 (`sources/collect._media_objects`), 但老行没有 ——
    所以这里再兜一次底: 一张卡片有没有图, 不该由"这一行是哪天入库的"决定。
    """
    urls = htmlbody.media_urls(item.discovery.media)
    if not urls:
        urls = [image["url"] for image in htmlbody.images(item.body_html, limit=1)]
    # 卡片那一格是缩过的规格 (5.9): 一张 4000px 的原图印在 112px 的格子里, 是让每个读者
    # 替我们付流量。取不了的 (data: 之类) 原样还回。
    return [got for got in (media.proxied_image(url, "image-720") for url in urls) if got]


def item_payload(item: Item, story: Event | None) -> dict:
    """One row in a list. Everything the row prints, and nothing it would have to look up."""
    source = registry.of(item.discovery.source_id)
    reports = other_reports(story, item)
    return {
        "id": "%s:%s" % (item.discovery.source_id, item.discovery.external_id),
        "story_id": story.identity_key if story else "",
        "title": item.display_title,
        #: 源自己的标题, 只在它和上面那条**不一样**的时候给 —— 页面据此决定要不要在标题底下多印
        #: 一行灰字. 两样一样时给空串, 免得页面把同一句话印两遍.
        #: X 帖没有这一行: 源标题就是那条推文本身, 中文标题底下再印一遍原文只是噪音
        #: (AIHOT 的 item.tsx 也是这么排除的: `!isX && item.originalTitle`).
        "title_original": (item.discovery.title.strip()
                           if (item.title_zh and item.discovery.source_kind != "x"
                               and item.title_zh != item.discovery.title.strip())
                           else ""),
        #: 模型写过这一条没有. 页面上"AI 导读"这个说法是不是真的, 由它决定.
        "written": bool(item.title_zh),
        "url": item.discovery.url,
        "summary": item.summary,
        "reason": item.reason,
        "category": item.category,
        "category_label": item.category_label,
        "tags": item.tags,
        "entities": item.entities,
        "display_tags": display_tags(item),
        "score": item.score,
        "first_party": item.first_party,
        "source_id": item.discovery.source_id,
        "source_name": source.name,
        "source_kind": source.kind,
        "author": item.discovery.author,
        "published_at": item.discovery.published_at,
        "time_label": time_label(item.discovery.published_at),
        # 图和别家报道随行走: 列表要画, 详情页那一份也从这里长出来 (item_detail 只是往上加字段).
        "media": first_media(item),
        # 这件事别家还怎么说过它 (折叠行的列表). 截断到 OTHER_REPORTS_LIMIT 条, 但计数是
        # **总数** —— 折叠行印的是它, 拿列表长度去印会在截断之后撒谎.
        "other_reports": reports[:OTHER_REPORTS_LIMIT],
        "other_report_count": len(reports),
        #: 这一条走哪一路 (AIHOT 的 `channelOf`): 卡片筛选、发布资格与出口都按它分。
        "channel": rules.channel_of(source.kind, bool(item.discovery.quoted)),
        #: 正文档位 (AIHOT 的 `bodyModeOf`): 清单允许印全文、而且这一版
        #: 确实读到了正文才给 `full`。
        "body_mode": item.body_mode,
        #: 这一条有没有自己的页。
        "has_page": rules.has_item_page(source.visibility, source.participation),
        #: X 这一路的几格 (AIHOT 的 `x_view`): 作者、句柄、带图的正文、引用。
        #: 不是 X 的那些给 None —— 页面据此选一套版式, 而不是猜。
        "x_view": _x_view(item) if rules.channel_of(source.kind, bool(item.discovery.quoted)) == "x" else None,
        #: 分析档位 (4.9b): `primary` / `composite` / 空. 复合的那一条在页面上标「综合」,
        #: 它不立事件 —— 所以卡片看起来和别的行一样, 只是没有"所属事件"。
        "analysis_scope": item.analysis_scope,
        "scope_label": item.scope_label,
    }


def _x_view(item: Item) -> dict:
    """X 这一条在卡片上的几格 (AIHOT 的 `XPostView`)。

    一个字段也不猜: 没有的就是空 —— 一个猜出来的 @句柄会被读者当成真的。
    """
    quoted = item.discovery.quoted or {}
    handle = (item.discovery.author or "").strip()
    out = {
        "author": handle,
        "text": item.summary or item.discovery.title.strip(),
        "original": item.discovery.title.strip(),
        "media": [media.proxied_image(url, "image-720") for url in item.discovery.media],
        "quoted": None,
    }
    if quoted:
        out["quoted"] = {
            "handle": str(quoted.get("handle") or ""),
            "author_name": str(quoted.get("author_name") or quoted.get("authorName") or ""),
            "text": str(quoted.get("text") or ""),
            "translation": str(quoted.get("translation") or ""),
            "url": str(quoted.get("url") or ""),
        }
    return out


def availability(now: datetime, ids: list[str]) -> list[dict]:
    """这些 id 里哪些还在、哪些已经不公开了 (AIHOT 的 `availability.ts`)。

    星标是存在浏览器里的,而一条东西可以在被星标之后下架。页面不能自己猜“它还在吧”
    —— 一张点不开的卡片比一张不在的卡片更难解释。
    """
    out: list[dict] = []
    for raw in ids[:200]:
        source_id, _, external_id = raw.partition(":")
        row = db.feed_item_get(source_id, external_id) if external_id else None
        if row is None:
            out.append({"id": raw, "exists": False, "listed": False, "title": "", "url": ""})
            continue
        out.append({"id": raw, "exists": True, "listed": scope.listed(row, now),
                    "title": row.get("title_zh") or row.get("title") or "",
                    "url": site.absolute("/")})
    return out


def faces(event: Event, limit: int = 5) -> list[dict]:
    """这件事的参与者头像 (AIHOT 的 `Faces.tsx`): 每家一枚, 最多 `limit` 枚。

    没图标时给首字母 —— 页面不猜“这一家叫什么”, 一个空头像和一个白块在
    列表里长得一样, 而“谁在说这件事”正是这一栏要回答的。
    """
    seen: set[str] = set()
    out: list[dict] = []
    for item in sorted(event.items,
                       key=lambda item: materials.moment(item.discovery.published_at),
                       reverse=True):
        source_id = item.discovery.source_id
        if source_id in seen:
            continue
        seen.add(source_id)
        source = registry.of(source_id)
        out.append({"source_id": source_id, "name": source.name,
                    "initial": textlib.source_initial(source.name),
                    "icon": getattr(source, "icon", "") or ""})
        if len(out) >= limit:
            break
    return out


def story_payload(event: Event, now: datetime) -> dict:
    """One ranked story: its numbers, its line, and who is saying it."""
    reports = [item for item in event.items if not evidence(item)]
    lead = registry.of(reports[0].discovery.source_id) if reports else None
    return {
        "item_count": len(reports),
        "faces": faces(event),
        "id": event.identity_key,
        "title": event.title,
        "url": event.url,
        "latest": event.latest,
        "category": event.category,
        "category_label": event.category_label,
        "tags": event.tags,
        "entities": event.entities,
        "display_tags": display_tags(event),
        "score": event.score,
        "sources": event.sources,
        "participants": event.participants_editorial + event.participants_signal,
        "source_count": event.participants_editorial,
        "signal_count": event.participants_signal,
        "source_names": heat.source_names(event, now),
        "heat_index": event.heat_index,
        "series": event.series,
        "trend": event.trend,
        "trend_pct": event.trend_pct,
        "hot_badges": event.badges,
        "first_seen_at": event.first_seen_at,
        "last_seen_at": event.last_seen_at,
        "lead_source": lead.name if lead else "",
        "items": [
            {
                "source_name": registry.of(item.discovery.source_id).name,
                "first_party": item.first_party,
                "title": item.display_title,
                "url": item.discovery.url,
                "time_label": time_label(item.discovery.published_at),
            }
            for item in sorted(reports,
                               key=lambda item: materials.moment(item.discovery.published_at),
                               reverse=True)
        ],
    }


def by_day(items: list[Item], story_of: dict[int, Event]) -> list[dict]:
    """The list as the page draws it: one group per local date, newest first.

    Grouping happens here rather than in the frontend for the reason every other word on the
    page is written here: "10月3日 星期六" is this language's way of writing a date, and a
    second place that formats one is a second place that can disagree (CLAUDE.md 2.3.3).
    """
    groups: dict[str, list[Item]] = {}
    for item in items:
        groups.setdefault(cal.beijing_date(item.discovery.published_at), []).append(item)
    return [
        {
            "date": key,
            "date_label": date_label(rows[0].discovery.published_at),
            "weekday_label": weekday_label(rows[0].discovery.published_at),
            "count": len(rows),
            "items": [item_payload(item, story_of.get(id(item))) for item in rows],
        }
        for key, rows in sorted(groups.items(), reverse=True)
    ]


def timeline_item(item: Item) -> dict:
    """事件时间线上的一行: 谁在什么时候说了这条, 点进去就是它的详情页."""
    source = registry.of(item.discovery.source_id)
    return {
        "id": "%s:%s" % (item.discovery.source_id, item.discovery.external_id),
        "title": item.display_title,
        "url": item.discovery.url,
        "source_name": source.name,
        "first_party": item.first_party,
        "category_label": item.category_label,
        "score": item.score,
        "time_label": time_label(item.discovery.published_at),
    }


#: 同一家连着说、间隔不超过多久就算"一口气发的" (4.10).
TIMELINE_FOLD_HOURS = 6.0


def _timeline_id(item: Item) -> str:
    return "%s:%s" % (item.discovery.source_id, item.discovery.external_id)


def _fold_timeline(rows: list[Item]) -> list[dict]:
    """同源连发几条时怎么收 (4.10): 相邻的同一家合成一行.

    时间线上"这家一口气发了四条"不是四个进展, 是一个进展被四次播报 —— 四条各占一行
    会把真正的新进展挤下去. 收成一行, `folded` 就是被收起来的条数 (读者知道那底下还有),
    `ids` 是那几条的 id —— 列表删了它们, 但点得进去。

    只在**相邻**之间合: 中间插了别家就断开, 所以它不会把"上午一条、晚上一条"
    也说成一口气发的; 间隔也要在 `TIMELINE_FOLD_HOURS` 以内。
    """
    out: list[dict] = []
    for item in rows:
        moment = materials.moment(item.discovery.published_at)
        if out:
            top = out[-1]
            same = top["item"].discovery.source_id == item.discovery.source_id
            close = (top["moment"] - moment) <= timedelta(hours=TIMELINE_FOLD_HOURS)
            if same and close:
                top["row"]["folded"] += 1
                top["row"]["ids"].append(_timeline_id(item))
                continue
        out.append({"moment": moment, "item": item,
                    "row": dict(timeline_item(item), folded=0, ids=[_timeline_id(item)])})
    return [entry["row"] for entry in out]


def story_days(event: Event) -> list[dict]:
    """这件事的报道按天分组, 新的在前. 和列表页同一个理由分组在后端: 日期是这套语言的写法."""
    rows = sorted((item for item in event.items if not evidence(item)),
                  key=lambda item: materials.moment(item.discovery.published_at),
                  reverse=True)
    groups: dict[str, list[Item]] = {}
    for item in rows:
        groups.setdefault(cal.beijing_date(item.discovery.published_at), []).append(item)
    return [
        {
            "date": key,
            "date_label": date_label(rows[0].discovery.published_at),
            "weekday_label": weekday_label(rows[0].discovery.published_at),
            "count": len(rows),
            "items": _fold_timeline(rows),
        }
        for key, rows in sorted(groups.items(), reverse=True)
    ]


#: 一件事现在算哪一档 (digest.status_for). 词在后端, 理由同 LABELS: 它是"多久没动"这条规则的
#: 翻译, 阈值改了词要跟着改.
STATUS_LABELS = {"active": "持续更新", "watching": "观察中", "settled": "历史事件"}


def badges(event: Event, now: datetime) -> list[str]:
    """标题上那两枚小标签. 由规则定, 所以由后端给字 —— 页面看不到"几家在说"这件事."""
    out = ["热点事件" if event.sources > 1 else "单源报道"]
    # 综述站写过就按它那一档 (24/72 小时), 没写过就按"最近 6 小时有没有新报道" —— 旧规则不是
    # 第二套答案, 是同一件事在还没有状态时能算出的那一半.
    label = STATUS_LABELS.get(event.status, "")
    if not label and heat.age_hours(materials.moment(event.last_seen_at),
                                    now) <= heat.TREND_BACK_HOURS:
        label = STATUS_LABELS["active"]
    if label:
        out.append(label)
    return out


#: 详情页的目录 (AIHOT 的 `ItemDetail.outline`) 少于两条就不画: 一条目录比正文还短时, 它
#: 不是导航, 是多出来的一行.
OUTLINE_MIN = 2

#: 事件页的「官方报道」 (AIHOT 的 `officialReports`) 截这么多条 —— 一页印不下更多.
OFFICIAL_LIMIT = 12


def _bodies(item: Item) -> tuple[str, str, list[dict]]:
    """正文的两份字, 以及它们的目录.

    两份用**同一套编号**: 译文是照原文的块换的, 小标题在同样的位置, 所以 `sec-3` 在两边指
    的是同一节. 两边标题数对不上就整个不给目录 —— 锚点指向不存在的那一节, 比没有目录更糟.
    """
    original, outline = htmlbody.with_outline(item.body_html)
    translated, outline_zh = htmlbody.with_outline(item.body_zh) if item.body_zh else ("", [])
    if item.body_zh and len(outline_zh) != len(outline):
        return original, translated, []
    if len(outline) < OUTLINE_MIN:
        return original, translated, []
    return original, translated, outline


def official_reports(event: Event) -> list[dict]:
    """这件事里**当事方自己**发的报道, 最新在前 (AIHOT 的 `officialReports`).

    `other_reports` 收的是别家还怎么说过它, 两边各按 `first_party` 滤一次, 所以同一条不会
    在两处都出现. 被判定为"不是证据"的那些照旧不进 —— 事件页只印证据.
    """
    rows = [item for item in event.items if item.first_party and not evidence(item)]
    rows.sort(key=lambda item: materials.moment(item.discovery.published_at), reverse=True)
    return [
        {
            "id": "%s:%s" % (item.discovery.source_id, item.discovery.external_id),
            "source_name": registry.of(item.discovery.source_id).name,
            "title": item.display_title,
            "url": item.discovery.url,
            "time_label": time_label(item.discovery.published_at),
        }
        for item in rows[:OFFICIAL_LIMIT]
    ]


def item_detail(item: Item, story: Event | None, now: datetime) -> dict:
    """一条的整页: 列表那一行的全部字段, 加上只有详情页才看得见的那几样.

    形状是"列表行 + 追加", 不是另一份平行定义 —— 两条路各写一套, 迟早会有一边多一个字段.
    """
    body_html, body_zh, outline = _bodies(item)
    return {
        **item_payload(item, story),
        # 正文的两份字 (原文 / 译文). `body_complete` 只在真有译文时才有意义: 没译过的那一条
        # 印的是原文, 没有"半篇"这回事. 两份都注过锚点, 目录那一栏才点得动.
        "body_html": body_html,
        "body_zh": body_zh,
        #: 正文的目录 (AIHOT 的 `outline`): 小标题的锚点、文字与层级. 空 = 这一篇没有可跳的
        #: 节, 页面就不画那一栏.
        "outline": outline,
        "body_complete": bool(item.body_zh) and item.body_complete,
        "domain": domain_of(item.discovery.url),
        "published_label": stamp_label(item.discovery.published_at),
        "published_ago": ago_label(item.discovery.published_at, now),
        #: 桶里只有它自己那一条时不画「所属事件」—— 那个方块会把本文标题重印一遍, 读到的
        #: 是一件只属于它自己的事 (AIHOT 的详情页同理: 后续列表先把自己滤掉, 空了整块不画).
        #: `story_id` 照给, 那是页面跳转用的.
        "story_title": story.title if story and len(story.items) > 1 else "",
        #: 「事件后续」(AIHOT 的 `StoryFollowups.tsx`): 这件事别家还怎么说过它, 最新在前.
        #: 和卡片上那行折叠是**同一份** `other_reports` —— 两处各查一遍, 迟早会差一条.
        "followups": other_reports(story, item),
        "followup_count": len(other_reports(story, item)),
        #: 这条盘点**提及**了哪几件事 (4.9b): 提及不是证据, 所以它们不在 followups 里,
        #: 单独一格, 页面按"被它提到过"印。
        "mentions": db.feed_item_mentions_of(item.discovery.source_id, item.discovery.external_id),
        # 这一条为什么够格 / 卡在哪一道 (5.5): 唯一的资格名单在 `eligibility.py`, 页面只印它。
        "eligibility": eligibility.explain_item(item, registry.of(item.discovery.source_id), now),
    }


def item_markdown(item: Item, story: Event | None, now: datetime) -> str:
    """这一条的 Markdown 导出 (AIHOT 的 `5.17`): 和页面同一套可见性与许可规则。

    正文能不能出全文由 `body_mode` 说 —— 一个只许印摘要的源, 导出的那一份也只有摘要.
    导出不是第二个出口, 所以它不自己判一遍.
    """
    source = registry.of(item.discovery.source_id)
    lines = ["# %s" % item.display_title, ""]
    lines.append("- 来源：%s" % source.name)
    lines.append("- 时间：%s" % stamp_label(item.discovery.published_at))
    lines.append("- 原文：%s" % item.discovery.url)
    if story:
        lines.append("- 所属事件：%s" % story.title)
    lines.append("")
    lines.append("## 摘要")
    lines.append("")
    lines.append(item.summary or "")
    if item.body_mode == "full" and item.body_html:
        lines.append("")
        lines.append("## 正文")
        lines.append("")
        lines.append(htmlbody_markdown(item.body_html))
    lines.append("")
    return "\n".join(lines)


def htmlbody_markdown(body_html: str) -> str:
    """把净化过的块级 HTML 转成 Markdown。块只有 p / h2 / h3 / ul / li / blockquote 这几种
    (htmlbody 那一层的产物), 所以转换是有限的一张小表, 不引第三方库。
    """
    import re
    text = body_html or ""
    text = re.sub(r"<h2[^>]*>(.*?)</h2>", r"## \1", text, flags=re.S)
    text = re.sub(r"<h3[^>]*>(.*?)</h3>", r"### \1", text, flags=re.S)
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"- \1", text, flags=re.S)
    text = re.sub(r"<blockquote[^>]*>(.*?)</blockquote>", r"> \1", text, flags=re.S)
    text = re.sub(r"</p>\s*<p[^>]*>", "\n\n", text)
    text = re.sub(r"</(p|ul|ol)>|<(p|ul|ol)[^>]*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def related_stories(event: Event, now: datetime, limit: int = 8) -> list[dict]:
    """那一栏「关联事件」(AIHOT 的 `relatedStories`): 对面还活着、还有人在报, 最多 8 条.

    只有库里有整数 id 的事才查得到边 (规则桶还没有边); 名字与关系词都由后端给 —— "同一故事线"
    是 `relation` 这个枚举的翻译, 页面不认识那个枚举 (CLAUDE.md 2.3.3).
    """
    raw = event.identity_key.split(":", 1)[1] if ":" in event.identity_key else ""
    if not raw.isdigit():
        return []
    out: list[dict] = []
    for row in db.feed_story_links_for(int(raw), limit):
        relation = row["relation"] or "related"
        out.append({
            "id": "%s%d" % (stories.STORY_PREFIX, row["other_id"]),
            "title": row["title"],
            "relation": relation,
            "relation_label": LABELS.get(relation, ""),
            "last_seen_ago": ago_label(row["last_at"], now),
        })
    return out


def story_detail(event: Event, rank: int, now: datetime) -> dict:
    """一件事的整页: 热点榜那张卡的全部字段, 加上详情页要的.

    `items` 留着 (卡上那份"谁在说"), `days` 是同一批报道按天的分组 —— 两个投影来自同一份
    `event.items`, 算在同一个函数里, 所以不可能对不上; 页面读 `days`.
    """
    base = story_payload(event, now)
    base.update({
        "rank": rank,
        "badges": badges(event, now),
        "summary": event.summary,
        #: 当事方自己发的那些 (AIHOT 的 `officialReports`): 事件页单独一栏"谁在说"它们的原话.
        "official_reports": official_reports(event),
        "recent_sources": event.recent_sources,
        "recent_reports": event.recent_reports,
        "related": related_stories(event, now),
        "first_seen_ago": ago_label(event.first_seen_at, now),
        "last_seen_ago": ago_label(event.last_seen_at, now),
        "days": story_days(event),
    })
    return base


def ranked_stories(now: datetime) -> list[Event]:
    """热点榜的次序与门槛, 只此一处. 榜单和详情页的"榜内第 N 名"都读它, 所以两个数不会分家."""
    _, stories = collect(now, HOT_HOURS)
    ranked = sorted(stories, key=lambda event: (-event.heat, event.first_seen_at))
    # 入榜的两条都由 `heat.eligible` 说 (AIHOT 的 `MIN_PARTICIPANTS` 与 `>=1 发布源`): 至少两家
    # 现在还在说它, 而且其中至少一家是发布源. 没有"够精选就放行"这条后门 —— 一条只有一家在说的
    # 东西, 无论分多高都不是热点. 单选过的条目走它自己的位置, 不占热点榜.
    return [event for event in ranked if heat.eligible(event, now)]


def find_story(now: datetime, story_id: str) -> tuple[Event, int] | None:
    """一个事件和它在热点榜上的名次 (1 起). 不在榜上的事, 详情页就不该有排名."""
    story_id = _live_story(story_id)
    for index, event in enumerate(ranked_stories(now)):
        if event.identity_key == story_id:
            return event, index + 1
    return None


def _live_story(story_id: str) -> str:
    """`s:<id>` 指向的事如果被合并过, 跟到还活着的那一头 (AIHOT 的 `liveStory`)。

    合并之后成员都挂到保留那件事上了, 旧 id 在读取侧就没有对应的桶 —— 一条已经贴出去
    的链接会变成 404, 而它只是换了名字。
    """
    if not story_id.startswith(stories.STORY_PREFIX):
        return story_id
    raw = story_id[len(stories.STORY_PREFIX):]
    if not raw.isdigit():
        return story_id
    return "%s%d" % (stories.STORY_PREFIX, db.feed_story_leader(int(raw)))


def find_item(now: datetime, item_id: str) -> tuple[Item, Event | None] | None:
    """一条和它所属的事件. 事件可能没有 —— 条目在窗口里而它那件事被挤出去了."""
    items, stories = collect(now, STREAM_HOURS)
    story_of = {id(item): story for story in stories for item in story.items}
    for item in items:
        if "%s:%s" % (item.discovery.source_id, item.discovery.external_id) == item_id:
            return item, story_of.get(id(item))
    return None


def page_key(item: Item) -> tuple[str, str]:
    """一条东西在列表次序上的坐标 (发布时间, id)。游标带的是它, 不是 OFFSET。"""
    return (materials.moment(item.discovery.published_at).isoformat(timespec="seconds"),
            "%s:%s" % (item.discovery.source_id, item.discovery.external_id))


def matches_query(item: Item, query: str) -> bool:
    """列表里的搜索: 标题、摘要、来源名三处 (AIHOT 的 `pool.ts` 同一条口径)。"""
    needle = query.strip().lower()
    if not needle:
        return True
    haystack = "%s %s %s" % (item.display_title, item.summary,
                             registry.of(item.discovery.source_id).name)
    return needle in haystack.lower()


def items_view(now: datetime, *, scope: str, category: str = "",
               channel: str = "all", query: str = "",
               page: int = 1, cursor: str = "", page_size: int = 0) -> dict:
    """全部动态 (scope=all) 与精选 (scope=curated) 共用的一份答案.

    分页走游标 (`lib/cursor`): 这一秒新来一条, OFFSET 会让后面的每一页都错位一次,
    而游标读的是"上一页最后那一条之后"的. 没有游标时才退回页码.
    """
    items, stories = collect(now, STREAM_HOURS)
    # 按对象身份关联, 不拼 id: 一个条目在视图里就是一个对象, 拼一遍字符串再拆开, 只是让两处
    # 去约定一个本身没有意义的拼法.
    story_of: dict[int, Event] = {id(item): story for story in stories for item in story.items}

    total = len(items)
    if scope == "curated":
        # 精选由 `item.selected` 说话: 模型评过分按分级门槛算, 没评过才轮到规则分 (writing)。
        items = [item for item in items if item.selected]
    # 渠道计数和类别计数一样, 数在筛选之前: 读者点进 `x` 之后还要看得见别处有多少条.
    channel_counts = Counter(
        "firstParty" if item.first_party
        else rules.channel_of(registry.of(item.discovery.source_id).kind,
                              bool(item.discovery.quoted))
        for item in items)
    channel_counts["all"] = len(items)
    items = [item for item in items if in_channel(item, channel)]
    if query:
        items = [item for item in items if matches_query(item, query)]
    # 筛选栏的计数是"这一类在这批里有几条", 所以数在类别筛选之前: 数完再筛, 否则点进一个
    # 类别, 其它类别的计数全变成 0, 读者就没有回去的路.
    counts = Counter(item.category for item in items)
    if category:
        items = [item for item in items if item.category == category]

    filtered = len(items)
    next_cursor = ""
    size = page_size or 0
    marker = cursorlib.decode(cursor) if cursor else {}
    after = marker.get("after")
    if after:
        boundary = tuple(after)
        items = [item for item in items if page_key(item) < boundary]
    offset = max(0, (page - 1) * size) if (size and page > 1) else 0
    if offset:
        items = items[offset:]
    # `page_size = 0` 是「一次给全」: 它还是今天的默认, 因为页面的按天分组与滚动加载
    # (5.29 的前端一半) 还没接线 —— 现在切开, 读者会安静地少看到一半条目.
    window = items[:size] if size else items
    if size and len(items) > size and window:
        next_cursor = cursorlib.encode({"after": list(page_key(window[-1]))})

    days = by_day(window, story_of)
    return {
        "scope": scope,
        "category": category,
        "channel": channel,
        "query": query,
        "page": max(1, page),
        "page_size": size,
        "next_cursor": next_cursor,
        "window_hours": STREAM_HOURS,
        "total": total,
        "filtered": filtered,
        "shown": len(window),
        "channels": [{"key": key, "count": channel_counts.get(key, 0)}
                     for key in rules.CHANNELS],
        "categories": [
            {"key": entry["key"], "label": entry["label"], "count": counts.get(entry["key"], 0)}
            for entry in taxonomy.CATEGORIES
        ],
        "days": days,
    }



def topics_view(now: datetime) -> dict:
    """主题页左栏的目录: 实体名录按组分段, 每段里每个实体带一个窗口内的条数.

    条数与右栏那一批是**同一个窗口、同一次读** —— 两处都走 `collect(now, TOPIC_HOURS)`,
    所以"左栏写着 10、右栏只列出 9 条"这种事不会发生. 词表来自 `taxonomy.ENTITY_GROUPS`
    与 `ENTITY_TAGS`, 页面一个名字都不认识 (CLAUDE.md 2.3.3).
    """
    items, _ = collect(now, TOPIC_HOURS)
    counts = Counter(name for item in items for name in item.entities)
    return {
        "window_days": TOPIC_HOURS / 24,
        "groups": [
            {"key": group["key"], "name": group["name"],
             "rows": [{"id": name, "name": name, "count": counts.get(name, 0)}
                      for name in group["members"]]}
            for group in taxonomy.ENTITY_GROUPS
        ],
    }


def topic_items_view(now: datetime, topic: str) -> dict:
    """一个实体名下的条目, 按天分组 —— 和列表页同一个形状、同一个 `by_day`.

    词表之外的名字回一个空窗口而不是 404: 读者手上那个连接可能是在更早一份词表上点出来的,
    而"这条线现在没东西"与"这条线不存在"在页面上长得一样 (都是一句空的提示), 不值得让一个
    404 去区分它们.
    """
    items, stories = collect(now, TOPIC_HOURS)
    story_of: dict[int, Event] = {id(item): story for story in stories for item in story.items}
    picked = [item for item in items if topic in item.entities]
    return {
        "topic": topic,
        "window_days": TOPIC_HOURS / 24,
        "shown": len(picked),
        "days": by_day(picked, story_of),
    }


def hot_view(now: datetime, limit: int = HOT_LIMIT) -> dict:
    """热点榜: the same items, clustered into stories and ranked by heat."""
    ranked = ranked_stories(now)
    return {
        "window_hours": HOT_HOURS,
        "generated_at": now.isoformat(timespec="seconds"),
        "total": len(ranked),
        "stories": [story_payload(event, now) for event in ranked[:limit]],
    }


#: 页面印在数字旁边的那几个词. 它们在**后端**而不是在页面里, 理由和类别一样 (CLAUDE.md 2.3.3):
#: 一个词是对一条规则的解释 —— "独立来源"说的是"按 `source_id` 去重之后有几家", 这个解释改了,
#: 词要跟着改, 而页面不认识它. 布局性的标题 (当前热点 / 继续看) 不在其中: 那些是页面自己的版式.
LABELS = {
    "score": "AI 评分",
    "reason": "推荐理由",
    "heat": "热度指数",
    #: 热点榜那三枚小标签. 词在后端 (CLAUDE.md 2.3.3): 它是三条规则的翻译, 规则改了词要跟着改.
    "badge_surge": "起量",
    "badge_new": "新",
    "badge_rising": "升温",
    "sources": "独立来源",
    #: 热点榜那栏印的是**发布源**家数 (AIHOT 的 `sourceCount` = `editorial_participants`):
    #: 讨论帖不是"谁在报这件事", 把它算进"来源"会让一条只有人在聊的事看着像有好几家在报.
    "source_count": "发布来源",
    "first_party": "一手",
    #: 关联事件那一栏的两个说法 (AIHOT 的 `relation`): 「同一故事线」是同一件事的下一集,
    #: 「相关事件」是另一件事, 但读者会想一起看.
    "storyline": "同一故事线",
    "related": "相关事件",
    "latest": "最新进展",
    "new": "新上榜",
    "flat": "持平",
    "unknown": "暂不比较",
    "empty_all": "这个窗口里还没有条目。",
    "empty_curated": "这个窗口里没有够得上精选的东西 —— 空的一页也是答案。",
    "empty_hot": "过去 48 小时里没有形成一件事。",
    #: 30 天写在这句里, 和上面那句写 48 小时同一个道理: 句子是对窗口的解释, 窗口改了这句要跟着改.
    "empty_topic": "这条线在近 30 天里没有东西。",
    #: 详情页那几个词. 版式标题 (事件概览 / 报道时间线 / 热度走势) 不在这里 —— 那些是页面的
    #: 版式, 不是对任何一条规则的翻译.
    #: 「AI 导读」只在字**真的是模型写的**时才是真话 (列表行上的 `written`); 规则版的摘要就是
    #: 正文的第一段, 所以它叫「摘要」. 两个说法都由后端给 (CLAUDE.md 2.3.3).
    "summary": "摘要",
    "summary_ai": "AI 导读",
    #: 正文那一栏的三个说法, 以及译文/原文那个开关上的一半 (另一半复用 `original`).
    "body": "正文",
    "body_zh": "正文 · AI 翻译",
    "body_src": "正文 · 原文",
    "body_incomplete": "译文尚不完整，完整内容请切换到原文。",
    #: 引用块那一栏 (3.6). 里面是**别人的原话**, 所以栏名不能是"正文" —— 那句话不是这条帖子说的.
    "quote": "引用",
    "translated": "中文",
    "original": "原文",
    "story_of": "所属事件",
    "open_original": "打开原文",
    "back": "返回",
    "why_hot": "为什么热",
    "recent_sources": "最近 6 小时新增",
    "recent_reports": "近期报道",
    "rank": "榜内排名",
    "reports": "篇报道",
    "report_sources": "个报道来源",
    "updated": "更新",
    "first_seen": "最早报道",
    "last_seen": "最近更新",
    #: 同一件事里别家怎么说它. `%d` 是**信源家数**而不是条目数 —— "家"字就是这个去重规则,
    #: 所以这句话属于后端而不是页面 (页面只做替换).
    "other_reports": "另有 %d 家信源报道",
}


def taxonomy_view() -> dict:
    """The filter bar's own words, so the page never keeps a second copy of the category list."""
    return {
        "categories": [{"key": entry["key"], "label": entry["label"],
                        "section": entry["section"]}
                       for entry in taxonomy.CATEGORIES],
        "topics": [name for name, _ in taxonomy.TOPIC_TAGS],
        "entities": [name for name, _ in taxonomy.ENTITY_TAGS],
        "labels": LABELS,
    }
