"""The Solana mint read: the two authorities that can still rug a holder.

Where the answer comes from, and where it deliberately does not, is in
`app/sources/solana/mint.py`. These tests guard the two properties that make it usable as
a hard drop: a completed read answers even when the answer is "nothing left", and
everything else stays 未判定 - which never rejects.
"""

import base64
from pathlib import Path

import yaml

from app.api.v1 import forms
from app.core import filters
from app.core.models import SafetyFacts
from app.sources.solana import mint

TEMPLATE = Path(__file__).resolve().parents[2] / "config" / "chains" / "solana.example.yaml"
BOTH = ["mint_authority", "freeze_authority"]


def _account(*, held: tuple[str, ...] = (), owner: str | None = None, size: int = 82):
    """A mint account payload as `getAccountInfo` hands it over."""
    raw = bytearray(size)
    for name in held:
        offset = mint.AUTHORITIES[name]
        raw[offset:offset + mint.OPTION_TAG] = (1).to_bytes(mint.OPTION_TAG, "little")
    return base64.b64encode(bytes(raw)).decode(), owner or mint.TOKEN_PROGRAMS[0]


def _perm_values(chain: dict) -> list[str]:
    built = forms.build(chain, {}, {})
    for group in built["blocks"][0]["groups"]:
        for field in group["fields"]:
            if field["key"].endswith("forbid_dangerous_perms"):
                return [check["value"] for check in field["checks"]]
    raise AssertionError("the 无危险权限 row is gone from the settings form")


def test_each_held_authority_reads_as_its_own_permission():
    mint_only, owner = _account(held=("mint_authority",))
    assert mint.authorities(mint_only, owner) == ["mint_authority"]
    freeze_only, owner = _account(held=("freeze_authority",))
    assert mint.authorities(freeze_only, owner) == ["freeze_authority"]
    both, owner = _account(held=("mint_authority", "freeze_authority"))
    assert mint.authorities(both, owner) == BOTH
    # The tag is a `COption`, so a byte that is neither 0 nor 1 is still "held": only the
    # absent tag is an answer about absence.
    raw = bytearray(82)
    raw[46:50] = (2).to_bytes(4, "little")
    assert mint.authorities(base64.b64encode(bytes(raw)).decode(), owner) == ["freeze_authority"]


def test_a_renounced_mint_is_an_empty_list_and_not_a_blank():
    """The whole point of the read: `[]` passes the rule, `None` is 未判定. A completed
    read that found nothing must not look like the node never answered."""
    data, owner = _account()
    assert mint.authorities(data, owner) == []


def test_anything_that_is_not_a_readable_mint_stays_a_blank():
    data, owner = _account()
    assert mint.authorities(data, "11111111111111111111111111111111") is None, "not a token program"
    assert mint.authorities(None, owner) is None, "the node answered nothing"
    assert mint.authorities(data, None) is None, "no owner field at all"
    short, owner = _account(size=40)
    assert mint.authorities(short, owner) is None, "too short to hold the layout"
    assert mint.authorities("not base64!!", owner) is None


def test_the_read_has_no_sweep():
    """A mint is read by its address. A `fetch_universe` that returned anything would put
    it in a pull it has no list for - see `registry.ENRICH_ONLY` for the same rule."""
    assert mint.SolanaMint().fetch_universe("solana", 5000) == []
    assert mint.SolanaMint().chains == ("solana",)


def test_the_adapter_hands_the_permissions_to_the_rule(monkeypatch):
    data, owner = _account(held=("freeze_authority",))
    monkeypatch.setattr(mint, "rpc", lambda *_a, **_k: {"value": {"data": [data, "base64"], "owner": owner}})
    monkeypatch.setattr(mint.config, "chain", lambda chain_id: {"id": chain_id, "rpc": "https://x"})
    metrics, facts = mint.SolanaMint().fetch_token("solana", "CA")
    assert facts.dangerous_perms == ["freeze_authority"]
    assert metrics.ca == "CA", "the row keeps its address"


def test_a_node_that_says_nothing_leaves_the_question_open(monkeypatch):
    monkeypatch.setattr(mint, "rpc", lambda *_a, **_k: None)
    monkeypatch.setattr(mint.config, "chain", lambda chain_id: {"id": chain_id, "rpc": "https://x"})
    _, facts = mint.SolanaMint().fetch_token("solana", "CA")
    assert facts.dangerous_perms is None


def test_the_rule_drops_a_held_authority_and_never_a_blank():
    safety = {"forbid_dangerous_perms": BOTH}
    dropped = filters.check_safety(SafetyFacts(dangerous_perms=["mint_authority"]), safety)
    assert [reason.value for reason in dropped.failed] == ["dangerous_perm"]
    assert filters.check_safety(SafetyFacts(dangerous_perms=[]), safety).passed
    blank = filters.check_safety(SafetyFacts(), safety)
    assert blank.passed
    assert "dangerous_perm" in [reason.value for reason in blank.unknown]


def test_the_shipped_config_asks_this_question():
    """The source is useless if the chain does not configure it, and the rule is useless if
    the source is not asked. Both live in the template, so both are asserted here."""
    config = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    default = config["configs"][0]
    assert "solana_mint" in default["sources"]
    assert "solana_mint" not in default["pipeline"]["scan_sources"], "it has no sweep"
    assert set(default["pipeline"]["safety"]["forbid_dangerous_perms"]) == set(BOTH)


def test_the_two_permissions_are_offered_only_where_the_read_is():
    """`forms` reads the chain's own `sources` list, so a chain that cannot answer the
    question does not offer a checkbox that can never fire - and the chain that can, does."""
    base = {"sources": ["gmgn"], "pipeline": {}, "schedule": {}, "report": {}}
    assert _perm_values(base) == [
        "hidden_owner", "owner_change_balance", "can_take_back_ownership",
    ]
    assert _perm_values({**base, "sources": ["gmgn", "solana_mint"]}) == [
        "hidden_owner", "owner_change_balance", "can_take_back_ownership", *BOTH,
    ]
