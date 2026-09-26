"""The round, end to end, against fake sources.

The order is the thing worth testing, and the order must not be a special case for tests:
every assertion here is about which token was asked about, when, and what came out.
"""

from app.core import filters, funnel
from app.core.models import SafetyFacts, TokenMetrics
from app.pipeline.decide import Engine, _rule_note

CHAIN = {"id": "arc", "label": "Arc", "pipeline": {"scan_limit": 5000}, "sources": []}


def _settings(**over):
    settings = {
        "schedule": {},
        "scoring": {"onchain_weight": 55, "product_weight": 45},
        "report": {"per_round": 2},
        "pipeline": {
            "enrich_limit": 3,
            "prefilter": {"market_cap_min_usd": 50_000, "market_cap_max_usd": 200_000,
                          "liquidity_min_usd": 8_000, "holders_min": 150},
            "safety": {},
        },
    }
    settings.update(over)
    return settings


def _row(ca, **kw):
    base = dict(ca=ca, symbol=ca[-4:].upper(), market_cap_usd=100_000, liquidity_usd=30_000,
                volume_24h_usd=10_000, holders=500, top10_pct=10.0, bundling_pct=1.0)
    base.update(kw)
    return TokenMetrics(**base)


class FakeSource:
    chains = ("arc",)

    def __init__(self, name, rows, facts=None, boom=False, per_token=None):
        self.name = name
        self.rows = rows
        self.facts = facts or {}
        self.boom = boom
        self.per_token = per_token or {}
        self.asked = []

    def fetch_universe(self, chain_id, limit):
        if self.boom:
            raise RuntimeError("source is down")
        return list(self.rows)

    def fetch_token(self, chain_id, ca):
        self.asked.append(ca)
        return self.per_token.get(ca) or _row(ca), self.facts.get(ca, SafetyFacts())


class FakeReader:
    def __init__(self, locked=True, unlocked=()):
        self.locked = locked
        self.unlocked = set(unlocked)
        self.asked = []

    def lock_facts(self, chain, *, ca, pair, creator, launchpad):
        self.asked.append(ca)
        return (False if ca in self.unlocked else self.locked), "test basis", {}


def _engine(sources, **kw):
    reader = kw.pop("reader", FakeReader())
    return Engine(CHAIN, sources=sources, reader=reader, **kw), reader


def test_only_the_pool_rules_decide_who_gets_asked_about():
    lake = [_row("0xsmall", market_cap_usd=10_000), _row("0xok"), _row("0xlarge", market_cap_usd=900_000)]
    source = FakeSource("gmgn", lake)
    engine, _reader = _engine([source])
    engine.run(_settings(), "run-1")
    assert source.asked == ["0xok"]


def test_a_batch_is_the_biggest_movers_cut_to_the_limit():
    """The expensive per-token question is asked a batch at a time, biggest mover first."""
    rows = [_row("0x%02d" % i, volume_24h_usd=1000 * i) for i in range(1, 8)]
    source = FakeSource("gmgn", rows)
    engine, _reader = _engine([source])
    engine.run(_settings(), "run-1")
    assert source.asked[:3] == ["0x07", "0x06", "0x05"]


def test_an_uncapped_pool_is_judged_even_once_enough_have_qualified():
    """`gate_limit: 0` is the old behaviour, kept for rounds that would rather be complete
    than quick: the first batch already clears four against a `per_round` of 2, and the pool
    still gets judged to the end."""
    rows = [_row("0x%02d" % i, volume_24h_usd=1000 * i) for i in range(1, 8)]
    source = FakeSource("gmgn", rows)
    engine, _reader = _engine([source])
    result = engine.run(_settings(pipeline=_pipeline(gate_limit=0)), "run-1")
    assert source.asked == ["0x07", "0x06", "0x05", "0x04", "0x03", "0x02", "0x01"]
    assert result.qualified == 7


def test_an_uncapped_gate_that_rejects_everything_still_judges_the_whole_pool():
    """With `gate_limit: 0` every survivor ends up either 候选 or 淘汰 - there is no budget
    to strand the rest against."""
    rows = [_row("0x%02d" % i, volume_24h_usd=1000 * i) for i in range(1, 13)]
    source = FakeSource("gmgn", rows)
    engine, _reader = _engine([source], reader=FakeReader(locked=False))
    result = engine.run(_settings(pipeline=_pipeline(gate_limit=0)), "run-1")
    assert len(source.asked) == 12
    assert result.qualified == 0
    assert result.picked == []


def test_a_round_can_be_told_to_stop_after_a_few_batches():
    """`pipeline.gate_rounds` is the ceiling for a round that would rather be quick."""
    rows = [_row("0x%02d" % i, volume_24h_usd=1000 * i) for i in range(1, 13)]
    source = FakeSource("gmgn", rows)
    engine, _reader = _engine([source], reader=FakeReader(locked=False))
    pipeline = dict(_settings()["pipeline"], gate_rounds=2)
    engine.run(_settings(pipeline=pipeline), "run-1")
    assert len(source.asked) == 6


def test_the_candidate_count_is_the_pool_the_gate_drew_from():
    """候选 is the pool, not the part of it that came back with a number.

    `qualified` is the reference's own `cands = _safe`, which is the right number for "has
    the gate bought enough cards yet" - but it under-reports the pool by every survivor a
    cap left unasked. Reported as 候选, it put 9 on the strip against a pool of 60 and was
    the gap that stopped 淘汰 + 候选 + 入选 from reaching 扫描.
    """
    rows = [_row("0x%02d" % i, volume_24h_usd=1000 * i) for i in range(1, 8)]
    source = FakeSource("gmgn", rows)
    engine, _reader = _engine([source], reader=FakeReader(unlocked={"0x07"}))
    result = engine.run(_settings(), "run-1")
    assert result.universe == 7
    assert len(result.survivors) == 7
    assert result.prefilter_dropped == 0
    assert result.qualified == 6               # judged, and the gate let them through
    assert result.unjudged == 0                # nothing here was left without an answer
    # 0x07 is the safety gate's to claim; every other survivor is 候选, picked or not.
    assert result.funnel_summary.candidates == 7 - 1


def test_the_strip_adds_up_to_the_scan_even_when_the_cap_strands_survivors():
    """扫描 = 淘汰 + 候选 + 入选, whatever a cap did to the pool in between.

    Thirty survivors against a cap of twenty: ten were never asked about. Nothing rejected
    them, so they are 候选 - and if they are not, they are tokens the strip counts nowhere
    and the sum reaches less than the scan while every row on the page is accounted for.
    """
    source = FakeSource("gmgn", _movers(30))
    engine, _reader = _engine([source])
    result = engine.run(_settings(), "run-1")
    assert result.unjudged == 10               # twenty judged, ten left without a source

    summary = result.funnel_summary
    assert summary.scanned == 30
    assert summary.dropped + summary.candidates == summary.scanned
    assert summary.candidates == 30          # 入选 are inside 候选, not beside them


def test_the_two_淘汰_rows_on_the_panel_are_a_split_of_the_strip_one():
    """The panel says 淘汰 twice - 硬过滤 and 安全门槛 - and the strip says it once.

    `dropped` is the total; `prefilter_dropped` is the hard filter's share of it. Both
    rows using the total would count the safety gate's tokens twice, which is the other
    way to make a funnel that does not add up.
    """
    source = FakeSource("gmgn", _movers(30))
    # The unlocked two have to be inside the cap's twenty, or they are never judged and
    # never dropped: the gate reads the pool biggest-volume first.
    engine, _reader = _engine([source], reader=FakeReader(unlocked={"0x30", "0x29"}))
    result = engine.run(_settings(), "run-1")

    pool = {reason.value for reason in filters.PREFILTER_PRIORITY}
    gate = {reason.value for reason in filters.SAFETY_PRIORITY} | {"entrapment"}
    dropped = [item for item in result.items.values() if item.decision == "dropped"]
    credited = [funnel.first_reason({"failed": item.failed}) for item in dropped]

    # Two rows on the panel, one number on the strip: nothing may land outside them.
    assert credited and all(reason in pool | gate for reason in credited), credited
    assert sum(1 for reason in credited if reason in pool) == result.prefilter_dropped
    assert result.funnel_summary.dropped == len(dropped)
    assert result.funnel_summary.dropped + result.funnel_summary.candidates \
        == result.funnel_summary.scanned


def test_the_line_is_asked_only_after_the_shortlist_is_chosen():
    """The expensive per-token question follows the free one. A round that asked about the
    whole universe would be a different (and much slower) pipeline."""
    rows = [_row("0x%02d" % i, volume_24h_usd=1000 * i) for i in range(1, 8)]
    reader = FakeReader()
    engine, _reader = _engine([FakeSource("gmgn", rows)], reader=reader)
    engine.run(_settings(), "run-1")
    assert reader.asked == ["0x07", "0x06", "0x05", "0x04", "0x03", "0x02", "0x01"]


def test_a_token_with_no_entry_cannot_rank_and_says_so():
    source = FakeSource("gmgn", [_row("0x01")])
    engine, _reader = _engine([source])
    result = engine.run(_settings(), "run-1")
    assert result.picked == []
    assert any("未建档" in note for note in result.notes)


def test_the_picks_are_the_top_of_the_scored_set_in_order():
    rows = [_row("0x%02d" % i, volume_24h_usd=1000 * i) for i in range(1, 4)]
    source = FakeSource("gmgn", rows)
    engine, _reader = _engine([source], lexicon=lambda ca: {"narrative": 3.0, "product": 3.0})
    result = engine.run(_settings(), "run-1")
    assert [item.rank for item in result.picked] == [1, 2]
    assert result.picked[0].score >= result.picked[1].score


def test_a_pool_rule_that_only_bites_after_enrichment_is_still_a_pool_drop():
    """The settings page credits a drop to the first rule that rejected it, and the 硬过滤
    row reads that same credit. A token the bulk view liked and the token's own numbers did
    not is a pool-rule drop, not a safety-gate one - otherwise the panel's columns would
    not add up to the scan."""
    rows = [_row("0x01", volume_24h_usd=2_000), _row("0x02", volume_24h_usd=1_000)]
    source = FakeSource("gmgn", rows, per_token={"0x02": _row("0x02", liquidity_usd=100)})
    engine, _reader = _engine([source])
    result = engine.run(_settings(), "run-1")
    assert len(result.survivors) == 2          # the bulk view let both through
    assert result.qualified == 1               # only 0x01 was left to score
    assert result.prefilter_dropped == 1       # and 0x02 is the pool rules' to claim


def test_the_percentile_denominator_is_the_survivors_not_the_gated_pool():
    """口径 #1: a token is ranked inside the round's prefilter survivors. A peer the safety
    gate threw out still counts in the denominator - otherwise the same token would read
    as the best of one and take the whole 20% factor weight instead of half of it."""
    rows = [_row("0x01", liquidity_usd=10_000, volume_24h_usd=2_000),
            _row("0x02", liquidity_usd=30_000, volume_24h_usd=1_000)]
    per_token = {"0x01": _row("0x01", liquidity_usd=10_000),
                 "0x02": _row("0x02", liquidity_usd=30_000)}
    source = FakeSource("gmgn", rows, per_token=per_token)
    engine, _reader = _engine([source], reader=FakeReader(unlocked={"0x02"}))
    result = engine.run(_settings(), "run-1")
    assert [item.ca for item in result.scored] == ["0x01"]
    factors = {f.factor: f.score for f in result.scored[0].breakdown.factors}
    # 0x01 is the smaller of two survivors: half of the 20% liquidity weight, not all of it.
    assert factors["liquidity"] == 5.0


def test_every_candidate_carries_its_reason_or_its_score():
    """Nothing is left in the middle: a token is dropped with a reason, or it has a number."""
    lake = [_row("0xsmall", market_cap_usd=10_000), _row("0x01"), _row("0x02", market_cap_usd=900_000)]
    engine, _reader = _engine([FakeSource("gmgn", lake)], lexicon=lambda ca: {"narrative": 3.0, "product": 3.0})
    result = engine.run(_settings(), "run-1")
    for item in result.items.values():
        if item.decision == "dropped":
            assert item.failed, item.ca
        else:
            assert item.score is not None, item.ca


def test_a_dead_source_is_a_note_not_a_crash():
    rows = [_row("0x01")]
    engine, _reader = _engine([FakeSource("gmgn", rows), FakeSource("radardex", [], boom=True)])
    result = engine.run(_settings(), "run-1")
    assert any("数据源未答复" in note for note in result.notes)


def test_the_stages_are_reported_in_order():
    seen = []
    engine, _reader = _engine([FakeSource("gmgn", [_row("0x01")])])
    engine.run(_settings(), "run-1", on_stage=seen.append)
    assert seen == ["collected", "scored", "selected"]


def test_a_gate_failure_is_a_drop_with_a_reason():
    """The gate runs after enrichment, not with the pool rules: it needs a fact the bulk
    pull does not carry. A dropped token is not offered to the ranker."""
    source = FakeSource(
        "gmgn",
        [_row("0x01"), _row("0x02")],
        facts={"0x01": SafetyFacts(source_verified=False)},
    )
    engine, _reader = _engine([source], lexicon=lambda ca: {"narrative": 3.0, "product": 3.0})
    settings = _settings()
    settings["pipeline"]["safety"] = {"require_verified_source": True}
    result = engine.run(settings, "run-1")
    dropped = [item for item in result.items.values() if item.ca == "0x01"][0]
    assert dropped.decision == "dropped"
    assert "source_unverified" in dropped.failed
    assert [item.ca for item in result.picked] == ["0x02"]


def _pipeline(**over):
    pipeline = dict(_settings()["pipeline"])
    pipeline.update(over)
    return pipeline


def _movers(n: int):
    return [_row("0x%02d" % i, volume_24h_usd=1000 * i) for i in range(1, n + 1)]


def test_the_gate_spends_its_budget_on_the_biggest_movers_and_stops():
    """甲: twenty candidates by 24h volume, then stop. This is the reference's own shape -
    judging the whole pool is what made a round take twenty minutes, because per-token
    reads are paced per host and that pace is a serial cost more workers cannot shorten."""
    source = FakeSource("gmgn", _movers(30))
    engine, _reader = _engine([source])
    result = engine.run(_settings(), "run-1")

    assert len(source.asked) == 20
    assert source.asked[:3] == ["0x30", "0x29", "0x28"]
    assert result.qualified == 20


def test_the_rest_of_the_pool_is_left_unjudged_and_the_round_says_so():
    """A survivor nobody judged is 候选 - nothing rejected it - but it has no number, so
    the round says so out loud rather than let 候选 imply every one of them was checked."""
    source = FakeSource("gmgn", _movers(30))
    engine, _reader = _engine([source])
    result = engine.run(_settings(), "run-1")

    assert any("未判定" in note and "10" in note for note in result.notes), result.notes
    assert not any(item.enriched for item in result.survivors[20:])


def test_a_short_report_beats_padding_it_with_rows_nobody_judged():
    """宁缺勿滥: `per_round` is 4 here and the budget is one candidate, so the round ships
    one card. It used to keep reading until it held four, and that fallback is what walked
    into the tail of the volume sort - which is where a thin pull leaves its empty rows, and
    how four thousand of them reached the lexicon. The budget also cuts the batch it is
    inside: one means one read, not the three-candidate batch that read sits in."""
    source = FakeSource("gmgn", _movers(30))
    engine, _reader = _engine([source])
    settings = _settings(report={"per_round": 4}, pipeline=_pipeline(gate_limit=1))
    result = engine.run(settings, "run-1")

    assert len(source.asked) == 1
    assert result.qualified == 1


def test_a_round_that_qualified_nothing_is_the_one_case_that_keeps_looking():
    """宁缺勿滥 has one exception, and this is it: with a cap that has qualified nothing at
    all the round keeps looking, because zero cards is not a short report, it is a reading of
    the pool. Every other short round publishes what it has."""
    source = FakeSource("gmgn", _movers(30))
    engine, _reader = _engine([source], reader=FakeReader(locked=False))
    result = engine.run(_settings(pipeline=_pipeline(gate_limit=5)), "run-1")

    assert len(source.asked) == 30
    assert result.qualified == 0


def test_a_zero_budget_means_the_whole_pool():
    """`gate_limit: 0` is the switch back to judging everyone - the setting the operator
    moves when they want completeness instead of speed."""
    source = FakeSource("gmgn", _movers(30))
    engine, _reader = _engine([source])
    engine.run(_settings(pipeline=_pipeline(gate_limit=0)), "run-1")

    assert len(source.asked) == 30


def test_a_token_that_left_is_named_not_shown_as_an_address():
    """The change bar read `退出 0x0bb3befb`: `_changes` asked a previous pick for `sym`
    while `previous_picks` writes `symbol`, so the lookup always missed and the address
    fallback always fired. The name was in the database the whole time."""
    source = FakeSource("gmgn", [_row("0xnew", volume_24h_usd=5_000)])
    previous = [{"ca": "0xold", "symbol": "MEMESPAD", "market_cap_usd": 120_000.0}]
    engine, _reader = _engine([source], previous=previous)

    result = engine.run(_settings(), "run-1")

    out = [change for change in result.changes if change["direction"] == "out"]
    assert len(out) == 1
    assert out[0]["sym"] == "MEMESPAD"
    assert "MEMESPAD" in out[0]["detail"]


def test_an_exit_says_which_filter_it_missed():
    """The chip read 跌出硬过滤或排名 without saying which, and a reader cannot act on
    that. The round still holds the verdict on the token it dropped, so it prints it -
    the rule, the number that missed, and the bound it missed."""
    source = FakeSource("gmgn", [
        _row("0xnew", volume_24h_usd=5_000),
        _row("0xold", liquidity_usd=7_000),
    ])
    previous = [{"ca": "0xold", "symbol": "OLD", "market_cap_usd": 90_000.0}]
    engine, _reader = _engine([source], previous=previous)

    result = engine.run(_settings(), "run-1")

    out = [change for change in result.changes if change["direction"] == "out"]
    assert len(out) == 1
    assert "流动性 $7.0K 低于下限 $8.0K" in out[0]["detail"]


def test_an_exit_that_lost_on_rank_says_the_place_it_took():
    """per_round is 2 here, so the token that came third is the one that left - and
    third is a different event from a threshold miss, so the bar says which."""
    source = FakeSource("gmgn", [
        _row("0xbest", volume_24h_usd=30_000, liquidity_usd=90_000, holders=900, top10_pct=5.0),
        _row("0xmid", volume_24h_usd=20_000, liquidity_usd=60_000, holders=600, top10_pct=8.0),
        _row("0xlast", volume_24h_usd=10_000, holders=200, top10_pct=20.0),
    ])
    previous = [{"ca": "0xlast", "symbol": "LAST", "market_cap_usd": 90_000.0}]
    # A score needs a lexicon entry (45% of the total), so the ranking exists at all.
    engine, _reader = _engine([source], previous=previous,
                              lexicon=lambda _ca: {"narrative": 3.0, "product": 3.0})

    result = engine.run(_settings(), "run-1")

    out = [change for change in result.changes if change["direction"] == "out"]
    assert len(out) == 1
    assert "排名第 3，入选只取前 2" in out[0]["detail"]


def test_an_exit_nobody_judged_says_so_instead_of_borrowing_a_verdict():
    """The gate spends a budget on the biggest movers; a token the budget never
    reached was neither passed nor dropped, and the bar must not invent either."""
    source = FakeSource("gmgn", [
        _row("0xloud", volume_24h_usd=20_000),
        _row("0xquiet", volume_24h_usd=5_000),
    ])
    previous = [{"ca": "0xquiet", "symbol": "QUIET", "market_cap_usd": 90_000.0}]
    engine, _reader = _engine([source], previous=previous)

    result = engine.run(_settings(pipeline=_pipeline(gate_limit=1, enrich_limit=1)), "run-1")

    out = [change for change in result.changes if change["direction"] == "out"]
    assert len(out) == 1
    assert "未判定" in out[0]["detail"]


def test_an_exit_no_source_pulled_says_that_too():
    previous = [{"ca": "0xgone", "symbol": "GONE", "market_cap_usd": 90_000.0}]
    engine, _reader = _engine([FakeSource("gmgn", [_row("0xnew", volume_24h_usd=5_000)])],
                              previous=previous)

    result = engine.run(_settings(), "run-1")

    out = [change for change in result.changes if change["direction"] == "out"]
    assert "本轮未被任何数据源拉到" in out[0]["detail"]


def test_every_rule_has_a_sentence_of_its_own_on_the_change_bar():
    """A rule that falls through to its plain label tells the reader nothing they did
    not already know, so every rule the funnel can credit is walked here: the note has
    to be more than the words the settings row is drawn under."""
    metrics = TokenMetrics(ca="0xabc", market_cap_usd=1_000, liquidity_usd=1,
                           volume_24h_usd=1, holders=1, top10_pct=99.0, bundling_pct=99.0,
                           honeypot_pct=99.0, drawdown_pct=-99.0)
    prefilter = {"market_cap_min_usd": 10_000, "market_cap_max_usd": 200_000,
                 "liquidity_min_usd": 8_000, "volume_24h_min_usd": 1_000,
                 "holders_min": 100, "holders_max": 2_000, "top10_max_pct": 40,
                 "bundling_max_pct": 25, "drawdown_max_pct": -70}
    safety = {"honeypot_max_pct": 40}
    for rule in funnel.DROP_PRIORITY:
        note = _rule_note(rule, metrics, prefilter, safety)
        assert note != funnel.RULE_LABEL[rule], rule
        assert note

def test_a_previous_pick_with_no_name_still_falls_back_to_the_address():
    """The address is a last resort, not the plan: a pick the roster never named has
    nothing else to show, and a wrong-but-readable name would be worse."""
    source = FakeSource("gmgn", [_row("0xnew", volume_24h_usd=5_000)])
    previous = [{"ca": "0x" + "b" * 40, "symbol": None, "market_cap_usd": 1.0}]
    engine, _reader = _engine([source], previous=previous)

    result = engine.run(_settings(), "run-1")

    out = [change for change in result.changes if change["direction"] == "out"]
    assert out[0]["sym"] == "0x" + "b" * 8


def test_the_deferred_source_is_asked_about_the_picks_and_nothing_else():
    """GeckoTerminal charges three paced requests per token and nothing it fills is scored
    or gated, so it is asked after the picks are known - about the picks alone. A gate
    candidate that was not picked must not have cost one."""
    lake = [_row("0x%02d" % i, volume_24h_usd=1000 * i) for i in range(1, 6)]
    sharpened = {
        row.ca: _row(row.ca, holders=777, volume_24h_usd=row.volume_24h_usd)
        for row in lake
    }
    gmgn = FakeSource("gmgn", lake, per_token=sharpened)
    gecko = FakeSource(
        "geckoterminal",
        [_row(row.ca) for row in lake],
        per_token={
            row.ca: TokenMetrics(ca=row.ca, pools=7, price_series=[1.0, 2.0, 3.0])
            for row in lake
        },
    )
    engine, reader = _engine([gmgn, gecko],
                             lexicon=lambda ca: {"narrative": 3.0, "product": 3.0})
    result = engine.run(_settings(), "run-1")       # per_round 2, all five judged

    assert [item.ca for item in result.picked] == ["0x05", "0x04"]
    assert sorted(gecko.asked) == ["0x04", "0x05"], "only the picks are worth three paced reads"
    assert len(gmgn.asked) == 5, "the gate still judges the whole candidate pool"
    # The second pass merges into the first pass's views: a per-token answer must not be
    # replaced by the stale bulk row it was meant to sharpen.
    assert [item.metrics.holders for item in result.picked] == [777, 777]
    # ...and it must not touch the safety verdict the first pass reached either.
    assert all(item.facts.lp_locked is True for item in result.picked)
    assert len(reader.asked) == len(set(reader.asked)) == 5, "the LP read is not repeated"
    # What the deferred source fills lands on the cards; the unmatched gate candidates
    # keep the bulk row's blank rather than a number nobody paid for.
    assert all(item.metrics.pools == 7 for item in result.picked)
    judged = [item for item in result.scored if item.ca not in {i.ca for i in result.picked}]
    assert len(judged) == 3
    assert all(item.metrics.pools is None and not item.metrics.price_series for item in judged)


def test_a_caller_supplied_clock_is_filled_in_and_printed_by_its_owner(caplog):
    """The commentary and the write happen after the engine returns, so the caller hands
    its stopwatch in. The engine fills it, the owner prints it - one line per round."""
    from app.pipeline import progress

    clock = progress.Timer("run-1")
    engine, _reader = _engine([FakeSource("gmgn", [_row("0x01")])])
    with caplog.at_level("INFO", logger="crypto-hunter.pipeline"):
        engine.run(_settings(), "run-1", clock=clock)
    assert [name for name, _secs in clock.spans][-1] == "sharpen"
    assert not [r for r in caplog.records if "phase timings" in r.getMessage()]

    caplog.clear()
    with caplog.at_level("INFO", logger="crypto-hunter.pipeline"):
        engine.run(_settings(), "run-2")
    lines = [r.getMessage() for r in caplog.records if "phase timings" in r.getMessage()]
    assert len(lines) == 1 and "sharpen" in lines[0]


def test_the_enrich_only_source_is_asked_per_token_and_never_in_the_pull():
    """`registry.ENRICH_ONLY` has no chain-wide list to sweep, which is why it is absent from
    `sources:` in the chain config: a source that answers nothing in the pull is a source that
    shows up in the credential row with a zero next to it. It is asked in the per-token pass,
    and that pass runs before the lexicon drafts - so a token’s own links are in front of the
    writer, not just on the card that is drawn afterwards."""
    lake = [_row("0x%02d" % i, volume_24h_usd=1000 * i) for i in range(1, 6)]
    gmgn = FakeSource("gmgn", lake)
    links = FakeSource("dexscreener", [], per_token={
        row.ca: TokenMetrics(ca=row.ca, socials={"twitter": "https://x.com/%s" % row.ca})
        for row in lake
    })
    engine, _reader = _engine([gmgn], extras=(links,),
                              lexicon=lambda ca: {"narrative": 3.0, "product": 3.0})
    result = engine.run(_settings(), "run-1")       # per_round 2, all five judged

    assert [row.source for row in result.reports] == ["gmgn"], "the pull never swept it"
    assert len(links.asked) == 5, "every judged token is asked, not only the picks"
    assert all(item.metrics.socials for item in result.scored), "the sheet the writer reads"
    assert result.picked[0].metrics.socials["twitter"].startswith("https://x.com/")


def test_a_caller_that_brings_its_own_sources_brings_its_own_extras():
    """Every test in this file hands the engine fake sources, and none of them should reach
    the network because the registry has an extra in it."""
    engine, _reader = _engine([FakeSource("gmgn", [_row("0xok")])])
    assert engine.extra_sources == ()


def test_the_drawdown_rule_buys_the_pool_read_only_for_the_tokens_it_flags():
    """距高回撤, in one round. The free half (the highest market cap this project has seen)
    is read for every judged token; the paid half (the pool ten-day high) only for the
    0xflg candidate our own history put 75% below its high. 0xcalm is never flagged, so the
    only reason it appears at all is the deferred pass that fills the picks afterwards."""
    prefilter = dict(_settings()["pipeline"]["prefilter"], drawdown_max_pct=-70)
    bulk = FakeSource("gmgn", [_row("0xflg", volume_24h_usd=20_000),
                               _row("0xcalm", volume_24h_usd=10_000)])
    pool = FakeSource("geckoterminal", [], per_token={
        "0xflg": _row("0xflg", volume_24h_usd=20_000, drawdown_pct=-80.0),
    })
    engine, _reader = _engine([bulk, pool], peaks={"0xflg": 400_000},
                              lexicon=lambda ca: {"narrative": 3.0, "product": 3.0})
    result = engine.run(_settings(pipeline=_pipeline(prefilter=prefilter)), "run-1")
    # the gate buys the pool read for the flagged token alone - 0xflg never reaches
    # the ranking; 0xcalm is only ever asked by the picks pass afterwards.
    assert pool.asked == ["0xflg", "0xcalm"]
    flagged = result.items["0xflg"]
    assert flagged.decision == "dropped"
    assert flagged.failed == ["market_cap_drawdown"]


def test_a_flagged_token_the_pool_cannot_answer_keeps_its_place():
    """Half a confirmation is not a confirmation: the token is not dropped, and the round
    says 未判定 rather than pretending the second reading agreed."""
    prefilter = dict(_settings()["pipeline"]["prefilter"], drawdown_max_pct=-70)
    bulk = FakeSource("gmgn", [_row("0xflg", volume_24h_usd=20_000)])
    pool = FakeSource("geckoterminal", [])
    engine, _reader = _engine([bulk, pool], peaks={"0xflg": 400_000},
                              lexicon=lambda ca: {"narrative": 3.0, "product": 3.0})
    result = engine.run(_settings(pipeline=_pipeline(prefilter=prefilter)), "run-1")
    flagged = result.items["0xflg"]
    assert flagged.decision == "selected"
    assert flagged.unknown == ["market_cap_drawdown"]
    assert flagged.failed == []
