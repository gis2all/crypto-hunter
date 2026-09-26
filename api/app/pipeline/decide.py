"""Steps 2-6 of a round: prefilter, enrich, gate, score, select.

The rules live in `core/` and the network lives in `sources/`; this module is the
orchestration that walks one past the other, and it is the only place that knows the
order. It is a class rather than a function so a test can hand it fake sources and a
fake chain reader and still exercise the exact path a live round takes - the order is
the thing worth testing, and the order must not be a special case for tests.

What the stages are, and why they are in this order:

1. **collect**    - one bulk pull per source, merged field-wise.
2. **prefilter**  - the pool rules, which are free: they read the bulk pull alone.
3. **shortlist**  - the prefilter survivors by 24h volume, cut to `enrich_limit`. The
   expensive per-token questions are spent here and nowhere else.
4. **enrich**     - per-token answers from every source, including the deployer.
5. **gate**       - the entrapment ceiling, the source check, LP lock, permissions.
6. **draft**      - a lexicon entry for any shortlisted token that has none, because
   this chain's 未入库标的 policy is "待建档，本轮不参与排名": without the entry there is no
   product score, and without a product score there is no ranking. The same stage also
   rewrites the entries that have gone stale (`core.lexicon`), and that has to happen here
   rather than after the publish: the entry is 45% of the score as well as the words on the
   card, so a card written from a fresh entry beside a score computed from a stale one would
   be two claims about one token.
7. **score**      - five on-chain percentiles inside this round, plus the product block.
8. **select**     - the top `per_round` by total, and the in-pool ranks for the card.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

from .. import cancel
from ..core import filters, funnel, lexicon, scoring
from ..core.models import DropReason, SafetyFacts, ScoreBreakdown, TokenMetrics
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

#: How many survivors the gate judges, by default. The reference implementation enriched
#: twelve and stopped once it held twice the report count; judging the whole pool is what
#: made our rounds take twenty minutes, because per-token reads are paced per host and that
#: cap is serial - more workers cannot shorten it. Twenty is the operator's number: the
#: pool is sorted by 24h volume, so these are the twenty most traded candidates, and the
#: rest are left 未判定 (neither passed nor dropped) rather than quietly judged on stale
#: bulk numbers. 0 restores "judge the whole pool".
GATE_LIMIT = 20

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

Lexicon = Callable[[str], dict | None]
#: `gaps` in, first drafts out, and told how far along it is so the bar can move while
#: the model writes.
Drafts = Callable[[list[dict], Callable[[float, float], None]], dict[str, dict]]
#: `targets` in (each carrying its own `reason`), rewritten entries out - `{}` for the ones
#: that were not rewritten, a confirmed entry among them.
Refresh = Callable[[list[dict], Callable[[float, float], None]], dict[str, dict]]


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
    #: The lexicon entry this token was scored against, resolved once and kept, so the
    #: writer and the score cannot end up reading two different entries.
    entry: dict = field(default_factory=dict)
    #: The badge for a card whose words are older than its numbers: how long ago the entry
    #: was written, and why it was not rewritten this round. Empty when this round wrote it.
    entry_note: str = ""
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
    def prefilter_dropped(self) -> int:
        """Tokens the pool-level rules rejected, on the run panel's 硬过滤 row.

        Credited the way the settings page credits them - the first rule to reject a token
        owns it - so the row equals the sum of its own per-rule numbers and the panel's
        columns add up to the scan. A token that only failed a pool rule once enrichment
        sharpened its numbers is a pool-rule drop too: it was never the safety gate's.
        """
        pool_rules = {reason.value for reason in filters.PREFILTER_PRIORITY}
        return sum(
            1
            for item in self.items.values()
            if item.decision == "dropped"
            and funnel.first_reason({"failed": item.failed}) in pool_rules
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
        lexicon: Lexicon | None = None,
        drafts: Drafts | None = None,
        refresh: Refresh | None = None,
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
        self.lexicon = lexicon or (lambda ca: None)
        self.drafts = drafts or (lambda gaps, _tell: {})
        self.refresh = refresh or (lambda targets, _tell: {})
        self.previous = list(previous or [])
        #: 距高回撤 reads its high from here: the highest market cap this project has seen
        #: for a CA. The pipeline hands it over, because a store read belongs to the caller
        #: and the engine stays offline for every test that does not want one.
        self.peaks = {str(ca).lower(): float(value) for ca, value in (peaks or {}).items()}

    # ------------------------------------------------------------------ the round

    def run(self, settings: dict, run_id: str,
            on_stage: Callable[[str], None] | None = None,
            on_progress: Callable[[int, str], None] | None = None,
            clock: progress.Timer | None = None,
            on_funnel: Callable[..., None] | None = None) -> RoundResult:
        """One round. `on_stage` fires at each boundary in `STAGES`, in order; `on_progress`
        fires inside the long phases, so the page has something to draw between them.

        `clock` is the phase stopwatch. A caller that owns what happens after this returns
        - the commentary, the write - hands its own in, so those phases land on the same
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
        survivors: list[Candidate] = []
        for ca, metrics in universe.metrics.items():
            verdict = filters.check_prefilter(metrics, prefilter_cfg)
            item = Candidate(
                ca=ca,
                metrics=metrics,
                failed=[reason.value for reason in verdict.failed],
                unknown=[reason.value for reason in verdict.unknown],
            )
            if verdict.passed:
                survivors.append(item)
            else:
                item.decision = "dropped"
            items[ca] = item

        survivors.sort(key=lambda row: -(row.metrics.volume_24h_usd or 0))
        clock.mark("prefilter")
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
                "按 24h 成交额排序，本轮只判定前 %d 个候选；其余 %d 个未判定"
                "（既不计通过也不计淘汰）" % (len(survivors) - len(unjudged), len(unjudged))
            )

        if not gated:
            notes.append("本轮没有标的通过安全判定，未产出报告")

        gaps = [
            {"ca": item.ca, "symbol": item.symbol, "metrics": item.metrics, "facts": item.facts}
            for item in gated
            if not self.lexicon(item.ca)
        ]
        # The rewrite pass runs beside the draft pass, before the score, and the reason it
        # has to be before the score is the whole point: the entry is 45% of the product
        # block as well as the words on the card. Rewriting after the ranking would publish
        # a card whose prose and whose score were read off two different dossiers.
        stale = self._stale(gated, cfg)
        drafted = self.drafts(gaps, told("draft")) if gaps else {}
        refreshed = self.refresh(stale, told("draft")) if stale else {}
        clock.mark("draft")
        missing = [item for item in gated if not (self.lexicon(item.ca) or drafted.get(item.ca))]
        if missing:
            notes.append(
                "未建档：" + "、".join(item.symbol or item.ca[:10] for item in missing)
                + "（本轮不参与排名）"
            )

        # 口径 #1: the denominator is this round's prefilter survivors, read after the
        # gate so the token and the pool it is ranked against are on the same basis. Under
        # the default `gate_rounds` every survivor is judged, so no token is compared
        # against a peer whose numbers were never refreshed.
        percentiles = scoring.CandidateSetPercentile([item.metrics for item in survivors])
        for item in gated:
            fresh = refreshed.get(item.ca) or drafted.get(item.ca)
            entry = fresh or self.lexicon(item.ca)
            item.entry = dict(entry or {})
            # Only when the words are not this round's. A fresh entry needs no badge; an old
            # one gets one, and getting one is the difference between a card that ages and a
            # card that lies.
            if not fresh and entry:
                item.entry_note = self._entry_note(item, cfg)
            item.breakdown = scoring.score_token(item.metrics, item.facts, cfg, percentiles, entry)
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
        picked = ranked[:per_round]
        # The place each token took in this round's score order, for the 退出 chip: a
        # token that left by ranking sixth and a token that left by falling under a
        # floor are two different events, and the bar used to call both 跌出. Kept local
        # rather than written onto `item.rank`, which is the card's own number and only
        # ever set for the tokens that got one.
        pool_rank = {item.ca: index for index, item in enumerate(ranked, start=1)}
        # The deferred read, spent on the picks alone - see `DEFERRED_ENRICH`. It runs
        # after the score on purpose: nothing it fills is scored, and the card is the only
        # reader that wants it.
        cancel.raise_if(run_id)
        self._enrich(picked, universe, only=DEFERRED_ENRICH)
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

    # ------------------------------------------------------------------- lexicon

    def _stale(self, gated: list[Candidate], cfg: dict) -> list[dict]:
        """The entries this round has to rewrite, each carrying the reason it is due.

        The reason travels with the target because it lands in `lexicon.refresh_reason`:
        a row that says why it changed is worth more than one that only says when.
        """
        out = []
        for item in gated:
            entry = self.lexicon(item.ca)
            if not entry:
                continue
            # `item.facts` goes with the numbers: an entry whose words were written while
            # the LP question was open is due as soon as the answer lands, whatever the
            # price did in the meantime.
            verdict = lexicon.due(entry, self._entry_metrics(item), cfg.get("lexicon"),
                                  safety=item.facts)
            if verdict.action != "rewrite":
                continue
            out.append({"ca": item.ca, "symbol": item.symbol, "metrics": item.metrics,
                        "facts": item.facts, "reason": verdict.reason})
        return out

    @staticmethod
    def _entry_metrics(item: Candidate) -> dict:
        """The two readings the refresh conditions measure: market cap and holders."""
        return {"market_cap_usd": item.metrics.market_cap_usd, "holders": item.metrics.holders}

    def _entry_note(self, item: Candidate, cfg: dict) -> str:
        """What the card says when it shows words this round did not write.

        Two cases, and they are not the same sentence: a rewrite that was skipped on purpose
        (the entry is confirmed, and a human owns those words) and one that was attempted and
        failed. Saying "未重写" without saying which would leave the reader unable to tell a
        model outage from a rule.
        """
        entry = self.lexicon(item.ca) or {}
        verdict = lexicon.due(entry, self._entry_metrics(item), cfg.get("lexicon"),
                              safety=item.facts)
        age = lexicon.age_text(verdict.age_hours)
        if verdict.action != "rewrite":
            return "解读写于 %s · 本轮数据已更新" % age
        if str(entry.get("review_state")) == "confirmed":
            return "解读写于 %s · 人工已确认，未自动改写" % age
        return "解读写于 %s · 本轮重写未完成，仍用旧档" % age

    # -------------------------------------------------------------------- stages

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
        prefilter_cfg = cfg.get("prefilter") or {}
        limit = int(cfg.get("gate_rounds") or GATE_ROUNDS)
        cap = cfg.get("gate_limit")
        cap = GATE_LIMIT if cap is None else int(cap)
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
            # The gate is three quarters of a round - twenty reads of twenty tokens - so it is
            # the phase a stop is most likely to land in, and the candidate is the finest
            # boundary the round already has. One `raise_if` per candidate keeps the answer
            # honest to within one token's network wait.
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
            self._enrich(chunk, universe, judged, defer=DEFERRED_ENRICH)
            for item in chunk:
                cancel.raise_if(run_id)
                # 距高回撤 is the one gate rule with a second half to read, and that half is
                # the expensive one: ask the pool side about the flagged tokens alone,
                # before the verdict is read. Everyone else is judged on the free half.
                peak = self.peaks.get(item.ca)
                if filters.drawdown_needs_pool(item.metrics, prefilter_cfg, peak):
                    self._enrich([item], universe, None, only=DEFERRED_ENRICH)
                verdict = filters.check_all(item.metrics, item.facts, cfg,
                                            peak_market_cap_usd=peak)
                item.failed = [reason.value for reason in verdict.failed]
                item.unknown = [reason.value for reason in verdict.unknown]
                if verdict.passed:
                    gated.append(item)
                else:
                    item.decision = "dropped"
            checked += len(chunk)
            rounds += 1
        return gated

    def _enrich(self, shortlist: list[Candidate], universe: collector.Universe,
                done: Callable[[], None] | None = None, *,
                defer: tuple[str, ...] = (), only: tuple[str, ...] = ()) -> None:
        """Ask every source about every shortlisted token, then re-merge the token's view.

        Per token the sources are asked at the same time and each host paces itself, so five
        network calls overlap without any one host seeing a burst. Asking them one after
        another instead made a candidate cost the *sum* of every source's latency - its own
        reads are independent of each other, and only the merge needs them all.

        `defer` names the sources this pass leaves out and `only` the ones a later pass
        asks about on its own - the two halves of `DEFERRED_ENRICH`. `extra_sources` are
        asked here and nowhere else: they have no bulk row to sharpen, so the pull cannot
        have asked them, and a pass that names an `only` is asking for its own short list.
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
                try:
                    return source.name, source.fetch_token(self.chain["id"], item.ca)
                except Exception:
                    return source.name, None

            views: dict[str, TokenMetrics] = {}
            facts = SafetyFacts()
            for name, answer in pmap(ask, asked, workers=len(asked) or 1):
                if answer is None:
                    continue
                views[name], safety = answer
                facts = _absorb(facts, safety)
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

        pmap(one, shortlist, workers=ENRICH_WORKERS)

    def _lock_facts(self, item: Candidate) -> None:
        """The LP question, asked of the chain rather than of a vendor."""
        creator = item.facts.creator or item.metrics.creator or ""
        try:
            locked, basis, evidence = self.reader.lock_facts(
                self.chain,
                ca=item.ca,
                pair=item.metrics.dex_pair,
                creator=creator,
                launchpad=item.metrics.launchpad,
            )
        except Exception as exc:
            locked, basis, evidence = None, "链上查询失败：%r" % (exc,), {}
        if locked is None:
            # The chain read cannot see a v4 position - the pool has no contract to ask -
            # and GoPlus answers the same question from the LP holder table. Two states, so
            # a token the chain could not judge either gets a verdict from the holder table
            # or stays 未判定 because that table covered too little of the LP to say.
            locked, sourced = onchain.lp_lock_from_holders(item.metrics.lp_holders)
            if locked is not None:
                basis = sourced
        item.facts.lp_locked = locked
        item.facts.lp_note = basis
        item.evidence = dict(evidence or {})
        item.evidence["creator"] = creator

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
        before = next((row for row in self.previous if str(row.get("ca") or "").lower() == item.ca), None)
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
        before = {str(row.get("ca") or "").lower(): row for row in self.previous}
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
            return _rule_note(reason, item.metrics, prefilter, safety) if reason else "本期被硬过滤淘汰"
        rank = pool_rank.get(ca)
        if rank is not None:
            return "本期综合分排名第 %d，入选只取前 %d" % (rank, per_round)
        if not item.enriched:
            # 候选, but the gate's budget ran out before anyone asked about it. A silence,
            # and it says so rather than borrowing a verdict it never got.
            return "本轮未判定（按 24h 成交额排序，判定名额之外）"
        return "本期无综合分"

def _absorb(base: SafetyFacts, extra: SafetyFacts) -> SafetyFacts:
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


def _rule_note(reason: str, metrics: TokenMetrics, prefilter: dict, safety: dict) -> str:
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
        return _clause("距高回撤", metrics.drawdown_pct, prefilter.get("drawdown_max_pct"),
                       formatter=_pct, wording="超过")
    if reason == "missing_data":
        return "五个评分因子全无数据"
    if reason == "source_unverified":
        return "源码未验证"
    if reason == "lp_unlocked":
        return "LP 未锁定"
    if reason == "dangerous_perm":
        return "合约含危险权限"
    return funnel.RULE_LABEL.get(reason, reason)
