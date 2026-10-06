"""Funnel arithmetic. Pure.

待定口径 #2 is settled here too: a token is credited to the **first** rule that rejects
it, walking the same order the settings page lists the rules in. That ordering is why the
per-rule numbers add up to the number of tokens dropped - the reference round's own table
reconciles exactly that way (2,417 + 318 + 262 + 121 + 46 + 18 + 4 = 3,186 dropped out of
3,199 scanned). Independent tallies would let one token be reported five times and the
column would stop meaning anything.

The priority order lives in core/filters.py next to the rules themselves, so a rule and
its rank cannot drift apart.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Mapping

from .filters import PREFILTER_PRIORITY, SAFETY_PRIORITY
from .models import DropReason, FunnelSummary

#: Prefilter first, then the safety gate, then the enrichment-stage risk ceiling. The
#: settings page lists its rows in this order, and so does the round's summary.
DROP_PRIORITY: tuple[str, ...] = tuple(
    [reason.value for reason in PREFILTER_PRIORITY]
    + [reason.value for reason in SAFETY_PRIORITY]
    + ["entrapment"]
    #: 距高回撤 is judged at the gate, once the pool read it needs exists, but it is not a
    #: safety rule. Credited last, so a token that also broke a threshold above it is still
    #: counted against that threshold.
    + [DropReason.MARKET_CAP_DRAWDOWN.value]
)


#: The reader's words for each rule. Kept beside the priority list rather than in the
#: view layer because two readers need them now: the settings rows, and the change bar's
#: 退出 reason. A token that leaves the report has to be explained in the same words the
#: rule is configured under, and a second copy of this dict is how the two drift apart.
#: `api/v1/forms.rule_label` delegates here.
RULE_LABEL: dict[str, str] = {
    "market_cap": "市值",
    "liquidity": "流动性",
    "volume_24h": "24h 成交",
    "holders": "持有人",
    "top10": "Top10 集中度",
    "bundling": "捆绑率",
    "honeypot": "貔貅 / 陷阱占比",
    "entrapment": "陷阱占比",
    "source_unverified": "源码已验证",
    "lp_unlocked": "LP 已锁",
    "dangerous_perm": "无危险权限",
    "empty_shell": "空壳淘汰",
    "missing_data": "零数据淘汰",
    "market_cap_drawdown": "距高回撤",
}


def summarize(items: Iterable[Mapping]) -> FunnelSummary:
    """The scan split three ways, with 入选 counted inside 候选.

    候选 is the pool that survived the prefilter - the tokens 入选 is chosen from - so the
    identities are 扫描 = 淘汰 + 候选 and 入选 <= 候选. Keeping 入选 out of 候选 would make the
    number depend on which stage is reading it: the strip's 候选 and 落选池's 候选 would
    differ by exactly the report count, which is how a page ends up saying 候选 56 above a
    table of 56 rows headed 候选 61.
    """
    counts = Counter(i.get("decision") for i in items)
    dropped = counts.get("dropped", 0)
    selected = counts.get("selected", 0)
    candidates = counts.get("candidate", 0) + selected
    return FunnelSummary(
        scanned=dropped + candidates,
        dropped=dropped,
        candidates=candidates,
        selected=selected,
    )


def _failed(item: Mapping) -> list[str]:
    """Every reason an item failed. The first one by priority is what the table credits."""
    reasons = item.get("failed")
    if reasons:
        return [str(reason) for reason in reasons]
    single = item.get("drop_reason")
    return [str(single)] if single else []


def first_reason(item: Mapping, priority: tuple[str, ...] = DROP_PRIORITY) -> str | None:
    reasons = set(_failed(item))
    for rule in priority:
        if rule in reasons:
            return rule
    return next(iter(reasons), None)


def independent_drop_counts(items: Iterable[Mapping]) -> dict[str, int]:
    """How many tokens each rule rejects on its own, counted independently."""
    counts = Counter(
        i.get("drop_reason") for i in items if i.get("decision") == "dropped" and i.get("drop_reason")
    )
    return dict(counts)


def cumulative_drop_counts(
    items: Iterable[Mapping], priority: tuple[str, ...] = DROP_PRIORITY
) -> list[tuple[str, int]]:
    """Each rule's tally, counting only the tokens it is the first to reject.

    Returned in `priority` order - which is also the order the settings page draws its
    rows in, so the reader can follow one list from top to bottom.
    """
    rows = list(items)
    credited = Counter()
    for item in rows:
        if item.get("decision") != "dropped":
            continue
        reason = first_reason(item, priority)
        if reason:
            credited[reason] += 1
    # A reason that is not in the priority list still happened; keep it visible at the end
    # rather than dropping it, or a new rule would silently go unreported.
    extra = sorted(set(credited) - set(priority))
    return [(rule, credited.get(rule, 0)) for rule in priority] + [
        (rule, credited[rule]) for rule in extra
    ]
