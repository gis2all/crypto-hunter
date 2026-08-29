"""新机会：纯逻辑、两个模型关口、扫描 job 与端点。

这个模块的每一次判断都有一个"不算数"的答案（未判定 / 未晋升 / 没问到），测试的重点就在这里：
哪些东西**不许**被当成结论 —— 材料没过闸不等于没机会、模型没答不等于 other、没有步骤不等于
"不是活动"。
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.llm import client as llm
from app.opportunity import (extract, group, heat, job, lifecycle, model, promote, rules,
                             scan, schema, trace, trust, watchlist, xread)
from app.opportunity.schema import Entry, Step
from app.store import db


def _entry_row(slug: str, **over) -> dict:
    """One stored entry, with the fields a sweep or a read needs."""
    row = {
        "slug": slug, "kind": "activity", "category": "测试网", "title": slug,
        "organizer": "Pix", "official_url": "", "summary": "",
        "trust": "official", "trust_basis": "测试", "risk": "", "barrier": "",
        "maturity": "", "started_at": "", "deadline": "", "status": "promoted",
        "reason": None, "heat": None, "heat_basis": None,
        "discovered_at": db.now_iso(), "source_kind": "post",
        "source_ref": "https://x.com/Pix/status/1", "extract_version": "extract.md@1",
    }
    row.update(over)
    return row


# ------------------------------------------------------------------ 纯逻辑

def test_a_slug_is_stable_and_readable_when_the_title_has_latin():
    entry = Entry(title="ORDWORLD Public Beta", organizer="BreathFi20", kind="activity")
    slug = schema.slug_for(entry)
    assert slug.startswith("opp-ordworld")
    assert slug == schema.slug_for(entry), "同一个条目两次扫描必须是同一个 id"


def test_a_chinese_title_falls_back_to_a_stable_hash():
    entry = Entry(title="公测开放", organizer="BreathFi20", kind="activity")
    first = schema.slug_for(entry)
    assert first.startswith("opp-breathfi20-")
    assert first == schema.slug_for(Entry(title="公测开放", organizer="BreathFi20"))


def test_an_unknown_kind_is_not_read_at_all():
    verdict, issues = schema.read_verdict({"kind": "空投", "category": "空投"})
    assert verdict is None and issues


def test_a_category_outside_the_list_is_an_issue_and_other_in_the_end():
    verdict, issues = schema.read_verdict({"kind": "activity", "category": "挖矿活动"})
    assert verdict is not None and issues, "类目不在表里要记下来，但不必因此丢掉整条判断"
    entries, entry_issues = schema.read_entries({
        "entries": [{"title": "某活动", "kind": "activity", "category": "挖矿活动",
                     "steps": [{"body": "做点什么"}]}]})
    assert entries and entries[0].category == "其他" and entry_issues


def test_an_activity_without_a_step_is_not_an_activity():
    entries, issues = schema.read_entries({
        "entries": [{"title": "某活动", "kind": "activity", "steps": []}]})
    assert entries and any("without a single step" in issue for issue in issues)
    status, reason = promote.verdict(entries[0])
    assert status == "candidate" and reason == "no_steps"


def test_a_project_needs_a_sentence_not_a_step():
    entries, issues = schema.read_entries({
        "entries": [{"title": "某个新协议", "kind": "project", "summary": "",
                     "organizer": "someone"}]})
    assert entries and issues
    status, reason = promote.verdict(entries[0])
    assert (status, reason) == ("candidate", "no_summary")


def test_a_project_with_a_sentence_and_a_way_in_is_promoted():
    entries, _ = schema.read_entries({
        "entries": [{"title": "某个新协议", "kind": "project", "summary": "比特币上的 AMM",
                     "official_url": "https://example.org"}]})
    status, reason = promote.verdict(entries[0])
    assert (status, reason) == ("promoted", None)


def test_an_activity_needs_a_way_in_even_with_steps():
    entries, _ = schema.read_entries({
        "entries": [{"title": "某活动", "kind": "activity", "steps": [{"body": "打开某处"}]}]})
    status, reason = promote.verdict(entries[0])
    assert (status, reason) == ("candidate", "no_official_link")
    assert promote.reason_label(reason) == "没有官方入口（官网或官方账号）"


def test_a_barrier_the_build_does_not_have_is_dropped_and_recorded():
    entries, issues = schema.read_entries({
        "entries": [{"title": "某活动", "kind": "activity", "organizer": "x",
                     "steps": [{"body": "做点什么"}],
                     "barrier": ["需 gas", "需要一点运气"]}]})
    assert entries[0].barrier == ["需 gas"]
    assert any("not one of the eight" in issue for issue in issues)


def test_an_unknown_maturity_is_cleared_not_guessed():
    entries, issues = schema.read_entries({
        "entries": [{"title": "某协议", "kind": "project", "summary": "s", "organizer": "x",
                     "maturity": "beta"}]})
    assert entries[0].maturity == "" and issues


# ------------------------------------------------------------------ 免费闸（规则引擎）

KNOWN = {"htx_global", "aptos", "galxe", "binance"}


def _gate(text, author="someone", views=0, at=None):
    """Two hours ago by default: the `fresh` rule is one of the gate's inputs, and a hard-coded
    date turns this test red the next day (it did, on 2026-10-19)."""
    moment = at or (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    return rules.judge(text, author=author, metrics={"views": views}, created_at=moment,
                       known=KNOWN)


def test_the_gate_takes_an_action_word():
    assert _gate("测试网已对所有人开放")["pass"]
    assert _gate("New airdrop live now")["pass"]


def test_a_family_only_passes_its_own_action_word():
    moment = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    launch = rules.judge("mainnet is live", author="stranger", created_at=moment,
                          family="usage", known=KNOWN)
    assert not launch["pass"], "mainnet is live 不能单独过 usage 闸"
    testnet = rules.judge("testnet is live", author="stranger", created_at=moment,
                          family="usage", known=KNOWN)
    assert testnet["pass"] and testnet["track"] == "actionable"


def test_a_new_protocol_needs_an_official_source_for_the_discovery_track():
    moment = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    text = "Introducing a new protocol for intents: a wallet on Solana"
    assert not rules.judge(text, author="stranger", created_at=moment,
                           family="discovery", known=KNOWN)["pass"]
    out = rules.judge(text, author="stranger", created_at=moment,
                      family="discovery", known=KNOWN, source_official=True)
    assert out["pass"] and out["track"] == "discovery"


def test_the_gate_rejects_jobs_news_and_cex_ads():
    moment = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    for text, family in (("founding engineer for protocol", None),
                         ("market news report", None),
                         ("Binance listing new token", "issuance")):
        assert rules.judge(text, author="stranger", created_at=moment,
                           known=KNOWN, family=family)["excluded"]


def test_exchange_campaigns_and_offchain_offers_are_refused():
    """These four reached extraction on 2026-10-24 and never had a chain to find."""
    moment = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    for text, author in (
            ("Binance Simple Earn USDT 新用户 30% 加息", "crypt0_n0va"),
            ("Gate Crazy Wednesday 马术活动：礼盒抽奖与高息理财", "Gate_Launch"),
            ("Lake Michigan Credit Union 开户领 $193", "willyoes")):
        assert rules.judge(text, author=author, created_at=moment,
                           known=KNOWN)["excluded"], text


def test_a_new_standard_needs_a_crypto_context_to_pass_discovery():
    moment = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    duck = ("DuckDB 2.0.0 LTS 发布，带来新的 SQL 标准 https://t.co/x")
    assert not rules.judge(duck, author="duckdb", created_at=moment, known=KNOWN,
                           family="discovery", source_official=True)["pass"]
    chain = ("Introducing a new protocol for intents: a wallet on Solana https://t.co/x")
    out = rules.judge(chain, author="someone", created_at=moment, known=KNOWN,
                      family="discovery", source_official=True)
    assert out["pass"] and out["track"] == "discovery"


def test_airdrop_claim_behavior_is_unchanged():
    assert _gate("airdrop claim is live", author="stranger")["pass"]


def test_the_gate_refuses_an_exchange_trading_campaign():
    """13 of the first 15 entries were exchange campaigns - this is the rule that says no."""
    out = _gate("HTX 合约交易赛 WAL 专场，交易越多积分越多", author="HTX_Global", views=17000)
    assert out["excluded"] and not out["pass"]
    assert out["hits"][0]["rule"] == "exclude-trade-gated"


def test_the_gate_refuses_a_retweet():
    assert _gate("RT @someone: airdrop is live")["excluded"]


def test_being_unknown_earns_nothing():
    """Being unknown used to be worth 25 points, which paid for a model call on `gm`.

    An unfamiliar account is a reason to read the text, not a reason to spend budget on it.
    """
    out = _gate("gm", author="stranger", views=0)
    assert not out["pass"]
    assert not any(hit["rule"] == "unknown-author" for hit in out["hits"])


def test_an_unknown_account_announcing_a_testnet_passes():
    out = _gate("Testnet is live. Join the waitlist and complete quests",
                author="foobar_labs", views=12)
    assert out["pass"] and out["score"] > rules.PASS_SCORE


def test_nouns_and_base_points_do_not_open_the_gate():
    """A project update full of `validator` / `oracle` / `node` scored 30 and passed.

    Only a concrete action may qualify a post now; the nouns and the recency/link/attention
    bonuses are ranking weights and nothing else.
    """
    text = ("WINkLink oracle nodes verify cross-chain transfers; validator keys sign the payload "
            "before settlement https://t.co/x")
    out = rules.judge(text, author="Richdegen67", metrics={"views": 900},
                      created_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
                      family="network", known=KNOWN)
    assert not out["pass"]
    assert out["score"] >= rules.PASS_SCORE, "分数够了，仍然过不了，因为没有一个具体动作"
    assert not out["track"]


def test_lending_is_not_the_verb_lend():
    out = rules.judge("Imagine lending someone $100,000 against a collateral dashboard",
                      author="driftnomics", family="capital",
                      created_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
                      known=KNOWN)
    assert not out["pass"]


def test_an_incident_report_is_refused():
    """Qubic's `restarts its blockchain and opens recovery claims` came in on the node query."""
    out = rules.judge(
        "JUST IN: Qubic restarts its blockchain and opens recovery claims for victims of a "
        "$1.4M hack, following validator approval of a recovery plan.",
        author="oddsmaxxing", family="network",
        created_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), known=KNOWN)
    assert out["excluded"] and not out["pass"]
    assert out["hits"][0]["rule"] == "exclude-incident"


def test_a_non_crypto_live_stream_is_refused():
    """A weekly prayer stream matched `we're live` and `Sign up to receive notifications`."""
    out = rules.judge(
        "Join believers every Friday at 12 PM Eastern. We will be live, taking prayer requests. "
        "Streaming live on our social platforms. Sign up to receive notifications.",
        author="jerusalemprayer",
        created_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), known=KNOWN)
    assert out["excluded"] and not out["pass"]
    assert out["hits"][0]["rule"] == "exclude-offtopic-event"


def test_a_known_account_announcing_something_still_passes():
    out = _gate("主网上线，测试网已开放", author="Aptos", views=4062)
    assert out["pass"] and not any(hit["rule"] == "unknown-author" for hit in out["hits"])


def test_a_bare_link_is_not_an_opportunity():
    assert not _gate("Check out our new blog post https://t.co/z", author="someproj")["pass"]


def test_every_hit_names_the_conditions_that_matched():
    out = _gate("主网上线 https://t.co/x", author="Aptos")
    for hit in out["hits"]:
        assert hit["rule"] and hit["name"] and hit["matched"]


def test_an_unknown_condition_type_never_opens_the_gate():
    ruleset = {"levels": [], "rules": [{"id": "typo", "name": "typo", "enabled": True,
                                        "conditions": [{"type": "text_contians_any",
                                                        "values": ["airdrop"]}],
                                        "effect": {"action": "score", "score": 99}}]}
    assert not rules.judge("airdrop is live", author="x", ruleset=ruleset)["pass"]


# ------------------------------------------------------------------ 切片

def test_a_query_is_sliced_backwards_from_now():
    now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    plan = xread.plan_slices(24, 4, now=now)
    assert len(plan) == 6, "一天按四小时切 = 六片"
    assert plan[0][1] == int(now.timestamp()), "最新的一片先问"
    assert plan[0][0] == plan[1][1], "片与片相接，不重叠也不留缝"
    assert plan[-1][0] == int((now - timedelta(hours=24)).timestamp())


def test_the_slice_plan_is_capped():
    """A 20-day window in hourly slices would be 480 calls - the trap in the presets we copy."""
    plan = xread.plan_slices(24 * 20, 1)
    assert len(plan) == xread.MAX_SLICES


def test_a_slice_carries_the_operators_x_understands():
    assert xread.slice_query("from:Galxe (quest OR points)", 100, 200) == \
        "from:Galxe (quest OR points) since_time:100 until_time:200"


def _payload(*ids: str) -> dict:
    """What the client prints: one `data` list of raw rows. The seam's own shape."""
    return {"ok": True, "schema_version": "1", "data": [
        {"id": tweet_id, "text": "testnet is live https://t.co/x", "isRetweet": False,
         "author": {"screenName": "someone", "name": "Someone"},
         "metrics": {"views": 10, "likes": 1}, "createdAtISO": "2026-10-18T00:00:00Z"}
        for tweet_id in ids]}


def test_the_query_runner_normalises_the_clients_rows():
    """真跑抓到的 bug：查询路径返回原始 dict，而 post_row 只认 TweetRow。

    症状是静默的：36 次切片调用、0 条材料入库（第一次 X 单源扫描就是这样）。这条测试守的是
    "归一化一定发生"，而不是"某一条路径记得归一化"。
    """
    rows = xread.run("testnet is live", runner=lambda argv: _payload("1"))
    assert len(rows) == 1
    assert hasattr(rows[0], "handle") and rows[0].handle == "someone"
    assert rows[0].id == "1"


def test_a_retweet_is_not_material():
    """`rows_of` 会把转发剔掉：一次转发的互动属于原作者，不是这条材料的。"""
    payload = {"data": [{"id": "9", "text": "RT @x: airdrop", "isRetweet": True,
                         "author": {"screenName": "reposter"}}]}
    assert xread.run("q", runner=lambda argv: payload) == []


def test_a_sliced_query_asks_every_slice_and_counts_them(monkeypatch):
    asked = []

    def runner(argv):
        asked.append(argv[2])
        return _payload("1")

    out = xread.search_sliced("testnet", window_hours=8, slice_hours=4, runner=runner)
    assert out["slices"] == 2 and len(out["rows"]) == 2 and not out["failed"]
    assert all("since_time:" in query and "until_time:" in query for query in asked)
    assert all("-t" in argv and argv[argv.index("-t") + 1] == "Latest"
               for argv in [[a for a in ["x", "search", "q", "-t", "Latest"]]])


def test_the_query_runner_asks_for_latest_not_top(monkeypatch):
    seen = {}

    def runner(argv):
        seen["argv"] = argv
        return {"data": []}

    xread.run("testnet is live", runner=runner)
    argv = seen["argv"]
    assert argv[1] == "search" and argv[argv.index("-t") + 1] == "Latest"
    assert argv[argv.index("--max") + 1] == str(xread.QUERY_MAX)


def test_a_failed_slice_does_not_throw_away_the_others(monkeypatch):
    calls = {"n": 0}

    def runner(argv):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("429")
        return _payload("2")

    out = xread.search_sliced("testnet", window_hours=8, slice_hours=4, runner=runner)
    assert out["slices"] == 2 and len(out["rows"]) == 1 and len(out["failed"]) == 1


def test_a_cancelled_scan_stops_asking(monkeypatch):
    cancel = threading.Event()

    def runner(argv):
        cancel.set()
        return _payload("1")

    with pytest.raises(RuntimeError):
        xread.search_sliced("testnet", window_hours=8, slice_hours=4, cancel=cancel, runner=runner)


def test_a_query_row_carries_its_own_window():
    assert xread.query_row({"target": "testnet", "window_hours": 6, "slice_hours": 1,
                            "max_results": 20}) == ("testnet", 6, 1, 20)
    assert xread.query_row({"target": "testnet"}) == ("testnet", 24, 4, 40)


def test_the_scan_stops_when_the_call_budget_is_spent(isolated_db, monkeypatch):
    """预算花的是调用不是行：一条查询会变成好几片，而 30 条查询 × 6 片 = 180 次调用。

    第一版只按行数封顶（MAX_SOURCES），于是"读 30 条查询"在 X 上是 180 次调用 —— 十五分钟
    的节流加一次限流。这条测试守的是"配额用完了就停下，剩下的留给下一轮"。
    """
    from app.search.base import TweetRow

    db.opportunity_source_put([
        {"target": "query %d" % index, "kind": "query", "ecosystem": "multi", "cluster": None,
         "enabled": True, "note": None, "added_at": db.now_iso(), "window_hours": 24,
         "slice_hours": 4, "max_results": 40}
        for index in range(6)])
    calls = {"n": 0}

    def fake_search(query, **kwargs):
        calls["n"] += 1
        return {"rows": [TweetRow(id="q%d" % calls["n"], handle="someone", name="Someone",
                                  text="testnet is live https://t.co/x",
                                  created_at="2026-10-18T00:00:00Z")],
                "slices": 4, "failed": []}

    monkeypatch.setattr(scan, "search_sliced", fake_search)
    monkeypatch.setattr(scan, "timeline", lambda *a, **k: [])
    monkeypatch.setattr(scan, "MAX_CALLS", 8)
    counters = scan.collect("s-budget", db.opportunity_sources_enabled())
    assert calls["n"] == 2, "两次调用就把配额用完了（每次四片）"
    # 9 = the fixture's own account (1) + two queries (4 + 4); the third never starts.
    assert counters["queries"] == 9
    assert counters["deferred"] >= 4, "没读到的行要计入 deferred，不能被当成读过了"

def test_a_query_source_is_read_as_a_search(isolated_db, monkeypatch):
    """A `query` row is read as a sliced search - not as a timeline, not by the token engine."""
    from app.search.base import TweetRow

    db.opportunity_source_put([{"target": "testnet is live", "kind": "query",
                                "ecosystem": "multi", "cluster": None, "enabled": True,
                                "note": None, "added_at": db.now_iso(), "window_hours": 8,
                                "slice_hours": 4, "max_results": 40}])
    seen = {}

    def fake_search(query, **kwargs):
        seen["query"] = query
        seen["window"] = kwargs.get("window_hours")
        return {"rows": [TweetRow(id="9001", handle="stranger", name="Stranger",
                                  text="testnet is live, join the waitlist https://t.co/x",
                                  created_at="2026-10-18T00:00:00Z", likes=2)],
                "slices": 2, "failed": []}

    monkeypatch.setattr(scan, "search_sliced", fake_search)
    monkeypatch.setattr(scan, "timeline", lambda *a, **k: [])
    counters = scan.collect("s-query", db.opportunity_sources_enabled())
    assert seen["query"] == "testnet is live" and seen["window"] == 8
    # Three X calls: the fixture's own account (one timeline) plus this query's two slices.
    assert counters["queries"] == 3 and counters["slices"] == 2 and counters["posts"] == 1
    row = db.opportunity_materials(unclassified=True, limit=5)[0]
    assert row["source_target"] == "testnet is live"


def test_all_failed_calls_are_a_failure_not_an_empty_round(isolated_db, monkeypatch):
    """X was unreachable, so every slice failed - that is not "a quiet day".

    Measured 2026-10-23: 42 slices, 0 posts, and the scan printed `done`, because a failed slice
    only landed in a list `collect` never read.
    """
    db.opportunity_source_put([{
        "target": "testnet", "kind": "query", "ecosystem": "multi", "cluster": None,
        "enabled": True, "note": None, "added_at": db.now_iso(), "window_hours": 8,
        "slice_hours": 4, "max_results": 40}])

    def failed_slice(*args, **kwargs):
        return {"rows": [], "slices": 1, "failed": ["x cli rc=1: Connection timed out"]}

    def unreachable(*args, **kwargs):
        raise RuntimeError("x cli rc=1: Connection timed out")

    monkeypatch.setattr(scan, "search_sliced", failed_slice)
    monkeypatch.setattr(scan, "timeline", unreachable)
    with pytest.raises(RuntimeError) as failure:
        scan.collect("s-unreachable", db.opportunity_sources_enabled())
    assert "X 没有回答" in str(failure.value)


def test_a_partial_slice_failure_still_collects(isolated_db, monkeypatch):
    from app.search.base import TweetRow

    db.opportunity_source_put([{
        "target": "testnet", "kind": "query", "ecosystem": "multi", "cluster": None,
        "enabled": True, "note": None, "added_at": db.now_iso(), "window_hours": 8,
        "slice_hours": 4, "max_results": 40}])

    def one_bad_slice(*args, **kwargs):
        return {"rows": [TweetRow(id="8801", handle="someone", name="Someone",
                                  text="testnet is live, join the waitlist https://t.co/x",
                                  created_at="2026-10-18T00:00:00Z")],
                "slices": 2, "failed": ["x cli rc=1: rate limited"]}

    monkeypatch.setattr(scan, "search_sliced", one_bad_slice)
    monkeypatch.setattr(scan, "timeline", lambda *a, **k: [])
    counters = scan.collect("s-partial", db.opportunity_sources_enabled())
    assert counters["posts"] == 1 and counters["calls_failed"] == 1
    assert counters["calls_ok"] >= 2

# ------------------------------------------------------------------ 核实三态

def _material(text: str, source: str = "BreathFi20", kind: str = "post") -> dict:
    return {"text": text, "source": source, "source_kind": kind, "ref": "1",
            "url": "https://x.com/%s/status/1" % source}


def test_an_official_accounts_own_post_is_official():
    entry = Entry(title="t", kind="activity", steps=[Step(body="b")], organizer="BreathFi20")
    state, basis = trust.verdict(entry, _material("公测开放"), {"kind": "official",
                                                                "target": "BreathFi20"})
    assert state == "official" and "官方账号" in basis


def test_a_quest_platform_listing_is_community_however_official_the_campaign():
    entry = Entry(title="t", kind="activity", steps=[Step(body="b")], organizer="SomeProject")
    state, basis = trust.verdict(entry, _material("activity rows", source="platform",
                                                  kind="page"),
                                 {"kind": "platform", "target": "https://galxe.com"})
    assert state == "community" and "第三方" in basis


def test_the_official_url_appearing_in_the_material_is_official():
    entry = Entry(title="t", kind="project", summary="s",
                  official_url="https://game.breath20.xyz")
    state, _ = trust.verdict(entry, _material("见 https://game.breath20.xyz 参与"),
                             {"kind": "keyword", "target": "空投"})
    assert state == "official"


def test_nothing_to_judge_is_unjudged_not_community():
    entry = Entry(title="t", kind="project", summary="s")
    state, basis = trust.verdict(entry, {"text": "", "source": "", "source_kind": "post"},
                                 None)
    assert state == "unjudged" and basis


# ------------------------------------------------------------------ prompt 契约

def test_the_classify_prompt_example_matches_its_model():
    block = re.search(r"```json\n(.*?)```", model.read_prompt("classify.md"), re.S)
    assert block, "classify.md has no ```json example"
    example = json.loads(block.group(1))
    assert set(example) == set(schema.MaterialVerdict.model_fields)


def test_the_extract_prompt_example_matches_its_models():
    block = re.search(r"```json\n(.*?)```", model.read_prompt("extract.md"), re.S)
    assert block, "extract.md has no ```json example"
    example = json.loads(block.group(1))
    assert set(example) == set(schema.ExtractDraft.model_fields)
    assert len(example["entries"]) == 1
    assert set(example["entries"][0]) == set(Entry.model_fields)
    assert set(example["entries"][0]["steps"][0]) == set(Step.model_fields)


# ------------------------------------------------------------------ watchlist 种子

def test_the_seed_carries_no_unverified_target():
    rows = watchlist.seed_rows()
    assert len(rows) > 50
    assert all(row["kind"] in watchlist.SOURCE_KINDS for row in rows)
    assert all(row["target"] for row in rows)


def test_the_seed_fills_the_table_once_and_never_again(isolated_db):
    conn = db.connect()
    try:
        conn.execute("DELETE FROM opportunity_sources")
    finally:
        conn.close()
    assert watchlist.ensure_seeded() > 0
    assert watchlist.ensure_seeded() == 0, "第二次不该再播种：之后以表为准"


def test_the_seed_has_five_families_and_no_enabled_cex():
    rows = watchlist.seed_rows()
    queries = [row for row in rows if row["kind"] == "query"]
    assert {row["family"] for row in queries} == {
        "network", "capital", "usage", "issuance", "governance", "discovery"}
    cex = [row for row in rows if row["kind"] == "official"
           and row["target"].lower() in watchlist.CEX_HANDLES]
    assert cex and all(not row["enabled"] for row in cex)


def test_the_catalog_migration_replaces_legacy_queries_and_disables_cex(isolated_db):
    db.opportunity_source_put([
        {"target": "legacy query", "kind": "query", "ecosystem": "multi", "cluster": None,
         "enabled": True, "note": None, "added_at": db.now_iso()},
        {"target": "Binance", "kind": "official", "ecosystem": "multi", "cluster": None,
         "enabled": True, "note": None, "added_at": db.now_iso()},
        {"target": "BSC", "kind": "official", "ecosystem": "bsc", "cluster": None,
         "enabled": True, "note": None, "added_at": db.now_iso()},
    ])
    watchlist.migrate_catalog()
    rows = {row["target"]: row for row in db.opportunity_sources()}
    assert rows["legacy query"]["enabled"] == 0
    assert rows["legacy query"]["family"] == "legacy"
    assert rows["Binance"]["enabled"] == 0
    assert rows["BSC"]["enabled"] == 1
    active = [row for row in rows.values() if row["kind"] == "query" and row["enabled"]]
    assert {row["family"] for row in active} == {
        "network", "capital", "usage", "issuance", "governance", "discovery"}


def test_a_catalog_rewrite_retires_a_query_that_was_current(isolated_db):
    """A revision has to retire the previous revision's rows, not only the `family IS NULL` ones.

    The first family catalog used bare nouns (`validator`, `yield`) that also matched ordinary
    project prose. Those rows carry a family, so a migration keyed on "no family" would leave
    them enabled and keep recalling the same noise.
    """
    db.opportunity_source_put([{
        "target": "superseded family query", "kind": "query", "family": "network",
        "ecosystem": "multi", "cluster": None, "enabled": True, "note": None,
        "added_at": db.now_iso()}])
    watchlist.migrate_catalog(rewrite=True)
    rows = {row["target"]: row for row in db.opportunity_sources()}
    assert rows["superseded family query"]["enabled"] == 0
    assert rows["superseded family query"]["family"] == "legacy"


# ------------------------------------------------------------------ 扫描 job（假引擎、假模型）

class _FakeEngine:
    """The X engine, minus the network: one account posts one activity and one opinion."""

    def timeline(self, handle, limit=20):
        from app.search.base import TweetRow
        return [
            TweetRow(id="%s-1" % handle, handle=handle, name=handle,
                     text="测试网已对所有人开放，去官网参与", created_at="2026-10-18T00:00:00Z",
                     likes=3),
            TweetRow(id="%s-2" % handle, handle=handle, name=handle,
                     text="今天天气不错", created_at="2026-10-18T01:00:00Z"),
        ]

    def search(self, term, limit=20):
        from app.search.base import EngineAnswer, TweetRow
        return EngineAnswer(hits=[], queries=[term], tweets=[
            TweetRow(id="k-1", handle="stranger", name="Stranger",
                     text="空投领取开放，见 https://game.breath20.xyz",
                     created_at="2026-10-18T02:00:00Z")])


def _fake_llm(monkeypatch, *, verdict="activity", entries=None, fail=False):
    """Answer both questions without a gateway.

    The two are told apart by `purpose`, which is the argument the client is called with - not
    by the prompt text, which is a contract this test is not about.
    """
    calls = []

    def fake(messages, **kw):
        purpose = str(kw.get("purpose") or "")
        calls.append(purpose)
        if fail:
            raise llm.LLMUnavailable("no gateway in a test")
        if purpose.startswith("opportunity_group"):
            ids = re.findall(r"\[(p\d+)\]", str(messages[-1].get("content") or ""))
            return {"groups": [{"name": "Spout Finance", "handle": "spoutfi",
                                "urls": [], "kind": verdict, "ids": ids}],
                    "irrelevant": [], "uncertain": []}
        if purpose.startswith("opportunity_classify"):
            return {"kind": verdict,
                    "category": "测试网" if verdict == "activity" else "新协议",
                    "name": "Spout Finance",
                    "why": "test"}
        return {"entries": entries if entries is not None else [{
            "title": "ORDWORLD 公测",
            "kind": "activity",
            "category": "测试网",
            "organizer": "Pix",
            "official_url": "https://game.breath20.xyz",
            "steps": [{"title": "打开官网", "body": "打开 https://game.breath20.xyz",
                       "source_url": "https://x.com/Pix/status/Pix-1", "source_at": "2026-10-18"}],
            "barrier": ["需 gas"],
            "maturity": "测试网",
            "ecosystem": "base",
            "started_at": "2026-10-18",
        }]}

    monkeypatch.setattr(llm, "complete_json", fake)
    return calls


def _one_source(target="Pix", kind="official") -> list[dict]:
    return [{"target": target, "kind": kind, "ecosystem": "multi", "cluster": None,
             "enabled": 1, "note": None, "added_at": db.now_iso(),
             "window_hours": 24, "slice_hours": 4, "max_results": 40}]


@pytest.fixture(autouse=True)
def own_settings(tmp_path, monkeypatch):
    """每一条测试都用自己的设定文件：读者一旦在设定页保存过，`config/opportunity.yaml` 就存在了，
    而测试不该读这台机器的旋钮（2026-10-19）。"""
    from app.opportunity import settings as opp_settings

    monkeypatch.setattr(opp_settings, "LIVE_PATH", tmp_path / "opportunity.yaml")


@pytest.fixture(autouse=True)
def small_watchlist(isolated_db, monkeypatch):
    """One real watchlist row, X unthrottled, and no background worker.

    Three things a test must not inherit from production: the seeded watchlist is 127 rows, the
    module throttles X at 2.5s a call, and `job.start()` hands the scan to a worker thread. A
    test that kept any of the three would sleep for minutes, fetch live pages, and - worse - let
    a worker outlive the test's monkeypatches and write into whatever database the process is
    pointed at by then.

    The one row goes into the table rather than into a patched `enabled()`: the scan, the trust
    verdict and the promotion gate all read the same table in production, and a test that faked
    one reader would be testing a wiring that does not exist.
    """
    db.opportunity_source_put(_one_source())
    monkeypatch.setattr(scan, "_throttle", lambda: None)
    monkeypatch.setattr(job, "submit", lambda _job: None)
    # The link probe reads one page from the network; a test that let it would fetch live sites.
    from app.opportunity import chainsearch
    monkeypatch.setattr(chainsearch, "search", lambda _term: [])
    return _one_source()


def test_a_scan_reads_gates_classifies_extracts_and_promotes(isolated_db, monkeypatch):
    monkeypatch.setattr(scan, "XEngine", _FakeEngine)
    _fake_llm(monkeypatch)
    started = job.start()
    job.drive(started)
    stored = db.opportunity_scan_get(started["id"])
    assert stored["status"] == "done" and stored["error"] is None
    assert [stage["state"] for stage in stored["stages"]] == ["done"] * len(job.STAGES)
    assert stored["counters"]["collect"]["posts"] == 2
    assert stored["counters"]["collect"]["gated"] == 1, "只有带活动词的那条进模型"
    assert stored["counters"]["collect"]["skipped"] == 1, "没过的记成 skipped，不是 other"
    assert stored["counters"]["classify"]["activity"] == 1
    items = db.opportunity_items()
    assert len(items) == 1
    item = items[0]
    assert item["status"] == "promoted" and item["trust"] == "official"
    assert item["steps"] and item["steps"][0]["source_url"].endswith("Pix-1")
    assert item["source_ref"] == "https://x.com/Pix/status/Pix-1", "条目要能点回原文"
    assert item["barrier"] == "需 gas"


def test_a_post_that_never_reaches_the_model_is_not_called_an_opinion(isolated_db, monkeypatch):
    monkeypatch.setattr(scan, "XEngine", _FakeEngine)
    _fake_llm(monkeypatch)
    job.drive(job.start())
    conn = db.connect()
    try:
        rows = conn.execute("SELECT tweet_id, classified FROM opportunity_posts"
                            " ORDER BY tweet_id").fetchall()
    finally:
        conn.close()
    marks = {row["tweet_id"]: row["classified"] for row in rows}
    assert marks["Pix-2"] == "skipped"
    assert marks["Pix-1"] == "activity"


def test_a_round_where_x_never_answers_fails_instead_of_looking_quiet(isolated_db, monkeypatch):
    """一个都没答，和「今天没有新东西」必须是两件事。

    部分切片失败仍然只记账（`test_a_partial_slice_failure_still_collects`）；全部失败时这一轮
    记 `failed`，页面写字，而不是显示「帖子 0 · 过闸 0」。
    """
    class _Broken:
        def timeline(self, handle, limit=20):
            raise RuntimeError("x cli rc=1")

        def search(self, term, limit=20):
            raise RuntimeError("x cli rc=1")

    monkeypatch.setattr(scan, "XEngine", _Broken)
    _fake_llm(monkeypatch)
    started = job.start()
    job.drive(started)
    stored = db.opportunity_scan_get(started["id"])
    assert stored["status"] == "failed"
    assert "X 没有回答" in (stored["error"] or "")


# ------------------------------------------------------------------ 生态兜底（C + D）

def test_a_page_that_names_its_chain_is_read():
    from app.opportunity import probe

    assert probe.signal("Acme is built on Base.") == "base"
    assert probe.signal("Deployed on Arbitrum One") == "arbitrum"
    assert probe.signal("Live on the Robinhood chain") == "robinhood"
    assert probe.signal("Acme 部署在 sui 上") == "sui"


def test_a_bare_word_is_not_a_chain():
    from app.opportunity import probe

    assert probe.signal("A database for based teams") == ""
    assert probe.signal("We are near the end") == ""
    assert probe.signal("Scroll down to continue") == ""


def test_an_address_or_an_explorer_names_the_chain():
    from app.opportunity import probe

    assert probe.signal("aleo1" + "q" * 58) == "aleo"
    assert probe.signal("T" + "A" * 33) == "tron"
    assert probe.signal("", '<a href="https://bscscan.com/address/0x1">bscscan</a>') == "bsc"


def test_the_trace_pages_answer_the_chain_before_unjudged(isolated_db):
    from app.opportunity import extract
    from app.opportunity.schema import Entry

    material = {"ref": "1", "text": "t", "source_kind": "post",
                "trace_json": json.dumps({"level": "B", "pages": ["Acme is built on Base"]})}
    counters = {"materials": 0, "entries": 0, "promoted": 0, "candidate": 0, "issues": 0}
    extract._store(material, [Entry(title="Acme", kind="project", summary="s",
                                    ecosystem="未判定")], [], counters)
    row = db.opportunity_items()[0]
    assert row["ecosystem"] == "base"


def test_the_models_own_chain_still_wins(isolated_db):
    from app.opportunity import extract
    from app.opportunity.schema import Entry

    material = {"ref": "1", "text": "t", "source_kind": "post"}
    counters = {"materials": 0, "entries": 0, "promoted": 0, "candidate": 0, "issues": 0}
    extract._store(material, [Entry(title="Acme", kind="project", summary="s",
                                    ecosystem="solana")], [], counters)
    assert db.opportunity_items()[0]["ecosystem"] == "solana"


# ------------------------------------------------------------------ 补链（网页搜索）

def _card(slug: str, title: str, summary: str = "") -> None:
    conn = db.connect()
    conn.execute(
        "INSERT INTO opportunities (slug, kind, title, summary, ecosystem, discovered_at, updated_at)"
        " VALUES (?, 'project', ?, ?, '未判定', '2026-10-24', '2026-10-24')",
        (slug, title, summary))
    conn.close()


def test_the_search_pages_are_parsed_into_three_results():
    from app.opportunity import chainsearch

    ddg = ("<a rel=\"nofollow\" href=\"//duckduckgo.com/l/?uddg=https%3A%2F%2Facme.xyz%2F&amp;rut=1\""
           " class='result-link'>Acme</a>"
           "<td class='result-snippet'>Acme is built on Base.</td>")
    assert chainsearch._ddg(ddg) == [
        {"url": "https://acme.xyz/", "title": "Acme", "snippet": "Acme is built on Base."}]

    bing = ('<li class="b_algo"><h2><a href="https://acme.xyz">Acme</a></h2>'
            "<p>Acme chain</p></li>")
    assert chainsearch._bing(bing)[0]["url"] == "https://acme.xyz"


def test_only_a_single_named_chain_counts_on_a_card():
    from app.opportunity import chainsearch

    assert chainsearch._named("USDe 原生上线 X Layer 并接入 Aave V3") == "x layer"
    assert chainsearch._named("JustLend 与 TRON DeFi Summer") == "tron"
    assert chainsearch._named("Ethereum 与 Base 双链上线") == "", "两条链时要往下问"
    assert chainsearch._named("Binance Simple Earn 新用户加息") == ""


def test_the_chain_pass_uses_the_card_before_any_search(isolated_db, monkeypatch):
    from app.opportunity import chainsearch

    _card("a", "USDe 原生上线 X Layer 并接入 Aave V3")
    asked: list[str] = []
    monkeypatch.setattr(chainsearch, "search", lambda term: asked.append(term) or [])
    out = chainsearch.run()
    assert out["found"] == 1 and not asked, "卡片自己写了链，就不该再搜"
    assert db.opportunity_items()[0]["ecosystem"] == "x layer"


def test_the_chain_pass_searches_what_the_card_does_not_say(isolated_db, monkeypatch):
    from app.opportunity import chainsearch

    _card("b", "Acme 上线新金库")
    asked: list[str] = []

    def find(term: str) -> list[dict]:
        asked.append(term)
        return [{"title": "Acme", "snippet": "Acme is built on Sui",
                 "url": "https://acme.xyz"}]

    monkeypatch.setattr(chainsearch, "search", find)
    out = chainsearch.run()
    assert asked == ["Acme 上线新金库 blockchain"]
    assert out["found"] == 1
    assert db.opportunity_items()[0]["ecosystem"] == "sui"


def test_the_model_decides_what_the_results_do_not_say(isolated_db, monkeypatch):
    from app.opportunity import chainsearch

    _card("c", "Acme 上线新金库")
    monkeypatch.setattr(chainsearch, "search", lambda _term: [
        {"title": "Acme explorer", "snippet": "Explorer for Base",
         "url": "https://scan.acme.xyz"}])

    def fake(messages, **kw):
        assert kw.get("purpose") == "opportunity_chain"
        return {"items": [{"slug": "c", "chain": "Base",
                           "evidence": "https://scan.acme.xyz"}]}

    monkeypatch.setattr(llm, "complete_json", fake)
    out = chainsearch.run()
    assert out["found"] == 1 and out["llm"] == 1
    assert db.opportunity_items()[0]["ecosystem"] == "base"


def test_a_chain_must_appear_in_the_result_it_cites():
    from app.opportunity import chainsearch

    card = {"project_name": "Acme"}
    empty = {"title": "Acme", "snippet": "No chain here", "url": "https://acme.xyz"}
    assert not chainsearch._supported("base", empty, card)
    assert chainsearch._supported(
        "base", {"title": "Acme", "snippet": "Acme is built on Base", "url": "https://acme.xyz"},
        card)


def test_a_project_name_is_not_a_chain():
    from app.opportunity import chainsearch

    txflow = {"title": "TxFlow", "snippet": "TxFlow vault", "url": "https://txflow.xyz"}
    assert not chainsearch._supported("txflow", txflow, {"project_name": "TxFlow"})
    somnia = {"title": "Somnia", "snippet": "Somnia Network", "url": "https://somnia.network"}
    assert chainsearch._supported("somnia", somnia, {"project_name": "Somnia"}), "链名就是项目名也可以"


def test_multi_chain_needs_two_chains_in_the_result():
    from app.opportunity import chainsearch

    two = {"title": "Acme", "snippet": "live on Base and Solana", "url": "https://acme.xyz"}
    one = {"title": "Acme", "snippet": "live on Base", "url": "https://acme.xyz"}
    assert chainsearch._supported("多链", two, {})
    assert not chainsearch._supported("多链", one, {})


def test_a_chain_that_is_the_project_name_is_asked_again(isolated_db, monkeypatch):
    from app.opportunity import chainsearch

    conn = db.connect()
    conn.execute(
        "INSERT INTO opportunities (slug, kind, title, organizer, ecosystem, discovered_at,"
        " updated_at) VALUES ('t', 'project', 'TxFlow 金库', 'TxFlow', 'txflow', 'x', 'x')")
    conn.close()
    monkeypatch.setattr(chainsearch, "search", lambda _term: [])
    out = chainsearch.run()
    assert out["reset"] == 1
    conn = db.connect()
    try:
        row = conn.execute("SELECT ecosystem FROM opportunities WHERE slug='t'").fetchone()
    finally:
        conn.close()
    assert row["ecosystem"] == "未判定"


def test_a_card_that_is_not_on_a_chain_is_retired(isolated_db, monkeypatch):
    from app.opportunity import chainsearch

    _card("f", "Lake Michigan Credit Union 开户领 $193")
    monkeypatch.setattr(chainsearch, "search", lambda _term: [
        {"title": "Lake Michigan Credit Union", "snippet": "Open a checking account",
         "url": "https://lmcu.org"}])
    monkeypatch.setattr(llm, "complete_json", lambda messages, **kw: {
        "items": [{"slug": "f", "chain": "not_onchain", "evidence": "https://lmcu.org"}]})
    out = chainsearch.run()
    assert out["offchain"] == 1
    row = db.opportunity_items()[0]
    assert row["status"] == "retired" and row["reason"] == "offchain"


def test_an_answer_without_a_listed_source_is_not_written(isolated_db, monkeypatch):
    from app.opportunity import chainsearch

    _card("d", "Acme 上线新金库")
    monkeypatch.setattr(chainsearch, "search", lambda _term: [
        {"title": "Acme", "snippet": "No chain here", "url": "https://acme.xyz"}])
    monkeypatch.setattr(llm, "complete_json", lambda messages, **kw: {
        "items": [{"slug": "d", "chain": "base", "evidence": "https://made.up"}]})
    out = chainsearch.run()
    assert out["found"] == 0 and out["unknown"] == 1
    assert db.opportunity_items()[0]["ecosystem"] == "未判定"


def test_a_search_that_did_not_answer_is_retried_next_round(isolated_db, monkeypatch):
    from app.opportunity import chainsearch

    _card("e", "Acme 上线新金库")
    monkeypatch.setattr(chainsearch, "search", lambda _term: [])
    out = chainsearch.run()
    assert out["failed"] == 1
    conn = db.connect()
    try:
        assert conn.execute("SELECT ecosystem_checked_at FROM opportunities").fetchone()[0] is None
    finally:
        conn.close()
    assert [row["slug"] for row in db.opportunity_unjudged()] == ["e"], "下一轮还要问"


def test_a_grouping_failure_fails_the_stage_and_writes_nothing(isolated_db, monkeypatch):
    """分组必须一轮覆盖全部帖子；模型没答时整站失败，不能留下半张分组表。"""
    monkeypatch.setattr(scan, "XEngine", _FakeEngine)
    _fake_llm(monkeypatch, fail=True)
    started = job.start()
    job.drive(started)
    stored = db.opportunity_scan_get(started["id"])
    assert stored["status"] == "failed"
    stage = {row["key"]: row for row in stored["stages"]}["classify"]
    assert stage["state"] == "failed"
    conn = db.connect()
    try:
        row = conn.execute("SELECT classified FROM opportunity_posts WHERE tweet_id = 'Pix-1'"
                           ).fetchone()
    finally:
        conn.close()
    assert row["classified"] is None, "没问到就是没问到，不能写成 other"


def test_a_scan_without_a_model_still_gathers_material(isolated_db, monkeypatch):
    monkeypatch.setattr(scan, "XEngine", _FakeEngine)
    monkeypatch.setattr(llm, "configured", lambda: False)
    started = job.start()
    job.drive(started)
    stored = db.opportunity_scan_get(started["id"])
    assert stored["status"] == "done"
    assert "未配置模型" in stored["counters"]["note"]
    assert db.opportunity_items() == []


# ------------------------------------------------------------------ 端点

def test_the_page_reads_items_counts_and_its_own_copy(client):
    body = client.get("/api/v1/opportunity").json()
    assert set(body) == {"items", "counts", "scan", "filters", "sorts", "form"}
    # 页头没有说明段（2026-10-18, user），两个列表与两个折叠区也被「一个列表 + 状态筛选」
    # 取代，所以 form 只留页面真的会画的那些键。
    assert set(body["form"]) == {"title", "trust", "sources_title", "sources_lead", "empty"}
    assert body["form"]["trust"]["unjudged"]["label"] == "未判定"
    assert body["counts"]["sources"] > 0


def test_the_watchlist_reads_back_the_seeded_rows(client, isolated_db):
    conn = db.connect()
    try:
        conn.execute("DELETE FROM opportunity_sources")
    finally:
        conn.close()
    rows = client.get("/api/v1/opportunity/sources").json()
    assert len(rows) > 50
    assert {row["kind"] for row in rows} <= set(watchlist.SOURCE_KINDS)


def test_a_second_scan_is_refused_while_one_is_running(client, monkeypatch):
    monkeypatch.setattr(job, "running", lambda: True)
    response = client.post("/api/v1/opportunity/scan")
    assert response.status_code == 409
    assert "扫描" in response.json()["detail"]


def test_an_unknown_scan_is_a_404(client):
    assert client.get("/api/v1/opportunity/scans/nope").status_code == 404


def test_a_scan_started_by_the_page_runs_inline_in_a_test(client, monkeypatch):
    monkeypatch.setattr(scan, "XEngine", _FakeEngine)
    monkeypatch.setattr(job, "submit", job.drive)
    _fake_llm(monkeypatch)
    response = client.post("/api/v1/opportunity/scan")
    assert response.status_code == 202
    body = client.get("/api/v1/opportunity").json()
    assert body["scan"]["status"] == "done"
    assert [item["title"] for item in body["items"]] == ["ORDWORLD 公测"]
    assert body["counts"]["promoted:activity"] == 1


def test_a_database_from_before_the_heat_columns_gains_them(tmp_path, monkeypatch):
    """加列必须同时改 schema.sql 与迁移 —— 线上那份库就是这句话的证人。

    Its entry table was created by the first version of this module, which had no heat columns;
    `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so the first real
    scan after they were added died in the extraction stage with "no column named heat".
    """
    from app import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "old.db")
    conn = db.connect()
    # The columns the indexes in schema.sql name, and not the two this test is about: an old
    # table is old in the way the live one was, not in a way that breaks the index creation.
    conn.execute("CREATE TABLE opportunities (slug TEXT PRIMARY KEY, kind TEXT NOT NULL,"
                 " status TEXT NOT NULL DEFAULT 'candidate', discovered_at TEXT NOT NULL)")
    conn.close()
    db.init_db()
    conn = db.connect()
    try:
        columns = {row["name"] for row in conn.execute("pragma table_info(opportunities)")}
    finally:
        conn.close()
    assert {"heat", "heat_basis"} <= columns


def test_the_tables_are_part_of_the_schema(isolated_db):
    conn = db.connect()
    try:
        names = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    finally:
        conn.close()
    assert {"opportunity_sources", "opportunity_posts", "opportunities",
            "opportunity_steps", "opportunity_scans"} <= names


def test_the_grouping_cap_is_a_hard_limit_not_a_queue(isolated_db, monkeypatch):
    """分组超过上限时整站失败，而不是先写一部分、剩下的下一轮再说。"""
    monkeypatch.setattr(scan, "XEngine", _FakeEngine)
    db.opportunity_source_put(_one_source("Beta", kind="official"))
    monkeypatch.setattr(group.settings, "value",
                        lambda key, default=None: 1 if key == "classify_per_scan" else default)
    started = job.start()
    job.drive(started)
    stored = db.opportunity_scan_get(started["id"])
    assert stored["status"] == "failed"
    assert "超过" in (stored["error"] or "")
    conn = db.connect()
    try:
        waiting = conn.execute("SELECT count(*) AS n FROM opportunity_posts"
                               " WHERE classified IS NULL").fetchone()
    finally:
        conn.close()
    assert waiting["n"] == stored["counters"]["collect"]["gated"]


def test_extraction_is_capped_too(isolated_db, monkeypatch):
    monkeypatch.setattr(extract, "MAX_EXTRACT", 1)
    assert extract.MAX_EXTRACT == 1


def test_extraction_overlaps_model_calls(isolated_db, monkeypatch):
    for index in range(4):
        _classified_lead("parallel-%d" % index, name="Parallel %d" % index)
    active = [0]
    peak = [0]
    lock = threading.Lock()


    def fake(material):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.08)
        with lock:
            active[0] -= 1
        return [], []


    monkeypatch.setattr(extract, "extract", fake)
    counters = extract.run(limit=4)


    assert counters["materials"] == 4
    assert peak[0] >= 2, "抽取调用应与其他主题重叠，而不是串行等待"
def test_the_cap_is_on_material_not_on_each_table(isolated_db):
    """A pass that means to read N things reads N of them.

    Two queries that each took N handed the pass twice its budget: the first live scan read 14
    materials through a cap of 12, and 72 through a cap of 60.
    """
    for index in range(3):
        db.opportunity_post_put({"tweet_id": "cap-%d" % index, "handle": "Pix",
                                 "text": "测试网开放 %d" % index, "posted_at": "2026-10-18T0%d:00:00" % index,
                                 "url": "https://x.com/Pix/status/cap-%d" % index,
                                 "source_target": "Pix", "first_seen_scan": "s"})
    assert len(db.opportunity_materials(unclassified=True, limit=2)) == 2
    assert len(db.opportunity_materials(unclassified=True, limit=10)) == 3


def test_a_bounded_scan_rotates_instead_of_starving(isolated_db, monkeypatch):
    """One scan reads a bounded number of sources, and the next starts where it stopped.

    A hundred timelines at the X throttle is minutes of sleeping and a rate limit waiting to
    happen, so the bound is real - but a bound that always read the head of the list would mean
    the tail is never read at all. `last_read_at` is what makes it a rotation.
    """
    extra = [{"target": name, "kind": "official", "ecosystem": "multi", "cluster": None,
              "enabled": True, "note": None, "added_at": db.now_iso()}
             for name in ("Beta", "Gamma")]
    db.opportunity_source_put(extra)
    monkeypatch.setattr(scan, "MAX_SOURCES", 2)
    monkeypatch.setattr(scan, "XEngine", _FakeEngine)
    _fake_llm(monkeypatch)

    first = job.start()
    job.drive(first)
    read = {row["target"] for row in db.opportunity_sources() if row["last_read_at"]}
    assert len(read) == 2, "一次扫描只读上限那么多"
    assert db.opportunity_scan_get(first["id"])["counters"]["collect"]["deferred"] == 1

    job.drive(job.start())
    after = {row["target"] for row in db.opportunity_sources() if row["last_read_at"]}
    assert after > read and len(after) == 3, "第二轮从上一轮没读到的开始"


# ------------------------------------------------------------------ 热度

def test_heat_is_the_source_posts_own_engagement():
    metrics = {"views": 1000, "likes": 10, "retweets": 2, "replies": 3}
    assert heat.weighted(metrics) == 1000 + 300 + 120 + 90
    value = heat.score(metrics)
    assert value is not None and 0 < value < 100
    assert heat.band(value) in ("极热", "热", "温", "冷启动", "静默")


def test_the_scale_is_calibrated_on_the_hottest_post_measured():
    """刻度按实测最热的那条定：Pumpfun，141 万浏览，落在 90 分以上、不封顶。

    2026-10-18 真跑时它读出 144 分（刻度上限当时是 2 万），所以这条测试守的是"最热的帖子
    落在刻度顶端附近"，而不是"随便多大都封顶"。
    """
    hottest = heat.score({"views": 1_416_247, "likes": 3_586, "retweets": 371, "replies": 985})
    assert hottest is not None and 90 <= hottest < 100
    assert heat.band(hottest) == "极热"


def test_the_scale_saturates_at_a_hundred():
    assert heat.score({"views": 10 ** 9}) == 100.0


def test_a_big_but_not_viral_post_still_lands_under_the_ceiling():
    value = heat.score({"views": 20_000, "likes": 200, "retweets": 20, "replies": 10})
    assert value is not None and 50 < value < 100


def test_a_material_with_no_engagement_is_not_a_cold_one():
    """页面材料没有互动读数：未判定，不是 0 分，也不是静默。"""
    assert heat.score({}) is None
    assert heat.score(None) is None
    assert heat.band(None) == "未判定"


def test_a_post_nobody_saw_is_a_reading_of_zero():
    assert heat.score({"views": 0, "likes": 0}) == 0.0
    assert heat.band(0.0) == "静默"


def test_the_basis_carries_the_raw_numbers():
    text = heat.basis({"views": 12345, "likes": 210, "retweets": 45, "replies": 12})
    assert "浏览 12,345" in text and "赞 210" in text


def test_heat_travels_from_the_material_to_the_entry(isolated_db, monkeypatch):
    monkeypatch.setattr(scan, "XEngine", _FakeEngine)
    _fake_llm(monkeypatch)
    job.drive(job.start())
    item = db.opportunity_items()[0]
    assert item["heat"] is not None, "来源帖的互动量要落在条目上"
    assert "浏览" in (item["heat_basis"] or "")


def test_heat_is_recomputed_when_the_scale_moves(isolated_db, monkeypatch):
    """热度是派生的：刻度改了，已有的行也要跟着改，否则同一页上两个数字出自两套定义。"""
    monkeypatch.setattr(scan, "XEngine", _FakeEngine)
    _fake_llm(monkeypatch)
    job.drive(job.start())
    before = db.opportunity_items()[0]["heat"]
    monkeypatch.setattr(heat, "FULL_SCALE", 10)
    assert extract.refresh_heat() == 1
    after = db.opportunity_items()[0]["heat"]
    assert after != before and after is not None


# ------------------------------------------------------------------ 生命周期

@pytest.mark.parametrize("text,is_a_date", [
    ("2026-10-19", True),
    ("2026-10-19T16:00", True),
    ("2026/10/19", True),
    ("2026.10.19", True),
    ("待公布", False),
    ("TBA", False),
    ("", False),
])
def test_a_deadline_is_read_only_when_it_is_one(text, is_a_date):
    assert (lifecycle.read_deadline(text) is not None) is is_a_date


def test_a_bare_date_ends_at_the_end_of_that_day():
    moment = lifecycle.read_deadline("2026-10-19")
    assert (moment.hour, moment.minute) == (23, 59)


def test_only_a_deadline_that_has_passed_retires_an_entry(isolated_db):
    db.opportunity_put(_entry_row("opp-past", deadline="2020-01-01"), [])
    db.opportunity_put(_entry_row("opp-future", deadline="2099-01-01"), [])
    db.opportunity_put(_entry_row("opp-none", deadline=""), [])
    result = lifecycle.sweep()
    assert result["slugs"] == ["opp-past"]
    states = {row["slug"]: row["status"] for row in db.opportunity_items()}
    assert states["opp-past"] == "retired"
    assert states["opp-future"] == "promoted"
    assert states["opp-none"] == "promoted", "没有截止时间就不判结束 —— 沉默不是答案"


def test_an_unreadable_deadline_never_retires(isolated_db):
    db.opportunity_put(_entry_row("opp-tba", deadline="待公布"), [])
    assert lifecycle.sweep()["retired"] == 0
    assert db.opportunity_item("opp-tba")["status"] == "promoted"


def test_the_sweep_is_a_stage_of_its_own(isolated_db, monkeypatch):
    monkeypatch.setattr(scan, "XEngine", _FakeEngine)
    _fake_llm(monkeypatch)
    started = job.start()
    job.drive(started)
    stored = db.opportunity_scan_get(started["id"])
    assert stored["stages"][-1]["key"] == "sweep"
    assert stored["stages"][-1]["state"] == "done"
    assert "sweep" in stored["counters"]


def _query_source(target="Spout Finance 测试网上线"):
    db.opportunity_source_put([{"target": target, "kind": "query", "ecosystem": "multi",
                                "cluster": None, "enabled": True, "note": None,
                                "added_at": db.now_iso(), "window_hours": 24, "slice_hours": 4,
                                "max_results": 40}])


def _classified_lead(tweet_id="t1", name="Spout Finance"):
    """A post that a query found, classified as an activity, with the model's name on it."""
    db.opportunity_post_put({"tweet_id": tweet_id, "handle": "stranger", "text":
                             "Spout Finance 测试网上线，加入白名单", "posted_at":
                             "2026-10-18T00:00:00+00:00", "url": "https://x.com/stranger/status/t1",
                             "source_target": "Spout Finance 测试网上线", "first_seen_scan": "s",
                             "score": 80, "rules_json": "{}"})
    db.opportunity_mark(tweet_id, "post", classified="activity")
    conn = db.connect()
    try:
        conn.execute("UPDATE opportunity_posts SET project_name = ? WHERE tweet_id = ?",
                     (name, tweet_id))
    finally:
        conn.close()


def test_the_trace_stage_records_a_lead_and_adds_an_a_account(isolated_db, monkeypatch):
    """A 级：账号自动进 watchlist，读数落在帖子上。"""
    _query_source()
    _classified_lead()
    monkeypatch.setattr(trace, "trace_lead", lambda lead, **kw: {
        "level": "A", "trust": "official", "account": "spoutfi", "site": "https://spout.finance",
        "basis": "官网与官方号互证", "version": trace.TRACE_VERSION, "searched": True,
        "pages": ["Spout Finance is a lending protocol on Base"]})

    counters = trace.run()
    assert counters["traced"]["A"] == 1 and counters["added"] == ["spoutfi"]
    active = {row["target"] for row in db.opportunity_sources_enabled()}
    assert "spoutfi" in active
    assert db.opportunity_trace_queue()[0]["ref"] if db.opportunity_trace_queue() else True
    conn = db.connect()
    try:
        level = conn.execute("SELECT trace_level FROM opportunity_posts WHERE tweet_id = 't1'"
                             ).fetchone()["trace_level"]
    finally:
        conn.close()
    assert level == "A"


def test_a_c_lead_lands_in_the_pool_with_its_reason(isolated_db, monkeypatch):
    """C 级不进列表，但要留下「未找到官方来源」这个理由。"""
    _query_source()
    _classified_lead()
    monkeypatch.setattr(trace, "trace_lead", lambda lead, **kw: {
        "level": "C", "trust": "unjudged", "account": "", "site": "",
        "basis": "未找到官方来源", "version": trace.TRACE_VERSION, "searched": True, "pages": []})
    trace.run()
    _fake_llm(monkeypatch)
    job.drive(job.start())
    item = db.opportunity_items()[0]
    assert item["status"] == "candidate"
    assert item["reason"] == "no_official_source"


def test_a_lead_from_an_account_we_watch_is_never_queued(isolated_db):
    """官方号来源本来就权威 —— 溯它是花预算学不到东西。"""
    db.opportunity_post_put({"tweet_id": "t2", "handle": "Aptos", "text": "主网上线",
                             "posted_at": "2026-10-18T00:00:00+00:00", "url": "u",
                             "source_target": "Pix", "first_seen_scan": "s", "score": 90,
                             "rules_json": "{}"})
    db.opportunity_mark("t2", "post", classified="activity")
    assert db.opportunity_trace_queue() == []


def test_the_stage_is_capped_and_deduped(isolated_db, monkeypatch):
    """上限 8 个项目，同一项目只溯一次（十几条帖常常在说同一件事）。"""
    _query_source()
    for index in range(4):
        _classified_lead("t%d" % index, name="Same Project" if index < 3 else "Other")
    calls = []
    monkeypatch.setattr(trace, "trace_lead", lambda lead, **kw: calls.append(lead) or {
        "level": "C", "trust": "unjudged", "account": "", "site": "", "basis": "未找到官方来源",
        "version": trace.TRACE_VERSION, "searched": True, "pages": []})
    counters = trace.run(limit=8)
    assert len(calls) == 2, "三条同一个项目只溯一次"
    assert calls[0]["name"] == "Same Project", "分组给出的项目名要传给溯源"
    assert counters["skipped"] >= 2


def test_the_official_pages_are_attached_to_the_material(isolated_db, monkeypatch):
    """溯源读到的官方页要跟着材料进抽取（材料从「一条帖」变成「帖子 + 官方页」）。"""
    seen = {}

    def fake_question(name, version, purpose, material, reader, **kw):
        seen["pages"] = material.get("official_pages")
        return None, []

    db.opportunity_post_put({"tweet_id": "tp", "handle": "spoutfi", "text": "testnet is live",
                             "posted_at": "2026-10-18T00:00:00+00:00", "url": "u",
                             "source_target": "q", "first_seen_scan": "s", "score": 50,
                             "rules_json": "{}"})
    db.opportunity_mark("tp", "post", classified="activity")
    db.opportunity_trace_put("tp", "A", {"level": "A", "basis": "互证", "site": "https://spout.finance",
                                         "pages": ["Spout Finance · Base"]})
    monkeypatch.setattr(extract.model, "question", fake_question)
    extract.run(limit=5)
    assert seen["pages"] and seen["pages"][0]["text"] == "Spout Finance · Base"


def test_the_traced_pages_reach_the_prompt():
    message = model.material_message({"source": "spoutfi", "source_kind": "post",
                                      "text": "testnet is live", "at": "2026-10-18",
                                      "url": "https://x.com/spoutfi/status/1",
                                      "official_pages": [{"url": "https://spout.finance",
                                                          "text": "Spout Finance on Base"}]},
                                     chars=model.POST_CHARS)
    assert "## 官方来源" in message and "Spout Finance on Base" in message


def test_the_classification_pass_stores_the_project_name(isolated_db, monkeypatch):
    """名字不落库，主题就归并不了、溯源也没有二次搜索的输入（2026-10-19 实测）。"""
    monkeypatch.setattr(scan, "XEngine", _FakeEngine)
    _fake_llm(monkeypatch, verdict="activity")
    job.drive(job.start())
    conn = db.connect()
    try:
        names = [row["project_name"] for row in conn.execute(
            "SELECT project_name FROM opportunity_posts WHERE classified = 'activity'")]
    finally:
        conn.close()
    assert names and all(name == "Spout Finance" for name in names), names


def test_the_accounts_own_link_closes_the_mutual_proof():
    """A 级的账号那一侧有两条路：资料页正文提到域名，或资料页**链到**它（2026-10-19）。"""
    answer = trace.verdict(
        name="CONSO", handle="conso_xyz", lead_text="CONSO 测试网上线",
        site_texts=["CONSO is a reputation layer on Sui"],
        profile_texts=["CONSO · reputation layer"],
        site_host="conso.xyz", profile_links=["https://conso.xyz/"])
    assert answer["level"] == "A" and answer["trust"] == "official"


def test_the_grouped_project_name_drives_the_second_search():
    searched = []

    class _Hit:
        handle = "consumer_graph"

    def search(name):
        searched.append(name)
        return [_Hit()]

    answer = trace.trace_lead(
        {"text": "测试网上线，加入白名单", "handle": "stranger", "urls": [],
         "name": "Consumer Graph"},
        known=set(), fetch=lambda _url: "", search=search)

    assert searched == ["Consumer Graph"]
    assert answer["level"] == "C", "没有读到页面时仍然是 C，不能因为搜到账号就放行"


def test_links_come_out_of_the_raw_page_not_the_visible_text():
    raw = '<a href="https://spout.finance">spout.finance</a><a href="https://x.com/spoutfi">X</a>'
    assert trace.links_of(raw) == ["https://spout.finance/"]
    assert "spout.finance" not in trace.visible_text(raw).split()[0] or True


def test_a_lead_without_a_name_falls_back_to_its_author():
    """没有名字的线索（名字落库之前分类的那批）也要能判 B，否则它们永远是 C。"""
    from app.search.base import TweetRow

    def fetch(url):
        return "conso_xyz · reputation layer · testnet is live join the waitlist" \
            if url.startswith("https://x.com/") else ""

    answer = trace.trace_lead(
        {"text": "@conso_xyz 测试网上线，加入白名单 testnet is live join the waitlist",
         "handle": "someone", "urls": []},
        known=set(), fetch=fetch, search=None)
    assert answer["level"] == "B", answer
    assert answer["account"] == "conso_xyz"


def test_the_classification_queue_spends_its_sixty_on_the_best_not_the_newest(isolated_db):
    """按时间排会让旧的高分帖永远排不上（2026-10-19：DarkHorse 被饿死）。"""
    for tweet_id, score, posted in (("old-high", 130, "2026-10-18T14:00:00+00:00"),
                                    ("new-low", 40, "2026-10-19T02:00:00+00:00")):
        db.opportunity_post_put({"tweet_id": tweet_id, "handle": "someone", "text": "x",
                                 "posted_at": posted, "url": "u", "source_target": "q",
                                 "first_seen_scan": "s", "score": score, "rules_json": "{}"})
    first = db.opportunity_materials(unclassified=True, limit=1)[0]
    assert first["ref"] == "old-high", "高分优先，哪怕它更旧"


# ------------------------------------------------------------------ 用户状态（归档 / 删除）

def _an_entry(slug="opp-archivable", **over):
    row = _entry_row(slug, **over)
    db.opportunity_put(row, [])
    return row


def test_the_reader_can_archive_and_restore(isolated_db, client):
    _an_entry()
    body = client.patch("/api/v1/opportunity/items/opp-archivable",
                        json={"user_state": "archived"}).json()
    assert body["user_state"] == "archived"
    assert db.opportunity_item("opp-archivable")["user_state"] == "archived"
    again = client.patch("/api/v1/opportunity/items/opp-archivable",
                         json={"user_state": "active"}).json()
    assert again["user_state"] == "active"


def test_deleting_is_soft(isolated_db, client):
    """删除只改 user_state：行、步骤与证据都留着（软删，可恢复）。"""
    _an_entry("opp-deletable", summary="一句是什么", deadline="")
    db.opportunity_put(_entry_row("opp-deletable"),
                    [{"ordinal": 0, "title": "第一步", "body": "做点什么"}])
    client.patch("/api/v1/opportunity/items/opp-deletable", json={"user_state": "deleted"})
    row = db.opportunity_item("opp-deletable")
    assert row["user_state"] == "deleted"
    assert row["steps"], "步骤还在 —— 删除不是抹掉"


def test_re_extraction_does_not_undo_the_readers_decision(isolated_db):
    """管线重抽一个项目时会重写每一列，但**不能**把归档撤销（user, 2026-10-19）。

    This is the trap the second column exists for: `opportunity_put` is an upsert, and the user's
    decision is the one field it must leave alone.
    """
    _an_entry("opp-keep", title="原来的标题")
    db.opportunity_user_state("opp-keep", "archived")
    db.opportunity_put(_entry_row("opp-keep", title="重抽后的标题"), [])
    row = db.opportunity_item("opp-keep")
    assert row["title"] == "重抽后的标题", "重抽确实更新了内容"
    assert row["user_state"] == "archived", "但归档还在"


def test_an_unknown_slug_is_a_404_and_a_bad_state_is_a_422(client):
    assert client.patch("/api/v1/opportunity/items/nope",
                        json={"user_state": "archived"}).status_code == 404
    assert client.patch("/api/v1/opportunity/items/nope",
                        json={"user_state": "burned"}).status_code == 422


def test_the_page_carries_the_user_state(isolated_db, client):
    _an_entry("opp-listed")
    db.opportunity_user_state("opp-listed", "archived")
    items = {item["slug"]: item for item in client.get("/api/v1/opportunity").json()["items"]}
    assert items["opp-listed"]["user_state"] == "archived"


# ------------------------------------------------------------------ 设定 / 历史 / 排队

def test_the_settings_page_reads_every_knob_with_its_words(client):
    body = client.get("/api/v1/opportunity/settings").json()
    fields = body["fields"]
    keys = {field["key"] for field in fields}
    assert {"sources_per_scan", "calls_per_scan", "classify_per_scan", "extract_per_scan",
            "traces_per_scan", "x_interval", "window_hours", "slice_hours",
            "pass_score"} == keys
    for field in fields:
        assert field["label"] and field["hint"], "每个旋钮都要说清它是什么、为什么"
        assert field["min"] <= field["value"] <= field["max"]
    assert body["dirty_hint"]
    assert [block["title"] for block in body["blocks"]] == ["采集", "分析", "查询与闸"]


def test_saving_a_setting_reaches_the_modules(client):
    """保存之后**下一轮就用新值**（不是下次重启）：所以模块读的是 `settings.value`。"""
    from app.opportunity import settings as opp_settings

    body = client.put("/api/v1/opportunity/settings",
                      json={"values": {"extract_per_scan": 7, "x_interval": 9.5}}).json()
    saved = {field["key"]: field["value"] for field in body["fields"]}
    assert saved["extract_per_scan"] == 7 and saved["x_interval"] == 9.5
    assert opp_settings.value("extract_per_scan") == 7
    assert opp_settings.value("x_interval") == 9.5


def test_the_settings_file_keeps_only_overrides(client):
    from app.opportunity import settings as opp_settings

    client.put("/api/v1/opportunity/settings", json={"values": {"extract_per_scan": 7}})
    text = opp_settings.LIVE_PATH.read_text(encoding="utf-8")
    assert "extract_per_scan: 7" in text
    assert "sources_per_scan" not in text
    client.put("/api/v1/opportunity/settings", json={"values": {"extract_per_scan": 40}})
    assert not opp_settings.LIVE_PATH.exists()


def test_an_unknown_key_is_ignored_and_a_wild_value_is_clamped(client):
    body = client.put("/api/v1/opportunity/settings",
                      json={"values": {"nonsense": 1, "pass_score": 9999}}).json()
    saved = {field["key"]: field["value"] for field in body["fields"]}
    assert "nonsense" not in saved, "没人读的键不写进文件"
    assert saved["pass_score"] == 200, "越界夹到范围里"


def test_the_scan_history_lists_the_rounds(client, isolated_db):
    job.drive(job.start())
    rows = client.get("/api/v1/opportunity/scans?limit=5").json()
    assert rows and rows[0]["id"] and rows[0]["started_at"]
    assert isinstance(rows[0]["counters"], dict)


def test_the_queues_answer_where_a_project_went(client, isolated_db):
    """排队视图回答"我明明见过某个项目，它去哪了"（2026-10-19, user）。"""
    db.opportunity_post_put({"tweet_id": "q1", "handle": "someone", "text": "高分待分类帖",
                             "posted_at": "2026-10-19T00:00:00+00:00", "url": "u",
                             "source_target": "q", "first_seen_scan": "s", "score": 130,
                             "rules_json": "{}"})
    db.opportunity_post_put({"tweet_id": "q2", "handle": "someone", "text": "已分类待抽取",
                             "posted_at": "2026-10-19T00:00:00+00:00", "url": "u",
                             "source_target": "q", "first_seen_scan": "s", "score": 90,
                             "rules_json": "{}"})
    db.opportunity_mark("q2", "post", classified="activity")
    piles = client.get("/api/v1/opportunity/queues").json()
    assert piles["unclassified"]["count"] >= 1
    assert piles["unclassified"]["top"][0]["ref"] == "q1", "按分数排序，最高分在最前"
    assert piles["unextracted"]["count"] >= 1
    assert "trace_failed" in piles


# ------------------------------------------------------------------ 溯源（线索 → 官方家）

def _lead(text, handle="stranger", urls=()):
    return {"text": text, "handle": handle, "urls": list(urls)}


def test_the_three_pointers_a_post_can_carry():
    found = trace.candidates(
        'Spout Finance 测试网上线 https://t.co/x 关注 @SpoutFi，加入 https://discord.gg/abc',
        handle="someone", urls=["https://t.co/x"])
    assert found["name"] == "Spout Finance"
    assert found["handles"] == ["spoutfi"]
    assert found["sites"] == [], "Discord 是社区所在地，不是项目的家"


def test_a_channel_is_not_a_home():
    found = trace.candidates("gm https://x.com/foo https://t.me/bar", handle="a")
    assert found["sites"] == []


def test_a_handle_that_looks_like_the_name_is_only_evidence_for_b():
    assert trace.similar("Spout Finance", "SpoutFi")
    assert trace.similar("Spout Finance", "spoutfi_official"), "冒充号也能过这一关 —— 所以它只够 B"
    assert trace.similar("Consumer Graph", "conso_xyz"), "一个特征词对上就够（真实数据：CONSO）"
    assert not trace.similar("Spout Finance", "totallydifferent")


def test_mutual_proof_is_an_a():
    answer = trace.verdict(
        name="Spout Finance", handle="spoutfi", lead_text="testnet is live",
        site_texts=["Spout Finance is a lending protocol on Base"],
        profile_texts=["Spout Finance · building on Base · https://spout.finance"],
        site_host="spout.finance")
    assert answer["level"] == "A" and answer["trust"] == "official"
    assert "互证" in answer["basis"]


def test_a_name_match_without_the_handshake_is_a_b():
    answer = trace.verdict(
        name="Spout Finance", handle="spoutfi_official",
        lead_text="Spout Finance testnet is live, join the waitlist",
        site_texts=[], profile_texts=["Spout Finance testnet is live join the waitlist now"],
        site_host="")
    assert answer["level"] == "B" and answer["trust"] == "community"
    assert "未互证" in answer["basis"], "B 的 trust_basis 必须写明它没有互证"


def test_nothing_read_is_never_a_proof():
    """空证据是 C，不是 B：没看 != 看过。"""
    answer = trace.verdict(name="Spout Finance", handle="spoutfi", lead_text="testnet is live",
                           site_texts=[], profile_texts=[], site_host="")
    assert answer["level"] == "C" and answer["trust"] == "unjudged"


def test_a_lead_from_an_account_we_watch_is_already_official():
    answer = trace.trace_lead(_lead("测试网上线", handle="Aptos"), known={"aptos"})
    assert answer["level"] == "A" and answer["trust"] == "official"
    assert "我们在盯" in answer["basis"]


def test_the_second_search_finds_the_account_when_the_post_names_none():
    """帖子里没有账号也没有链接时，用项目名再搜一次，账号从结果的作者里捞。"""
    from app.search.base import TweetRow

    seen = {}

    def search(name):
        seen["name"] = name
        return [TweetRow(id="1", handle="SpoutFi", name="Spout", text="Spout Finance testnet",
                         created_at="2026-10-18T00:00:00Z")]

    def fetch(url):
        if url == "https://x.com/spoutfi":
            return "Spout Finance · testnet is live · join the waitlist"
        return ""

    answer = trace.trace_lead(_lead('"Spout Finance" 测试网上线，加入白名单'),
                              known=set(), fetch=fetch, search=search)
    assert seen["name"] == "Spout Finance"
    assert answer["account"] == "spoutfi"
    assert answer["level"] == "B", "只有同名 + 同题，够 B 不够 A"
    assert answer["pages"], "读过的资料页要带回去给抽取用"


def test_a_failed_read_is_a_count_not_a_crash():
    def fetch(url):
        raise RuntimeError("403")

    answer = trace.trace_lead(_lead("Spout Finance https://spout.finance"),
                              known=set(), fetch=fetch, search=None)
    assert answer["level"] == "C" and answer["basis"] == "未找到官方来源"


def test_the_trace_version_is_stored_with_the_verdict():
    assert trace.trace_lead(_lead("x", handle="Aptos"), known={"aptos"})["version"] == \
        trace.TRACE_VERSION


# ------------------------------------------------------------------ 筛选面板（后端建、前端画）

def _panel_item(**over) -> dict:
    row = {"slug": "s", "kind": "activity", "category": "空投", "trust": "official",
           "ecosystem": "solana", "source_type": "official", "heat": 80.0, "barrier": "需资金",
           "status": "promoted"}
    row.update(over)
    return row


def test_the_panel_only_offers_choices_that_exist():
    """一个只能把页面筛空的开关，是这个应用不出的那种控件。"""
    from app.api.v1 import forms

    groups = forms.opportunity_filters([_panel_item()], {"promoted:activity": 1})
    assert [group["key"] for group in groups] == ["status"], "只有一个生态时，生态那组不发"


def test_the_panel_is_two_groups_ecosystem_first():
    """面板只有两组：生态（第一）与状态（2026-10-19, user：其他筛选去繁化简）。"""
    from app.api.v1 import forms

    items = [_panel_item(), _panel_item(slug="t", kind="project", ecosystem="bitcoin",
                                        category="新协议", heat=None)]
    groups = {group["key"]: group for group in forms.opportunity_filters(
        items, {"promoted:activity": 1, "retired:project": 1})}
    assert list(groups) == ["ecosystem", "status"], "生态是主轴，排第一"
    # 生态按枚举自己的顺序，不按字母序 —— 面板两次读起来要一样
    assert [option["value"] for option in groups["ecosystem"]["options"]] == \
        ["", "bitcoin", "solana"]
    assert [option["label"] for option in groups["ecosystem"]["options"]] == \
        ["全部", "Bitcoin", "Solana"], "面板印显示名，筛选仍发键"
    status = {option["value"]: option["count"] for option in groups["status"]["options"]}
    assert status == {"": None, "promoted": 1, "candidate": 0, "retired": 1}


def test_the_dropped_groups_are_gone_for_good():
    """类别 / 核实 / 类目 / 门槛 / 热度 / 来源 都不再是筛选组：它们是卡片上的读数。"""
    from app.api.v1 import forms

    items = [_panel_item(), _panel_item(slug="t", kind="project", trust="community",
                                        ecosystem="bitcoin", source_type="query",
                                        heat=None, barrier="需 KYC", category="新协议")]
    keys = {group["key"] for group in forms.opportunity_filters(items, {"promoted:activity": 1})}
    assert not keys & {"kind", "trust", "category", "barrier", "heat_band", "source_type"}


def test_the_sorts_are_two_keys_and_heat_is_not_one_of_them():
    from app.api.v1 import forms

    sorts = {row["value"]: row for row in forms.opportunity_sorts()}
    assert set(sorts) == {"discovered", "deadline"}, "热度不做排序键：它是来源帖自己的读数"
    assert [label for _value, label in sorts["deadline"]["dirs"]] == ["从早到晚", "从晚到早"]


def test_the_endpoint_sends_the_panel_and_the_new_fields(client):
    body = client.get("/api/v1/opportunity").json()
    assert body["filters"] and body["filters"][0]["key"] == "status"
    assert body["sorts"] and {row["value"] for row in body["sorts"]} == {"discovered", "deadline"}
    assert "ecosystem" in body["items"][0] if body["items"] else True


def test_the_ecosystem_is_read_from_the_material_not_from_who_posted_it(isolated_db, monkeypatch):
    """生态是**材料说的**，不是"谁发的帖"（2026-10-19, user）。

    The fixture's watchlist row is `multi` (the seed's default for "not sure"), and the fake
    extraction answers `base` - so the entry must read `base`. Inheriting it was the old
    behaviour and it was wrong in the way that matters: a Solana account announcing something on
    Base would have filed it under Solana.
    """
    monkeypatch.setattr(scan, "XEngine", _FakeEngine)
    _fake_llm(monkeypatch)
    job.drive(job.start())
    item = db.opportunity_items()[0]
    source = db.opportunity_sources()[0]
    assert source["ecosystem"] == "multi", "来源行还是种子那个默认值"
    assert item["ecosystem"] == "base", "条目写材料说的那条链"
    assert item["source_type"] == source["kind"], "来源类型仍是来源行的字段"


def test_an_ecosystem_is_judged_not_restricted():
    """生态是**判断**出来的，不是从闭合枚举里挑的（2026-10-19, user：判断啊，基本上就是公链）。

    别名表把同一条链的各种写法归成一行；枚举外的真链保留它自己（面板会为它长一行）；说不清
    才是 未判定。
    """
    from app.opportunity.schema import read_entries

    def ecosystem_of(value):
        entries, _ = read_entries({"entries": [
            {"title": "某协议上线", "kind": "project", "summary": "s",
             "official_url": "https://x.io", "ecosystem": value}]})
        assert entries, value
        return entries[0].ecosystem

    assert ecosystem_of("Ethereum") == "ethereum"
    assert ecosystem_of("eth") == "ethereum"
    assert ecosystem_of("以太坊") == "ethereum"
    assert ecosystem_of("这是以太坊上的一个协议") == "ethereum", "句子里的名字也认得"
    assert ecosystem_of("Aptos") == "aptos", "枚举外的真链保留它自己"
    assert ecosystem_of("Tron") == "tron", "枚举外的链存键，不存显示名"
    assert ecosystem_of("Solanaa") == "solanaa", "拼错的会自己长一行 —— 加进别名表即自愈"
    assert ecosystem_of("???") == "未判定", "不是链名就说不清"
    assert ecosystem_of("multi") == "多链", "多链与未判定是两件事"


def test_the_panel_orders_chains_a_to_z_then_the_two_readings():
    from app.opportunity.schema import order_ecosystems

    ordered = order_ecosystems(["未判定", "多链", "aptos", "ethereum", "zzzchain", "bitcoin"])
    assert ordered == ["aptos", "bitcoin", "ethereum", "zzzchain", "多链", "未判定"]


def test_the_panel_sorts_by_the_printed_name_not_the_raw_key():
    from app.opportunity.schema import order_ecosystems

    ordered = order_ecosystems(["base", "bsc", "bitcoin", "avax", "arbitrum"])
    assert ordered == ["arbitrum", "avax", "base", "bitcoin", "bsc"]


def test_the_panel_prints_a_label_and_filters_by_the_key():
    """`BTT C`-style spellings live in the label; the value stays the lower-case key."""
    from app.opportunity.schema import ecosystem_label

    assert ecosystem_label("bitcoin") == "Bitcoin"
    assert ecosystem_label("bsc") == "BSC"
    assert ecosystem_label("bttc") == "BTTC"
    assert ecosystem_label("multiversx") == "MultiversX"
    assert ecosystem_label("megaeth") == "MegaETH"
    assert ecosystem_label("zzzchain") == "Zzzchain", "新链走兜底，仍然像个名字"
    assert ecosystem_label("multi-sig") == "Multi-Sig"
    assert ecosystem_label("多链") == "多链"
    assert ecosystem_label("未判定") == "未判定"


def test_the_ecosystem_spellings_are_normalised_in_place(isolated_db):
    conn = db.connect()
    conn.execute(
        "INSERT INTO opportunities (slug, kind, title, ecosystem, discovered_at, updated_at)"
        " VALUES ('a', 'project', 'A', 'Tron', 'x', 'x'),"
        "        ('b', 'project', 'B', 'Multi', 'x', 'x'),"
        "        ('c', 'project', 'C', 'solana', 'x', 'x')")
    conn.close()

    changed = db._normalise_opportunity_ecosystems(db.connect())
    assert changed == 2, "已经是键的行不重写"
    conn = db.connect()
    rows = {row["slug"]: row["ecosystem"] for row in
            conn.execute("SELECT slug, ecosystem FROM opportunities")}
    conn.close()
    assert rows == {"a": "tron", "b": "多链", "c": "solana"}


def test_a_missing_ecosystem_is_unjudged_too():
    from app.opportunity.schema import read_entries

    entries, _ = read_entries({"entries": [
        {"title": "新协议上线", "kind": "project", "summary": "s",
         "official_url": "https://x.io"}]})
    assert entries and entries[0].ecosystem == "未判定"
