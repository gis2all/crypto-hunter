"""Chain catalogue, read from config/chains.yaml."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ... import config
from .schemas import ChainOut

router = APIRouter(prefix="/chains", tags=["chains"])

PUBLIC_FIELDS = ("id", "label", "enabled", "explorer", "sources", "schedule", "report", "pipeline")


def _public(cfg: dict) -> ChainOut:
    values = {k: cfg.get(k) for k in PUBLIC_FIELDS}
    values["sources"] = values.get("sources") or []
    return ChainOut(**values)


@router.get("", response_model=list[ChainOut])
def list_chains() -> list[ChainOut]:
    return [_public(c) for c in config.load_chains().values()]


@router.get("/{chain_id}", response_model=ChainOut)
def get_chain(chain_id: str) -> ChainOut:
    try:
        return _public(config.chain(chain_id))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown chain: {chain_id}")
