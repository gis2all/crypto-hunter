"""The Solana mint account: the two authorities that can still rug a holder.

An SPL mint names two owners beside its supply. The mint authority can print more of the
token whenever it likes, so nothing about the supply a holder bought bounds the next
mint; the freeze authority can freeze any account it chooses, which leaves a holder with a
balance that cannot be sold. Both live in the mint account itself, as a `COption<Pubkey>`
tag - 4 bytes at offset 0 for the mint authority, at offset 46 for the freeze authority -
and they are the closest thing this chain has to the dangerous-permission verdict GoPlus
answers on EVM. That verdict is what this feeds: the 无危险权限 rule drops a token whose
permission list intersects `pipeline.safety.forbid_dangerous_perms`.

**The reading is deliberately not GMGN's.** Its security sheet carries `renounced_mint` and
`renounced_freeze_account`, and neither means this. Measured 2026-10-11: USDC, whose mint
authority is still held on chain by Circle (`BJE5MMbqXjVwjAF7oxwPYXnTXDyspzZyt4vwenNw5ruG`),
comes back `renounced_mint: true`; so did all fourteen tokens of the last round's Solana
pool. `pumpCmUJuRPWNJm9JJ5De1kz9Z8DG2HPbBz3hrXQHp`, whose authorities really are gone, came
back `null`. That flag is a default value wearing the shape of a reading, so the account is
read from the node instead - the same correction `LP_LOCK_ONLY_WHEN_TRUE` makes for the LP
question, and for the same reason: a hard drop built on a silence rejects a whole chain at
once.

**An answer is `[]`, never `None`.** A read that completed is a verdict on both
permissions, so a mint that renounced both lands as an empty list - the shape GoPlus returns
for a clean EVM token, and for the same reason: `None` is read as 未判定, which never
rejects, and a completed read must not be mistaken for an unanswered one. A node that said
nothing, an account owned by something other than a token program, and bytes too short to
hold the layout all stay `None`.

**The addresses are not decoded.** The question is "is this permission still held", and
nothing prints who holds it - the card shows the permission, not its owner - so only the
option tag is read and the 32 key bytes beside it are skipped. That is also what keeps this
file free of a base58 encoder.
"""

from __future__ import annotations

import base64

from ... import config
from ...core.models import SafetyFacts, TokenMetrics
from ..onchain import rpc

#: The permissions this read reports, and where the mint layout records each one. The
#: offsets are shared by both token programs, so a token-2022 mint reads like an original
#: one. The names are the values `forbid_dangerous_perms` is configured with.
AUTHORITIES: dict[str, int] = {"mint_authority": 0, "freeze_authority": 46}
#: A `COption<Pubkey>` is a 4-byte tag (0 = absent, 1 = present) and then the key. The tag
#: is little-endian, like every other integer in the account.
OPTION_TAG = 4
#: Below this the account is not a mint whatever else it is, and the tag bytes would be
#: whatever happens to sit at those offsets.
MINT_BYTES = 82
#: Both token programs: the original one and token-2022. Anything else owns an account that
#: is not a mint, and reading its first bytes would invent a verdict out of unrelated data.
TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
)


def authorities(data: str | None, owner: str | None) -> list[str] | None:
    """The permissions this mint still holds: a list, empty when it holds none.

    `None` means the account did not answer - which is not the same fact as an empty list,
    and the difference is the whole point (see the module docstring).
    """
    if str(owner or "") not in TOKEN_PROGRAMS:
        return None
    try:
        raw = base64.b64decode(str(data or ""), validate=True)
    except Exception:
        return None
    if len(raw) < MINT_BYTES:
        return None
    return [
        name
        for name, offset in AUTHORITIES.items()
        if int.from_bytes(raw[offset:offset + OPTION_TAG], "little") != 0
    ]


class SolanaMint:
    """One read per token in the enrich pass. No sweep: a mint is read by its address."""

    name = "solana_mint"
    chains = ("solana",)

    def fetch_universe(self, chain_id: str, limit: int) -> list[TokenMetrics]:
        return []

    def fetch_token(self, chain_id: str, ca: str) -> tuple[TokenMetrics, SafetyFacts]:
        facts = SafetyFacts()
        # The chain's own node, never another chain's: `onchain.rpc_url` raises instead of
        # falling back, which is what stops a config that forgot `rpc` from reading another
        # chain under this one's name.
        chain = config.chain(chain_id)
        answer = rpc(chain, "getAccountInfo", [ca, {"encoding": "base64"}])
        value = answer.get("value") if isinstance(answer, dict) else None
        if not isinstance(value, dict):
            return TokenMetrics(ca=ca), facts
        data = value.get("data")
        blob = data[0] if isinstance(data, list) and data else data
        facts.dangerous_perms = authorities(blob, value.get("owner"))
        return TokenMetrics(ca=ca), facts
