"""Which sources a chain uses: declared in that chain's set of settings, resolved here.

A source that answers for one chain lives in that chain's folder (`arc/`, `robinhood/`);
anything that answers for more than one chain stays beside this file. The registry is the
one place that knows both, so `for_chain` can resolve a chain config's `sources:` list
without caring which kind it is asking for.
"""

from __future__ import annotations

from . import blockscout, dexscreener, geckoterminal, gmgn, goplus, sourcify
from .arc import radardex, tolly
from .robinhood import coinbarrel, peeps, pez

ALL = {
    s.name: s
    for s in (
        blockscout.Blockscout(),
        geckoterminal.GeckoTerminal(),
        radardex.RadarDEX(),
        gmgn.GmGn(),
        goplus.GoPlus(),
        sourcify.Sourcify(),
        tolly.Tolly(),
        coinbarrel.CoinBarrel(),
        pez.Pez(),
        peeps.Peeps(),
    )
}


#: Sources that are never part of the pull: they answer about one CA at a time and have no
#: chain-wide list to sweep. `decide.Engine` asks them in the per-token enrich pass, so they
#: are absent from `sources:` in the chain config on purpose - a source that returns nothing
#: from a sweep is a source that shows up in the credential row with a zero next to it.
ENRICH_ONLY = (dexscreener.DexScreener(),)


def for_chain(chain_cfg: dict) -> list:
    names = chain_cfg.get("sources") or []
    missing = [n for n in names if n not in ALL]
    if missing:
        raise KeyError(f"unknown sources in config: {missing}")
    return [ALL[n] for n in names]
