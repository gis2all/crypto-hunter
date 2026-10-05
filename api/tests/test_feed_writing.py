"""写作站: 两档、三态、熔断、按块译, 以及模型写的字怎么盖到页面上.

这一站是流水线上唯一会花钱的一步 (免费期是唯一会撞限流的一步), 所以它的每一条规矩回答的都是
"什么时候不打这个电话": 已经写完的不问、块数对不上的不收、连着失败就收手、一个字都没写上的那
一趟不算写过. 最后一条最像纸面上的事, 但它决定日志与页面说的是不是同一件事.

模型是假的 —— 这一层测的是编排, 不是模型写不写得出中文. 真模型由真机那一轮验.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from app.feed import analysis, htmlbody, views, writing
from app.feed.sources import snapshot
from app.feed.sources import store as source_store
from app.store import db

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)

BUDGET = {"maxItems": 40, "concurrency": 2, "timeoutSeconds": 5,
          "retryHours": 24, "maxFailures": 3}

NAMES = {"rss:a": "Example Wire"}

#: 两块正文: 翻译那一档的"块数一对一"要有一个能对错的东西, 一块是测不出这件事的.
BODY = "<p>Nova bridge drained for $12M.</p><p>More than 4,000 wallets were affected.</p>"
BLOCKS = ["Nova bridge drained for $12M.", "More than 4,000 wallets were affected."]
#: 两块都译上了的答案.
TRANSLATED = {"t": ["Nova 桥被掏空 1200 万美元。", "四千多个钱包受影响。"]}


@pytest.fixture(autouse=True)
def roster(isolated_db):
    """名录要有: 页面印来源名走的是它, 而 `import_roster` 之后缓存会重读一次表."""
    source_store.import_roster()


@pytest.fixture(autouse=True)
def one_model_only(monkeypatch):
    """这一份测的是理解/翻译/熔断的编排, 评分档由 test_feed_selection.py 单独测.

    把独立评分次数按到 0, 等于告诉写作站「这台机器只配了一个模型、不评精选分」: 分数没了,
    剩下两档照旧按规则分走 (writing.mean_score 的兜底). 不这么做的话, 这一份里每条夹具都要
    额外造两个评分答案, 而它想断言的东西一个字都没变.
    """
    monkeypatch.setattr(writing, "SCORE_CALLS", 0)
    monkeypatch.setattr(writing, "STRUCTURE_CALLS", 0)
    # 预筛档也在同一句里按掉: 这一份测的是理解/翻译/熔断的编排, 每条夹具都补一个预筛答案
    # 不会让它想断言的东西变一个字. 预筛自己有 test_feed_prefilter.py。
    monkeypatch.setattr(writing, "PREFILTER_CALLS", 0)


def _row(external_id: str, *, title: str = "Nova bridge drained", body: str = BODY,
         source_id: str = "rss:a", kind: str = "rss", quoted: dict | None = None) -> dict:
    return {
        "source_id": source_id,
        "external_id": external_id,
        "source_kind": kind,
        "quoted": quoted or {},
        "identity_key": "other|",
        "title": title,
        "url": "https://a/%s" % external_id,
        "author": "",
        "body": body,
        "media": [],
        "published_at": (NOW - timedelta(minutes=5)).isoformat(),
    }


def _after_a_failed_page_fetch(external_id: str, **kw) -> dict:
    """一条去过原文页、没读到正文的记录。

    写作站不打断还在等原文的那些 (AIHOT 的 `waitsForPage`)，所以想
    测"只有标题也要写"的那几条夹具, 得先把"读过了、没读到"这件事记上
    —— `detail_at` 就是它。实际跑起来时这一格由 `collect._fill_details` 写。
    """
    row = _row(external_id, body="", **kw)
    db.feed_items_put([row])
    db.feed_items_set_detail([{"source_id": row["source_id"], "external_id": external_id}],
                             tried_at=NOW.isoformat())
    return row


def _fake(monkeypatch, **answers):
    """把模型换成一个按 `purpose` 翻牌子的假人.

    值是常量, 或者一个读 messages 的函数 —— 分批与"某一块译不上"这些情形要能按**这一次问了
    什么**给答案, 而按 purpose 分牌子的写法本身也是断言的一部分: "这一站问了什么"是编排.
    """
    seen: list[str] = []
    asked: list[list[str]] = []

    def call(messages, **kw):
        seen.append(kw["purpose"])
        answer = answers[kw["purpose"]]
        if kw["purpose"] == writing.PURPOSE_TRANSLATE:
            asked.append(json.loads(messages[1]["content"])["segments"])
        if callable(answer):
            return answer(messages)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(writing.llm, "complete_json", call)
    return seen, asked


def _written(external_id: str = "a-1", source_id: str = "rss:a") -> dict:
    rows = snapshot.read_written_window(NOW, 1)
    assert (source_id, external_id) in rows, "这一行没有被模型写过"
    return rows[(source_id, external_id)]


def _blocks_body(count: int) -> str:
    """若干块英文正文. 译不译由正文的语言说了算, 所以这一档的夹具得是英文 (zh_source)."""
    return "".join("<p>Block %d: this is a paragraph long enough to count as one block.</p>" % index
                   for index in range(1, count + 1))


UNDERSTOOD = {"title_zh": "Nova 桥被掏空 1200 万美元", "summary": "Nova 桥被掏空, 四千多个钱包受影响.",
              "reason": "同类跨链桥通常会被一起重新定价."}


@pytest.fixture
def curated(monkeypatch):
    """把精选线按住. 这一档问不问模型由分数线说了算, 而"分数怎么算"是 analysis 自己的测试."""
    monkeypatch.setattr(analysis, "CURATED_MIN_SCORE", 0)


def test_the_prompt_version_is_its_content_not_a_serial_number():
    # DESIGN.md 9.6: 手写的序号会在"人改了文件、忘了改号"的那一版上冒充上一版.
    assert writing.version("understand.md") == writing.version("understand.md")
    assert writing.version("understand.md") != writing.version("translate-body.md")
    assert len(writing.version("understand.md").split("@")[1]) == 8


def test_a_draft_without_a_title_or_a_block_list_is_not_an_answer():
    with pytest.raises(Exception):
        writing.UnderstandDraft.model_validate({"summary": "只有摘要, 没有标题."})
    with pytest.raises(Exception):
        writing.TranslateDraft.model_validate({"sections": [{"body": "旧形状"}]})


def test_the_understanding_tier_writes_three_fields(isolated_db, monkeypatch):
    seen, _ = _fake(monkeypatch, **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD})
    _after_a_failed_page_fetch("a-1")
    report = writing.run(NOW, names=NAMES, budget=BUDGET)

    # 没有正文 → 翻译那一档没有可译的东西, 但这一条该由模型写的部分已经写完了, 所以是 `full`.
    assert seen == [writing.PURPOSE_UNDERSTAND]
    assert (report["asked"], report["wrote"], report["full"], report["failed"]) == (1, 1, 1, 0)

    row = _written()
    assert row["title_zh"] == UNDERSTOOD["title_zh"]
    assert row["summary_llm"] == UNDERSTOOD["summary"]
    assert row["reason_llm"] == UNDERSTOOD["reason"]
    assert row["llm_stage"] == "full"
    assert row["prompt_version"] == writing.understand_version()
    assert row["llm_at"]


def test_the_material_offers_the_entities_it_recognises():
    """候选实体是给模型的白名单 (AIHOT 的 `subjects.filter((s) => s in ENTITIES)`), 所以材料里
    必须有这一行 —— 少了它, 模型写的 id 全都会被词典筛掉, 而那是静默的。"""
    row = _row("e-1", title="Ethereum Foundation launches zkAPI")
    text = writing.material(row, {"rss:a": "Alpha"})
    assert "候选实体: ethereum, ethereum-foundation" in text

    quiet = _row("e-2", title="A quiet week for nobody in particular",
                 body="<p>Nothing happened.</p>")
    assert "候选实体: 无" in writing.material(quiet, {"rss:a": "Alpha"})


def test_an_item_below_the_line_gets_a_title_but_never_a_translation(isolated_db, monkeypatch):
    seen, asked = _fake(monkeypatch, **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD,
                                       writing.PURPOSE_TRANSLATE: TRANSLATED})
    monkeypatch.setattr(analysis, "CURATED_MIN_SCORE", 101)   # 线在 101: 没有条目够得着
    db.feed_items_put([_row("a-1")])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    assert seen == [writing.PURPOSE_UNDERSTAND] and asked == []
    row = _written()
    assert row["title_zh"] == UNDERSTOOD["title_zh"]
    assert not row["body_zh"] and not row["body_complete"]
    assert row["llm_stage"] == "full"


def test_the_translate_tier_rewrites_the_body_block_by_block(isolated_db, monkeypatch, curated):
    seen, asked = _fake(monkeypatch, **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD,
                                       writing.PURPOSE_TRANSLATE: TRANSLATED})
    db.feed_items_put([_row("a-1")])
    report = writing.run(NOW, names=NAMES, budget=BUDGET)

    assert seen == [writing.PURPOSE_UNDERSTAND, writing.PURPOSE_TRANSLATE]
    assert asked == [BLOCKS]   # 一次问的是**全部块**, 按原文顺序
    assert (report["wrote"], report["full"], report["failed"]) == (1, 1, 0)

    row = _written()
    # 结构是原文的, 字是模型的: 两个 `<p>` 都在, 只有里面的字换了.
    assert htmlbody.blocks(row["body_zh"]) == TRANSLATED["t"]
    assert row["body_zh"].count("<p>") == 2 and row["body_zh"].count("</p>") == 2
    assert row["body_complete"] == 1
    assert row["prompt_version"] == "%s,%s" % (writing.understand_version(),
                                               writing.version("translate-body.md"))


def test_a_translation_that_changed_the_block_count_is_refused(isolated_db, monkeypatch, curated):
    # 一块正文, 回来两块: 对不上就是没译成. 块多了也一样 —— 少一块、多一块都不是同一件事.
    _fake(monkeypatch, **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD,
                          writing.PURPOSE_TRANSLATE: {"t": ["第一块", "第二块"]}})
    db.feed_items_put([_row("a-1", body="<p>Only one block here, and it is the whole body.</p>")])
    report = writing.run(NOW, names=NAMES, budget=BUDGET)

    assert (report["wrote"], report["full"], report["failed"]) == (1, 0, 1)
    row = _written()
    # 理解档的答案照写: 一条的失败不该把上一档白写一遍, 下一轮从翻译接着走.
    assert row["title_zh"] == UNDERSTOOD["title_zh"]
    assert row["llm_stage"] == "understand"
    assert not row["body_zh"]
    assert row["llm_at"]


def test_a_batch_that_does_not_line_up_is_asked_again_in_halves(isolated_db, monkeypatch, curated):
    wrote = _blocks_body(4)
    parts = htmlbody.blocks(wrote)

    def answer(messages):
        segments = json.loads(messages[1]["content"])["segments"]
        # 四块一起问回来三条 (对不上); 按块问的那几次才对得上 —— 对半重问该把好的那半救回来.
        return {"t": ["译好的 " + text for text in segments[:3]]}

    _fake(monkeypatch, **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD, writing.PURPOSE_TRANSLATE: answer})
    db.feed_items_put([_row("a-1", body=wrote)])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    row = _written()
    assert htmlbody.blocks(row["body_zh"]) == ["译好的 " + text for text in parts]
    assert row["body_complete"] == 1


def test_a_block_that_never_comes_back_keeps_the_source_text_and_is_flagged(isolated_db, monkeypatch, curated):
    def answer(messages):
        segments = json.loads(messages[1]["content"])["segments"]
        if any("4,000" in text for text in segments):
            return {"t": []}                 # 带人数的那一块怎么也译不上 (块数对不上 = 没译成)
        return {"t": ["译好的 " + text for text in segments]}

    _fake(monkeypatch, **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD, writing.PURPOSE_TRANSLATE: answer})
    db.feed_items_put([_row("a-1")])
    report = writing.run(NOW, names=NAMES, budget=BUDGET)

    row = _written()
    # 译上的那一块换成中文, 没译上的那一块留着原文 —— 存半篇比存空好.
    assert htmlbody.blocks(row["body_zh"]) == ["译好的 " + BLOCKS[0], BLOCKS[1]]
    # 但说半篇是整篇比存半篇坏: 这一格是 false, 页面因此在正文上加一行提示.
    assert row["body_complete"] == 0
    # 没译完就停在上一档: 过了 `retryHours` 这一轮还会来把缺的那块补上.
    assert row["llm_stage"] == "understand"
    assert (report["wrote"], report["full"], report["failed"]) == (1, 0, 0)


def test_a_long_body_is_asked_in_batches_that_fit_together(isolated_db, monkeypatch, curated):
    wrote = _blocks_body(4)
    parts = htmlbody.blocks(wrote)
    monkeypatch.setattr(writing, "BATCH_CHARS", sum(len(part) for part in parts[:2]))

    def answer(messages):
        segments = json.loads(messages[1]["content"])["segments"]
        return {"t": ["译好的 " + text for text in segments]}

    _, asked = _fake(monkeypatch, **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD,
                                     writing.PURPOSE_TRANSLATE: answer})
    db.feed_items_put([_row("a-1", body=wrote)])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    assert [len(batch) for batch in asked] == [2, 2]      # 一趟装不完就分两趟, 一趟就是一次调用
    assert [text for batch in asked for text in batch] == parts   # 合起来还是原文那一串, 顺序没变
    assert htmlbody.blocks(_written()["body_zh"]) == ["译好的 " + text for text in parts]


def test_a_chinese_source_is_never_translated(isolated_db, monkeypatch, curated):
    seen, asked = _fake(monkeypatch, **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD,
                                       writing.PURPOSE_TRANSLATE: TRANSLATED})
    db.feed_items_put([_row("a-1", body="<p>Nova 桥被掏空了一千两百万美元, 四千多个钱包受影响。</p>",
                            source_id="cn:panews")])
    writing.run(NOW, names={**NAMES, "cn:panews": "PANews"}, budget=BUDGET)

    # 名录里写着 `language: zh` 的源, 翻译档一步都不走 —— 译一遍等于把原文抄一遍, 还让页面多
    # 一个切过去没有变化的"原文"开关.
    assert seen == [writing.PURPOSE_UNDERSTAND] and asked == []
    assert not _written(source_id="cn:panews")["body_zh"]


def test_the_source_language_decides_and_the_body_is_the_fallback():
    # 先信源的声明……
    assert writing.zh_source("zh", "This paragraph is in English.")
    assert not writing.zh_source("en", "这一段是中文, 但源自己写着 en。")
    # ……声明缺席或写着别的, 才去数正文里的汉字.
    assert writing.zh_source("", "这一段是中文。")
    assert not writing.zh_source("", "This one is not.")


def test_it_stops_asking_after_too_many_failures_in_a_row(isolated_db, monkeypatch):
    _fake(monkeypatch, **{writing.PURPOSE_UNDERSTAND: writing.llm.LLMUnavailable("503")})
    db.feed_items_put([_row("a-%d" % n) for n in range(5)])
    report = writing.run(NOW, names=NAMES, budget={**BUDGET, "concurrency": 1, "maxFailures": 2})

    assert report["asked"] == 5
    assert report["stopped"] is True and report["failed"] == 2   # 连着两次就收手
    # 一个字都没写上: 失败的补丁只带 `llm_at`, 不该数进 `wrote`, 页面上也没变.
    assert report["wrote"] == 0 and snapshot.read_written_window(NOW, 1) == {}
    # 没被问过的那三条还排在队首 (llm_at 还是空的), 下一轮先问它们.
    assert len(db.feed_items_pending_llm(NOW.isoformat(timespec="seconds"), limit=10)) == 3


def test_an_entry_nobody_wrote_is_not_printed_while_the_writing_station_is_on(isolated_db, monkeypatch):
    """发布门槛 (AIHOT 的 waits): 还没写出中文标题与摘要的条目先不出现在页面上.

    写作站关掉的那一档不能有这个门槛 —— 没有模型的机器上, 信息流会安静地变成空页.
    """
    db.feed_items_put([_row("a-1")])
    assert [row for day in views.items_view(NOW, scope="all")["days"] for row in day["items"]]

    monkeypatch.setattr(writing, "enabled", lambda: True)
    assert views.items_view(NOW, scope="all")["shown"] == 0

    _fake(monkeypatch, **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD})
    writing.run(NOW, names=NAMES, budget=BUDGET)
    assert views.items_view(NOW, scope="all")["shown"] == 1


def test_the_page_prints_the_models_words_and_keeps_the_source_title(isolated_db, monkeypatch, curated):
    _fake(monkeypatch, **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD,
                          writing.PURPOSE_TRANSLATE: TRANSLATED})
    db.feed_items_put([_row("a-1")])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    view = views.items_view(NOW, scope="all")
    rows = [row for day in view["days"] for row in day["items"]]
    row = next(row for row in rows if row["id"] == "rss:a:a-1")
    # 标题是模型写的中文, 源自己那条另给一格 —— 页面据此决定给不给「原文标题」那一行.
    assert row["written"] is True
    assert row["title"] == UNDERSTOOD["title_zh"]
    assert row["title_original"] == "Nova bridge drained"
    assert row["summary"] == UNDERSTOOD["summary"]

    item, story = views.find_item(NOW, "rss:a:a-1")
    detail = views.item_detail(item, story, NOW)
    assert htmlbody.blocks(detail["body_zh"]) == TRANSLATED["t"]
    assert detail["body_complete"] is True
    assert detail["body_html"] == BODY
    assert detail["title_original"] == "Nova bridge drained"


def test_an_x_post_prints_no_original_title_line(isolated_db, monkeypatch, curated):
    """X 帖的「源标题」就是那条推文本身: 中文标题底下再印一行原文只是噪音, 所以那一格给空串.

    这一条与语言无关 —— 它也是英文源、也译了中文, 唯独缺的只是那一行 (views.item_payload).
    """
    _fake(monkeypatch, **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD,
                          writing.PURPOSE_TRANSLATE: TRANSLATED})
    db.feed_items_put([_row("t-1", source_id="x:a", kind="x")])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    rows = [row for day in views.items_view(NOW, scope="all")["days"] for row in day["items"]]
    row = next(row for row in rows if row["id"] == "x:a:t-1")
    assert row["written"] is True
    assert row["title"] == UNDERSTOOD["title_zh"]
    assert row["title_original"] == ""

    item, story = views.find_item(NOW, "x:a:t-1")
    assert views.item_detail(item, story, NOW)["title_original"] == ""


#: 一条推文的正文可能不止一块 (空行分段), 而推文那一档要**整条一块**问 —— 分块是给长文准备的.
TWEET = ("Weekend plans? Here's one:\n\nRead about Base Cobalt https://t.co/o8myoAIC2j — the "
         "upgrade brings composite policies, scheduled multiplier updates and a new seizeWithMemo "
         "path for builders.")
TWEET_ZH = "周末安排？这里有一个：读一读 Base Cobalt https://t.co/o8myoAIC2j —— 这次升级带来组合策略、定期倍数更新，以及一条新的 seizeWithMemo 路径。"


def test_a_tweet_is_asked_as_one_block(isolated_db, monkeypatch, curated):
    """推文整条一块问: 按块问会把它切成半句话, 而译文的地址契约是那一块自己的位置."""
    _, asked = _fake(monkeypatch, **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD,
                                     writing.PURPOSE_TRANSLATE: {"t": [TWEET_ZH]}})
    db.feed_items_put([_row("t-1", source_id="x:a", kind="x", body=htmlbody.from_plain(TWEET))])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    assert asked == [[htmlbody.to_text(htmlbody.from_plain(TWEET))]]     # 一块进, 不是两块
    row = _written("t-1", "x:a")
    assert row["body_zh"] == "<p>%s</p>" % TWEET_ZH
    assert row["body_complete"] == 1


def test_a_short_tweet_is_not_translated(isolated_db, monkeypatch, curated):
    """短推的意思已经由中文标题与摘要承载: 再译一遍只是把同一句话说两遍 (DESIGN.md 10.11)."""
    seen, _ = _fake(monkeypatch, **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD})
    db.feed_items_put([_row("t-2", source_id="x:a", kind="x",
                            body=htmlbody.from_plain("Weekend plans? https://t.co/o8myoAIC2j"))])
    writing.run(NOW, names=NAMES, budget=BUDGET)

    assert seen == [writing.PURPOSE_UNDERSTAND]      # 翻译档没有被问
    row = _written("t-2", "x:a")
    assert not (row["body_zh"] or "")
    assert row["llm_stage"] == "full"


def test_the_short_post_line_ignores_the_link():
    """一条只有链接的推文, 长度全是那个链接 —— 所以先摘网址再数 (AIHOT 的 X_MIN_CHARS)."""
    assert writing.short_post("Weekend plans? https://t.co/o8myoAIC2j") is True
    assert writing.short_post("x" * writing.X_MIN_CHARS) is False
    assert writing.short_post("https://t.co/%s" % ("y" * 200)) is True


def _pending(external_id: str) -> dict:
    """写作站读一行时看到的那份 (与 `writing.run` 同一个问法)."""
    later = "2999-01-01T00:00:00+00:00"
    return next(row for row in db.feed_items_pending_llm(later, 10)
                if row["external_id"] == external_id)


def test_a_quoted_post_reaches_the_understanding_material(isolated_db):
    """引用推文既不进正文也不单独译: 它只作为背景进材料, 摘要要把它的关键点整合进去 (10.11).

    走真库: 采集器把引用交给 `feed_items_put`, 写作站再从库里读回来 —— 中间少一环, 这一条就会
    安静地只剩正文, 而摘要永远不知道有引用这回事.
    """
    quoted = {"text": "The repo went private", "handle": "someone",
              "url": "https://x.com/someone/status/9"}
    db.feed_items_put([_row("t-3", source_id="x:a", kind="x", quoted=quoted)])
    row = _pending("t-3")
    assert writing.quoted_block(row) == "引用推文（@someone）:\nThe repo went private"
    text = writing.material(row, {"x:a": "Base"})
    assert "引用推文（@someone）" in text and "The repo went private" in text

    # 没有引用的那些条目, 材料里一个字都不多.
    db.feed_items_put([_row("t-4", source_id="x:a", kind="x")])
    assert "引用推文" not in writing.material(_pending("t-4"), {"x:a": "Base"})


#: 提示词末尾那个 ```json 例子与校验它的模型, 是同一件事写在两个文件里. 例子多一个键、少一个
#: 键, 模型就在被要求给校验器不收的东西 —— 而那一条会安静地失败 (test_prompts.py 同一个理由).
@pytest.mark.parametrize("name,model", [("understand.md", writing.UnderstandDraft),
                                        ("translate-body.md", writing.TranslateDraft),
                                        ("translate-post.md", writing.TranslateDraft)])
def test_the_prompt_example_matches_the_model_that_validates_it(name, model):
    block = re.search(r"```json\n(.*?)```", writing.read_prompt(name, **writing._structure_fillings()), re.S)
    assert block, "%s has no ```json example" % name
    example = json.loads(block.group(1))
    assert set(example) == set(model.model_fields), \
        "%s: example vs %s: %s" % (name, model.__name__,
                                   sorted(set(example) ^ set(model.model_fields)))
def test_a_changed_prompt_puts_written_rows_back_in_the_queue(isolated_db, monkeypatch):
    """改了提示词就是一个新版本, 已经写完的行要重问一遍.

    提示词版本是内容哈希, 所以"人改了文件"这件事只能由版本说了算. 少了这一条, 改提示词只影响
    此后新抓到的条目, 库里已经写过的那几千行会一直用旧口径冒充新口径 —— 而这是完全静默的.
    """
    _fake(monkeypatch, **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD})
    _after_a_failed_page_fetch("a-1")
    writing.run(NOW, names=NAMES, budget=BUDGET)

    same = writing.understand_version()
    assert db.feed_items_pending_llm(NOW.isoformat(), version=same) == []
    assert len(db.feed_items_pending_llm(NOW.isoformat(), version="deadbeef")) == 1


def test_a_failed_row_waits_out_its_retry_hours(isolated_db, monkeypatch):
    """失败过的行不是"这一轮再试", 而是推后 `retryHours` —— 限流时硬试只是把额度打光."""
    from app.llm.client import LLMUnavailable

    seen, _ = _fake(monkeypatch, **{writing.PURPOSE_UNDERSTAND: LLMUnavailable("no model")})
    _after_a_failed_page_fetch("a-1")
    writing.run(NOW, names=NAMES, budget=BUDGET)

    assert seen == [writing.PURPOSE_UNDERSTAND]
    # 下一轮不算到它：`llm_at` 要再走满 retryHours（24 小时）。
    soon = (NOW + timedelta(hours=1) - timedelta(hours=24)).isoformat()
    assert db.feed_items_pending_llm(soon, version="deadbeef") == []
    # 过了 24 小时，它照样排在队里。
    later = (NOW + timedelta(hours=25) - timedelta(hours=24)).isoformat()
    assert len(db.feed_items_pending_llm(later, version="deadbeef")) == 1
def test_a_written_row_with_an_old_prompt_is_understood_again(isolated_db, monkeypatch):
    """写到 `full` 的旧行也要能追上版本.

    只比 `llm_stage` 是不够的：那样这一行每轮都会被排进队列，每轮只写回一个空的 `full` 补丁，
    永远追不上版本 —— 白花钱，而且完全静默.
    """
    _fake(monkeypatch, **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD})
    _after_a_failed_page_fetch("a-1")
    db.feed_items_write_llm([{"source_id": "rss:a", "external_id": "a-1",
                              "llm_stage": "full", "prompt_version": "understand.md@old",
                              "llm_at": NOW.isoformat()}])

    report = writing.run(NOW, names=NAMES, budget=BUDGET)

    assert (report["asked"], report["wrote"]) == (1, 1)
    assert _written()["prompt_version"] == writing.understand_version()
    assert db.feed_items_pending_llm(NOW.isoformat(),
                                    version=writing.understand_version()) == []


def test_a_row_that_is_still_waiting_for_its_page_is_not_asked(isolated_db, monkeypatch):
    """材料还在等原文时先不问模型 (AIHOT 的 `waitsForPage` / `needsBody`)。

    一条只有标题的 RSS 记录, 原文几分钟后就会到; 现在问它就是在标题上写摘要 ——
    而原文一到, 这一份结论就过期了: 同一件事花两遍钱。所以先等: 读过原文、
    或读失败, 再问。
    """
    seen, _ = _fake(monkeypatch, **{writing.PURPOSE_UNDERSTAND: UNDERSTOOD})
    db.feed_items_put([_row("a-1", body="")])
    report = writing.run(NOW, names=NAMES, budget=BUDGET)
    assert seen == []
    assert (report["asked"], report["wrote"]) == (0, 0)

    # 推文没有"原文页"可等: 它现在就该被问.
    db.feed_items_put([_row("t-1", source_id="x:a", kind="x", body="")])
    report = writing.run(NOW, names=NAMES, budget=BUDGET)
    assert seen == [writing.PURPOSE_UNDERSTAND]
    assert report["asked"] == 1
