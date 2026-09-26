"""The round as one readable page, for the writer.

The reference implementation wrote a `brief.md` per round and handed it to the model as
context. The same idea, built from the database rather than from a file, so the brief and
the page can never disagree - it is the same facts, formatted once for a reader and once
for a writer.

The facts themselves live in `report/facts.py`, which is also the sheet the card draws;
this module adds only what is the round’s rather than the token’s - the ranking line, and
the picks in order. The split is deliberate: the writer used to be handed eight fields while
the card beside it drew thirty off the same snapshot, and two formatters over one snapshot
is how the two of them drifted apart in the first place.
"""

from __future__ import annotations

from ..core.models import ScoreBreakdown
from . import facts as facts_mod

#: Thin aliases, kept because other modules import these names. `facts` owns the formatting.
_usd = facts_mod.money
_pct = facts_mod.pct
_count = facts_mod.count


def token_block(item, breakdown: ScoreBreakdown | None) -> str:
    """One pick’s whole sheet, with this round’s score under it."""
    onchain = breakdown.onchain if breakdown else None
    product = breakdown.product if breakdown else None
    total = breakdown.total if breakdown else None
    scored = "  【评分】链上质量 %s ｜ 赛道产品 %s ｜ 综合 %s"
    return "\n".join([
        facts_mod.block(item.metrics, item.facts),
        scored % (_pct(onchain), _pct(product), _pct(total)),
    ])


def build(picked, changes: list[dict], notes: list[str], near_miss: list | None = None) -> str:
    """The brief: the picks, what moved, what nearly made it, and what went wrong.

    The near misses are here because "why did this one win" is a question about the tokens
    it beat. A writer told only the winners can describe a ranking but not defend it, and
    the 落选 pool is the same list the page draws its own table from.
    """
    lines = ["本轮入选（按综合分排序）：", ""]
    for index, item in enumerate(picked, start=1):
        lines.append("%d. %s" % (index, token_block(item, item.breakdown)))
    lines.append("")
    lines.append("本期变化：")
    for change in changes:
        lines.append("- %s：%s" % (change.get("kind"), change.get("detail")))
    if near_miss:
        lines.append("")
        lines.append("落选池（同一批打分、未入选，供对照）：")
        for item in near_miss:
            metrics = item.metrics
            lines.append("- %s ｜ 市值 %s ｜ 流动性 %s ｜ 24h 成交 %s ｜ 持有人 %s ｜ 总分 %s" % (
                getattr(metrics, "symbol", None) or item.ca[:10],
                _usd(getattr(metrics, "market_cap_usd", None)),
                _usd(getattr(metrics, "liquidity_usd", None)),
                _usd(getattr(metrics, "volume_24h_usd", None)),
                _count(getattr(metrics, "holders", None)),
                _pct(item.score),
            ))
    if notes:
        lines.append("")
        lines.append("本轮提示：")
        for note in notes:
            lines.append("- %s" % note)
    return "\n".join(lines)
