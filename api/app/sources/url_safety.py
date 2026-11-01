"""URL boundaries shared by report links and outbound page reads.

Display links only need an HTTP(S) scheme check. Outbound reads need the stronger
guarantee: every DNS answer must be globally routable, every redirect is checked again,
and the connection itself must use a checked address rather than resolving a second time.
"""

from __future__ import annotations

import html
import ipaddress
import re
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

#: Public project sites are ordinary web endpoints. Other ports are not part of the
#: product and become an easy route to local services.
WEB_PORTS = frozenset((80, 443))
#: Clash-style fake-IP DNS answers live in the benchmarking range. They are not internal
#: addresses and are used only as a local proxy's synthetic destination, but a normal
#: global-unicast check would reject every hostname on such a machine.
SYNTHETIC_PROXY = ipaddress.ip_network("198.18.0.0/15")
_SPACE = re.compile(r"\s")


class UnsafeUrl(ValueError):
    """A URL that may be displayed or fetched only after crossing a denied boundary."""


Resolver = Callable[..., Iterable[tuple]]


def _literal_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _global_address(
    value: str,
    *,
    allow_synthetic: bool = False,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    synthetic = allow_synthetic and address in SYNTHETIC_PROXY
    if not address.is_global and not synthetic:
        raise UnsafeUrl("non-public address: %s" % value)
    return address


def _parsed(raw: str, *, keep_fragment: bool) -> tuple[str, str, int, str]:
    if not isinstance(raw, str) or not raw.strip():
        raise UnsafeUrl("empty URL")
    text = html.unescape(raw).strip()
    if "\\" in text or _SPACE.search(text) or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise UnsafeUrl("URL contains control characters, whitespace, or a backslash")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrl("malformed URL") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise UnsafeUrl("only http and https are allowed")
    if not parts.netloc or not parts.hostname:
        raise UnsafeUrl("URL has no host")
    if parts.username is not None or parts.password is not None:
        raise UnsafeUrl("URL credentials are not allowed")
    host = parts.hostname.rstrip(".").lower()
    if not host:
        raise UnsafeUrl("URL has no host")
    port = port if port is not None else (443 if scheme == "https" else 80)
    if port not in WEB_PORTS:
        raise UnsafeUrl("only ports 80 and 443 are allowed")
    if _literal_host(host):
        _global_address(host)
    else:
        if "." not in host or host == "localhost" or host.endswith(".localhost") \
                or host.endswith(".local"):
            raise UnsafeUrl("local hostnames are not allowed")
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise UnsafeUrl("invalid hostname") from exc
    fragment = parts.fragment if keep_fragment else ""
    host_text = "[%s]" % host if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    netloc = host_text if port == default_port else "%s:%d" % (host_text, port)
    return scheme, host, port, urlunsplit(
        (scheme, netloc, parts.path or "/", parts.query, fragment)
    )


def safe_http_url(raw: str, *, keep_fragment: bool = True) -> str | None:
    """A display-safe absolute web URL, or None when it must not become clickable."""
    try:
        return _parsed(raw, keep_fragment=keep_fragment)[3]
    except UnsafeUrl:
        return None


def resolve_public_host(
    host: str,
    port: int,
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> str:
    """Resolve once and return one address only after every answer passes the public check."""
    if port not in WEB_PORTS:
        raise UnsafeUrl("only ports 80 and 443 are allowed")
    host = str(host).strip("[]").rstrip(".").lower()
    if _literal_host(host):
        return str(_global_address(host))
    try:
        answers = list(resolver(
            host, port, socket.AF_UNSPEC, socket.SOCK_STREAM, socket.IPPROTO_TCP
        ))
    except OSError as exc:
        raise UnsafeUrl("host did not resolve") from exc
    if not answers:
        raise UnsafeUrl("host did not resolve")
    addresses = []
    for answer in answers:
        address = str(answer[4][0]).split("%", 1)[0]
        _global_address(address, allow_synthetic=True)
        addresses.append(address)
    return addresses[0]


def require_public_url(
    raw: str,
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> str:
    """Return a canonical public HTTP(S) URL or raise before any request is opened."""
    scheme, host, port, clean = _parsed(raw, keep_fragment=False)
    resolve_public_host(host, port, resolver=resolver)
    return clean
