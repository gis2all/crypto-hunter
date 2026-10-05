"""四种抓法，四种通用抓取器。

一个源抓什么由 `feed_sources.kind` 决定，抓法之外的差异全在那一行的 config 里 —— 于是接第
四十六个源是加一行数据，不是加一个函数。这里也是白名单的落点：config 里有一个键这个 kind
不认，这一轮就失败。
"""

from __future__ import annotations

from typing import Callable

from ...models import Discovery
from . import jsonlist, rss, weblist, x
from .config_keys import unknown

#: 一个抓取器拿 (源那一行, 是不是第一次导入)，回 (记录, 新的 cursor)。
#: `first_import` 只为一件事存在：RSS 的校验器在第一轮之后才存 —— 存早了，第二轮就吃
#: 一个 304 空手而归，而那一轮本该把整个窗口读一遍。
Fetcher = Callable[[dict, bool], tuple[list[Discovery], dict]]

FETCHERS: dict[str, Fetcher] = {
    "rss": rss.fetch,
    "json_list": jsonlist.fetch,
    "web_list": weblist.fetch,
    "x_search": x.fetch,
}


class ConfigRefused(RuntimeError):
    """config 里有一个这个 kind 不认的键。"""


def fetcher_for(source: dict) -> Fetcher:
    """这个源用哪个抓取器，顺带把 config 验一遍。

    验在这里、而不是在各抓取器里：一个源只有一个 kind，而验的是「这个 kind 认不认这个
    键」，与抓取本身无关。不认就抛，不静默忽略。
    """
    kind = source.get("kind", "")
    fetch = FETCHERS.get(kind)
    if fetch is None:
        raise ConfigRefused("unknown kind: %r" % (kind,))
    bad = unknown(kind, source.get("config") or {})
    if bad:
        raise ConfigRefused("unsupported config: %s" % ", ".join(bad))
    return fetch
