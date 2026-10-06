"""One token, on its own: what a CA search runs.

A round starts from a chain and sweeps it; this starts from one address. Everything after the
sweep is the same work on one token - each source's per-token read merged field-wise, the
chain's own LP reading, the off-chain research pass, and the same `cards.card` the report page
draws - so a searched card and a published one cannot drift apart in shape.

Two things here are deliberately not a round's.

**Nothing is ranked.** 综合分 is a percentile inside the round's pool, and a pool of one has no
percentiles; a searched card carries no score rather than a number borrowed from a stale pool
or invented outright. The facts are the answer, and they are all still on the card.

**Nothing is stored as a round.** The card goes to the table the shelf already reads, with
`origin = 'search'` and `kept = 0`: the reader can open it again without paying for it twice,
it does not show up in any report library, and if they collect it the same row becomes the
shelf's (report/assemble.py `lookup_row`).

The chain comes from the address and not the other way round, because a CA does not say what
chain it is on and every other read about a token is per chain. Its holes are honest: a chain
this app has no config for has no node, no explorer and no source list, so the on-chain half of
its card comes back 未判定 while the market and off-chain halves still answer.
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable
from uuid import uuid4

from . import config
from .core.models import SafetyFacts, TokenMetrics
from .pipeline import collect, decide
from .report import assemble, cards as card_builder, lexicon, research
from .sources import dexscreener, http, registry
from .store import db

log = logging.getLogger(__name__)

#: The two shapes an address arrives in. Deliberately coarse and chain-agnostic: the chain is
#: not known yet, so the shape cannot be checked against it, and the real test is whether the
#: feed has ever seen the address (`identify`). This only keeps a symbol or a word out.
EVM_CA = re.compile(r"^0x[0-9a-f]{40}$")
BASE58_CA = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

#: The stages one CA search goes through, in order: a key and the label the panel prints.
#: The only place either is written. The runner says which key it is on, the page draws the
#: list from the job it is polling, and a stage that gets renamed is renamed once.
STAGES: tuple[tuple[str, str], ...] = (
    ("resolve", "认链"),
    ("market", "读链上与市场数据"),
    ("safety", "安全判定"),
    ("research", "链下调研与写作"),
    ("card", "写卡片并落库"),
)


class Refused(Exception):
    """The question cannot be answered at all: not an address, or an address nothing knows."""


def _address(ca: str) -> str:
    """The address as the rest of this module keeps it, or a refusal.

    Checked before anything is queued: a word or a symbol never becomes a job, because there
    is no work to watch - it is answered on the spot, by the endpoint that asked.
    """
    cleaned = str(ca or "").strip().lower()
    if not (EVM_CA.match(cleaned) or BASE58_CA.match(cleaned)):
        raise Refused("这不像一个合约地址：%s" % ca)
    return cleaned


def resolve(ca: str) -> tuple[str, dict | None]:
    """The chain an address is on, and this app's config for it when it has one.

    `None` is a real answer rather than a failure: a chain this app has no config file for
    still gets a card, just without the half that needs a node and an explorer.
    """
    found = dexscreener.identify(ca)
    if not found:
        raise Refused("DexScreener 没见过这个 CA：%s" % ca)
    chain_id = found[0][0]
    try:
        return chain_id, config.chain(chain_id)
    except KeyError:
        return chain_id, None


def read(chain: dict, ca: str) -> tuple[TokenMetrics, SafetyFacts]:
    """Every source this chain names, about one token, merged field-wise.

    A round's enrich pass with the pull taken out: each source is asked about the one CA at
    the same time, and the answers are merged per field in `collect.DEFAULT_PRIORITY` order -
    `dexscreener` leads the four market numbers, which is why it is here at all even though
    it has no sweep. There is no bulk row to overlay, because nothing swept this chain, so
    the merge is the whole reading.

    A source that raises has said nothing, not broken the search: the merge already reads a
    missing view as a gap.
    """
    sources = registry.for_chain(chain) + list(registry.ENRICH_ONLY)

    def ask(source):
        try:
            return source.name, source.fetch_token(str(chain["id"]), ca)
        except Exception as exc:
            log.info("lookup %s: %s said nothing (%r)", ca, source.name, exc)
            return source.name, None

    views: dict[str, TokenMetrics] = {}
    facts = SafetyFacts()
    for name, answer in http.pmap(ask, sources, workers=len(sources) or 1):
        if answer is None:
            continue
        views[name], safety = answer
        facts = decide.absorb(facts, safety)
    ordered = tuple(sorted(
        (name for name in views if views[name] is not None),
        key=lambda name: collect.DEFAULT_PRIORITY.index(name)
        if name in collect.DEFAULT_PRIORITY else len(collect.DEFAULT_PRIORITY),
    ))
    merged = collect.merge(ca, views, ordered)
    # The footer's trading links, off the same function a round uses: nothing else writes
    # them, and a search does not run the collect pass that stamps them.
    collect.stamp_trackers(merged, str(chain["id"]), ca)
    return merged, facts


def _why(exc: Exception) -> str:
    """A source's own words when it has them - its messages are written for a reader - and a
    repr when it does not."""
    text = str(exc).strip()
    return text[:200] if text else repr(exc)[:200]


def run(ca: str, tell: Callable[[str], None] | None = None) -> dict:
    """One CA, end to end. Returns the stored row, whose payload is the card.

    `tell` is called with a stage key from STAGES as each stage begins, and never with a
    sentence: the labels are written once, where the pages read them, and a caller that
    relays the key does not have to know what they say.
    """
    ca = _address(ca)
    say = tell or (lambda _stage: None)
    say("resolve")
    chain_id, chain = resolve(ca)
    if chain is None:
        chain = {"id": chain_id, "pipeline": {}}

    say("market")
    metrics, facts = read(chain, ca)
    say("safety")
    if chain.get("rpc"):
        locked, basis, _evidence = decide.lp_verdict(chain, metrics, facts)
        facts = facts.model_copy(update={"lp_locked": locked, "lp_note": basis})

    say("research")
    note = ""
    entry: dict | None = None
    # The links this search just paid for. Everything after them - the engine pass, the page
    # reads, the writer - is the same work a round does; what differs is where the links come
    # from. The snapshots table is where a round files what it learned, so a token no round has
    # enriched has no row in it, and reading the table there is how a card whose footer carried
    # an X handle came back saying it had no page to read.
    live = metrics.model_dump(mode="json")
    known = research.links_for(live)
    row = {
        "ca": ca,
        "chain_id": chain_id,
        "chain_name": config.chain_name(chain),
        "chain_thesis": config.chain_thesis(chain),
        "symbol": metrics.symbol,
        "run_id": "",
        "config_id": "",
    }
    try:
        # Finding the links is its own step here, because it is also where the proved site
        # comes from: the candidate that earned its tier is the address the card prints, and
        # putting it on the numbers before the writer is asked is what keeps the card and the
        # sheet the writer read the same reading.
        gathered = research.gather(row, known=known)
        collect.share_site(metrics, str(gathered.get("site") or ""))
        entry = research.one(
            row,
            gathered,
            metrics=metrics.model_dump(mode="json"),
            safety=facts.model_dump(mode="json"),
            store=False,
        )
    except Exception as exc:
        # A token with no readable link, or a model that answered nothing, is still a token
        # with market numbers and a chain reading. The card goes out with its off-chain half
        # empty and says which of the two happened, rather than refusing the whole answer.
        note = "链下调研没有结果：%s" % _why(exc)
        log.info("lookup %s: research unavailable: %r", ca, exc)

    scoring = (chain.get("pipeline") or {}).get("scoring") or {}
    say("card")
    card = card_builder.card(
        rank=0,
        metrics=metrics.model_dump(mode="json"),
        safety=facts.model_dump(mode="json"),
        written=lexicon.report_payload(entry, []),
        # Empty on purpose - see the module docstring: there is no pool to rank in, so there
        # is no 综合分, and the card shows the facts alone rather than a score nobody can
        # reproduce.
        breakdown={},
        is_new=False,
        onchain_weight=float(scoring.get("onchain_weight", 55)),
        offchain_weight=float(scoring.get("offchain_weight", 45)),
    )
    card["research_note"] = note
    return db.favorite_put(
        assemble.lookup_row(chain_id, ca, metrics.symbol, card, db.now_iso())
    )


def recent(limit: int = 50) -> list[dict]:
    """The cards past searches left behind, newest first - what the page lists."""
    return db.lookups(limit)


def forget(chain_id: str, ca: str) -> bool:
    """Drop one card off 最近搜索 - the reader's own record of looking that address up.

    Only the record. A row the reader also starred is a card on the shelf, and this cannot
    take it off (see db.lookup_forget); 搜索管理 greys that row out instead of letting the
    press be made to be told why.
    """
    return db.lookup_forget(chain_id, ca)


def forget_all() -> int:
    """Empty 最近搜索 of the records that are only records. Says how many went."""
    return db.lookups_clear()


#: The search worker. One thread, and its own rather than the round scheduler's: a search is
#: someone waiting on one token, and it must not queue behind a round with minutes still to
#: go. The two can still overlap, which is what the per-host pacers are for - they serialise
#: a host for the whole process, not per worker.
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lookup")


def _blank_stages() -> list[dict]:
    """The stage list as a job starts: every stage waiting, none of them timed."""
    return [{"key": key, "label": label, "state": "pending", "seconds": None}
            for key, label in STAGES]


def start(ca: str) -> dict:
    """Queue one CA search and answer with the job the page watches.

    The work is minutes of network and a model call, so the endpoint no longer holds a
    request open for it: a page has nowhere to show which stage a search is on when the
    answer to the request is the search itself. The card still lands in `favorites`
    (`report/assemble.py` `lookup_row`), so this row is never the only copy of anything.
    """
    job = {
        "id": uuid4().hex[:12],
        "ca": _address(ca),
        "chain_id": None,
        "status": "running",
        "stage": STAGES[0][0],
        "stages": _blank_stages(),
        "error": None,
        "row": None,
        "started_at": db.now_iso(),
        "finished_at": None,
    }
    db.job_put(job)
    submit(job)
    return job


def submit(job: dict) -> None:
    """Hand a job to the worker. Its own function so a test can drive one inline instead."""
    _worker.submit(drive, job)


def drive(job: dict) -> None:
    """Run a queued job to the end, writing each stage down as it ends.

    The job is the clock as well as the record: the runner holds it, the page polls it, and
    every transition is one write. A search that raises stops on the stage it died on and
    keeps the reason, which is what the panel prints - the same reading a failed round gives.
    """
    mark = time.monotonic()

    def say(key: str) -> None:
        """Move onto `key`: the stage that was running has ended, this one has begun."""
        nonlocal mark
        now = time.monotonic()
        for stage in job["stages"]:
            if stage["state"] == "run":
                stage["state"], stage["seconds"] = "done", round(now - mark, 1)
            if stage["key"] == key:
                stage["state"] = "run"
        mark = now
        job["stage"] = key
        db.job_put(job)

    try:
        row = run(job["ca"], say)
    except Exception as exc:
        for stage in job["stages"]:
            if stage["state"] == "run":
                stage["state"] = "failed"
        job.update(status="failed", error=_why(exc), stage=None, finished_at=db.now_iso())
        db.job_put(job)
        return
    now = time.monotonic()
    for stage in job["stages"]:
        if stage["state"] == "run":
            stage["state"], stage["seconds"] = "done", round(now - mark, 1)
    job.update(status="done", stage=None, chain_id=row["chain_id"],
               row=assemble.row_card(row), finished_at=db.now_iso())
    db.job_put(job)


def state(job_id: str) -> dict | None:
    """One job as the page polls it, or None when the id is not one this app made."""
    return db.job_get(job_id)


def latest() -> dict | None:
    """The newest search this app ran - what 运行结果 folds on a page whose own browser has
    not queued one yet (8.8.11)."""
    return db.job_latest()
