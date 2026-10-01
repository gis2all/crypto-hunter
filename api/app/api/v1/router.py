"""Aggregate router for /api/v1."""

from __future__ import annotations

from fastapi import APIRouter

from . import chains, favorites, health, lexicon, lookup, runs, settings

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(chains.router)
api_router.include_router(runs.router)
api_router.include_router(settings.router)
api_router.include_router(lexicon.router)
api_router.include_router(favorites.router)
api_router.include_router(lookup.router)
