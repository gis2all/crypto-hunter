"""GoPlus: the dangerous-permission verdict, the holder table, the pool list, one of the
two source checks.

Numbers here are strings ("0"/"1"), shares arrive as fractions ("0.148343" is 14.83%), and
a chain GoPlus has never seen answers with an empty result rather than an error. All of
them are handled by returning `None` for "not established" instead of guessing a default:
a token whose permissions could not be read must not be counted as permissionless.

One response answers far more than the permission question, and the card shows all of it:
`dex[]` is the pool list (largest first, with the fee and the pair id), `holders[]` is the
largest-holder table with the feed's own burn/lock/contract marks, and the remaining
yes/no fields ride along in `SafetyFacts.flags` for the 附加警示 line.

The card's Chinese labels live in `report/cards.py`. This module keeps the feed's own field
names, which is the vocabulary the settings page and the gate filter in.
"""

from __future__ import annotations

from typing import Any

from ..core.models import SafetyFacts, TokenMetrics
from .http import SourceError, UA_JSON, json_get

BASE = "https://api.gopluslabs.io/api/v1"
#: GoPlus identifies chains by EVM chain id, not by name. Arc is 5042 and Robinhood is 4663.
CHAIN_IDS = {"arc": 5042, "robinhood": 4663}

#: The remaining yes/no fields GoPlus answers, kept for the card's 附加警示 line. Nothing
#: scores them, so they stay a list of field names rather than a dozen model fields.
EXTRA_FLAGS: tuple[str, ...] = (
    "is_whitelisted",
    "cannot_buy",
    "is_anti_whale",
    "anti_whale_modifiable",
    "slippage_modifiable",
    "personal_slippage_modifiable",
    "external_call",
    "selfdestruct",
    "trading_cooldown",
    "honeypot_with_same_creator",
)

#: Where "out of circulation" lives whatever the feed says about it. A balance at 0x...dEaD
#: cannot be spent, so it reads as burnt even when GoPlus leaves `is_locked` at 0.
BURN_ADDRESSES = frozenset({
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
})
#: The settings page stores permissions in English keys; the gate filter uses those keys.
PERMISSION_KEYS: dict[str, str] = {
    "is_mintable": "mintable",
    "is_blacklisted": "blacklist",
    "transfer_pausable": "pausable",
    "hidden_owner": "hidden_owner",
    "owner_change_balance": "owner_change_balance",
    "can_take_back_ownership": "can_take_back_ownership",
    "is_proxy": "is_proxy",
}


def _flag(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    return str(value) == "1"


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(value: Any) -> float | None:
    """GoPlus reports shares as 0..1 fractions; every threshold in config is a percentage."""
    number = _num(value)
    return None if number is None else round(number * 100, 4)


def _pools(row: dict) -> list[dict]:
    """`dex[]`, largest pool first.

    This is the pool breakdown the chain page lists: the venue, the fee tier, how much
    liquidity sits there and the pair id (for a v4 pool that id is the pool itself, which
    is the address the lock question has to be asked about).
    """
    out = []
    for entry in row.get("dex") or []:
        if not isinstance(entry, dict):
            continue
        pair = str(entry.get("pair") or "").strip()
        out.append({
            "name": str(entry.get("name") or "").strip(),
            "kind": str(entry.get("liquidity_type") or "").strip(),
            "liquidity_usd": _num(entry.get("liquidity")),
            "fee_pct": _pct(entry.get("pool_fee")),
            "pair": pair,
            "manager": str(entry.get("pool_manager") or "").strip(),
        })
    out.sort(key=lambda pool: pool["liquidity_usd"] or 0.0, reverse=True)
    return out


def _holders(row: dict) -> list[dict]:
    """`holders[]`, largest first, with the feed's marks read as booleans.

    `percent` is a share of the total supply, so the burn/lock/contract totals below are
    comparable with the 链上质量 numbers rather than with each other.
    """
    out = []
    for entry in row.get("holders") or []:
        if not isinstance(entry, dict):
            continue
        address = str(entry.get("address") or "").strip().lower()
        if not address:
            continue
        out.append({
            "address": address,
            "pct": _pct(entry.get("percent")),
            "contract": _flag(entry.get("is_contract")) is True,
            # A burn address that GoPlus also marks locked is one holding counted twice:
            # the card adds these shares up, so burnt wins the mark.
            "locked": _flag(entry.get("is_locked")) is True and address not in BURN_ADDRESSES,
            "burnt": address in BURN_ADDRESSES,
            "tag": str(entry.get("tag") or "").strip(),
        })
    out.sort(key=lambda holder: holder["pct"] or 0.0, reverse=True)
    return out


def _lp_holders(row: dict) -> list[dict]:
    """`lp_holders[]`: who holds the liquidity, largest first.

    `percent` is a share of the LP supply, not of the token's supply - the table sums to
    1 across the LP. For a v4 pool the holder is the position NFT's owner, which is what
    the lock question has to be asked about.
    """
    return _holders({"holders": row.get("lp_holders")})


def _share(holders: list[dict], mark: str) -> float | None:
    """The share of supply these holders hold, or None when the table did not answer.

    None rather than 0.0: "the feed listed no burnt holder" and "no holder is burnt" are
    different statements, and only the second one deserves a printed 0%.
    """
    if not holders:
        return None
    return round(sum(holder["pct"] or 0.0 for holder in holders if holder[mark]), 4)


class GoPlus:
    name = "goplus"
    chains = ("arc", "robinhood")

    def fetch_universe(self, chain_id: str, limit: int) -> list[TokenMetrics]:
        """GoPlus has no enumeration endpoint: it answers about a CA, never about a chain."""
        return []

    def security(self, chain_id: str, ca: str) -> dict:
        chain_number = CHAIN_IDS.get(chain_id)
        if chain_number is None:
            return {}
        url = "%s/token_security/%d?contract_addresses=%s" % (BASE, chain_number, ca)
        try:
            payload = json_get(url, UA_JSON, tries=2, sleep=2.0, timeout=25.0)
        except SourceError:
            return {}
        result = (payload or {}).get("result") or {}
        return result.get(ca.lower()) or {}

    def fetch_token(self, chain_id: str, ca: str) -> tuple[TokenMetrics, SafetyFacts]:
        row = self.security(chain_id, ca)
        facts = SafetyFacts()
        if not row:
            return TokenMetrics(ca=ca), facts

        facts.source_checked_by = ["GoPlus"]
        open_source = _flag(row.get("is_open_source"))
        facts.source_verified = open_source
        if open_source:
            facts.source_verified_by = ["GoPlus"]
        if row.get("creator_address"):
            facts.creator = str(row["creator_address"]).lower()
        perms = [
            key for field, key in PERMISSION_KEYS.items() if _flag(row.get(field)) is True
        ]
        # A row that answered but carries no permission field at all is "not established";
        # a row that answered "0" everywhere is a real all-clear.
        answered = [field for field in PERMISSION_KEYS if row.get(field) not in (None, "")]
        facts.dangerous_perms = perms if answered else None
        facts.owner_pct = _pct(row.get("owner_percent"))
        facts.creator_pct = _pct(row.get("creator_percent"))
        flags = {}
        for field in EXTRA_FLAGS:
            answer = _flag(row.get(field))
            if answer is not None:
                flags[field] = answer
        facts.flags = flags

        metrics = TokenMetrics(ca=ca)
        count = row.get("holder_count")
        try:
            metrics.holders = int(count) if count not in (None, "") else None
        except (TypeError, ValueError):
            metrics.holders = None
        metrics.honeypot_detected = _flag(row.get("is_honeypot"))
        if metrics.honeypot_detected:
            metrics.honeypot_note = "GoPlus 判定为貔貅"
        # Parsed, but not trusted: Arc's sheet answers "0" for every contract asked
        # (24/24 on 2026-09-25, transfer tax included), so `FIELD_PRIORITY` leaves these
        # fields to GMGN's security sheet rather than feeding a placeholder to the card.
        metrics.transfer_tax_pct = _num(row.get("transfer_tax"))
        metrics.buy_tax_pct = _num(row.get("buy_tax"))
        metrics.sell_tax_pct = _num(row.get("sell_tax"))
        metrics.total_supply = _num(row.get("total_supply"))
        metrics.pool_list = _pools(row)
        holders = _holders(row)
        metrics.top_holders = holders
        # Top-10 is the sum of the table itself, and only when the table is complete: a
        # truncated list would print the top-3 share under a Top10 label.
        if len(holders) >= 10:
            metrics.top10_pct = round(sum(holder["pct"] or 0.0 for holder in holders), 4)
        metrics.burn_pct = _share(holders, "burnt")
        metrics.locked_holder_pct = _share(holders, "locked")
        metrics.contract_holder_pct = _share(holders, "contract")
        metrics.lp_holders = _lp_holders(row)
        metrics.provenance.update({
            "holders": "goplus", "top_holders": "goplus", "pool_list": "goplus",
            "top10_pct": "goplus", "lp_holders": "goplus",
        })
        return metrics, facts
