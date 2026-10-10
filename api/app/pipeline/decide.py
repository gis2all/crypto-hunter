"""Steps 2-6 of a round: prefilter, enrich, gate, score, select.

The rules live in `core/` and the network lives in `sources/`; this module is the
orchestration that walks one past the other, and it is the only place that knows the
order. It is a class rather than a function so a test can hand it fake sources and a
fake chain reader and still exercise the exact path a live round takes - the order is
the thing worth testing, and the order must not be a special case for tests.

What the stages are, and why they are in this order:

1. **collect**    - one bulk pull per source, merged field-wise.
2. **prefilter**  - the pool rules the bulk pull alone can answer: the market bands and the
   two floors. Free, because no request is spent on them.
3. **top-up**     - the rows the bulk sweep left without a chip number (holders, top-10,
   bundling) are asked about by name, and then the chip rules run over the whole pool.
   未判定 never rejects, so a blank is only a blank once someone has been paid to look -
   the hard filter's second half runs after this, not before it.
4. **shortlist**  - the pool survivors by `liquidity_usd`, deepest first. The expensive
   per-token questions are spent here and nowhere else.
5. **enrich**     - per-token answers from every source, including the deployer.
6. **gate**       - the entrapment ceiling, the source check, LP lock, permissions.
7. **judge**      - the off-chain entry for every token this round shortlisted: five 0-5
   judgements and the prose beside them, written against this round's facts. It has to
   happen here rather than after the publish, because the entry is 45% of the score as
   well as the words on the card - a card written from a fresh entry beside a score
   computed from an older one would be two claims about one token. A token the model did
   not answer for has no entry and stays out of the ranking; nothing is carried over from
   an earlier round, so the two can never disagree.
8. **score**      - five on-chain percentiles inside this round, plus the off-chain block.
9. **select**     - the top `per_round` by total, and the in-pool ranks for the card.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

from .. import cancel
from ..core import filters, funnel, scoring
from ..core.models import SafetyFacts, ScoreBreakdown, TokenMetrics, ca_key
from ..sources import onchain, registry
from ..sources.base import SourceReport
from ..sources.http import pmap
from . import collect as collector
from . import progress

#: How many prefilter survivors are asked the expensive per-token questions per batch.
#: Twelve is the batch the reference implementation used.
DEFAULT_ENRICH_LIMIT = 12

#: How many batches a round will spend before settling for fewer; 0 means keep going until
#: the whole candidate pool has been judged. The reference loops while it holds under
#: `TOP_N * 2` qualified tokens and caps itself at three rounds - but its prefilter left it
#: thirteen candidates, so the cap never bound. Ours leaves a hundred-odd, and capping there
#: strands survivors that are neither 候选 nor 淘汰, which is exactly what the funnel's
#: columns are meant to account for. `pipeline.gate_rounds` puts a ceiling back if a round
#: ever needs one.
GATE_ROUNDS = 0

#: How many survivors the gate judges. Zero - the whole pool - is the rule.
#:
#: The cap used to be twenty, and that number was doing two unrelated jobs: limiting the
#: paid reads, and choosing who moved on. Sorting by liquidity *before* the paid read made
#: the second job wrong. The deepest rows a chain lists are disproportionately the rows the
#: chip and safety rules are about to reject (Robinhood 2026-09-30: the top twenty by depth
#: were consumed by seventeen LP-unlocked / entrapment / drawdown rejects), so a healthy
#: token below them was never asked about at all - 未判定, on the one question the round
#: exists to ask. A filter that runs on a sample of the pool is not a filter, so it runs on
#: all of it, and the cut moves to the off-chain research that follows (RESEARCH_LIMIT) -
#: the pass that actually costs money. The knob stays: 0 means the whole pool.
GATE_LIMIT = 0

#: How many candidates are asked about at once. Every host paces itself, so this number
#: changes no host's request rate - it only decides how much of the round is spent idle
#: waiting for each candidate's slowest host. The chain's node was the one left idle: it
#: answers a call in about 2.2s whenever it arrives and eight at once in 2.9s (measured),
#: so four workers were spending the gate waiting rather than reading.
ENRICH_WORKERS = 8

#: The boundaries a round crosses, in order, reported through `on_stage`. The engine keeps
#: its own vocabulary so it never has to know that a run has a state machine.
STAGES = ("collected", "scored", "selected")

#: The per-token source the gate does not wait for, asked about the picks instead.
#:
#: GeckoTerminal is the only source that can answer "how many pools, where has the price
#: been" - and it charges three requests per token on one paced host, so asking it about
#: all twenty gate candidates serialises the gate behind it. Neither question changes the
#: gate's verdict (entrapment ceiling, source check, LP lock, permissions) or the score
#: (liquidity, volume, holders, top-10 concentration, bundling): what it fills are card
#: fields. So it is asked once - after the picks are known, about the picks alone.
DEFERRED_ENRICH: tuple[str, ...] = ("geckoterminal",)

#: The per-token sources the top-up pass asks about a row whose chip picture is missing.
#:
#: `gmgn` is the chip source: the holder count, the top-10 share and the bundling rate are
#: per-token questions the scan sources do not all answer, and a row with no chip number is
#: exactly the row the hard filter has to be able to see - the tokens a launchpad reports
#: had four holders and 72% in ten wallets, and every one of them sat in the pool as a
#: blank, which is a blank the rules read as 未判定 and 未判定 never rejects.
#: `dexscreener` rides along for its own reason: it is the round's market caliber, and the
#: depth some launchpads report through their own API is inflated (5.52x on every Tolly row,
#: with the market cap and the volume identical), so the depth the sort and the liquidity
#: floor are about to read is re-read from the source of record before either of them run.
MARKET_ENRICH: tuple[str, ...] = ("dexscreener", "gmgn")

#: How many times the gate budget the top-up pass reaches, deepest pool first. A row below
#: that line cannot reach the gate's top `cap` even if every row above it is dropped, so
#: asking about it would cost the same and change no verdict.
ENRICH_POOL_MULTIPLE = 3


#: How many of the qualified pool gets an off-chain entry, deepest first. This is the one
#: cap in the round that is about money: the chip and safety reads cost seconds per token and
#: no tokens at all, while an entry costs a search, a handful of page reads and a model call
#: (report/research.py) - it is the only pass that spends. So the cut belongs on the far
#: side of the filter, where every row above it has already answered everything the round
#: asks, and never on the near side, where it was choosing between rows nobody had read.
#: Rows below it were rejected by nothing: they are 候选 and carry no score. 0 = all of them.
RESEARCH_LIMIT = 20


def _gate_cap(cfg: dict) -> int:
    """How many survivors the gate judges: the operator's knob, or the whole pool.

    A negative is read as 0 rather than compared against: the knob's own convention is that
    0 means the whole pool, and an unreachable cap would turn _top_up's reach into a
    negative slice - which drops the pool's *deepest* rows, the opposite of the intent.
    """
    cap = cfg.get("gate_limit")
    if cap is None:
        return GATE_LIMIT
    return max(0, int(cap))


def _research_cap(cfg: dict) -> int:
    """How many qualified tokens get an off-chain entry: the operator's knob.

    Same convention as _gate_cap: 0 means all of them, and a negative is read as 0.
    """
    cap = cfg.get("research_limit")
    if cap is None:
        return RESEARCH_LIMIT
    return max(0, int(cap))


def _depth(row: Candidate) -> tuple[float, float]:
    """The shortlist key: liquidity, deepest first, with 24h volume breaking an exact tie.

    Liquidity is the key; volume is not a second key that can outrank it. Depth is the
    expensive number to stand up and volume is the cheap one to fake, so a pool is ranked
    by what is behind it - and where two pools are exactly as deep, by which one is traded.
    """
    return (-(row.metrics.liquidity_usd or 0), -(row.metrics.volume_24h_usd or 0))


def _needs_chips(metrics: TokenMetrics) -> bool:
    """A row the bulk sweep left without a number the hard filter reads.

    The three chip numbers are GMGN's: it is the only scan source that carries all three,
    so asking for them is really the question "did GMGN list this token" - and the read
    that answers brings the honeypot flag back with them, which is why that flag is not a
    fourth condition here. A row that already carries the three carries its flag as well,
    and the gate re-reads the flag through `check_all` for every candidate either way.

    24h volume joins them for a different reason. The bulk sweep only has GMGN's rank row
    for it, and that row answers in the window it ranked in - a short-window reading is
    gated out at the source (`GmGn._metrics`), which leaves the row blank rather than
    wrong. A blank is the one thing the volume floor cannot judge, so it has to be bought
    here, where one per-token read settles it for good. Leaving it blank would be worse
    than asking: 未判定 never rejects, so every short-window row would sail through the
    floor that exists precisely to keep $6-a-day tokens out of the pool.
    """
    return (
        metrics.holders is None
        or metrics.top10_pct is None
        or metrics.bundling_pct is None
        or metrics.volume_24h_usd is None
    )

#: This round’s `targets` in, the off-chain judgement for each out, and told how far
#: along it is so the bar can move while the model writes.
Drafts = Callable[[list[dict], Callable[[float, float], None]], dict[str, dict]]


@dataclass
class Candidate:
    """One token's whole story in this round: facts, verdict, score, place."""

    ca: str
    metrics: TokenMetrics
    facts: SafetyFacts = field(default_factory=SafetyFacts)
    decision: str = "candidate"          # selected | candidate | dropped
    failed: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    breakdown: ScoreBreakdown | None = None
    score: float | None = None
    rank: int | None = None
    evidence: dict = field(default_factory=dict)
    #: This round’s off-chain judgement of the token, resolved once and kept, so the
    #: writer and the score cannot end up reading two different entries.
    entry: dict = field(default_factory=dict)
    #: How this token moved since the last round, in the card's own units.
    deltas: list[dict] = field(default_factory=list)
    #: Whether the per-token sources were asked about it. An enriched token's facts are
    #: worth keeping even when it was dropped, because "why" is a question about facts.
    enriched: bool = False
    #: Each source's own per-token answer, kept so a later pass can re-merge without
    #: re-asking the sources that already answered. Merging is lossy - one winner per
    #: field - so a second merge has to start from the inputs, not from its output.
    views: dict[str, TokenMetrics] = field(default_factory=dict)

    @property
    def symbol(self) -> str | None:
        return self.metrics.symbol


@dataclass
class RoundResult:
    chain_id: str
    run_id: str
    universe: int
    items: dict[str, Candidate]
    #: Prefilter survivors, before the safety gate - the pool the batches are drawn from.
    survivors: list[Candidate]
    #: Survivors that cleared the safety gate and were scored. This is what a round
    #: reports as 候选: the reference writes its candidate count after `cands = _safe`.
    scored: list[Candidate]
    picked: list[Candidate]
    rule_counts: list[tuple[str, int]]
    sources: list[dict]
    reports: list[SourceReport]
    changes: list[dict]
    notes: list[str]
    settings: dict
    #: Survivors the cap left unasked. They are part of 候选 - nothing rejected them -
    #: but no source was ever asked about them, so the round says so rather than letting
    #: the difference between 候选 and what got a number go unexplained.
    unjudged: int = 0

    @property
    def filter_dropped(self) -> int:
        """Tokens the filter rejected, on the run panel's single 淘汰 row.

        Credited the way the settings page credits them - the first rule to reject a token
        owns it - so the row equals the sum of its own per-rule numbers. Every rule is that
        one row by design (`filters.FILTER_PRIORITY`): the pool rules, the safety gate, the
        trap ceiling and 距高回撤 all reject a token before the round spends a token on it,
        which is exactly what the panel's filter row means. Two rows made a funnel that did
        not add up - 距高回撤 was credited to neither of them.
        """
        rules = {reason.value for reason in filters.FILTER_PRIORITY}
        return sum(
            1
            for item in self.items.values()
            if item.decision == "dropped"
            and funnel.first_reason({"failed": item.failed}) in rules
        )

    @property
    def qualified(self) -> int:
        """How many tokens came out of the safety gate with a number.

        This is the reference's `cands = _safe`, and it is what the gate uses to decide
        whether it has already bought enough cards to stop. It is *not* the round's 候选:
        that is the pool the gate drew from, and `funnel_summary` counts it.
        """
        return len(self.scored)

    @property
    def funnel_summary(self) -> funnel.FunnelSummary:
        """The round's four numbers, counted from the items' own decisions.

        One pass over `items` gives all four, so `scanned` is by construction the sum of
        the other three. Writing them from four different sources is what let the strip
        read 扫描 3,605 → 淘汰 3,537 → 候选 9 → 入选 5 while the rows said 60 candidates:
        淘汰 was the prefilter's subtotal, 候选 was what the gate scored, and neither was
        the count of items that had to add up. 候选 here is the pool that survived the
        prefilter, 入选 included: the tokens the gate drew from.
        """
        return funnel.summarize({"decision": item.decision} for item in self.items.values())


class Engine:
    """One round, end to end, against whatever sources it was handed."""

    def __init__(
        self,
        chain: dict,
        *,
        sources: list | None = None,
        extras: tuple | None = None,
        reader=onchain,
        drafts: Drafts | None = None,
        previous: list[dict] | None = None,
        peaks: dict[str, float] | None = None,
    ) -> None:
        self.chain = chain
        self.sources = list(sources) if sources is not None else registry.for_chain(chain)
        # The enrich-only sources come with the registry, so a caller that handed its own
        # sources - every test, and the engine’s own harness - gets none unless it names
        # them. That is what keeps a test run offline without five call sites each listing
        # the extras they do not want. See `registry.ENRICH_ONLY`.
        self.extra_sources = tuple(
            registry.ENRICH_ONLY if extras is None and sources is None else (extras or ())
        )
        self.reader = reader
        self.drafts = drafts or (lambda targets, _tell: {})
        self.previous = list(previous or [])
        #: 距高回撤 reads its high from here: the highest market cap this project has seen
        #: for a CA. The pipeline hands it over, because a store read belongs to the caller
        #: and the engine stays offline for every test that does not want one.
        self.peaks = {ca_key(ca): float(value) for ca, value in (peaks or {}).items()}

    # ------------------------------------------------------------------ the round

    def run(self, settings: dict, run_id: str,
            on_stage: Callable[[str], None] | None = None,
            on_progress: Callable[[int, str], None] | None = None,
            clock: progress.Timer | None = None,
            on_funnel: Callable[..., None] | None = None) -> RoundResult:
        """One round. `on_stage` fires at each boundary in `STAGES`, in order; `on_progress`
        fires inside the long phases, so the page has something to draw between them.

        `clock` is the phase stopwatch. A caller that owns what happens after this returns
        - the write - hands its own in, so those phases land on the same
        log line instead of the round's last stretch going unmeasured.

        `on_funnel` fires with whatever the round knows so far: the source counts once
        the pull is over, the pool once the hard filter has run, 候选 once the gate has
        answered. The page draws a running round out of `runs` and `run_summary`, and
        those used to be written once, at the end - which is why a live panel showed a
        column of dashes. The numbers are the ones `store_round` writes again later: one
        round, one set of numbers, two moments.
        """
        stage = on_stage or (lambda _status: None)

        def boundary(status: str) -> None:
            """A stop can only be answered where the round already looks up, and the three
            stage boundaries are the ones it cannot be inside: a round stopped at one of them
            can still say which stage it reached - which is the whole value of the record.
            """
            cancel.raise_if(run_id)
            stage(status)

        owns_clock = clock is None
        clock = clock or progress.Timer(run_id)

        def told(phase: str) -> Callable[..., None]:
            def tell(done: float, total: float) -> None:
                if on_progress:
                    on_progress(progress.pct(phase, done, total), progress.label(phase))
            return tell
        try:
            return self._round(settings, run_id, boundary, told, clock,
                               on_funnel or (lambda **_fields: None))
        finally:
            # The bill, always - a round that failed is the one whose timings someone will
            # want, and `emit()` used to have no caller at all, so no round ever printed one.
            # A caller-supplied clock is the caller's to emit: it owns phases after this.
            if owns_clock:
                clock.emit()

    def _round(self, settings: dict, run_id: str, stage: Callable[[str], None],
               told: Callable[[str], Callable[..., None]],
               clock: progress.Timer,
               on_funnel: Callable[..., None]) -> RoundResult:
        """The body of `run`, so the round is wrapped in one place."""
        cfg = settings.get("pipeline") or {}
        prefilter_cfg = cfg.get("prefilter") or {}
        safety_cfg = cfg.get("safety") or {}
        enrich_limit = int(cfg.get("enrich_limit") or DEFAULT_ENRICH_LIMIT)
        per_round = int((settings.get("report") or {}).get("per_round") or 5)
        # Before the first source is asked: a round stopped while it waited in the queue must
        # not spend a minute pulling a chain nobody is going to read.
        cancel.raise_if(run_id)

        notes: list[str] = []
        # The pull is told the same floor the prefilter is about to enforce, so a scanner
        # that can stop early is never told to stop above the line the round draws.
        collector.set_scan_floor(self.sources, prefilter_cfg.get("market_cap_min_usd"))
        # A source that filters its own board server-side takes the whole box, not just the
        # line under it: the same three numbers the prefilter is about to enforce.
        collector.set_scan_band(self.sources, prefilter_cfg)
        universe = collector.collect(
            self.chain, run_id, self.sources,
            on_source=told("pull"),
            on_pulled=lambda: clock.mark("pull"),
        )
        clock.mark("dedupe")
        # The pull is over, so the sources' own counts are final and so is the size of the
        # pool they produced. Neither waits for the write: a panel that shows them only at
        # the end is a panel that says nothing for the twenty minutes that matter.
        on_funnel(sources=self._source_counts(universe, settings),
                  scanned=len(universe.metrics))
        failed_sources = [report.source for report in universe.reports if not report.ok]
        if failed_sources:
            notes.append("数据源未答复：" + "、".join(failed_sources) + "（本轮按缺值处理）")
        stage(STAGES[0])
        items: dict[str, Candidate] = {}
        pool: list[Candidate] = []
        for ca, metrics in universe.metrics.items():
            verdict = filters.check_prefilter_free(metrics, prefilter_cfg)
            item = Candidate(
                ca=ca,
                metrics=metrics,
                failed=[reason.value for reason in verdict.failed],
                unknown=[reason.value for reason in verdict.unknown],
            )
            if verdict.passed:
                pool.append(item)
            else:
                item.decision = "dropped"
            items[ca] = item

        # 流动性, and nothing else - deepest pool first, with 24h volume breaking an exact
        # tie and never outranking depth.
        pool.sort(key=_depth)
        clock.mark("prefilter")

        # 二档: the chip rules read numbers the bulk sweep only carries for the rows its
        # sources happen to list, and 未判定 never rejects - so a row missing one is asked
        # about by name *before* the rules that read it run. A blank at this point is a blank
        # the paid read could not fill either.
        cap = _gate_cap(cfg)
        self._top_up(pool, universe, cap, told=told("topup"), run_id=run_id)
        pool.sort(key=_depth)      # the top-up may have corrected a row's depth
        clock.mark("topup")

        survivors: list[Candidate] = []
        for item in pool:
            verdict = filters.check_prefilter_enriched(item.metrics, prefilter_cfg)
            item.failed = [reason.value for reason in verdict.failed]
            item.unknown = [reason.value for reason in verdict.unknown]
            if verdict.passed:
                survivors.append(item)
            else:
                item.decision = "dropped"

        # The hard filter is done: everything it rejected is rejected, and what is left is
        # the pool the gate is about to spend its minutes on. 淘汰 is written here too, not
        # only at the gate: the strip under the panel reads `runs`, and a strip that says
        # 淘汰 0 while the row above it says 淘汰 3,518 is two answers to one question.
        pool_dropped = len(items) - len(survivors)
        on_funnel(dropped=pool_dropped, dropped_hard=pool_dropped)
        gated = self._gate(survivors, universe, cfg, enrich_limit, told=told("gate"),
                           run_id=run_id)
        clock.mark("gate")
        # 候选 is the pool that cleared the gate, 入选 counted inside it - the same identity
        # `store_round` writes at the end, so the running number and the final number are
        # one number and the page never re-labels itself mid-round.
        # The same counting `store_round` does at the end, run now. 候选 has to include the
        # survivors the cap left unjudged - nothing rejected them - or the running strip
        # would read 扫描 3,588 → 淘汰 3,530 → 候选 6 and quietly lose fifty-two tokens
        # between the rows. `len(gated)` is exactly that mistake: it is the judged pool,
        # not the candidate pool.
        counts = funnel.summarize([{"decision": item.decision} for item in items.values()])
        on_funnel(
            scanned=counts.scanned,
            dropped=counts.dropped,
            candidates=counts.candidates,
            rule_counts=funnel.cumulative_drop_counts(
                [{"decision": item.decision, "failed": item.failed} for item in items.values()]
            ),
        )
        unjudged = [item for item in survivors if not item.enriched]
        if unjudged:
            notes.append(
                "按流动性排序，本轮只判定前 %d 个候选；其余 %d 个未判定"
                "（既不计通过也不计淘汰）" % (len(survivors) - len(unjudged), len(unjudged))
            )

        if not gated:
            notes.append("本轮没有标的通过安全判定，未产出报告")

        # 调研上限. The filter is over, so what is left is the set the round would publish if
        # it could afford to read all of it. The off-chain pass is the one that costs money
        # and time (RESEARCH_LIMIT), so its budget is spent here - deepest first, the same
        # key the gate sorted by when it was the pass holding the budget. A row below the cut
        # was rejected by nothing: it stays 候选 with no score rather than being quietly
        # dropped, and the note says so.
        research_cap = _research_cap(cfg)
        qualified = sorted(gated, key=_depth)
        researched = qualified[:research_cap] if research_cap else qualified
        if len(researched) < len(qualified):
            notes.append(
                "过关候选按流动性排序，本轮只调研前 %d 个；其余 %d 个未调研"
                "（未判定，不参与排名）" % (len(researched), len(qualified) - len(researched))
            )

        # Every researched token is judged again this round. Nothing is carried over from the
        # round before, which is what makes the prose on a card and the score beside it the
        # same dossier by construction rather than by a refresh rule.
        targets = [
            {"ca": item.ca, "symbol": item.symbol, "metrics": item.metrics, "facts": item.facts}
            for item in researched
        ]
        judged = self.drafts(targets, told("draft")) if targets else {}
        clock.mark("draft")
        missing = [item for item in researched if not judged.get(item.ca)]
        if missing:
            notes.append(
                "未判定：" + "、".join(item.symbol or item.ca[:10] for item in missing)
                + "（本轮不参与排名）"
            )

        # 口径 #1: the denominator is this round's prefilter survivors, read after the
        # gate so the token and the pool it is ranked against are on the same basis. Under
        # the default `gate_rounds` every survivor is judged, so no token is compared
        # against a peer whose numbers were never refreshed.
        percentiles = scoring.CandidateSetPercentile([item.metrics for item in survivors])
        for item in gated:
            # An unjudged token stays out of the ranking: `None` here is what makes
            # `score_token` report it as pending instead of reading the blank as zeros.
            judged_entry = judged.get(item.ca)
            item.entry = dict(judged_entry or {})
            item.breakdown = scoring.score_token(item.metrics, item.facts, cfg, percentiles,
                                                 judged_entry)
            item.score = item.breakdown.total
            item.deltas = self._deltas(item)
        clock.mark("score")
        staged = told("score")
        staged(1, 1)
        stage(STAGES[1])

        ranked = sorted(
            [item for item in gated if item.score is not None],
            key=lambda row: -(row.score or 0),
        )
        picked = self._picks(ranked, universe, per_round, prefilter_cfg, run_id)
        # The place each token took in this round's score order, for the 退出 chip: a
        # token that left by ranking sixth and a token that left by falling under a
        # floor are two different events, and the bar used to call both 跌出. Kept local
        # rather than written onto `item.rank`, which is the card's own number and only
        # ever set for the tokens that got one.
        pool_rank = {item.ca: index for index, item in enumerate(ranked, start=1)}
        clock.mark("sharpen")
        for index, item in enumerate(picked, start=1):
            item.decision = "selected"
            item.rank = index
        self._pool_ranks(gated, picked)
        on_funnel(selected=len(picked))
        stage(STAGES[2])

        return RoundResult(
            chain_id=self.chain["id"],
            run_id=run_id,
            universe=len(universe.metrics),
            items=items,
            survivors=survivors,
            scored=gated,
            picked=picked,
            rule_counts=funnel.cumulative_drop_counts(
                [{"decision": item.decision, "failed": item.failed} for item in items.values()]
            ),
            sources=self._source_counts(universe, settings),
            reports=universe.reports,
            changes=self._changes(
                picked, items,
                pool_rank=pool_rank, per_round=per_round,
                prefilter=prefilter_cfg, safety=safety_cfg,
            ),
            notes=notes,
            settings=settings,
            unjudged=len(unjudged),
        )

    def _top_up(self, pool: list[Candidate], universe: collector.Universe, cap: int,
               told: Callable[[float, float], None] | None = None,
               run_id: str | None = None) -> None:
        """Buy the chip picture for the rows the bulk sweep left blank, before it is read.

        The chip rules can only reject a row whose number someone answered, and the bulk
        pull answers them only for the tokens its own list carries: the rows a launchpad
        reports arrive with market numbers and no chip numbers at all. Reading that blank as
        a small holder count is a wrong drop; reading it as "nothing to see" is a wrong pass,
        and the wrong pass is the one that put a family of four-holder rows in the candidate
        pool. So the blanks are filled here, by name, and the rules run afterwards - which
        two sources answer is spelled out on `MARKET_ENRICH`.

        Only rows missing one of those fields are asked about, and only the deepest
        `ENRICH_POOL_MULTIPLE` gate budgets of them: a row ranked below that cannot reach
        the gate even if every row above it drops, so the read would cost the same and
        change no verdict. An uncapped gate (a budget of 0, which means judge the whole
        pool) tops the whole pool up, because every row in it can still reach the gate.
        The answers land on the item and not on `facts`, and the row is not marked
        enriched - the gate's own pass is what reaches a safety verdict, and a row the
        sorting left out of the gate must not look like one that got a full read.
        """
        if not pool:
            return
        # A budget of 0 is "judge the whole pool", so the whole pool is what gets filled.
        reach = max(cap, 1) * ENRICH_POOL_MULTIPLE if cap else len(pool)
        wanted = [item for item in pool if _needs_chips(item.metrics)][:reach]
        if not wanted:
            return
        total = len(wanted)
        counted = 0
        counting = threading.Lock()

        def one_done() -> None:
            nonlocal counted
            with counting:
                counted += 1
                here = counted
            if told:
                told(here, total)

        self._enrich(wanted, universe, one_done, only=MARKET_ENRICH, light=True,
                     run_id=run_id)

    def _gate(self, survivors: list[Candidate], universe: collector.Universe, cfg: dict,
              batch: int, told: Callable[[float, float], None] | None = None,
              run_id: str | None = None,
              ) -> list[Candidate]:
        """Ask the safety questions until the whole candidate pool has an answer.

        The prefilter can leave a hundred survivors and a batch can be rejected wholesale,
        so one batch is not a sample of the pool. The reference pulled batches while it held
        fewer than twice the report count, but its prefilter only ever left it thirteen
        candidates, so that exit never fired: it judged its whole pool. Ours leaves it a
        hundred-odd, and stopping at "enough" would both strand survivors the funnel has no
        column for and pick the top five out of the biggest movers alone.

        The cap is a budget, not a target: when it is spent the round publishes what it has,
        even if that is fewer than `per_round` cards. The one exception is a round that has
        qualified nothing at all - that is not a short report, it is a reading of the pool,
        and it keeps looking rather than shipping an empty page.
        """
        limit = int(cfg.get("gate_rounds") or GATE_ROUNDS)
        cap = _gate_cap(cfg)
        total = len(survivors)
        # The bar measures the pool the round intends to judge, not the whole survivor
        # list: with a cap in place the honest denominator is the cap, and the fallback
        # below can only push the numerator past it - never the bar past 100%.
        planned = min(cap, total) if cap else total
        gated: list[Candidate] = []
        checked = 0
        rounds = 0
        counted = 0
        counting = threading.Lock()

        def judged() -> None:
            """One candidate answered. The chunks are minutes long, so the bar is drawn per
            candidate rather than per chunk."""
            nonlocal counted
            with counting:
                counted += 1
                here = counted
            if told:
                told(here, max(planned, here))

        while checked < len(survivors):
            # The gate asks every survivor of the chain, so it is the phase a stop is most
            # likely to land in, and the candidate is the finest boundary the round already
            # has. One `raise_if` per candidate keeps the answer honest to within one token's
            # network wait.
            cancel.raise_if(run_id)
            if limit and rounds >= limit:
                break
            # 宁缺勿滥. The old rule read "keep going until there are enough cards to fill",
            # and that is the rule that put empty rows in the lexicon: the shells a thin
            # pull leaves behind sit at the bottom of the volume sort, so the fallback was
            # exactly the part of the pool it walked into. A budget is a budget - 3 cards
            # beat 5 where the last two rows have no numbers to show.
            if cap and checked >= cap and gated:
                break
            # The last batch inside the budget is cut to the budget: a cap of twenty must
            # mean twenty reads, not the batch boundary above them. Once the budget is
            # spent the fallback below runs whole batches, because at that point the round
            # is buying cards rather than bytes.
            end = checked + batch
            if cap and checked < cap:
                end = min(end, cap)
            chunk = survivors[checked:end]
            self._enrich(chunk, universe, judged, defer=DEFERRED_ENRICH, run_id=run_id)
            for item in chunk:
                cancel.raise_if(run_id)
                # 距高回撤 is judged from what the row already carries: the high GMGN
                # measured, and the high this project recorded. The gate buys no pool read
                # of its own - the pool history can only ever drop a token, never save one,
                # and the free readings cover the tokens that read used to be bought for
                # (see `filters.drawdown_readings`). A pick is judged again against its
                # pools by the pass that reads them anyway (`_picks`).
                verdict = filters.check_all(item.metrics, item.facts, cfg,
                                            peak_market_cap_usd=self.peaks.get(item.ca))
                item.failed = [reason.value for reason in verdict.failed]
                item.unknown = [reason.value for reason in verdict.unknown]
                if verdict.passed:
                    gated.append(item)
                else:
                    item.decision = "dropped"
            checked += len(chunk)
            rounds += 1
        return gated

    # ------------------------------------------------------------------- the reads

    def _picks(self, ranked: list[Candidate], universe: collector.Universe, per_round: int,
               prefilter: dict, run_id: str | None) -> list[Candidate]:
        """The cards, taken in score order, with 距高回撤 judged as their pool is read.

        The deferred pass below spends one GeckoTerminal request per pool on the pools that
        draw the card, and that request carries their historical high. So 距高回撤 is judged
        a second time here, where that reading finally exists: the gate ruled on the two free
        readings (the high GMGN measured and the high this project recorded), and the pool
        history is what can still drop a pick neither of them has a high for - a reading
        that had to be bought anyway, and one that silence must not be read as a pass for
        (`metrics.drawdown_unread`). A pick that is 70% below its high is dropped
        and the next candidate by score takes the place, so a round still publishes
        `per_round` cards whenever the pool below them has that many live tokens.
        """
        picked: list[Candidate] = []
        cursor = 0
        while len(picked) < per_round and cursor < len(ranked):
            cancel.raise_if(run_id)
            # Only as many as still fit: a drop is the round asking about one more.
            batch = ranked[cursor:cursor + (per_round - len(picked))]
            cursor += len(batch)
            # The deferred read, spent on the picks alone - see `DEFERRED_ENRICH`. It runs
            # after the score on purpose: nothing it fills is scored, and the card is the
            # only reader that wants it.
            self._enrich(batch, universe, only=DEFERRED_ENRICH, run_id=run_id)
            for item in batch:
                verdict = filters.check_drawdown(item.metrics, prefilter, self.peaks.get(item.ca))
                if verdict.passed:
                    # A pick whose pool history came back unreadable is 未判定, not silently
                    # passed: this is the only pass that buys that read, so it is the only
                    # place the round can learn the question went unanswered.
                    item.unknown = list(dict.fromkeys(
                        item.unknown + [reason.value for reason in verdict.unknown]))
                    picked.append(item)
                    continue
                item.decision = "dropped"
                item.failed = [reason.value for reason in verdict.failed]
        return picked


    def _enrich(self, shortlist: list[Candidate], universe: collector.Universe,
                done: Callable[[], None] | None = None, *,
                defer: tuple[str, ...] = (), only: tuple[str, ...] = (),
                light: bool = False, run_id: str | None = None) -> None:
        """Ask every source about every shortlisted token, then re-merge the token's view.

        Per token the sources are asked at the same time and each host paces itself, so five
        network calls overlap without any one host seeing a burst. Asking them one after
        another instead made a candidate cost the *sum* of every source's latency - its own
        reads are independent of each other, and only the merge needs them all.

        `defer` names the sources this pass leaves out and `only` the ones a later pass
        asks about on its own - the two halves of `DEFERRED_ENRICH`. `extra_sources` are
        asked here and nowhere else: they have no bulk row to sharpen, so the pull cannot
        have asked them, and a pass that names an `only` is asking for its own short list.
        `light` asks each source's cheap entry instead (`fetch_chips`), for the passes that
        read one number off the answer - see `_top_up`. A source without one is still
        asked the full way.

        `run_id` is this round's own id, and it is what makes the pass a stop boundary: the
        token fan-out is handed the round's check (`pmap`), so a stop asked for while the
        pool is being filled starts no further token. The ones already in flight are left to
        finish - the price of a busy pool being seconds rather than the rest of it - and the
        round ends at the next boundary instead of carrying a half-filled pool into the gate.
        The later pass adds
        its answers to the first pass's `views` rather than starting over, because
        re-merging from the bulk rows alone would replace a source's fresh per-token
        answer with the stale row it was meant to sharpen. It leaves `facts` and the LP
        read alone too: the safety verdict was reached in the first pass, and one
        source's empty answer must not erase it.
        """
        rows = universe.rows

        def one(item: Candidate) -> None:
            asked = [
                source for source in (*self.sources, *self.extra_sources)
                if source.name not in defer and (not only or source.name in only)
            ]

            def ask(source) -> tuple[str, tuple[TokenMetrics, SafetyFacts] | None]:
                """One source's answer, or None. A source that raises has said nothing,
                which the merge already reads as a gap - it is not this round's failure."""
                fetch = getattr(source, "fetch_chips", None) if light else None
                try:
                    if fetch is None:
                        return source.name, source.fetch_token(self.chain["id"], item.ca)
                    return source.name, fetch(self.chain["id"], item.ca)
                except Exception:
                    return source.name, None

            views: dict[str, TokenMetrics] = {}
            facts = SafetyFacts()
            for name, answer in pmap(ask, asked, workers=len(asked) or 1):
                if answer is None:
                    continue
                views[name], safety = answer
                facts = absorb(facts, safety)
            views = {**item.views, **views}
            item.views = views
            merged_sources: dict[str, TokenMetrics] = {}
            for name, per_ca in rows.items():
                merged_sources[name] = collector.overlay(per_ca.get(item.ca), views.get(name) or TokenMetrics(ca=item.ca))
            for name, view in views.items():
                merged_sources.setdefault(name, view)
            ordered = tuple(sorted(
                (name for name in merged_sources if merged_sources[name] is not None),
                key=lambda name: collector.DEFAULT_PRIORITY.index(name)
                if name in collector.DEFAULT_PRIORITY else len(collector.DEFAULT_PRIORITY),
            ))
            # The universe's own merged view is the floor, not the ceiling: the per-token
            # answers win every field they filled, and the bulk sweep only fills the blanks
            # they left. `overlay(base, extra)` lets `extra` win, so the bulk view has to be
            # the base - the other way round the expensive per-token phase could never
            # sharpen a number the bulk sweep had already reported.
            merged = collector.merge(item.ca, merged_sources, ordered)
            item.metrics = collector.overlay(
                universe.metrics.get(item.ca) or TokenMetrics(ca=item.ca), merged,
            )
            # `overlay` deliberately leaves provenance alone, so the per-token winners would
            # lose their attribution to the bulk row they overrode. The merged view knows who
            # won each field; the floor only supplies the fields it never covered.
            item.metrics.provenance = {**item.metrics.provenance, **merged.provenance}
            if not only:
                item.facts = facts
                item.enriched = True
                self._lock_facts(item)
            if done:
                done()

        pmap(one, shortlist, workers=ENRICH_WORKERS, stop=cancel.check(run_id))

    def _lock_facts(self, item: Candidate) -> None:
        """Fill in the LP verdict for one candidate - see `lp_verdict`."""
        locked, basis, evidence = lp_verdict(self.chain, item.metrics, item.facts,
                                            reader=self.reader)
        item.facts.lp_locked = locked
        item.facts.lp_note = basis
        item.evidence = evidence

    def _pool_ranks(self, pool: list[Candidate], picked: list[Candidate]) -> None:
        """Where each pick sits inside this round's pool, for the card's three ranks."""
        chosen = {item.ca for item in picked}
        for field_name, attribute in (
            ("liquidity_usd", "liquidity_rank"),
            ("volume_24h_usd", "volume_rank"),
            ("holders", "holders_rank"),
        ):
            ordered = sorted(pool, key=lambda row: -(getattr(row.metrics, field_name) or 0))
            for index, item in enumerate(ordered, start=1):
                if item.ca in chosen:
                    setattr(item.metrics, attribute, index)

    def _source_counts(self, universe: collector.Universe, settings: dict) -> list[dict]:
        """Only the scanners: the verification sources have no universe to count."""
        names = list((settings.get("pipeline") or {}).get("scan_sources") or [])
        out = []
        for name in names:
            if name in universe.per_source:
                out.append({"name": name, "count": universe.per_source[name]})
        return out

    def _deltas(self, item: Candidate) -> list[dict]:
        """This token against its own last appearance: four numbers, in the card's units."""
        before = next((row for row in self.previous if ca_key(row.get("ca")) == ca_key(item.ca)), None)
        if not before:
            return []
        out: list[dict] = []
        metrics = item.metrics
        for key, label, current, previous, percent in (
            ("market_cap", "市值", metrics.market_cap_usd, before.get("market_cap_usd"), True),
            ("liquidity", "流动性", metrics.liquidity_usd, before.get("liquidity_usd"), True),
            ("volume_24h", "成交", metrics.volume_24h_usd, before.get("volume_24h_usd"), True),
            ("holders", "持有人", metrics.holders, before.get("holders"), False),
        ):
            if current is None or not previous:
                continue
            if percent:
                moved = (float(current) / float(previous) - 1) * 100
                # A move smaller than a percent is written as no move: "+0%" and "-0%" are
                # the same round, and a sign on a zero is a claim the numbers do not make.
                out.append({"key": label, "value": "0%" if abs(moved) < 0.5 else "%+.0f%%" % moved})
            else:
                changed = int(current) - int(previous)
                out.append({"key": label, "value": "0" if changed == 0 else "%+d" % changed})
        return out

    def _changes(self, picked: list[Candidate], items: dict[str, Candidate], *,
                 pool_rank: dict[str, int], per_round: int,
                 prefilter: dict, safety: dict) -> list[dict]:
        """What moved since the last round: who arrived, who left, and why it left.

        `items` is the round's whole verdict on the pool - every token it pulled, dropped
        or not - and it is what turns 退出 from a shrug into a reason. The chip used to say
        "跌出硬过滤或排名" without saying which, and which is the only thing a reader can
        act on: 市值 fell under the floor, LP came unlocked, or it simply ranked sixth this
        time. All three are in hand by the time this runs, and all three read out.
        """
        if not self.previous:
            return [{"kind": "首次运行", "direction": "in", "sym": "—", "detail": "首次运行，无对比基线"}]
        before = {ca_key(row.get("ca")): row for row in self.previous}
        now = {item.ca: item for item in picked}
        changes: list[dict] = []
        for item in picked:
            if item.ca in before:
                continue
            symbol = item.symbol or item.ca[:10]
            detail = "%s（市值 %s，24h %s）" % (
                symbol, _usd(item.metrics.market_cap_usd), _usd(item.metrics.volume_24h_usd),
            )
            changes.append({"kind": "新进", "direction": "in", "sym": symbol, "detail": detail})
        for ca, row in before.items():
            if ca in now:
                continue
            # `previous_picks` calls the field `symbol`. Reading `sym` here is why every
            # 退出 row on the change bar showed a hex address instead of a name: the
            # lookup always missed, so the fallback always fired.
            symbol = row.get("symbol") or ca[:10]
            changes.append({
                "kind": "退出",
                "direction": "out",
                "sym": symbol,
                "detail": "%s（上期市值 %s → %s）" % (
                    symbol, _usd(row.get("market_cap_usd")),
                    self._left(ca, items, pool_rank, per_round, prefilter, safety),
                ),
            })
        if not changes:
            changes.append({
                "kind": "与上期一致", "direction": "in", "sym": "—",
                "detail": "与上期 Top%d 一致（排名或数据有微调）" % max(1, len(picked)),
            })
        return changes


    def _left(self, ca: str, items: dict[str, Candidate], pool_rank: dict[str, int],
              per_round: int, prefilter: dict, safety: dict) -> str:
        """Why this CA is not on this round's report, in one clause."""
        item = items.get(ca)
        if item is None:
            # Not pulled at all: a source that stopped listing it, or a scan floor it fell
            # under before anyone had a chance to judge it. Not the same as being rejected.
            return "本轮未被任何数据源拉到"
        if item.decision == "dropped":
            reason = funnel.first_reason({"failed": item.failed})
            if not reason:
                return "本期被硬过滤淘汰"
            return _rule_note(reason, item.metrics, prefilter, safety, self.peaks.get(ca))
        rank = pool_rank.get(ca)
        if rank is not None:
            return "本期综合分排名第 %d，入选只取前 %d" % (rank, per_round)
        if not item.enriched:
            # 候选, but the gate's budget ran out before anyone asked about it. A silence,
            # and it says so rather than borrowing a verdict it never got.
            return "本轮未判定（按流动性排序，判定名额之外）"
        return "本期无综合分"


def lp_verdict(chain: dict, metrics: TokenMetrics, facts: SafetyFacts,
               reader=onchain) -> tuple[bool | None, str, dict]:
    """The LP question for one token: which reading of 已锁 is the one of record.

    GMGN answers it, and its answer wins: `lock_summary.is_locked` is the verdict the
    settings page means by 已锁, and it is the only one of the readings that covers the v4
    shape - a v4 pool has no LP token and no pool contract, so a chain read can only guess
    from position NFTs. The chain read stays as the fallback for the tokens GMGN has no sheet
    for, and the GoPlus LP holder table after it.

    (GoPlus's table is the last word for the same reason in reverse: two states, so a token
    the chain could not judge either gets a verdict from the holder table or stays 未判定
    because that table covered too little of the LP to say.)

    Module-level rather than a method because a CA search asks the same question about a
    token that is in no round. Two copies of this order would be two opinions about 已锁.
    """
    creator = facts.creator or metrics.creator or ""
    if facts.lp_source == "gmgn" and facts.lp_locked is not None:
        return facts.lp_locked, facts.lp_note or "", {"creator": creator, "lp_from": "gmgn"}
    try:
        locked, basis, evidence = reader.lock_facts(
            chain,
            ca=metrics.ca,
            pair=metrics.dex_pair,
            creator=creator,
            launchpad=metrics.launchpad,
        )
    except Exception as exc:
        locked, basis, evidence = None, "链上查询失败：%r" % (exc,), {}
    if locked is None:
        locked, sourced = onchain.lp_lock_from_holders(metrics.lp_holders)
        if locked is not None:
            basis = sourced
    out = dict(evidence or {})
    out["creator"] = creator
    return locked, basis, out


def absorb(base: SafetyFacts, extra: SafetyFacts) -> SafetyFacts:
    """Fill `base`'s blanks from `extra`, merging the two evidence lists.

    Neither source is asked twice about the same fact, so a value already present is not
    overwritten - except for the verified flag, which is a yes/no/unknown a second source
    may be able to turn into a yes.

    A blank is `None` and nothing else. An empty list is an answer: `dangerous_perms == []`
    is GoPlus saying it read the permissions and found none dangerous, and treating that as
    a gap is how a clean token came out 未判定 (the check is `is None`, so `0` and `[]`
    survive the merge as the values they are).
    """
    filled = {
        name: getattr(extra, name)
        for name in SafetyFacts.model_fields
        if getattr(extra, name) is not None
        and getattr(base, name) is None
    }
    merged = base.model_copy(update=filled)
    verified = list(dict.fromkeys(list(base.source_verified_by) + list(extra.source_verified_by)))
    checked = list(dict.fromkeys(list(base.source_checked_by) + list(extra.source_checked_by)))
    merged.source_verified_by = verified
    merged.source_checked_by = checked
    # The merged flag is only a fallback for a caller with no votes to read (a seeded
    # snapshot), and it uses the same reading as `dual` - one verifier vouching is enough -
    # so absorbing a second source can turn a no into a yes but never the other way round.
    # The verdict itself is read off the votes by core/filters.py, which is what makes it
    # independent of the order the sources happened to answer in.
    if checked or verified:
        merged.source_verified = bool(verified)
    return merged


def _usd(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1e6:
        return "$%.2fM" % (value / 1e6)
    if value >= 1e3:
        return "$%.1fK" % (value / 1e3)
    return "$%.0f" % value

def _pct(value: float | None) -> str:
    """A percentage as the settings page writes one, without a trailing `.0`."""
    return "—" if value is None else "%g%%" % round(float(value), 1)


def _count(value: int | None) -> str:
    return "—" if value is None else "{:,}".format(int(value))


def _clause(name: str, value, limit, *, formatter=None, wording: str = "低于下限") -> str:
    """One threshold miss as a sentence: the name, the number, the side, the bound.

    `formatter` is resolved at call time rather than as a default argument, because the
    money formatter is defined below the class that calls this one.
    """
    if value is None or limit is None:
        return "%s 无数据" % name
    render = formatter or _usd
    return "%s %s %s %s" % (name, render(value), wording, render(limit))


def _rule_note(reason: str, metrics: TokenMetrics, prefilter: dict, safety: dict,
               peak_market_cap_usd: float | None = None) -> str:
    """Which rule rejected a token this round, in the reader's own words and numbers.

    The label is `funnel.RULE_LABEL` - the words the settings row is drawn under - and the
    bound comes off the config the round actually ran with, so the sentence on the change
    bar and the knob on the settings page cannot disagree about what the rule is.
    """
    if reason == "market_cap":
        cap = metrics.market_cap_usd
        low = prefilter.get("market_cap_min_usd")
        high = prefilter.get("market_cap_max_usd")
        if cap is None:
            return "市值 无数据"
        if low is not None and cap < float(low):
            return _clause("市值", cap, low)
        if high is not None and cap > float(high):
            return _clause("市值", cap, high, wording="高于上限")
        return "市值 %s 出界" % _usd(cap)
    if reason == "liquidity":
        return _clause("流动性", metrics.liquidity_usd, prefilter.get("liquidity_min_usd"))
    if reason == "volume_24h":
        return _clause("24h 成交", metrics.volume_24h_usd, prefilter.get("volume_24h_min_usd"))
    if reason == "holders":
        low = prefilter.get("holders_min")
        high = prefilter.get("holders_max")
        now = metrics.holders
        # 持有人 is a band now, so the sentence has to say which end it missed. A value that
        # is simply absent is not a miss on either end - call it a hole, not a breach.
        if low is not None and (now is None or int(now) < int(low)):
            return _clause("持有人", now, low, formatter=_count)
        return _clause("持有人", now, high, formatter=_count, wording="高于上限")
    if reason == "top10":
        return _clause("Top10 集中度", metrics.top10_pct, prefilter.get("top10_max_pct"),
                       formatter=_pct, wording="高于上限")
    if reason == "bundling":
        return _clause("捆绑率", metrics.bundling_pct, prefilter.get("bundling_max_pct"),
                       formatter=_pct, wording="高于上限")
    if reason == "honeypot":
        return "检出貔貅"
    if reason == "entrapment":
        return _clause("陷阱占比", metrics.honeypot_pct, safety.get("honeypot_max_pct"),
                       formatter=_pct, wording="高于上限")
    if reason == "market_cap_drawdown":
        # The number that decided, not the pool reading: the rule drops on the deepest of
        # several readings of the same high, so a clause quoting any single one of them could
        # print a figure that is above the bound the sentence names.
        readings = filters.drawdown_readings(metrics, prefilter, peak_market_cap_usd)
        return _clause("距高回撤", min(readings) if readings else None,
                       prefilter.get("drawdown_max_pct"), formatter=_pct, wording="超过")
    if reason == "empty_shell":
        return "市值与流动性都没有（源只给到地址）"
    if reason == "missing_data":
        return "五个评分因子全无数据"
    if reason == "source_unverified":
        return "源码未验证"
    if reason == "lp_unlocked":
        return "LP 未锁定"
    if reason == "dangerous_perm":
        return "合约含危险权限"
    return funnel.RULE_LABEL.get(reason, reason)
