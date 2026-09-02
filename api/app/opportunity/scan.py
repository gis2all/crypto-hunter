"""Gathering material: what a scan reads, and the free gate it passes first.

One reader, one output. **X only** (user, 2026-10-18: the module read platform, exchange, catalog
and GitHub sources as well until then; the readers and their table are gone, and the verified
candidate lists are parked in the seed's `pending` section). What is left is the source that can
actually find things:

- an `official` row is one account's own timeline;
- a `query` row is one X search, asked in time slices (`xread.plan_slices`) so that forty posts
  per request is a slice length rather than a ceiling on what the query can find.

Both end up as **material** - a row in `opportunity_posts` - and nothing here decides whether
material is worth anything: that is the classification pass's job, one stage later.

The gate between them is free and that is why it exists. A scan reads a few hundred posts and
asks a model about a fraction of them; sending every post to be told it is an opinion is the most
expensive way to learn nothing. The gate is a rule set now (`rules.py`) rather than a word list:
it scores, it can refuse, and it records why - which is also what decides who is asked first when
more material passes than the extraction pass has budget for.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from ..search.engines.xsearch import XEngine
from ..store import db
from . import rules, settings, xread

log = logging.getLogger("crypto-hunter.opportunity")

#: Seconds between two X calls. Re-exported from the reader so the module has one number for it
#: (the tests patch `scan._throttle`, and a scan that ran wide open would be the one that gets the
#: session throttled or the throwaway account banned).
X_INTERVAL = xread.X_INTERVAL

#: How many posts one timeline read asks for. A query's own ceiling is `xread.QUERY_MAX`.
POSTS_PER_SOURCE = 20

#: How many watchlist rows one scan reads. Not a limit on what is watched - a limit on how much
#: of it is read per scan, because a few dozen X reads at the throttle above is minutes of
#: sleeping and a rate limit waiting to happen. `opportunity_sources.last_read_at` orders the
#: list, so the rows this scan did not reach are the ones the next scan starts with; how many
#: were deferred is written on the scan row, so a rotation is never mistaken for coverage.
MAX_SOURCES = 40

#: How many leads one scan traces to their official home. A trace costs about a minute (a
#: second search, up to three profile reads, up to two site reads), so eight of them is the
#: difference between a fifteen-minute scan and a two-hour one. Deduped by project: a dozen posts
#: about the same thing are one trace. The rest wait for the next scan, and say so in the pool.
MAX_TRACES = 8

#: ...and how many X **calls** one scan may spend, because a query row is not one call. Thirty
#: query rows at six slices each is 180 calls - fifteen minutes of throttle and a rate limit for
#: certain - while the same thirty rows as timelines would be thirty. The budget is calls, so the
#: batch stops when it is spent and the rest of the rotation waits for the next scan.
MAX_CALLS = 40

_LOCK = threading.Lock()


def _throttle() -> None:
    """Serialise X calls. The reader owns the interval; this is the seam the tests patch."""
    xread.throttle()


def timeline(handle: str, limit: int = POSTS_PER_SOURCE) -> list:
    """One account's own posts, newest first. Raises whatever the CLI raises."""
    _throttle()
    return XEngine().timeline(handle.lstrip("@"), limit=limit)


def search_sliced(query: str, **kwargs) -> dict:
    """One query over its whole window, as rows plus what the reading cost."""
    return xread.search_sliced(query, **kwargs)


def _stamp(value: object) -> str | None:
    """One source's own timestamp, as this database writes time.

    The CLI answers ISO text for posts; anything that will not parse is left as the string it
    was, because a scan that dropped an unreadable date would also drop the post's ordering.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat(timespec="seconds")
    except ValueError:
        return text


def look_up(name: str) -> list:
    """One name, one slice, as the rows a trace reads authors off.

    The second search of the trace step: a post that names no account and links to nothing is
    asked about by name, and the candidate accounts are the authors of what comes back. One slice,
    because the question is who is talking about this name now - not the history of the name.
    """
    answer = search_sliced(name, window_hours=24, slice_hours=24)
    return list(answer.get("rows") or [])


def post_row(tweet, source_target: str, scan_id: str, verdict: dict | None = None) -> dict:
    """One `TweetRow` as the posts table takes it, with the gate's own reading attached.

    The score and the hit rules are stored on the material rather than only used to decide
    whether to ask about it: "why did this one get through" is a question the page will ask, and
    re-running the rule set later would answer it with whatever the rules say *then*.
    """
    handle = str(getattr(tweet, "handle", "") or "").lstrip("@")
    return {
        "tweet_id": str(getattr(tweet, "id", "") or ""),
        "handle": handle,
        "author_name": getattr(tweet, "name", None),
        "text": str(getattr(tweet, "text", "") or ""),
        "lang": None,
        "metrics": {
            "likes": getattr(tweet, "likes", 0),
            "retweets": getattr(tweet, "retweets", 0),
            "replies": getattr(tweet, "replies", 0),
            "views": getattr(tweet, "views", 0),
        },
        "posted_at": _stamp(getattr(tweet, "created_at", None)),
        "url": "https://x.com/%s/status/%s" % (handle, getattr(tweet, "id", "")) if handle else None,
        "source_target": source_target,
        "first_seen_scan": scan_id,
        "urls": list(getattr(tweet, "urls", ()) or ()),
        "score": None if verdict is None else verdict.get("score"),
        "rules_json": None if verdict is None else _rules_json(verdict),
    }


def _rules_json(verdict: dict) -> str:
    import json
    return json.dumps({"level": verdict.get("level"), "excluded": verdict.get("excluded"),
                       "hits": verdict.get("hits") or []},
                      ensure_ascii=False)


def collect(scan_id: str, sources: list[dict], tell=None,
            cancel: threading.Event | None = None) -> dict:
    """Read up to `MAX_SOURCES` of the enabled sources once. A source that fails is a count,
    not an exception.

    Same rule as the screening pipeline's pull: one dead feed must not empty the scan, it must
    show up as a gap. The counters are what the page prints, and `failed` names the sources that
    did not answer rather than hiding them in a log line.

    `queries` counts X calls rather than sources, because a query row is several calls (one per
    slice) and the budget this lane spends is calls, not rows.
    """
    # 两个上限都读设定（`settings.py`）：常量只是默认值，而设定文件不存在时它什么都不改。
    budget = settings.value("calls_per_scan", MAX_CALLS)
    room = settings.value("sources_per_scan", MAX_SOURCES)
    counters = {"sources": 0, "posts": 0, "queries": 0, "slices": 0, "gated": 0, "skipped": 0,
                "excluded": 0, "failed": [], "deferred": max(0, len(sources) - room),
                # A slice that failed used to disappear into `search_sliced`'s own return value:
                # a run where X never answered looked exactly like a quiet day. These three make
                # "nothing was found" and "nothing was asked" different facts on the scan row.
                "calls_ok": 0, "calls_failed": 0, "errors": [],
                }
    # The accounts this watchlist already knows, injected into the gate: a post from one of them
    # is normal, a post from a stranger that announces something is a lead (rules.py).
    known = {str(row.get("target") or "").lstrip("@").lower()
             for row in sources if row.get("kind") == "official"}
    batch = sources[:room]
    total = len(batch) or 1
    for index, source in enumerate(batch, start=1):
        target, kind = source["target"], source["kind"]
        if counters["queries"] >= budget:
            # Out of budget: this row and everything behind it stay at the head of the rotation
            # for the next scan. Counted rather than silently skipped.
            counters["deferred"] += len(batch) - index + 1
            break
        counters["sources"] += 1
        try:
            if kind == "official":
                counters["queries"] += 1
                tweets = timeline(target)
                counters["calls_ok"] += 1
            else:
                query, window, step, limit = xread.query_row(source)
                answer = search_sliced(query, window_hours=window, slice_hours=step, limit=limit,
                                       cancel=cancel)
                slices = int(answer.get("slices") or 0)
                bad = list(answer.get("failed") or [])
                counters["queries"] += slices
                counters["slices"] += slices
                counters["calls_ok"] += max(0, slices - len(bad))
                counters["calls_failed"] += len(bad)
                for reason in bad:
                    _remember_error(counters, reason)
                tweets = answer["rows"]
            for tweet in tweets:
                row = post_row(tweet, target, scan_id)
                if not row["tweet_id"] or not row["text"].strip():
                    continue
                verdict = rules.judge(row["text"], author=row["handle"],
                                      metrics=row["metrics"], created_at=row["posted_at"],
                                      known=known, family=source.get("family"),
                                      source_official=(kind == "official"))
                row = post_row(tweet, target, scan_id, verdict)
                if not db.opportunity_post_put(row):
                    continue                      # already read in an earlier scan
                counters["posts"] += 1
                if verdict["pass"]:
                    counters["gated"] += 1
                else:
                    # Recorded, not deleted: "we read it and did not ask" is a different fact
                    # from "we never saw it", and only one of them is a funnel.
                    db.opportunity_mark(row["tweet_id"], "post", classified="skipped")
                    counters["skipped"] += 1
                    if verdict["excluded"]:
                        counters["excluded"] += 1
        except Exception as exc:
            if cancel is not None and cancel.is_set():
                raise
            counters["failed"].append(target)
            counters["calls_failed"] += 1
            _remember_error(counters, exc)
            log.info("opportunity: %s (%s) did not answer: %r", target, kind, exc)
        finally:
            # Touched whether it answered or not: a source that failed is exactly the one the
            # next scan should try again, and leaving it at the head of the queue is how that
            # happens.
            db.opportunity_source_touched(target)
        if tell:
            tell(index, total)
    if counters["calls_ok"] == 0 and counters["calls_failed"]:
        # Every call failed. Returning zero posts here would report an empty round, which reads
        # as "the filters found nothing" - the opposite of what happened.
        raise RuntimeError("X 没有回答：%d 次调用全部失败（%s）" % (
            counters["calls_failed"],
            counters["errors"][0] if counters["errors"] else "原因未记录"))
    return counters


def _remember_error(counters: dict, reason) -> None:
    """Keep the first few distinct failure reasons, trimmed to something a row can hold."""
    text = " ".join(str(reason or "").split())[:200]
    errors = counters["errors"]
    if text and text not in errors and len(errors) < 3:
        errors.append(text)
