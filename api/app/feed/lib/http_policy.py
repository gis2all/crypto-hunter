"""出站请求的一道门（AIHOT 的 `lib/url.ts` + `contracts/http-policy.ts`）。

两件事：

1. **URL 归一**：同一个页面的两种写法不该被当成两条材料（去段参、去 fragment、
   统一主机大小写）。
2. **SSRF 守卫**：一个来自材料的链接可以指向任何地方。所以只允许 http/https，
   并且主机名不能解到内网/本机/云元数据地址。这一层不发请求，只判断；
   发请求的地方在 `sources/http.py`。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

ALLOWED_SCHEMES = ("http", "https")


class RefusedURL(ValueError):
    """这个地址不该被访问。调用方把它记成一次失败，而不是重试。"""


def normalize(url: str) -> str:
    """去掉 fragment 与常见跟踪参数，主机小写，默认端口去掉。空串返回空串。"""
    text = (url or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if not parts.scheme:
        return text
    host = (parts.hostname or "").lower()
    netloc = host
    if parts.port and parts.port not in (80, 443):
        netloc = "%s:%d" % (host, parts.port)
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, _tidy_query(parts.query), ""))


_TRACKING = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "fbclid", "gclid")


def _tidy_query(query: str) -> str:
    if not query:
        return ""
    kept = []
    for pair in query.split("&"):
        name = pair.split("=", 1)[0].lower()
        if name in _TRACKING:
            continue
        kept.append(pair)
    return "&".join(kept)


def host_of(url: str) -> str:
    return (urlsplit(url or "").hostname or "").lower()


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified)


#: 字面量上就不该访问的名字。它们的解析结果一眼可知, 不必去问 DNS。
_LOCAL_NAMES = ("localhost", "metadata.google.internal", "metadata.goog")


def guard(url: str) -> str:
    """不做解析的那一半守卫: 字面量的本机/云元数据地址、非 http(s)、带凭证。

    每一次出站请求都要过它 —— 一个来自材料的链接可以指向任何地方, 而
    `169.254.169.254` / `localhost` 这类地址不需要 DNS 就知道不能碰.
    要做完整的那一半 (连主机也解析一遍) 的调用方用 `check()`.
    """
    text = normalize(url)
    if not text:
        raise RefusedURL("empty url")
    parts = urlsplit(text)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise RefusedURL("scheme not allowed: %s" % (parts.scheme or "(none)"))
    if parts.username or parts.password:
        raise RefusedURL("credentials in url")
    host = parts.hostname or ""
    if not host:
        raise RefusedURL("url has no host")
    if host in _LOCAL_NAMES or host.endswith((".localhost", ".internal", ".local")):
        raise RefusedURL("host not routable: %s" % host)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return text
    if not _is_public(host):
        raise RefusedURL("host resolves to a private address: %s" % host)
    return text


def check(url: str) -> str:
    """归一之后再守一遍。返回归一过的地址；不该访问就抛 `RefusedURL`。"""
    text = normalize(url)
    if not text:
        raise RefusedURL("empty url")
    parts = urlsplit(text)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise RefusedURL("scheme not allowed: %s" % (parts.scheme or "(none)"))
    if parts.username or parts.password:
        raise RefusedURL("credentials in url")
    host = parts.hostname or ""
    if not host or host in ("localhost",) or host.endswith(".localhost") or host.endswith(".internal"):
        raise RefusedURL("host not routable: %s" % host)
    try:
        info = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise RefusedURL("host does not resolve: %s" % host) from exc
    for entry in info:
        address = entry[4][0]
        if not _is_public(address):
            raise RefusedURL("host resolves to a private address: %s" % address)
    return text
