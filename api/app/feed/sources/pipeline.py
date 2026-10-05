"""信息流的全局旋钮: 所有源共用的那几个数。

一个源自己的事写在 `config/feed/sources.yaml` 那一行里, 由 `store.py` 导进表; 这个文件放的是
跨源的那一格 ——「这一轮最多为几篇去抓详情页」不属于任何一个源, 挂在谁身上都别扭。

认不出的键让这一轮失败, 与 `kind/config_keys.py` 同一句理由: 写在文件里、却被忽略的键, 比一个
拼错的键更坏 —— 它看起来设过了。

**没有缓存**: 这一层每轮 tick 读一次。一个小文件的 IO, 与「改了要重启才知道生效」相比, 前者
更好解释; 缓存的那些代码最后都长成「为什么我改了没用」。
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml.comments import CommentedMap

from ... import config

#: 出厂值。文件不见了也用这一份 —— 一台刚拉下来的机器不该因为少一个旋钮文件就不补正文。
DEFAULTS: dict[str, dict[str, Any]] = {
    "detail": {
        "maxFetches": 40,
        "minBodyChars": 200,
        "timeoutSeconds": 20,
        "retryHours": 24,
    },
    #: 写作站 (feed/writing.py)。这一格是整条流水线上唯一会花钱的一步, 所以它的闸门最全:
    #: 一轮多少条、在飞多少条、单条超时、失败之后隔多久再问、连着失败几次收手。
    #: `maxItems: 0` 就是"这一站关掉" —— 关掉之后页面照样有东西可画, 印的是规则版。
    "llm": {
        #: 三档预设 (见下面 PRESETS): 一个下拉决定四个站跑哪几档、正文翻译走哪条路。
        #: 出厂值按 standard —— 一台刚拉下来的机器先跑便宜的那一档, 想全开的人在页面上选。
        "preset": "standard",
        "maxItems": 40,
        "concurrency": 4,
        "timeoutSeconds": 60,
        "retryHours": 24,
        "maxFailures": 3,
    },
    #: 出刊节奏 (DESIGN.md 3.4): 三种刊各一个**北京时间**的时刻, 到点出刚过去的那一期 ——
    #: 08:00 的日报讲昨天, 周一的周报讲上周 (窗口是一个完整的日子/周/月, 出刊那一刻那一期才
    #: 刚开始, 印出来是半张纸)。**留空 = 这一期不出**: 一种刊的开关就是它有没有时刻, 不用第二个
    #: 布尔 —— 两个都能表达同一件事的地方, 迟早互相矛盾。周报在周一、月报在 1 日, 这两件事由
    #: 窗口自己的边界定, 这一格只管「几点」。
    "schedule": {
        "day": "08:00",
        "week": "08:00",
        "month": "08:00",
    },
}

PATH = config.ROOT / "config" / "feed" / "pipeline.yaml"


#: 三档预设, 页面上是「节省 / 标准 / 完整」一个下拉。
#:
#: 为什么是**预设**而不是十来个独立开关: 这些键互相牵着走。分组站的入口条件是「有 title_zh」,
#: 而 title_zh 是写作站写的 (`db.feed_items_for_grouping` 的 WHERE) —— 把写作站整个关掉,
#: 分组站就一条也排不进来, 热点榜退回规则分桶, 综述站跟着没材料, 最后报刊拿到的是规则版清单.
#: 四个开关各自看都对, 合起来是一片空白. 所以这里把「哪几档配哪几档」定死在一张表里.
#:
#: `translate` 是两条路: `local` = 本地 Argos (不花钱, 只译纯文字块),
#: `llm` = 模型整篇译. 一条调用的价钱从高到低是: 评分 x2 / 理解 / 预筛 / 结构 / 摘要 ——
#: 所以便宜档留到最后砍.
PRESETS: dict[str, dict[str, Any]] = {
    #: 只留报刊. 四站全关, 正文与引用走本地 —— 每天一次调用, 页面回到规则版.
    "saver": {"prefilter": 0, "structure": 0, "score": 0, "summarize": 0, "understand": False,
              "group": False, "digest": False, "translate": "local"},
    #: 结构 + 摘要 + 分组 + 综述 (推荐). 砍掉的是评分两次、理解档与预筛; 留下的是没有它页面
    #: 就散架的那几样: 中文标题 (分组站的入场券)、事件归组、事件综述、正文与引用的本地译文.
    "standard": {"prefilter": 0, "structure": 1, "score": 0, "summarize": 1, "understand": False,
                 "group": True, "digest": True, "translate": "local"},
    #: 全开, 与接预设之前完全一样.
    "full": {"prefilter": 1, "structure": 1, "score": 2, "summarize": 1, "understand": True,
             "group": True, "digest": True, "translate": "llm"},
}


def stages(budget: Mapping[str, Any] | None = None) -> dict:
    """这一档预设放行哪几站.

    给了 `budget` 就从它读 (它就是 `llm()` 那一格) —— 一轮只读一次文件; 没给就现读.
    **键缺席时按 `full`**: 一个自己传预算的调用方 (测试、脚本、老的调用点) 拿到的还是老
    行为, 而线上每一轮都带着 `preset`, 走的才是页面上选的那一档.
    """
    source = budget if budget is not None else llm()
    name = str(source.get("preset") or "full").strip()
    chosen = PRESETS.get(name)
    if chosen is None:
        raise PipelineRefused("unsupported llm.preset: %s" % name)
    return dict(chosen)


class PipelineRefused(RuntimeError):
    """旋钮文件里有一个不认识的键, 或者一个该是映射的值不是映射。"""


def _merge(defaults: dict, given: dict, prefix: str = "") -> dict:
    out = dict(defaults)
    for key, value in (given or {}).items():
        if key not in defaults:
            raise PipelineRefused("unsupported setting: %s%s" % (prefix, key))
        if isinstance(defaults[key], dict):
            if not isinstance(value, dict):
                raise PipelineRefused("%s%s must be a mapping, not %s"
                                      % (prefix, key, type(value).__name__))
            out[key] = _merge(defaults[key], value, prefix + key + ".")
        else:
            out[key] = value
    return out


def load(path: Path | None = None) -> dict:
    """整份设置: 出厂值 + 文件里写的。"""
    target = Path(path or PATH)
    if not target.exists():
        return {key: dict(value) for key, value in DEFAULTS.items()}
    document = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise PipelineRefused("%s must be a mapping" % target.name)
    return _merge(DEFAULTS, document)


def detail(path: Path | None = None) -> dict:
    """补正文那一格的四个数。"""
    return load(path)["detail"]


def llm(path: Path | None = None) -> dict:
    """写作站那一格的五个数。"""
    return load(path)["llm"]


def schedule(path: Path | None = None) -> dict:
    """出刊那一格: 三种刊各自的时刻, 留空 = 这一期不出。"""
    return load(path)["schedule"]


def save(patch: Mapping[str, Any], path: Path | None = None) -> None:
    """把页面上改的那几格写回文件, 下一轮 tick 就读到新值。

    和 `chains.yaml` / `system.yaml` 同一套规矩 (8.9.5): ruamel 往返改写, 注释与引号原样, 临时
    文件加 rename。写之前先让 `_merge` 走一遍 —— 一个不认识的键在这里被拒, 好过先落进文件、
    再让每一轮 tick 都失败在同一个键上。

    patch 的键是**它在文件里的路径** (`detail.maxFetches` / `schedule.day`), 与页面上那一行
    的 key 一致: 页面不认第二套形状, 这里也不用。
    """
    target = Path(path or PATH)
    document = config.read_document(target) if target.exists() else CommentedMap()
    if document is None:
        document = CommentedMap()
    for key, value in (patch or {}).items():
        parts = [part for part in str(key).split(".") if part]
        if not parts:
            raise PipelineRefused("empty setting name")
        node: Any = document
        for part in parts[:-1]:
            step = node.get(part)
            if not isinstance(step, MutableMapping):
                step = CommentedMap()
                node[part] = step
            node = step
        node[parts[-1]] = value
    _merge(DEFAULTS, document)
    config.write_document(target, document)
