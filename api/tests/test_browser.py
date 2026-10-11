"""The rendering lane: one browser, many threads, and no page worth failing a token over.

The driver is faked here on purpose. What is worth testing is not playwright - it is the
seam around it: a loop on its own thread, tabs that stay capped under fifteen callers at
once, a page that fails without taking the browser down with it, and a browser that never
starts without costing every later token the same wait.
"""

from __future__ import annotations

import logging
import threading
import time

from app.sources.browser import Renderer, _record, _same_site


class FakeChromium:
    """Stands in for the real driver: same three calls, no browser."""

    def __init__(self, *, settle: int, timeout: float, pages: dict, live: dict,
                 delay: float = 0.0) -> None:
        self.settle = settle
        self.timeout = timeout
        self.pages = pages
        self.live = live
        self.delay = delay
        pages["made"] = pages.get("made", 0) + 1

    async def start(self) -> None:
        self.pages["started"] = self.pages.get("started", 0) + 1

    async def page(self, url: str) -> str:
        self.live["now"] = self.live.get("now", 0) + 1
        self.live["peak"] = max(self.live.get("peak", 0), self.live["now"])
        try:
            if self.delay:
                await _sleep(self.delay)
            if url.endswith("/broken"):
                raise RuntimeError("this page is not there")
            return "<html><body>%s</body></html>" % url
        finally:
            self.live["now"] -= 1

    async def stop(self) -> None:
        self.pages["stopped"] = self.pages.get("stopped", 0) + 1


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def driver(pages: dict, live: dict, delay: float = 0.0):
    def make(**kwargs):
        return FakeChromium(pages=pages, live=live, delay=delay, **kwargs)

    return make


def test_a_page_comes_back_as_the_document_the_browser_drew():
    pages: dict = {}
    renderer = Renderer(driver=driver(pages, {}))
    try:
        assert renderer.page("https://a.xyz") == "<html><body>https://a.xyz</body></html>"
        assert pages["made"] == 1, "the browser is started once, not once a page"
        renderer.page("https://b.xyz")
        assert pages["made"] == 1
    finally:
        renderer.close()


def test_a_page_that_fails_is_an_empty_string_and_not_the_end_of_the_browser():
    pages: dict = {}
    renderer = Renderer(driver=driver(pages, {}))
    try:
        assert renderer.page("https://a.xyz/broken") == ""
        assert renderer.page("https://a.xyz") != "", "the browser is still there"
    finally:
        renderer.close()


def test_a_browser_that_will_not_start_is_not_waited_for_twice():
    pages: dict = {}

    def refuse(**kwargs):
        raise RuntimeError("no playwright today")

    renderer = Renderer(driver=refuse)
    started = time.monotonic()
    assert renderer.page("https://a.xyz") == ""
    assert renderer.page("https://b.xyz") == ""
    assert time.monotonic() - started < 5, "the second token does not wait again"
    renderer.close()


def test_the_open_tabs_stay_capped_under_more_callers_than_the_cap():
    pages: dict = {}
    live: dict = {}
    renderer = Renderer(pages=2, driver=driver(pages, live, delay=0.05))
    try:
        out: list[str] = []
        threads = [threading.Thread(target=lambda u=u: out.append(renderer.page(u)))
                   for u in ("https://%d.xyz" % n for n in range(12))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert len(out) == 12
        assert live["peak"] <= 2, "the cap is the cap: %s tabs were open at once" % live["peak"]
    finally:
        renderer.close()


def test_a_render_that_lands_on_another_host_names_both(caplog):
    """The address a page walked the browser to is the one fact a blocked request needs.

    Nothing recorded it until 2026-10-12, when a rendered page walked the browser to a domain
    the machine's own security software blocked, and no log line could name a single candidate
    site or say that the render had left the host it asked for. One line carries both
    addresses, and the request the machine refused is named by host beside them.
    """
    with caplog.at_level(logging.INFO, logger="app.sources.browser"):
        _record("https://zikeke.app/", "http://alerttrade.us/",
                [("http://alerttrade.us/x.js", "net::ERR_BLOCKED_BY_CLIENT")])
    assert "https://zikeke.app/" in caplog.text, "the site that was asked for"
    assert "http://alerttrade.us/" in caplog.text, "the site it ended on"
    assert "net::ERR_BLOCKED_BY_CLIENT" in caplog.text, "why the request never arrived"
    assert "ended on another host" in caplog.text


def test_a_site_own_housekeeping_is_not_another_host(caplog):
    """A bare name against its own www., and one scheme against another, are one site.

    Both are how a site moves a reader to its canonical address, and a warning on each of them
    would bury the line the test above is about.
    """
    with caplog.at_level(logging.INFO, logger="app.sources.browser"):
        _record("http://example.com/a", "https://www.example.com/a", [])
    assert "ended on another host" not in caplog.text
    assert _same_site("example.com", "www.example.com")
    assert _same_site("a.example.com", "example.com")
    assert not _same_site("example.com", "alerttrade.us")


def test_closing_stops_the_browser_and_the_thread_it_runs_on():
    pages: dict = {}
    renderer = Renderer(driver=driver(pages, {}))
    renderer.page("https://a.xyz")
    renderer.close()
    assert pages["stopped"] == 1
    assert renderer._thread is None or not renderer._thread.is_alive()
    renderer.close()  # closing twice is not an error
    assert renderer.page("https://b.xyz") != "", "a later page starts a new browser"
    renderer.close()
