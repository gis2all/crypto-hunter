"""The rendering lane: a real browser, for pages that only exist once JavaScript has run.

Most project sites arrive whole from the plain lane, and the browser is for the ones that do
not. Three of the sites this pipeline read on 2026-09-27 - memespad.com, www.arcdex.online
and usearckit.online - came back as 1.7 to 3.3 KB documents holding eight to ten words of
text and no contract address. Rendered, the same three carry 377 to 600 words, and the
address is on two of them. The next cheapest site in that set arrived with ninety-four words
of its own, so a shell is not a matter of degree: it is a document with nothing in it.

Four to six seconds a page against a fraction of a second is why this is a fallback rather
than a lane. Twenty pages through one shared browser were measured at 5.0 s of wall clock,
so the browser is started once and the open tabs are capped.

The pages are strangers' pages: every one arrived as a link a round harvested, and none was
vouched for. On 2026-10-12 one of them walked the browser to a domain the machine's own
security software recognized and blocked, and this file could not say which URL it was - it
kept no record of what it had opened. So every render writes the address it asked for, the
address it ended on and the requests that failed, and Chromium's sandbox - which Playwright
leaves off by default - stays on. See SANDBOX.

Playwright's synchronous API cannot be driven from several threads - it raises and takes the
browser down under them - so the one event loop lives on a thread of its own and callers hand
it work from wherever they are. The application runs fifteen of those threads at a time.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import threading
from typing import Any, Callable
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

#: Tabs open at once. Memory is the limit, not the site, and the round reads six candidate
#: sites at a time, so six is the width the callers already have.
PAGES = 6

#: Milliseconds to let the page draw after DOMContentLoaded. Waiting for the network to go
#: idle instead measured 21.8 s on memespad.com against 6.4 s here, for the same document: a
#: page with a live price feed never goes idle. Blocking images and stylesheets was measured
#: too, and it made the same twenty pages slower (8.1 s) while leaving some of them blank.
SETTLE = 2500

#: Seconds one page may take, and seconds the browser gets to start.
TIMEOUT = 25.0
STARTUP = 45.0

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/153.0.0.0 Safari/537.36")

#: Chromium's own sandbox, left on. Playwright passes --no-sandbox unless this is asked for,
#: and this lane loads whatever link a round harvested. Turning it on was measured on
#: 2026-10-12 against eight sites the rounds had already read: the same launch, and the same
#: document - six of the eight to the same word count - at a median of 4.5 s a page against
#: 5.0 s with the flag. Checked against the launched process rather than by reading this line:
#: the flag is gone from its command line.
SANDBOX = True


def _host(url: str) -> str:
    """The host a URL names, lowercased, or "" when it names none."""
    return (urlsplit(url).hostname or "").lower()


def _same_site(left: str, right: str) -> bool:
    """Whether two hosts are one site.

    A scheme change, and a bare name against its own `www.`, are a site's housekeeping
    rather than a jump, and reading either as one would make this log noise nobody reads.
    """
    if left == right:
        return True
    return left.endswith("." + right) or right.endswith("." + left)


def _record(url: str, final: str, failed: list[tuple[str, str]]) -> None:
    """One line per render: the address asked for, the address it ended on, what never came.

    This lane kept no record of the pages it opened, which is why a page that walked the
    browser to a domain the machine's own security software had blocked could not be traced
    back to the token that paid for it (2026-10-12). The failed requests are the other half of
    that answer: a request the machine refused arrives here as `net::ERR_BLOCKED_BY_CLIENT`,
    named by host.
    """
    missed = sorted({"%s (%s)" % (_host(item) or item, why) for item, why in failed})
    if len(missed) > 6:
        missed = missed[:6] + ["+%d more" % (len(missed) - 6)]
    tail = " | failed: " + ", ".join(missed) if missed else ""
    log.info("render %s -> %s%s", url, final, tail)
    if not _same_site(_host(url), _host(final)):
        log.warning("render %s ended on another host: %s", url, final)


class Chromium:
    """The playwright calls and nothing else, so the rest of this file can be tested."""

    def __init__(self, *, settle: int, timeout: float) -> None:
        self.settle = settle
        self.timeout = timeout * 1000
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None

    async def start(self) -> None:
        # Imported here, not at module scope: the dependency is optional, and a missing one
        # must not break the pipeline for the sites that do not need it.
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True,
                                                              chromium_sandbox=SANDBOX)
        self._context = await self._browser.new_context(user_agent=UA)

    async def page(self, url: str) -> str:
        tab = await self._context.new_page()
        # Attached before the navigation, not after: a request the machine refuses is
        # answered before the document finishes loading, and a listener attached afterwards
        # would miss exactly the events this line exists for.
        failed: list[tuple[str, str]] = []
        tab.on("requestfailed", lambda request: failed.append(
            (request.url, request.failure or "failed")))
        try:
            try:
                await tab.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
                await tab.wait_for_timeout(self.settle)
                document = await tab.content()
            finally:
                # Written even when the navigation itself failed, which is the case worth
                # reading most: a document the machine refused never gets this far, and the
                # address it was refused at is the one line that answers "which URL".
                _record(url, tab.url, failed)
            return document
        finally:
            await tab.close()

    async def stop(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception as exc:  # a browser already gone is not a problem worth raising
            log.info("closing the browser: %r", exc)


class Renderer:
    """One browser on one loop, handed work by whichever thread needs a page."""

    def __init__(self, *, pages: int = PAGES, settle: int = SETTLE, timeout: float = TIMEOUT,
                 driver: Callable[..., Any] = Chromium) -> None:
        self.pages = pages
        self.settle = settle
        self.timeout = timeout
        self.driver = driver
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._gate: asyncio.Semaphore | None = None
        self._closed: asyncio.Event | None = None
        self._one: Any = None
        self._refused = False

    def page(self, url: str) -> str:
        """The rendered document, or an empty string if the browser could not answer.

        Never raises. Callers reach for this after the plain lanes have already failed, and
        no failure here is worth turning a token into a failed token.
        """
        loop = self._start()
        if loop is None:
            return ""
        future = asyncio.run_coroutine_threadsafe(self._render(url), loop)
        try:
            return future.result(self.timeout + self.settle / 1000 + 10)
        except Exception as exc:
            log.info("render of %s did not answer: %r", url, exc)
            future.cancel()
            return ""

    def close(self) -> None:
        """Stop the browser and the thread that runs it. Safe to call twice."""
        with self._lock:
            loop, thread, closed = self._loop, self._thread, self._closed
            self._loop, self._thread, self._closed = None, None, None
        if loop is None or closed is None:
            return
        loop.call_soon_threadsafe(closed.set)
        if thread is not None:
            thread.join(timeout=10)

    async def _render(self, url: str) -> str:
        assert self._gate is not None
        async with self._gate:
            return await self._one.page(url)

    def _start(self) -> asyncio.AbstractEventLoop | None:
        with self._lock:
            if self._refused:
                return None
            if self._loop is None:
                self._loop = self._spawn()
            return self._loop

    def _spawn(self) -> asyncio.AbstractEventLoop | None:
        loop = asyncio.new_event_loop()
        self._ready.clear()
        thread = threading.Thread(target=self._run, args=(loop,), name="render",
                                  daemon=True)
        self._thread = thread
        thread.start()
        if not self._ready.wait(STARTUP) or self._refused:
            self._refused = True
            self._thread = None
            return None
        return loop

    def _run(self, loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except Exception as exc:
            log.warning("the render loop stopped: %r", exc)
            self._refused = True
        finally:
            self._ready.set()
            loop.close()

    async def _serve(self) -> None:
        self._closed = asyncio.Event()
        self._gate = asyncio.Semaphore(self.pages)
        try:
            self._one = self.driver(settle=self.settle, timeout=self.timeout)
            await self._one.start()
        except Exception as exc:
            log.error("no browser, so no page will be rendered for the rest of this process:"
                      " %r - every later render answers with an empty document, and nothing"
                      " downstream can tell that from a site with nothing on it", exc)
            self._refused = True
            self._ready.set()
            return
        self._ready.set()
        try:
            await self._closed.wait()
        finally:
            await self._one.stop()


_default: Renderer | None = None
_default_lock = threading.Lock()


def render(url: str) -> str:
    """The process's own browser, started the first time a page has to be drawn."""
    global _default
    with _default_lock:
        if _default is None:
            _default = Renderer()
            atexit.register(_default.close)
        renderer = _default
    return renderer.page(url)


def shutdown() -> None:
    """Close the browser if one was ever started. For the app lifespan, and for tests."""
    global _default
    with _default_lock:
        renderer, _default = _default, None
    if renderer is not None:
        renderer.close()
