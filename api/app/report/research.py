"""The reading pass: the outside world in, one entry per token out.

This is where an entry comes from, and the only place. It runs inside the round, before
the score, because the entry is 45% of that score as well as the words on the card - a
card written from one judgement beside a score computed from another is two claims about
one token, and the judge stage of `pipeline/decide.py` says so where the order is set.

None of it is carried over from an earlier round: the facts move and the words do not, so
every judged token is read again every round.

Per token: up to `MAX_PAGES` outside pages, the text of those pages handed to the model,
and the entry written from evidence. The pages are the round's own links plus the ones a
search confirms, and the two are combined rather than chosen between: the sources know
about tokens no search can find, a search finds what the sources never listed, and both
kinds of candidate earn their place by the same read. The search is asked for every token,
including the ones the sources already gave a handle to: a handle a source published is a
candidate, not a fact, and reading the account it names is what turns it into a tier - a
search is what finds the account the source got wrong. It costs a subprocess and no tokens.
A search that is unavailable costs the card its extra material and nothing else. A page
whose HTML comes back as an empty shell - a launchpad front end is a title tag and a
bundle - is fetched a second time through a reader that renders it, because a page nobody
could read is indistinguishable, to the model, from a project with nothing to say. The
pages are kept in `lexicon_sources` so the archive can point at what the words were read
off.

The reads are side by side: one token's pages are four different hosts, and the tokens of
a round are independent of each other, so what a round waits for is one page plus one model
call rather than the sum of all of them. The model calls are the half that costs money and
the half that has a ceiling, so that is the width `CH_RESEARCH_WORKERS` turns, and the
links are found at their own width before it.

It never raises for one token: a project that cannot be read or cannot be written about
leaves that token without an entry, which keeps it out of the ranking rather than filling
the gap with prose nobody decided. A rejected answer is not one of those cases yet: it
is asked for once more, with its own rejection quoted back, because a missing judgement
is a slip to correct rather than a token to drop. The round says so in its own note.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from html import unescape
from typing import Any, Callable, Mapping, Sequence

from .. import llm
from ..config import chain_name, chain_thesis
from ..core.models import ca_key
from ..pipeline import collect
from ..search.base import TweetRow
from ..search.discover import Discovery, discover
from ..search.identify import fetch_page
from ..sources import http
from ..sources.http import SourceError
from ..store import db
from . import facts
from . import lexicon
from .schema import LexiconDraft, validate_lexicon

log = logging.getLogger("crypto-hunter.pipeline")

#: The chain's own name, spelled in the prompt as this token. The prompt is one file for
#: every chain, so the name is substituted per call rather than written into it - an Arc
#: prompt asking a Robinhood token 「和 Arc 什么关系」 is the radar answering the wrong
#: question (user, 2026-09-30).
CHAIN_TOKEN = "__CHAIN__"

#: The chain's own thesis - one line saying what this chain is for - spelled in the prompt as
#: this token. `f_narrative` is judged against exactly that sentence, and nothing else, so
#: the line travels with the chain and not with the settings: two configs on one chain
#: screen the same tokens, and the same project must not score two ways because a different
#: set happened to be live.
THESIS_TOKEN = "__THESIS__"
#: What the prompt shows when a chain never registered a thesis. Leaving the blank would
#: leave the judgement with no target at all - saying so is at least a fact the model can
#: read, and the chain yaml is where the line gets written.
NO_THESIS = "（本链未登记主线）"

PROMPT_VERSION = "research.md@9"

#: What a rejected answer is told before it is asked again. Its own JSON goes back with it
#: along with the finding that rejected it, because the validator is the one thing here
#: that knows which field was missing - and the ask is to correct that answer, not to write
#: a new one, since a second pass over the same pages is how the same field goes missing
#: twice. Every rejection this model has produced was bookkeeping of that kind: one of the
#: five `f_*` judgements left out while the prose beside it was written.
REPAIR_HEAD = """上一次的回答没有通过校验：
%s

下面是你上一次的回答：
%s

按上面列出的问题修正它，重新输出完整的 JSON：结构与上一次完全一样，字段一个都不能少，
不要输出多余文本。"""

#: How many outside pages one token gets, and how much of them the model sees. Both are
#: caps on a cost no round waits for, and the eight are what the three ways in add up to:
#: the account and the site the search proved, the channels the project's own pages point
#: at, and the round's own links of the kinds a launch publishes.
MAX_PAGES = 8
#: The page half of the budget. A page that is too long is a document nobody trimmed, so
#: the pages are what gets cut - the eight share this between them. Together with
#: `POST_CHARS`, this is the 30000 characters a round may put in front of the model.
MAX_CHARS = 15000
#: The post half, and it is spent differently on purpose: a post that is cut is a claim
#: without its ending, so whole posts are taken until this runs out rather than each one
#: being trimmed.
POST_CHARS = 15000
#: A page shorter than this after tags are stripped is a login wall or an empty shell, not
#: evidence. Handing it to the model only invites the model to fill the gap from memory.
MIN_CHARS = 80
#: Socials worth fetching, in the order they are tried. Telegram is in the list because its
#: public preview (`t.me/<name>`) carries the channel title, the member count and the last
#: posts - the things the reader of a card wants before trusting a community.
READABLE_SOCIALS = ("twitter", "telegram", "github", "docs")
#: What a stored page kind reads as on screen. A kind this build does not know prints as
#: itself, which is how a new kind stays visible instead of turning into a blank.
KIND_LABELS = {"website": "官网", "twitter": "X", "github": "GitHub", "docs": "文档",
               "telegram": "TG"}

#: How many of one token's links are read at the same time. A card's links are four different
#: hosts, and `http.pmap` paces each host on its own, so the concurrency is across hosts and
#: never a burst at one of them.
LINK_WORKERS = 4
#: How many tokens one pass researches at the same time. The cards in a round are independent
#: of each other; the one shared resource is the model, so this is the knob to turn down first
#: when the provider starts answering 429. It gates the model calls only - the links and the
#: pages were already found under `SEARCH_WORKERS` - so raising it widens nothing else.
#:
#: Measured against this endpoint (2026-09-27, one card's worth of output per call):
#: four calls in flight took 36.7s wall clock and twelve took 33.6s for three times the
#: output - per-call latency does not grow with the width, so the phase costs one wave
#: instead of the sum of them. Twelve was clean, and twenty was clean on short answers
#: (20/20, no refusals, median 3.7s); the ceiling was never reached. Twelve is also the
#: width a round needs: it is `enrich_limit`, so the whole judged pool goes in one wave.
DEFAULT_WORKERS = 12


def _workers() -> int:
    """How many cards one pass researches at once. Read per pass, like the batch size."""
    raw = os.environ.get("CH_RESEARCH_WORKERS", "").strip()
    try:
        return max(1, int(raw or DEFAULT_WORKERS))
    except ValueError:
        return DEFAULT_WORKERS

#: How many tokens have their links found at the same time, which is a different width from
#: `DEFAULT_WORKERS` on purpose. This half is a search subprocess and a few page reads per token: it
#: costs no tokens and does not queue on the model, so the only limit is the search backend's
#: patience. Fifteen in flight was measured clean - twenty answered 429 on three of twenty
#: tokens - and it is also the faster setting, because a batch that ends early leaves workers
#: idle while the tail finishes.
SEARCH_WORKERS = 15

_BLOCKS = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_MARKUP = re.compile(r"<[^>]+>")



def save_sources(chain_id: str, ca: str, pages: list[dict],
                 config_id: str = "default") -> None:
    """Record which pages an entry was read from, so the archive can link them."""
    now = db.now_iso()
    conn = db.connect()
    try:
        for page in pages:
            conn.execute(
                "INSERT INTO lexicon_sources (ca, chain_id, config_id, kind, url,"
                " fetched_at, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(chain_id, config_id, ca, url) DO UPDATE SET"
                " fetched_at = excluded.fetched_at"
                " WHERE excluded.fetched_at > lexicon_sources.fetched_at",
                (ca, chain_id, config_id, page["kind"], page["url"], now,
                 page.get("note")),
            )
    finally:
        conn.close()


def sources_for(ca: str, chain_id: str | None = None,
                config_id: str | None = None) -> list[dict]:
    """The pages behind one entry, newest first.

    The pages belong to the entry and not to the token: a CA screened by two sets of
    settings on one chain has two cards, each read from its own round's pages. Calling
    this with no chain reads across all of them, which is what the lexicon screen wants.
    """
    conn = db.connect()
    try:
        if chain_id is None:
            rows = conn.execute(
                "SELECT kind, url, fetched_at FROM lexicon_sources WHERE ca = ?"
                " ORDER BY fetched_at DESC", (ca,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT kind, url, fetched_at FROM lexicon_sources"
                " WHERE chain_id = ? AND config_id = ? AND ca = ?"
                " ORDER BY fetched_at DESC",
                (chain_id, config_id or "default", ca)
            ).fetchall()
    finally:
        conn.close()
    return [
        {"kind": r["kind"], "label": KIND_LABELS.get(str(r["kind"]), str(r["kind"] or "来源")),
         "url": r["url"], "fetched_at": r["fetched_at"]}
        for r in rows
    ]


def links_for(metrics: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The pages the round already learned for one token, most direct first.

    Uncapped on purpose: these are the candidates the search is asked to confirm, and a list
    that has already been trimmed to `MAX_PAGES` cannot be added to. The cap belongs to
    `merge_links`, where the reading list is finally decided.
    """
    found: list[tuple[str, str]] = []
    for url in metrics.get("websites") or []:
        text = str(url or "").strip()
        if text:
            found.append(("website", text))
    socials = metrics.get("socials") or {}
    for kind in READABLE_SOCIALS:
        text = str(socials.get(kind) or "").strip()
        if text:
            found.append((kind, text))
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for kind, url in found:
        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append((kind, url))
    return unique


def search_links(chain_id: str, ca: str, symbol: str | None,
                 known: list[tuple[str, str]], *, chain_name: str = "",
                 search: bool = True) -> Discovery | None:
    """Read the round's links, and search for the ones the sources never listed.

    Never raises. This runs behind a publish, and a token whose search is unavailable is a
    token whose card keeps the links the round already had - not a card that failed. A
    provider that is down, or out of quota, costs the reader the extra material and nothing
    else.
    """
    try:
        return discover(chain_name or chain_id, ca, symbol or "", known=known, search=search)
    except Exception as exc:
        log.info("search for %s unavailable: %r", ca, exc)
        return None


def merge_links(found: Discovery | None,
                known: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The confirmed pages first, the harvested ones next, then the round's own.

    Confirmation supersedes but never deletes. A confirmed account replaces an unconfirmed
    one of the same kind, because two X accounts in one prompt is a question the writer
    cannot answer; a harvested channel replaces the one a data source listed for the same
    reason - it is the same channel read off the project's own page; and everything else the
    round already knew is kept, because those links came out of the token's own metadata
    and a search that found nothing is not a reason to read less than before.
    """
    verified = list(found.official_links) if found is not None else []
    harvested = list(found.harvested) if found is not None else []
    merged: list[tuple[str, str]] = []
    seen: set[str] = set()
    for kind, url in verified + harvested:
        if url.lower() in seen:
            continue
        seen.add(url.lower())
        merged.append((kind, url))
    replaced = {kind for kind, _ in merged}
    for kind, url in known:
        if kind in replaced or url.lower() in seen:
            continue
        seen.add(url.lower())
        merged.append((kind, url))
    return merged[:MAX_PAGES]


def _plain(html: str) -> str:
    """Enough HTML to text for a model to read: scripts out, tags out, whitespace flat."""
    text = _MARKUP.sub(" ", _BLOCKS.sub(" ", html))
    return " ".join(unescape(text).split())


def _read(kind: str, url: str, budget: int) -> dict | None:
    """One page as text, or None.

    The plain fetch, and the browser behind it for the sites that answer with a shell: the
    same reading the search layer judges a candidate site by (identify.fetch_page), so a
    page is not quoted to the model from one document and judged from another. Accounts are
    the exception, and identify.NO_RENDER is where that is written down: X serves a bio to a
    plain client and a login wall to a browser, and Telegram's preview page already has
    everything in it, so those two are read the cheap way and only that way.

    A page that is still too short to be a page after that is a login wall or an empty
    shell, and handing it to the model only invites the model to fill the gap from memory.
    One try: this pass is behind a publish, not in front of one.
    """
    try:
        body = _plain(fetch_page(url))
    except SourceError as exc:
        log.info("research could not read %s: %s", url, exc)
        return None
    if len(body) < MIN_CHARS:
        log.info("research found nothing on %s", url)
        return None
    return {"kind": kind, "url": url, "text": body[:budget]}


#: How many snapshots back a link is looked for. A source that goes quiet for a round - or a
#: token that drops out and comes back - must not blank a website an earlier round did learn,
#: and the alternative is a research pass that fails for "no links" on a token whose links we
#: have had in the table for days.
LINK_HISTORY = 10


def _latest_metrics(ca: str) -> tuple[dict, dict]:
    """The newest facts on file for this token, and the safety verdict beside them.

    Read here rather than carried on the row because the queue holds a CA, not a snapshot:
    the worker may run in another thread, or in the next process, after a restart.

    The verdict is returned with the numbers, not separately, because the two are one
    reading: this pass was writing "安全块三项全部未判定" onto cards whose snapshot held a
    verified contract, an empty permission list and a locked LP. The sheet the writer reads
    is only as wide as the columns this query pulls.
    """
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT metrics_json, safety_json, social_json FROM snapshots WHERE ca = ?"
            " ORDER BY created_at DESC LIMIT ?", (ca, LINK_HISTORY)
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return {}, {}
    out = json.loads(rows[0]["metrics_json"] or "{}")
    socials: dict[str, str] = {}
    websites: list[str] = []
    # Oldest first, so the newest row has the last word on a link that changed.
    for row in reversed(rows):
        social = json.loads(row["social_json"] or "{}")
        for kind, url in (social.get("socials") or {}).items():
            if url:
                socials[kind] = url
        for url in social.get("websites") or []:
            if url and url not in websites:
                websites.append(url)
    if socials:
        out["socials"] = socials
    if websites:
        out["websites"] = websites
    return out, json.loads(rows[0]["safety_json"] or "{}")


def _facts_line(ca: str, symbol: str | None, metrics: Mapping[str, Any],
               safety: Mapping[str, Any] | None = None) -> str:
    """The sheet the writer reads for a token the round already published. Wider than the
    eight fields this carried before: the reader of a page is asked to judge a product, and
    a product’s own links are only in the sheet if the sheet holds them.

    The safety verdict travels beside the numbers and not inside them, because it belongs to
    a different reading of the same token - and because the writer is asked to quote that row
    verbatim rather than describe the contract itself.
    """
    row = dict(metrics)
    row.setdefault("ca", ca)
    if symbol and not row.get("symbol"):
        row["symbol"] = symbol
    return "本轮事实：\n" + facts.block(row, safety)


#: How the two halves of the outside world are headed in the prompt. The pages carry their
#: own kind and URL; the posts are named for what they are, because a model that reads a
#: post as a page quotes it back as the project's own description.
POSTS_HEAD = "## 官方 X 时间线（最近 %d 条 · 正文完整 · 含时间与互动）"
TALK_HEAD = "## X 第三方讨论（搜索命中的陌生账号 · 只给统计 · 无正文）"


def post_block(rows: Sequence[TweetRow], budget: int) -> tuple[str, int]:
    """One account's posts as whole posts, newest first, and how many of them arrived.

    Whole ones and not trimmed ones. The engagement beside a post is most of what the round
    reads it for, and half a sentence with a number stuck to it is worse than the post left
    out - so the budget decides how many posts arrive rather than how long each may be, and
    what it dropped is logged instead of quietly shortened.
    """
    shown: list[str] = []
    used = 0
    for row in rows:
        entry = "%s ｜ 赞 %d ｜ 转 %d ｜ 评 %d ｜ 阅 %d\n%s" % (
            row.created_at or "—", row.likes, row.retweets, row.replies, row.views, row.text)
        if shown and used + len(entry) > budget:
            break
        shown.append(entry)
        used += len(entry)
    if len(shown) < len(rows):
        log.info("timeline: %d of %d posts fit the budget", len(shown), len(rows))
    return "\n\n".join(shown), len(shown)


def talk_block(rows: Sequence[TweetRow], official: Sequence[str]) -> str:
    """What strangers posted about the token, as counts rather than as text.

    The project's own posts are removed by handle, not by display name: a name is whatever
    the account felt like that week and the handle is what identifies it. What is left is the
    one thing the round owns about third-party discussion - how much of it there was, over
    how long, and how much of it landed - and it is counts because the prose is a stranger's
    and a stranger's prose is not evidence about the project.
    """
    mine = {handle.lower().lstrip("@") for handle in official if handle}
    others = [row for row in rows if row.handle.lower().lstrip("@") not in mine]
    if not others:
        return ""
    authors = {row.handle.lower() for row in others}
    likes = sorted(row.likes for row in others)
    when = sorted(row.created_at[:10] for row in others if row.created_at)
    return ("命中 %d 条 ｜ 作者 %d 个 ｜ 时间 %s ~ %s ｜ 合计赞 %d ｜ 中位赞 %d ｜ 最高赞 %d"
            % (len(others), len(authors), when[0] if when else "—",
               when[-1] if when else "—", sum(likes), likes[len(likes) // 2], likes[-1]))


def ask(ca: str, symbol: str | None, metrics: Mapping[str, Any], pages: list[dict],
        safety: Mapping[str, Any] | None = None, *, chain: str = "", thesis: str = "",
        timeline: Sequence[TweetRow] = (), talk: Sequence[TweetRow] = (),
        official: Sequence[str] = (), run_id: str = "") -> dict:
    """One model call: the pages and the posts in, the entry out, the same contract.

    A rejected answer buys exactly one more call, and only a rejected answer pays for it.
    The second ask carries the first answer and the findings together, so the model
    corrects a bookkeeping slip instead of writing the entry again from the pages. It is
    free to come back wrong a second time: `one` is still the place where a draft that
    fails twice ends the token, which is the same place a draft failed once before this
    existed.
    """
    blocks = ["\n\n".join(
        "### %s %s\n%s" % (page["kind"], page["url"], page["text"]) for page in pages
    )]
    posts, shown = post_block(timeline, POST_CHARS)
    if posts:
        blocks.append((POSTS_HEAD % shown) + "\n" + posts)
    discussion = talk_block(talk, official)
    if discussion:
        blocks.append(TALK_HEAD + "\n" + discussion)
    blocks.append(_facts_line(ca, symbol, metrics, safety))
    messages = [
        # The prompt is one file for every chain; the name and the thesis are substituted
        # here, so a Robinhood card is never asked about Arc or judged against no line.
        {"role": "system", "content": lexicon.read_prompt("research.md")
                                        .replace(CHAIN_TOKEN, chain or "本链")
                                        .replace(THESIS_TOKEN, thesis or NO_THESIS)},
        {"role": "user", "content": "\n\n".join(blocks)},
    ]
    raw = llm.complete_json(messages, purpose="research", prompt_version=PROMPT_VERSION,
                            run_id=run_id)
    issues = validate_lexicon(raw, [ca])
    if not issues:
        return raw
    log.info("research %s: rejected (%s), asking once more", ca, "; ".join(issues[:3]))
    answer = json.dumps(raw, ensure_ascii=False)
    return llm.complete_json(
        messages + [
            {"role": "assistant", "content": answer},
            {"role": "user", "content": REPAIR_HEAD % ("\n".join(issues[:8]), answer)},
        ],
        purpose="research_repair",
        prompt_version=PROMPT_VERSION + "+repair",
        run_id=run_id,
    )


def gather(row: Mapping[str, Any], *,
           known: Sequence[tuple[str, str]] | None = None) -> dict:
    """The links one token will be read from, found and merged. Never raises.

    Split out of `one` so it can run at its own width. Finding the links is a search and a
    handful of page reads; writing the card is a model call. The two have nothing to say to
    each other and one of them is thirty times more expensive, so they are not paced the
    same way - see `SEARCH_WORKERS`.

    `known` is the links the caller already has, and it replaces the ones the snapshots
    table would have handed in. A round leaves it out - collect has just written this round's
    snapshot, so the table holds the links it wants. A CA search hands in what the sources
    answered this time: a token no round has enriched has no row there, and reading the table
    is how a card whose footer carried an X handle came back saying it had no page to read.
    Only that starting list differs; the digging after it is the same work either way, the
    engine included - a search asks it exactly as a round does.

    A failure is carried rather than raised: the token still reaches `one`, and fails there
    with the same message it always did, on its own row.

    The answer carries the site the search proved, when it proved one. That is not another
    page to read - it is the one address a card prints, and this pass is the only one that
    knows which candidate earned it, so it is handed on rather than looked up again.
    """
    ca = ca_key(row["ca"])
    try:
        # The numbers are still the newest snapshot's when the caller handed in none of its
        # own; only the links are replaced. A search passes both, so this read answers nothing
        # there - it is kept because `gathered` is also the answer for a caller that did not.
        metrics, safety = _latest_metrics(ca)
        if known is None:
            known = links_for(metrics)
        known = list(known)
        found = search_links(str(row["chain_id"]), ca, row.get("symbol"), known,
                             chain_name=str(row.get("chain_name") or ""))
        # What the search layer decided is invisible from outside it, and a search that failed
        # and a search that found nothing are the two facts a card's author cannot tell apart.
        # Both are one line here, on the pass that paid for them.
        if found is not None and found.errors:
            log.info("search for %s: %s", ca, " | ".join(found.errors)[:200])
        merged = merge_links(found, known)
        site = found.proven_site if found is not None else ""
        log.info("links for %s: %d to read, handle %r, %d posts, %d harvested", ca,
                 len(merged), found.timeline_handle if found else "",
                 len(found.timeline) if found else 0, len(found.harvested) if found else 0)
        # The posts travel beside the links rather than inside them: a timeline is not a page
        # to read, it is the account the pages proved, asked what it has been saying.
        # The site the search proved travels the same way, for the same reason: it is not
        # another page to fetch, it is the one page whose address the card prints. Only the
        # top tier counts here - `website` is anything worth reading, and a page that
        # merely carries the address is not the project's own.
        return {"ca": ca, "links": merged, "metrics": metrics, "site": site,
                "safety": safety, "timeline": list(found.timeline) if found else [],
                "talk": list(found.tweets) if found else [],
                "official": found.mine if found else [], "error": ""}
    except Exception as exc:
        log.info("links for %s unavailable: %r", ca, exc)
        return {"ca": ca, "links": [], "metrics": {}, "site": "", "safety": {},
                "timeline": [], "talk": [], "official": [], "error": repr(exc)[:200]}


def one(row: Mapping[str, Any], gathered: Mapping[str, Any] | None = None, *,
        metrics: Mapping[str, Any] | None = None,
        safety: Mapping[str, Any] | None = None, store: bool = True,
        known: Sequence[tuple[str, str]] | None = None) -> dict:
    """Read, ask, write - for one entry. Raises; `research_entries` keeps it to itself.

    `gathered` is the earlier pass's answer for this token; without it this reads the links
    itself, which is what a caller driving one token wants.

    `metrics` / `safety` are the caller's own fresh reading, and they win over the ones
    `gather` pulls out of the snapshots table. A caller that has just paid for the token's
    numbers hands them in, so the writer describes the token the card is about instead of
    whatever an older round last recorded. `store` is for the same kind of caller: a CA
    search is not a round, so its reading must not overwrite the 词条 a round wrote for that
    CA under its own settings (`lexicon` is keyed by (chain, CA, settings)), and the card it
    keeps carries everything the reader needs anyway.

    `known` goes the other way - to `gather`, on a call that has to find the links itself.
    The same caller hands in the links it just read, so they are not looked up in a table it
    has no row in.
    """
    started = time.monotonic()
    ca = ca_key(row["ca"])
    symbol = row.get("symbol")
    if gathered is None:
        gathered = gather(row, known=known)
    if gathered.get("error"):
        raise SourceError(str(gathered["error"]))
    if metrics is None:
        metrics = gathered["metrics"]
    if safety is None:
        safety = gathered["safety"]
    links = gathered["links"]
    if not links:
        raise SourceError("没有可读的外链（官网 / X / GitHub 都没有）")
    budget = max(1200, MAX_CHARS // len(links))

    def read_link(link: tuple[str, str]) -> dict | None:
        return _read(link[0], link[1], budget)

    # The links go out together rather than one after another: they are different hosts, the
    # pacing in `sources/http.py` keeps each host serial on its own, and `pmap` keeps the
    # order - which is what both the prompt's page list and the stored evidence read.
    pages = [page for page in http.pmap(read_link, links, workers=LINK_WORKERS) if page]
    if not pages:
        raise SourceError("外链都打不开或读不到正文")
    read_s = time.monotonic() - started
    raw = ask(ca, symbol, metrics, pages, safety, chain=str(row.get("chain_name") or ""),
              thesis=str(row.get("chain_thesis") or ""),
              timeline=gathered.get("timeline") or (),
              talk=gathered.get("talk") or (),
              official=gathered.get("official") or (),
              run_id=str(row.get("run_id") or ""))
    model_s = time.monotonic() - started - read_s
    issues = validate_lexicon(raw, [ca])
    if issues:
        raise ValueError("research draft rejected: %s" % "; ".join(issues[:3]))
    draft = LexiconDraft.model_validate(raw)
    entry = {ca_key(key): value for key, value in draft.entries.items()}[ca_key(ca)]
    payload = lexicon.entry_payload(entry, symbol)
    # The pages are stored before the text: evidence is worth keeping even when the write
    # that follows it is refused, and the card can then show what was read either way.
    if store:
        chain_id = str(row["chain_id"])
        config_id = str(row.get("config_id") or "default")
        save_sources(chain_id, ca, pages, config_id=config_id)
        lexicon.store_entries(chain_id, str(row.get("run_id") or ""), {ca: payload},
                              config_id=config_id)
    log.info("research %s: %d/%d pages, %d posts, handle %s, model %.1fs, total %.1fs",
             ca, len(pages), len(links), len(gathered.get("timeline") or ()),
             gathered.get("official") or "-", model_s, time.monotonic() - started)
    return payload


def research_entries(chain: Mapping[str, Any], targets: list[dict], run_id: str,
                     notes: list[str] | None = None,
                     on_done: Callable[[float, float], None] | None = None) -> dict[str, dict]:
    """This round's entry for every token it has to judge. Never raises.

    `targets` is the judged pool - the tokens the gate cleared and the round is about to
    score. The answer is keyed by CA and holds only the tokens that produced an entry: a
    token whose pages would not load, or whose answer was not the contract, is absent, and an
    absent entry is a token with no score. That is where it belongs, rather than being ranked
    on numbers nobody produced while carrying prose nobody wrote.

    The two halves are paced apart on purpose. Finding the links is a search subprocess and a
    few page reads and costs no tokens, so it goes out at `SEARCH_WORKERS`. Writing the entry
    is a model call, so it goes out at `CH_RESEARCH_WORKERS`. Neither half is worth queuing
    behind the other: the first produces the second's input, and one token failing either
    half is not the next token failing it.
    """
    if not targets or not llm.configured():
        return {}
    config_id = str(chain.get("config_id") or "default")
    rows = [{"ca": ca_key(target["ca"]), "chain_id": str(chain["id"]),
             "chain_name": chain_name(chain),
             "chain_thesis": chain_thesis(chain),
             "config_id": config_id, "symbol": target.get("symbol"),
             "run_id": run_id} for target in targets]
    total = len(rows)
    done = 0

    def tell() -> None:
        nonlocal done
        done += 1
        if on_done:
            on_done(done, total)

    # The links first, and wider: this half is a subprocess and a few page reads, so throwing
    # it at the wall costs nothing and the model calls then have their whole input ready.
    gathered = {item["ca"]: item for item in
                http.pmap(gather, rows, workers=min(SEARCH_WORKERS, total))}

    failed: list[str] = []
    # The round's own rows, by CA, and this is where the two halves of one token meet: the
    # pool decide is holding, and the site a search just proved for it.
    targets_by_ca = {ca_key(target.get("ca")): target for target in targets}

    def entry_for(row: dict) -> tuple[str, dict] | None:
        """One token, and every way it can fail kept to itself. Raises nothing."""
        ca = row["ca"]
        try:
            payload = one(row, gathered.get(ca))
            # The site goes onto the round's own numbers, not onto a copy of them: the
            # snapshot a card is drawn from is written from the pool decide holds, so the
            # card's 官网 line is decided here or nowhere. A caller whose target carries no
            # metrics - a test's stub - is left alone.
            collect.share_site((targets_by_ca.get(ca) or {}).get("metrics"),
                               (gathered.get(ca) or {}).get("site"))
            return ca, payload
        except Exception as exc:
            reason = str(exc) or repr(exc)
            log.info("research on %s failed: %s", ca, reason)
            failed.append("%s（%s）" % (row.get("symbol") or ca[:10], reason[:60]))
            return None
        finally:
            tell()

    entries: dict[str, dict] = {}
    for result in http.pmap(entry_for, rows, workers=min(_workers(), total)):
        if result:
            ca, payload = result
            entries[ca] = payload
    if failed and notes is not None:
        notes.append("链下调研未完成：" + "、".join(failed[:6]))
    return entries
