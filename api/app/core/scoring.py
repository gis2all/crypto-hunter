"""Quality-score composition. Pure functions.

The 55% on-chain block is five factor percentiles; the 45% product block is the lexicon's
own judgement. Both weights, and the split between the five factors, are settings the
operator can move - nothing here is a constant.

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

#: Two of the five are "less is better". Their percentile is inverted so that every factor
#: reads the same way - one scale, higher is better - which is what makes them summable.
REVERSE_FACTORS = frozenset({"top10_concentration", "bundling"})

#: UI default: the 55% on-chain block is split evenly across its five factors.
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


def score_token(
    metrics: TokenMetrics,
    facts: SafetyFacts,
    settings: dict,
    percentiles: PercentileProvider,
    lexicon_entry: dict | None = None,
) -> ScoreBreakdown:
    """Compose the on-chain block (55%) with the product block (45%).

    The unlisted-token rule is a setting, not a default: `pending_review` leaves the token
    out of the ranking rather than showing it a 0, because a 0 would read as "was judged
    and failed". `zero` is the other honest option and the operator can pick it.
    """
    del facts  # the safety verdict is a gate, it does not enter the score
    scoring = settings.get("scoring") or {}
    weights = scoring.get("onchain_factor_weights") or None
    onchain, factors = score_onchain(metrics, percentiles, weights)
    notes: list[str] = []
    product: float | None = None
    pending = False

    narrative = (lexicon_entry or {}).get("narrative")
    completeness = (lexicon_entry or {}).get("product")
    if lexicon_entry is None or narrative is None or completeness is None:
        if lexicon_entry is not None:
            notes.append("lexicon judgement incomplete")
        if str(scoring.get("unlisted_policy") or "pending_review") == "zero":
            if lexicon_entry is None:
                notes.append("no lexicon entry: scored 0 by this chain's 未入库标的 policy")
            product = 0.0
        else:
            pending = True
            if lexicon_entry is None:
                notes.append("no lexicon entry: pending review, excluded from the ranking")
    else:
        product = (float(narrative) + float(completeness)) / 10.0 * 100.0

    total: float | None = None
    if not pending and onchain is not None and product is not None:
        w_on = float(scoring.get("onchain_weight", 55))
        w_prod = float(scoring.get("product_weight", 45))
        total = onchain * w_on / 100.0 + product * w_prod / 100.0

    return ScoreBreakdown(
        onchain=onchain,
        product=product,
        total=total,
        factors=factors,
        pending_review=pending,
        notes=notes,
    )
