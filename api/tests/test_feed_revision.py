"""内容身份与版本：正文真改了才重算 (AIHOT 的 `contentHash` / `revision` / `article_revisions`)。

这一份测的是 upsert 的四条"不算改版"规矩，以及改版之后模型写下的那些字要重新排队 ——
少了任何一条，结果都是完全静默的：要么每读一次就重花一遍钱，要么正文换了而结论还是旧的。
"""

from __future__ import annotations

from app.feed import htmlbody, materials
from app.store import db

NOW = "2026-10-05T09:00:00+00:00"


def _row(**over) -> dict:
    row = {
        "source_id": "rss:a",
        "external_id": "a-1",
        "source_kind": "rss",
        "identity_key": "hack|nova",
        "title": "Nova bridge drained",
        "url": "https://example.test/a-1",
        "author": "",
        "body": "<p>Nova bridge drained for $12M.</p>",
        "excerpt": "",
        "media": [],
        "quoted": {},
        "published_at": NOW,
    }
    row.update(over)
    return row


def _stored() -> dict:
    return db.feed_items_for("hack|nova")[0]


def _revisions() -> list[dict]:
    conn = db.connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT revision, content_hash, body_text FROM feed_item_revisions"
            " WHERE source_id = ? AND external_id = ? ORDER BY revision", ("rss:a", "a-1"))]
    finally:
        conn.close()


def test_a_new_row_starts_at_revision_one_with_the_hash_of_its_text(isolated_db):
    db.feed_items_put([_row()])
    row = _stored()
    assert row["revision"] == 1
    # 哈希是对**文字**算的，不是对 HTML：同一句话换个标签写不是改版。
    assert row["content_hash"] == materials.content_hash(
        "Nova bridge drained", htmlbody.to_text("<p>Nova bridge drained for $12M.</p>"), "")
    assert len(row["content_hash"]) == 64
    assert _revisions() == [{"revision": 1, "content_hash": row["content_hash"],
                             "body_text": "Nova bridge drained for $12M."}]


def test_reporting_the_same_content_again_is_not_a_new_revision(isolated_db):
    db.feed_items_put([_row()])
    digest = _stored()["content_hash"]
    db.feed_items_put([_row(url="https://example.test/a-1?utm=x")])
    assert _stored()["revision"] == 1
    assert _stored()["content_hash"] == digest
    assert len(_revisions()) == 1


def test_whitespace_and_lost_characters_are_not_new_revisions(isolated_db):
    """两种噪声，两种都得忍：重排版的空白，与 feed 每读一次糊掉的几个字。"""
    db.feed_items_put([_row()])
    db.feed_items_put([_row(body="<p>Nova  bridge drained\n   for $12M.</p>")])
    assert _stored()["revision"] == 1
    # U+FFFD 代表"这里本来有一个字" —— 它是替换, 不是插入, 所以长度不变。
    db.feed_items_put([_row(body="<p>Nova bridge drained for $\ufffd2M.</p>")])
    assert _stored()["revision"] == 1


def test_a_real_edit_is_a_new_revision_and_the_old_one_is_kept(isolated_db):
    db.feed_items_put([_row()])
    db.feed_items_put([_row(body="<p>Nova bridge drained for $19M.</p>")])
    row = _stored()
    assert row["revision"] == 2
    assert [item["revision"] for item in _revisions()] == [1, 2]
    assert _revisions()[0]["body_text"] == "Nova bridge drained for $12M."


def test_a_rendering_seen_before_is_not_a_new_revision(isolated_db):
    """列表在两种写法之间来回换、页面轮换推广位：都是同一篇文章。"""
    db.feed_items_put([_row()])
    db.feed_items_put([_row(body="<p>Bridge drained; Nova lost $12M.</p>")])
    assert _stored()["revision"] == 2
    db.feed_items_put([_row(body="<p>Nova bridge drained for $12M.</p>")])
    assert _stored()["revision"] == 2
    assert len(_revisions()) == 2


def test_a_report_without_a_body_does_not_wipe_the_stored_one(isolated_db):
    """一次列表更新没带正文, 不该把上一轮补到的正文抹掉 (AIHOT 的 coalesce)。"""
    db.feed_items_put([_row(body="")])
    db.feed_items_set_detail([{"source_id": "rss:a", "external_id": "a-1",
                               "body": "<p>Read from the page.</p>"}], tried_at=NOW)
    assert _stored()["body"] == "<p>Read from the page.</p>"
    db.feed_items_put([_row(body="")])
    assert _stored()["body"] == "<p>Read from the page.</p>"
    assert _stored()["revision"] == 2


def test_fetching_the_page_is_a_new_revision(isolated_db):
    """"补到一处更长的正文"就是新的一版 (AIHOT 在 `extract` 里做同一件事)。"""
    db.feed_items_put([_row(body="<p>One line from the feed.</p>")])
    assert _stored()["revision"] == 1
    db.feed_items_set_detail([{"source_id": "rss:a", "external_id": "a-1",
                               "body": "<p>Read from the page, at length.</p>"}], tried_at=NOW)
    assert _stored()["revision"] == 2
    assert [item["revision"] for item in _revisions()] == [1, 2]


def test_a_new_revision_puts_the_written_words_back_in_the_queue(isolated_db):
    """写过的字是接着某一版正文写的; 正文改版之后它们过期, 下一轮重写 (AIHOT 的 `stale`)。"""
    db.feed_items_put([_row()])
    db.feed_items_write_llm([{"source_id": "rss:a", "external_id": "a-1",
                              "llm_stage": "full", "prompt_version": "understand.md@now",
                              "llm_at": NOW}])
    assert _stored()["llm_revision"] == 1
    assert db.feed_items_pending_llm(NOW, versions=["understand.md@now"]) == []

    db.feed_items_put([_row(body="<p>Nova bridge drained for $19M.</p>")])
    # 结论还在 (页面还能看到), 只是它说的是上一版.
    assert _stored()["llm_revision"] == 1
    assert _stored()["llm_stage"] == "full"
    assert len(db.feed_items_pending_llm(NOW, versions=["understand.md@now"])) == 1


def test_a_library_without_a_hash_baseline_is_not_sent_back_to_paid_analysis(isolated_db):
    """库里还没有哈希基准时 (导入的历史、一次升级), 第一次报告只记基准。

    不这么做, 一次导入会把源仍然列着的每一篇都算成"正文改过", 而一整库重跑
    一遍付费档是一笔没人要求过的开销。
    """
    db.feed_items_put([_row()])
    db.feed_items_write_llm([{"source_id": "rss:a", "external_id": "a-1",
                              "llm_stage": "full", "prompt_version": "understand.md@now",
                              "llm_at": NOW}])
    conn = db.connect()
    try:
        conn.execute("UPDATE feed_items SET content_hash = ''")
    finally:
        conn.close()
    assert db.feed_items_pending_llm(NOW, versions=["understand.md@now"]) == []

    db.feed_items_put([_row()])
    row = _stored()
    assert row["revision"] == 1
    assert row["llm_revision"] == 1
    assert row["content_hash"] == materials.content_hash(
        "Nova bridge drained", "Nova bridge drained for $12M.", "")
    assert db.feed_items_pending_llm(NOW, versions=["understand.md@now"]) == []
