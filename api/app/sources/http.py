"""One transport for every adapter: pacing, retries, a TTL cache, and two TLS lanes.

The public DEX sites do not all read a Python client the same way. On 2026-09-24,
GMGN answered `requests` and `urllib` with 200 but answered `httpx` with 403 on the
same URL with the same headers: the edge looks at the TLS fingerprint, not the
headers. `curl -s` (Schannel) got 200 as well, which is what pinned it to the
handshake rather than to a missing cookie.

So the transport has two lanes, and adapters pick one:

- `"http"`   - a plain httpx client. Every JSON API uses this lane.
- `"browser"` - curl_cffi impersonating Chrome. GMGN uses this lane.

Nothing outside this module knows which lane a source took; a caller sees either a
response or a `SourceError`.

Pacing is the other half: each host gets a minimum interval between requests, and the
interval is spent while holding that host's lock, so a host is serialised rather than
merely slowed. The numbers come from what the sources tolerated in the reference
implementation - GeckoTerminal is the expensive one (a 429 costs a retry cycle).
"""

from __future__ import annotations

import atexit
import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Iterable, TypeVar
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

from .. import config
from .url_safety import UnsafeUrl, require_public_url, resolve_public_host

T = TypeVar("T")

#: Seconds of Chromium that curl_cffi replays. Changing this can change who answers.
BROWSER = "chrome"

UA_JSON = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, */*",
}
UA_HTML = {**UA_JSON, "Accept": "*/*"}
UA_GMGN = {**UA_HTML, "Referer": "https://gmgn.ai/"}


def explorer_headers(explorer: str | None) -> dict:
    """The explorer own origin as the referer.

    This used to be one hardcoded Arc referer, sent to whichever explorer the chain
    config named. A site that checks the referer answers that headers set with a 403 or a
    Cloudflare interstitial, and the refusal reads downstream as "this token is not in the
    explorer" rather than as a header problem - the wrong kind of fact to write into a
    report. The chain the caller already holds names its own explorer, so the header is
    derived from it, and the result is byte-identical to the old constant on Arc.
    """
    base = str(explorer or "").rstrip("/")
    return {**UA_JSON, "Referer": base + "/"} if base else UA_JSON

#: Host -> minimum seconds between two requests. Not a rate limit someone published;
#: a cadence the reference implementation ran for months without getting blocked.
MIN_INTERVAL: dict[str, float] = {
    "api.geckoterminal.com": 2.2,
    "api.radardex.pro": 0.5,
    "gmgn.ai": 0.45,
    "api.gopluslabs.io": 0.3,
    "sourcify.dev": 0.3,
    "api.dexscreener.com": 0.25,
    # GitHub's unauthenticated API allows 60 requests an hour. A 新机会 scan reads a handful of
    # organisations and remembers each answer for an hour (`opportunity/scan.py` GITHUB_TTL), so
    # this interval is what keeps a scan from spending that budget on retries.
    "api.github.com": 1.0,
    # 补链阶段的搜索：一个动作一次请求，引擎对连续查询很敏感，一秒是实测能过线的间隔。
    "lite.duckduckgo.com": 2.0,
    "www.bing.com": 1.5,
    "mcp.exa.ai": 0.5,
    "search.parallel.ai": 0.5,
    # The board pages `dexscreener_rank` reads, not the API: a sweep asks for six of them
    # back to back, and they are the site's own HTML rather than a published feed.
    "dexscreener.com": 0.5,
    # The public Solana node, one call per token in the enrich pass. Its limit is not
    # published, so this is deliberately slower than the default: a hundred-token pass that
    # gets 429ed leaves every row "unread", which is worse than the seconds it saves.
    "api.mainnet-beta.solana.com": 0.5,
    "rpc.arc-scan.org": 0.15,
    # Four.meme's public list, read over POST. No published limit; this is a cadence its
    # own site runs at, and a sweep is a handful of requests.
    "four.meme": 0.5,
    "explorer.arc.io": 1.0,
    # Robinhood: its own node and explorer first, then the three launchpad feeds. The feeds
    # answer the whole chain in one or two pages, so the cadence only has to keep a second
    # round from stacking on the first.
    "rpc.mainnet.chain.robinhood.com": 0.15,
    "robinhoodchain.blockscout.com": 1.0,
    "coinbarrel.com": 0.5,
    "api.pez.family": 0.5,
    "peeps.wtf": 0.5,
    # The fourth Robinhood launchpad, and the two Solana ones. PONS pages 40 rows at a time
    # and keeps its band rows deep in both orders, so its sweep is this chain's longest run
    # of requests; the Solana pair answer a page apiece, and the cadence there is to keep two
    # rounds from stacking rather than because either feed asked for it.
    "ponsfamily.com": 0.5,
    "launch-mint-v1.raydium.io": 0.3,
    "frontend-api-v3.pump.fun": 0.3,
}
DEFAULT_INTERVAL = 0.2


class SourceError(RuntimeError):
    """A request that used up its retries. What a missing source means is the caller's call."""


class _Pacer:
    """Serialises one host: the next request waits for the previous interval to elapse."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.time()
            at = max(now, self._next_at)
            self._next_at = at + self.interval
        delay = at - now
        if delay > 0:
            time.sleep(delay)


_pacers: dict[str, _Pacer] = {}
_pacers_lock = threading.Lock()

#: Per-host request tallies, counters only. A round's wall clock is mostly spent inside
#: `get`, and the phase timings say which stage spent it without saying on whom. These are
#: what turn "the gate takes seventeen minutes" into "the explorer took eleven of them",
#: which is the difference between a calibration and a guess. Cumulative for the process;
#: `stats(reset=True)` clears them at the top of a round.
_stats: dict[str, dict[str, float]] = {}
_stats_lock = threading.Lock()


def _tally(host: str, key: str, seconds: float = 0.0) -> None:
    with _stats_lock:
        row = _stats.setdefault(host, {"ok": 0.0, "fail": 0.0, "retry": 0.0, "slow": 0.0})
        row[key] = row.get(key, 0.0) + 1
        row["slow"] = row["slow"] + seconds


def tally(host: str, key: str, seconds: float = 0.0) -> None:
    """Count one request against a host that does not go through `get`.

    The chain's own node is asked over JSON-RPC POST, never through `get`, so the host a
    round spends the most time on was the one host the per-host line could not name. Same
    counters as `get`, so the same log line carries it.
    """
    _tally(host, key, seconds)


def stats(*, reset: bool = False) -> dict[str, dict[str, float]]:
    with _stats_lock:
        snapshot = {host: dict(row) for host, row in _stats.items()}
        if reset:
            _stats.clear()
    return snapshot


def stats_lines(limit: int = 8) -> list[str]:
    """The hosts that ate the round, worst first, as log lines."""
    rows = stats()
    ordered = sorted(rows.items(), key=lambda kv: -(kv[1].get("slow") or 0.0))
    return [
        "%s ok=%d fail=%d retry=%d waited=%.0fs" % (
            host, row.get("ok", 0), row.get("fail", 0), row.get("retry", 0), row.get("slow", 0.0),
        )
        for host, row in ordered[:limit]
    ]


def pacer(url: str) -> _Pacer:
    host = urlsplit(url).netloc or "?"
    with _pacers_lock:
        if host not in _pacers:
            _pacers[host] = _Pacer(MIN_INTERVAL.get(host, DEFAULT_INTERVAL))
        return _pacers[host]


_client: httpx.Client | None = None
_client_lock = threading.Lock()


class _PinnedBackend(httpcore.SyncBackend):
    """Connect to a checked DNS answer, while keeping the original host for TLS SNI."""

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        address = resolve_public_host(host, port)
        return super().connect_tcp(address, port, timeout, local_address, socket_options)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise UnsafeUrl("unix sockets are not public web endpoints")


class _ResponseStream(httpx.SyncByteStream):
    def __init__(self, stream) -> None:
        self._stream = stream

    def __iter__(self):
        yield from self._stream

    def close(self) -> None:
        self._stream.close()


class _PublicTransport(httpx.BaseTransport):
    """HTTPX transport whose TCP layer can never resolve to a private address."""

    def __init__(self) -> None:
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=True, trust_env=True),
            max_connections=16,
            max_keepalive_connections=8,
            http1=True,
            http2=False,
            network_backend=_PinnedBackend(),
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.read(),
            extensions=request.extensions,
        )
        resp = self._pool.handle_request(req)
        return httpx.Response(
            status_code=resp.status,
            headers=resp.headers,
            stream=_ResponseStream(resp.stream),
            extensions=resp.extensions,
        )

    def close(self) -> None:
        self._pool.close()


_public: httpx.Client | None = None
_public_lock = threading.Lock()


def _public_client() -> httpx.Client:
    global _public
    with _public_lock:
        if _public is None:
            _public = httpx.Client(
                transport=_PublicTransport(),
                timeout=40.0,
                follow_redirects=False,
                trust_env=False,
            )
            atexit.register(_public.close)
        return _public


def _httpx_client() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                timeout=40.0,
                follow_redirects=True,
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            )
        return _client


def _lane_http(url: str, headers: dict, timeout: float) -> tuple[int, str]:
    resp = _httpx_client().get(url, headers=headers, timeout=timeout)
    return resp.status_code, resp.text


def _lane_browser(url: str, headers: dict, timeout: float) -> tuple[int, str]:
    # Imported here, not at module scope: only the fingerprinted sources need it, and a
    # missing optional dependency must not break the adapters that do not.
    from curl_cffi import requests as curl_requests

    resp = curl_requests.get(url, headers=headers, impersonate=BROWSER, timeout=timeout)
    return resp.status_code, resp.text


LANES: dict[str, Callable[[str, dict, float], tuple[int, str]]] = {
    "http": _lane_http,
    "browser": _lane_browser,
}


_REDIRECTS = frozenset((301, 302, 303, 307, 308))
_MAX_REDIRECTS = 5


@dataclass(frozen=True)
class PublicResponse:
    status: int
    headers: dict[str, str]
    content: bytes
    text: str
    url: str


class PublicSession:
    """A cookie-isolated public client used by plain fetches and browser request routing."""

    def __init__(self, *, timeout: float = 40.0, client: httpx.Client | None = None) -> None:
        self.timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.Client(
            transport=_PublicTransport(),
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )
        self._state = threading.Condition()
        self._active = 0
        self._close_requested = False
        self._closed = False

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        content: bytes | None = None,
        timeout: float | None = None,
    ) -> PublicResponse:
        with self._state:
            if self._close_requested:
                raise SourceError("public session is closed")
            self._active += 1
        try:
            current = url
            current_method = method.upper()
            current_body = content
            seen: set[str] = set()
            for _ in range(_MAX_REDIRECTS + 1):
                current = require_public_url(current)
                if current in seen:
                    raise UnsafeUrl("redirect loop")
                seen.add(current)
                pacer(current).wait()
                reply = self._client.request(
                    current_method,
                    current,
                    headers=headers or UA_HTML,
                    content=current_body,
                    timeout=timeout if timeout is not None else self.timeout,
                )
                if reply.status_code not in _REDIRECTS:
                    return PublicResponse(
                        status=reply.status_code,
                        headers={key: value for key, value in reply.headers.items()},
                        content=reply.content,
                        text=reply.text,
                        url=str(reply.url),
                    )
                location = reply.headers.get("location")
                if not location:
                    raise SourceError("HTTP %s without Location" % reply.status_code)
                current = urljoin(current, location)
                if reply.status_code == 303 or (
                        reply.status_code in (301, 302) and current_method == "POST"):
                    current_method = "GET"
                    current_body = None
            raise UnsafeUrl("too many redirects")
        finally:
            with self._state:
                self._active -= 1
                self._state.notify_all()
                close = self._owns_client and self._close_requested and self._active == 0 \
                    and not self._closed
                if close:
                    self._closed = True
            if close:
                self._client.close()

    def close(self, *, wait: float = 0.0) -> None:
        with self._state:
            self._close_requested = True
            if self._active and wait:
                self._state.wait_for(lambda: self._active == 0, timeout=wait)
            close = self._owns_client and self._active == 0 and not self._closed
            if close:
                self._closed = True
        if close:
            self._client.close()


def _public_request(
    method: str,
    url: str,
    headers: dict,
    *,
    timeout: float,
    tries: int,
    sleep: float,
) -> tuple[int, str]:
    """One public request with a manual redirect loop and a per-hop boundary check."""
    last = "no attempt"
    started = time.monotonic()
    original_host = urlsplit(url).netloc or "?"
    for attempt in range(max(1, tries)):
        session = PublicSession(client=_public_client(), timeout=timeout)
        try:
            reply = session.request(method, url, headers=headers, timeout=timeout)
            if method == "HEAD" and reply.status < 400:
                _tally(original_host, "ok", time.monotonic() - started)
                return reply.status, ""
            if reply.status == 200:
                _tally(original_host, "ok", time.monotonic() - started)
                return reply.status, reply.text
            last = "HTTP %s" % reply.status
        except UnsafeUrl as exc:
            _tally(original_host, "fail", time.monotonic() - started)
            raise SourceError("%s -> %s" % (url, exc)) from exc
        except Exception as exc:
            last = repr(exc)[:120]
        _tally(original_host, "retry")
        if attempt + 1 < tries:
            time.sleep(sleep)
    _tally(original_host, "fail", time.monotonic() - started)
    raise SourceError("%s -> %s" % (url, last))


def get(
    url: str,
    headers: dict | None = None,
    *,
    transport: str = "http",
    timeout: float = 30.0,
    tries: int = 3,
    sleep: float = 0.8,
    public: bool = False,
) -> str:
    """Fetch a URL as text, retrying transient failures. Raises SourceError at the end."""
    if public:
        if transport != "http":
            raise SourceError("public fetches use the checked HTTP transport only")
        return _public_request(
            "GET", url, headers or UA_HTML, timeout=timeout, tries=tries, sleep=sleep
        )[1]
    lane = LANES.get(transport)
    if lane is None:
        raise SourceError("unknown transport: %r" % (transport,))
    last = "no attempt"
    host = urlsplit(url).netloc or "?"
    started = time.monotonic()
    for attempt in range(max(1, tries)):
        try:
            pacer(url).wait()
            status, text = lane(url, headers or UA_JSON, timeout)
            if status == 200:
                _tally(host, "ok", time.monotonic() - started)
                return text
            last = "HTTP %s" % status
        except Exception as exc:  # network, TLS, timeout: all retryable
            last = repr(exc)[:120]
        _tally(host, "retry")
        if attempt + 1 < tries:
            time.sleep(sleep)
    _tally(host, "fail", time.monotonic() - started)
    raise SourceError("%s -> %s" % (url, last))


def head(
    url: str,
    headers: dict | None = None,
    *,
    timeout: float = 8.0,
    tries: int = 1,
    sleep: float = 0.8,
) -> int:
    """A public HEAD check, used only to prefer HTTPS for a displayed project link."""
    return _public_request(
        "HEAD", url, headers or UA_HTML, timeout=timeout, tries=tries, sleep=sleep
    )[0]


def json_get(
    url: str,
    headers: dict | None = None,
    *,
    transport: str = "http",
    timeout: float = 30.0,
    tries: int = 3,
    sleep: float = 0.8,
    public: bool = False,
) -> Any:
    """Fetch a URL and parse it. A body that is not JSON is a failure, not a result."""
    text = get(url, headers, transport=transport, timeout=timeout, tries=tries, sleep=sleep,
               public=public)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise SourceError("%s -> not JSON: %s" % (url, repr(exc)[:80])) from exc


def post(
    url: str,
    body: Any,
    headers: dict | None = None,
    *,
    timeout: float = 30.0,
    tries: int = 3,
    sleep: float = 0.8,
) -> str:
    """POST a JSON body and return the text, retrying transient failures.

    The same pacing, tallying and retry policy as `get`, because a POST is the same
    request as far as the site is concerned: `four.meme` answers its token list over POST
    and nothing about that makes a throttle, a timeout or a 5xx more final than it is on a
    GET. There is no browser lane here - every endpoint this sends to has answered the
    plain client.
    """
    last = "no attempt"
    host = urlsplit(url).netloc or "?"
    started = time.monotonic()
    for attempt in range(max(1, tries)):
        try:
            pacer(url).wait()
            response = _httpx_client().post(url, headers=headers or UA_JSON, json=body,
                                            timeout=timeout)
            if response.status_code == 200:
                _tally(host, "ok", time.monotonic() - started)
                return response.text
            last = "HTTP %s" % response.status_code
        except Exception as exc:  # network, TLS, timeout: all retryable
            last = repr(exc)[:120]
        _tally(host, "retry")
        if attempt + 1 < tries:
            time.sleep(sleep)
    _tally(host, "fail", time.monotonic() - started)
    raise SourceError("%s -> %s" % (url, last))


def json_post(
    url: str,
    body: Any,
    headers: dict | None = None,
    *,
    timeout: float = 30.0,
    tries: int = 3,
    sleep: float = 0.8,
) -> Any:
    """POST a JSON body and parse the answer. A body that is not JSON is a failure."""
    text = post(url, body, headers, timeout=timeout, tries=tries, sleep=sleep)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise SourceError("%s -> not JSON: %s" % (url, repr(exc)[:80])) from exc


def shutdown() -> None:
    """Close the process-wide public client. The app lifespan and tests both call it."""
    global _public
    with _public_lock:
        client, _public = _public, None
    if client is not None:
        client.close()


_cache: dict[str, dict] | None = None
_cache_lock = threading.Lock()


def _cache_path():
    return config.RAW_DIR / "cache.json"


def _load_cache() -> dict[str, dict]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_cache_path().read_text(encoding="utf-8"))
        except Exception:
            _cache = {}
    return _cache


#: A source's explicit "I have nothing", which is worth remembering. `None` from a
#: producer means "we did not get an answer" and must be retried; this means the source
#: answered and the answer was empty. The difference is the whole cost of a round: an
#: uncached empty answer is re-asked on every round, three retries at a time, and on Arc
#: most tokens are absent from GeckoTerminal - so the un-cached miss, not the fetch, was
#: what a round spent its minutes on.
class _Negative:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "NEGATIVE"


NEGATIVE = _Negative()


def is_negative(value: Any) -> bool:
    """Did this source answer, and answer "nothing"?"""
    return value is NEGATIVE


def cached(key: str, ttl: float, produce: Callable[[], T]) -> T:
    """Remember a response for `ttl` seconds.

    `None` is never cached: a miss must be retried. `NEGATIVE` is: the source answered
    "I have nothing", and asking it again inside the same round only spends the same
    seconds to hear the same thing. Callers get `None` either way - what to remember is
    the producer's decision, not the caller's.
    """
    store = _load_cache()
    entry = store.get(key)
    if entry and (time.time() - float(entry.get("at") or 0)) < ttl:
        if entry.get("negative"):
            return None
        if entry.get("value") is not None:
            return entry.get("value")
    value = produce()
    negative = is_negative(value)
    if negative or value is not None:
        value = None if negative else value
        with _cache_lock:
            now = time.time()
            for stale in [k for k, e in store.items() if now - float(e.get("at") or 0) > 48 * 3600]:
                store.pop(stale, None)
            store[key] = {"at": now, "value": value, "negative": negative}
            try:
                _cache_path().parent.mkdir(parents=True, exist_ok=True)
                _cache_path().write_text(
                    json.dumps(store, ensure_ascii=False), encoding="utf-8", newline="\n"
                )
            except Exception:
                pass
    return value


def archive(run_id: str, name: str, payload: Any) -> None:
    """Keep the raw response a round ran on, so a surprising number can be re-read later."""
    path = config.RAW_DIR / run_id / ("%s.json" % name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n")
    except Exception:
        pass


def pmap(fn: Callable[[T], Any], items: Iterable[T], workers: int = 4,
         stop: Callable[[], None] | None = None) -> list[Any]:
    """Run `fn` over `items`, keeping order. A failure becomes None, never an exception.

    Pacing already serialises each host, so concurrency here buys latency across hosts
    without multiplying load on any one of them.

    `stop` is a round's own stop check (`cancel.check(run_id)`), asked on this thread before
    every task starts: a round that has been asked to stop starts no further work, and what
    the check raises is what ends the pass. It is asked out here rather than inside `fn`
    because a task that raised it would be read as a source that answered nothing - the one
    reading a stop must never be mistaken for. The tasks already in flight are left to
    finish, which is why a stop costs seconds and not a fraction of one, and the slots never
    started keep their None.

    The pool is topped up as tasks finish instead of being handed its whole queue at once,
    for the same reason: a queue submitted in one go has no "next task" left to stop at.
    Concurrency is unchanged - `workers` tasks run at a time either way.
    """
    queue = list(items)
    out: list[Any] = [None] * len(queue)
    if len(queue) <= 1:
        if stop is not None:
            stop()
        for index, item in enumerate(queue):
            out[index] = fn(item)
        return out
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending: dict[Any, int] = {}
        cursor = 0
        while cursor < len(queue) or pending:
            while cursor < len(queue) and len(pending) < workers:
                if stop is not None:
                    stop()
                pending[pool.submit(fn, queue[cursor])] = cursor
                cursor += 1
            if not pending:
                break
            finished, _ = wait(list(pending), return_when=FIRST_COMPLETED)
            for future in finished:
                position = pending.pop(future)
                try:
                    out[position] = future.result()
                except Exception:
                    out[position] = None
    return out
