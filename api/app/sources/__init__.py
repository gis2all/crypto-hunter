"""Data-source adapters.

One folder per chain for the sources that belong to it (`arc/`, `robinhood/`), and one file
beside this one for every source that answers for more than one chain: GMGN, GeckoTerminal,
DexScreener, GoPlus, Sourcify, Blockscout. A chain-specific source sits in its chain's folder
because that is where the proof it is chain-specific lives - see `arc/SOURCES.md`.

Sources never know about each other: the pipeline is what combines them. Endpoint shapes are
filled in by the step-0 probe; until then adapters declare their intent and raise rather than
inventing fields.
"""
