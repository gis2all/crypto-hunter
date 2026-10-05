"""FastAPI application: /api/v1 for the SPA, web/dist for everything else.

Production is a single process on one port (decided): the SPA is built by Vite and
served from here, so there is no CORS layer and no second service to babysit.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, config
from .api.root import router as root_router
from .api.v1.router import api_router
from .feed.sources import tick as feed_tick
from .pipeline import schedule
from .sources import browser
from .store import db


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    db.init_db()
    schedule.start()
    feed_tick.start()
    try:
        yield
    finally:
        await feed_tick.stop()
        schedule.stop()
        browser.shutdown()


class _SPA(StaticFiles):
    """静态包 + 单页应用的回退 (5.20b)。

    sitemap 与 llms.txt 列的条目地址是 `/item/<id>` 这样的路径, 而 dist 里没有对应的文件 ——
    与 `StaticFiles` 的 404 混在一起, 对外公布过的地址就变成打不开。所以: 找得到的照旧,
    找不到的、且最后一段里没有点号的, 回 `index.html`, 由前端接着画。

    带点号的一律 404: 那个请求要的是一份文件, 把 index.html 喂给一个 `<script src>`
    会让"文件没了"变成"白屏 + 一个语法错", 更难查。
    """

    def get_response(self, path, scope):  # type: ignore[no-untyped-def]
        # Starlette 找不到文件时抛 404, 不是回一个 404 —— 两种情况都要接住。
        try:
            return super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        if "." in path.rsplit("/", 1)[-1]:
            raise HTTPException(status_code=404)
        try:
            return super().get_response("index.html", scope)
        except HTTPException:
            raise HTTPException(status_code=404)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Crypto Hunter API",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.include_router(api_router, prefix="/api/v1")
    # 站点对外的根路由要在 StaticFiles **之前**: SPA 的 catch-all 挂在 `/`,
    # 后注册的根路由会被它先吃掉 (sitemap / RSS / og 全部变成一页 HTML)。
    app.include_router(root_router)

    if config.WEB_DIST.is_dir():
        app.mount("/", _SPA(directory=str(config.WEB_DIST), html=True), name="web")
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
