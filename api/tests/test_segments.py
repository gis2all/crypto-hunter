"""Stored report markup becomes safe display segments."""

from app.report.segments import segments


def _links(text):
    return [row for row in segments(text) if row["kind"] == "link"]


def test_http_and_https_anchors_stay_links():
    out = _links('<a href="https://example.com/a">site</a> '
                 '<a href="http://example.com">http</a>')
    assert [row["url"] for row in out] == [
        "https://example.com/a",
        "http://example.com/",
    ]


def test_dangerous_anchor_schemes_become_plain_text():
    for href in ("javascript:alert(1)", "JaVaScRiPt:alert(1)",
                 "data:text/html,<script>alert(1)</script>",
                 "javascript&#58;alert(1)"):
        out = segments('<a href="%s">click</a>' % href)
        assert out == [{"kind": "text", "text": "click"}]


def test_anchor_with_credentials_or_private_literal_is_not_clickable():
    for href in ("https://user:pass@example.com/", "http://127.0.0.1:8787/"):
        out = segments('<a href="%s">click</a>' % href)
        assert out == [{"kind": "text", "text": "click"}]
