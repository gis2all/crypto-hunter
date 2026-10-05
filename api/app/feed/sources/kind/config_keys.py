"""每个 kind 认哪些 config 键。

不认的键让这一轮失败，而不是落回通用解析 —— 一个拼错的 `itemSelector` 会让选择器变成空
的，于是整个列表页被当成一篇正文、日期永远找不到，而每一步都「成功」。雷达那边
`pipeline.scoring.product_weight` / `unlisted_policy` 两个键从来没生效过，就是这个毛病。
"""

from __future__ import annotations

#: 与抓法无关的那几个：传输方式、额外的请求头、首次导入的上限。
#:
#: `allowUrlPrefixes` / `denyUrlPrefixes` 曾经在这里，但从来没有一处读过它们 —— 一个写在白名单
#: 里、却被忽略的键比一个拼错的键更坏：它看起来设过了。要按 URL 挑，`web_list` 的
#: `linkPrefix` / `linkContains` 就是做这件事的；不够用的时候再加回这里，连带实现。
COLLECTED = (
    "transport",            # http | browser（curl_cffi 冒充 Chrome，过 Cloudflare 用）
    "headers",
    "initialBackfillLimit",
)

KEYS: dict[str, tuple[str, ...]] = {
    # `summaryIsBody`: 这份 feed 只把正文写在 `description` 里 (与 AIHOT 同名 config 键)。
    "rss": COLLECTED + ("feedUrl", "baseUrl", "maxItems", "summaryIsBody"),
    # 不装 CSS 引擎：这些列表页真正需要的是「哪些链接是文章」，而一个只认前缀的
    # 匹配器说得清、测得动，一套残缺的选择器语法两样都不行。
    "web_list": COLLECTED + (
        "url", "baseUrl", "linkPrefix", "linkContains", "titleMinLength", "maxItems",
    ),
    "json_list": COLLECTED + (
        "url", "itemsPath", "titlePaths", "summaryPaths", "authorPaths",
        "publishedAtPath", "publishedAtUnit", "externalIdPath", "urlTemplate",
        "urlTemplateFallback", "maxItems",
    ),
    # X 的账号名是它唯一的配置：读哪几个账号、怎么读，都在 search/engines/xsearch.py 里。
    "x_search": ("handle",),
}


def unknown(kind: str, config: dict) -> list[str]:
    """这些键这个 kind 不认识。空表示全都认识。"""
    known = KEYS.get(kind)
    if known is None:
        return [kind]
    return sorted(key for key in (config or {}) if key not in known)
