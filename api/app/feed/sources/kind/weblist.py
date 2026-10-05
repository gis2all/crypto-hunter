"""通用列表页抓取器：从一张 HTML 里挑出「哪些链接是文章」。

不装一套 CSS 选择器引擎。这些页面真正需要的判断只有一个 —— 哪些链接是文章 —— 而
`linkPrefix`（`/article/`）这一个条件就能回答它，说得清、测得动；一套残缺的选择器语法两样
都不行，它只会让下一个接源的人以为 `:nth-child` 能用。

**日期这里基本拿不到**，所以这一层交出去的 `published_at` 常常是空的，由采集层按「第一次
见到它的时刻」补上。这不是将就：列表页上的一条今天下午三点被我们第一次看见，它的时刻就是
今天下午三点，而这条链路从三十小时前就一直在看这个页面 —— 那个「第一次」是准的。

于是这一类的源必须配合「只存没见过的那几条」（`feed_items_known`）。没有那一步，每一轮都会
把同一批文章重新盖上新日期，一篇两周前的文章会一直看起来像刚发的。
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

from ....sources import http
from ...models import Discovery

_SKIP = {"script", "style", "noscript"}
_WS = re.compile(r"\s+")


class _Anchors(HTMLParser):
    """页面上的链接：href 与锚点里的字。

    自己写而不是引一个解析库：要的东西只有两样（地址、文字），而多一个依赖是每一次安装都
    要付的账。
    """

    def __init__(self, prefix: str, contains: str) -> None:
        super().__init__(convert_charrefs=True)
        self.prefix = prefix
        self.contains = contains
        self.found: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip += 1
            return
        if tag != "a" or self._skip:
            return
        href = dict(attrs).get("href") or ""
        href = href.strip()
        if not href:
            return
        if self.prefix and not href.startswith(self.prefix):
            return
        if self.contains and self.contains not in href:
            return
        self._href = href
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None and not self._skip:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "a" and self._href is not None:
            text = _WS.sub(" ", html.unescape("".join(self._text))).strip()
            self.found.append((self._href, text))
            self._href = None
            self._text = []


def parse(text: str, source_id: str, config: dict) -> list[Discovery]:
    """一张列表页 -> 记录。同一条链接只留一次（页面常常把它印两遍，一图一文）。"""
    parser = _Anchors(config.get("linkPrefix") or "", config.get("linkContains") or "")
    parser.feed(text)
    base = (config.get("baseUrl") or "").rstrip("/")
    shortest = int(config.get("titleMinLength") or 8)
    out: list[Discovery] = []
    seen: set[str] = set()
    for href, title in parser.found:
        if len(title) < shortest:
            continue
        url = base + href if href.startswith("/") and base else href
        if url in seen:
            continue
        seen.add(url)
        out.append(Discovery(
            source_kind="web_list",
            source_id=source_id,
            external_id=url,
            title=title,
            url=url,
            published_at="",
        ))
    limit = int(config.get("maxItems") or 0)
    return out[:limit] if limit > 0 else out


def fetch(source: dict, first_import: bool) -> tuple[list[Discovery], dict]:
    """读一张列表页。回 (记录, 新的 cursor)。这一类没有 cursor：列表页没有「读到哪儿」。"""
    config = source.get("config") or {}
    url = config.get("url") or ""
    if not url:
        raise http.SourceError("web_list source without url: %s" % source.get("id"))
    text = http.get(url, http.crawler_headers(config.get("headers")),
                    transport=config.get("transport", "http"))
    return parse(text, source["id"], config), dict(source.get("cursor") or {})
