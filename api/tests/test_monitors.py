"""监控：一个监控一套卡、一个自己的时钟（2026-10-29, user）。"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.store import db


@pytest.fixture(autouse=True)
def _isolated_monitor_config(tmp_path, monkeypatch):
    """设定文件在第一次读取时落盘，测试不能往仓库的 monitors/<id>/ 里写。"""
    from app.monitors.core import chain, settings as monitor_settings

    monkeypatch.setattr(monitor_settings, "ROOT", tmp_path)
    # No test may reach a public node: every chain reading is either stubbed or absent.
    monkeypatch.setattr(chain, "_rpc", lambda *args, **kwargs: None)


def _card(**over) -> dict:
    base = {
        "slug": "acme", "title": "Acme", "project": "Acme", "handle": "acme",
        "chain": "BSC", "status": "live", "price": "free", "supply": "1000",
        "deadline": None, "summary": "s", "official_url": None, "source_url": None,
        "fields": {"price": "free"}, "version": "v1",
        "first_seen": "2026-10-29T01:00:00+08:00",
        "updated_at": "2026-10-29T01:00:00+08:00",
    }
    base.update(over)
    return base


def test_a_card_keeps_the_moment_the_question_first_saw_it(isolated_db):
    db.monitor_card_put("m1", _card())
    db.monitor_card_put("m1", _card(title="Acme II",
                                    updated_at="2026-10-29T02:00:00+08:00"))
    rows = db.monitor_cards("m1")
    assert len(rows) == 1, "同一个 slug 只更新一行"
    assert rows[0]["title"] == "Acme II"
    assert rows[0]["first_seen"] == "2026-10-29T01:00:00+08:00", "首次发现的时间不动"
    assert rows[0]["fields"] == {"price": "free"}


def test_two_monitors_never_share_a_row(isolated_db):
    db.monitor_card_put("m1", _card())
    db.monitor_card_put("m2", _card())
    assert db.monitor_card_counts() == {"m1": 1, "m2": 1}


def test_the_clock_owes_the_default_slot_with_nothing_saved(isolated_db):
    from app.monitors.core import schedule

    at = datetime(2026, 10, 29, 10, 0, tzinfo=schedule.TZ)
    assert schedule.due("binance-agent-id", at) == \
        datetime(2026, 10, 29, 9, 15, tzinfo=schedule.TZ), "默认 01:15 + 8h"

    db.monitor_scan_put({"id": "s1", "monitor": "binance-agent-id", "status": "done",
                         "stages": [], "counters": {},
                         "started_at": "2026-10-29T09:20:00+08:00"})
    assert schedule.due("binance-agent-id", at) is None, "跑过就不再欠这一格"


def test_the_page_lists_the_monitors_this_build_knows(client):
    body = client.get("/api/v1/monitors").json()
    assert [row["id"] for row in body["monitors"]] == ["binance-agent-id"]
    assert body["monitors"][0]["cards"] == 0


def test_the_chain_reader_takes_an_address_and_merges_three_sources(monkeypatch):
    """Supply from the node, market from DexScreener, safety from GoPlus - each may be absent."""
    from app.monitors.core import chain

    ca = "0x" + "a" * 40
    assert chain.first_address("no address here", "mint at " + ca.upper().replace("0X", "0x")) == ca

    class Metrics:
        honeypot_detected = False
        buy_tax_pct = 0.0
        sell_tax_pct = 3.0
        holders = 1200
        top10_pct = 42.0

    class Facts:
        lp_locked = True
        dangerous_perms: list[str] = []

    class Dex:
        price_usd = 0.5
        liquidity_usd = 12000.0
        market_cap_usd = 50000.0

    monkeypatch.setattr(chain.goplus.GoPlus, "fetch_token",
                        lambda self, c, a: (Metrics(), Facts()))
    monkeypatch.setattr(chain.dexscreener.DexScreener, "fetch_token",
                        lambda self, c, a: (Dex(), None))
    # totalMinted() 走的是 _uint；这里没有网络，直接回答「这个合约没有它」。
    monkeypatch.setattr(chain, "_call", lambda address, data, **kw: None)
    monkeypatch.setattr(chain, "_uint", lambda address, data, **kw: None)
    monkeypatch.setattr(chain, "total_supply", lambda address, **kw: 4444 * 10**18)
    monkeypatch.setattr(chain, "decimals", lambda address, **kw: 18)
    monkeypatch.setattr(chain, "is_erc721", lambda address, **kw: False)
    chain.token_facts.cache_clear()

    info = chain.probe(ca)
    assert info["token_total"] == 4444, "20 的 totalSupply 是代币总量，不是铸造份数"
    assert "minted" not in info
    assert "token" not in info, "只有 decimals / totalSupply 不够：symbol 也必须有"
    assert info["liquidity_usd"] == 12000.0
    assert info["sell_tax"] == 3.0
    assert info["honeypot"] is False
    assert info["lp_locked"] is True

    monkeypatch.setattr(chain, "is_erc721", lambda address, **kw: True)
    monkeypatch.setattr(chain, "total_supply", lambda address, **kw: 624)
    monkeypatch.setattr(chain, "decimals", lambda address, **kw: None)
    monkeypatch.setattr(chain, "max_supply", lambda address, **kw: 95669)
    nft = chain.probe(ca)
    assert nft["minted"] == 624, "721 的 totalSupply 才是已铸造份数"
    assert nft["mint_total"] == 95669


def _abi_string(text: str) -> str:
    raw = text.encode().hex()
    return ("0x" + format(32, "x").rjust(64, "0")
            + format(len(text.encode()), "x").rjust(64, "0") + raw.ljust(64, "0"))


def _word(value: int) -> str:
    return "0x" + format(value, "x").rjust(64, "0")


def test_the_token_behind_a_sale_contract_is_read_from_the_contract(monkeypatch):
    """JAGENT 的铸造合约只会 `totalMinted()`；代币是它 `token()` 指的那个地址（2026-10-30）。"""
    from app.monitors.core import chain

    mint = "0x" + "1" * 40
    token = "0x" + "2" * 40
    table = {
        (mint, "0xfc0c546a"): "0x" + "0" * 24 + token[2:],
        (mint, chain.TOTAL_SUPPLY): None,
        (token, chain.SYMBOL): _abi_string("JAGENT"),
        (token, chain.DECIMALS): _word(18),
        (token, chain.TOTAL_SUPPLY): _word(1025669384479 * 10**18),
        (token, chain.ERC721): _word(0),
    }
    monkeypatch.setattr(chain, "_call", lambda address, data, **kw: table.get(
        (str(address).lower(), data)))
    chain.token_facts.cache_clear()

    assert chain.token_facts(mint) == {}, "铸造合约不是代币"
    found = chain.token_of(mint)
    assert found["token"] == token
    assert found["token_symbol"] == "JAGENT"
    assert found["token_decimals"] == 18


def test_an_address_from_the_site_still_has_to_pass_the_chain_check(monkeypatch):
    """官网印的地址不算数，symbol / decimals / totalSupply 齐了才算（2026-10-30, user）。"""
    from app.monitors.core import chain

    pass_only = "0x" + "3" * 40
    real = "0x" + "4" * 40
    table = {
        (pass_only, chain.SYMBOL): _abi_string("MINT PASS"),
        (pass_only, chain.DECIMALS): None,
        (real, chain.SYMBOL): _abi_string("REAL"),
        (real, chain.DECIMALS): _word(18),
        (real, chain.TOTAL_SUPPLY): _word(10**24),
        (real, chain.ERC721): _word(0),
    }
    monkeypatch.setattr(chain, "_call", lambda address, data, **kw: table.get(
        (str(address).lower(), data)))
    chain.token_facts.cache_clear()

    assert chain.token_first(pass_only, real)["token"] == real
    assert chain.token_first(pass_only) == {}


def test_the_lp_chip_reads_the_lp_table_not_the_dollar_size():
    """能不能撤池看 LP 在谁手里：锁住 / 烧掉多少，不是流动性有多少钱（2026-10-30）。"""
    from app.monitors.core import chain

    class Metrics:
        def __init__(self, rows):
            self.lp_holders = rows

    locked = Metrics([{"pct": 98.0, "locked": True, "burnt": False},
                      {"pct": 2.0, "locked": False, "burnt": False}])
    assert chain.lp_state(locked)["lp_state"] == "locked"
    assert chain.lp_state(locked)["lp_locked_pct"] == 98.0

    burnt = Metrics([{"pct": 100.0, "locked": False, "burnt": True}])
    assert chain.lp_state(burnt)["lp_state"] == "burnt"

    loose = Metrics([{"pct": 30.0, "locked": False, "burnt": False},
                     {"pct": 70.0, "locked": False, "burnt": False}])
    assert chain.lp_state(loose)["lp_state"] == "unlocked"
    assert chain.lp_state(Metrics([])) == {}, "没有 LP 表就是没回答，不写 0"


def test_a_pool_that_vanishes_between_two_rounds_is_a_rug():
    """撤池只有跨轮才判得出来：上一轮的池子 / 流动性对不上这一轮（2026-10-30, user）。"""
    from app.monitors.core import job

    assert job._rug({"pool": "0xpair", "pools": 1}, {"pools": 0})["state"] == "gone"
    assert job._rug({"pool": "0xpair", "pools": 1}, {}) == {}, "没回答不是撤池"
    assert job._rug({}, {"pools": 0}) == {}, "第一轮没有上一轮，不下结论"
    assert job._rug({"pool_liquidity_usd": 100000}, {"pool_liquidity_usd": 5000})["state"] \
        == "drained"
    assert job._rug({"pool_liquidity_usd": 100000}, {"pool_liquidity_usd": 90000})["state"] \
        == "live"


def test_a_count_without_a_total_is_still_progress():
    """agentsea 只写「3,073 cards claimed」，没有分母；这也是开卖的证据（2026-10-30）。"""
    from app.monitors.core import chain

    assert chain.parse_mint("3,073 cards claimed\n3,050 verified with a Binance Agent ID") \
        == {"done": 3073}
    # 渲染结果有时带标签：`3,073</b><span>cards claimed` 也要能读出来。
    assert chain.parse_mint("<b>3,073</b><span>cards claimed</span>") == {"done": 3073}
    assert chain.parse_mint("8,210 minted") == {"done": 8210}
    assert chain.parse_mint("9,004 / 444,000 shares") == {"done": 9004, "total": 444000}
    assert chain.parse_mint("nothing here") == {}
    # 每钱包上限旁边的 x / y 不是铸造进度（BNBS 的 10 shares per wallet）。
    assert chain.parse_mint("Tier 1 / 10 shares per wallet\nMinted 29 / 7,000") \
        == {"done": 29, "total": 7000}


def test_the_fill_excerpt_keeps_the_price_line():
    """价格和进度写在同一张表里，取窗口时不能只认「minted」（Phosphor 的 Price 1 USDT）。"""
    from app.monitors.core import job

    text = "Minted 6 / 10000\n" + ("filler " * 400) + "\nPrice 1 USDT\nLimit 5 cards"
    assert "1 USDT" in job._excerpt(text)


def test_the_page_title_wins_over_the_model_title():
    """官网 <title> 稳定，模型的叫法每轮会变（JAGENT → JAGENT Mint Pass，2026-10-30）。"""
    from app.monitors.core import job

    assert job._site_title(["<title>agentsea — profile pre-registration</title>"]) == "agentsea"
    assert job._site_title(["<title>$JAGENT Mint — BNB Smart Chain</title>"]) == "$JAGENT Mint"
    assert job._site_title(['<meta property="og:title" content="Phosphor Slot">']) \
        == "Phosphor Slot"
    assert job._site_title(["plain text, no tags"]) == ""
    # 挑战页 / 营销标语不能当项目名；逗号后的副标题要砍掉。
    assert job._site_title(["<title>Attention Required! | Cloudflare</title>"], ["IDENTIDE"]) == ""
    assert job._site_title(["<title>Weave ideas into markets.</title>"],
                           ["Proof of Agent (Ptah)"]) == ""
    assert job._site_title(["<title>Moribundus, One per Agent ID</title>"],
                           ["Moribundus"]) == "Moribundus"


def test_a_tagline_or_profile_title_is_not_a_name_even_without_the_model():
    from app.monitors.core import job

    assert job._site_title(["<title>Weave ideas into markets.</title>"]) == ""
    assert job._site_title(["<title>alice (@alice) / X</title>"]) == ""


def test_a_card_title_is_frozen_until_the_site_renames_it_twice():
    from app.monitors.core import job

    head = {"title": "$JAGENT Mint",
            "fields": {"site_title": "$JAGENT Mint", "title_source": "site"}}
    row = {"title": "$JAGENT", "fields": {}}
    job._settle_title(row, head, "$JAGENT Mint")
    assert row["title"] == "$JAGENT Mint"
    assert row["fields"]["title_source"] == "site"

    once = {"title": "$JAGENT", "fields": {}}
    job._settle_title(once, head, "$JAGENT MINT v2")
    assert once["title"] == "$JAGENT Mint"
    assert once["fields"]["site_title_pending"] == "$JAGENT MINT v2"

    head2 = {"title": "$JAGENT Mint", "fields": dict(once["fields"])}
    twice = {"title": "$JAGENT", "fields": {}}
    job._settle_title(twice, head2, "$JAGENT MINT v2")
    assert twice["title"] == "$JAGENT MINT v2"
    assert "site_title_pending" not in twice["fields"]


def test_a_model_named_card_adopts_the_site_name_once():
    from app.monitors.core import job

    legacy = {"title": "JAGENT", "fields": {"title_source": "model"}}
    row = {"title": "JAGENT", "fields": {}}
    job._settle_title(row, legacy, "$JAGENT Mint")
    assert row["title"] == "$JAGENT Mint"
    assert row["fields"]["site_title"] == "$JAGENT Mint"


def test_the_page_call_rewrites_summary_and_extracts_only_page_fields(monkeypatch):
    """The official page is read once: prose and page-owned fields share one call."""
    from app.monitors.core import job
    from app.monitors.registry import BINANCE_AGENT_ID

    seen = {}

    def fake(messages, **kw):
        seen["text"] = messages[1]["content"]
        seen.update(kw)
        return {"summary": "官网口径：每钱包 10 份，每份 10 万 BNBS。", "domain": "crypto",
                "price": "0.02 BNB", "deadline": "2026-11-10", "mint_unit": "shares",
                "mint_kind": "mint"}

    monkeypatch.setattr(job.llm, "complete_json", fake)
    out = job._page_card(
        BINANCE_AGENT_ID,
        {"pages": ["<p>Send 0.01 BNB to receive 100,000 BNBS. 10 shares per wallet</p>"]},
        run_id="scan-1",
    )
    assert out["summary"].startswith("官网口径")
    assert out["domain"] == "crypto"
    assert "10 shares per wallet" in seen["text"], "喂给模型的是官网原文"
    assert seen["purpose"] == "monitor_page"
    assert seen["run_id"] == "scan-1"
    assert job._page_card(BINANCE_AGENT_ID, {"pages": []}, run_id="scan-1") == {}


def test_a_page_call_that_found_nothing_keeps_the_old_summary(monkeypatch):
    """页面只有壳时模型会写「官网原文未提供」；这句话不能覆盖原摘要（Nemotron / ChyAI）."""
    from app.monitors.core import job
    from app.monitors.registry import BINANCE_AGENT_ID

    monkeypatch.setattr(
        job.llm, "complete_json",
        lambda messages, **kw: {"summary": "官网原文未提供，无法确认项目内容。"})
    assert job._page_card(
        BINANCE_AGENT_ID, {"pages": ["<p>loading</p>"]}, run_id="scan-1"
    ).get("summary", "") == ""
    monkeypatch.setattr(
        job.llm, "complete_json",
        lambda messages, **kw: {"summary": "HODLER 是像素宇航员项目，官方原文未说明参与方式与门槛。"})
    assert job._page_card(
        BINANCE_AGENT_ID, {"pages": ["<p>hodler</p>"]}, run_id="scan-1"
    ).get("summary", "") == ""


def test_an_unjudged_field_is_still_missing():
    """模型写的「未判定」不是答案，补字段阶段必须继续找（Phosphor 的 Price 1 USDT）。"""
    from app.monitors.core import job

    assert job._blank("未判定") and job._blank(" unknown ") and job._blank("")
    assert not job._blank("1 USDT")
    assert job._missing_page_fields({"price": "未判定", "mint_unit": "cards",
                                     "deadline": "2026-11-01", "mint_kind": "mint"})


def test_the_page_prompt_does_not_ask_for_chain_owned_fields():
    from pathlib import Path

    text = Path("app/monitors/binance-agent-id/prompts/summary.md").read_text(encoding="utf-8")
    assert '"summary"' in text and '"domain"' in text
    assert '"price"' in text and '"deadline"' in text
    assert "mint_done" not in text
    assert "mint_total" not in text
    assert "token_total" not in text
    assert "contract" not in text


def test_page_fields_never_overwrite_chain_values():
    from app.monitors.core import job

    project = {"mint_done": 12, "mint_total": 100, "token_total": 999,
               "contract": "0x" + "1" * 40, "price": "0.02 BNB"}
    job._apply_page_card(project, {
        "price": "9 BNB", "deadline": "2026-11-10", "mint_unit": "NFT",
        "mint_kind": "mint", "mint_done": 1, "mint_total": 2,
        "token_total": 3, "contract": "0x" + "2" * 40,
    })
    assert project["mint_done"] == 12
    assert project["mint_total"] == 100
    assert project["token_total"] == 999
    assert project["contract"] == "0x" + "1" * 40
    assert project["price"] == "0.02 BNB"
    assert project["deadline"] == "2026-11-10"
    assert project["mint_unit"] == "NFT"


def test_post_payload_is_trimmed_and_deduplicated():
    from app.monitors.core import job

    rows = [
        {"tweet_id": "1", "author": "a", "created_at": "t1",
         "text": "same " + "x" * 2000},
        {"tweet_id": "2", "author": "a", "created_at": "t2",
         "text": "same " + "x" * 2000},
        {"tweet_id": "3", "author": "b", "created_at": "t3", "text": "other"},
    ]
    payload = job._post_payload(rows)
    assert len(payload) == 2
    assert all(len(row["text"]) <= job.POST_CHARS for row in payload)
    assert job.POST_CHARS <= 800
    assert job.MAX_BATCHES <= 4


def test_mintable_is_a_risk_only_when_someone_owns_the_contract():
    """公开铸造不是后门：合约没有 owner 时，把 is_mintable 标红会把每个铸造中的项目都涂红。"""
    from app.monitors.core import job

    public = {"perms": ["mintable"]}
    job._mint_capability(public)
    assert public["perms"] == []
    assert public["mint_public"] is True

    owned = {"perms": ["mintable"], "owner": "0x" + "9" * 40}
    job._mint_capability(owned)
    assert owned["perms"] == ["mintable"], "有 owner 的增发才留在风险里"
    assert "mint_public" not in owned


def test_the_owner_is_read_on_chain_and_zero_means_renounced(monkeypatch):
    from app.monitors.core import chain

    ca = "0x" + "a" * 40
    monkeypatch.setattr(chain.goplus.GoPlus, "fetch_token", lambda self, c, a: (None, None))
    monkeypatch.setattr(chain.dexscreener.DexScreener, "fetch_token",
                        lambda self, c, a: (None, None))
    monkeypatch.setattr(chain, "_uint", lambda address, data, **kw: None)
    monkeypatch.setattr(chain, "token_facts", lambda address, **kw: {})

    monkeypatch.setattr(chain, "_call", lambda address, data, **kw:
                        ("0x" + "0" * 64) if data == chain.OWNER else None)
    assert "owner" not in chain.probe(ca), "0x0 是放弃所有权，不是 owner"

    monkeypatch.setattr(chain, "_call", lambda address, data, **kw:
                        ("0x" + "0" * 24 + "9" * 40) if data == chain.OWNER else None)
    assert chain.probe(ca)["owner"] == "0x" + "9" * 40


def test_the_pair_answers_when_goplus_has_no_lp_table(monkeypatch):
    """GoPlus 没有 LP 表时问池子自己：空池、烧掉多少。核对不到就什么都不说。"""
    from app.monitors.core import chain

    pair = "0x" + "b" * 40
    monkeypatch.setattr(chain, "_call", lambda address, data, **kw: None)
    monkeypatch.setattr(chain, "total_supply", lambda address, **kw: 0)
    assert chain.lp_from_pair(pair) == {"lp_state": "empty"}, "没有 LP 总量就是还没组池"

    burned = chain.BALANCE_OF + chain.BURNED[1][2:].rjust(64, "0")
    monkeypatch.setattr(chain, "total_supply", lambda address, **kw: 1000)
    monkeypatch.setattr(chain, "_uint", lambda address, data, **kw:
                        1000 if data == burned else 0)
    assert chain.lp_from_pair(pair) == {"lp_state": "burnt", "lp_locked_pct": 100.0}

    # 核对不到锁仓份额时不能反过来说「未锁」：那是没看见，不是看见了没有。
    monkeypatch.setattr(chain, "_uint", lambda address, data, **kw: 0)
    assert chain.lp_from_pair(pair) == {}


def test_the_card_type_says_what_the_buyer_gets():
    """类型按资产形态分：铭文 / 平台 / 任务的说法优先于链上，链上再分份额和 NFT。"""
    from app.monitors.core import job

    # 链上是 NFT 的合约，页面写着 runes，仍然是铭文。
    assert job._type({"nft": True}, {"title": "Bolt Runes", "summary": "8192 runestone"}) \
        == "inscription"
    assert job._type({}, {"title": "SoulBoard", "summary": "Robinhood 上的 launchpad"}) \
        == "launchpad"
    # 中文同义写法（发射平台 / 发币平台）也要认。
    assert job._type({}, {"title": "SoulBoard", "summary": "链上的发射平台"}) == "launchpad"
    assert job._type({}, {"title": "KOAI", "summary": "面向 Agent 的发币平台"}) == "launchpad"
    # 官网首屏写着 Launchpad，即使抽取摘要写成任务平台也按发射台（ChyAI / KOAI）。
    assert job._type({}, {"title": "ChyAI", "summary": "做任务换凭证", "mint_kind": "task",
                          "pages": ["<title>ChyAI · AI Agent Launchpad</title>"]}) == "launchpad"
    # 官网没有 launchpad、但有验证过的代币：BIBIAI 是份额，不是发射台。
    assert job._type({"token": "0x" + "6" * 40},
                     {"title": "BIBIAI", "summary": "铸造通行证", "mint_unit": "份",
                      "pages": ["BIBIAI Agent ID 铸造"]}) == "share"
    # 单位是「份」时压过摘要里的「发射平台」：BIBIAI 按份卖，不是发射台。
    assert job._type({"token": "0x" + "7" * 40},
                     {"title": "BIBIAI", "summary": "发射平台", "mint_unit": "份"}) == "share"
    # 摘要写明 NFT 时压过附带奖励币：Moribundus 是 NFT，SOUL 只是铸造后发放。
    assert job._type({"token": "0x" + "8" * 40},
                     {"title": "Moribundus", "summary": "Moribundus NFT 最后 444 枚",
                      "mint_total": "4444"}) == "nft"
    # 任务平台也发凭证 NFT，但 mint_kind 写着 task。
    assert job._type({"nft": True}, {"title": "ChyAI", "mint_kind": "task"}) == "task"
    # 每天「打卡」mint 的是 NFT，别因为一个动作词把收藏品判成任务平台。
    assert job._type({"nft": True},
                     {"title": "Ptah", "summary": "每天打卡 mint"}) == "nft"
    # 验证过的 ERC-20 比「页面出现过 NFT」更可信。
    assert job._type({"token": "0x" + "1" * 40},
                     {"title": "JAGENT", "summary": "持有 NFT 空投"}) == "share"
    assert job._type({}, {"title": "Tiny", "summary": "Farmer NFT mint"}) == "nft"
    assert job._type({}, {"title": "AIMONSTERS Genesis Egg", "summary": "mint"}) == "unknown"
    # 模型给了类型就采信（链上验不到的时候）；最后还有一条按份铸造的兜底。
    assert job._type({}, {"title": "X", "summary": "mint", "asset_type": "nft"}) == "nft"
    assert job._type({}, {"title": "X", "summary": "mint", "mint_total": "9,999"}) == "nft"
    assert job._type({}, {"title": "X", "summary": "mint", "mint_total": "4440000",
                          "mint_unit": "Shares"}) == "share"
    # 附近有 ERC-20 不等于卖份额：单位是 farmers / cards 的仍然是 NFT。
    assert job._type({"token": "0x" + "2" * 40},
                     {"title": "Tiny Farm", "summary": "Farmer NFT",
                      "mint_unit": "farmers"}) == "nft"
    assert job._type({"token": "0x" + "3" * 40},
                     {"title": "Phosphor Slot", "summary": "每个 handle 可铸造五张卡",
                      "mint_unit": "cards"}) == "nft"
    assert job._type({"token": "0x" + "4" * 40},
                     {"title": "JAGENT", "mint_unit": "Shares"}) == "share"
    # 页面全程只说 profile card、不提 nft 的，也算 NFT（agentsea）。
    assert job._type({}, {"title": "AgentSea",
                          "summary": "预注册 profile card，mint 要 Agent ID"}) == "nft"
    # 官网正文里没有 nft 字样时也能从页面认出来；但页脚里的 launchpad / runes 不能把卡带偏。
    assert job._type({}, {"title": "AgentSea", "summary": "预注册",
                          "pages": ["pre-registers your profile card"]}) == "nft"
    assert job._type({}, {"title": "HODLER", "summary": "像素头像 NFT",
                          "pages": ["we also run a launchpad for runes"]}) == "nft"
    # KOL 把代币份额叫「符文」不能当证据：单位是 shares、官网写 shares，就是份额。
    assert job._type({"token": "0x" + "5" * 40},
                     {"title": "BNBS", "summary": "第一个币安 Agent ID 的 Mint 符文项目",
                      "mint_unit": "shares"}) == "share"
    # 真正的铭文看官网原文，不看摘要。
    assert job._type({}, {"title": "Bolt Runes", "summary": "8192 个 runestone",
                          "pages": ["<h1>Bolt Runes</h1> etch on chain"]}) == "inscription"


def test_a_utc_time_is_written_once_in_local_time():
    """页面写 15:00 UTC，卡上就该是 23:00；开卖时间也不能被当成截止（SWARM, 2026-10-30）。"""
    from app.monitors.core import job

    assert job._when("2026-10-29 15:00 UTC") == "2026-10-29 23:00"
    assert job._when("2026-10-29T15:00Z") == "2026-10-29 23:00"
    assert job._when("30 October, 12:00 UTC") == f"{job._now().year}-10-30 20:00"
    assert job._when("2026-11-09") == "2026-11-09", "没有时区的日期原样保留"
    assert job._when("2026-10-29 15:00 UTC+8") == "2026-10-29 15:00", "已经本地口径不再加 8"
    assert job._when("") == ""
    assert job._when("未判定") == ""


def test_a_chain_named_in_the_prose_is_not_left_unjudged():
    from app.monitors.core import chain

    assert chain.guess_chain("Robinhood Chain 上的 launchpad") == "Robinhood"
    assert chain.guess_chain("BNB Chain 上 8192 个 runestone") == "BSC"
    assert chain.guess_chain("a database of nothing") == "", "Base 不能匹配 database"


def test_the_clock_respects_the_project_switch_and_the_schedule_switch(
        isolated_db, monkeypatch, tmp_path):
    """两个开关各管一件事：项目关掉 → 整个监控不跑；计划任务关掉 → 只停时钟。"""
    from app.monitors import registry
    from app.monitors.core import schedule, settings as monitor_settings

    monkeypatch.setattr(monitor_settings, "ROOT", tmp_path)
    monitor = registry.MONITORS["binance-agent-id"]
    at = datetime(2026, 10, 29, 10, 0, tzinfo=schedule.TZ)

    monitor_settings.write(monitor, {"schedule": False})
    assert schedule.due(monitor.id, at) is None, "计划任务关掉就不按点跑"

    monitor_settings.write(monitor, {"schedule": True, "enabled": False})
    assert schedule.due(monitor.id, at) is None, "项目关掉，什么都不跑"

    monitor_settings.write(monitor, {"schedule": True, "enabled": True})
    assert schedule.due(monitor.id, at) is not None


def test_settings_are_always_written_in_full(client, monkeypatch, tmp_path):
    """文件常驻：六项都写着，改回默认也不消失（2026-10-30, user）。"""
    from app.monitors.core import settings as monitor_settings

    monkeypatch.setattr(monitor_settings, "ROOT", tmp_path)
    body = client.put("/api/v1/monitors/binance-agent-id/settings",
                      json={"interval_hours": 6}).json()
    assert body["values"]["interval_hours"] == 6
    path = tmp_path / "binance-agent-id" / "settings.yaml"
    assert path.exists()

    body = client.put("/api/v1/monitors/binance-agent-id/settings",
                      json={"interval_hours": 8}).json()
    assert body["values"]["interval_hours"] == 8
    text = path.read_text(encoding="utf-8")
    assert "interval_hours: 8" in text, "回到默认也照样写回文件"
    for key in ("enabled", "schedule", "start_hour", "start_minute",
                "window_hours", "slice_hours", "min_score"):
        assert key + ":" in text


def test_the_three_collection_knobs_live_in_the_yaml(client, monkeypatch, tmp_path):
    """窗口 / 切片 / 闸分住在 monitors/<id>/settings.yaml，设置页只是把它们显示出来。"""
    from app.monitors.core import settings as monitor_settings

    monkeypatch.setattr(monitor_settings, "ROOT", tmp_path)
    body = client.put("/api/v1/monitors/binance-agent-id/settings",
                      json={"window_hours": 48, "slice_hours": 6, "min_score": 45}).json()
    assert body["values"]["window_hours"] == 48
    path = tmp_path / "binance-agent-id" / "settings.yaml"
    text = path.read_text(encoding="utf-8")
    assert "window_hours: 48" in text and "min_score: 45" in text

    assert "queries:" in text, "查询和别的设定在同一个文件里，保存时原样带上"


def test_the_queries_live_in_the_same_settings_file(monkeypatch, tmp_path):
    """查询和其它设定是同一条记录，手写的那条不会因为一次保存而丢。"""
    from app.monitors.core import settings as monitor_settings
    from app.monitors.registry import BINANCE_AGENT_ID

    monkeypatch.setattr(monitor_settings, "ROOT", tmp_path)
    seed = tmp_path / "binance-agent-id" / "settings.yaml"
    seed.parent.mkdir(parents=True)
    seed.write_text("queries:\n  - 'custom query'\n", encoding="utf-8")
    assert monitor_settings.values(BINANCE_AGENT_ID)["queries"] == ("custom query",)

    monitor_settings.write(BINANCE_AGENT_ID, {"interval_hours": 6})
    text = seed.read_text(encoding="utf-8")
    assert "custom query" in text, "保存别的键不能覆盖查询"

    seed.write_text("queries: []\n", encoding="utf-8")
    assert monitor_settings.values(BINANCE_AGENT_ID)["queries"] == BINANCE_AGENT_ID.queries


def test_posts_are_read_in_batches_and_merged_to_the_round_cap(monkeypatch):
    """一轮最多 4 批 × max_posts 条，合并后最多 20 个项目；同一项目两批里出现只留一条。"""
    from app.monitors.core import job
    from app.monitors.registry import BINANCE_AGENT_ID as monitor

    calls = []

    def fake(messages, **kw):
        calls.append(json.loads(messages[1]["content"]))
        index = len(calls)
        rows = [{"title": f"P{index}-{n}", "handle": f"p{index}-{n}"} for n in range(10)]
        rows.append({"title": "Dup", "handle": "dup",
                     "summary": "short" if index == 1 else "a much longer summary"})
        return {"projects": rows}

    monkeypatch.setattr(job.llm, "complete_json", fake)
    posts = [{"author": "@a", "text": "x"}
             for _ in range(monitor.max_posts * (job.MAX_BATCHES + 1))]
    projects, batches, failed, successful = job._extract(monitor, posts)

    assert batches == job.MAX_BATCHES and len(calls) == job.MAX_BATCHES, "超出批数的帖子不读，省成本"
    assert failed == 0
    assert successful == list(range(job.MAX_BATCHES))
    assert all(len(chunk) <= monitor.max_posts for chunk in calls)
    assert len(projects) == job.ROUND_PROJECTS, "宁缺毋滥：整轮最多 20 张新卡"
    dup = [row for row in projects if row["handle"] == "dup"]
    assert len(dup) == 1 and dup[0]["summary"] == "a much longer summary"


def test_a_failed_monitor_batch_does_not_reconsume_successful_batches(
        isolated_db, monkeypatch):
    """One unreadable batch must not put every successful batch back in the queue."""
    from dataclasses import replace

    from app.monitors.core import job
    from app.monitors.registry import BINANCE_AGENT_ID
    from app.store import db

    monitor = replace(BINANCE_AGENT_ID, max_posts=2)
    pending = [
        {"tweet_id": str(index), "author": "a", "created_at": "2026-10-30T10:00:00",
         "text": "bad" if index == 2 else "ok", "score": 40,
         "first_seen": "2026-10-30T10:00:00"}
        for index in range(4)
    ]
    db.monitor_post_add("m1", pending)

    def fake(messages, **kwargs):
        chunk = json.loads(messages[1]["content"])
        if any(row["text"] == "bad" for row in chunk):
            raise ValueError("unreadable batch")
        return {"projects": [{"title": "Read %s" % chunk[0]["text"], "handle": "read"}]}

    monkeypatch.setattr(job.llm, "complete_json", fake)
    payload = [{"author": row["author"], "text": row["text"]} for row in pending]
    projects, batches, failed, successful = job._extract(monitor, payload)

    assert batches == 2 and failed == 1 and successful == [0]
    marked = job._retire_batches("m1", pending, successful, monitor.max_posts)
    assert marked == 2
    assert sorted(row["tweet_id"] for row in db.monitor_posts_pending("m1")) == ["2", "3"]


def test_the_site_title_must_share_a_word_with_the_model_title():
    from dataclasses import replace

    from app.monitors.core import job
    from app.monitors.registry import BINANCE_AGENT_ID

    monitor = replace(BINANCE_AGENT_ID)
    project = {
        "title": "Current Project",
        "handle": "current",
        "pages": ["<html><title>Unrelated Marketing Page</title></html>"],
    }
    assert job._card(monitor, project, "scan")["title"] == "Current Project"

    project["pages"] = ["<html><title>Current Project Mint</title></html>"]
    assert job._card(monitor, project, "scan")["title"] == "Current Project Mint"


def test_posts_are_queued_once_and_retired_after_use(isolated_db):
    """贴子入队按 tweet_id 去重；读过的置 used，下一轮不会再占名额（2026-10-30, user）。"""
    from app.store import db

    post = {"tweet_id": "1", "author": "acme", "created_at": "2026-10-30T10:00:00",
            "text": "mint for agent id", "score": 40, "first_seen": "2026-10-30T10:00:00"}
    assert db.monitor_post_add("m1", [post]) == 1
    assert db.monitor_post_add("m1", [post]) == 0, "同一帖子只入队一次"
    assert [row["tweet_id"] for row in db.monitor_posts_pending("m1")] == ["1"]
    db.monitor_posts_mark("m1", ["1"])
    assert db.monitor_posts_pending("m1") == []


def test_a_lead_without_a_search_does_not_claim_it_searched():
    """没接二次搜索时 `searched` 必须是 false：诊断不能把「没查」写成「查过」."""
    from app.opportunity import trace

    answer = trace.trace_lead({"text": "mint for agent id", "name": "Niche Society",
                               "handle": ""}, known=set())
    assert answer["level"] == "C"
    assert answer["searched"] is False


def test_a_restart_retires_scans_left_running(isolated_db):
    """进程重启后没有线程在跑的扫描必须标成失败，界面不能一直显示「正在扫描」。"""
    from app.store import db

    db.monitor_scan_put({"id": "ms1", "monitor": "m1", "status": "running", "stages": [],
                         "counters": {}, "started_at": db.now_iso()})
    db.opportunity_scan_put({"id": "os1", "status": "running", "started_at": db.now_iso()})
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO search_jobs (id, ca, status, stages_json, started_at)"
            " VALUES ('js1', '0xabc', 'running', '[]', ?)",
            (db.now_iso(),),
        )
    finally:
        conn.close()
    assert db.reconcile_running_scans() == 3
    assert db.monitor_scan_get("ms1")["status"] == "failed"
    assert db.opportunity_scan_get("os1")["status"] == "failed"
    assert db.job_get("js1")["status"] == "failed"
    assert db.job_get("js1")["finished_at"]


def test_the_post_queue_keeps_only_thirty_days(isolated_db):
    from app.store import db

    old = {"tweet_id": "old", "author": "a", "created_at": "", "text": "x", "score": 30,
           "first_seen": "2026-09-01T00:00:00+08:00"}
    new = {"tweet_id": "new", "author": "a", "created_at": "", "text": "y", "score": 30,
           "first_seen": "2026-10-30T00:00:00+08:00"}
    db.monitor_post_add("m1", [old, new])
    assert db.monitor_posts_prune("m1", "2026-10-01T00:00:00+08:00") == 1
    assert [row["tweet_id"] for row in db.monitor_posts_pending("m1")] == ["new"]


def test_a_card_needs_an_official_site():
    """只有社区号、没有官网的项目不建卡（Polka Agents，2026-10-30, user）。"""
    from app.monitors.core import job

    assert job._has_site({"official_url": "https://example.com/"})
    assert not job._has_site({})
    assert not job._has_site({"official_url": "  "})


def test_a_card_needs_a_crypto_anchor():
    """官网正文没有加密内容、也拿不出链上证据的卡片不建（prismshrimp, 2026-10-30）."""
    from app.monitors.core import job

    assert job._credible({"pages": ["<p>Mint an NFT with your Agent ID</p>"]}, {})
    assert job._credible({"pages": ["Ask me things"]},
                         {"token": "0x" + "9" * 40, "agent_gated": True})
    assert job._credible({"pages": ["Ask me things"], "mint_total": "1000"},
                         {"agent_gated": True})
    assert not job._credible({"pages": ["<p>Ask me things anonymously</p>"]}, {})


def test_a_plain_http_site_is_upgraded_when_https_answers():
    """官网优先 https：trace 给 http://danta.ai/，卡上应该给 https（2026-10-30, user）."""
    from app.monitors.core import job

    assert job._secure("http://danta.ai/", probe=lambda url: True) == "https://danta.ai/"
    assert job._secure("http://danta.ai/", probe=lambda url: False) == "http://danta.ai/"
    assert job._secure("https://example.com/", probe=lambda url: False) == "https://example.com/"


def test_the_collection_knobs_default_to_the_registry(client):
    values = client.get("/api/v1/monitors/binance-agent-id/settings").json()["values"]
    assert (values["window_hours"], values["slice_hours"], values["min_score"]) == (24, 4, 35)


def test_the_refresh_fingerprint_ignores_clocks_and_unrelated_page_copy():
    from app.monitors.core import job

    one = job._field_fingerprint("Minted 6 / 10,000 at 21:00:01 Price 0.02 BNB")
    same = job._field_fingerprint("Minted 6 / 10,000 at 21:00:02 Price 0.02 BNB")
    moved = job._field_fingerprint("Minted 7 / 10,000 at 21:00:02 Price 0.02 BNB")
    noisy = job._field_fingerprint(
        "Minted 6 / 10,000 at 21:00:02 Price 0.02 BNB Latest partner: random"
    )
    assert one == same, "时钟抖动不算页面变化"
    assert one != moved, "计数变化必须算"
    assert one == noisy, "无关页脚变化不买模型调用"


def test_the_refresh_stage_skips_an_unchanged_page(monkeypatch, isolated_db):
    """页面没变时只记时间，不调模型（2026-10-30, user）."""
    from app.monitors.core import job, chain
    from app.monitors.registry import BINANCE_AGENT_ID
    from app.sources import browser

    page = ("<html><title>Acme</title><body>"
            + "Acme agent mint. " * 20
            + "Minted 6 / 10,000 at 21:00:01</body></html>")
    monkeypatch.setattr(browser, "render", lambda url: page)

    def boom(*args, **kwargs):
        raise AssertionError("an unchanged page must not call the model")

    monkeypatch.setattr(job.llm, "complete_json", boom)
    card = {"slug": "acme", "official_url": "https://acme/",
            "fields": {"site_hash": job._field_fingerprint(chain.plain(page))}}
    assert job._refresh_card(BINANCE_AGENT_ID, card) == "unchanged"


def test_a_site_number_below_the_chain_reading_is_ignored(monkeypatch, isolated_db):
    """进度只往前：站点给 4,474 / 10,000 时不能覆盖链上读到的 8,867."""
    from app.monitors.core import job
    from app.monitors.registry import BINANCE_AGENT_ID
    from app.sources import browser

    page = "<html><body>" + "mint " * 60 + "Minted 4474 / 10,000</body></html>"
    monkeypatch.setattr(browser, "render", lambda url: page)
    monkeypatch.setattr(job, "_probe_card", lambda card: {
        "minted": 8867, "mint_total": 10000, "contract": "0x" + "1" * 40})
    captured = {}
    monkeypatch.setattr(job.db, "monitor_card_patch",
                        lambda *args, **kwargs: captured.update({"fields": args[2]}))
    card = {"slug": "x", "official_url": "https://x/",
            "fields": {"mint": {"done": 8867, "total": 10000, "source": "chain"}}}
    assert job._refresh_card(BINANCE_AGENT_ID, card) == "changed"
    assert captured["fields"]["mint"]["done"] == 8867
    assert captured["fields"]["mint"]["source"] == "chain"


def test_refresh_uses_chain_and_page_rules_without_calling_llm(
        monkeypatch, isolated_db):
    from app.monitors.core import job
    from app.monitors.registry import BINANCE_AGENT_ID
    from app.sources import browser

    ca = "0x" + "2" * 40
    page = ("<html><body>" + "agent mint " * 40 +
            "Minted 12 / 100. Price 0.02 BNB. Ends 2026-11-10 18:00 UTC</body></html>")
    monkeypatch.setattr(browser, "render", lambda url: page)
    monkeypatch.setattr(job, "_dead_contract", lambda fields, card: False)
    monkeypatch.setattr(job, "_probe_card", lambda card: {
        "contract": ca, "minted": 12, "mint_total": 100})

    def boom(*args, **kwargs):
        raise AssertionError("refresh must not call the model")

    monkeypatch.setattr(job.llm, "complete_json", boom)
    captured = {}
    monkeypatch.setattr(
        job.db, "monitor_card_patch",
        lambda *args, **kwargs: captured.update({"fields": args[2], "price": kwargs.get("price"),
                                                 "deadline": kwargs.get("deadline")}),
    )
    card = {"slug": "acme", "official_url": "https://acme/",
            "fields": {"onchain": {"contract": ca}, "site_hash": "old",
                       "mint": {"done": 10, "total": 100, "source": "chain"}}}
    assert job._refresh_card(BINANCE_AGENT_ID, card) == "changed"
    assert captured["fields"]["mint"]["done"] == 12
    assert captured["fields"]["price"] == "0.02 BNB"
    assert captured["deadline"].startswith("2026-11-11")


def test_a_profile_site_is_not_downgraded_when_the_profile_cannot_be_read():
    """资料页认领过的站，不因为某一轮读不到资料页就退回帖子里的链接（danta pixel）."""
    from app.monitors.core import job
    from app.monitors.registry import BINANCE_AGENT_ID

    before = {"dantapixelnft": {
        "slug": "dantapixelnft", "official_url": "https://dantapixel.deed.page/",
        "sources": [], "fields": {"site_source": "profile"}}}
    project = {"title": "danta.ai", "handle": "dantapixelnft",
               "official_account": "dantapixelnft", "summary": "danta pixel",
               "source": "https://x.com/a/status/1",
               "official_url": "https://danta.ai/",
               "sources": [{"kind": "official_site", "label": "官网",
                            "url": "https://danta.ai/"}]}
    card = job._card(BINANCE_AGENT_ID, project, "scan", before)
    assert card["official_url"] == "https://dantapixel.deed.page/"
    assert card["fields"]["site_source"] == "profile"

def test_agent_gate_reads_the_registry_out_of_bytecode(monkeypatch):
    from app.monitors.core import chain

    ca = "0x" + "a" * 40
    gated = "0x" + "00" * 40 + chain.AGENT_ID_REGISTRY[2:] + "00" * 20
    monkeypatch.setattr(chain, "_code", lambda address, **kw: gated)
    assert chain.agent_gate(ca) is True

    monkeypatch.setattr(chain, "_code", lambda address, **kw: "0x" + "00" * 200)
    assert chain.agent_gate(ca) is False

    monkeypatch.setattr(chain, "_code", lambda address, **kw: "0x")
    assert chain.agent_gate(ca) is None


def test_a_proxy_is_followed_to_its_implementation(monkeypatch):
    from app.monitors.core import chain

    proxy = "0x" + "a" * 40
    impl = "0x" + "b" * 40

    def rpc(method, params, **kw):
        if method == "eth_getStorageAt":
            return "0x" + "0" * 24 + impl[2:]
        if method == "eth_getCode":
            return "0x60806040"
        return None

    monkeypatch.setattr(chain, "_rpc", rpc)
    assert chain.implementation(proxy) == impl


def test_sale_facts_read_common_price_and_deadline_getters(monkeypatch):
    from app.monitors.core import chain

    ca = "0x" + "a" * 40
    deadline = datetime(2026, 11, 10, 18, tzinfo=timezone.utc).timestamp()
    values = {
        chain.SALE_PRICE_GETTERS[0][0]: 20_000_000_000_000_000,
        chain.SALE_END_GETTERS[0][0]: int(deadline),
    }
    monkeypatch.setattr(
        chain, "_uint",
        lambda address, selector, timeout=8.0: values.get(selector),
    )
    facts = chain.sale_facts(ca, chain="bsc")
    assert facts["mint_price"] == "0.02 BNB"
    assert facts["sale_deadline"].startswith("2026-11-10T18:00:00")


def test_sale_facts_discard_implausible_price_and_timestamp(monkeypatch):
    from app.monitors.core import chain

    values = {
        chain.SALE_PRICE_GETTERS[0][0]: 1,
        chain.SALE_END_GETTERS[0][0]: 1,
    }
    monkeypatch.setattr(
        chain, "_uint",
        lambda address, selector, timeout=8.0: values.get(selector),
    )
    assert chain.sale_facts("0x" + "a" * 40, chain="bsc") == {}


def test_chain_sale_facts_land_on_the_card_fields():
    from app.monitors.core import job

    fields = {"onchain": {"contract": "0x" + "a" * 40}}
    assert job._merge_chain(fields, {
        "minted": 12, "mint_total": 100,
        "mint_price": "0.02 BNB",
        "sale_deadline": "2026-11-10T18:00:00+00:00",
    })
    assert fields["price"] == "0.02 BNB"
    assert fields["deadline"].startswith("2026-11-11 02:00")
    assert fields["mint"]["done"] == 12


def test_llm_usage_totals_one_scan(isolated_db):
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO llm_calls"
            " (run_id, purpose, prompt_tokens, completion_tokens, ok, created_at)"
            " VALUES"
            " ('scan-1', 'monitor_extract', 100, 20, 1, '2026-11-04T10:00:00+08:00'),"
            " ('scan-1', 'monitor_page', 30, 10, 0, '2026-11-04T10:01:00+08:00'),"
            " ('scan-2', 'monitor_page', 999, 999, 1, '2026-11-04T10:02:00+08:00')"
        )
    finally:
        conn.close()
    usage = db.llm_usage("scan-1")
    assert usage["calls"] == 2
    assert usage["prompt_tokens"] == 130
    assert usage["completion_tokens"] == 30
    assert usage["total_tokens"] == 160
    assert usage["failed"] == 1
    assert usage["by_purpose"]["monitor_extract"]["total_tokens"] == 120
    assert usage["by_purpose"]["monitor_page"]["failed"] == 1


def test_scan_budget_marks_an_over_budget_sweep(monkeypatch):
    from app.monitors.core import job

    monkeypatch.setattr(job, "MONITOR_TOKEN_BUDGET", 100)
    monkeypatch.setattr(job.db, "llm_usage", lambda scan_id: {
        "calls": 2, "prompt_tokens": 80, "completion_tokens": 30, "total_tokens": 110,
        "failed": 0,
    })
    counters = {}
    usage = job._record_llm_usage(counters, "scan-1")
    assert usage["total_tokens"] == 110
    assert counters["llm"] == usage
    assert counters["llm_over_budget"] is True


def test_mint_logs_count_only_zero_sender_transfers(monkeypatch):
    from app.monitors.core import chain

    ca = "0x" + "c" * 40
    wallet = "0x" + "d" * 40
    latest = 1_000_000
    transfer = lambda sender, receiver, block: {
        "topics": [chain.TRANSFER, "0x" + sender.rjust(64, "0"),
                   "0x" + receiver.rjust(64, "0")],
        "blockNumber": hex(block), "transactionHash": "0x" + "e" * 64}

    def rpc(method, params, **kw):
        if method == "eth_blockNumber":
            return hex(latest)
        if method == "eth_getLogs":
            start = int(params[0]["fromBlock"], 16)
            if start > latest - 50_000:
                return [transfer("0" * 40, wallet[2:], latest - 10),
                        transfer("0" * 40, "f" * 40, latest - 20),
                        transfer("1" * 40, wallet[2:], latest - 30)]
            return []
        return None

    monkeypatch.setattr(chain, "_rpc", rpc)
    logs = chain.mint_logs(ca, deep=True)
    assert logs["mint_logs"] == 2, "a normal transfer is not a mint"
    assert logs["mint_wallets"] == 2
    assert logs["mint_30m"] == 2


def test_the_same_project_is_absorbed_not_duplicated():
    from app.monitors.core import job

    head = {"slug": "head", "title": "$JAGENT Mint", "first_seen": "2026-10-30T00:00:00+08:00",
            "official_url": "https://jager.live/",
            "fields": {"onchain": {"code_fp": "100:abc", "contract": "0xabc",
                                   "token": "0xtoken"}}}
    before = {"head": head}
    # Same token.
    assert job._same_project({"code_fp": "100:abc", "token": "0xtoken"},
                             "new", "Different", before)["slug"] == "head"
    # Same contract address (case-insensitive).
    assert job._same_project({"code_fp": "100:abc", "contract": "0xABC"},
                             "new", "Different", before)["slug"] == "head"
    # The same contract is enough even when the probe could not fingerprint it.
    assert job._same_project({"contract": "0xABC"},
                             "new", "Different", before)["slug"] == "head"
    # Same name after normalisation.
    assert job._same_project({"code_fp": "100:abc"}, "new", "$JAGENT  MINT",
                             before)["slug"] == "head"
    # Same template, different project: keep both, never absorb.
    assert job._same_project({"code_fp": "100:abc", "contract": "0xother",
                              "token": "0xother"}, "new", "Other", before) == {}
    # One launchpad hosts many projects: a shared site is not one project.
    launch = {"slug": "launch", "title": "Basilurk",
              "first_seen": "2026-10-30T00:00:00+08:00",
              "official_url": "https://xiaomei.bid/",
              "fields": {"onchain": {"code_fp": "200:def", "contract": "0x1"}}}
    assert job._same_project({"code_fp": "200:def", "contract": "0x2"},
                             "new", "Aquarune", {"launch": launch}) == {}


def test_chain_progress_never_moves_backwards():
    from app.monitors.core import job

    fields = {"mint": {"done": 10, "total": 20, "source": "chain", "state": "live"}}
    assert job._merge_chain(fields, {"minted": 8}) is False
    assert fields["mint"]["done"] == 10
    assert job._merge_chain(fields, {"minted": 12}) is True
    assert fields["mint"]["done"] == 12


def test_agent_id_support_counts_without_a_gate():
    from app.monitors.core import job

    project = {
        "title": "MultiScreenAI",
        "summary": "A multi-model agent tool.",
        "facts": [],
        "pages": ["Binance Agent ID support for AI agents on BNB Chain."],
    }
    info = {"contract": "0x" + "a" * 40, "agent_gated": False, "agent_callers": 0.0}
    assert job._credible(project, info) is True


def test_a_crypto_project_without_an_agent_id_relation_is_not_credible():
    from app.monitors.core import job

    project = {"title": "Acme", "summary": "mint a thing", "facts": [],
               "pages": ["Mint an NFT on BNB Chain."]}
    info = {"contract": "0x" + "a" * 40, "agent_gated": False, "agent_callers": 0.0}
    assert job._credible(project, info) is False


def test_a_compiler_metadata_tail_does_not_make_a_new_template():
    from app.monitors.core import chain

    body = "6080604052" + "00" * 100 + "a264697066735822" + "11" * 40
    same = "6080604052" + "00" * 100 + "a264697066735822" + "22" * 40
    assert chain.fingerprint_code("0x" + body) == chain.fingerprint_code("0x" + same)
    assert chain.fingerprint_code("0x" + body) != chain.fingerprint_code("0x" + "00" * 200)


def test_the_registry_and_the_zero_address_are_never_a_project_contract():
    from app.monitors.core import chain

    registry = chain.AGENT_ID_REGISTRY
    zero = "0x" + "0" * 40
    assert chain.first_address("registry " + registry, "zero " + zero) == ""
    real = "0x" + "b" * 40
    assert chain.first_address(registry + " then " + real) == real


def test_a_card_pointing_at_the_registry_is_not_probed(monkeypatch):
    from app.monitors.core import job

    def boom(*args, **kwargs):
        raise AssertionError("an infrastructure address must not be probed")

    monkeypatch.setattr(job.chain, "probe", boom)
    card = {"fields": {"onchain": {"contract": job.chain.AGENT_ID_REGISTRY}}}
    assert job._probe_card(card) == {}


def test_absorb_keeps_the_identity_and_moves_progress_forward():
    from app.monitors.core import job

    head = {"slug": "head", "title": "$JAGENT Mint",
            "first_seen": "2026-10-30T00:00:00+08:00",
            "updated_at": "2026-10-30T00:00:00+08:00",
            "price": "2 U", "deadline": None, "summary": "short",
            "sources": [{"kind": "official_site", "url": "https://jager.live/"}],
            "trust": "community", "trust_basis": "x",
            "fields": {"onchain": {"contract": "0xabc", "code_fp": "100:abc",
                                   "token": "0xtoken"},
                       "mint": {"done": 10, "total": 100, "source": "chain"}}}
    card = {"slug": "new", "title": "Other",
            "first_seen": "2026-10-31T00:00:00+08:00",
            "updated_at": "2026-10-31T00:00:00+08:00",
            "price": "1 U", "deadline": "2026-11-01", "summary": "a much longer summary",
            "source_url": "https://x.com/new/status/1",
            "trust": "official", "trust_basis": "site",
            "sources": [{"kind": "official_account", "url": "https://x.com/jager"}],
            "fields": {"onchain": {"contract": "0xabc", "code_fp": "100:abc",
                                   "token": "0xtoken", "minted": 15},
                       "mint": {"done": 15, "total": 100, "source": "chain"}}}
    out = job._absorb(head, card)
    assert out["slug"] == "head"
    assert out["title"] == "$JAGENT Mint"
    assert out["first_seen"] == head["first_seen"]
    assert out["fields"]["mint"]["done"] == 15
    assert out["price"] == "1 U"
    assert out["deadline"] == "2026-11-01"
    assert out["summary"] == "a much longer summary"
    assert out["trust"] == "official"
    assert {row["url"] for row in out["sources"]} == {
        "https://jager.live/", "https://x.com/jager", "https://x.com/new/status/1"}


def test_each_chain_reads_its_own_node(monkeypatch):
    from app import config
    from app.monitors.core import chain

    monkeypatch.setattr(config, "load_chains",
                        lambda: {"robinhood": {"rpc": "https://rh.example"}})
    assert chain.chain_id("Robinhood") == "robinhood"
    assert chain.chain_id("未判定") == "bsc"
    assert chain.chain_id("Base") == ""
    assert chain._nodes("robinhood") == ("https://rh.example",)
    assert chain._nodes("base") == ()
    assert chain._nodes("bsc") == chain.BSC_RPCS


def test_a_confirmed_empty_address_is_not_a_contract(monkeypatch):
    from app.monitors.core import chain

    monkeypatch.setattr(chain, "_code", lambda ca, **kw: "0x")
    assert chain.probe("0x" + "a" * 40) == {}


def test_probe_first_never_takes_an_address_without_code(monkeypatch):
    from app.monitors.core import job

    first, second = "0x" + "1" * 40, "0x" + "2" * 40
    monkeypatch.setattr(job.chain, "mint_only", lambda ca, chain="bsc": {})
    monkeypatch.setattr(job.chain, "has_code",
                        lambda ca, chain="bsc": ca == second)
    assert job._probe_first([first, second], "BSC")["contract"] == second

    monkeypatch.setattr(job.chain, "has_code", lambda ca, chain="bsc": False)
    assert job._probe_first([first, second], "BSC") == {}


def test_a_card_whose_address_has_no_code_loses_the_contract(monkeypatch):
    from app.monitors.core import job

    ca = "0x" + "a" * 40
    card = {"chain": "BSC",
            "fields": {"onchain": {"contract": ca, "minted": 5},
                       "mint": {"done": 5, "source": "chain"}}}
    fields = dict(card["fields"])
    monkeypatch.setattr(job.chain, "has_code", lambda ca, chain="bsc": False)
    assert job._dead_contract(fields, card) is True
    assert fields["onchain"] == {}
    assert "mint" not in fields


def test_an_unanswered_node_keeps_the_contract(monkeypatch):
    from app.monitors.core import job

    ca = "0x" + "a" * 40
    card = {"chain": "BSC", "fields": {"onchain": {"contract": ca, "minted": 5}}}
    fields = dict(card["fields"])
    monkeypatch.setattr(job.chain, "has_code", lambda ca, chain="bsc": None)
    assert job._dead_contract(fields, card) is False
    assert fields["onchain"]["contract"] == ca


def test_the_zero_address_is_dropped_without_asking_a_node(monkeypatch):
    from app.monitors.core import job

    def boom(*args, **kwargs):
        raise AssertionError("an infrastructure address needs no node")

    monkeypatch.setattr(job.chain, "has_code", boom)
    card = {"chain": "BSC",
            "fields": {"onchain": {"contract": "0x" + "0" * 40}}}
    fields = dict(card["fields"])
    assert job._dead_contract(fields, card) is True
    assert fields["onchain"] == {}


def test_a_wei_scale_mint_reading_is_a_token_amount():
    from app.monitors.core import chain

    assert chain._token_amount(7245 * 10**18, 18) == 7245
    assert chain._token_amount(21813, 18) == 21813
    assert chain._token_amount(5000, None) == 5000


def _mint_row(receiver: str, tx: str = "0x" + "e" * 64) -> dict:
    from app.monitors.core import chain

    return {"topics": [chain.TRANSFER, chain.ZERO_TOPIC,
                       "0x" + receiver[2:].rjust(64, "0")],
            "blockNumber": "0x3e8", "transactionHash": tx}


def test_agent_evidence_reads_the_recipients_first(monkeypatch):
    """A backend-signed mint has one sender; the users are the recipients."""
    from app.monitors.core import chain

    receivers = ["0x" + str(i) * 40 for i in range(1, 7)]
    rows = [_mint_row(receiver) for receiver in receivers]
    monkeypatch.setattr(chain, "_mint_rows", lambda *a, **kw: (rows, True, 2000))
    monkeypatch.setattr(chain, "_rpc", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("no transaction lookup when recipients are enough")))
    holders = set(receivers[:4])
    monkeypatch.setattr(chain, "_uint", lambda ca, data, **kw: int(any(
        data.endswith(address[2:].rjust(64, "0")) for address in holders)))

    evidence = chain.agent_evidence("0x" + "a" * 40)
    assert evidence["agent_side"] == "recipients"
    assert evidence["agent_samples"] == 6
    assert evidence["agent_callers"] == 0.67
    assert evidence["agent_source"] == "callers"


def test_agent_evidence_falls_back_to_the_senders(monkeypatch):
    from app.monitors.core import chain

    rows = [_mint_row("0x" + "1" * 40, "0x" + str(i) * 64) for i in range(1, 7)]
    monkeypatch.setattr(chain, "_mint_rows", lambda *a, **kw: (rows, True, 2000))
    senders = ["0x" + str(i) * 40 for i in range(2, 8)]
    monkeypatch.setattr(chain, "_rpc", lambda method, params, **kw: {
        "from": senders[int(params[0][-1], 16) - 1]})
    monkeypatch.setattr(chain, "_uint", lambda ca, data, **kw: int(any(
        data.endswith(address[2:].rjust(64, "0")) for address in senders[:4])))

    evidence = chain.agent_evidence("0x" + "a" * 40)
    assert evidence["agent_side"] == "senders"
    assert evidence["agent_samples"] == 6
    assert evidence["agent_callers"] == 0.67


def test_the_agent_backfill_spends_three_cards_a_sweep(isolated_db, monkeypatch):
    from types import SimpleNamespace
    from app.monitors.core import job

    for index in range(5):
        db.monitor_card_put("m1", _card(
            slug="c%d" % index, title="C%d" % index,
            first_seen="2026-10-0%dT00:00:00+08:00" % (index + 1),
            fields={"onchain": {"contract": "0x" + str(index + 1) * 40}}))
    monkeypatch.setattr(job.chain, "agent_evidence", lambda ca, **kw: {
        "agent_source": "callers", "agent_callers": 1.0, "agent_side": "recipients",
        "agent_samples": 6, "agent_checked_at": "2026-10-31T12:00:00+08:00"})

    monitor = SimpleNamespace(id="m1")
    assert job._agent_backfill(monitor, db.monitor_cards("m1")) == 3
    rows = {row["slug"]: row for row in db.monitor_cards("m1")}
    done = sorted(slug for slug, row in rows.items()
                  if (row["fields"].get("onchain") or {}).get("agent_source") == "callers")
    assert done == ["c0", "c1", "c2"]


def test_a_checked_card_waits_for_the_backoff():
    from datetime import datetime, timedelta
    from app.monitors.core import job

    now = datetime.now().astimezone()
    def card(checked):
        return {"fields": {"onchain": {"contract": "0x" + "a" * 40,
                                       "agent_checked_at": checked}}}
    assert job._agent_evidence_missing(card(now.isoformat(timespec="seconds"))) is False
    old = (now - timedelta(days=40)).isoformat(timespec="seconds")
    assert job._agent_evidence_missing(card(old)) is True


def test_the_trace_known_set_is_not_shadowed():
    """The absorb helper is not allowed to take the trace stage's `_known()` name."""
    from app.monitors.core import job

    assert isinstance(job._known(), set)


def test_a_share_card_recovers_its_token_from_the_page(monkeypatch):
    from app.monitors.core import job

    token = "0x" + "a" * 40
    quote = sorted(job.QUOTE_TOKENS)[0]
    seen = {}

    def token_first(*addresses, limit=10, chain="bsc"):
        seen["addresses"] = addresses
        return {"token": token, "token_symbol": "TKN", "token_decimals": 18}

    monkeypatch.setattr(job.chain, "token_first", token_first)
    monkeypatch.setattr(job.chain, "probe", lambda ca, **kw: {
        "token": ca, "holders": 10, "perms": ["mintable"], "minted": 999})

    fields = {"type": "share", "onchain": {}}
    page = "pay with %s or receive %s" % (quote, token)
    assert job._find_token(fields, page, "bsc") is True
    assert fields["onchain"]["token"] == token
    assert fields["onchain"]["holders"] == 10
    assert fields["onchain"]["perms"] == ["mintable"]
    # The sale's own mint numbers must not be overwritten by the token's supply.
    assert "minted" not in fields["onchain"]
    assert quote not in seen["addresses"]


def test_a_card_with_a_token_is_left_alone(monkeypatch):
    from app.monitors.core import job

    token = "0x" + "a" * 40
    monkeypatch.setattr(job.chain, "token_first",
                        lambda *args, **kw: (_ for _ in ()).throw(
                            AssertionError("no second lookup")))
    fields = {"type": "share", "onchain": {"token": token}}
    assert job._find_token(fields, "token " + token, "bsc") is False


def test_the_domain_reads_the_project_not_the_post():
    from app.monitors.core import job

    assert job._domain_of({
        "summary": "NEO Noir 是数字时尚品牌，制作并销售 VRoid 用服装纹理；在 BOOTH 购买其付费商品参与。",
        "pages": ["neonoir - BOOTH shop, VRoid clothing textures"]}) == "other"
    assert job._domain_of({
        "summary": "MultiScreenAI 是同时调用 300 多个模型的多面板工具。",
        "pages": ["Binance Agent ID support. AI agents on BNB Chain."]}) in ("crypto", "ai_agent")
    assert job._domain_of({
        "summary": "SCRAPBEASTS 是浏览器射击游戏，付费铸造获得金币。",
        "pages": []}) == "crypto"
