"""实体词典: 一条新闻"在说谁"的那一半。

`config/feed/entities.yaml` 是人写的名单, 这里只做三件事: 读它、按整词认词、把模型的答案过一遍筛。
它不是 `materials.learn_aliases` 的替代 —— 那个是"窗口自己教自己", 会被 `$AI` 这种代码带偏;
手写的这一份是可审的: 一条新闻在说谁, 至少有一个答案是人挑过的。

两个用途:

1. `candidates(text)`: 这条材料里出现过的实体 id, 喂给理解档当白名单 —— 模型只能从里面挑, 挑不中
   就当没写 (AIHOT 的 `analyze.ts` 是同一手: `subjects.filter((s) => s in ENTITIES)`);
2. `subjects(names)`: 把模型交回来的列表过一遍筛, 以及 `label()` 把 id 印成人看的写法。

认词与 `materials.mentions` 同一条规矩 (整词、转义); 代码 (`tickers`) 另外算: 裸写必须是大写,
`$` 形式不分大小写 —— 一个英文句子里的小写 `eth` 不是代码, 大写的 `ETH` 是。
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import yaml

from .. import config
from .materials import mentions

PATH = config.ROOT / "config" / "feed" / "entities.yaml"

#: 一条实体能写的键。认不出的键让这一份**读失败**: 与 pipeline.yaml / sources.yaml 同一条理由 ——
#: 写在文件里却被忽略的键, 比一个拼错的键更坏, 它看起来设过了。
KEYS = ("id", "name", "kind", "aliases", "tickers")


class EntitiesRefused(RuntimeError):
    """词典里有一行读不出来。"""


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    kind: str
    aliases: tuple[str, ...]
    tickers: tuple[str, ...]


_lock = threading.Lock()
_cache: tuple[str, float, tuple[Entity, ...]] | None = None


def _row(index: int, raw: dict) -> Entity:
    if not isinstance(raw, dict):
        raise EntitiesRefused("entity #%d is not a mapping" % index)
    unknown = set(raw) - set(KEYS)
    if unknown:
        raise EntitiesRefused("entity #%d: unsupported key(s) %s"
                              % (index, ", ".join(sorted(unknown))))
    ident = str(raw.get("id") or "").strip()
    if not ident or not re.fullmatch(r"[a-z0-9-]+", ident):
        raise EntitiesRefused("entity #%d: id must be lower-case letters, digits or dashes" % index)
    aliases = tuple(str(a).strip().lower() for a in (raw.get("aliases") or []) if str(a).strip())
    tickers = tuple(str(t).strip().upper() for t in (raw.get("tickers") or []) if str(t).strip())
    if not aliases and not tickers:
        raise EntitiesRefused("entity %s: needs at least one alias or ticker" % ident)
    return Entity(id=ident, name=str(raw.get("name") or ident).strip(),
                  kind=str(raw.get("kind") or "").strip(), aliases=aliases, tickers=tickers)


def load(path: Path | None = None) -> tuple[Entity, ...]:
    """整份词典。文件不在时回空表 —— 一台刚拉下来的机器不该因为少一个词典就不能出刊。"""
    target = Path(path or PATH)
    if not target.exists():
        return ()
    document = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict) or not isinstance(document.get("entities"), list):
        raise EntitiesRefused("%s must hold an `entities:` list" % target.name)
    rows = tuple(_row(index, raw) for index, raw in enumerate(document["entities"]))
    seen: set[str] = set()
    for entity in rows:
        if entity.id in seen:
            raise EntitiesRefused("duplicate entity id: %s" % entity.id)
        seen.add(entity.id)
    return rows


def all() -> tuple[Entity, ...]:
    """词典, 按文件 mtime 缓存: 页面每个请求都要问它一次, 而词典一般是几分钟才动一次。"""
    global _cache
    target = PATH
    try:
        stamp = target.stat().st_mtime
    except OSError:
        return ()
    key = str(target)
    with _lock:
        if _cache and _cache[0] == key and _cache[1] == stamp:
            return _cache[2]
    rows = load(target)
    with _lock:
        _cache = (key, stamp, rows)
    return rows


def forget() -> None:
    """下一次问的时候重新读一遍文件 (测试与"我改了词典"用)。"""
    global _cache
    with _lock:
        _cache = None


def ids() -> set[str]:
    return {entity.id for entity in all()}


#: 这份词典**不是分组键**: 试过拿它当兜底分组的名字表, 结果是「Binance 上币 A」和
#: 「Binance 上币 B」粘成一个桶 (materials.parties_of 认到名字就不再拿代码区分)。它的位置是
#: 白名单 —— 模型可以说什么, 以及页面上印哪个写法。分组是 `grouping.py` 的事。
def _ticker_seen(text: str, ticker: str) -> bool:
    """代码的写法比名字严: `$` 形式不分大小写, 裸写要大写。"""
    if re.search(r"\$%s(?![a-z0-9])" % re.escape(ticker), text, re.IGNORECASE):
        return True
    return re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(ticker), text) is not None


def candidates(text: str) -> tuple[str, ...]:
    """这段文本里出现过的实体 id, 按词典顺序。

    给理解档当白名单用: 模型只能从这里挑 subjects。空表也是一个答案 —— 这条材料里没有认识的实体。
    """
    low = (text or "").lower()
    found: list[str] = []
    for entity in all():
        hit = any(mentions(low, alias) for alias in entity.aliases) or \
            any(_ticker_seen(text or "", ticker) for ticker in entity.tickers)
        if hit:
            found.append(entity.id)
    return tuple(found)


def subjects(names: Iterable[str]) -> list[str]:
    """把模型写的 subjects 过一遍筛: 只留词典里有的 id, 去重、保持原顺序 (AIHOT 的 `s in ENTITIES`)。"""
    known = ids()
    out: list[str] = []
    for name in names or ():
        ident = str(name or "").strip().lower()
        if ident and ident in known and ident not in out:
            out.append(ident)
    return out


def label(ident: str) -> str:
    """id 印在页面上时的写法 (词典里没有就印它自己)。"""
    for entity in all():
        if entity.id == ident:
            return entity.name
    return ident


def labels(names: Sequence[str]) -> list[str]:
    return [label(name) for name in names]
