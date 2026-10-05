"""信息流那条车道的 UA: 报自己的名字, 不冒充浏览器 (AIHOT 的 `DEFAULT_UA`).

雷达那边的 JSON API 不走这条判断 —— 那些站点把 TLS 指纹与 UA 一起判, 换掉只会静默地少拿到数据,
所以这一份盯的是"信息流用了诚实 UA, 而共用的默认值没被动过".
"""

from __future__ import annotations

from app.feed.sources.kind import rss
from app.sources import http


def test_the_feed_lane_declares_itself_as_a_crawler():
    assert http.crawler_headers(None) is http.UA_CRAWLER
    assert "Chrome" not in http.UA_CRAWLER["User-Agent"]
    assert "CryptoHunterBot" in http.UA_CRAWLER["User-Agent"]
    # 源自己写了 `headers` 就听源的: 某一个站点认别的 UA 时不必改代码.
    mine = {"User-Agent": "something else"}
    assert http.crawler_headers(mine) is mine
    # 雷达的 JSON 车道一个字没动.
    assert "Chrome" in http.UA_JSON["User-Agent"]


def test_the_rss_adapter_sends_the_crawler_ua(monkeypatch):
    seen: dict = {}

    def fake(url, headers, **kw):
        seen["url"] = url
        seen["headers"] = headers
        return 304, {}, ""

    monkeypatch.setattr(rss.http, "get_conditional", fake)
    records, cursor = rss.fetch({"id": "rss:a", "config": {"feedUrl": "https://a/feed"}}, False)
    assert records == [] and cursor == {}
    assert seen["url"] == "https://a/feed"
    assert seen["headers"] is http.UA_CRAWLER
