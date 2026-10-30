"""FastAPI application: /api/v1 for the SPA, web/dist for everything else.

Production is a single process on one port (decided): the SPA is built by Vite and
served from here, so there is no CORS layer and no second service to babysit.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, config
from .api.v1.router import api_router
from .monitors import schedule as monitor_schedule
from .opportunity import schedule as opportunity_schedule
from .opportunity import watchlist as opportunity_watchlist
from .pipeline import schedule
from .sources import browser
from .store import db


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    db.init_db()
    # 上一进程留下的 running 扫描没有线程在跑，启动时统一标成中断，界面不再显示僵尸。
    db.reconcile_running_scans()
    # The watchlist seed fills the table once, on the first start, and never again: after that
    # the table is the truth, deletions included (see `opportunity/watchlist.py`).
    opportunity_watchlist.ensure_seeded()
    schedule.start()
    opportunity_schedule.start()
    monitor_schedule.start()
    try:
        yield
    finally:
        monitor_schedule.stop()
        opportunity_schedule.stop()
        schedule.stop()
        browser.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Crypto Hunter API",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.include_router(api_router, prefix="/api/v1")

    if config.WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=str(config.WEB_DIST), html=True), name="web")
    else:

        @app.get("/")
        def _no_build() -> JSONResponse:
            return JSONResponse(
                {
                    "detail": "web/dist not built yet",
                    "hint": "cd web && npm install && npm run build",
                },
                status_code=503,
            )

    return app


app = create_app()
