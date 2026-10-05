"""详情页: 从一张 HTML 里取出正文与真标题。

与 `weblist.py` 同一个理由不引解析库 —— 要的东西只有两样 (正文、标题), 而多一个依赖是每一次
安装都要付的账。

取法是最笨但可解释的那一种: 块级标签 (`<p>` 与源自己的 `<h2>`/`<h3>`) 里的字, 跳过脚本、样式、
导航、页脚、侧栏与表单, 按出现顺序留下**块** (htmlbody.py) —— 小标题是源的, 不是我们编的。取不够
就认输, 交回空串 —— 交一个「这篇没有正文」比交一堆菜单文字好: 后者会被
当成文章印在页面上, 而且没有人看得出来。

一句话为什么需要它: 列表页与 RSS 只给标题和一句摘要, 而写一条像样的摘要是要读原文的。
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

from .. import htmlbody

#: 哪几种源的条目背后有一页可以去读。`x_search` 不在其列：推文的正文
#: 就是它自己，而它的 url 是那张推文页 —— 没有登录态时抓回来的是一层 JS 壳。
#: 采集层针对它决定"值不值得去读"，写作层针对它决定"要不要等"。
PAGE_KINDS = ("rss", "web_list", "json_list")

#: 整段丢掉的那些标签里的字。它们的 `<p>` 也是 `<p>`, 不认这几样, 页面上的导航与页脚会变成
#: 文章的前三段。
_SKIP = ("script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg",
         "template", "iframe")

#: 留的块级标签. 标题短, 所以标题不走 `_MIN_PARAGRAPH` 那条线 (「发生了什么」只有五个字).
_BLOCK_TAGS = ("p", "h2", "h3", "h4", "h5", "li", "blockquote")
_HEADING_TAGS = ("h2", "h3", "h4", "h5")

#: 短于这个字数的段落不算段落 —— 菜单项、按钮、版权行都在这儿被挡掉。
_MIN_PARAGRAPH = 10
#: 正文短于这个字数就认输 (AIHOT 的 `MIN_BODY_CHARS = 200`)。一句话加一个菜单不是文章 ——
#: 一百二十字那条线太松: 一张列表页的「摘要 + 时间 + 三行推荐语」正好落在 120-200 之间, 而它
#: 会被当成正文印在页面上。
_MIN_BODY = 200
#: 存下来的上限 (AIHOT 的 60k)。有些站一页十万字, 而这一层只要够写摘要的那一段
#: (htmlbody.clip 在块的边界上截)。喂给模型的那份由 feed/input.py 按档各自截, 所以这里放宽
#: 不会把长文的模型输入一起抬高。
MAX_BODY_CHARS = htmlbody.MAX_CHARS

#: 「标题」长过这个字数就不是标题, 是一段落错地方的正文 (DESIGN.md 8.5)。
MAX_TITLE_CHARS = 100

#: 一页最多留几张图 (AIHOT `content/extract.ts` 的 12): 卡片只排得下几张, 多抽的没人看。
_MAX_IMAGES = 12

#: 这些字面上的「标题」不是标题, 是按钮: 见到它们说明列表页给的是占位。
_BOILERPLATE = frozenset((
    "阅读全文", "阅读更多", "了解更多", "查看更多",
    "查看详情", "继续阅读",
    "read more", "learn more", "continue reading", "more",
))

#: 标题尾巴上挂站点名的几种写法。rpartition 取的是最后一个, 也就是最靠近尾巴的那个。
_SUFFIX_DASHES = (" | ", " - ", " — ", " – ", " _ ")

_WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    """一段字归一成一行: 换行、制表、连续空格都折成一个空格。"""
    return _WS.sub(" ", html.unescape(text or "")).strip()


class _Reader(HTMLParser):
    """一张页面里的段落、`<title>`、第一个 `<h1>` 与两个 og 元信息。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        #: 正文的块 (标签, 文字), 按顺序 —— 交出去的是块不是字符串, 上层才挑得动.
        self.blocks: list[tuple[str, str]] = []
        #: 页面里的图, 按出现顺序, 最多 `_MAX_IMAGES` 张 —— 正文 HTML 只留文字, 图另存
        #: (`media_json`), 卡片媒体视图与理解档的首图都读它 (AIHOT `content/extract.ts`).
        self.images: list[dict] = []
        self._tag: str | None = None
        self.title = ""
        self.h1 = ""
        self.og_title = ""
        self.og_description = ""
        self._skip = 0
        self._buffer: list[str] | None = None
        self._title: list[str] | None = None
        self._h1: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "img":
            self._image(attrs)
            return
        if tag in _BLOCK_TAGS:
            self._close_block()
            self._tag, self._buffer = tag, []
        elif tag == "br" and self._buffer is not None:
            self._buffer.append(" ")
        elif tag == "title":
            self._title = []
        elif tag == "h1":
            self._h1 = [] if not self.h1 else self._h1
        elif tag == "meta":
            values = {(key or "").lower(): (value or "") for key, value in attrs}
            prop = (values.get("property") or values.get("name") or "").lower()
            if prop == "og:title" and not self.og_title:
                self.og_title = values.get("content", "")
            elif prop == "og:description" and not self.og_description:
                self.og_description = values.get("content", "")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == self._tag and self._buffer is not None:
            self._close_block()
        elif tag == "title" and self._title is not None:
            self.title = self.title or _clean("".join(self._title))
            self._title = None
        elif tag == "h1" and self._h1 is not None:
            self.h1 = self.h1 or _clean("".join(self._h1))
            self._h1 = None

    def _image(self, attrs: list[tuple[str, str | None]]) -> None:
        """页面上的一张图. 没有地址的丢掉, `data-src` 优先 —— 懒加载站的 `src` 是占位图."""
        if len(self.images) >= _MAX_IMAGES:
            return
        values: dict = {}
        for key, value in attrs:
            key = (key or "").lower()
            if key and key not in values:
                values[key] = value or ""
        src = (values.get("data-src") or values.get("data-original")
               or values.get("src") or "").strip()
        if not src:
            return
        self.images.append({
            "kind": "image", "url": html.unescape(src),
            "width": int(values["width"]) if (values.get("width") or "").isdigit() else None,
            "height": int(values["height"]) if (values.get("height") or "").isdigit() else None})

    def _close_block(self) -> None:
        """收下一块. 空块不留: 一个空的 `<p>` 在页面上是一条没有来由的空行."""
        if self._buffer is None:
            return
        text = _clean("".join(self._buffer))
        self._buffer = None
        if self._tag and text:
            self.blocks.append((self._tag, text))
        self._tag = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        for buffer in (self._buffer, self._title, self._h1):
            if buffer is not None:
                buffer.append(data)


def strip_site_suffix(title: str) -> str:
    """「文章标题 - 站点名」把尾巴去掉。

    两个数都是估出来的, 也都在测试里钉着: 尾巴二到二十五字 (站点名就是这么长), 剩下那半至少
    八字 (短于此的不是标题, 是一对并列的名词 ——「Bitcoin - Ethereum」不该被砍成半句)。判断不了
    就原样留着: 标题上多一截站点名只是难看, 砍掉半个标题是错。
    """
    text = _clean(title)
    for dash in _SUFFIX_DASHES:
        head, sep, tail = text.rpartition(dash)
        if sep and 2 <= len(tail) <= 25 and len(head) >= 8:
            return head.strip()
    return text


def looks_like_title(title: str) -> bool:
    """这一串字像不像一个标题。

    DESIGN.md 8.5 的那条: 超过一百字, 或者干脆是「阅读全文」, 都不是标题 —— 详情页里有真的
    那一个。这一类条目即使带着正文, 也要去补一趟。
    """
    text = _clean(title)
    if not text or len(text) > MAX_TITLE_CHARS:
        return False
    return text.lower().rstrip(".…") not in _BOILERPLATE


def parse(text: str) -> dict:
    """一张详情页 -> `{"body": ..., "title": ..., "media": [...]}`。取不到就是空串, 不猜。"""
    reader = _Reader()
    try:
        reader.feed(text or "")
    except Exception:  # 一张畸形页面不该让整轮失败, 更不该让它变成一条空记录
        pass
    kept = [(tag, text) for tag, text in reader.blocks
            if tag in _HEADING_TAGS or len(text) >= _MIN_PARAGRAPH]
    body = htmlbody.clip(htmlbody.render([(tag, _clean(text)) for tag, text in kept]))
    if len(htmlbody.to_text(body)) < _MIN_BODY:
        # 没有块级正文时, 描述是第二好的: 它至少是这一页在说自己是什么。
        description = _clean(reader.og_description)
        body = htmlbody.sanitize(description) if len(description) >= _MIN_BODY else ""
    title = reader.og_title or reader.title or reader.h1
    return {"body": body, "title": strip_site_suffix(title), "media": reader.images}
