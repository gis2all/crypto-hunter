"""The official-site lane: read websites instead of X, and write only what changed.

Why this exists (2026-10-28, user): X tells us what a project says, not what it ships. A new
testnet, a new vault, a new claim page often appears on the project's own site first - and the
watchlist no longer reads official timelines, so nothing else looks at the site at all. This lane
costs no X calls: it rotates over the sites the cards already know, keeps a snapshot of the
homepage plus up to two blog / news pages, and only when the text really moved does it write a
material.

Two rules keep it from being a crawler with an opinion:

- **The first read is not news.** It records the snapshot and writes nothing; otherwise turning
  the lane on would produce one card per project.
- **A repaint is not news.** A change under `SIMILAR` (0.98) is copy churn - counters, dates,
  rotating banners - and only refreshes the snapshot.

What it writes is an ordinary `opportunity_posts` row, so grouping, extraction and the card
pipeline need no second path. It is marked `trace_level = A` with the page itself as the official
source: for this material the site *is* the home, and the trace would only search for it.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import threading
from urllib.parse import urlsplit

from ..search.base import normalize_url
from ..search.identify import fetch_page
from ..store import db
from . import settings
from .trace import NOT_A_HOME, links_of, visible_text

log = logging.getLogger("crypto-hunter.opportunity")

#: Bumped when the rules below change, so a material can say which rules wrote it.
SITE_VERSION = "site@1"

#: Default rotation width; the settings page overrides it (`sites_per_scan`).
MAX_SITES = 10
#: Homepage plus at most this many blog / news pages. Three pages is enough to see a release
#: note without turning one site into a crawl.
MAX_EXTRA = 2
#: What one page contributes to a snapshot and to a material.
MAX_TEXT = 3000
#: Below this, a diff is copy churn (dates, counters, rotating banners), not news.
SIMILAR = 0.98
#: Three misses in a row and the site stops being read; a dead domain should not eat budget.
MAX_FAILURES = 3

_BLOG_HINTS = ("blog", "news", "announcement", "announcements", "update", "updates",
               "changelog", "release", "releases", "posts", "insights")
_SLUG = re.compile(r"[^a-z0-9]+")


def _clean(url: object) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return normalize_url(raw) or ""


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _channel(host: str) -> bool:
    return bool(host) and any(host == bad or host.endswith("." + bad) for bad in NOT_A_HOME)


def _root(url: str) -> str:
    parts = urlsplit(url)
    return "%s://%s/" % (parts.scheme or "https", parts.netloc.lower())


def _slug(host: str) -> str:
    return _SLUG.sub("-", host).strip("-")[:40] or "site"


def _trim(text: str, cap: int = MAX_TEXT) -> str:
    body = " ".join(str(text or "").split())
    return body[:cap]


def _page(url: str, fetch=None) -> tuple[str, list[str]]:
    """One page as (visible text, links). An empty document is the caller's failure to count."""
    reader = fetch or fetch_page
    raw = str(reader(url) or "")
    if not raw:
        return "", []
    return visible_text(raw), links_of(raw)


def _similar(before: str, after: str) -> float:
    if not before or not after:
        return 0.0
    return difflib.SequenceMatcher(None, before, after, autojunk=False).ratio()


def _blog_pages(host: str, links: list[str]) -> list[str]:
    """Same-host blog / news / changelog pages, in the order the site links them."""
    out: list[str] = []
    for url in links:
        low = str(url or "").lower()
        if not low or _host(low) != host:
            continue
        if not any(hint in low for hint in _BLOG_HINTS):
            continue
        if low.rstrip("/") == _root(low).rstrip("/"):
            continue
        if low in out:
            continue
        out.append(low)
        if len(out) >= MAX_EXTRA:
            break
    return out


def _snapshot(row: dict) -> dict:
    try:
        payload = json.loads(row.get("snapshot_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _moved(before: dict, pages: list[dict]) -> list[dict]:
    """The pages that really changed. The first read answers nothing, by construction."""
    if not before:
        return []
    known = {str(page.get("url") or ""): page for page in before.get("pages") or []}
    out: list[dict] = []
    for page in pages:
        old = known.get(page["url"])
        if old is None:
            # A page the site did not link last time: a new section or a new post index.
            out.append(page)
            continue
        if old.get("hash") == page["hash"]:
            continue
        if _similar(str(old.get("text") or ""), page["text"]) >= SIMILAR:
            continue
        out.append(page)
    return out


def bootstrap() -> int:
    """Register one site per host the cards already point at. Idempotent, so it runs per scan."""
    rows: list[dict] = []
    seen: set[str] = set()
    for card in db.opportunity_site_candidates():
        url = _clean(card.get("official_url"))
        host = _host(url)
        if not url or not host or _channel(host) or host in seen:
            continue
        seen.add(host)
        rows.append({"host": host, "url": _root(url), "slug": card.get("slug"),
                     "label": card.get("title"), "handle": card.get("official_account"),
                     "ecosystem": card.get("ecosystem"), "added_at": db.now_iso()})
    return db.opportunity_sites_put(rows)


def _write(scan_id: str, row: dict, page: dict) -> str:
    """One changed page as a material. Empty when the same text was already stored."""
    ref = "site-%s-%s" % (_slug(row["host"]), page["hash"])
    label = str(row.get("label") or row["host"])
    text = "官网更新 · %s\n\n%s" % (label, page["text"])
    stored = db.opportunity_post_put({
        "tweet_id": ref,
        "handle": str(row.get("handle") or row["host"]).lstrip("@"),
        "author_name": label,
        "text": text,
        "lang": None,
        "metrics": {},
        "score": None,
        "rules_json": json.dumps({"site": row["host"]}, ensure_ascii=False),
        "urls": [page["url"]],
        "posted_at": db.now_iso(),
        "url": page["url"],
        "source_target": "site:" + row["host"],
        "first_seen_scan": scan_id,
    })
    if not stored:
        return ""
    # 官网自己就是家：A 级、材料即官方页，溯源不必再为它搜一次。
    db.opportunity_trace_put_many([ref], "A", {
        "level": "A", "basis": "官网刷新：%s" % row["host"], "version": SITE_VERSION,
        "page_sources": [{"url": page["url"], "text": page["text"]}],
    })
    return ref


def refresh(row: dict, fetch=None) -> tuple[list[dict], dict]:
    """Read one site: (pages that moved, snapshot). Raises when the site does not answer."""
    home_text, links = _page(row["url"], fetch)
    if not home_text.strip():
        raise RuntimeError("官网没有返回正文")
    pages: list[dict] = []
    for url in [row["url"]] + _blog_pages(row["host"], links):
        text, _ = (home_text, links) if url == row["url"] else _page(url, fetch)
        body = _trim(text)
        if not body:
            continue
        pages.append({"url": url, "hash": hashlib.sha1(body.encode("utf-8")).hexdigest()[:16],
                      "text": body})
    if not pages:
        raise RuntimeError("官网没有返回正文")
    return _moved(_snapshot(row), pages), {"at": db.now_iso(), "pages": pages}


def run(scan_id: str, tell=None, cancel: threading.Event | None = None, fetch=None) -> dict:
    """Refresh up to `sites_per_scan` sites, least recently read first."""
    counters = {"sites": 0, "read": 0, "changed": 0, "unchanged": 0, "failed": 0,
                "disabled": 0, "posts": 0, "deferred": 0, "errors": []}
    bootstrap()
    budget = int(settings.value("sites_per_scan", MAX_SITES))
    if budget <= 0:
        return counters
    pending = db.opportunity_sites_due(1000)
    due = pending[:budget]
    counters["deferred"] = max(0, len(pending) - len(due))
    total = len(due) or 1
    for index, row in enumerate(due, start=1):
        if cancel is not None and cancel.is_set():
            raise RuntimeError("scan cancelled")
        counters["sites"] += 1
        try:
            moved, snapshot = refresh(row, fetch)
        except Exception as exc:  # noqa: BLE001 - one dead site is a count, not a crash
            counters["failed"] += 1
            if db.opportunity_site_failed(row["host"], MAX_FAILURES):
                counters["disabled"] += 1
            reason = " ".join(str(exc or "").split())[:200]
            if reason and reason not in counters["errors"] and len(counters["errors"]) < 3:
                counters["errors"].append(reason)
            log.info("opportunity: site %s did not answer: %r", row["host"], exc)
            if tell:
                tell(index, total)
            continue
        counters["read"] += 1
        if not moved:
            db.opportunity_site_saved(row["host"], snapshot, False)
            counters["unchanged"] += 1
        else:
            # One material per changed page; the homepage is the fallback when it is the one.
            posts = sum(1 for page in moved if _write(scan_id, row, page))
            db.opportunity_site_saved(row["host"], snapshot, True)
            counters["changed"] += 1
            counters["posts"] += posts
        if tell:
            tell(index, total)
    return counters
