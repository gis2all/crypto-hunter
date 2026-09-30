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
#: Every rule the run panel's single 淘汰 row covers, and the set that row is credited from.
#: It used to be split in two - 硬过滤 and 安全判定 - and that split was never a stage
#: boundary: LP 已锁, 陷阱占比 and 距高回撤 are numbers like the others, and the filter is
#: what reads numbers (`check_all`). One row also settles the arithmetic the funnel is read
#: against: 淘汰 = 扫描 - 候选, with nothing credited to a third column.
FILTER_PRIORITY = PREFILTER_PRIORITY + SAFETY_PRIORITY + (
    DropReason.ENTRAPMENT,
    DropReason.MARKET_CAP_DRAWDOWN,
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


def _ordered(reasons: list[DropReason]) -> list[DropReason]:
    """The rules' own order, so which half of the prefilter saw a token first cannot
    change which rule gets credited with rejecting it."""
    order = {reason: index for index, reason in enumerate(PREFILTER_PRIORITY)}
    return sorted(set(reasons), key=lambda reason: order.get(reason, len(order)))


def check_prefilter_free(metrics: TokenMetrics, prefilter: dict) -> FilterVerdict:
    """The pool rules the bulk pull alone can settle: the market-cap band, and the
    empty-shell rule.

    It costs no request, which is why it runs first - and what is left in it is only what
    every bulk source answers the same way. Market cap passes that test: GMGN, Tolly and
    DexScreener read the same number to the dollar on the same token (measured
    2026-09-29). Liquidity and 24h volume do not, so both floors moved to
    `check_prefilter_enriched`, where they run on the caliber that corrects them:
    Tolly's liquidity runs 5.52x high, and GMGN's rank row answers volume in whichever
    window it ranked in. A floor read straight off the pull judged a number before the
    read that fixes it had happened - and this half's verdicts are final.

    No rule that reads the chip picture is in here either. A rule reading a holder count
    this half never carried is reading a blank, and 未判定 never rejects: it would either
    drop nobody or drop the wrong token, and both are worse than waiting for the read.
    """
    failed: list[DropReason] = []
    unknown: list[DropReason] = []

    _ceiling(metrics.market_cap_usd, prefilter.get("market_cap_max_usd"),
             DropReason.MARKET_CAP, failed, unknown)
    _floor(metrics.market_cap_usd, prefilter.get("market_cap_min_usd"),
           DropReason.MARKET_CAP, failed, unknown)

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

    return FilterVerdict(passed=not failed, failed=_ordered(failed), unknown=_ordered(unknown))


def check_prefilter_enriched(metrics: TokenMetrics, prefilter: dict) -> FilterVerdict:
    """The half that reads numbers the bulk sweep cannot be trusted for: the liquidity and
    24h-volume floors, the holder band, the top-10 and bundling ceilings, and the
    honeypot flag.

    Each of these needs a per-token answer, for one of two reasons: the field only exists
    per token (holders, top-10, bundling), or the feed that bulk-carries it is a different
    measurement than the one the rule means (Tolly's 5.52x liquidity, GMGN's volume,
    which is the ranking window's total rather than the day's). So they run after `decide`
    has bought those numbers. A blank here is then a blank the paid read could not fill
    either, which is the only sense in which 未判定 is an honest answer for them.
    """
    failed: list[DropReason] = []
    unknown: list[DropReason] = []

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

    return FilterVerdict(passed=not failed, failed=_ordered(failed), unknown=_ordered(unknown))


def check_prefilter(metrics: TokenMetrics, prefilter: dict) -> FilterVerdict:
    """Both halves at once, for the one caller that has no enrich stage between them.

    `check_all` is that caller: the safety gate re-runs the pool rules against the token's
    own numbers, where every blank the pull left has already been asked about by name.
    """
    free = check_prefilter_free(metrics, prefilter)
    enriched = check_prefilter_enriched(metrics, prefilter)
    return FilterVerdict(
        passed=free.passed and enriched.passed,
        failed=_ordered(free.failed + enriched.failed),
        unknown=_ordered(free.unknown + enriched.unknown),
    )


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

#: The verifiers `dual` asks, one entry per source that can answer 已验证. A
#: negative verdict needs every one of them to have answered: a verifier whose request
#: failed has said nothing, and reading that silence as a veto is how a token GoPlus
#: calls `is_open_source == 1` came out of the round as 未验证.
DUAL_VERIFIERS = ("GoPlus", "Sourcify")


def source_verdict(facts: SafetyFacts, mode: str) -> bool | None:
    """源码已验证, under whichever口径 the chain ran with. None means 未知, not a no.

    `dual` asks both verifiers and lets either one vouch: the reference implementation's
    rule is `GoPlus.is_open_source == '1' or Sourcify has a match`, so a verifier that has
    never heard of the contract cannot veto one that vouched for it. A *no* is read the
    other way round: it stands only when every verifier answered. One that stayed silent -
    its request failed, or it has no record for the chain at all - has not said no, and
    letting the verifier that happened to reply decide is how a verified contract was
    written down as unverified. Asking twice is worth something because the second
    answer can turn a no into a yes. `goplus_only` lets GoPlus decide alone,
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
    if verified:
        return True
    if set(DUAL_VERIFIERS) - checked:
        return None
    return False


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


#: A high above this multiple of the chain own ceiling is a parsing artefact rather than a
#: token that ran. Tolly reports launchpad shells at 8.9e13 with liquidity set to the same
#: number and a cent of volume; read as a high, every later round would see that token down
#: 100% and drop it forever. The same guard covers the store column and GMGN own high, so no
#: single bad reading can turn into a permanent drop.
HIGH_SANITY_MULTIPLE = 50


def _drawdown_pct(now: float | None, peak: float | None) -> float | None:
    """How far below its own high a token trades, in percent, or None when unanswerable."""
    if not now or not peak or peak <= 0:
        return None
    return (now / peak - 1) * 100


def _believable_high(value: float | None, prefilter: dict) -> float | None:
    """One estimate of the token high, or None when it is missing or not believable."""
    if value is None or value <= 0:
        return None
    ceiling = prefilter.get("market_cap_max_usd")
    if ceiling and value > float(ceiling) * HIGH_SANITY_MULTIPLE:
        return None
    return float(value)


def drawdown_readings(metrics: TokenMetrics, prefilter: dict,
                      peak_market_cap_usd: float | None = None) -> list[float]:
    """Every estimate of the same fall, in percent. The deepest one is the best one.

    Three readings of one high, and they are estimates rather than a disagreement:

    - `metrics.high_market_cap_usd`, the high the source itself measured (GMGN own column).
      This is the reading the rule wants first: it is a measurement, not a sample.
    - `peak_market_cap_usd`, the highest market cap this project ever recorded for the CA.
      A lower bound on the same number - a token that spiked between two rounds carries a
      high well below its real one - and the reason this side used to flag and never drop.
      It is written back from the source high (`pipeline/write.py`), so once any source has
      answered it carries a real high even in a round where that source says nothing.
    - `metrics.drawdown_pct`, the pool history: what the pools the token trades in did over
      their whole daily life (`sources/geckoterminal.py`).

    Any one of them saying "past the ceiling" drops the token: of the 678 snapshots that
    carried both readings on 2026-09-27, 169 had fallen 70% or more by the pool number while
    our own side said they had not, and not one of them was dropped - which is what asking
    the readings to agree did to this rule.
    """
    out: list[float] = []
    now = metrics.market_cap_usd
    for high in (metrics.high_market_cap_usd, peak_market_cap_usd):
        value = _drawdown_pct(now, _believable_high(high, prefilter))
        if value is not None:
            out.append(value)
    if metrics.drawdown_pct is not None:
        out.append(metrics.drawdown_pct)
    return out


def check_drawdown(metrics: TokenMetrics, prefilter: dict,
                   peak_market_cap_usd: float | None = None) -> FilterVerdict:
    """距高回撤. One negative knob (`drawdown_max_pct`, -70 by default): a token this far
    below its high has had its run.

    The readings are `drawdown_readings` and the deepest of them decides. A token with no
    reading at all is not a fallen token - it is either one this project has never seen, or
    one whose pool history could not be read - so it passes; the second of those is marked
    未知 rather than passed in silence (`metrics.drawdown_unread`), because a throttled read
    is a question we did not get to ask, not an answer.
    """
    ceiling = prefilter.get("drawdown_max_pct")
    if ceiling is None:
        return FilterVerdict(passed=True)
    readings = drawdown_readings(metrics, prefilter, peak_market_cap_usd)
    if any(value <= float(ceiling) for value in readings):
        return FilterVerdict(passed=False, failed=[DropReason.MARKET_CAP_DRAWDOWN])
    if readings:
        return FilterVerdict(passed=True)
    if metrics.drawdown_unread:
        return FilterVerdict(passed=True, unknown=[DropReason.MARKET_CAP_DRAWDOWN])
    return FilterVerdict(passed=True)


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
