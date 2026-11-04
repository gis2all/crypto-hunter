"""On-chain facts for one monitor card: supply, market and the safety flags (2026-10-30).

The monitor's cards are about mints that may not have a pool yet, so three sources are asked and
each may answer nothing: the supply is read straight off the contract, the market comes from
DexScreener, and the safety sheet from GoPlus. A missing answer stays missing - the card prints
未判定, never 0.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache

import httpx

from ...sources import dexscreener, goplus

log = logging.getLogger("crypto-hunter.monitors")

#: BSC public nodes. They are rotated: one rate-limited node must not turn a whole sweep into
#: undecided (2026-10-31, after agentid-sniper's six-node rotation).
BSC_RPC = "https://bsc-dataseed.binance.org"
BSC_RPCS = (BSC_RPC, "https://bsc.publicnode.com", "https://bsc-rpc.publicnode.com")
#: `bsc-dataseed` answers `eth_getLogs` with an empty array instead of an error, so it must not
#: be the first node for logs; publicnode serves the 50k-block window (2026-10-31).
LOG_RPCS = ("https://bsc.publicnode.com", "https://bsc-rpc.publicnode.com", BSC_RPC)
_RPC_AT = 0

#: The card stores a label ("BSC", "Robinhood"); the settings file is keyed by id.
CHAIN_ALIASES = {
    "bsc": "bsc", "bnb": "bsc", "bnb chain": "bsc", "binance": "bsc",
    "binance smart chain": "bsc",
    "robinhood": "robinhood", "robinhood chain": "robinhood",
    "arc": "arc",
    "solana": "solana", "sol": "solana",
}
_LOCAL = threading.local()


def chain_id(label: str) -> str:
    """The settings file's id for a card's label. Unknown chains get no id.

    "未判定" stays BSC: the registry this monitor is about lives there, and a probe that
    answers is how a card without a chain becomes "BSC" (2026-10-31).
    """
    text = str(label or "").strip().lower()
    if text in ("", "未判定", "unknown", "n/a"):
        return "bsc"
    return CHAIN_ALIASES.get(text, "")


@contextmanager
def _on_chain(chain: str):
    """Make every helper in this module read the same chain for the duration of one probe."""
    previous = getattr(_LOCAL, "chain", "bsc")
    _LOCAL.chain = chain or "bsc"
    try:
        yield
    finally:
        _LOCAL.chain = previous


def _current_chain() -> str:
    return getattr(_LOCAL, "chain", "bsc")


def _nodes(chain: str) -> tuple[str, ...]:
    """The nodes for one chain: BSC rotates, every other chain has the one in its settings."""
    if chain in ("", "bsc"):
        return BSC_RPCS
    if chain == "solana":  # not EVM; eth_getCode has no meaning here
        return ()
    try:
        from ... import config
        rpc = str((config.load_chains().get(chain) or {}).get("rpc") or "").strip()
    except Exception:  # noqa: BLE001 - a missing chain is an empty node list, not a crash
        rpc = ""
    return (rpc,) if rpc else ()
#: `totalSupply()` - the one method ERC-20 and ERC-721 both have.
TOTAL_SUPPLY = "0x18160ddd"
#: `decimals()` - ERC-20 answers 18 (usually); ERC-721 has none and the call reverts.
DECIMALS = "0x313ce567"
#: `supportsInterface(0x80ac58cd)` - ERC-721's own interface id.
ERC721 = "0x01ffc9a7" + "80ac58cd".ljust(64, "0")
#: `symbol()` and `name()` - ERC-20 metadata. ERC-721 answers both too, so neither decides alone.
SYMBOL = "0x95d89b41"
NAME = "0x06fdde03"
#: `owner()` - tells "owner can still mint" apart from "anyone can mint, nobody can stop it".
OWNER = "0x8da5cb5b"
#: `getReserves()` on a UniV2 pair, `balanceOf(address)` on its LP token (the pair itself).
PAIR_RESERVES = "0x0902f1ac"
BALANCE_OF = "0x70a08231"
#: Where an LP can be parked so nobody can pull it: burn addresses come first, then the
#: lockers BSC projects actually use. A miss here is not proof of "unlocked" - see
#: `lp_from_pair()`, which only reports a share it could actually account for.
BURNED = ("0x0000000000000000000000000000000000000000",
          "0x000000000000000000000000000000000000dead")
LOCKERS = ("0x663a5c229c09b049e36dcc11a9b0d4a8eb9db214",   # Unicrypt
           "0x71b5759d73262fbb223956913ecf4ecc51057641",   # PinkLock
           "0xe2fe530c047f2d85298b07d9333c05737f1435fb")   # Team Finance
#: Getters a sale contract uses to point at the ERC-20 it sells. Non-standard, so each is tried
#: and the first address that passes `token_facts()` wins. Selectors are keccak256(sig)[..4].
TOKEN_GETTERS = (
    ("0xfc0c546a", "token()"),
    ("0x9d76ea58", "tokenAddress()"),
    ("0x1083f761", "assetToken()"),
    ("0xe985e367", "saleToken()"),
    ("0x55a373d6", "tokenContract()"),
    ("0x3013ce29", "paymentToken()"),
)
#: Common "how many were sold" getters, most specific first. There is no standard for this, so
#: the pool is tried in order and the first answer wins. Selectors are keccak256(signature)[..4].
#: `totalSupply()` is not in this pool: on an ERC-20 it is the token supply, not the sold count.
MINT_GETTERS = (
    ("0xa2309ff8", "totalMinted()"),
    ("0x4f02c420", "minted()"),
    ("0xcf721b15", "mintedCount()"),
    ("0xd52079b4", "numMinted()"),
    ("0xd082e381", "tokenCounter()"),
)
#: Common "how many exist in total" getters. Non-standard, so each is tried and ignored.
MAX_SELECTORS = ("0xd5abeb01", "0x355274ea", "0x32cb6b0c")
#: Sale-specific getters. `price()` / `getPrice()` are deliberately absent: on many
#: contracts they answer something other than the mint price.
SALE_PRICE_GETTERS = (
    ("0x6feaf968", "mintPrice()"),
    ("0x178740ed", "cost()"),
    ("0xb671629f", "publicSalePrice()"),
    ("0x7fc1776c", "salePrice()"),
)
SALE_START_GETTERS = (
    ("0x153c9232", "publicSaleStart()"),
    ("0x0c0e39b5", "mintStart()"),
    ("0xd6c3b4c7", "saleStart()"),
    ("0x55b82df7", "startTime()"),
)
SALE_END_GETTERS = (
    ("0xec9b5cb2", "publicSaleEnd()"),
    ("0x64d66320", "mintEnd()"),
    ("0x3472aaa0", "saleEnd()"),
    ("0xb6f739ee", "endTime()"),
    ("0xc5d6e87f", "deadline()"),
    ("0xe7e4e238", "endTimestamp()"),
)
NATIVE_SYMBOLS = {"bsc": "BNB", "ethereum": "ETH", "base": "ETH",
                  "robinhood": "ETH", "arc": "USDC"}
MIN_NATIVE_PRICE = 10**9
MAX_NATIVE_PRICE = 10**21
SALE_TIME_MIN = 1_577_836_800  # 2020-01-01
SALE_TIME_MAX = 4_102_444_800  # 2100-01-01
AGENT_ID_REGISTRY = "0x60b9262456de011f7d15a54cdd8c48baaa8b022f"
#: Addresses that show up in posts but are never a project's own contract: the zero address and
#: the registry every Agent ID project merely points at (2026-10-31).
INFRA = frozenset({AGENT_ID_REGISTRY, "0x" + "0" * 40})
AGENT_ID_SELECTOR = "9f92d0dd"
EIP1967_IMPL = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
EIP1967_BEACON = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"
IMPLEMENTATION = "0x5c60da1b"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TRANSFER_SINGLE = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
TRANSFER_BATCH = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"
ZERO_TOPIC = "0x" + "0" * 64
LOG_CHUNK = 50_000
HEAT_CHUNK = 10_000
LOG_CHUNKS = 12
#: The deep evidence scan walks this many blocks; the card records it as the window.
DEEP_WINDOW = LOG_CHUNK * LOG_CHUNKS
BLOCKS_30M = 2_400

_ADDRESS = re.compile(r"0x[a-fA-F0-9]{40}")
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
#: One keep-alive client for every `eth_call`: a fresh Client per call spent more time on the
#: TLS handshake than on the node (`token_of` is a dozen calls per card, 2026-10-30).
_CLIENT: httpx.Client | None = None
#: `9,004 / 444,000 shares`, `27 / 7,000`, `Minted 434 / 1000` - the shapes these pages print.
_MINT_PAIR = re.compile(r"(?<![\d.])([\d][\d,]{0,})\s*/\s*([\d][\d,]{0,})")
#: 只给一个数、没有分母的写法：`3,073 cards claimed`、`8,210 minted`（agentsea 的计数条）。
_MINT_SINGLE = re.compile(
    r"(?<![\d.])([\d][\d,]{1,})\s*(?:[a-z]{2,12}\s+)?(?:claimed|minted|issued|sold)\b",
    re.I)
_MINT_WORDS = ("minted", "shares", "supply", "confirmed", "progress", "铸造", "已铸", "total")
#: 每钱包上限、单地址限制这类附近的「x / y」不是铸造进度（BNBS 的 10 shares per wallet）。
_LIMIT_WORDS = ("per wallet", "per address", "每钱包", "每地址", "limit", "max ", "上限")
_PRICE = re.compile(
    r"\b(?:mint\s*price|price|cost|售价|价格)\b\s*[:：]?\s*"
    r"(免费|free|(?:\d[\d,]*(?:\.\d+)?)\s*(?:ETH|BNB|USDT|USDC|BUSD|DAI|SOL|USD)?)",
    re.I,
)
_DEADLINE = re.compile(
    r"\b(?:deadline|ends?|closes?|until|sale\s*end|mint\s*end|截止(?:时间)?|结束)\b"
    r"\s*[:：]?\s*"
    r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?"
    r"(?:\s*(?:UTC|GMT))?)",
    re.I,
)
_UNIT = re.compile(
    r"\b\d[\d,]*\s+(shares?|cards?|nfts?|seats?|spots?|份|席位|枚)\b", re.I
)


def plain(value) -> str:
    """Page text without markup: the renderer sometimes hands back HTML, and `3,073</b><span>cards
    claimed` is not a number any regex can read (agentsea, 2026-10-30)."""
    text = _SCRIPT.sub(" ", str(value or ""))
    text = _TAG.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_mint(text: str) -> dict:
    """Pull `minted / total` out of the page text without asking a model.

    The model kept returning the total and dropping the numerator even when both were on the
    page (`9,004 / 444,000`); a pair next to a mint word is not something to summarise away.
    """
    body = plain(text)
    best: tuple[int, int] | None = None
    for match in _MINT_PAIR.finditer(body):
        try:
            done = int(match.group(1).replace(",", ""))
            total = int(match.group(2).replace(",", ""))
        except ValueError:
            continue
        if total <= 0 or done > total:
            continue
        window = body[max(0, match.start() - 120):match.end() + 80].lower()
        if not any(word in window for word in _MINT_WORDS):
            continue
        # 上限词跟着这一对走（"1 / 10 shares per wallet"）；隔壁表里的 limit 不影响它。
        near = body[max(0, match.start() - 15):match.end() + 40].lower()
        if any(word in near for word in _LIMIT_WORDS):
            continue
        # 一页上常有好几对（池子进度、格子进度），铸造那一对的分母最大。
        if best is None or total > best[1]:
            best = (done, total)
    if best:
        return {"done": best[0], "total": best[1]}
    # 没有分母时，一个带铸造动词的数也算进度：总比整张卡落成「未判定」强。
    for match in _MINT_SINGLE.finditer(body):
        try:
            done = int(match.group(1).replace(",", ""))
        except ValueError:
            continue
        if done > 0:
            return {"done": done}
    return {}


def parse_page_facts(text: str) -> dict:
    """Deterministic page-owned fields: mint price, deadline, and unit wording."""
    body = plain(text)
    out: dict = {}
    price = _PRICE.search(body)
    if price:
        value = re.sub(r"\s+", " ", price.group(1)).strip()
        out["price"] = "免费" if value.lower() == "free" or value == "免费" else value
    deadline = _DEADLINE.search(body)
    if deadline:
        value = deadline.group(1).replace("/", "-").replace(".", "-")
        out["deadline"] = re.sub(r"\s+", " ", value).strip()
    unit = _UNIT.search(body)
    if unit:
        out["mint_unit"] = unit.group(1)
    return out


def _native_price(value: int, chain: str) -> str:
    amount = Decimal(value) / Decimal(10**18)
    return "%s %s" % (format(amount.normalize(), "f"),
                      NATIVE_SYMBOLS.get(chain, "native"))


def sale_facts(ca: str, chain: str = "bsc") -> dict:
    """Common sale getters, accepted only after range checks.

    There is no standard mint-price or deadline interface. Values outside a plausible
    native-price / calendar range are ignored rather than printed as facts.
    """
    with _on_chain(chain):
        ca = str(ca or "").strip().lower()
        if not _ADDRESS.fullmatch(ca):
            return {}
        out: dict = {}
        for selector, _name in SALE_PRICE_GETTERS:
            value = _uint(ca, selector)
            if value is not None and MIN_NATIVE_PRICE <= value <= MAX_NATIVE_PRICE:
                out["mint_price"] = _native_price(value, chain)
                break
        for key, getters in (("sale_start", SALE_START_GETTERS),
                             ("sale_deadline", SALE_END_GETTERS)):
            for selector, _name in getters:
                value = _uint(ca, selector)
                if value is not None and SALE_TIME_MIN <= value <= SALE_TIME_MAX:
                    out[key] = datetime.fromtimestamp(value, timezone.utc).isoformat(
                        timespec="seconds")
                    break
        return out


#: Chain names worth recognising in prose. Ordered: the specific spellings first, so
#: "Robinhood Chain" is not read as something else. ASCII names match on word boundaries —
#: "Base" must not match "database", and "Arc" must not match "archive".
CHAIN_HINTS = (
    (r"\brobinhood chain\b|\brobinhood\b", "Robinhood"),
    (r"\bbnb chain\b|\bbsc\b|\bbinance smart chain\b", "BSC"),
    (r"\barbitrum\b", "Arbitrum"),
    (r"\bhyperliquid\b", "Hyperliquid"),
    (r"\bpolygon\b", "Polygon"),
    (r"\bsolana\b", "Solana"),
    (r"\bethereum\b|\beth mainnet\b", "Ethereum"),
    (r"\bbase\b", "Base"),
    (r"\bnear\b", "Near"),
)


def guess_chain(*texts: str) -> str:
    """The chain a card is on, read off its own words. Empty when the prose names none."""
    blob = " ".join(str(text or "") for text in texts).lower()
    for pattern, label in CHAIN_HINTS:
        if re.search(pattern, blob):
            return label
    return ""


def first_address(*texts: str) -> str:
    """The first contract address in any of these texts, lowercased. Empty when there is none."""
    for text in texts:
        for found in _ADDRESS.finditer(str(text or "")):
            addr = found.group(0).lower()
            if addr not in INFRA:
                return addr
    return ""


def _call(ca: str, data: str, *, timeout: float = 8.0) -> str | None:
    """One `eth_call`, raw hex back. None means the node or the contract did not answer."""
    value = str(_rpc("eth_call", [{"to": ca, "data": data}, "latest"], timeout=timeout) or "")
    return value if value.startswith("0x") and len(value) > 2 else None


def _rpc(method: str, params: list, *, timeout: float = 8.0, chain: str | None = None):
    """One JSON-RPC call, rotated across that chain's nodes."""
    global _CLIENT, _RPC_AT
    chain = chain or _current_chain()
    nodes = LOG_RPCS if (method == "eth_getLogs" and chain == "bsc") else _nodes(chain)
    if not nodes:
        return None
    if _CLIENT is None:
        _CLIENT = httpx.Client(timeout=timeout)
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last = None
    for offset in range(len(nodes)):
        at = (_RPC_AT + offset) % len(nodes)
        try:
            reply = _CLIENT.post(nodes[at], json=body, timeout=timeout).json()
            if (reply or {}).get("error"):
                message = str(reply["error"].get("message") or "rpc error")
                # A revert is the contract answering "no such getter". Every node reverts
                # alike, so trying the other two buys nothing and fills the log (2026-10-31).
                if "revert" in message.lower():
                    return None
                raise RuntimeError(message)
            _RPC_AT = at
            return reply.get("result")
        except Exception as exc:  # noqa: BLE001 - try the next node
            last = exc
    log.info("monitor: %s did not answer on any node: %r", method, last)
    return None


def _code(ca: str, *, timeout: float = 8.0, chain: str | None = None) -> str | None:
    """Runtime bytecode. `None` = no node answered; `"0x"` = the address has no code."""
    result = _rpc("eth_getCode", [ca, "latest"], timeout=timeout, chain=chain)
    return None if result is None else str(result).lower()


def _uint(ca: str, data: str, *, timeout: float = 8.0) -> int | None:
    """One `eth_call` read as a uint. None means the node or the contract did not answer."""
    value = _call(ca, data, timeout=timeout)
    if value is None:
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _text(value: str | None) -> str:
    """An ABI `string` (offset + length) or a `bytes32` name, as text. Empty when unreadable."""
    raw = (value or "")[2:]
    try:
        if len(raw) >= 192:
            offset = int(raw[:64], 16) * 2
            length = int(raw[offset:offset + 64], 16) * 2
            chunk = bytes.fromhex(raw[offset + 64:offset + 64 + length])
        elif len(raw) == 64:
            chunk = bytes.fromhex(raw)
        else:
            return ""
        return chunk.decode("utf-8", "ignore").strip("\x00").strip()
    except ValueError:
        return ""


def symbol(ca: str, *, timeout: float = 8.0) -> str:
    """ERC-20 / ERC-721 `symbol()`, or empty when the contract has none."""
    return _text(_call(ca, SYMBOL, timeout=timeout))


def total_supply(ca: str, *, timeout: float = 8.0) -> int | None:
    """`totalSupply()` straight off the node. None means the node did not answer."""
    return _uint(ca, TOTAL_SUPPLY, timeout=timeout)


def decimals(ca: str, *, timeout: float = 8.0) -> int | None:
    """ERC-20's `decimals()`. ERC-721 has none; the call reverts and this stays None."""
    value = _uint(ca, DECIMALS, timeout=timeout)
    return value if value is not None and 0 <= value <= 36 else None


def is_erc721(ca: str, *, timeout: float = 8.0) -> bool | None:
    value = _uint(ca, ERC721, timeout=timeout)
    return None if value is None else bool(value)


#: A share / mint count above this is not believable; the same ceiling max_supply uses.
SHARE_CAP = 10**12


def _token_amount(value: int, scale: int | None) -> int:
    """A getter answering in base units (SoulBoard: 7.245e24) is an amount, not a count."""
    if scale is not None and value > SHARE_CAP:
        return value // (10 ** scale)
    return value


def max_supply(ca: str, *, minted: int | None = None, timeout: float = 8.0) -> int | None:
    """The cap, when the contract really means a share cap.

    A non-standard getter can answer something else entirely - JAGENT's `maxSupply()` slot
    returned a wei-scale number that is not a share count. A share cap above a trillion is not
    believable, so it is ignored and the site's number is used instead.
    """
    for selector in MAX_SELECTORS:
        value = _uint(ca, selector, timeout=timeout)
        if value and 0 < value <= SHARE_CAP and (minted is None or value >= minted):
            return value
    return None


@lru_cache(maxsize=256)
def token_facts(ca: str) -> dict:
    """Whether this address really is a tradeable ERC-20, plus its metadata.

    GMGN and Debot only have a page for a token, and a mint contract is not one: JAGENT's
    answers `totalMinted()` but no `symbol()`. "Not ERC-721" therefore proves nothing - an
    address has to answer all four of symbol / decimals / totalSupply and be non-721 before
    it is called a token (2026-10-30, user).
    """
    ca = str(ca or "").strip().lower()
    if not _ADDRESS.fullmatch(ca) or int(ca, 16) == 0:
        return {}
    name = symbol(ca)
    if not name:
        return {}
    scale = decimals(ca)
    if scale is None:
        return {}
    if is_erc721(ca):
        return {}
    supply = total_supply(ca)
    if not supply:
        return {}
    return {"token": ca, "token_symbol": name[:24], "token_decimals": scale,
            "token_supply": supply}


def token_of(ca: str) -> dict:
    """The ERC-20 behind a sale contract: its own `token()`-style getter first, then itself."""
    ca = str(ca or "").strip().lower()
    if not _ADDRESS.fullmatch(ca):
        return {}
    for selector, _ in TOKEN_GETTERS:
        value = _call(ca, selector)
        linked = "0x" + value[-40:].lower() if value and len(value) >= 42 else ""
        facts = token_facts(linked) if linked else {}
        if facts:
            return facts
    return token_facts(ca)


def token_first(*addresses: str, limit: int = 6, chain: str = "bsc") -> dict:
    """The first of these addresses that really is a token.

    A page lists many addresses (mint pass, forge, emitter, the token); only one of them is
    the token, and it is verified on-chain rather than trusted because it was printed on the
    site (2026-10-30, user).
    """
    with _on_chain(chain):
        tried = 0
        for ca in addresses:
            if tried >= limit:
                break
            tried += 1
            facts = token_facts(ca)
            if facts:
                return facts
    return {}


def lp_state(metrics) -> dict:
    """Who can pull the pool: the LP table, not the dollar size of the liquidity.

    GoPlus's `lp_holders[]` says which share of the LP sits in a locker or a burn address. The
    card prints that share, because "liquidity $40k" says nothing about whether the deployer
    can take it back tomorrow (2026-10-30, user).
    """
    holders = [row for row in (getattr(metrics, "lp_holders", None) or [])
               if (row.get("pct") or 0) > 0]
    if not holders:
        return {}
    total = sum(row["pct"] for row in holders) or 1.0
    safe = sum(row["pct"] for row in holders if row.get("locked") or row.get("burnt"))
    burnt = sum(row["pct"] for row in holders if row.get("burnt"))
    share = round(safe / total * 100, 1)
    out = {"lp_locked_pct": share, "lp_burnt_pct": round(burnt / total * 100, 1)}
    if burnt / total >= 0.99:
        out["lp_state"] = "burnt"
    elif share >= 90:
        out["lp_state"] = "locked"
    elif share >= 50:
        out["lp_state"] = "partial"
    else:
        out["lp_state"] = "unlocked"
    return out


def lp_from_pair(pair: str) -> dict:
    """The pool itself, for the tokens GoPlus has no `lp_holders` table for.

    Two readings are honest here: an empty pair (reserves and LP supply both 0) is "the pool
    exists but nobody funded it yet" - BabyJagent only builds the pool after all 6,000 sell
    out - and the share of the LP sitting in a burn address or a known locker. A pool with
    supply but no burner/locker holding it is reported as nothing, not as "unlocked": not
    being able to account for the LP is not the same as proving whoever holds it can dump it
    (2026-10-30, user).
    """
    pair = str(pair or "").strip().lower()
    if not _ADDRESS.fullmatch(pair):
        return {}
    reserves = _call(pair, PAIR_RESERVES)
    supply = total_supply(pair)
    if reserves and len(reserves) >= 130:
        left, right = int(reserves[2:66], 16), int(reserves[66:130], 16)
        if not left and not right:
            return {"lp_state": "empty"}
    if not supply:
        return {"lp_state": "empty"}
    safe = 0
    for holder in BURNED + LOCKERS:
        held = _uint(pair, BALANCE_OF + holder[2:].rjust(64, "0"))
        safe += held or 0
    if not safe:
        return {}
    share = round(min(safe, supply) / supply * 100, 1)
    return {"lp_state": "burnt" if share >= 99 else "locked" if share >= 90 else "partial",
            "lp_locked_pct": share}


def _without_metadata(body: str) -> str:
    """Drop Solidity's CBOR metadata tail; it changes with the compiler, not the template."""
    for marker in ("a264697066735822", "a165627a7a72305820"):
        at = body.rfind(marker)
        if at != -1:
            return body[:at]
    return body


def fingerprint_code(code: str) -> str:
    """Length plus the first and last 120 bytes: the same template keeps this value."""
    body = _without_metadata(str(code or "").lower().removeprefix("0x"))
    if len(body) < 8:
        return ""
    digest = hashlib.sha1((body[:240] + body[-240:]).encode("ascii", "ignore")).hexdigest()
    return f"{len(body)}:{digest[:12]}"


def code_fingerprint(ca: str) -> str:
    return fingerprint_code(_code(ca))


def agent_gate(ca: str, *, chain: str = "bsc") -> bool | None:
    """True when bytecode embeds the Agent ID registry; False when it has code but no check.

    The registry lives on BSC; asking another chain for that constant is a category mistake.
    """
    if chain != "bsc":
        return None
    code = _code(ca, chain=chain)
    if not code or len(code) <= 2:
        return None
    return AGENT_ID_REGISTRY[2:] in code or AGENT_ID_SELECTOR in code


def has_code(ca: str, *, chain: str = "bsc") -> bool | None:
    """True / False from eth_getCode; None when no node answered."""
    code = _code(ca, chain=chain)
    if code is None:
        return None
    return len(code) > 2


def identify(ca: str, *, chain: str = "bsc") -> dict:
    """Bytecode identity in one round-trip: template fingerprint plus the Agent ID gate."""
    code = _code(ca, chain=chain)
    if not code or len(code) <= 2:
        return {}
    out = {"code_fp": fingerprint_code(code)}
    if chain == "bsc":
        out["agent_gated"] = AGENT_ID_REGISTRY[2:] in code or AGENT_ID_SELECTOR in code
    return out


def implementation(ca: str) -> str:
    """The logic contract behind an EIP-1967 proxy, or empty when this is not a proxy."""
    ca = str(ca or "").strip().lower()
    if not _ADDRESS.fullmatch(ca):
        return ""
    for slot in (EIP1967_IMPL, EIP1967_BEACON):
        raw = str(_rpc("eth_getStorageAt", [ca, slot, "latest"]) or "")
        if not raw or not raw.startswith("0x") or int(raw, 16) == 0:
            continue
        target = "0x" + raw[-40:].lower()
        if slot == EIP1967_BEACON:
            value = _call(target, IMPLEMENTATION)
            target = "0x" + value[-40:].lower() if value and len(value) >= 42 else ""
        target_code = _code(target)
        if target and target != ca and target_code and len(target_code) > 2:
            return target
    return ""


def _mint_rows(ca: str, *, deep: bool = False, chunks: int = LOG_CHUNKS,
               chunk: int = LOG_CHUNK):
    """Mint logs, newest chunk first, as (rows, complete, latest)."""
    head = _rpc("eth_blockNumber", [])
    try:
        latest = int(str(head), 16)
    except (TypeError, ValueError):
        return [], False, 0
    rows = []
    end = latest
    complete = False
    for index in range(chunks if deep else 1):
        start = max(0, end - chunk + 1)
        batch = _rpc("eth_getLogs", [{"address": ca, "fromBlock": hex(start),
                                     "toBlock": hex(end),
                                     "topics": [[TRANSFER, TRANSFER_SINGLE, TRANSFER_BATCH]]}])
        if batch is None:
            return rows, False, latest
        if not batch:
            if index > 0:
                complete = True
                break
        else:
            rows.extend(batch)
        if start == 0:
            complete = True
            break
        end = start - 1
    return rows, complete, latest


def _mint_of(row: dict):
    """(block, to, tx) when this log is a mint; None for a normal transfer."""
    topics = [str(item).lower() for item in row.get("topics") or []]
    if not topics:
        return None
    topic0 = topics[0]
    if topic0 == TRANSFER and len(topics) >= 3:
        sender, receiver = topics[1], topics[2]
    elif topic0 in (TRANSFER_SINGLE, TRANSFER_BATCH) and len(topics) >= 4:
        sender, receiver = topics[2], topics[3]
    else:
        return None
    if sender != ZERO_TOPIC or receiver == ZERO_TOPIC:
        return None
    try:
        block = int(str(row.get("blockNumber") or "0x0"), 16)
    except ValueError:
        block = 0
    return block, "0x" + receiver[-40:], str(row.get("transactionHash") or "")


def mint_logs(ca: str, *, deep: bool = False) -> dict:
    """Mint count and wallet heat from logs; mint_logs is a total only when complete."""
    window = LOG_CHUNK if deep else HEAT_CHUNK
    rows, complete, latest = _mint_rows(ca, deep=deep, chunk=window)
    mints = [item for row in rows if (item := _mint_of(row))]
    if not mints:
        return {"mint_logs": 0} if complete else {}
    recent_edge = latest - window + 1
    recent = [item for item in mints if item[0] >= recent_edge]
    thirty = [item for item in mints if item[0] >= latest - BLOCKS_30M]
    out = {
        "mint_recent": len(recent),
        "mint_wallets": len({item[1] for item in recent}),
        "mint_30m": len(thirty),
        "mint_30m_wallets": len({item[1] for item in thirty}),
    }
    if complete:
        out["mint_logs"] = len(mints)
    return out


def _unique(values, limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
        if len(out) >= limit:
            break
    return out


def _holders(addresses: list[str]) -> int:
    holders = 0
    for address in addresses:
        value = _uint(AGENT_ID_REGISTRY, BALANCE_OF + address[2:].rjust(64, "0"))
        if value:
            holders += 1
    return holders


def _stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def agent_evidence(ca: str, *, deep: bool = False, sample: int = 8) -> dict:
    """Who minted, and how many of them hold an Agent ID.

    A backend-signed mint has the project's own hot wallet as the transaction sender, so the
    recipients are the users and the senders are the project. Recipients first; senders only
    when a mint does not name five distinct recipients (2026-10-31).
    """
    window = DEEP_WINDOW if deep else HEAT_CHUNK
    rows, _, _ = _mint_rows(ca, deep=deep, chunk=LOG_CHUNK if deep else HEAT_CHUNK)
    mints = [item for row in rows if (item := _mint_of(row))]
    side, addresses = "recipients", _unique(
        [item[1].lower() for item in reversed(mints)], sample)
    if len(addresses) < 5:
        hashes = _unique([item[2] for item in reversed(mints)], sample)
        senders = []
        for tx in hashes:
            reply = _rpc("eth_getTransactionByHash", [tx])
            sender = str((reply or {}).get("from") or "").lower()
            if _ADDRESS.fullmatch(sender):
                senders.append(sender)
        side, addresses = "senders", _unique(senders, sample)
    checked = _stamp()
    if len(addresses) < 5:
        return {"agent_source": "none", "agent_side": side,
                "agent_samples": len(addresses), "agent_window": window,
                "agent_checked_at": checked}
    share = round(_holders(addresses) / len(addresses), 2)
    return {"agent_callers": share, "agent_side": side,
            "agent_samples": len(addresses), "agent_window": window,
            "agent_source": "callers" if share >= 0.35 else "none",
            "agent_checked_at": checked}


def mint_only(ca: str, chain: str = "bsc") -> dict:
    with _on_chain(chain):
        return _mint_only(ca)


def _mint_only(ca: str) -> dict:
    """A cheap pass over one address: is it the mint contract, and how many were minted?

    `_probe_first` tries up to six addresses from one page. Running the full probe (market,
    safety, logs, bytecode) on each is minutes of round-trips; this only asks the mint getters
    (2026-10-31).
    """
    ca = str(ca or "").strip().lower()
    if not _ADDRESS.fullmatch(ca):
        return {}
    out: dict = {}
    raw = total_supply(ca)
    nft = is_erc721(ca)
    minted = None
    minted_by = ""
    if nft is True:
        out["nft"] = True
        if raw is not None:
            minted, minted_by = raw, "totalSupply()"
    else:
        for selector, getter in MINT_GETTERS:
            value = _uint(ca, selector)
            if value is not None:
                minted, minted_by = value, getter
                break
    if minted is not None:
        out["minted"] = minted
        out["minted_by"] = minted_by
        cap = max_supply(ca, minted=minted)
        if cap:
            out["mint_total"] = cap
    return out


def probe(ca: str, chain: str = "bsc", *, verify_code: bool = True,
          read: str = "") -> dict:
    with _on_chain(chain):
        return _probe(ca, verify_code=verify_code, read=read)


def _probe(ca: str, *, verify_code: bool = True, read: str = "") -> dict:
    """Everything the card's on-chain row prints. Missing answers are simply absent.

    `verify_code=False` is the refresh path: the template and Agent ID gate do not change every
    sweep, and fetching a 40 KB bytecode costs more than every other call (2026-10-31).
    """
    chain = _current_chain()
    if chain == "solana":
        return {}
    ca = str(ca or "").strip().lower()
    out: dict = {"contract": ca}
    read = str(read or "").strip().lower()
    if verify_code:
        code = _code(ca)
        if code is not None and len(code) <= 2:
            return {}  # a real answer: there is no bytecode at this address
        # A node that did not answer is a gap, not a verdict: keep reading what can be read.
        # A full contract is not a proxy; the two storage reads cost more than most calls.
        target = (implementation(ca)
                  if (code and _ADDRESS.fullmatch(ca) and len(code) <= 4000) else "")
        if target:
            out["proxy"] = target
            read = target
            code = _code(read)
        else:
            read = ca
        if code and len(code) > 2:
            if chain == "bsc":
                out["agent_gated"] = (AGENT_ID_REGISTRY[2:] in code
                                      or AGENT_ID_SELECTOR in code)
            fingerprint = fingerprint_code(code)
            if fingerprint:
                out["code_fp"] = fingerprint
    if not read:
        read = ca

    metrics = facts = None
    try:
        metrics, facts = goplus.GoPlus().fetch_token(chain, ca)
    except Exception as exc:  # noqa: BLE001
        log.info("monitor: goplus did not answer for %s: %r", ca, exc)

    dex = None
    try:
        dex, _ = dexscreener.DexScreener().fetch_token(chain, ca)
    except Exception as exc:  # noqa: BLE001
        log.info("monitor: dexscreener did not answer for %s: %r", ca, exc)

    minted = None
    minted_by = ""
    raw = total_supply(read)
    scale = decimals(read)
    nft = is_erc721(read)
    if nft is True:
        # totalSupply() is the minted count for a 721; asking five getters first is five
        # round-trips that cannot answer better (2026-10-31).
        if raw is not None:
            minted, minted_by = raw, "totalSupply()"
    else:
        for selector, getter in MINT_GETTERS:
            value = _uint(read, selector)
            if value is not None:
                minted, minted_by = value, getter
                break
    if nft is True:
        out["nft"] = True
    done = minted if minted is not None else (raw if nft else None)
    if done is not None and nft is not True:
        done = _token_amount(done, scale)
    logs = mint_logs(read)
    if nft is True and not done:
        # Some 721s never update totalSupply(); the Transfer logs still know.
        deeper = mint_logs(read, deep=True)
        if deeper.get("mint_logs"):
            done, minted_by = deeper["mint_logs"], "logs"
            logs.update(deeper)
    if logs:
        out.update(logs)
    if done is not None:
        out["minted"] = done
        if minted_by:
            out["minted_by"] = minted_by
        cap = max_supply(read, minted=done)
        if cap:
            out["mint_total"] = cap
    if raw is not None and not nft:
        out["kind"] = "erc20"
        out["token_total"] = raw // (10 ** scale) if scale is not None else raw
        if scale is not None:
            out["decimals"] = scale
    token_meta = token_of(read)
    if token_meta:
        out.update(token_meta)
    owner = str(getattr(facts, "owner_address", "") or "") if facts is not None else ""
    if not owner:
        value = _call(str(token_meta.get("token") or read), OWNER)
        owner = "0x" + value[-40:].lower() if value and len(value) >= 42 else ""
    try:
        if owner and int(owner, 16):
            out["owner"] = owner
    except ValueError:
        log.info("monitor: %s reported an unreadable owner: %r", ca, owner)
    if dex is not None:
        if dex.price_usd is not None:
            out["price_usd"] = dex.price_usd
        if dex.liquidity_usd is not None:
            out["liquidity_usd"] = dex.liquidity_usd
        if dex.market_cap_usd is not None:
            out["fdv_usd"] = dex.market_cap_usd
    if metrics is not None:
        if metrics.honeypot_detected is not None:
            out["honeypot"] = metrics.honeypot_detected
        if metrics.buy_tax_pct is not None:
            out["buy_tax"] = metrics.buy_tax_pct
        if metrics.sell_tax_pct is not None:
            out["sell_tax"] = metrics.sell_tax_pct
        if metrics.holders is not None:
            out["holders"] = metrics.holders
        if metrics.top10_pct is not None:
            out["top10"] = metrics.top10_pct
        pools = [pool for pool in (getattr(metrics, "pool_list", None) or []) if pool]
        out["pools"] = len(pools)
        if pools:
            if pools[0].get("pair"):
                out["pool"] = pools[0]["pair"]
            if pools[0].get("liquidity_usd") is not None:
                out["pool_liquidity_usd"] = pools[0]["liquidity_usd"]
        lp = lp_state(metrics)
        if lp:
            out.update(lp)
    if "lp_state" not in out and out.get("pool"):
        fallback = lp_from_pair(str(out["pool"]))
        if fallback:
            out.update(fallback)
    if facts is not None:
        if facts.lp_locked is not None:
            out["lp_locked"] = facts.lp_locked
        if facts.dangerous_perms is not None:
            out["perms"] = list(facts.dangerous_perms)
    return out
