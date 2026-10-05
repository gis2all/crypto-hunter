"""正文那层 HTML: 块级结构、行内标签、占位符协议 (AIHOT `sanitize.ts` + `translate.ts`).

这一份是纯函数测试 —— 不碰库、不碰模型. 它盯住两件事: 采集进来的标签被收成"一串平铺的块",
以及翻译时护住的行内标签必须**按记号对账** (少一个、多一个、重复一个都作废).

为什么值得单测: 这是整条链上唯一一处"丢一个字符就静默印错"的地方 —— 一个错位的链接缝进译文,
页面上看不出来.
"""

from __future__ import annotations

from app.feed import htmlbody


# ── 采集: 块与行内标签 ──────────────────────────────────────────────────────────


def test_sanitize_keeps_the_inline_tags_and_drops_the_script_subtree() -> None:
    body = htmlbody.sanitize(
        '<p>See <a href="https://x.test/a" title="t">this</a> and <strong>that</strong> '
        '<code class="language-py">x = 1</code></p><script>var a = 1;</script>')
    assert body == ('<p>See <a href="https://x.test/a" title="t">this</a> and '
                    '<strong>that</strong> <code class="language-py">x = 1</code></p>')


def test_sanitize_normalises_the_heading_levels() -> None:
    # `h1` 并进 `h2`、`h6` 并进 `h5` (AIHOT 的 `transformTags`): 页面上只有一个一级标题.
    assert htmlbody.sanitize("<h1>Big</h1><h6>Small</h6><p>body text here</p>") == \
        "<h2>Big</h2><h5>Small</h5><p>body text here</p>"


def test_sanitize_keeps_a_pre_block() -> None:
    # 整篇只有一段代码也算 HTML: 不认 `pre` 的话它会被当成一行纯文字.
    assert htmlbody.sanitize('<pre class="language-js"><code>let a = 1;</code></pre>') == \
        "<pre><code>let a = 1;</code></pre>"


def test_sanitize_drops_promotions_tracking_pixels_and_empty_anchors() -> None:
    body = htmlbody.sanitize(
        '<p>a</p><div class="msr-promo"><p>Buy now</p></div>'
        '<p>b <a href="https://x.test/a"></a></p>'
        '<p><img src="https://a/px.gif" width="1" height="1"></p>'
        '<p><img src="https://secure.adnxs.com/p.gif" width="10" height="10"></p>'
        '<p>c<img alt="no src"></p>')
    assert body == "<p>a</p><p>b</p><p>c</p>"


def test_sanitize_reads_the_lazy_image_address() -> None:
    # 懒加载站的 `src` 是占位图, 真地址在 `data-src` (AIHOT 的 `data-src || data-original || src`).
    body = htmlbody.sanitize('<p><img data-src="https://a/lazy.png" src="https://a/blank.gif" alt="z"></p>')
    assert body == '<p><img src="https://a/lazy.png" alt="z"></p>'


def test_blocks_are_plain_text_and_first_paragraph_has_no_tags() -> None:
    body = htmlbody.sanitize('<p>See <a href="https://x.test/a">this</a> now</p><h2>Next</h2>')
    assert htmlbody.blocks(body) == ["See this now", "Next"]
    assert htmlbody.first_paragraph(body) == "See this now"
    assert htmlbody.to_text(body) == "See this now Next"


def test_rewrite_html_keeps_the_structure() -> None:
    body = htmlbody.sanitize("<p>a</p><p>b</p>")
    assert htmlbody.rewrite_html(body, ["x", "y"]) == "<p>x</p><p>y</p>"
    # 纯文字那条老路仍旧转义: 换进来的字带 `&` 不该变成标签.
    assert htmlbody.rewrite(body, ["a & b", "y"]) == "<p>a &amp; b</p><p>y</p>"


# ── 采集: 图片那一格 ───────────────────────────────────────────────────────────


def test_images_read_the_size_and_stop_at_twelve() -> None:
    body = "".join('<p><img src="https://a/%d.png" width="%d" height="20"></p>' % (i, i)
                   for i in range(15))
    found = htmlbody.images(body)
    assert len(found) == 12
    assert found[0] == {"kind": "image", "url": "https://a/0.png", "width": 0, "height": 20}
    # 没有宽高的那一张是 None, 不是 0 —— 页面上"尺寸未知"与"零像素"是两件事.
    assert htmlbody.images('<p><img src="https://a/x.png"></p>')[0]["width"] is None


def test_media_urls_read_both_the_new_and_the_old_shape() -> None:
    # 新行存对象 (带宽高), 老行是纯 URL 串; 读到页面那一层都得是 URL.
    assert htmlbody.media_urls(["https://a/1.png", {"url": "https://a/2.png", "width": 3}]) == \
        ["https://a/1.png", "https://a/2.png"]


# ── 翻译: 占位符协议 ──────────────────────────────────────────────────────────


def test_shield_marks_media_code_and_links() -> None:
    guarded = htmlbody.shield('<a href="https://x.test/a">this</a> and <img src="https://a/b.png">')
    assert guarded.html == '<a id="L0">this</a> and \u27e60\u27e7'
    assert guarded.tokens == ('<img src="https://a/b.png">',)
    assert guarded.links == ('<a href="https://x.test/a">',)
    assert guarded.raw is True


def test_a_plain_block_is_handed_over_as_plain_text() -> None:
    guarded = htmlbody.shield("just text")
    assert (guarded.html, guarded.raw, guarded.tokens) == ("just text", False, ())


def test_a_picture_with_its_image_is_one_token() -> None:
    # 先切里面的 `img` 会让外层那个记号永远回不来: 容器排在内层前面.
    guarded = htmlbody.shield('<picture><img src="https://a/p.png"></picture>')
    assert guarded.tokens == ('<picture><img src="https://a/p.png"></picture>',)


def test_unshield_puts_the_media_and_the_link_back() -> None:
    guarded = htmlbody.shield('<a href="https://x.test/a">this</a> and <img src="https://a/b.png">')
    answer = guarded.html.replace("this", "THIS")
    assert htmlbody.unshield(answer, guarded) == \
        '<a href="https://x.test/a">THIS</a> and <img src="https://a/b.png">'


def test_a_lost_duplicated_or_invented_token_voids_the_block() -> None:
    guarded = htmlbody.shield('see <img src="https://a/b.png"> and <a href="https://x.test/a">x</a>')
    # 少一个记号 / 多一个记号 / 记号编号是编出来的: 三种都对不上, 整块作废.
    assert htmlbody.unshield("see and <a id=\"L0\">x</a>", guarded) is None
    assert htmlbody.unshield(guarded.html + " \u27e60\u27e7", guarded) is None
    assert htmlbody.unshield(guarded.html + " \u27e67\u27e7", guarded) is None


def test_a_lost_link_voids_the_block_and_a_repeated_one_too() -> None:
    guarded = htmlbody.shield('<a href="https://x.test/a">x</a>')
    assert htmlbody.unshield("no link at all", guarded) is None
    assert htmlbody.unshield('<a id="L0">x</a> <a id="L0">x</a>', guarded) is None
    # 模型可能给那个 `a` 添了别的属性: 只认 id, 其余的照旧换回原文的属性.
    assert htmlbody.unshield('<a id="L0" class="k">x</a>', guarded) == \
        '<a href="https://x.test/a">x</a>'


def test_restore_escapes_a_plain_answer_and_checks_a_shielded_one() -> None:
    assert htmlbody.restore(htmlbody.shield("t"), "A & B") == "A &amp; B"
    guarded = htmlbody.shield('<img src="https://a/b.png">')
    assert htmlbody.restore(guarded, "\u27e60\u27e7") == '<img src="https://a/b.png">'
    assert htmlbody.restore(guarded, "lost it") is None
    assert htmlbody.restore(guarded, "") is None