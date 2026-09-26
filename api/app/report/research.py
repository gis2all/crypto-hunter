"""The heavy pass: after the publish, go and read the outside world.

The cheap draft writes an entry out of what the round already knows, which is market facts
and a safety block - nothing about what the thing actually is. That is why its prompt can
only say "do not invent", and why the entry ages: the facts move and the words do not.

So a second pass reads the project itself. It runs after the publish on purpose. Publishing
is a deadline; reading a website is not. The round reaches `sent` in seconds and this walks
it to `researched` in the background, which is what that state was declared for long before
anything wrote it.

Per token: up to `MAX_PAGES` outside pages (the site, X, GitHub, docs, TG - whatever the round
already learned, never a search engine), the text of those pages handed to the model, and the
entry rewritten from evidence. A page whose HTML comes back as an empty shell - a launchpad
front end is a title tag and a bundle - is fetched a second time through a reader that renders
it, because a page nobody could read is indistinguishable, to the model, from a project with
nothing to say. Same nine fields and same JSON contract as a draft, so an entry has one shape
whether it came from a draft or from research. The pages are kept in
`lexicon_sources` so the archive can point at what the words were read off.

The reads are side by side: one token's pages are four different hosts, and the tokens of a
round are independent of each other, so what a pass waits for is one page plus one model
call rather than the sum of all of them.

It never blocks a round and it never raises: a token it cannot research keeps the entry it
has, and its row says `failed` with the reason.
"""

from __future__ import annotations

import json
import logging
import re
import time
from html import unescape
from typing import Any, Mapping

from .. import llm
from ..sources import http
from ..sources.http import SourceError, UA_HTML
from ..store import db
from . import draft as draft_stage
from . import facts
from .schema import LexiconDraft, validate_lexicon

log = logging.getLogger("crypto-hunter.pipeline")

PROMPT_VERSION = "research.md@4"

#: How many outside pages one token gets, and how much of them the model sees. Both are caps
#: on a cost no round waits for; the four are the kinds a launch actually publishes.
MAX_PAGES = 4
MAX_CHARS = 8000
#: A page shorter than this after tags are stripped is a login wall or an empty shell, not
#: evidence. Handing it to the model only invites the model to fill the gap from memory.
MIN_CHARS = 80
#: A reader that renders the page before returning it, for the sites whose HTML is an empty
#: shell. It is a second opinion, not the default: it is slower and rate limited, and most of
#: what this pass reads is plain HTML that the plain fetch already returns.
READER_PREFIX = "https://r.jina.ai/"
#: The reader answers a browser User-Agent with 403 and a plain one with the rendered page -
#: the same kind of edge rule the GMGN adapter met on the TLS handshake, decided by the header
#: this time. Same UA the reference implementation fetched it with, not a disguise.
UA_READER = {"User-Agent": "curl/8.5.0"}
#: Socials worth fetching, in the order they are tried. Telegram is in the list because its
#: public preview (`t.me/<name>`) carries the channel title, the member count and the last
#: posts - the things the reader of a card wants before trusting a community.
READABLE_SOCIALS = ("twitter", "telegram", "github", "docs")
#: The states a research pass can leave behind, terminal ones included.
TERMINAL = ("done", "failed")
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
#: when the provider starts answering 429.
WORKERS = 3

_BLOCKS = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_MARKUP = re.compile(r"<[^>]+>")


def enabled(chain: Mapping[str, Any] | None) -> bool:
    """Whether this chain researches at all. On by default: it costs the round no time."""
    return bool(((chain or {}).get("report") or {}).get("research_enabled", True))


def queue(chain: Mapping[str, Any], result: Any) -> list[str]:
    """Line up the round picks for a research pass, and return what was lined up.

    Called from the round thread right after the publish, and it only writes rows: the
    reading happens on the research worker, so a website that takes thirty seconds cannot
    make a round take thirty seconds longer. An entry already `done` is left alone - it was
    read against the same facts, and re-reading the same pages for a token that did not move
    is the kind of work that makes a pipeline feel expensive.
    """
    if not enabled(chain):
        return []
    cas = [str(item.ca).lower() for item in (getattr(result, "picked", None) or [])]
    if not cas:
        return []
    marks = ",".join("?" * len(cas))
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE lexicon SET research_state = 'queued'"
            " WHERE ca IN (%s) AND COALESCE(research_state, 'none') <> 'done'" % marks,
            cas,
        )
    finally:
        conn.close()
    return cas


def claim(limit: int = 1) -> list[dict]:
    """Take up to `limit` queued entries out of the line, one claimant per row.

    The guarded UPDATE is the lock. Two workers - or a restart overlapping one - cannot
    walk away with the same entry, because the second UPDATE matches nothing and rowcount
    says so.
    """
    conn = db.connect()
    taken: list[dict] = []
    try:
        rows = conn.execute(
            "SELECT * FROM lexicon WHERE research_state = 'queued'"
            " ORDER BY updated_at LIMIT ?", (limit,)
        ).fetchall()
        for row in rows:
            cur = conn.execute(
                "UPDATE lexicon SET research_state = 'running'"
                " WHERE ca = ? AND research_state = 'queued'", (row["ca"],)
            )
            if cur.rowcount:
                taken.append(dict(row))
    finally:
        conn.close()
    return taken


def set_state(ca: str, state: str, note: str | None = None) -> None:
    """Move one entry along the research states, with the reason when there is one."""
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE lexicon SET research_state = ?, research_note = ? WHERE ca = ?",
            (state, (note or None) if note else None, ca),
        )
    finally:
        conn.close()


def requeue_running() -> int:
    """Put back whatever a killed process left at `running`. Returns how many.

    `running` is a claim held in a process memory, so it cannot survive the process. Left
    alone, those rows would sit there being neither queued nor finished, and the round they
    belong to would never reach `researched`.
    """
    conn = db.connect()
    try:
        cur = conn.execute(
            "UPDATE lexicon SET research_state = 'queued'"
            " WHERE research_state = 'running'"
        )
        return cur.rowcount or 0
    finally:
        conn.close()


def queued_count() -> int:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM lexicon WHERE research_state = 'queued'"
        ).fetchone()
    finally:
        conn.close()
    return int(row["n"] or 0)


def save_sources(chain_id: str, ca: str, pages: list[dict]) -> None:
    """Record which pages an entry was read from, so the archive can link them."""
    now = db.now_iso()
    conn = db.connect()
    try:
        for page in pages:
            conn.execute(
                "INSERT INTO lexicon_sources (ca, chain_id, kind, url, fetched_at, note)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(ca, url) DO UPDATE SET fetched_at = excluded.fetched_at"
                " WHERE excluded.fetched_at > lexicon_sources.fetched_at",
                (ca, chain_id, page["kind"], page["url"], now, page.get("note")),
            )
    finally:
        conn.close()


def sources_for(ca: str) -> list[dict]:
    """The pages behind one entry, newest first."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT kind, url, fetched_at FROM lexicon_sources WHERE ca = ?"
            " ORDER BY fetched_at DESC", (ca,)
        ).fetchall()
    finally:
        conn.close()
    return [
        {"kind": r["kind"], "label": KIND_LABELS.get(str(r["kind"]), str(r["kind"] or "来源")),
         "url": r["url"], "fetched_at": r["fetched_at"]}
        for r in rows
    ]


def sources_by_ca(chain_id: str) -> dict[str, list[dict]]:
    """Every evidence page this chain has filed, grouped by CA, newest first per token.

    One query for the whole table, because the archive page asks about a hundred entries at
    once and a lookup per entry would be a hundred visits to the same file.
    """
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT ca, kind, url FROM lexicon_sources WHERE chain_id = ?"
            " ORDER BY fetched_at DESC", (chain_id,)
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, list[dict]] = {}
    for row in rows:
        kind = str(row["kind"] or "")
        out.setdefault(str(row["ca"]).lower(), []).append(
            {"kind": kind, "label": KIND_LABELS.get(kind, kind or "来源"), "url": row["url"]})
    return out


def links_for(metrics: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The pages worth reading for one token, most direct first, capped at `MAX_PAGES`.

    Only what the round already learned. A search engine would be a second data source with
    its own rules and its own failures; this pass exists to read the project, not to hunt for
    it, and a wrong URL guessed from a symbol is worse than no page at all.
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
    return unique[:MAX_PAGES]


def _plain(html: str) -> str:
    """Enough HTML to text for a model to read: scripts out, tags out, whitespace flat."""
    text = _MARKUP.sub(" ", _BLOCKS.sub(" ", html))
    return " ".join(unescape(text).split())


def _read(kind: str, url: str, budget: int) -> dict | None:
    """One page, or None.

    The plain fetch first; when the text it returns is too short to be a page, the same URL
    goes through a reader that renders it. A launchpad front end is a title tag and a
    bundle, so the plain fetch of it comes back as thirty characters of shell - and a site
    with a working dApp behind it is exactly the site this pass exists to describe. One try
    each: this pass is behind a publish, not in front of one.
    """
    body = _fetch_text(url, UA_HTML)
    if len(body) < MIN_CHARS:
        rendered = _fetch_text(READER_PREFIX + url, UA_READER)
        if len(rendered) > len(body):
            body = rendered
    if len(body) < MIN_CHARS:
        log.info("research found nothing on %s", url)
        return None
    return {"kind": kind, "url": url, "text": body[:budget]}


def _fetch_text(url: str, headers: dict) -> str:
    """One URL as plain text. Empty when it cannot be read; never raises."""
    try:
        return _plain(http.get(url, headers, tries=1, sleep=1))
    except SourceError as exc:
        log.info("research could not read %s: %s", url, exc)
        return ""


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


def ask(ca: str, symbol: str | None, metrics: Mapping[str, Any], pages: list[dict],
        safety: Mapping[str, Any] | None = None) -> dict:
    """One model call: the pages in, the entry out, the same contract as a draft."""
    context = "\n\n".join(
        "### %s %s\n%s" % (page["kind"], page["url"], page["text"]) for page in pages
    )
    return llm.complete_json(
        [
            {"role": "system", "content": draft_stage.read_prompt("research.md")},
            {"role": "user",
             "content": context + "\n\n" + _facts_line(ca, symbol, metrics, safety)},
        ],
        purpose="research",
        prompt_version=PROMPT_VERSION,
    )


def one(row: Mapping[str, Any]) -> dict:
    """Read, ask, store - for one entry. Raises; `work_once` is what catches it."""
    started = time.monotonic()
    ca = str(row["ca"]).lower()
    chain_id = str(row["chain_id"])
    symbol = row.get("symbol")
    metrics, safety = _latest_metrics(ca)
    links = links_for(metrics)
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
    raw = ask(ca, symbol, metrics, pages, safety)
    model_s = time.monotonic() - started - read_s
    issues = validate_lexicon(raw, [ca])
    if issues:
        raise ValueError("research draft rejected: %s" % "; ".join(issues[:3]))
    draft = LexiconDraft.model_validate(raw)
    entry = {key.lower(): value for key, value in draft.entries.items()}[ca]
    payload = entry.model_dump()
    payload["symbol"] = symbol or None
    # The pages are stored before the text: evidence is worth keeping even when the write
    # that follows it is refused, and the card can then show what was read either way.
    save_sources(chain_id, ca, pages)
    written = draft_stage.store_entries(
        chain_id, {ca: payload}, metrics={ca: metrics}, safety={ca: safety},
        reasons={ca: "重档调研：读了 %d 个外链" % len(pages)},
    )
    log.info("research %s: %d/%d pages in %.1fs, model %.1fs, total %.1fs",
             ca, len(pages), len(links), read_s, model_s, time.monotonic() - started)
    if ca not in written:
        # A confirmed entry is a human decision, and this pass does not overturn it. The
        # evidence is still filed - that is the part a person can act on.
        set_state(ca, "done", note="人工已确认，本次调研只存证据，未改文字")
        return {"ca": ca, "chain_id": chain_id, "state": "done", "pages": len(pages),
                "text": "kept"}
    # The card that is on screen was copied out of the entry this pass just replaced. Its
    # round is still at `sent` - waiting for exactly this - so the words go back onto the
    # card, and the reader gets the researched version without the round being re-run.
    cards = draft_stage.restate_cards(chain_id, [ca])
    if cards:
        log.info("restated %d card(s) for %s from the researched entry", cards, ca)
    set_state(ca, "done")
    return {"ca": ca, "chain_id": chain_id, "state": "done", "pages": len(pages),
            "text": "rewritten"}


def work_once(limit: int = 2) -> list[dict]:
    """One pass over up to `limit` queued entries. Never raises.

    The caller owns a background thread that has to outlive bad pages, dead DNS and a model
    that answers with something that is not JSON: everything one token can get wrong is
    caught here and written on its own row, so the next token still gets its turn.
    """

    def one_card(row: Mapping[str, Any]) -> dict:
        """One card, and every way it can fail kept to itself. Raises nothing."""
        ca = str(row["ca"])
        try:
            return one(row)
        except Exception as exc:
            log.warning("research on %s failed: %r", ca, exc)
            set_state(ca, "failed", note=repr(exc)[:200])
            return {"ca": ca, "chain_id": str(row["chain_id"]), "state": "failed"}

    claimed = claim(limit)
    if not claimed:
        return []
    # The cards are researched side by side. Each one is a page read plus a model call, and
    # five of those in a row is five waits for the same thing; `one_card` is still the
    # boundary, so a card that fails writes its own row while the others carry on. A card
    # missing from the answer is one whose own handler died, which the caller may read as
    # "not settled" - the same thing a dropped tick means.
    return [row for row in http.pmap(one_card, claimed, workers=min(WORKERS, len(claimed)))
            if row]