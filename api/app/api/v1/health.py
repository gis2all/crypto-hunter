"""Liveness and environment probe. Cheap: touches no chain data."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter

from ... import __version__, config, llm
from ...store import db
from .schemas import HealthOut

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    ok = True
    error: str | None = None
    try:
        conn = db.connect()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - environment dependent
        ok = False
        error = repr(exc)[:200]
    return HealthOut(
        status="ok" if ok else "degraded",
        version=__version__,
        time=datetime.now().astimezone().isoformat(timespec="seconds"),
        db=ok,
        db_error=error,
        db_path=str(config.DB_PATH),
        web_dist=config.WEB_DIST.is_dir(),
        llm=llm.configured(),
        llm_model=llm.settings()["model"] if llm.configured() else "",
        chains=[c["id"] for c in config.enabled_chains()],
    )
