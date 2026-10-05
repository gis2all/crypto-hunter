"""分析输入层: 一次读完, 每个判定档各自渲染 —— 与 AIHOT 的 `editorial/input.ts` 对齐。

这一层存在的理由是一个上限只能是一件事: 库里存多少 (htmlbody.MAX_CHARS, 60k) 与喂给模型
多少 (本层各档自己的数) 必须分开, 否则抬存储上限就会把长文的模型输入一起抬三倍。所以这几条
测的都是**上限与字段**: 哪个档截到多少、评分档有没有漏出来源名、摘要档的文章与帖子是不是两把尺.
"""

from __future__ import annotations

from app.feed import input as feed_input

NAMES = {"rss:a": "Example Wire"}


def _row(**over) -> dict:
    row = {
        "source_id": "rss:a",
        "external_id": "a-1",
        "source_kind": "rss",
        "title": "Nova bridge drained",
        "url": "https://example.test/a-1",
        "author": "",
        "body": "<p>Nova bridge drained for $12M.</p>",
        "published_at": "2026-10-03T08:55:00+00:00",
        "media_json": "[]",
        "quoted_json": None,
        "detail_at": None,
    }
    row.update(over)
    return row


def test_load_reads_the_quoted_post_and_the_media() -> None:
    a = feed_input.load(_row(quoted_json='{"text": "The repo went private", "handle": "someone"}',
                             media_json='["https://img.test/1.png", "https://img.test/2.png"]'), NAMES)
    assert (a.quoted_handle, a.quoted_text) == ("someone", "The repo went private")
    assert feed_input.quoted_parts(_row(quoted_json='{"text": "hi", "handle": "h"}')) == ("h", "hi")
    assert a.media == ("https://img.test/1.png", "https://img.test/2.png")
    # 新行存的是对象 (带宽高, AIHOT 那一格), 老行是纯 URL 串: 读出来都是 URL.
    obj = feed_input.load(_row(media_json='[{"kind": "image", "url": "https://img.test/3.png", '
                                                '"width": 12, "height": null}]'), NAMES)
    assert obj.media == ("https://img.test/3.png",)
    assert a.source_name == "Example Wire"


def test_missing_evidence_is_only_the_title() -> None:
    assert feed_input.missing_evidence(feed_input.load(_row(body=""), NAMES)) is True
    assert feed_input.missing_evidence(feed_input.load(_row(), NAMES)) is False
    quoted = _row(body="", quoted_json='{"text": "the chart"}')
    assert feed_input.missing_evidence(feed_input.load(quoted, NAMES)) is False


def test_the_score_input_never_shows_the_source_name_or_tier() -> None:
    text = feed_input.score_input(feed_input.load(_row(), NAMES))
    assert "Example Wire" not in text and "分级" not in text
    assert text.startswith("请按系统规则评估")
    assert "【标题】\nNova bridge drained" in text
    assert "【完整正文】" in text


def test_the_score_input_falls_back_to_the_title_and_keeps_the_quoted_post() -> None:
    row = _row(body="", quoted_json='{"text": "the chart is fake", "handle": "someone"}')
    text = feed_input.score_input(feed_input.load(row, NAMES))
    assert "【完整正文】\n[引用 @someone]：the chart is fake" in text

    plain = feed_input.score_input(feed_input.load(_row(body=""), NAMES))
    assert plain.endswith("【完整正文】\nNova bridge drained")


def test_the_structure_material_is_capped_at_7000() -> None:
    text = feed_input.structure_material(feed_input.load(_row(body="<p>%s</p>" % ("A" * 7000 + "TAIL")), NAMES))
    assert "TAIL" not in text
    assert "<source>" in text and "</material>" in text
    assert "分级：" in text and "一手来源：" in text
    assert "原文链接：https://example.test/a-1" in text


def test_the_context_material_is_capped_at_60000_and_names_the_quality() -> None:
    text = feed_input.context_material(
        feed_input.load(_row(body="<p>%s</p>" % ("B" * 60_000 + "TAIL")), NAMES))
    assert "TAIL" not in text
    assert "材料质量: 完整正文（来自源自带的正文）" in text
    assert "原文链接: https://example.test/a-1" in text

    fetched = feed_input.context_material(
        feed_input.load(_row(body="<p>hi</p>", detail_at="2026-10-03T09:00:00+00:00"), NAMES))
    assert "材料质量: 完整正文（抓自原始网页）" in fetched


def test_the_summary_material_uses_two_rulers_and_clips_the_quoted_post() -> None:
    body = "<p>%s</p>" % ("C" * 7_000 + "TAIL")
    quoted = '{"text": "%s", "handle": "someone"}' % ("D" * 2_000 + "QTAIL")
    row = _row(body=body, quoted_json=quoted)
    article = feed_input.summary_material(feed_input.load(row, NAMES), post=False)
    post = feed_input.summary_material(feed_input.load(row, NAMES), post=True)
    assert "TAIL" not in article and "TAIL" not in post
    assert "QTAIL" not in article
    assert "（（" not in article
    # 文章那把尺比帖子长: 同一段材料, 文章给得多.
    assert len(article) > len(post)
