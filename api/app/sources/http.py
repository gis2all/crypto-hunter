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
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Callable, Iterable, TypeVar
from urllib.parse import urlsplit

import httpx

from .. import config

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
    # The board pages `dexscreener_rank` reads, not the API: a sweep asks for six of them
    # back to back, and they are the site's own HTML rather than a published feed.
    "dexscreener.com": 0.5,
    # The public Solana node, one call per token in the enrich pass. Its limit is not
    # published, so this is deliberately slower than the default: a hundred-token pass that
    # gets 429ed leaves every row "unread", which is worse than the seconds it saves.
    "api.mainnet-beta.solana.com": 0.5,
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
    the check raises is what ends the pass (CLAUDE.md 9.2.6). It is asked out here rather than inside `fn`
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
