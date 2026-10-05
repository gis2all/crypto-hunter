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

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterable, TypeVar
from urllib.parse import urlsplit

import httpx

from .. import config
from ..feed.lib import http_policy

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

#: 信息流那条车道用这个: 声明白己是谁, 不冒充浏览器 (AIHOT 的 `DEFAULT_UA`). 它抓的是媒体
#: 的订阅端点与正文页 —— 那些站点的 robots 与封禁规则按 UA 放行或封禁, 装成 Chrome 是我们替
#: 读者做的决定, 而不是一个技术细节. 雷达那边的 JSON API 不走这里: 那些站点把 TLS 指纹与 UA
#: 一起判, 换掉只会静默地少拿到数据. 某一个源确实要别的 UA, 在 sources.yaml 的 `headers` 里写.
UA_CRAWLER = {
    "User-Agent": "Mozilla/5.0 (compatible; CryptoHunterBot/1.0)",
    "Accept": "*/*",
}


def crawler_headers(given: dict | None = None) -> dict:
    """Feed 车道的请求头: 源自己写了 `headers` 就听它的, 否则申明我们是机器人."""
    return given or UA_CRAWLER


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
    "rpc.arc-scan.org": 0.15,
    "explorer.arc.io": 1.0,
    # Robinhood: its own node and explorer first, then the three launchpad feeds. The feeds
    # answer the whole chain in one or two pages, so the cadence only has to keep a second
    # round from stacking on the first.
    "rpc.mainnet.chain.robinhood.com": 0.15,
    "robinhoodchain.blockscout.com": 1.0,
    "coinbarrel.com": 0.5,
    "api.pez.family": 0.5,
    "peeps.wtf": 0.5,
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


def get(
    url: str,
    headers: dict | None = None,
    *,
    transport: str = "http",
    timeout: float = 30.0,
    tries: int = 3,
    sleep: float = 0.8,
) -> str:
    """Fetch a URL as text, retrying transient failures. Raises SourceError at the end."""
    lane = LANES.get(transport)
    if lane is None:
        raise SourceError("unknown transport: %r" % (transport,))
    # 出站守卫 (AIHOT 的 `lib/url.ts` + `contracts/http-policy.ts`): 一个来自材料的链接
    # 不该把我们带到本机、内网或云元数据地址上。归一过的地址继续往下走 —— 两种写法同一个页面
    # 不该被当成两条材料。
    try:
        url = http_policy.guard(url)
    except http_policy.RefusedURL as exc:
        raise SourceError("%s -> refused: %s" % (url, exc)) from exc
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


def json_get(
    url: str,
    headers: dict | None = None,
    *,
    transport: str = "http",
    timeout: float = 30.0,
    tries: int = 3,
    sleep: float = 0.8,
) -> Any:
    """Fetch a URL and parse it. A body that is not JSON is a failure, not a result."""
    text = get(url, headers, transport=transport, timeout=timeout, tries=tries, sleep=sleep)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise SourceError("%s -> not JSON: %s" % (url, repr(exc)[:80])) from exc


def _fetch_meta(url: str, headers: dict, timeout: float, transport: str) -> tuple[int, dict, str]:
    """The lanes, but keeping the response headers. Not `LANES`: those stay two-valued because
    the adapters that use them never read a header, and a third lane shape would be a contract
    change for every one of them."""
    if transport == "browser":
        from curl_cffi import requests as curl_requests

        resp = curl_requests.get(url, headers=headers, impersonate=BROWSER, timeout=timeout)
        return resp.status_code, dict(resp.headers), resp.text
    resp = _httpx_client().get(url, headers=headers, timeout=timeout)
    return resp.status_code, dict(resp.headers), resp.text


def get_conditional(
    url: str,
    headers: dict | None = None,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    transport: str = "http",
    timeout: float = 30.0,
    tries: int = 3,
    sleep: float = 0.8,
) -> tuple[int, dict, str]:
    """`get`, but conditional and header-aware: answers (status, headers, text).

    304 is a normal answer here rather than a failure. A feed that says "nothing changed" has
    done its job, and the caller's job is then to leave its cursor alone - which is why this
    returns the status instead of collapsing the two cases into one string.
    """
    try:
        url = http_policy.guard(url)
    except http_policy.RefusedURL as exc:
        raise SourceError("%s -> refused: %s" % (url, exc)) from exc
    sent = dict(headers or UA_JSON)
    if etag:
        sent["If-None-Match"] = etag
    if last_modified:
        sent["If-Modified-Since"] = last_modified
    last = "no attempt"
    host = urlsplit(url).netloc or "?"
    started = time.monotonic()
    for attempt in range(max(1, tries)):
        try:
            pacer(url).wait()
            status, heads, text = _fetch_meta(url, sent, timeout, transport)
            if status in (200, 304):
                _tally(host, "ok", time.monotonic() - started)
                return status, heads, text
            last = "HTTP %s" % status
        except Exception as exc:  # network, TLS, timeout: all retryable
            last = repr(exc)[:120]
        _tally(host, "retry")
        if attempt + 1 < tries:
            time.sleep(sleep)
    _tally(host, "fail", time.monotonic() - started)
    raise SourceError("%s -> %s" % (url, last))


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


def pmap(fn: Callable[[T], Any], items: Iterable[T], workers: int = 4) -> list[Any]:
    """Run `fn` over `items`, keeping order. A failure becomes None, never an exception.

    Pacing already serialises each host, so concurrency here buys latency across hosts
    without multiplying load on any one of them.
    """
    queue = list(items)
    if len(queue) <= 1:
        return [fn(item) for item in queue]
    out: list[Any] = [None] * len(queue)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, item): index for index, item in enumerate(queue)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                out[index] = future.result()
            except Exception:
                out[index] = None
    return out
