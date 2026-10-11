"""The link columns, where a handle is not a link.

RadarDEX and Tolly publish `Solonlabs1` and `solonpad.fun`; GMGN publishes `twitter_username`.
Printed as they arrive they are text shaped like a link - the reference implementation’s
社媒 line read `X [Solonlabs1]` next to a clickable 官网, one dead link and one live one in
the same line.
"""

from app.sources import gmgn
from app.sources.arc import radardex, tolly
from app.sources.base import social_url, web_url
from app.sources.robinhood import pez


def test_a_handle_becomes_the_host_that_serves_it():
    assert social_url("twitter", "Solonlabs1") == "https://x.com/Solonlabs1"
    assert social_url("twitter", "@Solonlabs1") == "https://x.com/Solonlabs1"
    assert social_url("telegram", "solonpad") == "https://t.me/solonpad"
    assert social_url("discord", "abc123") == "https://discord.gg/abc123"


def test_a_value_that_is_already_a_link_is_left_alone():
    assert social_url("twitter", "https://x.com/Solonlabs1") == "https://x.com/Solonlabs1"
    assert social_url("telegram", "t.me/solonpad") == "https://t.me/solonpad"


def test_an_empty_column_is_no_link_rather_than_an_empty_string():
    assert social_url("twitter", "") is None
    assert social_url("twitter", None) is None
    assert web_url("  ") is None


def test_a_bare_domain_gets_a_scheme():
    assert web_url("solonpad.fun") == "https://solonpad.fun"
    assert web_url("https://solonpad.fun/") == "https://solonpad.fun/"


def test_both_registries_publish_links_through_the_same_reader():
    row = {"twitter": "Solonlabs1", "telegram": "solonpad", "website": "solonpad.fun"}
    assert radardex._socials(row)["twitter"] == "https://x.com/Solonlabs1"
    assert radardex._websites(row) == ["https://solonpad.fun"]
    assert tolly._socials(row)["telegram"] == "https://t.me/solonpad"


def test_the_nested_column_is_unwrapped_by_the_caller():
    row = {"socials": {"twitter": "pezpad", "discord": "abc123", "website": "pezpad.fun"}}
    assert pez._socials(row)["discord"] == "https://discord.gg/abc123"
    assert pez._websites(row) == ["https://pezpad.fun"]
    assert pez._socials({}) == {}


def test_a_twitter_column_named_differently_still_feeds_the_same_kind():
    assert gmgn._socials({"twitter_username": "somedev", "telegram": "t.me/somedev"}) == {
        "twitter": "https://x.com/somedev",
        "telegram": "https://t.me/somedev",
    }
