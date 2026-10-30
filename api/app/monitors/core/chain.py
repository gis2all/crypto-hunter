"""On-chain facts for one monitor card: supply, market and the safety flags (2026-10-30).

The monitor's cards are about mints that may not have a pool yet, so three sources are asked and
each may answer nothing: the supply is read straight off the contract, the market comes from
DexScreener, and the safety sheet from GoPlus. A missing answer stays missing - the card prints
未判定, never 0.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache

import httpx

from ...sources import dexscreener, goplus

log = logging.getLogger("crypto-hunter.monitors")

#: BSC's public seed node. One `eth_call` per card; a mint sweep is a dozen of them.
BSC_RPC = "https://bsc-dataseed.binance.org"
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
        found = _ADDRESS.search(str(text or ""))
        if found:
            return found.group(0).lower()
    return ""


def _call(ca: str, data: str, *, timeout: float = 8.0) -> str | None:
    """One `eth_call`, raw hex back. None means the node or the contract did not answer."""
    global _CLIENT
    body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": ca, "data": data}, "latest"]}
    try:
        if _CLIENT is None:
            _CLIENT = httpx.Client(timeout=timeout)
        reply = _CLIENT.post(BSC_RPC, json=body, timeout=timeout).json()
    except Exception as exc:  # noqa: BLE001 - an unanswered node is a reading, not a failure
        log.info("monitor: %s(%s) did not answer: %r", data, ca, exc)
        return None
    value = str((reply or {}).get("result") or "")
    return value if value.startswith("0x") and len(value) > 2 else None


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


def max_supply(ca: str, *, minted: int | None = None, timeout: float = 8.0) -> int | None:
    """The cap, when the contract really means a share cap.

    A non-standard getter can answer something else entirely - JAGENT's `maxSupply()` slot
    returned a wei-scale number that is not a share count. A share cap above a trillion is not
    believable, so it is ignored and the site's number is used instead.
    """
    for selector in MAX_SELECTORS:
        value = _uint(ca, selector, timeout=timeout)
        if value and 0 < value <= 10**12 and (minted is None or value >= minted):
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


def token_first(*addresses: str, limit: int = 6) -> dict:
    """The first of these addresses that really is a token.

    A page lists many addresses (mint pass, forge, emitter, the token); only one of them is
    the token, and it is verified on-chain rather than trusted because it was printed on the
    site (2026-10-30, user).
    """
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


def probe(ca: str, chain: str = "bsc") -> dict:
    """Everything the card's 链上 row prints. Missing answers are simply absent."""
    out: dict = {"contract": ca}

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

    # 已铸造优先问合约自己：没有标准方法，所以按一组常见 getter 依次试，第一个答上来的算数。
    minted = None
    minted_by = ""
    for selector, getter in MINT_GETTERS:
        value = _uint(ca, selector)
        if value is not None:
            minted, minted_by = value, getter
            break
    raw = total_supply(ca)
    scale = decimals(ca)
    nft = is_erc721(ca)
    if nft is True:
        # 卡片的类型判定用它：721 的 totalSupply 是「份数」，不是代币总量。
        out["nft"] = True
    # 721 的 totalSupply 同样是「已铸造份数」，可以直接当分子。
    done = minted if minted is not None else (raw if nft else None)
    if done is not None:
        out["minted"] = done
        if minted_by:
            out["minted_by"] = minted_by
        cap = max_supply(ca, minted=done)
        if cap:
            out["mint_total"] = cap
    if raw is not None and not nft:
        # 20 的 totalSupply 是代币总量，不是卖出了多少份 —— 拿它当进度会差好几个数量级。
        out["kind"] = "erc20"
        out["token_total"] = raw // (10 ** scale) if scale is not None else raw
        if scale is not None:
            out["decimals"] = scale
    # 交易站只认代币：铸造合约 ≠ 代币（JAGENT 的 `token()` 指向另一个 ERC-20，2026-10-30）。
    token_meta = token_of(ca)
    if token_meta:
        out.update(token_meta)
    # 增发要分两种：合约还有 owner（有人能单方面增发）和没有 owner（公开铸造，谁也关不掉）。
    owner = str(getattr(facts, "owner_address", "") or "") if facts is not None else ""
    if not owner:
        value = _call(str(token_meta.get("token") or ca), OWNER)
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
        # 池子数量是「回答 0 个池子」还是「没回答」的分界，撤池要靠它分辨（2026-10-30）。
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
        # GoPlus 没给 LP 表时问池子自己：空池 / 烧掉或锁在已知锁仓合约里的份额。
        fallback = lp_from_pair(str(out["pool"]))
        if fallback:
            out.update(fallback)
    if facts is not None:
        if facts.lp_locked is not None:
            out["lp_locked"] = facts.lp_locked
        # 空列表是「问过、没有危险权限」，不能和「没回答」混成一个空值。
        if facts.dangerous_perms is not None:
            out["perms"] = list(facts.dangerous_perms)
    return out
