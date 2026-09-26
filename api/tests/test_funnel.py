"""The elimination counts. The 设定 page's column and the round's rule rows both come
from here, so these are the numbers a reader can add up by hand."""

from app.api.v1 import forms
from app.core.funnel import (DROP_PRIORITY, RULE_LABEL, cumulative_drop_counts,
                            first_reason, summarize)
from app.pipeline.write import RULE_ROWS


def test_a_token_is_credited_to_its_first_rule_only():
    counts = dict(cumulative_drop_counts([{"decision": "dropped", "failed": ["holders", "market_cap"]}]))
    assert counts["market_cap"] == 1
    assert counts.get("holders", 0) == 0


def test_the_column_adds_up_to_the_tokens_that_left():
    items = (
        [{"decision": "dropped", "failed": ["market_cap"]}] * 3
        + [{"decision": "dropped", "failed": ["liquidity"]}] * 2
        + [{"decision": "candidate", "failed": []}, {"decision": "selected", "failed": []}]
    )
    assert sum(count for _key, count in cumulative_drop_counts(items)) == 5


def test_the_rows_come_back_in_the_order_the_settings_page_draws_them():
    keys = [key for key, _count in cumulative_drop_counts([])]
    assert keys[:3] == ["market_cap", "liquidity", "holders"]
    assert keys[-2] == "entrapment"
    # 距高回撤 is judged last - it needs the pool read the gate buys - so it is the row the
    # settings page draws at the bottom, under the safety rules.
    assert keys[-1] == "market_cap_drawdown"
    assert "source_unverified" in keys and "dangerous_perm" in keys


def test_the_writers_rows_and_the_priority_list_carry_the_same_rules():
    """Two hand-kept lists, one rule set: a rule added to the funnel but not to the
    writer is a rule that drops tokens and shows no row - 距高回撤 arrived that way."""
    assert set(key for key, _label in RULE_ROWS) == set(DROP_PRIORITY)


def test_every_row_the_round_draws_has_a_label_on_the_settings_page():
    for key, _label in RULE_ROWS:
        assert forms.rule_label(key) != key, key


def test_the_rule_words_are_written_down_once():
    """The change bar's 退出 reason prints the same words the settings row is drawn
    under, so the words live beside the priority list in core and the view layer reads
    them from there. A rule with no entry prints its own key, which is how a new rule
    ends up on the bar as an English slug."""
    for rule in DROP_PRIORITY:
        assert rule in RULE_LABEL, rule
        assert forms.rule_label(rule) == RULE_LABEL[rule]
        assert RULE_LABEL[rule] != rule


def test_first_reason_follows_the_same_order():
    assert first_reason({"failed": ["entrapment", "top10"]}) == "top10"
    assert first_reason({"drop_reason": "liquidity"}) == "liquidity"
    assert first_reason({"decision": "candidate"}) is None


def test_a_gated_token_counts_under_the_safety_rules():
    counts = dict(cumulative_drop_counts([
        {"decision": "dropped", "failed": ["market_cap"]},
        {"decision": "dropped", "failed": ["source_unverified"]},
    ]))
    assert counts["market_cap"] == 1 and counts["source_unverified"] == 1


def test_summarize_reads_the_universe_off_the_decisions():
    """扫描 = 淘汰 + 候选, and 入选 is the part of 候选 that got a card.

    The picked token is counted in 候选: it was in the pool, and which number a page shows
    must not depend on whether the round went on to pick it.
    """
    summary = summarize(
        [{"decision": "dropped"}] * 4 + [{"decision": "candidate"}] * 2 + [{"decision": "selected"}]
    )
    assert (summary.scanned, summary.dropped, summary.candidates, summary.selected) == (7, 4, 3, 1)
    assert summary.scanned == summary.dropped + summary.candidates
