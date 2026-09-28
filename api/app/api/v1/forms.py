"""The settings page as a form model.

The UI has no knowledge of filters, weights or schedules: it renders whatever widgets
this module describes. Adding a rule is therefore a backend change, and a knob that
shows up on screen always has a real rule behind it - never a decorative input.

Every `FieldOut.key` is a dotted path into the settings payload, so `PUT /settings`
round-trips what the page shows. The frontend sends strings from inputs; the backend
coerces each leaf to the type of the current value (`config.coerce_into`).
"""

from __future__ import annotations

from typing import Any

from ...core import funnel

SCAN_SOURCES = ("gmgn", "radardex", "tolly", "geckoterminal")
# Display order for the credential row: scanners first, then the verification sources.
SOURCE_ORDER = ["gmgn", "radardex", "tolly", "geckoterminal", "blockscout", "goplus", "sourcify"]

SOURCE_SHORT = {
    "gmgn": "GMGN",
    "radardex": "RadarDEX",
    "tolly": "Tolly",
    "geckoterminal": "GT",
    "blockscout": "Blockscout",
    "goplus": "GoPlus",
    "sourcify": "Sourcify",
}
SOURCE_LABELS = {
    "gmgn": "GMGN 活跃榜",
    "radardex": "RadarDEX 发射台",
    "tolly": "Tolly 全链榜",
    "geckoterminal": "GeckoTerminal 池",
    "blockscout": "Blockscout 全链底表",
    "goplus": "GoPlus",
    "sourcify": "Sourcify",
}
FACTOR_LABELS = {
    "liquidity": ("流动性", "池深分位"),
    "volume_24h": ("24h 成交", "成交额分位"),
    "holders": ("持有人", "地址数分位"),
    "top10_concentration": ("集中度", "Top10 反向分位"),
    "bundling": ("捆绑", "捆绑率反向分位"),
}
FACTOR_SHORT = {
    "liquidity": "流动性",
    "volume_24h": "成交",
    "holders": "持有人",
    "top10_concentration": "集中度",
    "bundling": "捆绑",
}
PUSH_CONTENT = {
    "cards": "报告卡",
    "table": "对比表",
    "links": "原链接",
}
PUSH_TARGETS = {"telegram": "Telegram", "discord": "Discord", "none": "仅落库不推"}
INTERVAL_CHOICES = (1, 2, 3, 4, 6, 12, 24)
#: The five 0-5 judgements the off-chain block is computed from, with the anchor each
#: one is scored against. Read-only rows: nothing here is a knob.
OFFCHAIN_LABELS = {
    "f_completeness": ("产品完成度", "0–5 · 有界面 / 能交互 / 收费并迭代"),
    "f_substance": ("项目实质与亮点", "0–5 · 纯 meme 到机制+差异化+独家资源"),
    "f_team": ("团队与 dev 可信度", "0–5 · 只奖可追溯；批量建币一票降档"),
    "f_narrative": ("叙事与官方关系", "0–5 · 离开 Arc 跑不起来 = 5"),
    "f_community": ("社区活跃度", "0–5 · 互动率与持续性，粉丝数无效"),
}
ALERT_LABELS = {
    "rename_count": "X 改名次数",
    "creator_token_count": "X 建币数量",
    "duplicate_symbol": "同名合约并行",
    "missing_socials": "官网漏收录 X/TG",
}


def _field(key: str, label: str, widget: str, value: Any = None, **kw: Any) -> dict:
    out = {"key": key, "label": label, "widget": widget, "value": value}
    out.setdefault("options", [])
    out.setdefault("checks", [])
    out.update(kw)
    return out


def _options(pairs: list[tuple[str, str]]) -> list[dict]:
    return [{"value": value, "label": label} for value, label in pairs]


def _checks(values: list[str], labels: dict[str, str]) -> list[dict]:
    return [{"value": v, "label": labels.get(v, v)} for v in values]


def build(
    chain: dict,
    totals: dict,
    rule_counts: dict[str, int],
    run_stages: list[dict] | None = None,
) -> dict:
    """Compose the settings form for one chain. Pure: everything arrives as arguments."""
    pipeline = chain.get("pipeline") or {}
    prefilter = pipeline.get("prefilter") or {}
    safety = pipeline.get("safety") or {}
    scoring = pipeline.get("scoring") or {}
    schedule = chain.get("schedule") or {}
    report = chain.get("report") or {}
    alerts = chain.get("alerts") or {}
    factor_weights = scoring.get("onchain_factor_weights") or {}

    def drops(*reasons: str) -> int | None:
        values = [rule_counts.get(r) for r in reasons if rule_counts.get(r) is not None]
        return sum(values) if values else None

    hard_fields = [
        _field(
            "pipeline.prefilter.market_cap_min_usd", "市值", "range",
            "%s,%s" % (prefilter.get("market_cap_min_usd", 0),
                       prefilter.get("market_cap_max_usd", 0)),
            unit="$",
            key2="pipeline.prefilter.market_cap_max_usd", drop_count=drops("market_cap"),
        ),
        _field(
            "pipeline.prefilter.liquidity_min_usd", "流动性", "num",
            str(prefilter.get("liquidity_min_usd", "")), op="≥", unit="$",
            drop_count=drops("liquidity"),
        ),
        _field(
            "pipeline.prefilter.holders_min", "持有人", "range",
            "%s,%s" % (_blank(prefilter.get("holders_min")),
                       _blank(prefilter.get("holders_max"))),
            unit="个",
            key2="pipeline.prefilter.holders_max", drop_count=drops("holders"),
        ),
        _field(
            "pipeline.prefilter.top10_max_pct", "Top10 集中度", "num",
            str(prefilter.get("top10_max_pct", "")), op="≤", unit="%",
            drop_count=drops("top10"),
        ),
        _field(
            "pipeline.prefilter.bundling_max_pct", "捆绑率", "num",
            str(prefilter.get("bundling_max_pct", "")), op="≤", unit="%",
            drop_count=drops("bundling"),
        ),
        _field(
            "pipeline.prefilter.forbid_honeypot", "貔貅", "bool",
            bool(prefilter.get("forbid_honeypot")), check_label="检测到即淘汰",
            drop_count=drops("honeypot"),
        ),
        _field(
            "pipeline.prefilter.volume_24h_min_usd", "24h 成交", "num",
            str(prefilter.get("volume_24h_min_usd", "")), op="≥", unit="$",
            drop_count=drops("volume_24h"),
        ),
        _field(
            "pipeline.prefilter.require_market_data", "空壳币", "bool",
            bool(prefilter.get("require_market_data")), check_label="无市值且无流动性即淘汰",
        ),
        _field(
            "pipeline.prefilter.min_scored_fields", "零数据淘汰", "num",
            str(prefilter.get("min_scored_fields", "")), op="≥", unit="项",
            drop_count=drops("missing_data"),
        ),
        _field(
            "pipeline.prefilter.drawdown_max_pct", "距高回撤", "num",
            str(prefilter.get("drawdown_max_pct", "")), op="≤", unit="%",
            check_label="源高点 / 库记录高点 / 池面全历史，任一跌够即淘汰",
            drop_count=drops("market_cap_drawdown"),
        ),
    ]

    safety_fields = [
        _field(
            "pipeline.safety.source_verification_mode", "源码已验证", "select",
            safety.get("source_verification_mode", "dual"),
            options=_options([("dual", "双源 GoPlus + Sourcify"), ("goplus_only", "仅 GoPlus")]),
            drop_count=drops("source_unverified"),
        ),
        _field(
            "pipeline.safety.lp_unjudged_passes", "LP 已锁", "select",
            bool(safety.get("lp_unjudged_passes", False)),
            options=_options([("false", "必须已锁"), ("true", "未判定按通过")]),
            drop_count=drops("lp_unlocked"),
            warn=totals.get("lp_unjudged_warn") or "",
        ),
        _field(
            "pipeline.safety.forbid_dangerous_perms", "无危险权限", "checks",
            list(safety.get("forbid_dangerous_perms") or []),
            checks=_checks(
                ["hidden_owner", "owner_change_balance", "can_take_back_ownership"],
                {"hidden_owner": "隐藏所有者",
                 "owner_change_balance": "可改持有人余额",
                 "can_take_back_ownership": "可夺回所有权"},
            ),
            # 提示项照常读、卡片上照常显示，只是不淘汰 —— 门槛只留真能卷走资产的三条。
            warn="提示不淘汰：mintable、blacklist、pausable、is_proxy",
            drop_count=drops("dangerous_perm"),
        ),
        _field(
            "pipeline.safety.honeypot_max_pct", "陷阱占比", "num",
            str(safety.get("honeypot_max_pct", "")), op="<", unit="%",
            drop_count=drops("entrapment"),
        ),
    ]

    quality_fields = [
        _field(
            "pipeline.scoring.onchain_weight", "质量分权重", "num",
            str(scoring.get("onchain_weight", 55)), unit="%", dim="占综合分", dim_in="label",
        )
    ]
    for factor, (label, how) in FACTOR_LABELS.items():
        if factor not in (scoring.get("onchain_factors") or []):
            continue
        quality_fields.append(
            _field(
                "pipeline.scoring.onchain_factor_weights.%s" % factor, label, "num",
                str(factor_weights.get(factor, "")), unit="%", dim=how, dim_in="label",
            )
        )
    quality_fields.append(_field("pipeline.scoring.factor_sum", "合计", "sum", "100", unit="%"))

    offchain_fields = [
        _field("pipeline.scoring.offchain_weight", "链下调研权重", "num",
               str(scoring.get("offchain_weight", 45)), unit="%", dim="占综合分",
               dim_in="label"),
        _field("pipeline.scoring.source", "数据来源", "ro", "每轮全新调研"),
    ] + [
        _field("pipeline.scoring.%s" % key, label, "ro", anchor)
        for key, (label, anchor) in OFFCHAIN_LABELS.items()
    ]

    interval = int(schedule.get("interval_hours") or 3)
    slots = interval_slots(interval)
    output_fields = [
        _field("report.per_round", "每轮报告数", "num", str(report.get("per_round", 5)), unit="份",
               drop_x="下轮生效"),
        _field("pipeline.gate_limit", "判定候选数", "num",
               str(pipeline.get("gate_limit", 20)), op="≤", unit="个",
               dim="按流动性取前 N；0 = 判完整个幸存者池", dim_in="field",
               drop_x="下轮生效"),
        _field("schedule.interval_hours", "间隔", "select", str(interval),
               options=_options([(str(h), "%dh" % h) for h in INTERVAL_CHOICES]),
               drop_x="下轮生效"),
        _field("schedule.cron", "触发时刻", "derived", "/".join("%02d" % s for s in slots),
               dim="由间隔生成", dim_in="field"),
        _field("schedule.push_delay_minutes", "推送延迟", "num",
               str(schedule.get("push_delay_minutes", 6)), op="+", unit="分钟",
               dim="发布后等待", dim_in="field", drop_x="下轮生效"),
        _field("pipeline.scan_sources", "扫描源", "checks",
               list(pipeline.get("scan_sources") or []),
               checks=_source_checks(pipeline.get("scan_sources") or [], totals.get("sources") or []),
               drop_count=totals.get("universe") or None, drop_prefix=""),
    ]

    alert_fields = []
    for key, label in ALERT_LABELS.items():
        if key == "rename_count":
            alert_fields.append(
                _field("alerts.rename_count_min", label, "num",
                       str(alerts.get("rename_count_min", 2)), op="≥", unit="次",
                       dim=None, enabled_key="alerts.rename_count", drop_x="下轮生效",
                       enabled=bool(alerts.get("rename_count", True)))
            )
        else:
            alert_fields.append(
                _field("alerts.%s" % key, label, "bool", bool(alerts.get(key, True)),
                       check_label="开启", drop_x="下轮生效")
            )

    push_fields = [
        _field("report.push_target", "发送目标", "select",
               report.get("push_target", "telegram"),
               options=_options([(v, l) for v, l in PUSH_TARGETS.items()]),
               drop_x="下轮生效"),
        _field("report.push_content", "推送内容", "checks",
               list(report.get("push_content") or []),
               checks=_checks(
                   ["cards", "table", "links"],
                   {**PUSH_CONTENT, "cards": f"{totals.get('selected') or 0} 张报告卡"},
               ),
               drop_x="下轮生效"),
        _field("runtime.status", "运行状态", "ro", totals.get("run_status") or "—",
               ro_row=True),
        _field("runtime.sources", "数据源凭据", "ro",
               " · ".join(
                   SOURCE_SHORT.get(name, name)
                   for name in sorted(
                       chain.get("sources") or [],
                       key=lambda name: SOURCE_ORDER.index(name)
                       if name in SOURCE_ORDER else len(SOURCE_ORDER),
                   )
               ) or "—",
               ro_row=True, ro_value=True,
               ro_button="重新授权",
               ro_button_note="尚未接入：目前只能读凭据清单，授权流程还没做"),
        _field("runtime.copy", "复制参数", "ro",
               ("从「%s」复制" % totals.get("copy_from")) if totals.get("copy_from") else "没有其他链可复制",
               ro_row=True, ro_value=True,
               ro_button="复制",
               ro_button_note="尚未接入：跨链复制参数还没做"),
    ]

    blocks = [
        {
            "key": "onchain",
            "title": "链上质量",
            "weight": "占 %s%%" % scoring.get("onchain_weight", 55),
            "columns": 3,
            "groups": [
                {
                    "key": "hard", "title": "硬过滤", "hint": "",
                    "fields": hard_fields,
                    "note": (
                        "漏斗：扫描 {universe:,} → 淘汰 {dropped:,}"
                        " → 候选 {candidates} → 入选 {selected}"
                    ).format(
                        universe=totals.get("universe", 0),
                        dropped=max(0, totals.get("universe", 0) - totals.get("candidates", 0)),
                        candidates=totals.get("candidates", 0),
                        selected=totals.get("selected", 0),
                    ),
                    "detail": (
                        "不满足直接淘汰，不进后面的评分。\n"
                        "每条右侧是本轮被该条淘汰的数量；"
                        "一旦改动，旧计数作废并标「待重算」。"
                    ),
                },
                {
                    "key": "safety", "title": "安全门槛", "hint": "",
                    "fields": safety_fields,
                    "note": "",
                    "detail": (
                        "必判：没有「不判就放过」这一档，不合格同样淘汰。\n"
                        "门槛只切换判定口径，不提供整条关闭"
                        " —— 硬线变软线必须是显式动作，"
                        "不能悄悄生效。"
                    ),
                    "warn": totals.get("safety_warn") or "",
                },
                {
                    "key": "quality", "title": "质量分构成", "hint": "",
                    "fields": quality_fields,
                    "note": "",
                    "detail": (
                        "5 项各自归一后加权求和 = 质量分，"
                        "权重合计必须等于 100%。\n"
                        "质量分权重与链下调研权重互补，"
                        "合计 100%；改一边另一边跟着走。"
                    ),
                },
            ],
        },
        {
            "key": "offchain",
            "title": "链下调研",
            "weight": "占 %s%%" % scoring.get("offchain_weight", 45),
            "columns": 2,
            "groups": [
                {
                    "key": "scoring", "title": "评分口径", "hint": "",
                    "fields": offchain_fields,
                    "note": "",
                    "detail": (
                        "和链上那 %s%% 的根本区别：这一项"
                        "抓不到，只能读官网／X／GitHub 之后下判断。 "
                        "每轮都是一次全新的调研，不沿用上一轮的结论——"
                        "判断和它依据的事实永远是同一份。"
                    ) % scoring.get("onchain_weight", 55),
                },
            ],
        },
        {
            "key": "run",
            "title": "运行",
            "weight": "不进分",
            "avg": totals.get("last_batch") or "",
            "columns": 3,
            "groups": [
                {
                    "key": "output", "title": "产出与节奏", "hint": "",
                    "fields": output_fields, "note": "",
                },
                {
                    "key": "alerts", "title": "提醒", "hint": "",
                    "fields": alert_fields, "note": "",
                    "detail": "只看阈值有没有踩线，不进分、也不淘汰。",
                },
                {
                    # The read-only banner below is a `sub` row inside push_fields, so this
                    # group carries no trailing note: rendering both printed it twice.
                    "key": "push", "title": "推送与依赖", "hint": "",
                    "fields": push_fields, "note": "",
                    "detail": (
                        "Telegram 没配就跳过推送、报告照常落库，"
                        "不会让这一轮算失败。"
                    ),
                },
            ],
        },
    ]
    return {"blocks": blocks, "stages": run_stages or []}


def interval_slots(hours: int) -> list[int]:
    """The trigger hours an interval produces, anchored at 02:00 like the live schedule."""
    if hours <= 0 or 24 % hours:
        return [2, 5, 8, 11, 14, 17, 20, 23]
    return [h % 24 for h in range(2, 26, hours)]



def _source_checks(enabled: list[str], counts: list[dict]) -> list[dict]:
    """Only the scanners. GoPlus and Sourcify verify safety, they do not scan."""
    by_name = {c.get("name"): c.get("count") for c in counts}
    out = []
    for name in SCAN_SOURCES:
        count = by_name.get(name)
        out.append({
            "value": name,
            "label": SOURCE_LABELS[name],
            "count": int(count) if count is not None else None,
        })
    return out


def _stamp(value: Any) -> str:
    """A stored UTC instant, short enough for a read-only row."""
    if not value:
        return "—"
    return str(value)[:16].replace("T", " ")


def _blank(value: Any) -> str:
    """A range input's two ends as strings, with a blank for "no bound".

    A range widget puts both ends in one comma-joined string, so a None has to arrive as
    an empty box rather than the word "None" - an unset bound is a legal answer here.
    """
    if value is None:
        return ""
    return "%g" % float(value) if isinstance(value, (int, float)) else str(value)


def _num(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return "%g" % float(value)
    except (TypeError, ValueError):
        return str(value)


def _usd(value: Any) -> str:
    """$0 / $8K / $200K / $1.2M - the shorthand the口径 line speaks in."""
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


def caliber_text(chain: dict) -> list[str]:
    """The round口径 as the three lines the overview shows.

    Built from the settings the round ran under, never from the report text, so the
    overview and the settings page cannot drift apart.
    """
    pipeline = chain.get("pipeline") or {}
    prefilter = pipeline.get("prefilter") or {}
    safety = pipeline.get("safety") or {}
    scoring = pipeline.get("scoring") or {}
    hard = [
        "市值 %s – %s" % (_usd(prefilter.get("market_cap_min_usd", 0)),
                          _usd(prefilter.get("market_cap_max_usd"))),
        "流动性 ≥ %s" % _usd(prefilter.get("liquidity_min_usd")),
        ("持有人 %s – %s" % (_num(prefilter.get("holders_min")),
                            _num(prefilter.get("holders_max")))
         if prefilter.get("holders_max") is not None
         else "持有人 ≥ %s" % _num(prefilter.get("holders_min"))),
        "Top10 集中度 ≤ %s%%" % _num(prefilter.get("top10_max_pct")),
        "捆绑率 ≤ %s%%" % _num(prefilter.get("bundling_max_pct")),
        "貔貅 = 0" if prefilter.get("forbid_honeypot") else "貔貅 不设门槛",
        "24h 成交 ≥ %s" % _usd(prefilter.get("volume_24h_min_usd")),
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


def rule_label(reason: str) -> str:
    """The reader's words for one drop rule. The words themselves live in
    `core/funnel.RULE_LABEL`, beside the rule order, because the change bar's 退出
    reason now prints them too - one dict, two readers, so they cannot drift apart."""
    return funnel.RULE_LABEL.get(reason, reason)
