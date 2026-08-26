"""The settings pages as a form model - a chain's口径, and this machine's own wiring.

The UI has no knowledge of filters, weights or schedules: it renders whatever widgets
this module describes. Adding a rule is therefore a backend change, and a knob that
shows up on screen always has a real rule behind it - never a decorative input.

Every `FieldOut.key` is a dotted path into the settings payload, so `PUT /settings`
round-trips what the page shows. The frontend sends strings from inputs; the backend
coerces each leaf to the type of the current value (`config.coerce_into`).
"""

from __future__ import annotations

from ...opportunity import heat, schema

from typing import Any

from ... import config
from ...core import funnel

# Display order for the credential row: scanners first, then the verification sources.
# One list for every chain - the checkbox row reads the chain's own `scan_sources`, so a
# scanner that is not in a chain's config simply never appears on that chain's page.
SOURCE_ORDER = [
    "gmgn", "radardex", "tolly", "coinbarrel", "pez", "peeps",
    "pons", "launchlab", "pumpfun", "fourmeme",
    "geckoterminal", "dexscreener_rank", "blockscout", "goplus", "sourcify",
]

SOURCE_SHORT = {
    "gmgn": "GMGN",
    "radardex": "RadarDEX",
    "tolly": "Tolly",
    "coinbarrel": "CoinBarrel",
    "pez": "PEZ",
    "peeps": "Peeps",
    "pons": "PONS",
    "launchlab": "LaunchLab",
    "pumpfun": "pump.fun",
    "fourmeme": "four.meme",
    "geckoterminal": "GT",
    "dexscreener_rank": "DexScreener",
    "blockscout": "Blockscout",
    "goplus": "GoPlus",
    "sourcify": "Sourcify",
}
SOURCE_LABELS = {
    "gmgn": "GMGN 活跃榜",
    "radardex": "RadarDEX 发射台",
    "tolly": "Tolly 全链榜",
    "coinbarrel": "CoinBarrel 全链榜",
    "pez": "PEZ 发射台",
    "peeps": "Peeps 发射台",
    "pons": "PONS 发射台",
    "launchlab": "Raydium LaunchLab 发射台",
    "pumpfun": "pump.fun 发射台",
    "fourmeme": "four.meme 发射台",
    "geckoterminal": "GeckoTerminal 池",
    "dexscreener_rank": "DexScreener 榜单",
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
PUSH_CONTENT = {
    "cards": "报告卡",
    "table": "对比表",
    "links": "原链接",
}
PUSH_TARGETS = {"telegram": "Telegram", "discord": "Discord", "none": "仅落库不推"}
INTERVAL_CHOICES = (1, 2, 3, 4, 6, 12, 24)
#: The five 0-5 judgements the off-chain block is computed from, with the anchor each one
#: is scored against. The anchors are 口径, not knobs: they ride in the group's 说明, and
#: the weights that split the block live in `pipeline.scoring.offchain_factor_weights`.
#: The narrative anchor names a chain, and the page must name its own: 「核心业务就是 Arc 的主线 = 5」
#: printed on the Robinhood page scores the wrong chain's question. Substituted per page from
#: `config.chain_name` - the chain's own name, not the radar's label (user, 2026-09-30).
OFFCHAIN_LABELS = {
    "f_completeness": ("产品完成度", "0–5 · 有界面 / 能交互 / 收费并迭代"),
    "f_substance": ("项目实质与亮点", "0–5 · 纯 meme 到机制+差异化+独家资源"),
    "f_team": ("团队与 dev 可信度", "0–5 · 只奖可追溯；批量建币一票降档"),
    "f_narrative": ("叙事与主线", "0–5 · 核心业务就是 %s 的主线 = 5"),
    "f_community": ("社区活跃度", "0–5 · 互动率与持续性，粉丝数无效"),
}


def _offchain_labels(chain: dict) -> dict[str, tuple[str, str]]:
    """The five 0-5 judgements, with this chain's own name in the narrative anchor.

    One list for every chain, and exactly one entry in it names a chain - so the name is
    filled in here rather than written into the constant, and a chain that never set a
    `name` still prints its label.
    """
    label, anchor = OFFCHAIN_LABELS["f_narrative"]
    return {**OFFCHAIN_LABELS, "f_narrative": (label, anchor % config.chain_name(chain))}


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


def _drop_reading(fields: list[dict]) -> str:
    """This round's eliminations, per rule, on the group note.

    The counts used to ride in a trailing column. A column of numbers reads as data
    even when every row in it is a rule, so each count travels with the definition of
    the rule that produced it instead of standing beside the knob.
    """
    parts = [
        "%s -%s" % (field["label"], format(field["drop_count"], ","))
        for field in fields
        if field.get("drop_count")
    ]
    return "本轮淘汰：" + (" · ".join(parts) if parts else "无")

def _scan_reading(checks: list[dict], universe: int) -> str:
    """What each scanner contributed this round, for the group note.

    The counts used to ride on the four checkboxes and one more beside the label. The row
    is the switches now; the round's own numbers read in one place.
    """
    parts = [
        "%s %s" % (check["label"], format(check["count"], ","))
        for check in checks
        if check.get("count")
    ]
    if not parts:
        return ""
    line = "本轮扫描源：" + " · ".join(parts)
    if universe:
        line += "；去重后进入漏斗 %s 个" % format(universe, ",")
    return line + "。"


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
    # 可增发 / 可冻结 are answerable only where the mint read is configured
    # (`sources/solana/mint.py`), so the two checkboxes are offered exactly there: a
    # permission the chain cannot answer is a checkbox that can never fire. The EVM three
    # stay on every chain, because GoPlus - the source that answers them - is on every chain.
    solana_perms = (
        ["mint_authority", "freeze_authority"]
        if "solana_mint" in (chain.get("sources") or []) else []
    )
    schedule = chain.get("schedule") or {}
    report = chain.get("report") or {}
    factor_weights = scoring.get("onchain_factor_weights") or {}
    offchain_weights = scoring.get("offchain_factor_weights") or {}

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
            drop_count=drops("empty_shell"),
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
                ["hidden_owner", "owner_change_balance", "can_take_back_ownership"] + solana_perms,
                {"hidden_owner": "隐藏所有者",
                 "owner_change_balance": "可改持有人余额",
                 "can_take_back_ownership": "可夺回所有权",
                 # Solana 的两条读的是 mint 账户里的两个权限（sources/solana/mint.py），EVM 的
                 # 三条读的是合约字节码。同一条规则、同一行，因为它们回答的是同一个问题：
                 # 持有人之外，还有谁能动这个币。
                 "mint_authority": "可增发（铸造权未放弃）",
                 "freeze_authority": "可冻结（冻结权未放弃）"},
            ),
            # 提示项照常读、卡片上照常显示，只是不淘汰 —— 门槛只留真能卷走资产的三条。
            drop_count=drops("dangerous_perm"),
        ),
        _field(
            "pipeline.safety.honeypot_max_pct", "陷阱占比", "num",
            str(safety.get("honeypot_max_pct", "")), op="<", unit="%",
            drop_count=drops("entrapment"),
        ),
    ]

    # The two judgement groups read their eliminations from the group note now, so the
    # counts leave the rows: a row keeps only the knob, the reading moves behind the icon.
    hard_drops = _drop_reading(hard_fields)
    safety_drops = _drop_reading(safety_fields)
    for field in hard_fields + safety_fields:
        field["drop_count"] = None

    quality_fields = [
        _field(
            "pipeline.scoring.onchain_weight", "链上质量权重", "num",
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

    # The five rows are weights, not readings, so they are drawn like the on-chain block's
    # factors: a knob per judgement, the anchor it is scored against folded into the 说明.
    offchain_fields = [
        _field("pipeline.scoring.offchain_weight", "链下调研权重", "num",
               str(scoring.get("offchain_weight", 45)), unit="%", dim="占综合分",
               dim_in="label"),
    ] + [
        _field("pipeline.scoring.offchain_factor_weights.%s" % key, label, "num",
               str(offchain_weights.get(key, "")), unit="%")
        for key, (label, _anchor) in _offchain_labels(chain).items()
    ]

    interval = int(schedule.get("interval_hours") or 0)
    start = schedule.get("start_hour")
    start = config.DEFAULT_START_HOUR if start is None else int(start)
    slots = config.interval_slots(interval, start)
    scan_checks = _source_checks(pipeline.get("scan_sources") or [], totals.get("sources") or [])
    scan_reading = _scan_reading(scan_checks, totals.get("universe") or 0)
    for check in scan_checks:
        check["count"] = None
    output_fields = [
        _field("report.per_round", "每轮报告数", "num", str(report.get("per_round", 5)), unit="份",
               drop_x="下轮生效"),
        _field("pipeline.gate_limit", "判定候选数", "num",
               str(pipeline.get("gate_limit", 0)), op="≤", unit="个",
               drop_x="下轮生效"),
        _field("pipeline.research_limit", "调研上限", "num",
               str(pipeline.get("research_limit", 20)), op="≤", unit="个",
               drop_x="下轮生效"),
        _field("schedule.interval_hours", "间隔", "select", str(interval),
               options=_options([(str(h), "%dh" % h) for h in INTERVAL_CHOICES]
                                + [("0", "不排期")]),
               drop_x="下轮生效"),
        _field("schedule.start_hour", "起始时刻", "select", str(start),
               options=_options([(str(h), "%02d:00" % h) for h in range(24)]),
               drop_x="下轮生效"),
        _field("schedule.slots", "触发时刻", "derived",
               "/".join("%02d" % s for s in slots) if slots else "不排期"),
        _field("schedule.push_delay_minutes", "推送延迟", "num",
               str(schedule.get("push_delay_minutes", 6)), op="+", unit="分钟",
               drop_x="下轮生效"),
        _field("pipeline.scan_sources", "扫描源", "checks",
               list(pipeline.get("scan_sources") or []),
               checks=scan_checks),
    ]

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
               ro_row=True, ro_value=True),
    ]

    blocks = [
        {
            "key": "onchain",
            "title": "链上质量",
            "columns": 3,
            "groups": [
                {
                    "key": "quality", "title": "评分口径", "hint": "",
                    "fields": quality_fields,
                    "note": "",
                    "detail": (
                        "链上质量占综合分 %s%%。\n" % scoring.get("onchain_weight", 55)
                        + "5 项各自归一后加权求和 = 质量分，"
                        "五项权重合计需等于 100%。\n"
                        "链上质量权重与链下调研权重互补，"
                        "合计 100%；改一边另一边跟着走。"
                    ),
                },
                {
                    "key": "hard", "title": "硬过滤", "hint": "",
                    "fields": hard_fields,
                    "note": "",
                    "detail": (
                        "漏斗：扫描 {universe:,} → 淘汰 {dropped:,}"
                        " → 候选 {candidates} → 入选 {selected}。\n"
                        "不满足直接淘汰，不进后面的评分。\n"
                        + hard_drops + "。\n"
                        "改动后旧计数作废，重跑本轮才作数。"
                    ).format(
                        universe=totals.get("universe", 0),
                        dropped=max(0, totals.get("universe", 0) - totals.get("candidates", 0)),
                        candidates=totals.get("candidates", 0),
                        selected=totals.get("selected", 0),
                    ),
                },
                {
                    "key": "safety", "title": "安全门槛", "hint": "",
                    "fields": safety_fields,
                    "note": "",
                    "detail": "\n".join(
                        line
                        for line in (
                            "必判：没有「不判就放过」这一档，不合格同样淘汰。",
                            "门槛只切换判定口径，不提供整条关闭"
                            " —— 硬线变软线必须是显式动作，不能悄悄生效。",
                            "提示不淘汰：mintable、blacklist、pausable、is_proxy。",
                            safety_drops + "。",
                            totals.get("safety_warn") or "",
                        )
                        if line
                    ),
                },
            ],
        },
        {
            "key": "offchain",
            "title": "链下调研",
            "columns": 2,
            "groups": [
                {
                    "key": "scoring", "title": "评分口径", "hint": "",
                    "fields": offchain_fields,
                    "note": "",
                    # The prepended share is formatted on its own: an outer %-format would
                    # run over the "45%" it just produced and choke on the stray percent.
                    "detail": "\n".join(
                        [
                            "链下调研占综合分 %s%%。" % scoring.get("offchain_weight", 45),
                            "数据来源：每轮全新调研 —— 判断和它依据的事实永远是同一份，"
                            "不沿用上一轮的结论。",
                            "和链上那 %s%% 的根本区别：这一项抓不到，"
                            "只能读官网／X／GitHub 之后下判断。"
                            % scoring.get("onchain_weight", 55),
                            "五个 0–5 判断（权重默认各 20%）：",
                        ]
                        + ["%s %s" % (label, anchor)
                           for label, anchor in _offchain_labels(chain).values()]
                    ),
                },
            ],
        },
        {
            "key": "run",
            "title": "运行",
            "columns": 3,
            "groups": [
                {
                    "key": "output", "title": "产出与节奏", "hint": "",
                    "fields": output_fields, "note": "",
                    "detail": "\n".join(
                        line
                        for line in (
                            scan_reading,
                            "判定候选数 0 = 判完整个幸存者池；调研上限按流动性取前 N 个过关候选做链下调研，0 = 全部调研。",
                            "触发时刻由起始时刻和间隔算出；推送延迟 = 报告发布后等待的分钟数。",
                        )
                        if line
                    ),
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


def field_labels(chain: dict) -> dict[str, str]:
    """Every dotted key the settings page shows -> the words it prints for that box.

    Read when the API refuses a save: a refusal has to name the box the operator can see,
    never a dotted path. Composed from the same `build` the page renders, so the answer and
    the row above it cannot drift apart (CLAUDE.md · 设计取舍). A range is one label over two boxes, so
    its two ends are told apart as 下限 / 上限 - "「市值」不能留空" would not say which.
    """
    labels: dict[str, str] = {}
    for block in build(chain, {}, {})["blocks"]:
        for group in block["groups"]:
            for field in group["fields"]:
                if field.get("widget") == "range" and field.get("key2"):
                    labels[field["key"]] = field["label"] + "下限"
                    labels[field["key2"]] = field["label"] + "上限"
                else:
                    labels[field["key"]] = field["label"]
    return labels



def _source_checks(enabled: list[str], counts: list[dict]) -> list[dict]:
    """The scanners this chain's own config lists, in the config's own order.

    The list is read from `scan_sources` rather than kept here as a constant: Arc scans
    RadarDEX and Tolly, Robinhood scans its launchpads, and a constant would offer one
    chain's sources on the other chain's page. GoPlus and Sourcify verify safety, they
    never scan, so they are not in `scan_sources` to begin with.
    """
    by_name = {c.get("name"): c.get("count") for c in counts}
    out = []
    for name in enabled:
        count = by_name.get(name)
        out.append({
            "value": name,
            "label": SOURCE_LABELS.get(name, name),
            "count": int(count) if count is not None else None,
        })
    return out


def _blank(value: Any) -> str:
    """A range input's two ends as strings, with a blank for "no bound".

    A range widget puts both ends in one comma-joined string, so a None has to arrive as
    an empty box rather than the word "None" - an unset bound is a legal answer here.
    """
    if value is None:
        return ""
    return "%g" % float(value) if isinstance(value, (int, float)) else str(value)


def rule_label(reason: str) -> str:
    """The reader's words for one drop rule. The words themselves live in
    `core/funnel.RULE_LABEL`, beside the rule order, because the change bar's 退出
    reason now prints them too - one dict, two readers, so they cannot drift apart."""
    return funnel.RULE_LABEL.get(reason, reason)


#: 系统设置 - this machine's own wiring (`config/system.yaml`), which is not a chain's口径.
#: The labels live here with every other sentence a page prints (CLAUDE.md · 设计取舍); the values
#: arrive from `system_settings.state()`, so the page and the loader cannot drift - both walk
#: `config.SYSTEM_KEYS`. A row's `dim` says where its value came from, and it says it only when
#: that is not the file: a variable the shell exported beats the file, so that row is a reading
#: and not a box, and a value nobody set is the code's own default. A row the file itself decides
#: stays blank - the file is the normal case, and a column of the same word on every row says
#: nothing.
SYSTEM_LABELS = {
    "llm.base_url": "网关地址",
    "llm.api_key": "API Key",
    "llm.api_key_fallback": "备用 Key",
    "llm.model": "模型",
    "llm.reasoning": "推理档位",
    "llm.max_tokens": "回答上限",
    "llm.timeout": "单次超时",
    "llm.tries": "重试次数",
    "llm.key_cooldown": "失效冷却",
    "research_workers": "调研并发",
    "x.auth_token": "auth_token",
    "x.ct0": "ct0",
    "x.cli": "搜索客户端",
    "log_level": "日志等级",
}

#: The file is the normal case and is not marked: a column of the same word on every row says
#: nothing, and it buries the rows that are worth looking at (user, 2026-10-02).
SYSTEM_SOURCE = {"file": "", "env": "环境变量", "unset": "默认"}

#: The endpoint's own default is the empty answer for both choices: leaving the row unset is a
#: real answer, and it is what every call did before the knob existed.
SYSTEM_REASONING = (("", "端点默认"), ("none", "不思考"), ("minimal", "minimal"),
                    ("low", "low"), ("medium", "medium"), ("high", "high"))
SYSTEM_LEVELS = (("", "默认（INFO）"), ("DEBUG", "DEBUG"), ("INFO", "INFO"),
                 ("WARNING", "WARNING"), ("ERROR", "ERROR"))

#: The gateways this install is most often pointed at, as a shortcut and not a whitelist:
#: picking one only types an address into a box that stays editable, because the endpoint
#: someone actually runs is as likely to be a machine on their own LAN as one of these.
SYSTEM_ENDPOINTS = (
    ("https://opencode.ai/zen/go/v1", "opencode zen"),
    ("https://api.openai.com/v1", "OpenAI"),
    ("https://api.deepseek.com/v1", "DeepSeek"),
)


def system_blocks(state: dict[str, dict]) -> list[dict]:
    """Compose the 系统设置 form from `system_settings.state()`. Pure: everything arrives."""

    def row(path: str, widget: str = "text", **kw: Any) -> dict:
        entry = state[path]
        label = SYSTEM_LABELS[path]
        source = SYSTEM_SOURCE[entry["source"]]
        if entry["kind"] == "secret":
            # Never the value itself: the row says which credential is stored, and a blank box
            # keeps it (the one thing a blank box cannot mean is "clear this key").
            # The box is drawn as a password field on the page, so typing one in is not for
            # whoever walks past the screen.
            stored = ("已配置 · " + entry["mask"]) if entry["set"] else "未配置"
            if entry["source"] == "env":
                return _field(path, label, "ro", value=stored, dim=source, dim_in="label")
            return _field(path, label, "password", value="", dim=stored, dim_in="field")
        if entry["source"] == "env":
            return _field(path, label, "ro", value=entry["value"], dim=source, dim_in="label")
        return _field(path, label, widget, value=entry["value"], dim=source, dim_in="label", **kw)

    def group(key: str, title: str, detail: str, fields: list[dict],
              actions: list[dict] | None = None) -> dict:
        return {"key": key, "title": title, "hint": "", "detail": detail, "fields": fields,
                "actions": actions or []}

    endpoint = group("llm-endpoint", "端点", (
        "网关、Key、模型。改完下次调用生效，不用重启。网关要的固定请求头由客户端"
        "按网关自动带，不用填。"
    ), [
        row("llm.base_url", presets=_options(list(SYSTEM_ENDPOINTS))),
        row("llm.api_key"),
        row("llm.api_key_fallback"),
        row("llm.model"),
    ], actions=[
        {"id": "models", "label": "获取模型"},
        {"id": "test", "label": "测试连接"},
    ])
    call = group("llm-call", "调用", (
        "推理档位只有「不思考」是真的关：minimal 与 low 一样会花思考 token。回答上限 0 = 不设，"
        "调小会让推理模型返回空消息（不是报错）。超时要高于真实回答耗时，它与重试相乘。"
        "调研并发是同时问几个币，网关开始 429 就先调小它。"
    ), [
        row("llm.reasoning", "select", options=_options(list(SYSTEM_REASONING))),
        row("llm.max_tokens", "num"),
        row("llm.timeout", "num", unit="秒"),
        row("llm.tries", "num"),
        row("llm.key_cooldown", "num", unit="秒"),
        row("research_workers", "num"),
    ])
    session = group("x-session", "X会话", (
        "两个值来自一个已登录 x.com 的会话 cookie（DevTools → Application → Cookies），"
        "用一个一次性账号：搜索是会被限流的读请求，429 会让币丢掉链接。"
        "「测试连接」用它读一次这个账号本身：一次请求，不占搜索额度。"
    ), [
        row("x.auth_token"),
        row("x.ct0"),
        row("x.cli"),
    ], actions=[
        {"id": "x", "label": "测试连接"},
    ])
    level = group("log-level", "等级", (
        "日志是唯一记录阶段耗时的地方，改完当场生效。"
    ), [
        row("log_level", "select", options=_options(list(SYSTEM_LEVELS))),
    ])
    return [
        {"key": "llm", "title": "模型", "columns": 1, "groups": [endpoint, call]},
        {"key": "x", "title": "搜索", "columns": 1, "groups": [session]},
        {"key": "log", "title": "日志", "columns": 1, "groups": [level]},
    ]


# ---------------------------------------------------------------- 新机会（页面文案）

#: The two lists the page draws, in the order it draws them. The key is the entry's `kind`.
#: The two kinds, as the panel's 类别 options are labelled. The two lists they used to title are
#: gone (one list + a status filter, 2026-10-18), and their hints went with them - a hint for a
#: section nothing renders is copy with no reader.
OPPORTUNITY_LISTS = (
    ("activity", "可参与活动"),
    ("project", "新项目协议"),
)

#: The trust states, in the reader's words. Three of them, and 未判定 is one: an entry nobody
#: could confirm must not read as either confirmed or refuted.
OPPORTUNITY_TRUST = {
    "official": {"label": "官方", "tone": "ok"},
    "community": {"label": "社区", "tone": "warn"},
    "unjudged": {"label": "未判定", "tone": "muted"},
}


#: 新机会的筛选面板：分组顺序、键名与它们读的字段。选项由数据决定（只有真的出现过的
#: 类目 / 门槛 / 生态才成为一行），标题与说法在这里，前端不识别任何一条。
OPPORTUNITY_STATUS = (("promoted", "可参与"), ("candidate", "未晋升"), ("retired", "已结束"))
#: 来源类型只有两种（2026-10-18 起 X 单源）：一个账号的时间线，或一条查询。
OPPORTUNITY_SOURCE_LABELS = {
    "official": "官方账号",
    "query": "查询",
}
#: 排序键与它的两个方向。**热度不在其中**：它是来源帖自己的读数，换一批源就会变，
#: 只做徽章与筛选（见 `api/app/opportunity/README.md` · 落地形状）。
OPPORTUNITY_SORTS = (
    ("discovered", "发现时间", (("desc", "从新到旧"), ("asc", "从旧到新"))),
    ("deadline", "截止时间", (("asc", "从早到晚"), ("desc", "从晚到早"))),
)


def _present(values, keep: tuple[str, ...] = ()) -> list[str]:
    """The options a group may offer: what the entries actually carry, in a fixed order.

    A group that offers a category nobody used is a switch that can only ever empty the page,
    which is the one kind of control this app does not ship. `keep` fixes the order where the
    vocabulary has one (the eight barriers), so the rows do not shuffle between reads.
    """
    seen = {str(value) for value in values if value}
    if keep:
        return [name for name in keep if name in seen]
    return sorted(seen)


def opportunity_filters(items: list[dict], counts: dict) -> list[dict]:
    """The panel: **生态, then 状态** - two groups, and that is the whole panel (2026-10-19, user).

    The chain is the axis a reader actually browses by ("BTC 生态有什么、Eth 生态有什么"), and
    状态 is the one question the page has to keep answerable: why is this not on the list. Every
    other group was noise - 类别 duplicated 类目, 核实 collapses once entries trace to an official
    home, and 类目 / 门槛 / 热度 / 来源 are all readings the card itself already prints.

    A group is only sent when it has at least two choices: 生态 with a single ecosystem present is
    not a choice, and a group that can only empty the page is the kind of control this app does
    not ship.
    """
    groups: list[dict] = []

    # 已知链按 `CHAINS` 的顺序，枚举外的新链排在它们后面（按字母），多链与未判定永远最后。
    ecosystems = schema.order_ecosystems(item.get("ecosystem") for item in items)
    if len(ecosystems) > 1:
        groups.append({"key": "ecosystem", "title": "生态", "options":
                       [{"value": "", "label": "全部"}] +
                       [{"value": name, "label": schema.ecosystem_label(name)}
                        for name in ecosystems]})

    status = [{"value": "", "label": "全部", "count": None}] + [
        {"value": key, "label": label,
         # `opportunity_counts` keys read `status:kind`, so a status' total is the sum of its
         # two kinds - which is also what makes 可参与 + 未晋升 + 已结束 equal the page's 条目.
         "count": sum(value for name, value in counts.items() if name.startswith(key + ":"))}
        for key, label in OPPORTUNITY_STATUS
    ]
    groups.append({"key": "status", "title": "状态", "options": status})
    return groups


def opportunity_sorts() -> list[dict]:
    return [{"value": value, "label": label, "dirs": list(dirs)}
            for value, label, dirs in OPPORTUNITY_SORTS]


def opportunity_form() -> dict:
    """The 新机会 page's own copy: what the page is, what the two lists mean, and the空态.

    Same rule as the settings page: the explanatory sentences live here and the frontend prints
    them without recognising any of them. Button labels stay on the page, the way the CA search
    page keeps its own - these are the sentences a reader has to be able to trust, and they have
    to be one copy.
    """
    return {
        "title": "新机会",
        # 只留页面真的会画的那些（2026-10-18）：页头没有说明段，两个列表与两个折叠区已被
        # 「一个列表 + 状态筛选」取代，所以 lists / pending_* / retired_* 都不再发。
        # 热度口径挂在徽章的 tooltip 上，不占一行字。
        "trust": OPPORTUNITY_TRUST,
        "sources_title": "watchlist",
        "sources_lead": ("扫描会读的东西：官方账号的时间线，以及查询行（每条查询按时间切片）。"
                         "种子来自 seeds/watchlist.yaml，之后以这张表为准。"),
        "empty": "还没有扫描过。点「开始扫描」跑一次。",
    }
