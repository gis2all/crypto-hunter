"""Reading a chain off a page we already hold.

Two callers use this: extraction, on the pages the trace already read, and the 补链 stage, on
the results of a web search. The signal is deliberately narrow - an explorer host, a chain-shaped
address, or an explicit "built on / X chain" phrase. A page that does not say which chain it is
on leaves the entry 未判定, which is the honest answer.
"""

from __future__ import annotations

import html
import logging
import re

log = logging.getLogger("crypto-hunter.opportunity")

#: An explorer host names its chain better than any sentence does.
EXPLORERS = {
    "etherscan.io": "ethereum",
    "bscscan.com": "bsc",
    "basescan.org": "base",
    "arbiscan.io": "arbitrum",
    "tronscan.org": "tron",
    "solscan.io": "solana",
    "suiscan.xyz": "sui",
    "snowtrace.io": "avax",
    "explorer.aleo.org": "aleo",
    "explorer.near.org": "near",
    "mintscan.io": "cosmos",
    "starkscan.co": "starknet",
    "explorer.berachain.com": "berachain",
}

#: Address shapes that belong to exactly one chain. `0x…` is intentionally absent: an EVM
#: address is shared by every EVM chain, so it proves the family and not the chain.
ADDRESSES = (
    (re.compile(r"\baleo1[0-9a-z]{40,}\b"), "aleo"),
    (re.compile(r"\bbc1[02-9ac-hj-np-z]{25,}\b", re.I), "bitcoin"),
    (re.compile(r"\bT[1-9A-HJ-NP-Za-km-z]{33}\b"), "tron"),
)

#: Names, not tickers: `sol`, `eth` and `atom` are ordinary words on a landing page. A bare
#: name only counts with a chain-shaped suffix (`base chain`), never on its own - `database`
#: and `based on` are why.
NAMES = {
    "bitcoin": ("bitcoin",),
    "ethereum": ("ethereum",),
    "solana": ("solana",),
    "base": ("base",),
    "bsc": ("bsc", "bnb chain", "binance smart chain"),
    "arbitrum": ("arbitrum one", "arbitrum"),
    "sui": ("sui",),
    "aptos": ("aptos",),
    "near": ("near protocol", "near"),
    "avax": ("avalanche", "avax"),
    "cosmos": ("cosmos",),
    "starknet": ("starknet",),
    "berachain": ("berachain",),
    "megaeth": ("megaeth",),
    "tron": ("tron",),
    "hyperliquid": ("hyperliquid",),
    "flare": ("flare",),
    "aleo": ("aleo",),
    "injective": ("injective",),
    "multiversx": ("multiversx", "elrond"),
    "ronin": ("ronin",),
    "bttc": ("bttc", "bit torrent chain"),
    "somnia": ("somnia",),
    "pharos": ("pharos",),
    "bittensor": ("bittensor", "tao"),
    "robinhood": ("robinhood chain", "robinhood"),
    "stacks": ("stacks",),
    "x layer": ("x layer", "xlayer"),
    "osmosis": ("osmosis",),
    "pi": ("pi network",),
    "stellar": ("stellar",),
    "ton": ("toncoin", "ton blockchain"),
    "optimism": ("op mainnet", "optimism"),
    "polygon": ("polygon",),
    "sei": ("sei network",),
    "monad": ("monad",),
    "unichain": ("unichain",),
    "linea": ("linea",),
    "scroll": ("scroll",),
    "blast": ("blast",),
    "mantle": ("mantle",),
    "zksync": ("zksync",),
    "celo": ("celo",),
    "cardano": ("cardano",),
    "polkadot": ("polkadot",),
    "filecoin": ("filecoin",),
}

#: Longest first, so `robinhood chain` wins over `robinhood`.
_ORDERED = sorted(((alias, chain) for chain, aliases in NAMES.items() for alias in aliases),
                  key=lambda item: -len(item[0]))

_VERB = r"(?:built|deployed|running|live|launched|available|operating|settled|issued)"
_SUFFIX = r"(?:chain|network|mainnet|testnet|ecosystem|l1|l2)"
_CN_BUILT = r"(?:基于|部署在|运行在|构建在|发在)"


def visible(raw: str) -> str:
    """The page as a reader sees it: tags dropped, entities decoded, whitespace collapsed."""
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw or "")
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", html.unescape(body)).strip()


def signal(text: str, raw: str = "") -> str:
    """The chain this page names, or `""` when it names none.

    Strong evidence only, in order: an explorer host, an address shape that belongs to one
    chain, then an explicit phrase. A bare name is never enough on its own.
    """
    lowered = (raw or "").lower()
    for host, chain in EXPLORERS.items():
        if host in lowered or host in (text or "").lower():
            return chain
    for pattern, chain in ADDRESSES:
        if pattern.search(text or ""):
            return chain
    body = text or ""
    low = body.lower()
    for alias, chain in _ORDERED:
        word = re.escape(alias)
        if re.search(r"\b" + _VERB + r"\s+on\s+(?:the\s+)?" + word + r"\b", low):
            return chain
        if re.search(r"\b" + word + r"\s+" + _SUFFIX + r"\b", low):
            return chain
        if re.search(_CN_BUILT + r"\s*" + word, body, re.I):
            return chain
    return ""


def from_pages(pages) -> str:
    """The chain named by the pages a trace already read, or `""`.

    The trace stores what it read and a verdict, never the chain - the question was not asked.
    Asking it here costs nothing: the pages are already in the material.
    """
    for body in pages or []:
        chain = signal(visible(str(body or "")))
        if chain:
            return chain
    return ""


