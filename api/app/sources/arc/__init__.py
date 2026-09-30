"""Sources that exist for one chain only.

RadarDEX and Tolly are Arc products: they index Arc, they answer about Arc, and asking
them about another chain returns the Arc rollout byte for byte (measured 2026-09-30 -
`?chain=robinhood` came back with the same CA set as the default call, overlap with
GeckoTerminal's robinhood pools: 0). The chain-specific half of a radar lives in a folder
like this one; anything that answers for more than one chain stays in `sources/`.
"""
