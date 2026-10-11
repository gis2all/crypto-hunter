"""The round口径 in words: the three lines the overview prints, and the archive keeps.

Two layers need the same sentence, and neither of them is the view: `pipeline/write` stores it
with the round - a round is archived under the口径 of its own time - and `report/assemble`
prints it on the overview. It was written in `api/v1/forms.py`, so both of them imported the
HTTP layer's widget module to borrow a string. The words live here instead, for the same reason
`core/funnel.RULE_LABEL` does: one copy, two readers, so the archive and the page cannot drift
apart.

Pure: a settings dict in, strings out.
"""

from __future__ import annotations

from typing import Any

#: The five factors as the口径 line prints them: short, because they ride inside one sentence.
#: The long name and how each one is read are the settings page's own words (`FACTOR_LABELS`).
FACTOR_SHORT = {
    "liquidity": "流动性",
    "volume_24h": "成交",
    "holders": "持有人",
    "top10_concentration": "集中度",
    "bundling": "捆绑",
}


def _num(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return "%g" % float(value)
    except (TypeError, ValueError):
        return str(value)


def usd(value: Any) -> str:
    """$0 / $8K / $200K / $1.2M - the shorthand the口径 line and the card's reason speak in."""
    if value is None:
        return "—"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    for scale, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(amount) >= scale:
            short = ("%.1f" % (amount / scale)).rstrip("0").rstrip(".")
            return "$%s%s" % (short, suffix)
    return "$%s" % _num(amount)


def text(chain: dict) -> list[str]:
    """The round口径 as the three lines the overview shows.

    Built from the settings the round ran under, never from the report text, so the
    overview and the settings page cannot drift apart.
    """
    pipeline = chain.get("pipeline") or {}
    prefilter = pipeline.get("prefilter") or {}
    safety = pipeline.get("safety") or {}
    scoring = pipeline.get("scoring") or {}
    hard = [
        "市值 %s – %s" % (usd(prefilter.get("market_cap_min_usd", 0)),
                          usd(prefilter.get("market_cap_max_usd"))),
        "流动性 ≥ %s" % usd(prefilter.get("liquidity_min_usd")),
        ("持有人 %s – %s" % (_num(prefilter.get("holders_min")),
                            _num(prefilter.get("holders_max")))
         if prefilter.get("holders_max") is not None
         else "持有人 ≥ %s" % _num(prefilter.get("holders_min"))),
        "Top10 集中度 ≤ %s%%" % _num(prefilter.get("top10_max_pct")),
        "捆绑率 ≤ %s%%" % _num(prefilter.get("bundling_max_pct")),
        "貔貅 = 0" if prefilter.get("forbid_honeypot") else "貔貅 不设门槛",
        "24h 成交 ≥ %s" % usd(prefilter.get("volume_24h_min_usd")),
        "零数据淘汰 ≥ %s 项可打分字段" % _num(prefilter.get("min_scored_fields")),
        ("距高回撤 ≥ %s%%（源高点 / 库记录高点 / 池面全历史，任一跌够）" % _num(abs(prefilter["drawdown_max_pct"]))
         if prefilter.get("drawdown_max_pct") is not None else "距高回撤 不设门槛"),
    ]
    verified = ("双源：GoPlus + Sourcify"
                if safety.get("source_verification_mode", "dual") == "dual"
                else "仅 GoPlus")
    guard = [
        "源码已验证 %s" % verified,
        "LP 已锁 %s" % ("发射台路径 / 锁定证明"
                        if safety.get("lp_unjudged_passes") else "必须已锁"),
        "无危险权限 0 项命中",
        "陷阱占比 < %s%%" % _num(safety.get("honeypot_max_pct")),
    ]
    factors = (scoring.get("onchain_factors") or list(FACTOR_SHORT))
    return [
        "硬过滤：" + " ｜ ".join(hard),
        "安全门槛：" + " ｜ ".join(guard),
        "评分 = 质量分 %s%%（%s）+ 链下调研分 %s%%（每轮全新调研）" % (
            _num(scoring.get("onchain_weight", 55)),
            "/".join(FACTOR_SHORT.get(f, f) for f in factors),
            _num(scoring.get("offchain_weight", 45)),
        ),
    ]
