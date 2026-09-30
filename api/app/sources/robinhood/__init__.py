"""Robinhood-only sources.

The chain-specific half of a radar lives in a folder like this one. CoinBarrel, PEZ and
Peeps index Robinhood and answer about Robinhood; none of them has a list for another
chain, and asking RadarDEX or Tolly about Robinhood returns the Arc rollout byte for byte
(measured 2026-09-30). Anything that answers for more than one chain - GMGN, GeckoTerminal,
DexScreener, GoPlus, Sourcify - stays in `sources/` beside this folder.
"""