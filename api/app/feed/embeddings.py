"""向量召回: 把「这段文本」变成一串数, 用余弦代替字符二元组 (AIHOT 的 providers/embeddings.ts)。

召回是分组的第一步 (过去 14 天的事实里挑最像的十条), 而"像"这个判断有两种做法: 字符二元组
(不需要任何外部依赖, 但对换了说法的同一件事不敏感) 和向量余弦 (要多一个 /embeddings 端点)。

它是**可选**的: 配了 `llm.embedding.*` 就用向量 (召回线 0.6), 没配就退回 `grouping.lexical`
(召回线 0.25)。留退路不是保守 —— 不是每个网关都提供 /embeddings, 而一个 404 不该让分组整轮停摆。

一次一批: 一条新报道 + 它的十条候选一次问完。逐条问是每条一次往返, 而这一步每轮要跑几十次;
缓存按**文本内容**索引, 所以同一批候选在下一轮不再花钱。
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import threading
import time
from typing import Callable, Sequence

import httpx

from .. import config
from . import grouping

log = logging.getLogger(__name__)

#: 端点、key、模型名与维度. 三者齐了才算配好 (AIHOT 的 EMBEDDING_BASE_URL / _API_KEY / _MODEL).
ENV = ("CH_EMBEDDING_BASE_URL", "CH_EMBEDDING_API_KEY",
       "CH_EMBEDDING_MODEL", "CH_EMBEDDING_DIMS")

#: 一次请求装多少条文本. 候选池十条上下, 留出的余量给"这一轮同时要问几条新报道"的将来.
BATCH = 64
TIMEOUT = 30.0

#: 一条文本截到多少字再送. 2 千字封顶是 AIHOT 的 slice(0, 2000) —— 嵌入模型有个上下文上限,
#: 而召回要的是「这篇在说什么」, 正文后半段帮不上忙.
TRUNCATE = 2000

#: 缓存. 键是文本的哈希, 值是 (过期时刻, 向量). 上限是给长跑进程留的天花板: 池子有几千条事实,
#: 一次全进来也不该把内存吃光.
TTL_SECONDS = 1800.0
CACHE_LIMIT = 8192

_CACHE: dict[str, tuple[float, list[float]]] = {}
_lock = threading.Lock()


def settings() -> dict:
    """端点, 每次现读. 与 llm.client.settings 同一条理由: 下一轮就生效, 不用重启."""
    config.load_env_file()
    return {
        "base_url": os.environ.get(ENV[0], "").strip().rstrip("/"),
        "api_key": os.environ.get(ENV[1], "").strip(),
        "model": os.environ.get(ENV[2], "").strip(),
        "dims": int(os.environ.get(ENV[3], "0") or 0),
    }


def available() -> bool:
    """配没配向量端点. 页面据此决定要不要显示这一组旋钮的"已配置"."""
    current = settings()
    return bool(current["base_url"] and current["api_key"] and current["model"])


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """两串向量的余弦相似度. 长度不同/有空串就回 0 —— 那是两个不可比的答案, 不是"很像"."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left <= 0 or norm_right <= 0:
        return 0.0
    return dot / (norm_left * norm_right)


def _key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _batched(items: Sequence[str], size: int):
    for start in range(0, len(items), size):
        yield list(items[start:start + size])


def _fetch(current: dict, texts: Sequence[str]) -> list[list[float]] | None:
    """一次 /embeddings. 任何不合预期 (状态码、条数、index、向量长度) 都当整批没拿到.

    部分写入比整批退回坏得多: 一半候选用余弦、另一半用字面相似度, 两条线不在一个尺度上,
    召回结果既不是向量召回也不是字面召回, 而且完全静默。
    """
    body: dict = {"model": current["model"],
                  "input": [text[:TRUNCATE] for text in texts],
                  "encoding_format": "float"}
    if current["dims"] > 0:
        body["dimensions"] = current["dims"]
    try:
        response = httpx.post(current["base_url"] + "/embeddings",
                              headers={"Authorization": "Bearer " + current["api_key"],
                                       "Content-Type": "application/json"},
                              json=body, timeout=TIMEOUT)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # 网络、状态码、坏 JSON 都是同一件事: 这一批没有向量
        log.warning("feed: embeddings unavailable: %s", str(exc)[:200])
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or len(data) != len(texts):
        log.warning("feed: embeddings returned %s vectors for %d texts",
                    len(data) if isinstance(data, list) else "?", len(texts))
        return None
    out: list[list[float]] = [[] for _ in texts]
    width = 0
    for entry in data:
        index = entry.get("index") if isinstance(entry, dict) else None
        vector = entry.get("embedding") if isinstance(entry, dict) else None
        if (not isinstance(index, int) or not 0 <= index < len(texts) or out[index]
                or not isinstance(vector, list) or not vector
                or not all(isinstance(value, (int, float)) for value in vector)):
            return None
        values = [float(value) for value in vector]
        if width and len(values) != width:
            return None
        width = len(values)
        out[index] = values
    if current["dims"] > 0 and width != current["dims"]:
        log.warning("feed: embeddings width %d != configured %d", width, current["dims"])
        return None
    return out


def warm(texts: Sequence[str]) -> dict[str, list[float]] | None:
    """这一批文本的向量, 按文本索引; 端点没配、或有一句没拿到, 就整批回 None.

    整批的意思: 调用方要么走向量那条线 (召回线 0.6), 要么走字面那条线 (0.25), 不混。
    """
    current = settings()
    if not (current["base_url"] and current["api_key"] and current["model"]):
        return None
    wanted: list[str] = []
    for text in texts:
        if text and text not in wanted:
            wanted.append(text)
    if not wanted:
        return None
    now = time.monotonic()
    table: dict[str, list[float]] = {}
    missing: list[str] = []
    with _lock:
        for text in wanted:
            hit = _CACHE.get(_key(text))
            if hit and hit[0] > now:
                table[text] = hit[1]
            else:
                missing.append(text)
    for batch in _batched(missing, BATCH):
        vectors = _fetch(current, batch)
        if vectors is None:
            return None
        with _lock:
            for text, vector in zip(batch, vectors):
                _CACHE[_key(text)] = (now + TTL_SECONDS, vector)
                if len(_CACHE) > CACHE_LIMIT:
                    _CACHE.pop(next(iter(_CACHE)))
        table.update(zip(batch, vectors))
    return table if all(text in table for text in wanted) else None


def similarity(table: dict[str, list[float]]) -> Callable[[str, str], float]:
    """把一张「文本 → 向量」的表变成 `grouping.recall` 要的那个相似度函数."""
    def score(left: str, right: str) -> float:
        return cosine(table.get(left) or [], table.get(right) or [])

    return score


def forget() -> None:
    """把缓存清掉 (测试与"我改了端点"用)."""
    with _lock:
        _CACHE.clear()
