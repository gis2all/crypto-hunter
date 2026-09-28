"""Quality-score composition. Pure functions.

The 55% on-chain block is five factor percentiles; the 45% off-chain block is this
round's five 0-5 judgements about the project itself. Both weights, and the split
inside each of the two blocks, are settings the operator can move - nothing here is a
constant.

待定口径 #1 is settled here, by the frozen settings page rather than by taste. Those rows
are labelled 池深分位 / 成交额分位 / 地址数分位 / Top10 反向分位 / 捆绑率反向分位, so the
number is a rank inside this round: what share of this round's candidates this token
beats. The reference implementation scaled each factor by the round's maximum instead,
which turns a round with one outlier into a round where everyone else scores zero; a rank
does not, and it is what the column already claims to be.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from typing import Iterable, Protocol

from .models import FactorScore, SafetyFacts, ScoreBreakdown, TokenMetrics

ONCHAIN_FACTORS: tuple[str, ...] = (
    "liquidity",
    "volume_24h",
    "holders",
    "top10_concentration",
    "bundling",
)

FACTOR_FIELDS: dict[str, str] = {
    "liquidity": "liquidity_usd",
    "volume_24h": "volume_24h_usd",
    "holders": "holders",
    "top10_concentration": "top10_pct",
    "bundling": "bundling_pct",
}

#: The off-chain half: what the research pass judged about the project, each 0-5, keyed
#: exactly as the prompt asks for them. 产品完成度 / 项目实质与亮点 / 团队与 dev 可信度 /
#: 叙事与官方关系 / 社区活跃度. Weights ship even and live in
#: `scoring.offchain_factor_weights`, and there is no "undetermined": 0 is the judgement
#: that the search came back empty, which is itself a finding.
OFFCHAIN_FIELDS: tuple[str, ...] = (
    "f_completeness",
    "f_substance",
    "f_team",
    "f_narrative",
    "f_community",
)

#: Two of the five are "less is better". Their percentile is inverted so that every factor
#: reads the same way - one scale, higher is better - which is what makes them summable.
REVERSE_FACTORS = frozenset({"top10_concentration", "bundling"})

#: UI default: each block's five factors split evenly until the operator moves them.
DEFAULT_FACTOR_WEIGHT = 20.0


class PercentileProvider(Protocol):
    def percentile(self, factor: str, value: float) -> float:
        """0..1, higher is better; reverse factors are already inverted."""


class CandidateSetPercentile:
    """分位分母 = 本轮的硬过滤幸存者。

    Built once per round from the prefilter survivors, then asked about one token at a
    time. The midpoint convention (`below + half the ties`) keeps the best candidate off
    1.0 and the worst off 0.0, so a round of two tokens does not read as 100% and 0%.
    """

    def __init__(self, metrics: Iterable[TokenMetrics]) -> None:
        store: dict[str, list[float]] = {factor: [] for factor in ONCHAIN_FACTORS}
        for item in metrics:
            for factor, field in FACTOR_FIELDS.items():
                value = getattr(item, field)
                if value is not None:
                    store[factor].append(float(value))
        self._sorted = {factor: sorted(values) for factor, values in store.items()}

    def percentile(self, factor: str, value: float) -> float:
        rows = self._sorted.get(factor) or []
        if not rows:
            return 0.0
        if len(rows) == 1:
            # A set of one has no spread. Midpoint says "unknown" without saying "best".
            return 0.5
        below = bisect_left(rows, value)
        ties = bisect_right(rows, value) - below
        share = (below + 0.5 * ties) / len(rows)
        return 1.0 - share if factor in REVERSE_FACTORS else share

    def size(self, factor: str) -> int:
        return len(self._sorted.get(factor) or [])


def score_onchain(
    metrics: TokenMetrics,
    percentiles: PercentileProvider,
    factor_weights: dict[str, float] | None = None,
) -> tuple[float | None, list[FactorScore]]:
    """Weighted factor score on a 0..100 scale, normalised over factors that exist.

    A missing factor is reported as `missing` and excluded from the normalisation - it
    must not drag the score toward zero, and it must not be silently read as the worst
    possible value either.
    """
    weights = factor_weights or {f: DEFAULT_FACTOR_WEIGHT for f in ONCHAIN_FACTORS}
    factors: list[FactorScore] = []
    for factor in ONCHAIN_FACTORS:
        weight = float(weights.get(factor, DEFAULT_FACTOR_WEIGHT))
        raw = getattr(metrics, FACTOR_FIELDS[factor])
        if raw is None:
            factors.append(FactorScore(factor=factor, raw=None, score=0.0, weight=weight, note="missing"))
            continue
        pct = percentiles.percentile(factor, float(raw))
        factors.append(
            FactorScore(factor=factor, raw=float(raw), weight=weight, score=pct * weight)
        )

    known = sum(f.weight for f in factors if f.raw is not None)
    full = sum(f.weight for f in factors)
    if known <= 0 or full <= 0:
        return None, factors
    return sum(f.score for f in factors) / known * full, factors


def offchain_weights(scoring: dict) -> dict[str, float]:
    """The five off-chain weights, defaulting to the even split the block shipped with.

    A snapshot saved before this knob existed carries no weights at all, and a saved
    round could in principle hold five zeroes; both fall back to the even split, so a
    missing knob can never turn the normaliser into a zero divisor.
    """
    weights = scoring.get("offchain_factor_weights") or {}
    spread = {name: float(weights.get(name, DEFAULT_FACTOR_WEIGHT) or 0.0)
              for name in OFFCHAIN_FIELDS}
    if sum(spread.values()) <= 0:
        return {name: DEFAULT_FACTOR_WEIGHT for name in OFFCHAIN_FIELDS}
    return spread


def score_token(
    metrics: TokenMetrics,
    facts: SafetyFacts,
    settings: dict,
    percentiles: PercentileProvider,
    lexicon_entry: dict | None = None,
) -> ScoreBreakdown:
    """Compose the on-chain block (55%) with the off-chain block (45%).

    The off-chain block is five 0-5 judgements this round made about the token, weighted
    by `scoring.offchain_factor_weights` (an even split by default). A token with no entry
    was not judged at all and stays out of the ranking: a 0
    would read as "judged and found worthless", which is a different claim from "not
    judged". Once an entry exists the prompt requires all five; one that arrives empty is
    read as 0 and noted, because the round does have a judgement to record.
    """
    del facts  # the safety verdict is a gate, it does not enter the score
    scoring = settings.get("scoring") or {}
    weights = scoring.get("onchain_factor_weights") or None
    onchain, factors = score_onchain(metrics, percentiles, weights)
    notes: list[str] = []
    offchain: float | None = None
    pending = False

    if lexicon_entry is None:
        pending = True
        notes.append("no off-chain entry: pending review, excluded from the ranking")
    else:
        missing = [name for name in OFFCHAIN_FIELDS if lexicon_entry.get(name) is None]
        if missing:
            notes.append("off-chain judgement incomplete (%s), read as 0"
                         % ", ".join(missing))
        spread = offchain_weights(scoring)
        offchain = (
            sum(spread[name] * float(lexicon_entry.get(name) or 0.0)
                for name in OFFCHAIN_FIELDS)
            / (sum(spread.values()) * 5.0) * 100.0
        )

    total: float | None = None
    if not pending and onchain is not None and offchain is not None:
        w_on = float(scoring.get("onchain_weight", 55))
        w_off = float(scoring.get("offchain_weight", 45))
        total = onchain * w_on / 100.0 + offchain * w_off / 100.0

    return ScoreBreakdown(
        onchain=onchain,
        product=offchain,
        total=total,
        factors=factors,
        pending_review=pending,
        notes=notes,
    )
