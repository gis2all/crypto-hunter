"""Filters: the three-state answer (pass / fail / unknown) is the whole point."""

from app.core.filters import (check_all, check_drawdown, check_entrapment, check_prefilter,
                              check_safety, drawdown_needs_pool)
from app.core.models import DropReason, SafetyFacts, TokenMetrics

PRE = {"market_cap_max_usd": 200000, "liquidity_min_usd": 8000, "volume_24h_min_usd": 1000}


def _metrics(**kw) -> TokenMetrics:
    base = dict(ca="0xabc", market_cap_usd=100000, liquidity_usd=50000, volume_24h_usd=20000)
    base.update(kw)
    return TokenMetrics(**base)


def test_prefilter_passes_a_healthy_token():
    verdict = check_prefilter(_metrics(), PRE)
    assert verdict.passed and not verdict.failed and not verdict.unknown


def test_prefilter_drops_on_market_cap():
    verdict = check_prefilter(_metrics(market_cap_usd=250000), PRE)
    assert not verdict.passed
    assert DropReason.MARKET_CAP in verdict.failed


def test_prefilter_drops_above_the_holder_ceiling():
    """持有人 is a band, not a floor: a token over the ceiling is credited to the
    same rule the floor lives under, so the settings page keeps one row for it."""
    verdict = check_prefilter(_metrics(holders=4000), {"holders_min": 100, "holders_max": 2000})
    assert not verdict.passed
    assert DropReason.HOLDERS in verdict.failed


def test_an_unset_holder_ceiling_drops_nobody():
    """A snapshot saved before the ceiling existed has no key for it, and a missing knob
    is a missing rule - not a ceiling of zero, which would empty the pool."""
    verdict = check_prefilter(_metrics(holders=900000), {"holders_min": 100})
    assert verdict.passed
    assert verdict.failed == []


def test_a_holder_that_answered_nothing_is_unknown_on_both_ends():
    verdict = check_prefilter(_metrics(holders=None), {"holders_min": 100, "holders_max": 2000})
    assert verdict.passed
    assert DropReason.HOLDERS in verdict.unknown


def test_prefilter_reports_missing_data_as_unknown_not_ok():
    verdict = check_prefilter(_metrics(liquidity_usd=None), PRE)
    assert verdict.passed
    assert verdict.unknown == [DropReason.LIQUIDITY]
    assert verdict.failed == []


def test_safety_unverified_source_fails():
    verdict = check_safety(SafetyFacts(source_verified=False), {"require_verified_source": True})
    assert not verdict.passed and DropReason.SOURCE_UNVERIFIED in verdict.failed


def test_safety_unknown_source_is_unknown():
    verdict = check_safety(SafetyFacts(source_verified=None), {"require_verified_source": True})
    assert verdict.passed
    assert DropReason.SOURCE_UNVERIFIED in verdict.unknown
    assert verdict.failed == []


def test_safety_dangerous_permission_fails():
    facts = SafetyFacts(source_verified=True, dangerous_perms=["mintable", "pausable"])
    verdict = check_safety(facts, {"forbid_dangerous_perms": ["mintable", "blacklist", "pausable", "hidden_owner"]})
    assert not verdict.passed and DropReason.DANGEROUS_PERM in verdict.failed


def test_safety_unknown_permissions_are_unknown():
    facts = SafetyFacts(source_verified=True, dangerous_perms=None)
    verdict = check_safety(facts, {"forbid_dangerous_perms": ["mintable"]})
    assert verdict.passed
    assert DropReason.DANGEROUS_PERM in verdict.unknown
    assert verdict.failed == []


def test_lp_unjudged_passes_only_when_configured():
    strict = check_safety(SafetyFacts(lp_locked=None), {})
    assert strict.unknown == [DropReason.LP_UNLOCKED] and strict.passed
    unlocked = check_safety(SafetyFacts(lp_locked=False), {"lp_unjudged_passes": True})
    assert not unlocked.passed and DropReason.LP_UNLOCKED in unlocked.failed


def _votes(verified_by, checked_by):
    return SafetyFacts(
        source_verified_by=list(verified_by), source_checked_by=list(checked_by),
        source_verified=bool(verified_by),
    )


def test_dual_takes_either_verifier():
    """The reference's rule is `GoPlus.is_open_source == '1' or Sourcify has a match`, so
    one verifier vouching is enough - a verifier with no record cannot veto it."""
    facts = _votes(["GoPlus"], ["GoPlus", "Sourcify"])
    verdict = check_safety(facts, {"require_verified_source": True, "source_verification_mode": "dual"})
    assert verdict.passed


def test_dual_is_a_no_when_every_answer_was_no_record():
    facts = _votes([], ["GoPlus", "Sourcify"])
    verdict = check_safety(facts, {"require_verified_source": True, "source_verification_mode": "dual"})
    assert not verdict.passed
    assert DropReason.SOURCE_UNVERIFIED in verdict.failed


def test_goplus_only_ignores_the_second_verifier():
    facts = _votes(["GoPlus"], ["GoPlus", "Sourcify"])
    verdict = check_safety(facts, {"require_verified_source": True, "source_verification_mode": "goplus_only"})
    assert verdict.passed


def test_a_verifier_that_never_answered_is_not_a_no():
    assert check_safety(_votes(["GoPlus"], ["GoPlus"]), {"require_verified_source": True}).passed


def test_goplus_only_is_unknown_when_goplus_did_not_answer():
    facts = _votes(["Sourcify"], ["Sourcify"])
    verdict = check_safety(facts, {"require_verified_source": True, "source_verification_mode": "goplus_only"})
    assert verdict.unknown and not verdict.failed and verdict.passed


def test_entrapment_ceiling():
    assert check_entrapment(_metrics(honeypot_pct=44), {"honeypot_max_pct": 45}).passed
    assert not check_entrapment(_metrics(honeypot_pct=67), {"honeypot_max_pct": 45}).passed
    assert check_entrapment(_metrics(honeypot_pct=None), {"honeypot_max_pct": 45}).unknown


def test_check_all_combines_every_gate():
    cfg = {
        "prefilter": PRE,
        "safety": {"require_verified_source": True, "forbid_dangerous_perms": ["mintable"]},
    }
    facts = SafetyFacts(source_verified=True, dangerous_perms=[], lp_locked=True)
    verdict = check_all(_metrics(market_cap_usd=999999), facts, cfg)
    assert not verdict.passed and DropReason.MARKET_CAP in verdict.failed


def _bare_metrics(**kw) -> TokenMetrics:
    """A token with nothing the five ranking factors can read."""
    base = dict(ca="0xabc", market_cap_usd=100000, liquidity_usd=None, volume_24h_usd=None,
                holders=None, top10_pct=None, bundling_pct=None)
    base.update(kw)
    return TokenMetrics(**base)


def test_zero_data_drops_a_token_no_source_could_rank():
    """零数据淘汰: after every source has answered, a token with none of the five factors
    cannot be scored or ranked, so it is a hard drop rather than a row nobody can read."""
    cfg = {"prefilter": {**PRE, "min_scored_fields": 1}, "safety": {}}
    verdict = check_all(_bare_metrics(), SafetyFacts(), cfg)
    assert not verdict.passed
    assert verdict.failed == [DropReason.MISSING_DATA]


def test_zero_data_leaves_a_token_with_one_readable_factor_alone():
    cfg = {"prefilter": {**PRE, "min_scored_fields": 1}, "safety": {}}
    verdict = check_all(_bare_metrics(liquidity_usd=50000), SafetyFacts(), cfg)
    assert verdict.passed
    assert DropReason.MISSING_DATA not in verdict.failed


def test_zero_data_counts_only_the_ranking_factors():
    """Market cap is a filter input, not a ranking factor: a round cannot rank a pool by
    it, so a token that only has one is still unrankable."""
    cfg = {"prefilter": {**PRE, "min_scored_fields": 1}, "safety": {}}
    verdict = check_all(_bare_metrics(market_cap_usd=90000), SafetyFacts(), cfg)
    assert not verdict.passed
    assert DropReason.MISSING_DATA in verdict.failed


def test_zero_data_rule_is_off_until_its_key_is_set():
    """A config written before this rule existed must not start dropping rows on upgrade."""
    cfg = {"prefilter": dict(PRE), "safety": {}}
    assert check_all(_bare_metrics(), SafetyFacts(), cfg).passed

    cfg = {"prefilter": {**PRE, "min_scored_fields": 0}, "safety": {}}
    assert check_all(_bare_metrics(), SafetyFacts(), cfg).passed


def test_zero_data_threshold_is_a_口径_not_a_flag():
    cfg = {"prefilter": {**PRE, "min_scored_fields": 3}, "safety": {}}
    assert not check_all(_bare_metrics(liquidity_usd=50000, holders=900), SafetyFacts(), cfg).passed
    assert check_all(
        _bare_metrics(liquidity_usd=50000, holders=900, bundling_pct=3.0), SafetyFacts(), cfg,
    ).passed


def test_zero_data_gives_a_real_threshold_the_credit(monkeypatch):
    """A token that misses a threshold and has nothing else is credited to the threshold:
    that is the row the operator can act on."""
    from app.core import funnel

    cfg = {"prefilter": {**PRE, "min_scored_fields": 2}, "safety": {}}
    verdict = check_all(_bare_metrics(liquidity_usd=100), SafetyFacts(), cfg)
    assert set(verdict.failed) == {DropReason.LIQUIDITY, DropReason.MISSING_DATA}
    credited = funnel.first_reason({"failed": [r.value for r in verdict.failed]})
    assert credited == DropReason.LIQUIDITY.value


def test_the_shell_rule_drops_a_row_with_no_market_data_at_all():
    """未知不淘汰 exists for a source that did not answer. A row carrying neither a market
    cap nor a reserve is not an unanswered question, it is a token with nothing to answer
    with - the exact shape a roster-only feed drops into the pull."""
    cfg = {**PRE, "require_market_data": True}
    verdict = check_prefilter(_metrics(market_cap_usd=None, liquidity_usd=None,
                                       volume_24h_usd=None), cfg)
    assert not verdict.passed
    assert DropReason.MISSING_DATA in verdict.failed


def test_one_market_number_is_enough_to_keep_a_row():
    """Either end answering means there is something real to rank, so the shell rule
    must stay quiet."""
    cfg = {**PRE, "require_market_data": True}
    assert check_prefilter(_metrics(market_cap_usd=None, liquidity_usd=50000), cfg).passed
    assert check_prefilter(_metrics(market_cap_usd=100000, liquidity_usd=None), cfg).passed


def test_the_shell_rule_is_on_unless_the_chain_turns_it_off():
    """It used to be opt-in, and the hole was exactly the absence of the key: a round that
    ran against a settings version written before `require_market_data` existed judged four
    thousand empty rows on "every floor answered 未知" and filed the survivors in the
    lexicon. A missing knob is not a missing rule."""
    verdict = check_prefilter(_metrics(market_cap_usd=None, liquidity_usd=None,
                                       volume_24h_usd=None), PRE)
    assert not verdict.passed
    assert DropReason.MISSING_DATA in verdict.failed


def test_the_shell_rule_can_still_be_switched_off_by_the_chain():
    """Turning it off is a real choice for a chain whose feeder answers later than the pull,
    so the flag has to keep working - it just cannot be what makes the rule exist."""
    cfg = {**PRE, "require_market_data": False}
    verdict = check_prefilter(_metrics(market_cap_usd=None, liquidity_usd=None,
                                       volume_24h_usd=None), cfg)
    assert DropReason.MISSING_DATA not in verdict.failed


DD = {"market_cap_max_usd": 200000, "liquidity_min_usd": 8000, "volume_24h_min_usd": 1000,
      "drawdown_max_pct": -70}


def test_drawdown_rule_is_off_until_the_chain_asks_for_it():
    """No knob, no rule - the same contract every other threshold here keeps."""
    verdict = check_drawdown(_metrics(market_cap_usd=1000, drawdown_pct=-95.0), PRE, 1_000_000)
    assert verdict.passed and not verdict.unknown and not verdict.failed


def test_drawdown_drops_only_when_both_readings_agree():
    """Ours says -80% off the high we have seen, the pool says -75% off its ten-day high."""
    verdict = check_drawdown(_metrics(market_cap_usd=20000, drawdown_pct=-75.0), DD, 100000)
    assert not verdict.passed
    assert verdict.failed == [DropReason.MARKET_CAP_DRAWDOWN]


def test_drawdown_is_not_asked_of_the_pool_until_our_own_history_flags_it():
    """The free reading decides whether the expensive one is worth three requests."""
    deep = _metrics(market_cap_usd=20000, drawdown_pct=-95.0)
    shallow = _metrics(market_cap_usd=50000, drawdown_pct=-95.0)
    assert drawdown_needs_pool(deep, DD, 100000) is False  # the pool already answered
    assert drawdown_needs_pool(_metrics(market_cap_usd=20000), DD, 100000) is True
    assert drawdown_needs_pool(shallow, DD, 100000) is False
    assert check_drawdown(shallow, DD, 100000).passed


def test_drawdown_passes_when_the_pool_side_disagrees_with_our_history():
    verdict = check_drawdown(_metrics(market_cap_usd=20000, drawdown_pct=-40.0), DD, 100000)
    assert verdict.passed and not verdict.failed


def test_drawdown_without_a_pool_reading_is_unknown_and_keeps_the_token():
    """Half an answer does not drop anything, and it does not pass silently either."""
    verdict = check_drawdown(_metrics(market_cap_usd=20000), DD, 100000)
    assert verdict.passed
    assert verdict.unknown == [DropReason.MARKET_CAP_DRAWDOWN]
    assert verdict.failed == []


def test_a_token_we_have_never_seen_is_not_a_fallen_token():
    """No history is a first sighting rather than a blank: the round does not file it
    under 未判定, or every new token would pad that number every round."""
    verdict = check_drawdown(_metrics(market_cap_usd=20000), DD, None)
    assert verdict.passed and not verdict.unknown


def test_check_all_reads_the_peak_it_was_handed():
    cfg = {"prefilter": DD, "safety": {}}
    facts = SafetyFacts()
    dropped = check_all(_metrics(market_cap_usd=20000, drawdown_pct=-80.0), facts, cfg,
                        peak_market_cap_usd=100000)
    assert not dropped.passed
    assert DropReason.MARKET_CAP_DRAWDOWN in dropped.failed
    # The same token without the history that makes it a fall passes every gate.
    assert check_all(_metrics(market_cap_usd=20000, drawdown_pct=-80.0), facts, cfg).passed
