"""The fact sheet: what the writer is handed, which is what the card draws.

The measurement behind this file: the draft was handed eight fields while the card beside it
drew thirty off the same snapshot, so the entries read like security reports - the security
rows were the only block the model had.
"""

from app.core.models import SafetyFacts, TokenMetrics
from app.report import facts

CA = "0x" + "a" * 40


def test_the_sheet_carries_what_the_card_draws():
    metrics = TokenMetrics(
        ca=CA, symbol="SOLON", market_cap_usd=147_600, liquidity_usd=55_700,
        volume_24h_usd=83_100, holders=1510, top10_pct=22.2, bundling_pct=0.0,
        burn_pct=4.118, main_pool="SOLON / USDC 1%", pools=10,
        price_series=[1.0, 2.0, 3.0], drawdown_pct=-58.0,
        socials={"twitter": "https://x.com/Solonlabs1"}, websites=["https://solonpad.fun"],
        description="launchpad on Arc",
    )
    sheet = facts.block(metrics, SafetyFacts())
    for needle in (
        "市值 $147.6K", "流动性 $55.7K", "持有人 1,510", "烧毁 4.12%",
        "SOLON / USDC 1%", "距 ATH -58.0%", "https://x.com/Solonlabs1",
        "https://solonpad.fun", "launchpad on Arc",
    ):
        assert needle in sheet


def test_a_missing_field_prints_as_a_gap_not_as_a_zero():
    """`—` says "five sources were asked and none had it". A zero says the source answered,
    which is the difference between an entry that says 未判定 and one that invents."""
    sheet = facts.block(TokenMetrics(ca=CA))
    assert "市值 —" in sheet
    assert "$0" not in sheet and "0.0%" not in sheet


def test_a_model_and_a_snapshot_dict_draw_the_same_sheet():
    """The draft reads a live model, the research pass reads the snapshot back out of the
    database. Two formats over one snapshot is how the two of them drifted apart before."""
    row = {"ca": CA, "symbol": "T", "market_cap_usd": 1234.5, "holders": 12}
    assert facts.block(TokenMetrics.model_validate(row)) == facts.block(row)


def test_the_pool_line_carries_the_pools_own_volume_not_the_rounds_liquidity():
    """The brief used to print 主池 with the round’s total liquidity beside the main pool’s
    name, so one pool read as the whole book - a number the card never showed."""
    metrics = TokenMetrics(ca=CA, pools=3, main_pool="T / USDC 1%", main_pool_share_pct=97.0,
                           liquidity_usd=55_700, pool_volume_usd=84_300)
    line = [row for row in facts.lines(metrics) if row.startswith("【池】")][0]
    assert "主池 T / USDC 1%" in line
    assert "主池占 97.0%" in line and "总池量 $84.3K" in line
    assert "$55.7K" not in line


def test_the_top_holders_line_names_what_each_seat_is():
    """A burn address and a locked position are different facts; reading the first as the
    second is how a card came to claim “LP 已锁在合约” about a contract that holds
    it without locking it."""
    metrics = TokenMetrics(ca=CA, top_holders=[
        {"address": "0xd" * 40, "pct": 4.12, "burnt": True, "contract": True},
        {"address": "0xb" * 40, "pct": 3.0, "locked": True},
    ])
    line = [row for row in facts.lines(metrics) if row.startswith("【持有人前")][0]
    assert "烧毁" in line and "锁定" in line


def test_a_locked_pool_needs_no_explanation_beside_it():
    """The same rule the card follows: the note explains a verdict the reader could not
    otherwise read, and 已锁 is not one of those."""
    safety = SafetyFacts(lp_locked=True, lp_note="GoPlus：LP 99.96% 在合约 0xd6b05564 内")
    line = facts.safety_line(TokenMetrics(ca=CA), safety)
    assert "LP 已锁" in line
    assert "0xd6b05564" not in line


def test_a_source_reporting_no_match_does_not_get_its_name_on_the_line():
    """「match: none」是一个源在说「没有可报的匹配标签」，而不是一句可以写在源名旁边
    的事实：它原本在安全行上读成了「GoPlus: none」。"""
    safety = SafetyFacts(source_verified=True, source_verified_by=["GoPlus"], source_match="none")
    line = facts.safety_line(TokenMetrics(ca=CA), safety)
    assert line.startswith("源码 已验证（GoPlus）")
    assert "none" not in line
