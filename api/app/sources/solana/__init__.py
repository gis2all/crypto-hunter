"""Solana-only sources.

GMGN, GeckoTerminal and DexScreener all answer about Solana and stay in `sources/` beside
this folder, the same way they answer about Arc and Robinhood. What lives here is the read
that only exists on this chain: the mint account, which is where the two authorities that
can rug a holder are recorded (`mint.py`). A chain-specific half of a radar goes in a
folder like this one; anything that answers for more than one chain does not.
"""
