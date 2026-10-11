"""The fact sheet the writer is handed, which is the same sheet the card draws.

Why this module exists, in one measurement: the card rendered the snapshot through 30+
fields (`report/cards.py`), and the lexicon draft was handed eight of them - symbol, CA,
market cap, liquidity, 24h volume, holders, Top10, bundling. So the model wrote about the
security rows because the security rows were all it had, while the facts it needed (the fee,
the pool split, the burn, the price journey, the project’s own blurb) sat unread in the same
snapshot. This is not a prompt problem and it cannot be fixed by asking harder.

Order and labels follow the card: 行情 / 筹码 / 持有人前 10 / 供应 / 池 / 税 / 貔貅 / 安全 /
社媒. A field that is missing prints as 「—」 on purpose - "five sources were asked and none
had it" is itself a fact the writer may state, and dropping the line instead is how a blank
gets filled with something plausible.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..core.models import SafetyFacts, TokenMetrics
from ..core.parse import as_float as _num

NA = "—"
#: List sections are capped: the writer needs the shape of the distribution, not every row,
#: and a 400-holder table would push the facts it scores from out of the context window.
TOP_HOLDERS = 5
TOP_POOLS = 5
SERIES_POINTS = 8


def _val(source: Any, name: str) -> Any:
    """One reader for both shapes: a `TokenMetrics`/`SafetyFacts` model or a snapshot dict."""
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def money(value: Any) -> str:
    """USD, shortened the way the card shortens it - except below $1, where a price needs
    the digits that make it a price."""
    number = _num(value)
    if number is None:
        return NA
    if abs(number) >= 1e6:
        return "$%.2fM" % (number / 1e6)
    if abs(number) >= 1e3:
        return "$%.1fK" % (number / 1e3)
    if abs(number) >= 1:
        return "$%.2f" % number
    return "$%.8f" % number


def count(value: Any) -> str:
    number = _num(value)
    return NA if number is None else format(int(number), ",")


def pct(value: Any, digits: int = 1) -> str:
    number = _num(value)
    return NA if number is None else ("%.*f%%" % (digits, number))


def _tri(value: Any) -> str:
    """A yes / no / not-asked, for the fields where a blank must not read as a no."""
    if value is None:
        return "未判定"
    return "是" if value else "否"


#: Both shapes a sheet arrives in: the model off a live round, or the snapshot dict a later
#: pass reads back out of the database. `_val` is the one reader for the pair.
def safety_line(
    metrics: TokenMetrics | Mapping[str, Any],
    facts: SafetyFacts | Mapping[str, Any] | None = None,
) -> str:
    """源码 / 权限 / LP, on one line. The same verdict the card renders as icons."""
    verified = _val(facts, "source_verified")
    match = str(_val(facts, "source_match") or "").strip()
    if match.lower() in ("none", "-", NA):
        # `match: none` is a source saying "no match label to report", not a claim worth
        # printing next to a source’s name - it read as "GoPlus: none" on the safety line.
        match = ""
    if verified is True:
        by = "、".join(_val(facts, "source_verified_by") or []) or "已验证"
        source = "已验证（%s%s）" % (by, (": " + match) if match else "")
    elif verified is False:
        source = "未验证"
    else:
        source = "未判定"
    perms = _val(facts, "dangerous_perms")
    perms = ("、".join(perms) if perms else "无危险权限") if perms is not None else "未判定"
    locked = _val(facts, "lp_locked")
    # A locked LP needs no explanation beside it - the same rule the card follows, so the
    # writer phrases the lock the way the reader sees it.
    note = _val(facts, "lp_note") if locked is not True else None
    return "源码 %s ｜ 危险权限 %s ｜ LP %s%s" % (
        source, perms, {True: "已锁", False: "未锁", None: "未判定"}[locked],
        ("（%s）" % note) if note else "")


def _holders(rows: Any) -> str:
    """The largest holders, with what each seat is: 合约 / 烧毁 / 锁定."""
    out = []
    for row in (rows or [])[:TOP_HOLDERS]:
        if not isinstance(row, Mapping):
            continue
        marks = [label for label, key in (("合约", "contract"), ("烧毁", "burnt"), ("锁定", "locked")) if row.get(key)]
        tag = row.get("tag")
        if tag:
            marks.append(str(tag))
        addr = str(row.get("address") or "")
        out.append("%s %s%s" % (addr[:10], pct(row.get("pct"), 2),
                                 ("（%s）" % "、".join(marks)) if marks else ""))
    return " ｜ ".join(out) or NA


def _pools(rows: Any) -> str:
    out = []
    for row in (rows or [])[:TOP_POOLS]:
        if not isinstance(row, Mapping):
            continue
        name = row.get("name") or "池"
        kind = row.get("kind")
        fee = _num(row.get("fee_pct"))
        out.append("%s%s%s %s" % (name, (" %s" % kind) if kind else "",
                                   (" 费 %.4g%%" % fee) if fee is not None else "",
                                   money(row.get("liquidity_usd"))))
    return " ｜ ".join(out)


def _series(values: Any) -> str:
    points = [_num(v) for v in (values or [])]
    points = [v for v in points if v is not None]
    if not points:
        return NA
    shown = points[-SERIES_POINTS:]
    # Eight significant digits would be noise here: the shape is the message, not the ticks.
    return " → ".join("%.6g" % v for v in shown) + ("（共 %d 点，旧→新）" % len(points))


def lines(
    metrics: TokenMetrics | Mapping[str, Any],
    facts: SafetyFacts | Mapping[str, Any] | None = None,
) -> list[str]:
    """The whole sheet, one labelled line per section."""
    if metrics is None:
        return []
    ca = _val(metrics, "ca") or NA
    symbol = _val(metrics, "symbol") or NA
    out = ["【标的】%s %s" % (symbol, ca)]
    out.append("【行情】市值 %s ｜ 流动性 %s ｜ 24h 成交 %s ｜ 换手 %s ｜ 价格 %s"
               % (money(_val(metrics, "market_cap_usd")), money(_val(metrics, "liquidity_usd")),
                  money(_val(metrics, "volume_24h_usd")), pct(_val(metrics, "turnovers"), 2),
                  money(_val(metrics, "price_usd"))))
    out.append("【价格走势】%s ｜ 距 ATH %s（ATH %s）｜ 24h %s ｜ 开盘 %s"
               % (_series(_val(metrics, "price_series")), pct(_val(metrics, "drawdown_pct")),
                  money(_val(metrics, "price_ath_usd")), pct(_val(metrics, "price_change_24h_pct"), 2),
                  _val(metrics, "open_at") or NA))
    out.append("【筹码】持有人 %s ｜ Top10 %s ｜ 捆绑 %s ｜ 狙击 %s ｜ 老鼠仓 %s ｜ 机器人 %s ｜ 新钱包 %s ｜ 蓝筹 %s"
               % (count(_val(metrics, "holders")), pct(_val(metrics, "top10_pct")),
                  pct(_val(metrics, "bundling_pct")), pct(_val(metrics, "sniper_pct")),
                  pct(_val(metrics, "insider_pct")), pct(_val(metrics, "bot_degen_pct")),
                  pct(_val(metrics, "fresh_wallet_pct")), pct(_val(metrics, "bluechip_holder_pct"))))
    out.append("【持有人前 %d】%s" % (TOP_HOLDERS, _holders(_val(metrics, "top_holders"))))
    lp_rows = _val(metrics, "lp_holders") or []
    top = lp_rows[0] if lp_rows and isinstance(lp_rows[0], Mapping) else None
    out.append("【供应与烧毁】总量 %s ｜ 流通 %s ｜ 烧毁 %s ｜ 合约持有 %s ｜ 锁定持有 %s ｜ LP 最大持有 %s"
               % (count(_val(metrics, "total_supply")), count(_val(metrics, "circulating_supply")),
                  pct(_val(metrics, "burn_pct"), 2), pct(_val(metrics, "contract_holder_pct"), 2),
                  pct(_val(metrics, "locked_holder_pct"), 2),
                  ("%s %s" % (str(top.get("address"))[:10], pct(top.get("pct"), 2))) if top else NA))
    out.append("【税】买 %s / 卖 %s ｜ 转账税 %s"
               % (pct(_val(metrics, "buy_tax_pct")), pct(_val(metrics, "sell_tax_pct")),
                  pct(_val(metrics, "transfer_tax_pct"))))
    honeypot = _val(metrics, "honeypot_detected")
    out.append("【貔貅】链上判定 %s ｜ GMGN 陷阱占比 %s%s"
               % (_tri(honeypot), pct(_val(metrics, "honeypot_pct")),
                  ("（%s）" % _val(metrics, "honeypot_note")) if _val(metrics, "honeypot_note") else ""))
    out.append("【池】%s 个 ｜ 主池 %s ｜ 主池占 %s ｜ 总池量 %s"
               % (count(_val(metrics, "pools")), _val(metrics, "main_pool") or NA,
                  pct(_val(metrics, "main_pool_share_pct")), money(_val(metrics, "pool_volume_usd"))))
    pools = _pools(_val(metrics, "pool_list"))
    if pools:
        out.append("【池明细】%s" % pools)
    out.append("【安全】%s" % safety_line(metrics, facts))
    creator = _val(facts, "creator") or _val(metrics, "creator")
    if creator or _val(facts, "dev_holding_pct") is not None or _val(facts, "dev_created_count") is not None:
        out.append("【部署者】地址 %s ｜ 持仓 %s ｜ 发币 %s 个 ｜ owner %s ｜ creator %s"
                   % (creator or NA, pct(_val(facts, "dev_holding_pct")),
                      count(_val(facts, "dev_created_count")), pct(_val(facts, "owner_pct")),
                      pct(_val(facts, "creator_pct"))))
    flags = _val(facts, "flags") or {}
    raised = [name for name, value in flags.items() if value]
    if raised:
        out.append("【其他链上标记】%s" % "、".join(str(name) for name in raised[:8]))
    row = {
        "发射台": _val(metrics, "launchpad") or NA,
        "改名次数": count(_val(metrics, "renames")),
        "本轮流动性排名": count(_val(metrics, "liquidity_rank")),
        "本轮成交排名": count(_val(metrics, "volume_rank")),
        "本轮持有人排名": count(_val(metrics, "holders_rank")),
    }
    out.append("【身份】" + " ｜ ".join("%s %s" % (key, value) for key, value in row.items()))
    links = dict(_val(metrics, "socials") or {})
    sites = list(_val(metrics, "websites") or [])
    if links or sites:
        out.append("【社媒与官网】%s"
                   % " ｜ ".join(["%s %s" % (k, v) for k, v in links.items()] + ["官网 %s" % u for u in sites]))
    blurb = _val(metrics, "description")
    if blurb:
        out.append("【项目自述】%s" % str(blurb).strip()[:600])
    trackers = [(label, _val(metrics, key)) for label, key in
                (("GMGN", "gmgn_url"), ("Debot", "debot_url"),
                 ("DexScreener", "dexscreener_url"))]
    trackers = ["%s %s" % (label, url) for label, url in trackers if url]
    if trackers:
        out.append("【交易页】" + " ｜ ".join(trackers))
    return out


def block(
    metrics: TokenMetrics | Mapping[str, Any],
    facts: SafetyFacts | Mapping[str, Any] | None = None,
) -> str:
    """`lines` as one string, which is how both prompts carry it."""
    return "\n".join(lines(metrics, facts))
