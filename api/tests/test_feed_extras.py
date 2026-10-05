"""五件新东西的合同: 抓取队列 (1.2) / 飞书投递 (5.8) / 管理台口令 (5.12) /
金标评测 (5.22) / MCP 出口 (routes/mcp)。

它们各自只有一两条真正会坏的承诺, 所以测的就是那一两条: 队列在崩了之后能接着跑、
同一期不会发两条、设了口令之后改数据的入口真的关上、金标全对、MCP 的工具表就是那几个。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.feed import auth, delivery, eval as feed_eval, mcp, writing
from app.store import db

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


# ── 1.2 抓取队列 ──────────────────────────────────────────────────────────────

def test_an_expired_lease_is_claimed_again(isolated_db):
    """崩在半路的那几条还是 `leased`, 而它们的租约一过期就回到 `pending` ——
    这就是「崩了下一分钟接着跑」那一行代码。
    """
    db.feed_queue_enqueue([{"source_id": "rss:a", "external_id": "a-1",
                            "url": "https://a/1"}])
    assert db.feed_queue_stats(_stamp(NOW))["pending"] == 1

    # 认领并“崩”掉: 租约到这一刻为止.
    claimed = db.feed_queue_claim(5, _stamp(NOW), at=_stamp(NOW))
    assert [row["external_id"] for row in claimed] == ["a-1"]
    assert db.feed_queue_stats(_stamp(NOW))["leased"] == 1
    assert db.feed_queue_claim(5, _stamp(NOW + timedelta(minutes=1)),
                              at=_stamp(NOW + timedelta(minutes=1))) == []

    # 租约过了: 回到 pending, 下一次认领又拿得到它。
    assert db.feed_queue_reclaim(_stamp(NOW + timedelta(minutes=2))) == 1
    again = db.feed_queue_claim(5, _stamp(NOW + timedelta(minutes=3)),
                               at=_stamp(NOW + timedelta(minutes=2)))
    assert [row["external_id"] for row in again] == ["a-1"]


def test_a_finished_row_is_not_queued_again(isolated_db):
    """已经补好的不能被重新排队 —— 它的正文已经在表上了, 再跑一趟就是白跑。"""
    rows = [{"source_id": "rss:a", "external_id": "a-1", "url": "https://a/1"}]
    db.feed_queue_enqueue(rows)
    claimed = db.feed_queue_claim(1, _stamp(NOW), at=_stamp(NOW))
    db.feed_queue_finish(claimed[0]["id"], "done")

    db.feed_queue_enqueue(rows)
    stats = db.feed_queue_stats(_stamp(NOW))
    assert stats["done"] == 1 and stats["pending"] == 0


# ── 5.8 飞书投递 ──────────────────────────────────────────────────────────────

def _issue(**extra) -> dict:
    row = {"id": "feed-day-2026-10-03", "headline": "库拉索被盗", "key": "2026-10-03",
           "entries": 3, "seen": 40,
           "payload_json": json.dumps({"lead": "一条导语。",
                                       "entries": [{"title": "一条"},
                                                   {"title": "二条"},
                                                   {"title": "三条"}]},
                                      ensure_ascii=False)}
    row.update(extra)
    return row


def test_the_same_issue_is_only_queued_once(isolated_db, monkeypatch):
    monkeypatch.setattr(delivery, "webhook", lambda: "https://open.feishu.cn/hook/x")
    assert delivery.queue_issue(_issue()) is not None
    assert delivery.queue_issue(_issue()) is None          # 第二遍排不进去
    assert [row["subject"] for row in db.feed_deliveries_recent(5)] == [
        "feed-day-2026-10-03"]


def test_an_empty_issue_is_not_sent(isolated_db, monkeypatch):
    """没人需要被告知「这一天没事」。"""
    monkeypatch.setattr(delivery, "webhook", lambda: "https://open.feishu.cn/hook/x")
    assert delivery.queue_issue(_issue(entries=0)) is None
    assert db.feed_deliveries_recent(5) == []


def test_no_webhook_means_no_queue_and_no_claim(isolated_db, monkeypatch):
    monkeypatch.setattr(delivery, "webhook", lambda: "")
    assert delivery.queue_issue(_issue()) is None
    assert delivery.run(NOW) == {"claimed": 0, "sent": 0, "failed": 0}


def test_a_failed_delivery_backs_off(isolated_db, monkeypatch):
    """发不出去就记一笔带退避的账, 而不是每五分钟敲一次。"""
    monkeypatch.setattr(delivery, "webhook", lambda: "https://open.feishu.cn/hook/x")
    delivery.queue_issue(_issue())

    def boom(url, body, timeout):
        raise RuntimeError("nope")

    monkeypatch.setattr(delivery, "send", boom)
    report = delivery.run(NOW)
    assert (report["claimed"], report["sent"], report["failed"]) == (1, 0, 1)
    row = db.feed_deliveries_recent(1)[0]
    assert row["status"] == "failed" and row["attempts"] == 1
    assert row["next_try_at"] > _stamp(NOW)
    assert delivery.run(NOW)["claimed"] == 0                # 还在退避里


# ── 5.12 管理台口令 ─────────────────────────────────────────────────────────────

def test_a_token_survives_a_round_trip_and_expires(monkeypatch):
    token = auth.issue(NOW)
    assert auth.valid(token, NOW + timedelta(hours=1))
    assert not auth.valid(token, NOW + timedelta(hours=auth.TTL_HOURS + 1))
    assert not auth.valid(token[:-1] + ("0" if token[-1] != "0" else "1"), NOW)
    assert not auth.valid("", NOW) and not auth.valid("nonsense", NOW)


def test_no_password_means_the_door_is_open(isolated_db):
    assert auth.guarded() is False and auth.signed_in(_request(None)) is True
    assert auth.matches("") is False            # 什么都能进, 但空口令不算答对


def test_a_password_shuts_the_door(isolated_db):
    db.feed_setting_put("admin_password", "s3cret")
    assert auth.guarded() is True
    assert auth.signed_in(_request("")) is False
    assert auth.matches("s3cret") and not auth.matches("s3cre")
    assert auth.signed_in(_request(auth.issue())) is True


class _request:
    """只有 `cookies` 一个属性的请求 —— `auth` 读的就只有这一个。"""

    def __init__(self, token: str | None) -> None:
        self.cookies = {auth.COOKIE: token} if token else {}


def test_the_admin_settings_never_answer_with_the_plaintext(isolated_db, client):
    db.feed_setting_put("feishu_webhook", "https://open.feishu.cn/hook/very-secret")
    body = client.get("/api/v1/feed/admin/settings").json()
    rows = {row["key"]: row for row in body["settings"]}
    assert rows["feishu_webhook"]["configured"] is True
    assert "very-secret" not in json.dumps(body, ensure_ascii=False)
    assert rows["feishu_webhook"]["masked"].endswith("cret")


def test_the_gate_answers_401_until_the_password_is_given(isolated_db, client):
    db.feed_setting_put("admin_password", "s3cret")
    assert client.post("/api/v1/feed/override", json={
        "source_id": "rss:a", "external_id": "a-1", "action": "withdraw"}).status_code == 401
    assert client.post("/api/v1/feed/admin/login",
                       json={"password": "wrong"}).status_code == 401

    ok = client.post("/api/v1/feed/admin/login", json={"password": "s3cret"})
    assert ok.status_code == 200 and ok.json() == {"guarded": True, "signed_in": True}
    # 同一个 TestClient 接下来带着那枚 cookie: 这一次不再是 401。
    assert client.get("/api/v1/feed/admin/settings").status_code == 200
    assert client.post("/api/v1/feed/admin/logout").status_code == 200
    assert client.get("/api/v1/feed/admin/settings").status_code == 401


# ── 5.22 金标评测 ────────────────────────────────────────────────────────────

def test_the_golden_set_agrees_with_the_rules(isolated_db):
    """人工标的那一份与现在的规则对得上 —— 对不上就是有人改了规则而没同步那份标答。"""
    report = feed_eval.score()
    assert report["total"] >= 20 and report["passed"] == report["total"]
    assert report["misses"] == []
    assert {row["axis"] for row in report["axes"]} == {
        "certainty", "channel", "pool", "selectable", "page"}


def test_the_endpoint_serves_the_same_number(isolated_db, client):
    body = client.get("/api/v1/feed/eval").json()
    assert body["agreement"] == 1.0 and body["total"] == feed_eval.score()["total"]


# ── routes/mcp ────────────────────────────────────────────────────────────────────

def test_mcp_lists_its_tools():
    answer = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [tool["name"] for tool in answer["result"]["tools"]]
    assert names == ["feed_hot", "feed_items", "feed_item", "feed_story",
                     "feed_topics", "feed_site"]
    for tool in answer["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object"


def test_mcp_answers_a_notification_with_nothing():
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_an_unknown_method_or_tool_is_an_error_not_a_crash():
    method = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "nope"})
    assert method["error"]["code"] == -32601
    tool = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "nope", "arguments": {}}})
    assert tool["error"]["code"] == -32602


def test_mcp_over_http_answers_both_get_and_post(isolated_db, client):
    described = client.get("/api/mcp").json()
    assert described["server"] == "crypto-hunter-feed"

    answered = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 9,
                                             "method": "initialize"}).json()
    assert answered["result"]["serverInfo"]["name"] == "crypto-hunter-feed"


def test_mcp_calls_a_read_tool(isolated_db, client):
    """`tools/call` 回两份: 一段给模型读的文本, 一份给程序读的结构。"""
    answer = client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "feed_site", "arguments": {}}}).json()
    result = answer["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["name"] == mcp.outlets.site.NAME
    assert json.loads(result["content"][0]["text"])["name"] == mcp.outlets.site.NAME
# ── 4.8a 等重判的那些不算证据 ────────────────────────────────────────────────

def _evidence_pool():
    """一件事 + 两条报道: `a-1` 是按现在这一版定的, `b-1` 是按 `v1` 定的 (还在等重判)。"""
    story = db.feed_story_new({"title": "Nova 被抽干", "at": _stamp(NOW)})
    fact = db.feed_fact_new({"story_id": story, "title": "Nova 被抽干",
                             "at": _stamp(NOW - timedelta(hours=3)), "root": 1})
    db.feed_items_put([
        {"source_id": "rss:a", "external_id": "a-1", "source_kind": "rss",
         "identity_key": "nova|", "title": "Nova drained", "url": "https://a/1",
         "published_at": _stamp(NOW - timedelta(hours=2))},
        {"source_id": "rss:b", "external_id": "b-1", "source_kind": "rss",
         "identity_key": "nova|", "title": "Nova drained again", "url": "https://b/1",
         "published_at": _stamp(NOW - timedelta(hours=1))},
    ])
    db.feed_item_facts_set([
        {"source_id": "rss:a", "external_id": "a-1", "fact_id": fact, "role": "primary",
         "prompt_version": "v2"},
        {"source_id": "rss:b", "external_id": "b-1", "fact_id": fact, "role": "report",
         "prompt_version": "v1"},
    ])
    return fact


def test_a_report_waiting_for_a_regroup_is_not_evidence(isolated_db):
    """按旧版规则定过、还没重判的那一条不算证据 (AIHOT 的 `recall.trusted`)。

    拿它去给别人当证据, 等于把旧口径借着别人再抄一遍 —— 而它自己的归属本来就该重新决定。
    不筛的时候 (老的调用方) 两条都在, 免得这个规则把别人的读法一起改了。
    """
    _evidence_pool()
    since = _stamp(NOW - timedelta(days=1))
    _, all_members = db.feed_fact_pool(since)
    assert {m["external_id"] for m in all_members} == {"a-1", "b-1"}
    _, trusted = db.feed_fact_pool(since, evidence_version="v2")
    assert {m["external_id"] for m in trusted} == {"a-1"}


def test_a_manual_decision_is_evidence_at_any_version(isolated_db):
    """人工钉过的一直算 (同一条规则的另一半): 人已经判过了, 机器不该拿版本号把它算没。"""
    _evidence_pool()
    db.feed_override_set({"source_id": "rss:b", "external_id": "b-1",
                          "reason": "人判的", "actor": "op"})
    since = _stamp(NOW - timedelta(days=1))
    _, trusted = db.feed_fact_pool(since, evidence_version="v2")
    assert {m["external_id"] for m in trusted} == {"a-1", "b-1"}


# ── 3.6 引用帖的译文 ────────────────────────────────────────────────────────

#: 一句够长、不是中文的原话 —— 两个门槛都过.
QUOTED = "the protocol was never audited by anyone"


def _quoting_item(source_id: str, external_id: str, url: str, text: str = QUOTED):
    return {"source_id": source_id, "external_id": external_id, "source_kind": "x_search",
            "identity_key": "q|" + external_id, "title": "有人说话了",
            "url": "https://x.com/i/status/" + external_id,
            "published_at": _stamp(NOW - timedelta(hours=1)),
            "quoted": {"text": text, "handle": "alice", "url": url}}


def test_a_quoted_post_is_translated_once_and_written_back(isolated_db, monkeypatch):
    """十条帖子引同一句话只问一次模型, 而译文要落到**每一条**引用了它的行上。

    页面读的是条目自己的 `quoted_json.translation`, 所以只把译文存进缓存表等于没做 —— 读者那一
    格还是空的。两条链接一个 `x.com` 一个 `twitter.com` 带尾巴, 同一个号必须认成同一条推。
    """
    db.feed_items_put([
        _quoting_item("x:a", "1", "https://x.com/alice/status/9"),
        _quoting_item("x:b", "2", "https://twitter.com/alice/status/9?s=20"),
    ])
    asked: list[str] = []

    def fake_ask(prompt, block, schema, **kwargs):
        asked.append(str(kwargs.get("purpose")))
        return writing.TranslateDraft(t=["那句话从来没有被人审计过"])

    # 这一份测的是模型那条路: 档位来自仓库里那份 pipeline.yaml, 而这里按 full 走.
    monkeypatch.setattr(writing.pipeline, "stages",
                        lambda *a, **k: dict(writing.pipeline.PRESETS["full"]))
    monkeypatch.setattr(writing, "_ask", fake_ask)
    assert writing.translate_quotes(NOW) == {"stored": 1, "reused": 0, "asked": 1, "written": 2,
                                             "local": 0}
    assert asked == ["feed.translate_quoted"]
    for source, external in (("x:a", "1"), ("x:b", "2")):
        stored = json.loads(db.feed_item_get(source, external)["quoted_json"])
        assert stored["translation"] == "那句话从来没有被人审计过"
    # 第二趟: 同一句话的指纹没变, 一个字都不再问.
    assert writing.translate_quotes(NOW) == {"stored": 0, "reused": 0, "asked": 0, "written": 0,
                                             "local": 0}
    assert asked == ["feed.translate_quoted"]


def test_a_quoted_post_we_already_translated_is_reused(isolated_db, monkeypatch):
    """被引的那条帖子自己也收过、译过: 直接拿它那一份, 不再问模型。

    同一条推文的两份译文不一样, 读的人会以为其中一份在改口.
    """
    db.feed_sources_put([{"id": "x:alice", "name": "Alice", "kind": "x_search"}])
    db.feed_items_put([
        {"source_id": "x:alice", "external_id": "9", "source_kind": "x_search",
         "identity_key": "x|9", "title": "原帖", "url": "https://x.com/alice/status/9",
         "published_at": _stamp(NOW - timedelta(hours=4))},
        _quoting_item("x:bob", "3", "https://x.com/alice/status/9"),
    ])
    db.feed_items_write_llm([{"source_id": "x:alice", "external_id": "9",
                              "body_zh": "那份审计报告没有人签过字",
                              "body_complete": 1}])

    def boom(*args, **kwargs):
        raise AssertionError("own translation exists: the model must not be asked")

    # 这一份测的是模型那条路: 档位来自仓库里那份 pipeline.yaml, 而这里按 full 走.
    monkeypatch.setattr(writing.pipeline, "stages",
                        lambda *a, **k: dict(writing.pipeline.PRESETS["full"]))
    monkeypatch.setattr(writing, "_ask", boom)
    assert writing.translate_quotes(NOW) == {"stored": 1, "reused": 1, "asked": 0, "written": 1,
                                             "local": 0}
    stored = json.loads(db.feed_item_get("x:bob", "3")["quoted_json"])
    assert stored["translation"] == "那份审计报告没有人签过字"


def test_a_chinese_quote_is_left_alone(isolated_db, monkeypatch):
    """本来就是中文的那句原话不译 —— 译一遍只是把同一句话抄两遍, 还多一份会走样的副本。"""
    db.feed_items_put([
        _quoting_item("x:c", "4", "https://x.com/alice/status/11", text="这份审计报告没有人签过字"),
    ])

    def boom(*args, **kwargs):
        raise AssertionError("a Chinese quote must not be translated")

    # 这一份测的是模型那条路: 档位来自仓库里那份 pipeline.yaml, 而这里按 full 走.
    monkeypatch.setattr(writing.pipeline, "stages",
                        lambda *a, **k: dict(writing.pipeline.PRESETS["full"]))
    monkeypatch.setattr(writing, "_ask", boom)
    assert writing.translate_quotes(NOW) == {"stored": 0, "reused": 0, "asked": 0, "written": 0,
                                             "local": 0}
