"""The one way in. Engines come and go behind it.

Callers ask a question and get leads back; which backend answered, and whether one of them
was down, is this layer's problem. That is what makes the engine layer replaceable - a
paid API added later is one more adapter, and nothing upstream learns about it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Sequence

from .base import Engine, SearchHit, TweetRow
from .engines.xsearch import XEngine

log = logging.getLogger("crypto-hunter.search")


@dataclass(frozen=True)
class SearchResult:
    """One question, every lead, and what went wrong while asking."""

    query: str
    hits: list[SearchHit] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: The posts the engine read on its way to the leads. They are not leads - a tweet is
    #: not a place a project lives - and they travel with the answer because they are what
    #: "who is talking about this" is counted from, and only this layer saw them.
    tweets: list[TweetRow] = field(default_factory=list)


class SearchService:
    """Ask the engines in order, and stop at the first that answers."""

    def __init__(self, engines: Sequence[Engine] | None = None):
        self.engines = list(engines) if engines is not None else [XEngine()]

    def search(self, query: str, *, limit: int = 20, only: str | None = None) -> SearchResult:
        """One query, deduplicated across the engines that were asked.

        The order is the point. Engines are not interchangeable - one is a subprocess and
        another may be a paid call - and the list is written cheapest first, so a later
        engine is reached only when every engine before it came back with nothing. A paid
        call made for a token a free engine already found is money spent to learn nothing.

        An engine that fails is recorded and the next one is still asked. A backend that
        answered nothing and a backend that refused are different facts about a token, and
        neither is allowed to look like the other.
        """
        hits: list[SearchHit] = []
        queries: list[str] = []
        errors: list[str] = []
        posts: list[TweetRow] = []
        seen: set[str] = set()
        seen_posts: set[str] = set()
        for engine in self.engines:
            if only and engine.name != only:
                continue
            try:
                answer = engine.search(query, limit=limit)
            except Exception as exc:
                errors.append('%s: %s' % (engine.name, str(exc)[:160]))
                continue
            for search in answer.queries:
                if search not in queries:
                    queries.append(search)
            for hit in answer.hits:
                if hit.url in seen:
                    continue
                seen.add(hit.url)
                hits.append(hit)
            for post in answer.tweets:
                if post.id and post.id in seen_posts:
                    continue
                seen_posts.add(post.id)
                posts.append(post)
            if hits:
                break
        return SearchResult(query=query, hits=hits[:limit], queries=queries, errors=errors,
                             tweets=posts)

    def timeline(self, handle: str, *, limit: int = 20) -> list[TweetRow]:
        """One account's own recent posts, from the first engine that can bring them.

        Not a search: the question is about a handle rather than a token, and only an engine
        that knows how to ask for an account's timeline can answer it. An engine that cannot
        is skipped rather than asked, and one that fails is not fatal - a round without a
        timeline is a round whose cards are missing the newest half of their community block,
        which is a worse card and not a broken one.
        """
        for engine in self.engines:
            ask = getattr(engine, 'timeline', None)
            if ask is None:
                continue
            try:
                return list(ask(handle, limit=limit) or [])
            except Exception as exc:
                log.info("timeline for %s unavailable: %r", handle, exc)
                return []
        return []
