"""站点对外的根路由：sitemap / robots.txt / llms.txt / RSS / og。

为什么不放在 `/api/v1` 下：读者与爬虫要的是 `https://站点/sitemap.xml`，
不是 `https://站点/api/v1/sitemap.xml`。这一组在 `main.create_app` 里注册在
`StaticFiles` **之前** —— SPA 的 catch-all 会把同名的路径吃掉。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import Response

from ..feed import mcp as feed_mcp
from ..feed import media, outlets

router = APIRouter()

#: 出口支持的渠道键 (AIHOT 的 `CHANNEL_KEYS`)。
_CHANNELS = ("all", "news", "x", "firstParty")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _xml(text: str) -> Response:
    return Response(content=text, media_type="application/xml; charset=utf-8")


@router.get("/sitemap.xml")
def sitemap() -> Response:
    return _xml(outlets.sitemap_xml(_now()))


@router.get("/robots.txt")
def robots() -> Response:
    return Response(content=outlets.robots_txt(), media_type="text/plain; charset=utf-8")


@router.get("/llms.txt")
def llms() -> Response:
    return Response(content=outlets.llms_txt(_now()), media_type="text/plain; charset=utf-8")


@router.get("/feeds/{channel}.xml")
def feed(channel: str) -> Response:
    if channel not in _CHANNELS:
        raise HTTPException(status_code=404, detail="没有这个渠道：%s" % channel)
    return _xml(outlets.feed_xml(_now(), channel))


#: 签名图片代理的一个字节也不该被存下来过两次 —— 缓存的那一份在调用它的那一层。
_CACHE_CONTROL = "public, max-age=604800, s-maxage=604800, immutable"


@router.get("/api/mcp")
def mcp_describe() -> dict:
    """MCP 出口 (routes/mcp) 的人读版说明: 有哪些工具、各要什么参数。

    真正的协议在 POST 上。这一条存在是因为一个只能用 curl 的出口
    不是一个能被人检查的出口。
    """
    return feed_mcp.describe()


@router.post("/api/mcp")
def mcp_call(message: dict = Body(...)) -> dict:
    """JSON-RPC 2.0: initialize / tools/list / tools/call / ping。只读, 不写库、不调模型。"""
    answer = feed_mcp.handle(message)
    return answer if answer is not None else {}


@router.get("/api/img-proxy")
def img_proxy(u: str = "", mode: str = "", exp: str = "", sig: str = "") -> Response:
    """签名图片代理 (5.9)。未签名 / 签名错 / 过期一律 403, 一个字节都不去上游取。"""
    ok, _reason, mode_name = media.verify(u, mode, exp, sig)
    if not ok:
        return Response(content="Forbidden", status_code=403,
                        media_type="text/plain; charset=utf-8",
                        headers={"Cache-Control": "no-store"})
    try:
        data = media.fetch(u)
    except media.UpstreamError:
        # 上游坏了是 502 而不是 500: 这一层没坏, 是那张图取不到, 短缓存让别人别跟着排队。
        return Response(content="Upstream image unavailable", status_code=502,
                        media_type="text/plain; charset=utf-8",
                        headers={"Cache-Control": "public, max-age=300"})
    body, content_type = media.render(data, mode_name)
    return Response(content=body, media_type=content_type or "application/octet-stream",
                    headers={"Cache-Control": _CACHE_CONTROL,
                             "X-Content-Type-Options": "nosniff"})


@router.get("/og/{item_id}")
def og(item_id: str) -> dict:
    payload = outlets.og_payload(item_id, _now())
    if payload is None:
        raise HTTPException(status_code=404, detail="没有这一条：%s" % item_id)
    return payload
