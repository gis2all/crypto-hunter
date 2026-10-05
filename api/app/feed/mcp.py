"""MCP 出口 (routes/mcp, AIHOT 的 `packages/contracts/src/mcp.ts`)。

一个**只读**的 MCP 服务端, 挂在 `/api/mcp`: JSON-RPC 2.0, 四个方法 (`initialize` /
`tools/list` / `tools/call` / `ping`)。工具就是我们自己页面上那几个读口 —— 工具表在
这里定义一次, 页面与模型读的是同一条路, 所以“模型看到的信息”不会是另一个产品。

为什么只读: 对外的工具不叫任何付费的东西, 也不改库。一个能被一串 token 叫醒的“跑一轮采集”
就是一个任何人都能按的计费按钮。

这里不做 SSE 也不做会话: 每一条消息都是一次独立的 POST, 回一个
JSON-RPC 对象。一个无状态的只读服务端没有需要回话的东西。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from . import outlets, snapshot_heat, views

#: 回答给客户端的协议版本。客户端说它自己的, 我们回我们支持的 ——
#: 版本差异归客户端判断, 而不是让这一层去猜对方是谁。
PROTOCOL = "2025-06-18"
SERVER = "crypto-hunter-feed"


class McpError(Exception):
    """一条 JSON-RPC 错误。`code` 用协议自己的那几个 (与 JSON-RPC 一致)。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hot(args: dict) -> dict:
    return snapshot_heat.hot_payload(_now(), limit=int(args.get("limit") or 10))


def _items(args: dict) -> dict:
    return views.items_view(
        _now(), scope=str(args.get("scope") or "all"), category=str(args.get("category") or ""),
        channel=str(args.get("channel") or "all"), query=str(args.get("query") or ""),
        page=int(args.get("page") or 1), cursor=str(args.get("cursor") or ""),
        page_size=int(args.get("page_size") or 0))


def _item(args: dict) -> dict:
    identifier = str(args.get("id") or "")
    found = views.find_item(_now(), identifier)
    if found is None:
        raise McpError(-32602, "没有这一条：%s" % identifier)
    item, story = found
    return views.item_detail(item, story, _now())


def _story(args: dict) -> dict:
    identifier = str(args.get("id") or "")
    found = views.find_story(_now(), identifier)
    if found is None:
        raise McpError(-32602, "没有这一件：%s" % identifier)
    story, rank = found
    return views.story_detail(story, rank, _now())


def _topics(args: dict) -> dict:
    return views.topics_view(_now())


def _site(args: dict) -> dict:
    return outlets.site_meta()


#: 工具表。`inputSchema` 是 JSON Schema —— 模型看的就是它, 所以它就是契约。
TOOLS: tuple[dict, ...] = (
    {
        "name": "feed_hot",
        "description": "热点榜：同一批条目先聚成事件，再按热度排。回名次、标题、热度与参与者。",
        "inputSchema": {"type": "object",
                        "properties": {"limit": {"type": "integer", "minimum": 1,
                                                 "maximum": 30, "default": 10}},
                        "additionalProperties": False},
        "run": _hot,
    },
    {
        "name": "feed_items",
        "description": "全部动态 (scope=all) 与精选 (scope=curated)，按天分组，带游标分页。",
        "inputSchema": {"type": "object",
                        "properties": {"scope": {"type": "string",
                                                 "enum": ["all", "curated"], "default": "all"},
                                       "category": {"type": "string"},
                                       "channel": {"type": "string",
                                                   "enum": ["all", "news", "x", "firstParty"]},
                                       "query": {"type": "string"},
                                       "page": {"type": "integer", "minimum": 1},
                                       "cursor": {"type": "string"},
                                       "page_size": {"type": "integer"}},
                        "additionalProperties": False},
        "run": _items,
    },
    {
        "name": "feed_item",
        "description": "一条的整页：正文、理解、来源、合格明细。参数是条目 id。",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}},
                        "required": ["id"], "additionalProperties": False},
        "run": _item,
    },
    {
        "name": "feed_story",
        "description": "一件事的整页：综述、名次、作者、后续报道。参数是事件 id。",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}},
                        "required": ["id"], "additionalProperties": False},
        "run": _story,
    },
    {
        "name": "feed_topics",
        "description": "主题目录：实体名录分组，每组带一个窗口内的条数。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "run": _topics,
    },
    {
        "name": "feed_site",
        "description": "站点元信息：名字、说明、静态页与联系方式。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "run": _site,
    },
)

_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def describe() -> dict:
    """工具清单。`GET /api/mcp` 还的就是它 —— 一个人也能读的一页说明。"""
    return {"protocolVersion": PROTOCOL, "server": SERVER,
            "transport": "json-rpc over POST /api/mcp",
            "tools": [{"name": tool["name"], "description": tool["description"],
                       "inputSchema": tool["inputSchema"]} for tool in TOOLS]}


def call(name: str, arguments: dict) -> dict:
    """调一个工具。回 MCP 的那个包装形状: 一段给模型读的文本 + 一份结构化的.

    两份都回, 因为模型读文本、程序读结构 —— 只给一份的话, 另一边就要去猜 JSON 里哪一段是给它的。
    """
    tool = _BY_NAME.get(name)
    if tool is None:
        raise McpError(-32602, "没有这个工具：%s" % name)
    try:
        payload = tool["run"](arguments or {})
    except (KeyError, ValueError, TypeError) as exc:
        raise McpError(-32602, "参数读不懂：%s" % exc) from exc
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "structuredContent": payload, "isError": False}


def handle(message: dict) -> dict | None:
    """一条 JSON-RPC 进, 一条出。通知 (`notifications/*`, 没有 `id`) 回 None。"""
    method = str(message.get("method") or "")
    ident = message.get("id")
    if ident is None and method.startswith("notifications/"):
        return None
    try:
        if method == "initialize":
            result: dict = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                            "serverInfo": {"name": SERVER, "version": "1"}}
        elif method == "tools/list":
            result = {"tools": describe()["tools"]}
        elif method == "tools/call":
            params = message.get("params") or {}
            result = call(str(params.get("name") or ""), params.get("arguments") or {})
        elif method == "ping":
            result = {}
        else:
            raise McpError(-32601, "没有这个方法：%s" % method)
    except McpError as exc:
        return {"jsonrpc": "2.0", "id": ident,
                "error": {"code": exc.code, "message": exc.message}}
    return {"jsonrpc": "2.0", "id": ident, "result": result}
