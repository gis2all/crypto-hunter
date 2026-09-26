"""Deterministic filters. Pure functions.

Unknown is a first-class answer: a missing field means "not judged" - never "passed",
never "dropped". This is what keeps a dead source from silently emptying the funnel.

Every threshold here is editable from the settings page; the keys below are the ones
`api/v1/forms.py` exposes as widgets, so a knob in the UI always has a rule behind it.
"""

from __future__ import annotations

from . import scoring
from .models import DropReason, FilterVerdict, SafetyFacts, TokenMetrics

#: Attribution order, and the order the settings page draws its rows in. A token is
#: credited to the first rule here that rejects it, which is what makes the per-rule
#: tallies add up to the number of tokens dropped.
PREFILTER_PRIORITY = (
    DropReason.MARKET_CAP,
    DropReason.LIQUIDITY,
    DropReason.HOLDERS,
    DropReason.TOP10,
    DropReason.BUNDLING,
    DropReason.HONEYPOT,
    DropReason.VOLUME_24H,
    #: Last on purpose. 零数据淘汰 is a hard filter, but a token that both has nothing to
    #: rank on and misses a real threshold is more usefully credited to the threshold.
    DropReason.MISSING_DATA,
)
SAFETY_PRIORITY = (
    DropReason.SOURCE_UNVERIFIED,
    DropReason.LP_UNLOCKED,
    DropReason.DANGEROUS_PERM,
)


def _floor(value, floor, reason, failed, unknown) -> None:
    if floor is None:
        return
    if value is None:
        unknown.append(reason)
    elif value < floor:
        failed.append(reason)


def _ceiling(value, ceiling, reason, failed, unknown) -> None:
    if ceiling is None:
        return
    if value is None:
        unknown.append(reason)
    elif value > ceiling:
        failed.append(reason)


def check_prefilter(metrics: TokenMetrics, prefilter: dict) -> FilterVerdict:
    """Pool-level rules: two bands (market cap, holders), liquidity/volume floors,
    concentration and bundling ceilings, and the zero-tolerance honeypot flag.

    These are computable from the universe pull alone, so they run before any
    per-token request is spent. `require_market_data` is the one rule that reads absence
    rather than a value, and it exists because unknown is otherwise never dropped. It is
    on by default: a row with no market cap and no reserve is not an unanswered question.
    """
    failed: list[DropReason] = []
    unknown: list[DropReason] = []

    _ceiling(metrics.market_cap_usd, prefilter.get("market_cap_max_usd"),
             DropReason.MARKET_CAP, failed, unknown)
    _floor(metrics.market_cap_usd, prefilter.get("market_cap_min_usd"),
           DropReason.MARKET_CAP, failed, unknown)
    _floor(metrics.liquidity_usd, prefilter.get("liquidity_min_usd"),
           DropReason.LIQUIDITY, failed, unknown)
    _floor(metrics.volume_24h_usd, prefilter.get("volume_24h_min_usd"),
           DropReason.VOLUME_24H, failed, unknown)
    _floor(metrics.holders, prefilter.get("holders_min"),
           DropReason.HOLDERS, failed, unknown)
    # 持有人上限留空 = 不设上限。它和市值一样是「区间」而不是「下限」：一个键在设置页里
    # 是一个区间，但在老快照里可能只有下限，所以缺键必须等于「这条规则不存在」，否则
    # 一次升级就会开始砍掉一批本来就合规的币。
    _ceiling(metrics.holders, prefilter.get("holders_max"),
             DropReason.HOLDERS, failed, unknown)
    _ceiling(metrics.top10_pct, prefilter.get("top10_max_pct"),
             DropReason.TOP10, failed, unknown)
    _ceiling(metrics.bundling_pct, prefilter.get("bundling_max_pct"),
             DropReason.BUNDLING, failed, unknown)

    if prefilter.get("forbid_honeypot"):
        if metrics.honeypot_detected is None:
            unknown.append(DropReason.HONEYPOT)
        elif metrics.honeypot_detected:
            failed.append(DropReason.HONEYPOT)

    # 空壳淘汰: a row no source could put a market cap or a reserve on is not a token we
    # failed to measure, it is a token with nothing to measure. Unknown is never dropped,
    # so without this rule any source that ships addresses without market data floods the
    # funnel with rows every floor above waves straight through. Both fields missing is
    # the signal; either one answering means there is something real to rank.
    #
    # On unless the chain turns it off. It used to be opt-in, and that was the hole: a round
    # running against a settings version saved before the key existed judged four thousand
    # empty rows on "every floor answered 未知". A missing knob is not a missing rule.
    if prefilter.get("require_market_data", True):
        if metrics.market_cap_usd is None and metrics.liquidity_usd is None:
            failed.append(DropReason.MISSING_DATA)

    return FilterVerdict(passed=not failed, failed=failed, unknown=unknown)


def scored_fields(metrics: TokenMetrics) -> int:
    """How many of the five ranking factors carry a number.

    This is the round's ability to rank the token at all: with none of them it cannot be
    scored, cannot take a pool rank, and cannot be compared against the five that get a
    report. It reads `scoring.FACTOR_FIELDS` rather than its own list, so a factor added
    to the score cannot go missing from the rule that guards it.
    """
    return sum(
        1 for field_name in scoring.FACTOR_FIELDS.values()
        if getattr(metrics, field_name, None) is not None
    )


def check_scored_data(metrics: TokenMetrics, prefilter: dict) -> FilterVerdict:
    """零数据淘汰: every source answered and none of them had anything to rank on.

    Deliberately post-enrichment - it is called from `check_all`, never from the
    prefilter. Before the per-token phase a blank field means "the bulk pull did not
    carry it", and a dead source must not be allowed to empty the funnel; afterwards a
    blank means "we asked, and nobody has it", which is a property of the token.

    `min_scored_fields` is the口径: how many of the five factors must carry a number.
    Absent or 0 leaves the rule off, so a config written before this rule existed does
    not start dropping rows on upgrade.
    """
    floor = prefilter.get("min_scored_fields")
    if floor is None:
        return FilterVerdict(passed=True)
    need = int(floor)
    if need <= 0:
        return FilterVerdict(passed=True)
    if scored_fields(metrics) < need:
        return FilterVerdict(passed=False, failed=[DropReason.MISSING_DATA])
    return FilterVerdict(passed=True)


def check_safety(facts: SafetyFacts, safety: dict) -> FilterVerdict:
    """Safety gate. Same rule: unknown is neither a pass nor a drop.

    `lp_unjudged_passes` is explicit configuration rather than a silent default,
    because treating "LP not established" as a pass is a product decision. There is
    deliberately no way to switch a row off - base rules stay on, only their口径 moves.
    """
    failed: list[DropReason] = []
    unknown: list[DropReason] = []

    if safety.get("require_verified_source"):
        verdict = source_verdict(facts, str(safety.get("source_verification_mode") or "dual"))
        if verdict is None:
            unknown.append(DropReason.SOURCE_UNVERIFIED)
        elif verdict is False:
            failed.append(DropReason.SOURCE_UNVERIFIED)

    forbidden = list(safety.get("forbid_dangerous_perms") or [])
    if forbidden:
        if facts.dangerous_perms is None:
            unknown.append(DropReason.DANGEROUS_PERM)
        elif set(forbidden) & set(facts.dangerous_perms):
            failed.append(DropReason.DANGEROUS_PERM)

    lp = facts.lp_locked
    if lp is False:
        failed.append(DropReason.LP_UNLOCKED)
    elif lp is None and not safety.get("lp_unjudged_passes", False):
        unknown.append(DropReason.LP_UNLOCKED)

    return FilterVerdict(passed=not failed, failed=failed, unknown=unknown)


#: The verifier `goplus_only` trusts. Named so the口径 and the code cannot drift apart.
PRIMARY_VERIFIER = "GoPlus"


def source_verdict(facts: SafetyFacts, mode: str) -> bool | None:
    """源码已验证, under whichever口径 the chain ran with. None means 未知, not a no.

    `dual` asks both verifiers and lets either one vouch: the reference implementation's
    rule is `GoPlus.is_open_source == '1' or Sourcify has a match`, so a verifier that has
    never heard of the contract cannot veto one that vouched for it. It still has to be a
    positive answer from somewhere - if a verifier answered "no record" and nobody
    confirmed, the token is a no, not an unknown. Asking twice is worth something because
    the second answer can turn that no into a yes. `goplus_only` lets GoPlus decide alone,
    which is the other option the settings page offers.

    A caller that filled in only the merged flag (a seeded snapshot) is judged on that
    flag, so the two readings agree wherever there are no votes to read.
    """
    verified = set(facts.source_verified_by)
    checked = set(facts.source_checked_by)
    if not verified and not checked:
        return facts.source_verified
    if mode == "goplus_only":
        if PRIMARY_VERIFIER not in checked and PRIMARY_VERIFIER not in verified:
            return None
        return PRIMARY_VERIFIER in verified
    return bool(verified)


def check_entrapment(metrics: TokenMetrics, safety: dict) -> FilterVerdict:
    """Entrapment share above this chain's ceiling is a hard drop.

    Its own reason rather than the GMGN honeypot flag: the settings page lists 貔貅 and
    陷阱占比 as two rows, and they are two different measurements - one is a verdict, one
    is a share of wallets that behave like a trap.
    """
    ceiling = safety.get("honeypot_max_pct")
    if ceiling is None:
        return FilterVerdict(passed=True)
    if metrics.honeypot_pct is None:
        return FilterVerdict(passed=True, unknown=[DropReason.ENTRAPMENT])
    if metrics.honeypot_pct > ceiling:
        return FilterVerdict(passed=False, failed=[DropReason.ENTRAPMENT])
    return FilterVerdict(passed=True)


def _drawdown_pct(now: float | None, peak: float | None) -> float | None:
    """How far below its own high a token trades, in percent, or None when unanswerable."""
    if not now or not peak or peak <= 0:
        return None
    return (now / peak - 1) * 100


def check_drawdown(metrics: TokenMetrics, prefilter: dict,
                   peak_market_cap_usd: float | None) -> FilterVerdict:
    """距高回撤, judged twice: our own history first, the pool side to confirm.

    The knob is one negative number (`drawdown_max_pct`, -70 by default) and it means the
    same thing on both readings: a token this far below its high has already had its run.

    The two are asked in that order on purpose. Ours is free and covers the whole scan, so
    it decides whether the expensive question is worth asking at all - GeckoTerminal charges
    three paced requests per token. `peak_market_cap_usd` is the highest market cap any
    source has reported for this CA across every round, so it is not capped by this chain own
    band: a token that ran to two million and came back into range still carries the two
    million, which is exactly the fall this rule is about.

    A token with no history is not a fallen token - it is one this project has never seen
    before - so it passes without an unknown. Where our side flags a token and the pool side
    has nothing to say, the answer is 未知: not dropped, and the settings page can show that
    this rule wanted a second opinion it never got.
    """
    ceiling = prefilter.get("drawdown_max_pct")
    if ceiling is None:
        return FilterVerdict(passed=True)
    ceiling = float(ceiling)
    own = _drawdown_pct(metrics.market_cap_usd, peak_market_cap_usd)
    if own is None or own > ceiling:
        return FilterVerdict(passed=True)
    pool = metrics.drawdown_pct
    if pool is None:
        return FilterVerdict(passed=True, unknown=[DropReason.MARKET_CAP_DRAWDOWN])
    if pool <= ceiling:
        return FilterVerdict(passed=False, failed=[DropReason.MARKET_CAP_DRAWDOWN])
    return FilterVerdict(passed=True)


def drawdown_needs_pool(metrics: TokenMetrics, prefilter: dict,
                        peak_market_cap_usd: float | None) -> bool:
    """Whether the pool side still has to be read before 距高回撤 can be judged.

    True for the tokens our own history already put below the ceiling - the ones this rule
    would drop if the second reading agreed. Everyone else is answered for free, which is
    what keeps a rule that needs a pooled price history from costing a request per token.
    """
    ceiling = prefilter.get("drawdown_max_pct")
    if ceiling is None:
        return False
    own = _drawdown_pct(metrics.market_cap_usd, peak_market_cap_usd)
    if own is None or own > float(ceiling):
        return False
    return metrics.drawdown_pct is None


def check_all(metrics: TokenMetrics, facts: SafetyFacts, pipeline_cfg: dict,
              peak_market_cap_usd: float | None = None) -> FilterVerdict:
    """Every gate, in one answer: what failed, and what could not be judged.

    This is the whole "does it survive" question - the pool-level rules, the safety gate,
    the entrapment ceiling and 距高回撤 - so a caller that wants the round's own verdict
    asks once. `peak_market_cap_usd` is that rule's own input and comes from the store;
    a caller without it leaves the rule with nothing to judge on and it passes.
    """
    prefilter = pipeline_cfg.get("prefilter") or {}
    verdicts = [
        check_prefilter(metrics, prefilter),
        check_safety(facts, pipeline_cfg.get("safety") or {}),
        check_entrapment(metrics, pipeline_cfg.get("safety") or {}),
        check_scored_data(metrics, prefilter),
        check_drawdown(metrics, prefilter, peak_market_cap_usd),
    ]
    failed = [r for v in verdicts for r in v.failed]
    unknown = [r for v in verdicts for r in v.unknown]
    return FilterVerdict(passed=not failed, failed=failed, unknown=unknown)
