"""正文的 HTML 那一层: 块级结构照源留, 块内留行内标签, 脚本/样式/表单整棵丢.

为什么正文存 HTML 而不是纯文本: 译文要**按块**换, 换完结构还得是原文的 —— 小标题是源自己的
`<h2>`, 不是模型编的. 存纯文本, "这一段原来是标题" 这件事就丢了, 模型只能按位置给段落起名,
一篇文章四十段就印出三十七个「后续」(DESIGN.md 9). 所以采集层留下块, 写作站只换块里的字.

为什么块里留着行内标签 (对齐 AIHOT `content/sanitize.ts` 的 `ALLOWED_TAGS`): 段落里的外链、
图片、行内代码与加粗是原文的一部分, 压掉就再也回不来. 代价是翻译要按 AIHOT
`editorial/translate.ts` 的**占位符协议**走 —— 媒体与行内代码换成 `[[n]]` 记号, 链接改成
`<a id="Ln">`, 模型只翻文字; 回来时每个记号的**出现次数必须恰好一次**, 少了或多了整块作废
(这一条停在 `understand`, 下一轮重问). 宁可留原文, 也不把错位的链接缝进译文.

四组纯函数, 无 IO:

* 采集层: `sanitize` (源 HTML -> 块级 HTML)、`from_plain` (纯文本 -> 块级 HTML)、`render`.
* 读字: `to_text` / `blocks` / `first_paragraph` / `images` —— 归类、去重、取标题看这几份.
* 翻译站: `shields` / `shield` / `unshield` / `restore` / `rewrite_html`.
* 收尾: `clip` 只在块的边界上截.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser

#: 留的块级标签 —— 读者一眼看得出"这段是标题 / 这条是列表项"的那些 (AIHOT `ALLOWED_TAGS` 的
#: 块级部分; `h1` 与 `h6` 在解析时并成 `h2`/`h5`, 与 AIHOT 的 `transformTags` 一致).
BLOCKS = ("p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "figcaption",
          "td", "th", "caption", "dt", "dd", "pre", "figure")

#: 块里**保留**的行内标签. 不留属性的只留标签; `a`/`img`/`code`/`pre` 的属性另按表挑.
INLINE = ("a", "code", "strong", "em", "b", "i", "u", "s", "del", "sup", "sub", "mark", "span")

#: `h1`/`h6` 并档 (AIHOT `transformTags: { h1: "h2", h6: "h5" }`).
_NORMALISE = {"h1": "h2", "h6": "h5"}

#: 正文的**存储**上限 (AIHOT 的 `MAX_BODY_CHARS = 60_000`). 超了就在块的边界上截 —— 截半个
#: 标签会印出一页乱码. 这一格只管库里存多少: 喂给模型的份由 `feed/input.py` 按档各自截
#: (结构档 7000 / 理解档 60000 / 摘要档 6000), 两者不再是同一个数。
MAX_CHARS = 60_000

#: 一整棵子树都丢 (AIHOT 的 `DROP_WHOLE`). 比 AIHOT 多收了站点骨架 (`nav`/`footer`/`aside`):
#: 它们在提取那一站就该没了, 留着只是保险.
_SKIP = {"script", "style", "noscript", "textarea", "option", "iframe", "object", "embed",
         "applet", "form", "input", "select", "button", "label", "fieldset", "legend", "svg",
         "link", "meta", "base", "title", "head", "template", "audio", "map", "area", "frame",
         "frameset", "track", "source", "param", "nav", "footer", "aside",
         "annotation", "annotation-xml", "mglyph"}

#: 不是块, 但要**断开**一次: 一个 `div` 收尾就是一段结束, 哪怕源没写 `p`.
_BREAK = {"div", "section", "article", "main", "header", "br", "hr", "ul", "ol",
          "table", "thead", "tbody", "tfoot", "tr", "dl"}

#: 没有结束标签的. 不计进跳过深度 —— 少减一次就等于把后面整篇吞掉.
_VOID = {"br", "hr", "img", "input", "meta", "link", "source", "track", "wbr", "area",
         "base", "col", "embed", "param"}

#: 翻译时要护住的媒体 (AIHOT `translate.ts` 的 `picture, video, img, code`). 容器排在前面:
#: `<picture><img>` 要整块当一个记号, 先切里面的 `img` 会让外层那个记号永远回不来.
_TOKENS = ("picture", "video", "img", "code")

_MEDIA_RE = re.compile(r"<(?:img|picture|video)\b", re.I)
_BLOCK_RE = re.compile("<(%s)>(.*?)</\\1>" % "|".join(BLOCKS), re.S)
_TAG_RE = re.compile("<[^>]+>")
_WS = re.compile(r"\s+")
_LEAD = re.compile(r"^\s+")
_TAIL = re.compile(r"\s+$")
#: "这串字里有真标签吗". 只有 `<` 不算 —— 一篇讲 `a < b` 的文章不该被当 HTML 解析.
_HTML = re.compile(r"<\s*(p|div|br|h[1-6]|li|ul|ol|blockquote|pre|code|a|span|strong|em"
                   r"|img|figure|table|section|article|td|tr|dl|dt|dd|figcaption|caption"
                   r"|picture|video|th)\b", re.I)
#: 占位符: 媒体与行内代码. 记号用 U+27E6/U+27E7, 正常文本里不会出现.
_TOKEN_RE = re.compile("\u27e6(\\d+)\u27e7")
_OPEN_A_RE = re.compile(r"<a\b[^>]*>", re.I)
_ATTR_RE = re.compile("([a-zA-Z_:][-a-zA-Z0-9_:.]*)\\s*=\\s*(?:\"([^\"]*)\"|'([^']*)')")
_ID_RE = re.compile(r"^L(\d+)$")
_LANG_CLASS = re.compile(r"language-[\w-]+")
_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
#: 空链接: 没有文字也没有媒体子节点 (AIHOT `exclusiveFilter`).
_EMPTY_A_RE = re.compile(r"<a\b[^>]*>\s*</a>", re.I)
#: 促销块的 class (`msr-promo` / `promo-box` …), 不是 "promotion" 这种正文里的词.
_PROMO_CLASS = re.compile(r"(?:^|[-_])promo(?:$|[-_])", re.I)
#: 广告 ID 同步端点被抓成过图片; 就算它返回一个一像素 GIF, 也不是文章插图
#: (AIHOT `lib/image-url.ts` 的 `TRACKING_HOSTS`).
_TRACKING_HOSTS = frozenset((
    "ids4.ad.gt", "ids.ad.gt", "secure.adnxs.com", "sync.1rx.io", "ssum-sec.casalemedia.com",
    "sync.smartadserver.com", "token.rubiconproject.com", "image2.pubmatic.com",
    "sync.go.sonobi.com", "onetag-sys.com"))
_SEAL = "\u27e6%s\u27e7"


def _clean(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _tidy(inner: str) -> str:
    """收一块: 去掉两端的空白与空链接 (没有文字的 `<a>` 只是个壳), 标签之间多余的空白折成
    一个空格 —— 去掉一个空链接之后留下的两个空格不该印在页面上."""
    text = _EMPTY_A_RE.sub("", inner)
    out: list[str] = []
    last = 0
    for found in _TAG_RE.finditer(text):
        out.append(_WS.sub(" ", text[last:found.start()]))
        out.append(found.group(0))
        last = found.end()
    out.append(_WS.sub(" ", text[last:]))
    return _TAIL.sub("", _LEAD.sub("", "".join(out)))


def _host_of(url: str) -> str:
    """URL 的主机名, 小写; 相对地址读不出来就是空串."""
    found = re.match(r"https?://([^/?#]+)", url or "", re.I)
    return found.group(1).lower() if found else ""


def _tracking(src: str, width: str | None, height: str | None) -> bool:
    """一像素的图与已知的广告同步端点都不是文章插图 (AIHOT `isTrackingImage`)."""
    if (width or "").isdigit() and (height or "").isdigit():
        if int(width) <= 1 and int(height) <= 1:
            return True
    return _host_of(src) in _TRACKING_HOSTS


def from_plain(text: str) -> str:
    """纯文本 -> 块级 HTML. 空行分段: 段是源自己分的, 我们只是把它写成块."""
    parts = [part.strip() for part in re.split(r"\n\s*\n", (text or "").strip()) if part.strip()]
    return "".join("<p>%s</p>" % html.escape(_clean(part), quote=False) for part in parts)


def render(parts: list[tuple[str, str]] | tuple[tuple[str, str], ...]) -> str:
    """(标签, 文字) 的清单 -> 块级 HTML. 采集层挑完块之后由它拼, 免得每处都手写一遍转义."""
    out: list[str] = []
    for tag, text in parts:
        if not text:
            continue
        name = _NORMALISE.get(tag, tag)
        out.append("<%s>%s</%s>" % (name, html.escape(text, quote=False), name))
    return "".join(out)


def _attrs(attrs) -> dict:
    """(名字, 值) 或正则的三元组都收 —— 属性白名单只认第一次出现的那个."""
    found: dict = {}
    for item in attrs:
        key = (item[0] or "").lower()
        if not key or key in found:
            continue
        found[key] = next((value for value in item[1:] if value is not None), "")
    return found


def _inline_html(tag: str, values: dict) -> str:
    """行内标签按 AIHOT 的属性白名单重写: `a` 留 href/title, `img` 留 src 与宽高,
    `code`/`pre` 只留 `language-*` 的 class, 其余只留标签本身.
    没有地址的 `img` 丢掉 —— 留着它, 翻译时还要为一个空标签对账."""
    if tag == "img":
        src = (values.get("data-src") or values.get("data-original") or values.get("src") or "").strip()
        if not src or _tracking(src, values.get("width"), values.get("height")):
            return ""
        bits = ['src="%s"' % html.escape(src, quote=True)]
        for key in ("alt", "width", "height", "title"):
            if values.get(key):
                bits.append('%s="%s"' % (key, html.escape(values[key], quote=True)))
        return "<img %s>" % " ".join(bits)
    if tag == "a":
        bits = []
        if (values.get("href") or "").strip():
            bits.append('href="%s"' % html.escape(values["href"].strip(), quote=True))
        if values.get("title"):
            bits.append('title="%s"' % html.escape(values["title"], quote=True))
        return ("<a %s>" % " ".join(bits)) if bits else "<a>"
    if tag in ("code", "pre"):
        found = _LANG_CLASS.match((values.get("class") or "").strip())
        return ('<%s class="%s">' % (tag, found.group(0))) if found else ("<%s>" % tag)
    return "<%s>" % tag


class _Sanitizer(HTMLParser):
    """源的 HTML -> 块级 HTML.

    规则四条: 丢标签与它整棵子树; 遇到块级标签就把攒下的散字收成一块; 块里保留白名单里的行内
    标签 (属性按表重写); 认不得的标签只留它的文字. 输出永远是"一串平铺的块", 没有嵌套 ——
    `blocks()` 认得出它, 模型也只面对块里的字.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._cur: str | None = None
        self._buf: list[str] = []
        self._loose: list[str] = []
        self._open: list[str] = []
        self._skip = 0

    def _sink(self) -> list[str]:
        return self._buf if self._cur else self._loose

    def _close_inline(self) -> None:
        while self._open:
            self._sink().append("</%s>" % self._open.pop())

    def _emit(self, tag: str, inner: str) -> None:
        if inner and (to_text(inner) or _MEDIA_RE.search(inner)):
            self._out.append("<%s>%s</%s>" % (tag, inner, tag))

    def _close_block(self) -> None:
        self._close_inline()
        inner, self._buf = _tidy("".join(self._buf)), []
        if self._cur:
            self._emit(self._cur, inner)
        self._cur = None

    def _close_loose(self) -> None:
        self._close_inline()
        inner, self._loose = _tidy("".join(self._loose)), []
        if inner:
            self._emit("p", inner)

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = _NORMALISE.get(tag, tag)
        if self._skip:
            if tag not in _VOID:
                self._skip += 1
            return
        if tag not in _VOID and _PROMO_CLASS.search(_attrs(attrs).get("class") or ""):
            # 促销块整棵丢: 源在正文里轮播不同的产品框, 留着它每次抓页都像正文变了.
            self._skip = 1
            return
        if tag in _SKIP:
            if tag not in _VOID:
                self._skip = 1
            return
        if tag in _BREAK:
            # `<br>` 只断一段里的行, 段本身还接着; 容器标签则是这一块到此为止.
            if tag == "br":
                if self._cur:
                    self._buf.append("<br>")
                else:
                    self._close_loose()
            elif self._cur is None:
                self._close_loose()
            else:
                self._close_block()
            return
        if tag == "img":
            self._sink().append(_inline_html("img", _attrs(attrs)))
            return
        if tag in _VOID:
            return
        if tag in BLOCKS:
            self._close_block()
            self._close_loose()
            self._cur, self._buf, self._open = tag, [], []
            return
        if tag in INLINE:
            self._open.append(tag)
            self._sink().append(_inline_html(tag, _attrs(attrs)))
            return
        # 认不得的标签: 只当它的文字存在.

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = _NORMALISE.get(tag, tag)
        if self._skip:
            if tag not in _VOID:
                self._skip -= 1
            return
        if tag in INLINE:
            if tag in self._open:
                while self._open:
                    top = self._open.pop()
                    self._sink().append("</%s>" % top)
                    if top == tag:
                        break
            return
        if tag == self._cur:
            self._close_block()
        elif tag in _BREAK and self._cur is None:
            self._close_loose()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self._sink().append(_WS.sub(" ", html.escape(data, quote=False)))

    def result(self) -> str:
        self._close_block()
        self._close_loose()
        return "".join(self._out)


def sanitize(raw: str) -> str:
    """源的 HTML -> 块级 HTML. 没有真标签的当纯文本处理 (空行分段)."""
    text = (raw or "").strip()
    if not text:
        return ""
    if not _HTML.search(text):
        return from_plain(text)
    reader = _Sanitizer()
    reader.feed(text)
    reader.close()
    return reader.result()


def _parts(body: str):
    return [(match.group(1), match.group(2)) for match in _BLOCK_RE.finditer(body or "")]


def to_text(body: str) -> str:
    """正文的纯文字: 归类、去重、取标题这些读字的活都该看这一份, 不该看标签."""
    return _clean(html.unescape(_TAG_RE.sub(" ", body or "")))


def blocks(body: str) -> list[str]:
    """正文的块, 按原文顺序, 每块是它的**纯文字**. 交给模型的那一串在 `shields()`."""
    return [to_text(inner) for _tag, inner in _parts(body)]


#: 能当目录的标题档 (AIHOT `detail.ts` 的 `h2-h4`): 再深一档是原文自己的分层, 不是读者会
#: 跳去的地方.
OUTLINE_LEVELS = (2, 3, 4)

#: 目录里一条标题最长印多少字. 目录是一行, 不是一段.
OUTLINE_TEXT = 80

_HEADING = re.compile(r"<h([2-6])(?:\s[^>]*)?>([\s\S]*?)</h\1>", re.IGNORECASE)
_OUTLINE_TAG = re.compile(r"<[^>]+>")


def with_outline(body: str) -> tuple[str, list[dict]]:
    """给小标题编上号, 交出 (注过锚点的正文, 目录) —— AIHOT `detail.ts` 的 `withOutline`.

    号按**出现次序**走, 不按标题的文字: 译文换过字, `sec-3` 还是同一节, 所以两份正文能共用
    一套锚点. 没有字的标题不占目录, 号码却照走 —— 号码错开一位, 目录点下去就落到别的节上.
    """
    entries: list[dict] = []
    count = 0

    def _one(match: "re.Match") -> str:
        nonlocal count
        count += 1
        anchor = "sec-%d" % count
        level, inner = match.group(1), match.group(2)
        text = _OUTLINE_TAG.sub("", inner).strip()
        if text and int(level) in OUTLINE_LEVELS:
            entries.append({"id": anchor, "text": text[:OUTLINE_TEXT], "level": int(level)})
        return '<h%s id="%s">%s</h%s>' % (level, anchor, inner, level)

    return _HEADING.sub(_one, body or ""), entries


def first_paragraph(body: str) -> str:
    """正文的第一段 (第一个 `<p>`) 的**文字**. 摘要的规则版读它 —— 标题块不该被当成摘要
    印出来, 段落里的标签也不该跟着印出来."""
    for tag, inner in _parts(body):
        if tag == "p":
            text = to_text(inner)
            if text:
                return text
    return ""


def rewrite(body: str, texts: list[str] | tuple[str, ...]) -> str:
    """按块回写**纯文字**: 结构照旧, 换进来的字会转义. 条数必须与 `blocks(body)` 相同.
    带行内标签的回归写走 `rewrite_html`. """
    return rewrite_html(body, [html.escape(str(text), quote=False) for text in texts])


def rewrite_html(body: str, inners: list[str] | tuple[str, ...]) -> str:
    """按块回写**块内 HTML**: 结构照旧, 每一块的里面换成给进来的串 (调用方保证它已经安全)."""
    out: list[str] = []
    last = 0
    index = 0
    for match in _BLOCK_RE.finditer(body or ""):
        out.append(body[last:match.start()])
        tag = match.group(1)
        inner = inners[index] if index < len(inners) else ""
        out.append("<%s>%s</%s>" % (tag, inner, tag))
        last = match.end()
        index += 1
    out.append((body or "")[last:])
    return "".join(out)


@dataclass(frozen=True)
class Guarded:
    """一块交给模型的形状: 媒体与行内代码是占位记号, 链接是 `<a id="Ln">`.

    `raw` 说这块有没有被护住的东西: 没有的时候 `html` 就是纯文字, 答案要转义; 有的时候答案
    是 HTML, 要按 `unshield` 还原后原样回写.
    """

    html: str
    tokens: tuple
    links: tuple
    raw: bool
    inner: str


def shields(body: str) -> list:
    """把每一块的里面换成模型能安全翻的那份 (AIHOT `translate.ts` 的 `shield`)."""
    return [shield(inner) for _tag, inner in _parts(body)]


def shield(inner: str) -> Guarded:
    """一块的内部: 媒体与行内代码换记号 (只取最外层), 链接换 `<a id="Ln">` 并把属性寄存."""
    text = inner or ""
    tokens: list = []
    links: list = []
    for tag in _TOKENS:
        pattern = re.compile(r"<%s\b[^>]*>" % tag, re.I) if tag in _VOID else \
            re.compile(r"<%s\b[^>]*>.*?</%s>" % (tag, tag), re.S | re.I)
        while True:
            found = pattern.search(text)
            if not found:
                break
            tokens.append(found.group(0))
            text = text[:found.start()] + _SEAL % (len(tokens) - 1) + text[found.end():]

    def _swap(match: re.Match) -> str:
        links.append(match.group(0))
        return '<a id="L%d">' % (len(links) - 1)

    text = _OPEN_A_RE.sub(_swap, text)
    raw = bool(tokens or links)
    return Guarded(html=text if raw else html.unescape(inner or ""),
                   tokens=tuple(tokens), links=tuple(links), raw=raw, inner=inner or "")


def unshield(translated: str, guarded: Guarded) -> str | None:
    """译文把媒体、代码与链接放回去. 少了、多了或重复了任何一个记号, 这块作废 (`None`)."""
    source = translated or ""
    counts: dict = {}
    for match in _TOKEN_RE.finditer(source):
        number = int(match.group(1))
        counts[number] = counts.get(number, 0) + 1
    if len(counts) != len(guarded.tokens) or any(counts.get(i) != 1 for i in range(len(guarded.tokens))):
        return None

    seen: list = []
    broken = [False]

    def _swap(match: re.Match) -> str:
        values = _attrs(_ATTR_RE.findall(match.group(0)))
        found = _ID_RE.match(values.get("id", ""))
        number = int(found.group(1)) if found else -1
        if number < 0 or number >= len(guarded.links) or number in seen:
            broken[0] = True
            return match.group(0)
        seen.append(number)
        whole = guarded.links[number]
        attrs = whole[whole.find(" ") + 1:-1].strip() if " " in whole[:-1] else ""
        return ("<a %s>" % attrs) if attrs else "<a>"

    text = _OPEN_A_RE.sub(_swap, source)
    if broken[0] or len(seen) != len(guarded.links):
        return None
    return _TOKEN_RE.sub(lambda m: guarded.tokens[int(m.group(1))], text)


def restore(guarded: Guarded, answer: str) -> str | None:
    """块内译文 -> 能直接回写的块内 HTML. 纯文字块转义回去, 护过的块走 `unshield` 对账."""
    if not answer:
        return None
    if not guarded.raw:
        return html.escape(str(answer), quote=False)
    return unshield(str(answer), guarded)


def media_urls(items) -> list:
    """把存着的那一格归一成 URL 串: 新行是对象 (带宽高), 老行是纯 URL 串."""
    out: list = []
    for item in items or []:
        url = item.get("url") if isinstance(item, dict) else item
        if url:
            out.append(str(url))
    return out


def images(body: str, limit: int = 12) -> list:
    """正文里的图, 按出现顺序, 最多 `limit` 张 (AIHOT `content/extract.ts`: 12).
    卡片媒体视图与理解档的首图都读它, 所以宽高一起留下. 没有宽高的是 `None`, 不是 0. """
    out: list = []
    for match in _IMG_RE.finditer(body or ""):
        values = _attrs(_ATTR_RE.findall(match.group(0)))
        src = (values.get("src") or "").strip()
        if not src or _tracking(src, values.get("width"), values.get("height")):
            continue
        out.append({"kind": "image", "url": html.unescape(src),
                    "width": int(values["width"]) if (values.get("width") or "").isdigit() else None,
                    "height": int(values["height"]) if (values.get("height") or "").isdigit() else None})
        if len(out) >= limit:
            break
    return out


def clip(body: str, limit: int = MAX_CHARS) -> str:
    """超长就在块的边界上截: 截半个标签会印出一页乱码, 而正文的头几段就是读者要的那几段."""
    if len(body) <= limit:
        return body
    out: list[str] = []
    size = 0
    for match in _BLOCK_RE.finditer(body):
        whole = match.group(0)
        if size + len(whole) > limit:
            break
        out.append(whole)
        size += len(whole)
    return "".join(out)