"""监控：一个监控一套卡、一个自己的时钟（2026-10-29, user）。"""

import json
from datetime import datetime

import pytest

from app.store import db


@pytest.fixture(autouse=True)
def _isolated_monitor_config(tmp_path, monkeypatch):
    """设定文件在第一次读取时落盘，测试不能往仓库的 monitors/<id>/ 里写。"""
    from app.monitors.core import settings as monitor_settings

    monkeypatch.setattr(monitor_settings, "ROOT", tmp_path)


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


def test_the_summary_is_rewritten_from_the_official_page(monkeypatch):
    """帖子里的叫法会被官网原文覆盖：摘要重写只吃官网正文（2026-10-30, user）。"""
    from app.monitors.core import job
    from app.monitors.registry import BINANCE_AGENT_ID

    seen = {}

    def fake(messages, **kw):
        seen["text"] = messages[1]["content"]
        return {"summary": "官网口径：每钱包 10 份，每份 10 万 BNBS。"}

    monkeypatch.setattr(job.llm, "complete_json", fake)
    out = job._rewrite(BINANCE_AGENT_ID,
                       {"pages": ["<p>Send 0.01 BNB to receive 100,000 BNBS. 10 shares per wallet</p>"]})
    assert out.startswith("官网口径")
    assert "10 shares per wallet" in seen["text"], "喂给模型的是官网原文"
    assert job._rewrite(BINANCE_AGENT_ID, {"pages": []}) == "", "没有官网就不重写"


def test_a_rewrite_that_found_nothing_keeps_the_old_summary(monkeypatch):
    """页面只有壳时模型会写「官网原文未提供」；这句话不能覆盖原摘要（Nemotron / ChyAI）."""
    from app.monitors.core import job
    from app.monitors.registry import BINANCE_AGENT_ID

    monkeypatch.setattr(
        job.llm, "complete_json",
        lambda messages, **kw: {"summary": "官网原文未提供，无法确认项目内容。"})
    assert job._rewrite(BINANCE_AGENT_ID, {"pages": ["<p>loading</p>"]}) == ""
    monkeypatch.setattr(
        job.llm, "complete_json",
        lambda messages, **kw: {"summary": "HODLER 是像素宇航员项目，官方原文未说明参与方式与门槛。"})
    assert job._rewrite(BINANCE_AGENT_ID, {"pages": ["<p>hodler</p>"]}) == ""


def test_an_unjudged_field_is_still_missing():
    """模型写的「未判定」不是答案，补字段阶段必须继续找（Phosphor 的 Price 1 USDT）。"""
    from app.monitors.core import job

    assert job._blank("未判定") and job._blank(" unknown ") and job._blank("")
    assert not job._blank("1 USDT")
    assert job._missing({"price": "未判定", "mint_done": 6, "mint_total": 10000,
                         "mint_unit": "cards", "token_total": "", "deadline": "2026-11-01",
                         "contract": ""})


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
    projects, batches, failed = job._extract(monitor, posts)

    assert batches == job.MAX_BATCHES and len(calls) == job.MAX_BATCHES, "超出批数的帖子不读，省成本"
    assert failed == 0
    assert all(len(chunk) <= monitor.max_posts for chunk in calls)
    assert len(projects) == job.ROUND_PROJECTS, "宁缺毋滥：整轮最多 20 张新卡"
    dup = [row for row in projects if row["handle"] == "dup"]
    assert len(dup) == 1 and dup[0]["summary"] == "a much longer summary"


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
    assert db.reconcile_running_scans() == 2
    assert db.monitor_scan_get("ms1")["status"] == "failed"
    assert db.opportunity_scan_get("os1")["status"] == "failed"


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


def test_a_plain_http_site_is_upgraded_when_https_answers():
    """官网优先 https：trace 给 http://danta.ai/，卡上应该给 https（2026-10-30, user）."""
    from app.monitors.core import job

    assert job._secure("http://danta.ai/", probe=lambda url: True) == "https://danta.ai/"
    assert job._secure("http://danta.ai/", probe=lambda url: False) == "http://danta.ai/"
    assert job._secure("https://example.com/", probe=lambda url: False) == "https://example.com/"


def test_the_collection_knobs_default_to_the_registry(client):
    values = client.get("/api/v1/monitors/binance-agent-id/settings").json()["values"]
    assert (values["window_hours"], values["slice_hours"], values["min_score"]) == (24, 4, 30)
