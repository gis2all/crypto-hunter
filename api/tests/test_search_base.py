"""The search layer's pure parts: what a hit is, and which URL it really is.

Nothing here talks to a network. What is worth pinning down is the cleaning - the same
address arrives bare, wrapped in markdown, and with a full stop glued to its tail, and the
dedupe downstream only works if all three become one string.
"""

from __future__ import annotations

import pytest

from app.search.base import SearchHit, handle_of, kind_of, links_in, normalize_url


@pytest.mark.parametrize("raw, expected", [
    ("https://x.com/arcstudio_ai", "https://x.com/arcstudio_ai"),
    ("http://X.COM/ArcStudio_AI", "http://x.com/ArcStudio_AI"),
    ("x.com/arcstudio_ai", "https://x.com/arcstudio_ai"),
    ("https://www.arcstudio.fun/", "https://arcstudio.fun/"),
    ("https://x.com/arcstudio_ai?s=20", "https://x.com/arcstudio_ai"),
    ("https://x.com/arcstudio_ai?utm_source=telegram&ref=abc", "https://x.com/arcstudio_ai"),
    ("https://arcstudio.fun/recast#pricing", "https://arcstudio.fun/recast"),
    ("https://arcstudio.fun.", "https://arcstudio.fun/"),
    ("<https://arcstudio.fun>", "https://arcstudio.fun/"),
    ("  https://arcstudio.fun/recast  ", "https://arcstudio.fun/recast"),
    ("[https://arcstudio.fun](https://arcstudio.fun)", None),
    ("not a url", None),
    ("mailto:hi@arcstudio.fun", None),
    ("https://localhost:8787", None),
    ("", None),
])
def test_a_url_is_cleaned_to_the_one_string_two_mentions_share(raw, expected):
    assert normalize_url(raw) == expected


def test_the_host_is_the_part_that_is_compared_case_insensitively():
    """X answers both cases for one account, so the host folds and the path does not."""
    assert normalize_url("https://X.com/ArcStudio_AI") == "https://x.com/ArcStudio_AI"


@pytest.mark.parametrize("url, expected", [
    ("https://x.com/arcstudio_ai", "x"),
    ("https://twitter.com/arcstudio_ai", "x"),
    ("https://github.com/solonlend/solonpad-skill", "github"),
    ("https://t.me/solonpad", "telegram"),
    ("https://arcstudio.fun", "web"),
])
def test_a_hit_is_labelled_by_the_family_that_reads_it(url, expected):
    assert kind_of(url) == expected


@pytest.mark.parametrize("url, expected", [
    ("https://x.com/arcstudio_ai", "arcstudio_ai"),
    ("https://x.com/arcstudio_ai/", "arcstudio_ai"),
    ("https://x.com/arcstudio_ai/status/2103469960081395851", "arcstudio_ai"),
    ("https://twitter.com/Solonlabs1", "Solonlabs1"),
    ("https://x.com/i/user/1138123621916798976", None),
    ("https://x.com/i/status/2103469960081395851", None),
    ("https://x.com/search?q=studio", None),
    ("https://x.com/", None),
    ("https://arcstudio.fun", None),
])
def test_only_a_profile_url_yields_a_handle(url, expected):
    """The numeric form is a real account page and deliberately not a handle: taking its
    first path segment would invent a candidate named i."""
    assert handle_of(url) == expected


def test_a_handle_that_is_not_a_handle_is_rejected():
    assert handle_of("https://x.com/this_handle_is_far_too_long") is None
    assert handle_of("https://x.com/arc-studio") is None


def test_prose_yields_its_links_with_the_labels_still_attached():
    text = ("Official: [@arcstudio_ai](https://x.com/arcstudio_ai) - site "
            "https://arcstudio.fun/recast. Also https://github.com/x/y")
    assert links_in(text) == [
        ("@arcstudio_ai", "https://x.com/arcstudio_ai"),
        ("", "https://arcstudio.fun/recast."),
        ("", "https://github.com/x/y"),
    ]


def test_a_hit_carries_what_the_engine_said_around_it():
    hit = SearchHit(url="https://x.com/a", engine="grok", kind="x", title="A", snippet="s")
    assert (hit.url, hit.engine, hit.kind, hit.title, hit.snippet) == (
        "https://x.com/a", "grok", "x", "A", "s")
    assert SearchHit(url="u", engine="e", kind="web").title == ""
