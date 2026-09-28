"""Scoring composition. The percentile provider is injected, so this is testable
before the percentile口径 exists."""

import pytest

from app.core.models import SafetyFacts, TokenMetrics
from app.core.scoring import (
    CandidateSetPercentile,
    score_onchain,
    score_token,
)

SETTINGS = {"scoring": {"onchain_weight": 55, "offchain_weight": 45}}


class Fixed:
    def __init__(self, value: float) -> None:
        self.value = value

    def percentile(self, factor: str, value: float) -> float:
        return self.value


def _full(**kw) -> TokenMetrics:
    base = dict(ca="0xabc", liquidity_usd=50000, volume_24h_usd=20000, holders=2000,
                top10_pct=15.0, bundling_pct=1.0)
    base.update(kw)
    return TokenMetrics(**base)


def test_full_house_is_the_full_scale():
    onchain, factors = score_onchain(_full(), Fixed(1.0))
    assert onchain == 100
    assert len(factors) == 5


def test_missing_factor_is_excluded_not_zeroed():
    onchain, factors = score_onchain(_full(liquidity_usd=None), Fixed(1.0))
    assert onchain == 100
    assert [f.note for f in factors].count("missing") == 1


def test_no_known_factor_yields_no_score():
    onchain, _ = score_onchain(
        TokenMetrics(ca="0xabc"), Fixed(1.0)
    )
    assert onchain is None


def test_without_lexicon_entry_the_token_is_not_ranked():
    breakdown = score_token(_full(), SafetyFacts(), SETTINGS, Fixed(1.0), lexicon_entry=None)
    assert breakdown.pending_review is True
    assert breakdown.total is None
    assert breakdown.onchain == 100


def _offchain(**over):
    """The five 0-5 judgements, all present, so a test can move one at a time."""
    base = {"f_completeness": 3.0, "f_substance": 3.0, "f_team": 3.0,
            "f_narrative": 3.0, "f_community": 3.0}
    base.update(over)
    return base


def test_total_uses_both_blocks():
    strong = score_token(_full(), SafetyFacts(), SETTINGS, Fixed(1.0),
                         lexicon_entry=_offchain(f_completeness=5.0, f_substance=5.0,
                                                 f_team=5.0, f_narrative=5.0,
                                                 f_community=5.0))
    assert strong.product == 100
    assert strong.total == 100

    weak_offchain = score_token(_full(), SafetyFacts(), SETTINGS, Fixed(1.0),
                                lexicon_entry=_offchain(f_completeness=0.0, f_substance=0.0,
                                                        f_team=0.0, f_narrative=0.0,
                                                        f_community=0.0))
    assert weak_offchain.total == 55


def test_a_blank_judgement_is_read_as_zero_not_pending():
    """The research pass ran and came back empty: 0 is the finding, not a missing row."""
    breakdown = score_token(_full(), SafetyFacts(), SETTINGS, Fixed(1.0),
                            lexicon_entry={"f_narrative": 3.0})
    assert breakdown.pending_review is False
    assert breakdown.total is not None
    assert breakdown.product == pytest.approx(3.0 / 25.0 * 100.0)
    assert any("incomplete" in note for note in breakdown.notes)


# --------------------------------------------------------------------- 分位口径

def _pool():
    """Four survivors, each factor a straight ladder, so the arithmetic is visible."""
    return [
        TokenMetrics(ca="0x%d" % i, liquidity_usd=10 * i, volume_24h_usd=10 * i, holders=10 * i,
                     top10_pct=10.0 * i, bundling_pct=10.0 * i)
        for i in (1, 2, 3, 4)
    ]


def test_best_and_worst_sit_off_the_ends():
    """Midpoint, not max: the best candidate is not a 1.0 and the worst is not a 0.0, so a
    round of four does not read as a perfect score against itself."""
    provider = CandidateSetPercentile(_pool())
    assert provider.percentile("liquidity", 40) == pytest.approx(0.875)
    assert provider.percentile("liquidity", 10) == pytest.approx(0.125)


def test_ties_split_their_share():
    pool = [TokenMetrics(ca="0x%d" % i, liquidity_usd=10 * (i // 2 + 1)) for i in range(4)]
    provider = CandidateSetPercentile(pool)
    assert provider.percentile("liquidity", 10) == pytest.approx(0.25)
    assert provider.percentile("liquidity", 20) == pytest.approx(0.75)


def test_a_set_of_one_is_unknown_not_best():
    provider = CandidateSetPercentile(_pool()[:1])
    assert provider.percentile("liquidity", 10) == 0.5


def test_less_is_better_factors_are_inverted():
    provider = CandidateSetPercentile(_pool())
    assert provider.percentile("top10_concentration", 10) == pytest.approx(0.875)
    assert provider.percentile("bundling", 40) == pytest.approx(0.125)


def test_a_missing_value_stays_out_of_the_denominator():
    provider = CandidateSetPercentile(_pool() + [TokenMetrics(ca="0x9")])
    assert provider.size("liquidity") == 4
    assert provider.percentile("liquidity", 40) == pytest.approx(0.875)


def test_the_denominator_is_the_set_it_was_built_from():
    """口径 #1: the percentile is measured inside this round's survivors, so the same
    token scores differently in a weak round and a strong one. That is the point."""
    weak = CandidateSetPercentile([TokenMetrics(ca="0xa", liquidity_usd=100)])
    strong = CandidateSetPercentile(
        [TokenMetrics(ca="0x%d" % i, liquidity_usd=100 * i) for i in range(1, 5)]
    )
    assert weak.percentile("liquidity", 100) == 0.5
    assert strong.percentile("liquidity", 100) == pytest.approx(0.125)


# --------------------------------------------------------------- 链下调研（45%）

def test_a_blank_dict_is_a_judgement_of_zero_not_a_missing_one():
    breakdown = score_token(_full(), SafetyFacts(), SETTINGS, Fixed(1.0), {})
    assert breakdown.pending_review is False
    assert breakdown.product == 0.0
    assert breakdown.total == pytest.approx(55.0)


def test_the_offchain_block_is_five_judgements_out_of_twenty_five():
    breakdown = score_token(
        _full(), SafetyFacts(), SETTINGS, Fixed(1.0), _offchain(f_completeness=2.5,
                                                               f_substance=2.5,
                                                               f_team=2.5,
                                                               f_narrative=2.5,
                                                               f_community=2.5)
    )
    assert breakdown.product == pytest.approx(50.0)
    assert breakdown.total == pytest.approx(0.55 * 100 + 0.45 * 50.0)
