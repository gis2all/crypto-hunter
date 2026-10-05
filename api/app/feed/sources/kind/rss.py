"""通用 RSS / Atom 抓取器。

一份 feed 就是一个列表，而列表有好几种方言：RSS 2.0 的 `item`、Atom 的 `entry`、RSS 1.0 的
`item`（在 RDF 命名空间里）。这里按**标签的本地名**匹配，不管命名空间 —— 三种方言里
`title` / `link` / `published` 这些名字是共通的，而写死命名空间等于每多一家站点就多一个分支。

没有日期的条目照样交出去（`published_at` 为空），由采集层按「第一次见到它的时刻」补上：
窗口、归组、热度全都要一个时刻，而一条没有时刻的记录会在 `materials.moment` 上炸掉整轮。
在这里丢掉它，等于把「这家 feed 有几条没写日期」变成一件看不见的事。

校验器（ETag / Last-Modified）只在**非首次**导入时才存。存早了，第二轮就吃一个 304 空手
而归，而那一轮本该把整个窗口读一遍。
"""

from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from ....sources import http
from ... import htmlbody, materials
from ...models import Discovery

log = logging.getLogger(__name__)

#: 日期标签：RSS 用 pubDate（RFC 822），Atom 用 published / updated（ISO 8601），
#: 还有些站点把 dc:date 写上。顺序即优先级。
_DATE_TAGS = ("pubDate", "published", "updated", "date", "created", "issued", "modified")
#: 正文在哪几个标签里：RSS 的 `content:encoded`（本地名 `encoded`）
#: 与 Atom 的 `content`。
_CONTENT_TAGS = ("encoded", "content")
#: 摘要在哪几个标签里：RSS 的 `description` 与 Atom 的 `summary`。
#: 两者分开存是必须的 —— 合在一起就分不出"这是正文"还是"这是一句话的摘要",
#: 而一句话的摘要被当正文用，摘要档写出来的就是那句话的同义句。
_SUMMARY_TAGS = ("description", "summary")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\n]+")

#: 只有一句话的"正文"，而且结尾是一个"点进去看全文"的记号 (AIHOT `isTeaser`)。
#: The Verge 的 "Read the full story at The Verge." 就是典型：它是**摘要**，
#: 所以得先去读原文，再判这条。
TEASER_BELOW = 1200
TEASER_MARKS = (
    re.compile(r"\bappeared first on\b", re.I),
    re.compile(r"\bread (?:the )?full (?:story|article)\b", re.I),
    re.compile(r"\bcontinue reading\b", re.I),
    re.compile(r"\bread more\b", re.I),
    re.compile("\u2026\\s*$"),
    re.compile(r"\[\s*(?:\u2026|\.\.\.)\s*\]\s*$"),
)


def is_teaser(text: str) -> bool:
    """这段文字是不是一个"请去看全文"的钩子 (AIHOT `isTeaser`)。"""
    t = text.strip()
    return len(t) < TEASER_BELOW and any(mark.search(t) for mark in TEASER_MARKS)


def _feed_text(record: Discovery, source: dict) -> Discovery:
    """列表里这段文字是正文还是摘要 (AIHOT `feedText`)。

    三件事，都是同一件事的三面：摘要这一格总是填上（有就用源给的，钩子文就用钩子本身）；
    不到 280 字的不当正文；钩子文不当正文。后两条都把 `body` 置空 ——
    置空不是丢掉它，而是告诉下一站"这条还没有正文，先去读原文"（`collect._wants_detail`）。
    """
    body_text = htmlbody.to_text(record.body)
    editorial = (source.get("participation_mode") or "editorial") == "editorial"
    teaser = bool(body_text) and editorial and is_teaser(body_text)
    excerpt = record.excerpt or (materials.collapse(body_text) if teaser else "")
    kept = bool(body_text) and len(body_text) > materials.MIN_BODY_CHARS and not teaser
    if kept:
        return replace(record, excerpt=excerpt) if excerpt != record.excerpt else record
    return replace(record, body="", excerpt=excerpt)


def _local(tag: str) -> str:
    """`{http://...}title` -> `title`。"""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _text(raw: str) -> str:
    """HTML 去标签、实体解码、空白归一。feed 里的摘要几乎总是带标签的 HTML。"""
    if not raw:
        return ""
    plain = _TAG_RE.sub(" ", raw)
    plain = html.unescape(plain)
    return _WS_RE.sub(" ", plain).strip()


def _iso(value: str) -> str:
    """feed 自己的时间写法 -> 我们要的 ISO 8601。认不出来就回空串，由调用方丢掉这条。"""
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    try:
        stamp = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return ""
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.isoformat()


def _entry_link(node: ET.Element, base: str) -> str:
    """Atom 的 link 有个 rel，RSS 的 link 就是一段字。取那条给人看的。"""
    best = ""
    for child in node:
        if _local(child.tag) != "link":
            continue
        href = (child.get("href") or "").strip()
        rel = (child.get("rel") or "alternate").strip().lower()
        if not href:
            href = (child.text or "").strip()
        if not href:
            continue
        if rel == "alternate" or not best:
            best = href
            if rel == "alternate":
                break
    if best.startswith("/") and base:
        best = base.rstrip("/") + best
    return best


def _entry_media(node: ET.Element) -> list[str]:
    """feed 自己带的图。只有 X 这类会给，普通 RSS 是空的。"""
    found: list[str] = []
    for child in node:
        name = _local(child.tag)
        url = ""
        if name == "thumbnail":
            url = (child.get("url") or "").strip()
        elif name == "content":
            medium = (child.get("medium") or "").lower()
            kind = (child.get("type") or "").lower()
            if medium == "image" or kind.startswith("image"):
                url = (child.get("url") or "").strip()
        elif name == "enclosure":
            kind = (child.get("type") or "").lower()
            if kind.startswith("image"):
                url = (child.get("url") or "").strip()
        if url and url not in found:
            found.append(url)
    return found


def parse(text: str, source_id: str, base: str = "",
          summary_is_body: bool = False) -> list[Discovery]:
    """一份 feed 的文字 -> 记录。

    `summary_is_body` 是源自己说的"我这份 feed 只把正文写在 description 里"
    （AIHOT 同名 config 键）。没这个标记时，`description` 就只是摘要。"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise http.SourceError("feed is not XML: %s" % (repr(exc)[:80],)) from exc

    out: list[Discovery] = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        title = ""
        content_html = ""
        summary_html = ""
        author = ""
        published = ""
        guid = ""
        for child in node:
            name = _local(child.tag)
            if name == "title" and not title:
                title = _text(child.text or "")
            elif name in _CONTENT_TAGS and not content_html:
                content_html = child.text or ""
            elif name in _SUMMARY_TAGS and not summary_html:
                summary_html = child.text or ""
            elif name in _DATE_TAGS and not published:
                published = _iso(child.text or "")
            elif name in ("guid", "id") and not guid:
                guid = (child.text or "").strip()
            elif name in ("creator", "author", "name") and not author:
                author = _text(child.text or "")
        link = _entry_link(node, base)
        url = link or guid
        key = guid or link
        if not key:
            continue
        # 日期可能为空: 交给采集层按"第一次见到它的时刻"补, 这里不假装知道。
        # 一个没有日期的记录在哪一层补上时刻, 决定了它长什么样 —— 在这里丢掉就等于
        # 把「这家 feed 有几条没写日期」变成一个看不见的事。
        body_html = content_html or (summary_html if summary_is_body else "")
        out.append(Discovery(
            source_kind="rss",
            source_id=source_id,
            external_id=key,
            title=title or key,
            url=url,
            published_at=published,
            body=htmlbody.sanitize(body_html) if body_html else "",
            author=author,
            # 摘要是纯文字, 不是 HTML: 它只有两个用处 —— 当正文不够用时敷给模型, 与给判定
            # "这条值不值得去读原文". 两处都是给人读的一句话。
            excerpt=_text(summary_html)[:materials.EXCERPT_CHARS],
            media=_entry_media(node),
        ))
    return out


def fetch(source: dict, first_import: bool) -> tuple[list[Discovery], dict]:
    """读一份 feed。回 (记录, 新的 cursor)。"""
    config = source.get("config") or {}
    url = config.get("feedUrl") or ""
    if not url:
        raise http.SourceError("rss source without feedUrl: %s" % source.get("id"))
    cursor = dict(source.get("cursor") or {})
    saved = cursor.get("rss") or {}
    transport = config.get("transport", "http")
    status, heads, text = http.get_conditional(
        url,
        http.crawler_headers(config.get("headers")),
        etag=saved.get("etag"),
        last_modified=saved.get("lastModified"),
        transport=transport,
    )
    if status == 304:
        # 没变就是没变。cursor 原样还回去，一条记录都不吐。
        log.info("rss %s: not modified", source.get("id"))
        return [], cursor

    records = parse(text, source["id"], base=config.get("baseUrl", ""),
                    summary_is_body=config.get("summaryIsBody") is True)
    # 列表里这段文字到底是正文还是摘要, 在**这一层**判: 它要读源自己的
    # 身份 (participation_mode), 而 `parse` 是一个只读 feed 文字的纯函数。
    records = [_feed_text(record, source) for record in records]
    undated = sum(1 for record in records if not record.published_at)
    if undated:
        log.info("rss %s: %d of %d entries carry no date; dated on first sight",
                 source.get("id"), undated, len(records))

    limit = int(config.get("initialBackfillLimit") or 0)
    if first_import and limit > 0 and len(records) > limit:
        records = records[:limit]

    if not first_import:
        cursor["rss"] = {
            "etag": heads.get("etag") or heads.get("ETag") or "",
            "lastModified": heads.get("last-modified") or heads.get("Last-Modified") or "",
        }
    return records, cursor
