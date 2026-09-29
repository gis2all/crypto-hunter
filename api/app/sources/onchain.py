"""The chain itself as a source: Arc RPC plus Blockscout.

This is deliberately not listed under `sources:` in the chain's config file. That list is
the set of third-party credentials the operator sees on the settings page; a chain's
own node and explorer are not something anyone authorises, and the chain a radar
watches is already its id.

What lives here is the evidence no API sells: who holds the LP position, whether the
pool came from a launchpad or a person, whether the token is behind an EIP-1167 clone,
how much of the supply the dev still holds. Those facts decide `lp_locked`, which is
half the safety gate.
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlsplit

from .http import SourceError, UA_BLOCKSCOUT, get, json_get, pacer, pmap, tally

#: ERC-20 Transfer, and the two Uniswap-v3 pool events that name an LP position owner.
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
POOL_MINT_TOPIC = "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde"
POOL_BURN_TOPIC = "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"

#: Position managers a pool can be opened directly against. A creation transaction that
#: lands here is a person opening their own position - nobody is holding it for them.
POSITION_MANAGERS: dict[str, dict[str, str]] = {
    "arc": {
        "0x39654a85a4c05127f5fd6ed22caec077a0fb1377": "v3",
        "0x6049c9a0e26405c0985f9e3685c87d0ae917f82b": "v4",
    },
}

DEFAULT_RPC = "https://rpc.arc-scan.org"
DEFAULT_EXPLORER = "https://explorer.arc.io"

#: Function selectors that mean "the owner can hurt a holder". Presence in bytecode is
#: evidence of capability, not of intent - the report says "链上有控制函数", not "危险".
DANGEROUS_SELECTORS: tuple[tuple[str, str], ...] = (
    ("mint", "40c10f19"),
    ("blacklist", "f9f92be4"),
    ("pause", "8456cb59"),
    ("setMaxTxAmount", "ec28438a"),
    ("transferOwnership", "f2fde38b"),
    ("setTaxFee", "3a472ed2"),
)

#: EIP-1167 minimal-proxy preamble: a launchpad template, not a hand-written contract.
CLONE_PREFIX = "0x363d3d373d3d3d363d73"

_BLOCK_LOOKBACK = 150_000

#: The chain's node caps `eth_getLogs` twice: 100,000 blocks per call and 20,000
#: results. Asking for the whole lookback in one call is not a slow query, it is a
#: rejected one - and a rejected query is indistinguishable from a pool nobody ever
#: added liquidity to. So the lookback is read in windows, and a window the node still
#: refuses is halved until it fits.
LOG_WINDOW = 20_000
LOG_MIN_WINDOW = 2_000

#: How many of a pool's windows are read at once. The windows are disjoint, so which
#: answer arrives first does not change the set of owners - only the order the owners are
#: listed in does, and `pmap` keeps the asking order. Measured: one candidate whose pool
#: needed the whole lookback held the gate open for 39s, because eight windows at 2.5s
#: each were asked one after another; the node answers four at a time in about the time it
#: answers one, so this is where the round's slowest single read is spent.
LOG_WINDOW_WORKERS = 4

#: A pool is only readable this way when it is an address. Uniswap-v4 pools are named by
#: a 32-byte pool id and have no contract, so `eth_getLogs` can never answer for them -
#: asking is not slow, it is refused, and the refusal is about the query rather than its
#: width. `_logs_in` has to tell that apart from "too many logs", or it halves a query
#: that will fail at every width.
POOL_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

#: What the node says when the query itself is malformed (a pool id where an address
#: belongs). Matching on words is ugly, but the alternative - halving until the window is
#: two thousand blocks wide, thirty-one calls per window, every window - is what made one
#: candidate take ten minutes.
BAD_QUERY_MARKERS = ("invalid params", "invalid argument", "invalid variadic")


def _bad_query(error: str) -> bool:
    lowered = str(error or "").lower()
    return any(marker in lowered for marker in BAD_QUERY_MARKERS)

#: explorer.arc.io sits behind Cloudflare, which reads the TLS fingerprint rather than
#: the headers: the same URL with the same headers answers 200 to curl and 403 to httpx.
#: The explorer therefore goes out on the browser lane, the same one GMGN needs.
BLOCKSCOUT_TRANSPORT = "browser"

#: Why the launch path is unknown. "the explorer did not answer" and "the explorer has no
#: creation transaction" are two different facts: one is a retry, the other is a result.
#: The reference merged them into one string, which reads to an operator like a finding.
LAUNCH_OK = "ok"
LAUNCH_NO_POOL = "no_pool"
LAUNCH_V4 = "v4_id"
LAUNCH_NO_TX = "no_creation_tx"
LAUNCH_EXPLORER = "explorer_unreachable"
LAUNCH_NODE = "node_unreachable"
LAUNCH_NO_TARGET = "no_target"

#: What the report says for each reason that is not a finding. 可重试 tells the operator
#: this row is a flake, not a property of the token.
LAUNCH_BASIS: dict[str, str] = {
    LAUNCH_NO_POOL: "未取到交易对地址，建池路径未知",
    LAUNCH_V4: "未取到建池交易（v4 池无池合约）",
    LAUNCH_NO_TX: "未取到建池交易",
    LAUNCH_EXPLORER: "区块浏览器未响应，建池路径未知（可重试）",
    LAUNCH_NODE: "节点未响应建池交易查询（可重试）",
    LAUNCH_NO_TARGET: "建池交易无接收方，无法判断",
}


def rpc_url(chain: dict) -> str:
    return str(chain.get("rpc") or DEFAULT_RPC)


def explorer_url(chain: dict) -> str:
    return str(chain.get("explorer") or DEFAULT_EXPLORER)


def decimals(chain: dict) -> int:
    return int(chain.get("decimals") or 18)


def rpc(chain: dict, method: str, params: list, tries: int = 3) -> Any | None:
    """A JSON-RPC call. None means the node did not answer - callers record "unknown"."""
    return rpc_checked(chain, method, params, tries)[0]


def rpc_checked(chain: dict, method: str, params: list,
                tries: int = 3) -> tuple[Any | None, str]:
    """The same call, keeping the node's own words for why it said nothing.

    Silence and refusal are different facts: silence is worth another try, a refusal is a
    verdict on the query. `rpc` throws the words away, which is why a malformed `address`
    used to look exactly like a window that was too wide.
    """
    url = rpc_url(chain)
    host = urlsplit(url).netloc or "?"
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last = ""
    # Timed the way `get` times a request - one clock for the whole call - so the node
    # shows up on the round's per-host line instead of hiding inside the gate.
    started = time.monotonic()
    for attempt in range(tries):
        try:
            pacer(url).wait()
            import httpx

            resp = httpx.post(
                url,
                json=payload,
                timeout=40.0,
                headers={"Content-Type": "application/json", "User-Agent": "crypto-hunter"},
            )
            body = resp.json()
            if "result" in body:
                tally(host, "ok", time.monotonic() - started)
                return body["result"], ""
            if "error" in body:
                # The node answered. A rejected query - range too wide, too many
                # results - is a fact about the query, and asking it again is refused
                # exactly the same way. Only silence is worth a retry.
                tally(host, "ok", time.monotonic() - started)
                return None, str(body.get("error") or "").replace("\n", " ")[:200]
        except Exception as exc:
            last = repr(exc)[:200]
        tally(host, "retry")
        if attempt + 1 < tries:
            time.sleep(0.8)
    tally(host, "fail", time.monotonic() - started)
    return None, last


def rpc_batch(chain: dict, calls: list[tuple[str, list]], tries: int = 3) -> list[Any | None]:
    """Several reads in one round trip, in the order they were asked.

    The node charges about two and a half seconds per request whatever is inside it, so
    two independent reads are two full waits. Measured on Arc: two `eth_call`s one at a
    time 4.6s, the same two as one batch 3.5s - the second call is nearly free, the
    request is not. Position-manager balances are exactly that shape (the same question
    asked of the v3 and the v4 manager, neither answering the other), so they go together.

    Nothing here is retried per call: a reply that is not a list is a transport failure
    and the whole batch is asked again, while a single refused call comes back `None` -
    "the node would not answer this", which is a fact about that call.
    """
    if not calls:
        return []
    url = rpc_url(chain)
    host = urlsplit(url).netloc or "?"
    payload = [
        {"jsonrpc": "2.0", "id": index, "method": method, "params": params}
        for index, (method, params) in enumerate(calls)
    ]
    started = time.monotonic()
    for attempt in range(max(1, tries)):
        try:
            pacer(url).wait()
            import httpx

            resp = httpx.post(
                url,
                json=payload,
                timeout=40.0,
                headers={"Content-Type": "application/json", "User-Agent": "crypto-hunter"},
            )
            body = resp.json()
        except Exception:
            body = None
        if isinstance(body, list):
            tally(host, "ok", time.monotonic() - started)
            answers = {
                int(item["id"]): item.get("result")
                for item in body
                if isinstance(item, dict) and isinstance(item.get("id"), int)
            }
            return [answers.get(index) for index in range(len(calls))]
        tally(host, "retry")
        if attempt + 1 < tries:
            time.sleep(0.8)
    tally(host, "fail", time.monotonic() - started)
    return [None] * len(calls)


def blockscout_checked(chain: dict, path: str, tries: int = 3) -> tuple[dict, bool]:
    """(body, answered). `answered` is False when the explorer never replied at all.

    A 404 is the explorer answering "I do not know that address", which is a fact about the
    address; a timeout is a fact about the network. Collapsing the two is what makes a
    flaky explorer look like a token whose pool has no launch path.
    """
    url = "%s/api/v2/%s" % (explorer_url(chain).rstrip("/"), path)
    try:
        body = json_get(
            url, UA_BLOCKSCOUT, transport=BLOCKSCOUT_TRANSPORT, tries=tries, sleep=2.0,
            timeout=20.0,
        )
    except SourceError as exc:
        return {}, "HTTP 404" in str(exc)
    return (body if isinstance(body, dict) else {}), True


def blockscout(chain: dict, path: str, tries: int = 3) -> dict:
    """The body alone, for the callers that cannot act on "the explorer was down".

    `blockscout_checked` is the one to reach for when the difference matters - which is
    anywhere the answer is written into a report.
    """
    return blockscout_checked(chain, path, tries=tries)[0]


def bytecode(chain: dict, address: str) -> str:
    code = rpc(chain, "eth_getCode", [address, "latest"])
    return code if isinstance(code, str) else ""


def is_contract(chain: dict, address: str | None) -> bool | None:
    """None when the node did not answer: "no code" and "no answer" must not look alike."""
    if not address:
        return None
    code = bytecode(chain, address)
    if not code:
        return None
    return len(code) > 4


def contracts_of(chain: dict, addresses: list[str]) -> dict[str, bool | None]:
    """Which of these addresses are contracts, in one round trip.

    A pool has as many candidate owners as it has Mint/Burn events, and the LP verdict
    needs to know which of them are people. Asking one at a time was a second and a half
    of round trip each - the same per-request charge as everything else on this node - so
    they go out together. `None` keeps "no code" and "no answer" apart, the way the
    single-address read does.
    """
    if not addresses:
        return {}
    answers = rpc_batch(chain, [("eth_getCode", [address, "latest"]) for address in addresses])
    out: dict[str, bool | None] = {}
    for address, code in zip(addresses, answers):
        out[address] = None if not isinstance(code, str) or not code else len(code) > 4
    return out


def call(chain: dict, to: str, data: str) -> str | None:
    result = rpc(chain, "eth_call", [{"to": to, "data": data}, "latest"])
    return result if isinstance(result, str) else None


def _balance_of(chain: dict, token: str, who: str) -> float | None:
    data = "0x70a08231" + who.lower()[2:].rjust(64, "0")
    raw = call(chain, token, data)
    try:
        return int(raw, 16) / float(10 ** decimals(chain))
    except (TypeError, ValueError):
        return None


def supply(chain: dict, ca: str) -> float | None:
    raw = call(chain, ca, "0x18160ddd")
    try:
        return int(raw, 16) / float(10 ** decimals(chain))
    except (TypeError, ValueError):
        return None


def dev_balance(chain: dict, ca: str, creator: str) -> float | None:
    return _balance_of(chain, ca, creator) if creator else None


def nft_balances(chain: dict, managers: dict[str, str], who: str | None) -> dict[str, int | None]:
    """How many position NFTs each manager holds for `who`, in one round trip.

    `managers` is address -> label. `None` per label means the node did not answer, which
    the LP verdict reads as "not established" rather than as a zero.
    """
    if not who:
        return {label: None for label in managers.values()}
    data = "0x70a08231" + who.lower()[2:].rjust(64, "0")
    addresses = list(managers)
    answers = rpc_batch(
        chain, [("eth_call", [{"to": address, "data": data}, "latest"]) for address in addresses],
    )
    out: dict[str, int | None] = {}
    for address, raw in zip(addresses, answers):
        try:
            out[managers[address]] = int(raw, 16)
        except (TypeError, ValueError):
            out[managers[address]] = None
    return out


def proxy_kind(chain: dict, ca: str) -> str | None:
    """`eip1167` for a launchpad clone, `direct` for a contract deployed as written."""
    code = bytecode(chain, ca)
    if not code:
        return None
    return "eip1167" if code.lower().startswith(CLONE_PREFIX) else "direct"


def dangerous_selectors(code: str) -> list[str]:
    """Which known-dangerous functions a token's bytecode actually contains."""
    lowered = (code or "").lower()
    return [name for name, selector in DANGEROUS_SELECTORS if selector in lowered]


def _logs_in(chain: dict, pool: str, start: int, stop: int) -> list:
    """Mint/Burn logs in one block window, halved while the node refuses the width."""
    query = {"fromBlock": hex(start), "toBlock": hex(stop), "address": pool,
             "topics": [[POOL_MINT_TOPIC, POOL_BURN_TOPIC]]}
    logs, error = rpc_checked(chain, "eth_getLogs", [query])
    if logs is not None:
        return logs
    if _bad_query(error):
        # The node rejected the shape of the query, not its width. Every narrower window
        # would be refused for the same reason, so halving here only spends calls: a v4
        # pool id is not an address and never will be, however small the range.
        return []
    # A rejection of the width is a verdict on how much of the chain is being asked about.
    # Half of it is a narrower question, and the node keeps answering those until the
    # window is small enough to fit its result cap.
    width = stop - start
    if width <= LOG_MIN_WINDOW:
        return []
    middle = start + width // 2
    return _logs_in(chain, pool, start, middle) + _logs_in(chain, pool, middle, stop)


def _pool_logs(chain: dict, pool: str, latest: int, blocks: int) -> list:
    """Every Mint/Burn log for one pool in the lookback, newest window first.

    The windows do not depend on each other, so they are asked at the same time; the
    order they come back in is the order they were asked (see `pmap`), which is what
    keeps the owner list - and the words the report prints about the first owner - stable.
    """
    floor = max(0, latest - blocks)
    windows: list[tuple[int, int]] = []
    stop = latest
    while stop > floor:
        start = max(floor, stop - LOG_WINDOW)
        windows.append((start, stop))
        stop = start
    out: list = []
    for logs in pmap(lambda span: _logs_in(chain, pool, span[0], span[1]), windows,
                     workers=LOG_WINDOW_WORKERS):
        out.extend(logs or [])
    return out


def lp_holders(chain: dict, pool: str | None, blocks: int = _BLOCK_LOOKBACK) -> list[str]:
    """Everyone who ever opened or closed an LP position in this pool.

    Read from the pool's Mint/Burn events rather than from a position-manager query.
    A pool named by a 32-byte id - the v4 shape - has no contract to ask and no logs to
    read: the position is an NFT in a manager, and `POSITION_MANAGERS` is where that
    question is asked. Returning early keeps that fact out of the node's error path.
    """
    if not pool or not POOL_ADDRESS_RE.match(str(pool)):
        return []
    head = rpc(chain, "eth_blockNumber", [])
    try:
        latest = int(head, 16)
    except (TypeError, ValueError):
        return []
    owners: list[str] = []
    for entry in _pool_logs(chain, pool, latest=latest, blocks=blocks):
        topics = entry.get("topics") or []
        if not topics:
            continue
        if topics[0] == POOL_MINT_TOPIC and len(topics) > 2:
            owner = "0x" + topics[2][-40:]
        elif topics[0] == POOL_BURN_TOPIC and len(topics) > 1:
            owner = "0x" + topics[1][-40:]
        else:
            continue
        if owner not in owners:
            owners.append(owner)
    return owners


def launch_path(chain: dict, pool: str | None) -> tuple[str | None, bool | None, str, str]:
    """How the pool was created: (target, target is a contract, contract name, reason).

    A launchpad pool is created by a factory; a personal pool is created by calling the
    position manager directly. A v4 pool id is not an address, so there is nothing to ask.
    The fourth element says why an unknown is unknown - see `LAUNCH_BASIS`.
    """
    if not pool:
        return None, None, "", LAUNCH_NO_POOL
    if len(str(pool)) != 42:
        return None, None, "", LAUNCH_V4
    address_info, answered = blockscout_checked(chain, "addresses/%s" % pool)
    if not answered:
        return None, None, "", LAUNCH_EXPLORER
    tx_hash = address_info.get("creation_transaction_hash")
    if not tx_hash:
        return None, None, "", LAUNCH_NO_TX
    tx = rpc(chain, "eth_getTransactionByHash", [tx_hash])
    if not isinstance(tx, dict):
        return None, None, "", LAUNCH_NODE
    target = tx.get("to") or ""
    if not target:
        return None, None, "", LAUNCH_NO_TARGET
    target_is_contract = is_contract(chain, target)
    name = ""
    if target_is_contract:
        info, _answered = blockscout_checked(chain, "smart-contracts/%s" % target)
        name = info.get("name") or ""
    return target, target_is_contract, name, LAUNCH_OK


#: How much of the LP supply the holder table has to cover before it counts as an answer,
#: and how much of it has to sit out of reach before the LP counts as locked. One number
#: for both: a table covering less of the LP than this is truncated, not "unlocked".
LP_LOCK_SHARE = 50.0


def _out_of_reach(holder: dict) -> bool:
    """A locker, a vault, the launchpad's own holder, a burn address - anything that is not
    a wallet someone can spend from."""
    return bool(holder.get("locked") or holder.get("burnt") or holder.get("contract"))


def lp_lock_from_holders(lp_holders: list[dict] | None) -> tuple[bool | None, str]:
    """The LP question answered from the holder table instead of from the pool contract.

    `lock_facts` asks the pool's own contract, and a v4 pool has none: the position lives
    as an NFT inside a manager, so the chain read comes back with "未取到建池交易" and the
    card says 未判定 for pools that are in fact locked. This reads the same question off the
    LP supply instead.

    Two states and no maybe: the majority of the LP supply out of reach is 已锁, the
    majority of it in a wallet is 未锁. `None` only when the table covers too little of the
    LP to say which - which is not a state, it is a missing answer.
    """
    holders = [holder for holder in (lp_holders or []) if isinstance(holder, dict)]
    covered = sum(holder.get("pct") or 0.0 for holder in holders)
    if covered < LP_LOCK_SHARE:
        return None, ""
    out_of_reach = sum(holder.get("pct") or 0.0 for holder in holders if _out_of_reach(holder))
    locked = (out_of_reach * 100.0 / covered) >= LP_LOCK_SHARE
    side = [holder for holder in holders if _out_of_reach(holder) is locked]
    top = max(side, key=lambda holder: holder.get("pct") or 0.0)
    share = out_of_reach if locked else covered - out_of_reach
    basis = "GoPlus：LP %.2f%% 在%s %s…" % (
        share, "合约" if locked else "钱包", str(top.get("address") or "")[:10],
    )
    return locked, basis


def lock_facts(chain: dict, *, ca: str, pair: str | None, creator: str,
               launchpad: str | None) -> tuple[bool | None, str, dict]:
    """Is the liquidity locked, and what says so.

    Returns `(locked, basis, evidence)`. `None` is a real answer: "could not establish"
    is what keeps this out of the pass column rather than quietly counting as safe.
    """
    managers = POSITION_MANAGERS.get(str(chain.get("id") or ""), {})
    evidence: dict[str, Any] = {"lp_owners": [], "eoa_owners": [], "position_nfts": {}}

    owners = lp_holders(chain, pair)
    evidence["lp_owners"] = owners[:4]
    eoa = [owner for owner, is_code in contracts_of(chain, owners).items() if is_code is False][:3]
    evidence["eoa_owners"] = eoa

    evidence["position_nfts"] = nft_balances(chain, managers, creator)
    held_nft = [label for label, count in evidence["position_nfts"].items() if count]

    target, target_is_contract, target_name, reason = launch_path(chain, pair)
    evidence["launch_target"] = target
    evidence["launch_target_name"] = target_name
    evidence["launch_reason"] = reason

    if eoa or held_nft:
        who = (eoa[0] if eoa else creator)[:10]
        return False, "LP 头寸在 %s…（%s）" % (who, "EOA" if eoa else "创建者"), evidence
    if target and target.lower() in managers:
        return False, "建池交易直达 %s 头寸管理器（个人自建）" % managers[target.lower()], evidence
    if target_is_contract:
        return True, "建池路径：发射台合约 %s" % (target_name or (str(target)[:10] + "…")), evidence
    if target:
        return None, "建池方为 EOA %s…（无法判断是否已锁）" % str(target)[:10], evidence
    if launchpad:
        return True, "发射台 %s 路径发行，创建者未持有头寸" % launchpad, evidence
    # The reason decides the words. A flaky explorer must not be written down as a
    # missing launch transaction: the first one is worth retrying, the second one is a
    # fact about the token, and the report is what an operator acts on.
    return None, LAUNCH_BASIS.get(reason, "未取到建池交易"), evidence
