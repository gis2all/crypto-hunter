"""站点的对外出口：sitemap / llms.txt / RSS / og / agent / v1 快照。

AIHOT 把这些分散在 `publication/{agent,v1,feeds,og,links}.ts` 与 `routes/*` 里，
我们收在一个模块：它们问的是同一件事 —— “站外那一个读者能拿到什么”，
而这件事的答案只有一个（可见性 + 发布门 + 许可，见 `rules.py` / `scope.py`）。
分成五个文件之后，五处各写一遍可见性判断，迟早有一处漏一条。

这一层只读，不写库。落库的那条路在 `service.py`。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from xml.sax.saxutils import escape

from ..store import db
from . import htmlbody, media, rules, scope, site, views
from .lib import calendar as cal
from .sources import registry

#: 一个出口最多给多少条。RSS 与 v1 快照不是全量镜像 —— 全量走分页 API。
OUTLET_LIMIT = 60


def site_meta() -> dict:
    """站点元信息 (AIHOT 的 `site/meta.ts`)。页面与文档都读它, 不各自写一遍。"""
    return {
        "name": site.NAME,
        "name_zh": site.NAME_ZH,
        "industry": site.INDUSTRY,
        "tagline": site.TAGLINE,
        "contact_email": site.CONTACT_EMAIL,
        "crawler_name": site.CRAWLER_NAME,
        "crawler_homepage": site.CRAWLER_HOMEPAGE,
        "base_url": site.base_url(),
        "timezone": "Asia/Shanghai",
    }


def _outlet_items(now: datetime, *, channel: str = "all", limit: int = OUTLET_LIMIT,
                  scope_name: str = "all") -> list:
    """出口共用的那一份: 窗口里的条目, 过了可见性, 按渠道筛, 取前 N。"""
    items, _stories = views.collect(now, views.STREAM_HOURS)
    if scope_name == "curated":
        items = [item for item in items if item.selected]
    items = [item for item in items if views.in_channel(item, channel)]
    return items[:limit]


def item_url(item) -> str:
    """一条的站内地址。相对链接由 `site.absolute` 补成绝对 —— 被转发出去的
    RSS 里的链接要能直接打开。"""
    return site.absolute("/item/%s:%s" % (item.discovery.source_id, item.discovery.external_id))


def story_url(event) -> str:
    return site.absolute("/story/%s" % event.identity_key)


def _iso(value: str) -> str:
    """RSS 要 RFC 822 那一套, 但读者客户端大多认 ISO; 给 ISO 的北京时刻。"""
    try:
        return cal.moment_of(value).isoformat(timespec="seconds")
    except Exception:
        return ""


def sitemap_xml(now: datetime) -> str:
    """站点地图 (5.10): 只列**可以被收录**的条目 (AIHOT 的 `isIndexable`)。"""
    items, stories = views.collect(now, views.STREAM_HOURS)
    urls: list[tuple[str, str]] = []
    for item in items:
        if not item.summary:
            continue
        if not rules.is_indexable("public", True, item.selected, None, None):
            continue
        urls.append((item_url(item), _iso(item.discovery.published_at)))
    for event in stories:
        urls.append((story_url(event), _iso(event.last_seen_at)))
    latest = db.feed_latest_edition("day")
    if latest:
        urls.append((site.absolute("/editions/%s" % latest["id"]), _iso(latest["created_at"])))
    rows = ["  <url><loc>%s</loc><lastmod>%s</lastmod></url>" % (escape(loc), escape(mod))
            for loc, mod in urls]
    if not rows:
        return ""
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "\n".join(rows) + "\n</urlset>\n")


def robots_txt() -> str:
    """机器人规则 (5.10)。采集器自报家门, 也在这里说清楚。"""
    lines = ["User-agent: *", "Allow: /", "Disallow: /api/", ""]
    base = site.base_url()
    if base:
        lines.append("Sitemap: %s/sitemap.xml" % base)
        lines.append("")
    return "\n".join(lines)


def llms_txt(now: datetime) -> str:
    """给模型读的一份站点说明 (5.10): 站名、在做什么、去哪拿数据。"""
    items = _outlet_items(now, limit=20)
    lines = [
        "# %s (%s)" % (site.NAME, site.NAME_ZH),
        "",
        "> %s" % site.TAGLINE,
        "",
        "行业：%s" % site.INDUSTRY,
        "联系方式：%s" % site.CONTACT_EMAIL,
        "采集器：%s" % site.CRAWLER_NAME,
        "",
        "## 数据出口",
        "",
        "- 全部动态：%s" % site.absolute("/api/v1/feed/items?scope=all"),
        "- 精选：%s" % site.absolute("/api/v1/feed/items?scope=curated"),
        "- 热点榜：%s" % site.absolute("/api/v1/feed/hot"),
        "- 增量快照：%s" % site.absolute("/api/v1/feed/v1"),
        "- RSS：%s" % site.absolute("/feeds/all.xml"),
        "",
        "## 最近条目",
        "",
    ]
    for item in items:
        lines.append("- [%s](%s) — %s" % (item.display_title, item_url(item),
                                          item.summary.replace("\n", " ")[:140]))
    lines.append("")
    return "\n".join(lines)


def feed_xml(now: datetime, channel: str = "all") -> str:
    """一个渠道的 RSS (5.15)。正文只在**许可**时出全文 (AIHOT 的 `mayRedistribute`)。"""
    items = _outlet_items(now, channel=channel)
    base = site.base_url() or "/"
    rows = []
    for item in items:
        source = registry.of(item.discovery.source_id)
        body = ""
        if rules.may_redistribute(source, item.body_mode):
            body = "<![CDATA[%s]]>" % htmlbody.to_text(item.body_html)
        rows.append(
            "    <item>\n"
            "      <title>%s</title>\n"
            "      <link>%s</link>\n"
            "      <guid isPermaLink=\"false\">%s:%s</guid>\n"
            "      <pubDate>%s</pubDate>\n"
            "      <category>%s</category>\n"
            "      <description>%s</description>\n"
            "%s"
            "    </item>" % (
                escape(item.display_title), escape(item_url(item)),
                escape(item.discovery.source_id), escape(item.discovery.external_id),
                escape(_iso(item.discovery.published_at)),
                escape(item.category_label), escape(item.summary),
                ("      <content:encoded>%s</content:encoded>\n" % body) if body else ""))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">\n'
            '  <channel>\n'
            '    <title>%s · %s</title>\n'
            '    <link>%s</link>\n'
            '    <description>%s</description>\n'
            '    <language>zh-cn</language>\n'
            '    <lastBuildDate>%s</lastBuildDate>\n'
            '%s'
            '  </channel>\n'
            '</rss>\n' % (escape(site.NAME), escape(channel), escape(base),
                           escape(site.TAGLINE), escape(now.isoformat(timespec="seconds")),
                           "\n".join(rows)))


def og_payload(item_id: str, now: datetime) -> dict | None:
    """分享图要的那几格 (5.16)。页面没有正文也画得出卡 —— 图是后端给的, 不是前端截屏。"""
    found = views.find_item(now, item_id)
    if found is None:
        return None
    item, _story = found
    return {
        "title": item.display_title,
        "summary": item.summary,
        "source": registry.of(item.discovery.source_id).name,
        "category_label": item.category_label,
        "score": item.score,
        "media": [media.proxied_image(url, "og") for url in item.discovery.media],
        "url": item_url(item),
    }


def v1_snapshot(now: datetime, *, since: str = "", channel: str = "all",
                limit: int = OUTLET_LIMIT) -> dict:
    """v1 快照与增量 (5.14): `since` 之后的新条目 + 一份游标。

    只给"公开的东西" —— 出口是站外的读者, 他拿不到站内的可见性判断.
    """
    items = _outlet_items(now, channel=channel, limit=200)
    if since:
        boundary = cal.moment_of(since)
        items = [item for item in items
                 if cal.moment_of(item.discovery.published_at) > boundary]
    window = items[:limit]
    return {
        "version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "window_hours": views.STREAM_HOURS,
        "items": [
            {
                "id": "%s:%s" % (item.discovery.source_id, item.discovery.external_id),
                "title": item.display_title,
                "url": item_url(item),
                "original_url": item.discovery.url,
                "summary": item.summary,
                "category": item.category,
                "category_label": item.category_label,
                "channel": rules.channel_of(
                    registry.of(item.discovery.source_id).kind, bool(item.discovery.quoted)),
                "body_mode": item.body_mode,
                "source": registry.of(item.discovery.source_id).name,
                "published_at": item.discovery.published_at,
                "score": item.score,
            }
            for item in window
        ],
        "next_since": window[0].discovery.published_at if window else since,
    }


def agent_items(now: datetime, *, channel: str = "all", limit: int = OUTLET_LIMIT) -> dict:
    """给 agent 读的一份 (5.13): 比 v1 多正文的纯文本, 少版式字段。"""
    snapshot = v1_snapshot(now, channel=channel, limit=limit)
    items = _outlet_items(now, channel=channel, limit=limit)
    bodies = {}
    for item in items:
        key = "%s:%s" % (item.discovery.source_id, item.discovery.external_id)
        bodies[key] = htmlbody.to_text(item.body_html) if item.body_mode == "full" else ""
    for row in snapshot["items"]:
        row["text"] = bodies.get(row["id"], "")
    snapshot["channel"] = channel
    return snapshot
