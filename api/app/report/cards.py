"""Compose one report card from facts. Pure: no IO, no database.

The card is an assembler of three inputs that are each already frozen elsewhere:

* `TokenMetrics` + `SafetyFacts` - the run's snapshot (immutable),
* the written report payload - verdict, detail sections (immutable once written),
* the scoring breakdown - on-chain quality / track fit and their weights.

Everything here is formatting and threshold colour. Nothing re-fetches and nothing
invents a number: an absent value renders as the single missing-value marker "—"
(`CLAUDE.md`), never as a zero.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .segments import segments

NA = "—"
METRIC_KEYS = ("市值", "流动性", "24h 成交", "换手", "持有人", "Top10", "捆绑", "税", "貔貅")

# The spellings the round itself uses for "not established this round". They all
# collapse to NA on the card, so the marker means one thing everywhere.
NA_VALUES = {"—", "-", "/", "—/—", "未知", "未判定", "未取到", ""}


def num(value: Any) -> float | None:
    """Sources disagree about types - a tax can arrive as "5" and a market cap as 5.
    Anything that is not a real number is "not established", never zero."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def money(value: Any) -> str:
    number = num(value)
    if number is None:
        return NA
    if abs(number) >= 1_000_000:
        return "$%.2fM" % (number / 1_000_000)
    return "$%.1fK" % (number / 1_000)


def pct(value: Any, digits: int = 1) -> str:
    number = num(value)
    return NA if number is None else ("%.{}f%%".format(digits) % number)


def ratio(value: Any, digits: int = 2) -> str:
    number = num(value)
    return NA if number is None else ("%.{}f".format(digits) % number)


def count(value: Any) -> str:
    number = num(value)
    return NA if number is None else "{:,}".format(int(number))


def tone_high(value: float | None, warn: float, danger: float) -> str:
    if value is None:
        return "muted"
    if value >= danger:
        return "danger"
    if value >= warn:
        return "warn"
    return "ok"


def tone_low(value: float | None, warn: float, danger: float) -> str:
    if value is None:
        return "muted"
    if value <= danger:
        return "danger"
    if value <= warn:
        return "warn"
    return "ok"


def tax_text(buy: float | None, sell: float | None) -> str:
    if buy is None and sell is None:
        return NA
    return "买 %s / 卖 %s" % (pct(buy), pct(sell))


def is_na(value: Any) -> bool:
    text = str(value if value is not None else "").strip()
    if text in NA_VALUES:
        return True
    return text.replace("/", "").strip("—-") == ""


def metric_rows(
    metrics: Mapping[str, Any], recorded: Mapping[str, Any] | None = None
) -> list[dict]:
    """The ten 链上质量 rows plus the pool rows the round recorded.

    The round writes these rows as text - "0%/0%", "否", "无第三方判定(...)" - so the
    recording wins over the value derived from facts. Deriving them again reformats the
    round's own words, and for a falsy-but-real zero (`honeypot: 0`) the `or` fallback
    would quietly substitute a different metric. Deriving is only the fallback for a
    round that recorded no rows.
    """
    recorded = recorded or {}
    turnovers = num(metrics.get("turnovers"))
    volume, cap = num(metrics.get("volume_24h_usd")), num(metrics.get("market_cap_usd"))
    if turnovers is None and volume and cap:
        turnovers = volume / cap
    derived: list[tuple[str, str]] = [
        ("市值", money(metrics.get("market_cap_usd"))),
        ("流动性", money(metrics.get("liquidity_usd"))),
        ("24h 成交", money(metrics.get("volume_24h_usd"))),
        ("换手", ratio(turnovers)),
        ("持有人", count(metrics.get("holders"))),
        ("Top10", pct(metrics.get("top10_pct"))),
        ("捆绑", pct(metrics.get("bundling_pct"))),
        ("税", tax_text(metrics.get("buy_tax_pct"), metrics.get("sell_tax_pct"))),
        ("貔貅", honeypot_text(metrics.get("honeypot_detected"))),
        ("池数量", count(metrics.get("pools"))),
        ("总池量", money(metrics.get("pool_volume_usd"))),
        ("主池", metrics.get("main_pool") or NA),
        ("主池占", pct(metrics.get("main_pool_share_pct"), 0)),
    ]
    pairs = [
        (key, (recorded.get(key) or value) if key in METRIC_KEYS else value)
        for key, value in derived
    ]
    rows = []
    for key, value in pairs:
        # `0` and `否` are results, not absences: only None and "" are missing.
        value = NA if value is None else (str(value).strip() or NA)
        missing = is_na(value)
        rows.append(
            {
                "key": key,
                "value": NA if missing else value,
                "na": missing,
                "long": (not missing) and len(value) > 16,
                "title": ("%s：%s" % (key, value)) if missing else value,
                "tone": honeypot_tone(value) if key == "貔貅" else "",
            }
        )
    return rows


def honeypot_tone(value: Any) -> str:
    """The colour of the 貔貅 box: red for a trap, green for a clear, amber for unknown."""
    text = str(value).strip()
    if text == "是":
        return "danger"
    if text == "否":
        return "ok"
    return "warn"


def honeypot_text(value: Any) -> str:
    """The 貔貅 verdict, not a share.

    `honeypot_pct` is GMGN's share of wallets that behave like a trap, which the card shows
    as the 陷阱 chip; the verdict is GoPlus's answer about the contract itself. They are two
    measurements and the row prints the one that answers the question it is labelled with.
    """
    if value is None:
        return NA
    return "是" if value else "否"


# The thresholds the round colours each chip by, keyed by chip name.
CHIP_BOUNDS = {
    "陷阱": (30, 45),
    "老鼠仓": (0.1, 1.0),
    "捆绑": (10, 25),
    "狙击": (5, 15),
    "Top10": (25, 40),
    "dev 持仓": (3, 10),
}

PCT_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)%")


def _pct_of(text: Any) -> float | None:
    """The number inside a recorded chip value, e.g. "3.3%" -> 3.3."""
    match = PCT_RE.search(str(text or ""))
    return float(match.group(1)) if match else None


def _recorded_chip(name: str, value: str) -> dict:
    bounds = CHIP_BOUNDS.get(name)
    if bounds is None:
        return _chip(name, value or NA, "muted")
    return _chip(name, value or NA, tone_high(_pct_of(value), bounds[0], bounds[1]))


def _perms_tone(labels: list[str]) -> str:
    text = " ".join(labels)
    ok = ("已验证" in text or "无危险权限" in text) and "未验证" not in text
    return "ok" if ok else "warn"


def _lp_tone(label: str) -> str:
    text = label or ""
    if "已" in text and "未判定" not in text:
        return "ok"
    return "danger" if "未锁" in text else "warn"


def safety_block(
    metrics: Mapping[str, Any],
    safety: Mapping[str, Any],
    recorded: Mapping[str, Any] | None = None,
) -> dict:
    """The safety rows as the round wrote them, falling back to the facts.

    The recorded text is what the round actually wrote - it carries things the spread
    facts cannot ("源码已验证（match: UERC20）", a sniper share the raw feed reports in
    its own unit). Re-deriving would show different words, or a different number.
    """
    recorded = recorded or {}
    if recorded.get("perms") or recorded.get("chips"):
        block = {
            "perms": [
                {"label": label, "tone": _perms_tone(recorded["perms"])}
                for label in recorded["perms"]
            ],
            "lp": _recorded_lp(recorded["lp"], safety),
            "chips": [_recorded_chip(name, value) for name, value in recorded["chips"]],
        }
    else:
        block = _safety_from_facts(metrics, safety)
    block.update(_chain_details(metrics, safety))
    return block


#: How many rows the pool and holder tables print. The feeds answer with everything they
#: know - one Arc token's GoPlus row carried 17 pools - so the table is capped and the note
#: beneath it says how many there were: shorter card, nothing hidden.
POOL_ROWS = 6
HOLDER_ROWS = 10

#: The GoPlus flags nothing scores, in the order they matter, with the tone to print them
#: in. The label is the whole message here, so the wording carries the meaning.
FLAG_LABELS: tuple[tuple[str, str, str], ...] = (
    ("cannot_buy", "不可买入", "danger"),
    ("is_whitelisted", "仅白名单可买", "danger"),
    ("external_call", "外部调用", "danger"),
    ("selfdestruct", "可自毁", "danger"),
    ("honeypot_with_same_creator", "同创建者貔貅史", "danger"),
    ("trading_cooldown", "交易冷却", "warn"),
    ("is_anti_whale", "防鲸限制", "warn"),
    ("anti_whale_modifiable", "防鲸额度可改", "warn"),
    ("slippage_modifiable", "滑点可改", "warn"),
    ("personal_slippage_modifiable", "个人滑点可改", "warn"),
)


def _short(value: Any) -> str:
    """An address as the tables print it, with the full one kept for the tooltip."""
    text = str(value or "")
    return text if len(text) <= 14 else "%s…%s" % (text[:6], text[-4:])


def _capped(total: int, shown: int, unit: str) -> str:
    if not shown or total <= shown:
        return ""
    return "共 %d 个%s，表内前 %d" % (total, unit, shown)


#: Below this the feed's pool liquidity is a residue rather than a reading: one Arc token's
#: v4 rows carried 1.28 and 0.0029 next to the real 21,525. Printing "$0.0K" for those
#: would dress noise up as a measurement, so the card says nothing instead.
POOL_FLOOR_USD = 100.0


def _pool_rows(raw: Any) -> dict:
    """GoPlus's `dex[]`: where the token trades, and the pool id of each venue.

    The order is the feed's own (largest first), not re-sorted here: the number it sorts by
    is partial for v4 pools, and a second sort on a half-read field would shuffle the list
    without improving it. The fee is left out for the same reason - GoPlus reports 0.88 for
    one Arc v4 pool - and is kept in the snapshot for whoever can read it properly.
    """
    pools = [pool for pool in (raw or []) if isinstance(pool, Mapping)]
    rows = []
    for pool in pools[:POOL_ROWS]:
        name = str(pool.get("name") or "").strip()
        kind = str(pool.get("kind") or "").strip()
        pair = str(pool.get("pair") or "")
        liquidity = num(pool.get("liquidity_usd"))
        rows.append({
            "name": " · ".join(part for part in (name, kind) if part) or "池",
            "liquidity": money(liquidity) if (liquidity or 0) >= POOL_FLOOR_USD else NA,
            "pair": _short(pair),
            "pair_full": pair,
        })
    return {"rows": rows, "note": _capped(len(pools), len(rows), "池")}


def _holder_block(raw: Any, metrics: Mapping[str, Any]) -> dict:
    """GoPlus's `holders[]`, with the burn/lock/contract shares it implies.

    The shares are of the whole supply, the same denominator the 链上质量 rows use, so
    "烧毁 4.10%" means 4.10% of the supply sits in a burn address - among the holders the
    feed listed, which the note says.
    """
    holders = [holder for holder in (raw or []) if isinstance(holder, Mapping)]
    rows = []
    for index, holder in enumerate(holders[:HOLDER_ROWS], start=1):
        address = str(holder.get("address") or "")
        rows.append({
            "rank": index,
            "addr": _short(address),
            "addr_full": address,
            "pct": pct(holder.get("pct"), 2),
            "tags": [
                name for name, mark in (("烧毁", "burnt"), ("锁定", "locked"), ("合约", "contract"))
                if holder.get(mark)
            ],
        })
    if not rows:
        return {"rows": [], "summary": [], "note": ""}
    summary = [
        {"label": "烧毁", "value": pct(metrics.get("burn_pct"), 2), "tone": "muted"},
        {"label": "锁定", "value": pct(metrics.get("locked_holder_pct"), 2), "tone": "muted"},
        {"label": "合约", "value": pct(metrics.get("contract_holder_pct"), 2), "tone": "muted"},
    ]
    return {"rows": rows, "summary": summary, "note": _capped(len(holders), len(rows), "持有人")}


def _flags(raw: Any) -> list[dict]:
    """The GoPlus flags that are set. One left at 0 is not printed: a warning list lists
    what is on, and a row of "no" would bury the one that matters."""
    flags = raw if isinstance(raw, Mapping) else {}
    return [
        {"label": label, "tone": tone}
        for key, label, tone in FLAG_LABELS if flags.get(key) is True
    ]


def _holdings(metrics: Mapping[str, Any], safety: Mapping[str, Any]) -> list[dict]:
    """What the owner and the deployer still hold - the two GoPlus answers that no other
    row on the card duplicates."""
    rows = []
    for label, key in (("owner 持仓", "owner_pct"), ("creator 持仓", "creator_pct")):
        value = _either(metrics, safety, key)
        if value is not None:
            rows.append({"label": label, "value": pct(value, 2), "tone": "muted"})
    return rows


def _chain_details(metrics: Mapping[str, Any], safety: Mapping[str, Any]) -> dict:
    """The chain's own detail, straight off the feeds.

    All of it is display-only - nothing here scores - so an empty answer stays empty and
    the card drops the block instead of printing a row of "—" for a question no feed was
    asked.
    """
    return {
        "pools": _pool_rows(metrics.get("pool_list")),
        "holders": _holder_block(metrics.get("top_holders"), metrics),
        "warnings": _flags(safety.get("flags")),
        "holdings": _holdings(metrics, safety),
    }


def _recorded_lp(label: str | None, safety: Mapping[str, Any]) -> dict:
    """The round's own LP wording, minus the footnote a locked pool does not need.

    "已锁" needs no "（GoPlus：LP 99.96% 在合约 …）" beside it - the label is the whole
    answer. The two unresolved states keep their note, because there it is the reason.
    """
    if not label:
        return _lp_from(safety)
    text = label.strip()
    if text.startswith("已锁"):
        text = text.split("（")[0].strip()
    elif "）（" in text:
        text = text.replace("）（", "）·（")
    return {"label": text, "tone": _lp_tone(text)}


def _with_note(label: str, note: Any) -> str:
    """The label plus its reason, without the stutter of nested brackets.

    A note like "节点未响应建池交易查询（可重试）" already carries brackets, so wrapping
    it in another pair printed "未判定（…（可重试））". A dot separates them instead.
    """
    if not note:
        return label
    text = str(note).strip()
    return "%s · %s" % (label, text) if "（" in text else "%s（%s）" % (label, text)


def _lp_from(safety: Mapping[str, Any]) -> dict:
    locked = safety.get("lp_locked")
    note = safety.get("lp_note")
    if locked is True:
        return {"label": "已锁", "tone": "ok"}
    if locked is False:
        return {"label": _with_note("未锁", note), "tone": "danger"}
    return {"label": _with_note("未判定", note), "tone": "warn"}


def _either(metrics: Mapping[str, Any], safety: Mapping[str, Any], key: str) -> Any:
    """The reading, from whichever of the two copies has one.

    The round keeps two: `metrics` carries the bulk feed's numbers for the whole pool,
    `safety` the per-token reads for the shortlist. They do not cover the same tokens -
    GMGN's ranking rows answer top10, bundler, sniper and insider for hundreds of tokens,
    while the per-token call only ever runs for the twenty the gate reaches - so reading
    `safety` alone threw away numbers the round was already holding. That is how a token
    with a measured 0% share came out 未判定 on the card.
    """
    for source in (metrics, safety):
        value = source.get(key)
        if value is not None:
            return value
    return None

def _safety_from_facts(metrics: Mapping[str, Any], safety: Mapping[str, Any]) -> dict:
    verified = safety.get("source_verified")
    perms = safety.get("dangerous_perms")
    perm_labels: list[tuple[str, str]] = []
    if verified is True:
        perm_labels.append(("源码已验证", "ok"))
    elif verified is False:
        perm_labels.append(("源码未验证", "warn"))
    else:
        perm_labels.append(("未判定", "warn"))
    if perms:
        for perm in perms:
            perm_labels.append((perm, "danger"))
    else:
        perm_labels.append(("无危险权限", "ok"))

    lp = _lp_from(safety)

    entrapment = _either(metrics, safety, "entrapment_pct")
    insider = _either(metrics, safety, "insider_pct")
    bundling = _either(metrics, safety, "bundling_pct")
    sniper = _either(metrics, safety, "sniper_pct")
    top10 = _either(metrics, safety, "top10_pct")
    dev_holding = _either(metrics, safety, "dev_holding_pct")
    holders = _either(metrics, safety, "holders")
    buy_tax, sell_tax = num(metrics.get("buy_tax_pct")), num(metrics.get("sell_tax_pct"))
    chips = [
        _chip("陷阱", pct(entrapment), tone_high(entrapment, 30, 45)),
        _chip("老鼠仓", pct(insider), tone_high(insider, 0.1, 1.0)),
        _chip("捆绑", pct(bundling), tone_high(bundling, 10, 25)),
        _chip("狙击", pct(sniper), tone_high(sniper, 5, 15)),
        _chip("Top10", pct(top10), tone_high(top10, 25, 40)),
        _chip("dev 持仓", pct(dev_holding), tone_high(dev_holding, 3, 10)),
        _chip("买税", pct(buy_tax), tone_high(buy_tax, 5, 10)),
        _chip("卖税", pct(sell_tax), tone_high(sell_tax, 5, 10)),
        _chip("持有人", count(holders), "muted"),
    ]
    return {
        "perms": [{"label": label, "tone": tone} for label, tone in perm_labels],
        "lp": lp,
        "chips": chips,
    }


def _chip(label: str, value: str, tone: str) -> dict:
    return {"label": label, "value": value, "tone": tone}


def spark_path(points: list[float], width: int = 128, height: int = 26) -> str:
    if len(points) < 2:
        return "M0,13 L128,13"
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1.0
    step = width / float(len(points) - 1)
    coords = [
        (i * step, height - 3 - (p - lo) / span * (height - 6)) for i, p in enumerate(points)
    ]
    return "M" + " L".join("%.1f,%.1f" % (x, y) for x, y in coords)


def spark(metrics: Mapping[str, Any]) -> dict:
    series = [float(p) for p in (metrics.get("price_series") or [])]
    if len(series) < 2:
        return {"path": spark_path([]), "trend": "flat", "delta": ""}
    first, last = series[0], series[-1]
    delta = "" if not first else "%+.0f%%" % ((last - first) / first * 100)
    return {
        "path": spark_path(series),
        "trend": "down" if last < first else "up",
        "delta": delta,
    }


#: What a stored evidence page reads as, when the caller did not already name it. The name
#: is decided by the research pass (the side that knows what a kind is); this is the
#: fallback for a kind it did not recognise, so a new kind shows up as itself, not as blank.


#: The footer order: where to trade, then where the project lives. The three trackers come
#: off the snapshot; the three after them are the project's own pages, which the 社区活跃度
#: block no longer repeats - a block that argues about 互动率 and 持续性 is not where a reader
#: looks for a URL.
TRACKER_LINKS = (("gmgn_url", "GMGN"), ("debot_url", "Debot"),
                 ("dexscreener_url", "DexScreener"))
SOCIAL_LINKS = (("twitter", "X"), ("telegram", "TG"), ("github", "GitHub"))


def links(metrics: Mapping[str, Any]) -> list[dict]:
    """Every page the card links to: where to trade, then where the project lives.

    The deep pass's filed evidence used to be appended here too, which printed the same
    three labels (官网 / X / GitHub) a second time, on every card, as text a reader cannot
    use. Now the footer is the only place a card prints an entry point, so the links the
    社区活跃度 block used to carry live here instead - and the card prints them as plain
    underlined text, because a row of seven external-link icons is mostly icon.
    """
    out = []
    for key, label in TRACKER_LINKS:
        if metrics.get(key):
            out.append({"label": label, "url": metrics[key]})
    socials = dict(metrics.get("socials") or {})
    for kind, label in SOCIAL_LINKS:
        if socials.get(kind):
            out.append({"label": label, "url": socials[kind]})
    for url in list(metrics.get("websites") or [])[:1]:
        out.append({"label": "官网", "url": url})
    return out


def card(
    rank: int,
    metrics: Mapping[str, Any],
    safety: Mapping[str, Any],
    written: Mapping[str, Any],
    breakdown: Mapping[str, Any],
    is_new: bool = False,
    onchain_weight: float = 55,
    offchain_weight: float = 45,
) -> dict:
    """One card. `written` is the frozen report payload; `breakdown` is the scored run_item."""
    badges = dict(written.get("badges") or {})
    details = [
        {"title": title, "body": segments(body), "badge": badges.get(title, "")}
        for title, body in (written.get("sections") or {}).items()
        if body
    ]
    return {
        "rank": rank,
        "ca": metrics.get("ca"),
        "sym": metrics.get("symbol") or NA,
        "tag": written.get("tag") or "",
        "is_new": is_new,
        "score": _round(breakdown.get("total")),
        "onchain": _round(breakdown.get("onchain")),
        "product": _round(breakdown.get("product")),
        "onchain_weight": onchain_weight,
        "offchain_weight": offchain_weight,
        "ranks": {
            "liquidity": metrics.get("liquidity_rank"),
            "volume": metrics.get("volume_rank"),
            "holders": metrics.get("holders_rank"),
        },
        "spark": spark(metrics),
        "verdict": segments(written.get("verdict")),
        "metrics": metric_rows(metrics, written.get("rows")),
        "deltas": [{"key": d.get("key"), "value": d.get("value")} for d in (written.get("deltas") or [])],
        "safety": safety_block(metrics, safety, written.get("safety")),
        "details": details,
        "links": links(metrics),
    }


def _round(value: Any) -> float | None:
    return None if value is None else round(float(value), 1)


def decline_row(item: Mapping[str, Any], metrics: Mapping[str, Any]) -> dict:
    reasons = list(item.get("reasons") or [])
    if item.get("score") is None:
        base = "本轮未产出评分"
    else:
        base = "评分未进前 %d" % int(item.get("per_round") or 5)
    if reasons:
        base += " ｜ " + " · ".join(reasons)
    return {
        # The address travels with the row so the table can offer it without printing 42
        # characters of it: the pool is a list of candidates to go and look at, and the CA
        # is what you paste into wherever you look. `ca` is not in `metrics` - it is the key
        # the snapshot was filed under - hence the item mapping.
        "ca": str(item.get("ca") or ""),
        "sym": metrics.get("symbol") or NA,
        "market_cap": money(metrics.get("market_cap_usd")),
        "liquidity": money(metrics.get("liquidity_usd")),
        "holders": count(metrics.get("holders")),
        "entrapment": pct(metrics.get("honeypot_pct")),
        "score": _round(item.get("score")),
        "reason": base,
    }
