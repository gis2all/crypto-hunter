"""图片的多规格与签名代理 (AIHOT 的 `media/imgproxy.ts` + `media/renditions.ts`)。

卡片、正文与 RSS 里的图不再直接用源给的地址, 而是走站内一条签名地址:

    /api/img-proxy?u=<源地址>&mode=<规格>&exp=<到期>&sig=<签名>

三个理由, 缺一个这套东西就不值得:

1. **热链与防盗链**. 源站的图会被它自己的 Referer 规则挡住, 也会随着源站改路径而变成一片
   破图 —— 一条两个月前转发出去的卡片, 它的图不该由源站今天的心情决定。
2. **多规格**. 一张 4000px 的原图印在 112px 的卡片格子里, 是让每个读者替我们付流量。
   `mode` 是一个规格名, 代理按它缩到固定宽度; 同一张图在卡片、正文与分享卡上是三个地址,
   于是浏览器与前面的缓存各存一份小的。
3. **可撤销**. 签名带到期时间 (`exp`), 所以一个地址能过期。它是"这条链接还能不能取"的
   唯一开关, 而不用去封一个 IP 段。

`mode` 与 `exp` 都进签名, 所以改了任何一个都作废重签 —— 签名保护的正是"这个规格、这个
到期", 不是那个地址本身。

密钥从 `CH_IMG_PROXY_SECRET` 读; 环境里没有就用库里的那一枚, 库里也没有就当场生成一枚
写回去 (`feed_settings`)。所以**第一次用就自动配好了**, 不用谁记得去填一个变量 —— 而
重启不会换密钥, 已经发出去的地址照旧能取。

没配到密钥也**不是关掉这一层**: 见 `proxied_image` 的兜底。
"""

from __future__ import annotations

import hashlib
import hmac
import io
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from urllib.parse import urljoin

from . import site
from .lib import http_policy

log = logging.getLogger(__name__)

#: 规格名 → 目标宽度 (AIHOT 的 `IMAGE_WIDTHS`)。名字进网址, 所以是稳定的词, 不是数字。
IMAGE_WIDTHS: dict[str, int] = {
    "avatar": 96, "card": 336, "thumb": 720, "full": 1600, "og": 1200,
    "avatar-48": 48, "avatar-96": 96,
    "image-336": 336, "image-720": 720, "image-1200": 1200, "image-1600": 1600,
}

#: 一组候选规格, 按用途分组 (AIHOT 的 `RESPONSIVE_MODES`)。
RESPONSIVE_MODES: dict[str, tuple[str, ...]] = {
    "avatar": ("avatar-48", "avatar-96"),
    "card": ("image-336", "image-720"),
    "body": ("image-720", "image-1200", "image-1600"),
    "hero": ("image-720", "image-1200", "image-1600"),
}

#: 到期往上取整到一天 —— 同一个地址在一天的边界上换一次, 于是边缘与浏览器能整天复用一份。
WINDOW_SECONDS = 24 * 3600
#: 寿命两天: 转发出去的卡片还能开着, 又不会变成一个永不过期的公开地址。
LIFETIME_SECONDS = 48 * 3600
#: 签名取前 16 个十六进制位 (64 bit)。全长的十六进制不可压缩, 一页列表里它是实打实的字节。
SIG_HEX = 16

#: 库里那一枚密钥的名字。
SECRET_KEY = "img_proxy_secret"

#: 签名只可能是这两个长度之一: 现在的 16 位, 与早期发出去的 64 位全长。
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

_cached: str | None = None


def secret() -> str:
    """签名密钥: 环境变量优先, 其次库里那一枚, 都没有就当场生成一枚写回去。"""
    global _cached
    if _cached:
        return _cached
    configured = (os.environ.get("CH_IMG_PROXY_SECRET") or "").strip()
    if configured:
        _cached = configured
        return _cached
    from ..store import db

    stored = db.feed_setting_get(SECRET_KEY)
    if not stored:
        stored = secrets.token_urlsafe(32)
        try:
            db.feed_setting_put(SECRET_KEY, stored)
        except Exception:  # 写不进去也照用这一枚: 本次进程内的地址仍然自洽
            log.warning("could not persist the image proxy secret", exc_info=True)
    _cached = stored
    return _cached


def forget() -> None:
    """下一次现生成/现读一枚。管理台重签密钥时用 ——
    一枚泄露的密钥要能被人换掉，否则「换密钥」只是一个说法。
    """
    global _cached
    _cached = None


def signature(url: str, mode: str, exp: int | str) -> str:
    return hmac.new(secret().encode("utf-8"), ("%s|%s|%s" % (url, mode, exp)).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def proxy_expiry(now: datetime | None = None, lifetime_seconds: int = LIFETIME_SECONDS) -> int:
    """到期时刻: 往上取整到一天, 且至少 `lifetime_seconds` 之后。"""
    moment = now or datetime.now(timezone.utc)
    raw = moment.timestamp() + lifetime_seconds
    return int(-(-raw // WINDOW_SECONDS) * WINDOW_SECONDS)


def proxy_path(url: str, mode: str, now: datetime | None = None,
               lifetime_seconds: int = LIFETIME_SECONDS) -> str:
    exp = proxy_expiry(now, lifetime_seconds)
    sig = signature(url, mode, exp)[:SIG_HEX]
    return "/api/img-proxy?u=%s&mode=%s&exp=%d&sig=%s" % (
        _quote(url), _quote(mode), exp, sig)


def proxied_image(url: str | None, mode: str, absolute: bool = False,
                  now: datetime | None = None) -> str:
    """一条图的代理地址。取不了的那种 (data: / 跟踪像素 / 不是 http) 原样还回。

    `data:` 原样还回是因为它本来就不出网 —— 替它签一个名毫无意义, 而把它丢掉会让一条
    自带内联图的记录看起来像没有图。
    """
    from . import site

    text = (url or "").strip()
    if not text:
        return ""
    if text.startswith("data:"):
        return text
    if not text.startswith(("http://", "https://")):
        return text
    if _tracking(text):
        return ""
    if mode not in IMAGE_WIDTHS:
        mode = "full"
    path = proxy_path(text, mode, now)
    return site.absolute(path) if absolute else path


def proxied_image_set(url: str | None, kind: str, absolute: bool = False,
                      now: datetime | None = None) -> str:
    """一组候选 (给 `srcset` 用): "地址 宽度w, 地址 宽度w"。取不了就回空串。"""
    modes = RESPONSIVE_MODES.get(kind)
    if not modes or not (url or "").strip():
        return ""
    parts = []
    for mode in modes:
        got = proxied_image(url, mode, absolute=absolute, now=now)
        if got:
            parts.append("%s %dw" % (got, IMAGE_WIDTHS[mode]))
    return ", ".join(parts)


_IMG_TAG = re.compile(r"<img\b([^>]*)>", re.I)
_SRC_ATTR = re.compile(r"\ssrc=\"([^\"]+)\"", re.I)
_WIDTH_ATTR = re.compile(r"\bwidth=\"(\d+)\"", re.I)


def proxied_html(body: str, absolute: bool = False, now: datetime | None = None) -> str:
    """把正文里的 `<img>` 换成代理地址, 并给大的那张一组 `srcset`。

    只有**写了宽度**的大图才给候选组: 一张没写宽度的图, 它的显示尺寸由浏览器按原始比例
    决定, 而拉一组固定宽度的候选会改变它的自然大小 (AIHOT 的 `proxyBodyImages` 同一句理由)。
    跟踪像素在这里被整条删掉 —— 正文里的一个广告信标不是内容。
    """
    text = body or ""
    if "<img" not in text.lower():
        return text

    def one(match):
        attrs = match.group(1)
        found = _SRC_ATTR.search(attrs)
        if not found:
            return match.group(0)
        source = html_unescape(found.group(1))
        if _tracking(source):
            return ""
        proxied = proxied_image(source, "full", absolute=absolute, now=now)
        if not proxied:
            return match.group(0)
        extra = ""
        width = _WIDTH_ATTR.search(attrs)
        if width and int(width.group(1)) >= IMAGE_WIDTHS["image-720"]:
            candidates = proxied_image_set(source, "body", absolute=absolute, now=now)
            if candidates:
                extra = ' srcset="%s" sizes="(min-width: 1024px) 760px, calc(100vw - 32px)"' % candidates
        return "<img%s src=\"%s\"%s>" % (attrs[:found.start()], proxied, attrs[found.end():] + extra)

    return _IMG_TAG.sub(one, text)


def html_unescape(text: str) -> str:
    import html as _html

    return _html.unescape(text)


def verify(u: str, mode: str, exp: str, sig: str,
           now: datetime | None = None) -> tuple[bool, str, str]:
    """校验一条代理请求。回 (过没过, 原因, 规格名)。

    原因只有四种, 页面与运维读口都印它: `missing` / `bad-url` / `expired` / `bad-signature`。
    """
    if not u or not exp or not sig:
        return False, "missing", ""
    if len(mode) > 32:
        return False, "missing", ""
    if not exp.isdigit() or len(exp) < 9:
        return False, "missing", ""
    if int(exp) < (now or datetime.now(timezone.utc)).timestamp():
        return False, "expired", ""
    if not u.startswith(("http://", "https://")):
        return False, "bad-url", ""
    given = sig.lower() if _HEX16.match(sig) or _HEX64.match(sig) else ""
    if not given:
        return False, "bad-signature", ""
    want = signature(u, mode, exp)
    if not hmac.compare_digest(given, want[:len(given)]):
        return False, "bad-signature", ""
    return True, "", mode or "full"


def render(data: bytes, mode: str) -> tuple[bytes, str]:
    """按规格缩一张图。缩不了 (不是图 / 本来就小 / 没有 Pillow) 就把原图还回。

    WebP 优先: 同一张图它小一截, 而卡片上那点画质差别看不出来。
    """
    width = IMAGE_WIDTHS.get(mode) or 0
    if not data:
        return data, "application/octet-stream"
    try:
        from PIL import Image
    except Exception:  # 没装 Pillow: 这一层退化成纯粹的转发
        return data, ""
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            native = image.width
            out = image
            if width and native > width:
                height = max(1, round(image.height * width / native))
                out = image.resize((width, height), Image.LANCZOS)
            if out.mode not in ("RGB", "RGBA"):
                out = out.convert("RGBA" if "A" in out.getbands() else "RGB")
            buffer = io.BytesIO()
            out.save(buffer, format="WEBP", quality=82, method=4)
            return buffer.getvalue(), "image/webp"
    except Exception:
        log.debug("image %s could not be rendered; serving it whole", mode, exc_info=True)
        return data, ""


#: 明确的跟踪像素 / 广告信标。它们不是内容, 代理它们只是替广告商数一次人。
_TRACKING_HOSTS = ("facebook.com", "facebook.net", "doubleclick.net", "google-analytics.com",
                   "googletagmanager.com", "scorecardresearch.com", "adnxs.com",
                   "casalemedia.com", "1rx.io", "ad.gt", "pixel.wp.com")


def _tracking(url: str) -> bool:
    host = http_policy.host_of(url)
    return any(host == name or host.endswith("." + name) for name in _TRACKING_HOSTS)


def _quote(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="")


#: 一条图最多多大。8 MB 够放下任何一张卡片上的原图, 而它是一道闸: 一个 4 GB 的
#: "图" 不是一个图, 是一个把内存吃光的办法。
MAX_BYTES = 8 * 1024 * 1024
#: 跟随几次跳转。每一跳都重过一遍 SSRF 守卫 —— 一个公开地址可以把人送到 `127.0.0.1`。
MAX_HOPS = 3


class UpstreamError(RuntimeError):
    """上游取不到这张图。调用方把它翻成 502, 不重试。"""


def fetch(url: str, timeout: float = 12.0) -> bytes:
    """取一张图。这里没有缓存 —— 缓存是前面那一层的事, 我们只把字节交出去。"""
    import httpx

    target = http_policy.check(url)
    headers = {"User-Agent": site.crawler_headers()["User-Agent"],
               "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    with httpx.Client(timeout=timeout) as client:
        for _ in range(MAX_HOPS + 1):
            with client.stream("GET", target, headers=headers) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location") or ""
                    if not location:
                        raise UpstreamError("redirect without a location")
                    target = http_policy.check(urljoin(target, location))
                    continue
                if response.status_code >= 400:
                    raise UpstreamError("upstream said %d" % response.status_code)
                if not (response.headers.get("content-type") or "").startswith("image/"):
                    raise UpstreamError("not an image: %s" % (response.headers.get("content-type") or "?"))
                total = 0
                chunks: list[bytes] = []
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise UpstreamError("image larger than %d bytes" % MAX_BYTES)
                    chunks.append(chunk)
                if not chunks:
                    raise UpstreamError("empty body")
                return b"".join(chunks)
    raise UpstreamError("too many redirects")
