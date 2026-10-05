"""通用 JSON 列表抓取器。

站点自己喂给自己前端的那个接口，字段名写在 config 里。比抓 HTML 稳：JSON 的键是给程序看
的，改名的代价比改一个 div 的 class 大得多。

`publishedAtUnit` 是因为时间戳有两种写法：一种是 ISO 字符串，一种是「从 1970 年起的毫秒
数」。同一个字段名底下是哪一种，只有写 config 的人知道，所以由他说，不由我们猜 —— 猜错的
后果是整轮记录排到 1970 年去，而那看起来像「这家今天没发东西」。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from ....sources import http
from ... import htmlbody
from ...models import Discovery

log = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][\w]*)\}")


def _dig(obj: Any, path: str) -> Any:
    """`data.catalogs.0.articles` -> 那一层。数字段当列表下标。中途断了回 None。"""
    node = obj
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(node, dict):
            node = node.get(part)
        else:
            return None
        if node is None:
            return None
    return node


def _pick(item: Any, paths: list[str]) -> str:
    """第一个有值的路径。字段名各家不同，写几个候选比写一个准。"""
    for path in paths:
        value = _dig(item, path)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return ""


def _stamp(value: str, unit: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if unit in ("ms", "s"):
        try:
            seconds = float(raw) / (1000.0 if unit == "ms" else 1.0)
        except ValueError:
            return ""
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return ""


def _fill(template: str, item: Any) -> str:
    """`.../announcement/{code}` -> 把花括号里的字段从这一条里取出来填上。"""
    if not template:
        return ""
    return _PLACEHOLDER.sub(lambda m: str(_dig(item, m.group(1)) or ""), template)


def fetch(source: dict, first_import: bool) -> tuple[list[Discovery], dict]:
    """读一个 JSON 接口。回 (记录, 新的 cursor)。"""
    config = source.get("config") or {}
    url = config.get("url") or ""
    if not url:
        raise http.SourceError("json_list source without url: %s" % source.get("id"))
    payload = http.json_get(url, http.crawler_headers(config.get("headers")),
                            transport=config.get("transport", "http"))

    items = _dig(payload, config.get("itemsPath") or "")
    if not isinstance(items, list):
        raise http.SourceError("itemsPath %r is not a list in %s"
                               % (config.get("itemsPath"), source.get("id")))
    limit = int(config.get("maxItems") or 0)
    if limit > 0:
        items = items[:limit]

    titles = config.get("titlePaths") or ["title"]
    summaries = config.get("summaryPaths") or []
    authors = config.get("authorPaths") or []
    unit = config.get("publishedAtUnit") or "iso"
    out: list[Discovery] = []
    for item in items:
        url_one = _fill(config.get("urlTemplate") or "", item) \
            or _fill(config.get("urlTemplateFallback") or "", item)
        key = _pick(item, [config.get("externalIdPath") or "id"]) or url_one
        if not key:
            continue
        title = _pick(item, titles)
        out.append(Discovery(
            source_kind="json_list",
            source_id=source["id"],
            external_id=key,
            title=title or key,
            url=url_one or url,
            published_at=_stamp(_pick(item, [config.get("publishedAtPath") or ""]), unit),
            body=htmlbody.sanitize(_pick(item, summaries)),
            author=_pick(item, authors),
        ))
    return out, dict(source.get("cursor") or {})
