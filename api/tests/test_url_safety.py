"""URL boundaries shared by report links and outbound page reads."""

from __future__ import annotations

import socket

import httpx
import pytest

from app.sources import http as transport
from app.sources.url_safety import UnsafeUrl, require_public_url, safe_http_url


@pytest.mark.parametrize("raw", [
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "blob:https://example.com/id",
    "file:///C:/Windows/win.ini",
    "mailto:test@example.com",
    "//example.com/path",
    "https://user:pass@example.com/",
    "https://example.com\\@127.0.0.1/",
    "https://example.com:8443/",
    "http://127.0.0.1/",
    "http://[::1]/",
    "http://localhost/",
    "http://intranet/",
    "http://198.18.0.1/",
    "https://example.com/\nnext",
])
def test_display_links_accept_only_plain_public_web_urls(raw):
    assert safe_http_url(raw) is None


@pytest.mark.parametrize("raw, expected", [
    ("https://example.com/", "https://example.com/"),
    (" HTTP://Example.COM/path?q=1 ", "http://example.com/path?q=1"),
    ("https://8.8.8.8/", "https://8.8.8.8/"),
])
def test_display_links_keep_ordinary_public_urls(raw, expected):
    assert safe_http_url(raw) == expected


def test_public_ipv6_round_trips_with_brackets():
    url = "https://[2606:4700:4700::1111]/"
    assert safe_http_url(url) == url
    assert require_public_url(url) == url


def _resolver(*addresses: str):
    def resolve(host, port, *args, **kwargs):
        rows = []
        for address in addresses:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            rows.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port)))
        return rows

    return resolve


@pytest.mark.parametrize("address", [
    "127.0.0.1",
    "10.0.0.1",
    "169.254.1.1",
    "100.64.0.1",
    "192.0.2.1",
    "::1",
    "fe80::1",
    "::ffff:127.0.0.1",
])
def test_outbound_urls_reject_every_non_global_resolved_address(address):
    with pytest.raises(UnsafeUrl):
        require_public_url("https://example.com/", resolver=_resolver(address))


def test_one_private_answer_rejects_a_host_with_public_answers_too():
    with pytest.raises(UnsafeUrl):
        require_public_url(
            "https://example.com/",
            resolver=_resolver("93.184.216.34", "127.0.0.1"),
        )


def test_outbound_urls_accept_global_answers():
    assert require_public_url(
        "https://example.com/a", resolver=_resolver("93.184.216.34")
    ) == "https://example.com/a"


def test_outbound_urls_accept_a_local_fake_ip_proxy_answer():
    """Clash-style DNS uses 198.18/15 while the proxy still decides the real destination."""
    assert require_public_url(
        "https://example.com/a", resolver=_resolver("198.18.0.218")
    ) == "https://example.com/a"


def test_a_fake_ip_literal_is_not_treated_as_a_public_proxy_answer():
    with pytest.raises(UnsafeUrl):
        require_public_url("http://198.18.0.1/")


def test_dns_failure_is_a_refusal_not_an_unchecked_fetch():
    def fail(*args, **kwargs):
        raise socket.gaierror("no answer")

    with pytest.raises(UnsafeUrl):
        require_public_url("https://missing.example/", resolver=fail)


def test_public_get_follows_redirects_only_after_rechecking_each_hop(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/end"})
        return httpx.Response(200, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    monkeypatch.setattr(transport, "_public_client", lambda: client)
    monkeypatch.setattr(transport, "require_public_url", lambda url, **kw: url)

    assert transport.get("https://example.com/start", public=True) == "ok"
    assert seen == ["https://example.com/start", "https://example.com/end"]


def test_public_get_stops_when_a_redirect_leaves_the_public_web(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"})

    def checked(url, **kwargs):
        if "127.0.0.1" in url:
            raise UnsafeUrl("loopback")
        return url

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    monkeypatch.setattr(transport, "_public_client", lambda: client)
    monkeypatch.setattr(transport, "require_public_url", checked)

    with pytest.raises(transport.SourceError):
        transport.get("https://example.com/start", public=True, tries=1)
    assert seen == ["https://example.com/start"]
