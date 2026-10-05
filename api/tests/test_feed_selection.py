"""精选评分档 (AIHOT 的 score): 两次独立评分、按信源分级门槛入选、分不够只写便宜档.

评分与入选是两件事: 评分只看事件本身 (输入里没有来源名与分级), 门槛才按分级算. 这一份盯着三件:
存下来的分是两次的均值; 分数决定谁走理解档 / 谁走便宜档 / 谁能译正文; 模型没给分时规则分照旧兜底.

模型是假的 —— 这一层测的是编排与门槛, 不是模型打的分准不准.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.feed import analysis, writing
from app.feed.sources import snapshot
from app.feed.sources import store as source_store
from app.llm import client as llm
from app.store import db

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)

BUDGET = {"maxItems": 40, "concurrency": 2, "timeoutSeconds": 5,
          "retryHours": 24, "maxFailures": 3}

NAMES = {"rss:a": "Example Wire"}

BODY = "<p>Nova bridge drained for $12M.</p><p>More than 4,000 wallets were affected.</p>"
BLOCKS = ["Nova bridge drained for $12M.", "More than 4,000 wallets were affected."]

UNDERSTOOD = {"title_zh": "Nova 桥被掏空 1200 万美元",
              "summary": "Nova 桥被掏空, 四千多个钱包受影响.",
              "reason": "同类跨链桥通常会被一起重新定价."}
SUMMARIZED = {"title_zh": "Nova 桥被掏空",
              "summary": "Nova 桥被掏空 1200 万美元, 四千多个钱包受影响."}
TRANSLATED = {"t": ["Nova 桥被掏空 1200 万美元。", "四千多个钱包受影响。"]}
STRUCTURED = {"category": "security", "tags": ["安全", "跨链"], "subjects": ["ethereum"],
              "fact": {"subject": "Nova Bridge", "action": "drained", "object": "$12M"}}


@pytest.fixture(autouse=True)
def roster(isolated_db):
    source_store.import_roster()


@pytest.fixture(autouse=True)
def no_structure(monkeypatch):
    """结构抽取由 test_feed_structure.py 测: 这一份只测评分与门槛."""
    monkeypatch.setattr(writing, "STRUCTURE_CALLS", 0)
    # 预筛档与这一份要测的东西无关: 按掉它, 免得每条夹具都要补一个预筛答案 (预筛自己见 test_feed_prefilter.py)。
    monkeypatch.setattr(writing, "PREFILTER_CALLS", 0)


@pytest.fixture
def tier(monkeypatch):
    """把这条的源分级按住. 真分级在 config/feed/sources.yaml 里, 而这一层测的是门槛怎么用分级."""
    def use(name: str):
        monkeypatch.setattr(writing, "tier_of", lambda row: name)
    return use


def _row(external_id: str = "a-1") -> dict:
    return {
        "source_id": "rss:a",
        "external_id": external_id,
        "source_kind": "rss",
        "quoted": {},
        "identity_key": "other|",
        "title": "Nova bridge drained",
        "url": "https://a/%s" % external_id,
        "author": "",
        "body": BODY,
        "media": [],
        "published_at": (NOW - timedelta(minutes=5)).isoformat(),
    }


def _fake(monkeypatch, scores, **answers):
    """假模型: 评分那两问按顺序吃 `scores` (可以夹一个异常), 其余按 purpose 翻牌子."""
    seen: list[str] = []
    asked: list[list[str]] = []
    left = list(scores)

    def call(messages, **kw):
        purpose = kw["purpose"]
        seen.append(purpose)
        if purpose == writing.PURPOSE_SCORE:
            value = left.pop(0)
            if isinstance(value, Exception):
                raise value
            return {"attentionScore": value}
        answer = answers[purpose]
        if purpose == writing.PURPOSE_TRANSLATE:
            asked.append(json.loads(messages[1]["content"])["segments"])
        if isinstance(answer, Exception):
            raise answer
        return answer(messages) if callable(answer) else answer

    monkeypatch.setattr(writing.llm, "complete_json", call)
    return seen, asked


def _written(external_id: str = "a-1") -> dict:
    rows = snapshot.read_written_window(NOW, 1)
    assert ("rss:a", external_id) in rows, "这一行没有被模型写过"
    return rows[("rss:a", external_id)]


def test_the_mean_of_two_independent_scores_is_what_is_stored(isolated_db, monkeypatch, tier):
    """评分档存的是**两次**的分与均值: 一次评分把"这稿写得好不好"和"这件事值不值得看"混在一起."""
    tier("T2")
    seen, _ = _fake(monkeypatch, [40, 80], **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD})
    db.feed_items_put([_row()])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    assert seen.count(writing.PURPOSE_SCORE) == 2
    row = _written()
    assert json.loads(row["llm_scores_json"]) == [40, 80]
    assert row["score_llm"] == 60.0
    assert writing.version(writing.SCORE_PROMPT) in (row["prompt_version"] or "")


def test_below_the_understanding_floor_only_the_cheap_tier_runs(isolated_db, monkeypatch, tier):
    """没到理解档地板 (50) 的只写标题与摘要: 那一档写出来的东西是给"全部动态"用的, 不值得
    花理解档的钱 (AIHOT 的 understandFloor)."""
    tier("T2")
    seen, _ = _fake(monkeypatch, [20, 30], **{writing.PURPOSE_SUMMARIZE: SUMMARIZED})
    db.feed_items_put([_row()])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    assert writing.PURPOSE_UNDERSTAND not in seen
    assert seen.count(writing.PURPOSE_SUMMARIZE) == 1
    row = _written()
    assert row["title_zh"] == SUMMARIZED["title_zh"]
    assert row["summary_llm"] == SUMMARIZED["summary"]
    assert not row["reason_llm"], "便宜档不许写推荐理由"
    assert row["llm_stage"] == "full"


def test_the_stored_score_is_the_floored_mean(isolated_db, monkeypatch, tier):
    """页面上的分是两次均值的**下取整** (AIHOT 的 `Math.floor(sum / SCORE_CALLS)`).

    60.5 印成 60 不是 61 —— 一个分数不该因为一次四舍五入就越过它没到的门槛。
    """
    tier("T2")
    _fake(monkeypatch, [40, 81], **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD})
    db.feed_items_put([_row()])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    assert _written()["score_llm"] == 60


def test_exactly_the_understanding_floor_takes_the_cheap_tier(isolated_db, monkeypatch, tier):
    """均值正好 50 走便宜档: 地板是「高过」才算到 (AIHOT 的 `sum > UNDERSTAND_FLOOR * SCORE_CALLS`)."""
    tier("T2")
    seen, _ = _fake(monkeypatch, [50, 50], **{writing.PURPOSE_SUMMARIZE: SUMMARIZED})
    db.feed_items_put([_row()])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    assert writing.PURPOSE_UNDERSTAND not in seen
    assert seen.count(writing.PURPOSE_SUMMARIZE) == 1


def test_a_rewrite_by_the_cheap_tier_drops_the_old_understanding_token(
        isolated_db, monkeypatch, tier):
    """上次按理解档写的行, 这次分掉到地板以下只写了便宜档: 理解档的 token 必须跟着走.

    留着它, 下回分再涨上来这条会拿便宜档的字冒充理解档的字, 而且完全静默 —— 版本 token 是
    "这一档按哪一版写过"的唯一凭据, 它不能同时记着两个写作档。
    """
    tier("T2")
    db.feed_items_put([_row()])
    db.feed_items_write_llm([{**{k: _row()[k] for k in ("source_id", "external_id")},
                             "title_zh": "旧标题", "summary_llm": "旧摘要",
                             "llm_stage": "understand",
                             "prompt_version": writing.understand_version()}])
    _fake(monkeypatch, [20, 30], **{writing.PURPOSE_SUMMARIZE: SUMMARIZED})
    writing.run(NOW, names=NAMES, budget=BUDGET)

    versions = _written()["prompt_version"] or ""
    assert writing.summarize_version() in versions
    assert "understand.md@" not in versions


def test_between_the_floor_and_the_threshold_is_understood_but_not_translated(
        isolated_db, monkeypatch, tier):
    """50-76 这一档: 用理解档写 (精选线附近要写得好一点), 但没入选, 不花翻译的钱."""
    tier("T2")
    seen, asked = _fake(monkeypatch, [60, 60], **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD,
                                                 writing.PURPOSE_TRANSLATE: TRANSLATED})
    db.feed_items_put([_row()])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    assert writing.PURPOSE_UNDERSTAND in seen
    assert writing.PURPOSE_TRANSLATE not in seen and asked == []
    assert not _written()["body_zh"]


def test_over_the_tier_threshold_earns_the_translation(isolated_db, monkeypatch, tier):
    """T1 的门槛是 60: 两次都是 70 就入选, 英文正文要被译."""
    tier("T1")
    seen, asked = _fake(monkeypatch, [70, 70], **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD,
                                                 writing.PURPOSE_TRANSLATE: TRANSLATED})
    db.feed_items_put([_row()])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    assert seen == [writing.PURPOSE_SCORE, writing.PURPOSE_SCORE,
                    writing.PURPOSE_UNDERSTAND, writing.PURPOSE_TRANSLATE]
    assert asked == [BLOCKS]
    assert _written()["body_zh"]


def test_the_same_scores_miss_the_media_threshold(isolated_db, monkeypatch, tier):
    """同样 70+70: 媒体档 (T2) 的门槛是 76, 不入选, 也不译. 门槛按分级算, 不是一把尺."""
    tier("T2")
    _, asked = _fake(monkeypatch, [70, 70], **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD,
                                               writing.PURPOSE_TRANSLATE: TRANSLATED})
    db.feed_items_put([_row()])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    assert asked == [] and not _written()["body_zh"]


def test_a_tier_outside_the_table_is_never_scored(isolated_db, monkeypatch, tier):
    """不在这张表里的分级 (比如不参与精选的那一档) 不评分, 走便宜档 —— AIHOT 里 EXCLUDE_MP 与
    表外分级都是这么处理的."""
    tier("T9")
    seen, _ = _fake(monkeypatch, [], **{writing.PURPOSE_SUMMARIZE: SUMMARIZED})
    db.feed_items_put([_row()])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    assert writing.PURPOSE_SCORE not in seen and seen == [writing.PURPOSE_SUMMARIZE]
    assert _written()["llm_scores_json"] is None


def _stored(external_id: str = "a-1") -> dict:
    """库里的原行: 没写成的那一条没有 title_zh, 读不到"模型写过的窗口"里 (那是给页面用的)."""
    for row in db.feed_items_since((NOW - timedelta(hours=1)).isoformat(timespec="seconds")):
        if row["external_id"] == external_id:
            return row
    raise AssertionError("这一行不在库里")


def test_a_score_that_fails_leaves_the_row_on_the_rule_score(isolated_db, monkeypatch, tier):
    """评分挂掉不算写过: 只落 `llm_at` 把这条推到下一轮, 一个字都不改."""
    tier("T2")

    class Boom(RuntimeError):
        pass

    _fake(monkeypatch, [Boom("the endpoint is down")], **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD})
    db.feed_items_put([_row()])
    report = writing.run(NOW, names=NAMES, budget=BUDGET)

    assert report["failed"] == 1
    row = _stored()
    assert row["score_llm"] is None and row["llm_scores_json"] is None
    assert row["llm_stage"] is None and not row["title_zh"]


def test_the_rule_score_is_the_fallback_when_no_model_score_exists():
    """没有模型分的时候, 入选与否还是老规矩: 规则分过线 (没配模型的机器上页面不能空)."""
    row = {"source_id": "rss:a"}
    assert writing.picked_in_curated(row, analysis.CURATED_MIN_SCORE) is True
    assert writing.picked_in_curated(row, analysis.CURATED_MIN_SCORE - 1) is False


def test_the_thresholds_are_the_numbers_aihot_uses():
    assert writing.SELECTION_THRESHOLDS == {"T1": 60, "T1_5": 65, "T2": 76}
    assert writing.UNDERSTAND_FLOOR == 50
    assert writing.SCORE_CALLS == 2


def test_the_understanding_tier_tags_win_over_the_structure_tags(isolated_db, monkeypatch, tier):
    """标签听理解档的 (AIHOT 的 `writing.tags ?? structure.tags`): 结构档先写一组, 理解档给了
    就以它为准 —— 它读过全文, 比只看标题与首段的标签档准. 从 `subjects` 推出来的实体标签照旧
    补在后头, 那是另一条来源, 不是这一档的判断."""
    tier("T2")
    monkeypatch.setattr(writing, "STRUCTURE_CALLS", 1)
    _fake(monkeypatch, [60, 60], **{
        writing.PURPOSE_STRUCTURE: STRUCTURED,
        writing.PURPOSE_UNDERSTAND: {**UNDERSTOOD, "tags": ["安全", "跨链", "Solana"]}})
    db.feed_items_put([_row()])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    stored = json.loads(_written()["tags_json"])
    assert stored["tags"] == ["跨链"]          # 理解档那一组, 不是结构档的
    assert stored["entities"] == ["Solana", "Ethereum"]


def test_a_tagless_understanding_answer_keeps_the_structure_tags(isolated_db, monkeypatch, tier):
    """模型一个标签都没写时保留结构档那一组: 空答案不该把已经抽好的标签抹掉 (AIHOT 那边
    `.catch([])` 会退成只剩类别, 页面因此丢词; 这一处我们选择保住已有的字)."""
    tier("T2")
    monkeypatch.setattr(writing, "STRUCTURE_CALLS", 1)
    _fake(monkeypatch, [60, 60], **{writing.PURPOSE_STRUCTURE: STRUCTURED,
                                    writing.PURPOSE_UNDERSTAND: UNDERSTOOD})
    db.feed_items_put([_row()])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    stored = json.loads(_written()["tags_json"])
    assert stored["tags"] == ["跨链"]
    assert stored["entities"] == ["Ethereum"]


def test_a_content_refusal_writes_the_cheap_tier_instead(isolated_db, monkeypatch, tier):
    """理解档被内容审核挡下时退回便宜档 (AIHOT 的 `?? await runSummarize()`): 这条材料写不了
    "好稿", 但有标题与摘要就还能印; 整条失败只会让它下一轮再来撞同一道墙."""
    tier("T2")
    seen, _ = _fake(monkeypatch, [60, 60], **{
        writing.PURPOSE_UNDERSTAND: llm.ContentRefused("upstream 400: contentFilter"),
        writing.PURPOSE_SUMMARIZE: SUMMARIZED})
    db.feed_items_put([_row()])
    report = writing.run(NOW, names=NAMES, budget=BUDGET)

    assert seen == [writing.PURPOSE_SCORE, writing.PURPOSE_SCORE,
                    writing.PURPOSE_UNDERSTAND, writing.PURPOSE_SUMMARIZE]
    assert (report["wrote"], report["full"], report["failed"]) == (1, 1, 0)
    row = _written()
    assert row["title_zh"] == SUMMARIZED["title_zh"]
    assert row["llm_stage"] == "full"
    versions = row["prompt_version"] or ""
    assert writing.summarize_version() in versions
    assert writing.understand_version() not in versions


def test_an_ordinary_understanding_failure_is_not_downgraded(isolated_db, monkeypatch, tier):
    """只有内容拒绝才退档 (AIHOT 的 `!isContentFilter` 照旧上抛): 把限流与超时也当成"写不了",
    一整轮会静默地全降成便宜档, 而账单上看得出来、日志里看不出来."""
    tier("T2")
    seen, _ = _fake(monkeypatch, [60, 60], **{
        writing.PURPOSE_UNDERSTAND: llm.LLMUnavailable("timeout"),
        writing.PURPOSE_SUMMARIZE: SUMMARIZED})
    db.feed_items_put([_row()])
    report = writing.run(NOW, names=NAMES, budget=BUDGET)

    assert report["failed"] == 1
    assert writing.PURPOSE_SUMMARIZE not in seen


def test_the_understanding_call_carries_its_own_ceiling_and_timeout(isolated_db, monkeypatch, tier):
    """调用参数按档给 (AIHOT 的调用表): 理解档写三段字、读完整文, 所以上限 16384、超时地板
    180 秒 —— 与一条摘要共用一个小的全局上限, 会在答案中间被截断."""
    tier("T2")
    calls: list[dict] = []

    def answer(messages, **kw):
        calls.append(kw)
        if kw["purpose"] == writing.PURPOSE_SCORE:
            return {"attentionScore": 60}
        return UNDERSTOOD

    monkeypatch.setattr(writing.llm, "complete_json", answer)
    db.feed_items_put([_row()])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    understand = [k for k in calls if k["purpose"] == writing.PURPOSE_UNDERSTAND]
    assert len(understand) == 1
    assert understand[0]["max_tokens"] == 16384
    assert understand[0]["timeout"] == 180.0          # 夹具的旋钮是 5 秒, 地板把它顶到 180
    score = [k for k in calls if k["purpose"] == writing.PURPOSE_SCORE]
    assert {k["temperature"] for k in score} == {0.2}
    assert {k["max_tokens"] for k in score} == {1024}


def test_the_thinking_scorer_gets_the_thinking_parameters(isolated_db, monkeypatch, tier):
    """评分档的参数按模型查 AIHOT 的 `SCORE_CALL` 表: 思考型评分模型要 temperature 1 与
    65536 的额度, 否则额度被思考吃光、答案被截掉."""
    tier("T2")
    monkeypatch.setattr(writing.capability, "model_for",
                        lambda use: "glm-5.3-flash-selection")
    calls: list[dict] = []

    def answer(messages, **kw):
        calls.append(kw)
        if kw["purpose"] == writing.PURPOSE_SCORE:
            return {"attentionScore": 60}
        return UNDERSTOOD

    monkeypatch.setattr(writing.llm, "complete_json", answer)
    db.feed_items_put([_row()])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    score = [k for k in calls if k["purpose"] == writing.PURPOSE_SCORE]
    assert {k["temperature"] for k in score} == {1.0}
    assert {k["max_tokens"] for k in score} == {65536}
    assert {k["timeout"] for k in score} == {180.0}


def test_the_call_table_is_the_numbers_aihot_uses():
    assert writing.STAGE_MAX_TOKENS == {"structure": 800, "understand": 16384, "summarize": 2048}
    assert writing.SCORE_CALL_DEFAULT == (0.2, 1024, 120.0)
    assert writing.SCORE_CALL["glm-5.3-flash-selection"] == (1.0, 65536, 180.0)
    assert writing.UNDERSTAND_TIMEOUT == 180.0
