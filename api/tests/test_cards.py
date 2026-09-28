"""The card shows what the round wrote.

Every string on a card is either the round's own words (the report it sent) or, when a
run has no written report, derived from the snapshot. These tests pin that order: the
recording wins, so re-deriving can never quietly reword a sent report.
"""

from app.core.models import TokenMetrics
from app.report import cards as card_builder
from app.report.cards import NA, metric_rows, safety_block


CA = "0x" + "c" * 40


def rows(**kwargs):
    return {row["key"]: row for row in metric_rows(kwargs.pop("metrics"), kwargs.pop("recorded", None))}


def test_recorded_rows_win_over_derived_values():
    """A recorded "0" is a result; it must not be re-formatted into something else."""
    metrics = {
        "honeypot_pct": 36.6, "top10_pct": 0.0,
        "buy_tax_pct": "0", "sell_tax_pct": "0",
    }
    recorded = {"税": "0%/0%", "貔貅": "0", "Top10": "否"}
    out = rows(metrics=metrics, recorded=recorded)
    assert out["税"]["value"] == "0%/0%"
    assert out["貔貅"]["value"] == "0"
    assert out["Top10"]["value"] == "否"


def test_every_none_spelling_collapses_to_one_marker():
    """—, —/—, 未判定 and friends all render as the single missing marker."""
    recorded = {"税": "—/—", "貔貅": "未判定", "Top10": "—"}
    out = rows(metrics={}, recorded=recorded)
    for key in recorded:
        assert out[key]["value"] == "—"
        assert out[key]["na"] is True
    assert out["貔貅"]["title"] == "貔貅：未判定"


def test_a_long_recorded_note_is_marked_long():
    note = "无第三方判定（链上 24h 160 笔卖出｜GMGN 陷阱占比 67%）"
    assert rows(metrics={}, recorded={"貔貅": note})["貔貅"]["long"] is True


def test_safety_pills_and_chips_come_from_the_recording():
    """The recorded 安全 block carries detail the facts do not: a source-match note, and
    a sniper share the raw feed reports in its own unit."""
    metrics = {"honeypot_pct": None}
    safety = {"source_verified": True, "lp_locked": False, "sniper_pct": 32100.0}
    recorded = {
        "perms": ["源码已验证（match: UERC20）", "无危险权限"],
        "lp": "已锁（发射台 minara 路径发行，创建者未持有头寸）",
        "chips": [["陷阱", "40.1%"], ["狙击", "3.3%"], ["dev 持仓", "0.0%"], ["持有人", "496"]],
    }
    block = safety_block(metrics, safety, recorded)
    assert [p["label"] for p in block["perms"]] == recorded["perms"]
    assert {p["tone"] for p in block["perms"]} == {"ok"}
    assert block["lp"] == {"label": "已锁", "tone": "ok"}
    chips = {c["label"]: c for c in block["chips"]}
    assert chips["狙击"] == {"label": "狙击", "value": "3.3%", "tone": "ok"}
    assert chips["陷阱"]["tone"] == "warn"
    assert chips["持有人"]["tone"] == "muted"


def test_safety_falls_back_to_the_facts_when_nothing_was_recorded():
    safety = {"source_verified": False, "lp_locked": False, "sniper_pct": 20.0, "holders": 42}
    block = safety_block({}, safety, {})
    assert [p["tone"] for p in block["perms"]] == ["warn", "ok"]
    assert block["lp"]["tone"] == "danger"
    assert {c["label"]: c["tone"] for c in block["chips"]}["狙击"] == "danger"

def test_the_pool_table_keeps_the_feeds_order_and_drops_a_residue():
    """GoPlus reports 1.28 next to the real 21,525 for one Arc token: that is noise, and
    printing it as "$0.0K" would dress it up as a measurement."""
    metrics = {
        "pool_list": [
            {"name": "UniswapV4", "kind": "UniV4", "pair": "0xaa", "liquidity_usd": 21525.0},
            {"name": "UniswapV4", "kind": "UniV4", "pair": "0xbb", "liquidity_usd": 1.28},
        ]
    }
    block = safety_block(metrics, {})
    assert [row["liquidity"] for row in block["pools"]["rows"]] == ["$21.5K", "—"]
    assert block["pools"]["rows"][0]["pair"] == "0xaa"
    assert block["pools"]["note"] == ""


def test_a_long_pool_list_says_how_many_it_left_out():
    block = safety_block({"pool_list": [{"pair": "0x%02d" % index} for index in range(9)]}, {})
    assert len(block["pools"]["rows"]) == 6
    assert block["pools"]["note"] == "共 9 个池，表内前 6"


def test_holder_shares_are_shares_of_supply():
    metrics = {
        "top_holders": [
            {
                "address": "0x000000000000000000000000000000000000dead",
                "pct": 4.12, "burnt": True, "locked": True,
            },
            {
                "address": "0x8366a39cc670b4001a1121b8f6a443a643e40951",
                "pct": 15.88, "contract": True,
            },
        ],
        "burn_pct": 4.12, "locked_holder_pct": 4.12, "contract_holder_pct": 15.88,
    }
    block = safety_block(metrics, {})
    assert [chip["value"] for chip in block["holders"]["summary"]] == ["4.12%", "4.12%", "15.88%"]
    assert block["holders"]["rows"][0]["tags"] == ["烧毁", "锁定"]
    assert block["holders"]["rows"][0]["addr"] == "0x0000…dead"


def test_only_the_flags_that_are_on_are_printed():
    """A warning list lists what is on; a row of "no" would bury the one that matters."""
    block = safety_block(
        {}, {"flags": {"external_call": True, "cannot_buy": False, "is_anti_whale": True}}
    )
    assert [(pill["label"], pill["tone"]) for pill in block["warnings"]] == [
        ("外部调用", "danger"), ("防鲸限制", "warn"),
    ]


def test_a_locked_pool_drops_the_footnote():
    """已锁 answers the question by itself. The note stays where it is the reason."""
    locked = safety_block({}, {"lp_locked": True, "lp_note": "GoPlus: LP 99.96% 在合约 0xd6b0"})
    assert locked["lp"] == {"label": "已锁", "tone": "ok"}
    unknown = safety_block({}, {"lp_locked": None, "lp_note": "未取到建池交易"})
    assert unknown["lp"]["label"] == "未判定（未取到建池交易）"
    assert unknown["lp"]["tone"] == "warn"
    # A note that brings its own brackets must not stutter: "未判定（…（可重试））".
    nested = safety_block({}, {"lp_locked": None, "lp_note": "节点未响应建池交易查询（可重试）"})
    assert nested["lp"]["label"] == "未判定 · 节点未响应建池交易查询（可重试）"


def test_a_card_without_chain_detail_draws_no_table():
    block = safety_block({"ca": "0x1"}, {})
    assert block["pools"]["rows"] == []
    assert block["holders"]["rows"] == []
    assert block["holders"]["summary"] == []
    assert block["warnings"] == []
    assert block["holdings"] == []


def test_the_open_time_and_supply_rows_are_not_printed():
    """Both facts still live in the snapshot; neither earns a row on the quality table."""
    out = rows(metrics={
        "signal_count": 0, "total_supply": 1_000_000_000,
        "open_at": "2026-09-16T12:00:00+00:00",
    })
    assert "总供应" not in out
    assert "开盘" not in out


def test_the_honeypot_row_is_the_verdict_not_the_trap_share():
    """41.6% is GMGN's share of trap-like wallets - the 陷阱 chip. The 貔貅 row is the
    contract verdict, so the same number cannot appear under two labels."""
    out = rows(metrics={"honeypot_pct": 41.6, "honeypot_detected": True})
    assert out["貔貅"]["value"] == "是"
    assert rows(metrics={"honeypot_pct": 41.6, "honeypot_detected": False})["貔貅"]["value"] == "否"
    assert rows(metrics={"honeypot_pct": 41.6})["貔貅"]["value"] == NA


def test_the_honeypot_row_carries_the_colour_of_its_verdict():
    """The card boxes this one row, so the verdict has to arrive with its tone."""
    assert rows(metrics={"honeypot_detected": True})["貔貅"]["tone"] == "danger"
    assert rows(metrics={"honeypot_detected": False})["貔貅"]["tone"] == "ok"
    assert rows(metrics={})["貔貅"]["tone"] == "warn"
    assert rows(metrics={}, recorded={"貔貅": "未判定"})["貔貅"]["tone"] == "warn"


def test_only_the_honeypot_row_is_boxed():
    out = rows(metrics={"holders": 100, "honeypot_detected": False})
    assert set(row["tone"] for key, row in out.items() if key != "貔貅") == {""}


def test_the_wash_row_is_not_printed():
    """Nothing on Arc measures wash trading - the row was a permanent "—". The field is
    gone from the model too, so no future source can fill a row nobody shows."""
    surface = rows(metrics={})
    assert "洗量" not in surface
    assert "wash_pct" not in TokenMetrics.model_fields


def test_the_anti_bot_reads_are_not_printed():
    """GMGN answers 0 for all of them on Arc - a row that is always 0.0% is noise, so the
    card does not carry it (the snapshot still does)."""
    out = rows(metrics={
        "bot_degen_pct": 0.0, "fresh_wallet_pct": 0.0, "private_vault_pct": 0.0,
        "bluechip_holder_pct": 0.0, "signal_count": 0,
    })
    for key in ("机器人", "新钱包", "私人金库", "优质持有人", "信号"):
        assert key not in out


def test_team_holdings_are_printed_as_percentages():
    block = safety_block({}, {"owner_pct": 0.0, "creator_pct": 12.5})
    assert [(chip["label"], chip["value"]) for chip in block["holdings"]] == [
        ("owner 持仓", "0.00%"), ("creator 持仓", "12.50%"),
    ]


def test_a_card_carries_no_age_badge():
    """Every round writes its own words, so there is no "older than the numbers" state."""
    metrics = TokenMetrics(ca="0xaa", symbol="T").model_dump()

    card = card_builder.card(rank=1, metrics=metrics, safety={}, written={"verdict": "结论"},
                             breakdown={})

    assert "entry_note" not in card

