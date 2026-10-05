"""能力到模型：一次调用用哪一档模型（AIHOT 的 editorial/models.ts）。

一份网关可以同时挂着贵档与便宜档。评分与便宜摘要用便宜档，理解与翻译用贵档，是这一层的
全部意义 —— 没有它，加一档模型就是给全站换模型。

选择顺序与 AIHOT 相同：配置文件里按能力选的模型，其次环境变量，最后回落到系统设置里那一个
默认模型。读的时候每次现读并缓存一分钟，所以改完下一次调用生效，不用重启。

能力名与 AIHOT 的 CAPABILITIES 对齐（只列我们用得到的几个）：

  prefilter  行业相关性预筛（PASS / BLOCK / UNKNOWN，BLOCK 短路整条）
  score      精选评分（两次独立评分）
  understand 内容理解（标题、摘要、理由、骨架、实体）
  summarize  便宜档（标题与摘要）
  structure  结构抽取（类别、标签、主体、事实骨架，不写读者文字）
  translate  全文翻译
  group      事件归组（逐条关系判定）
  groupReview 归组复核（相似度不高的合并再读一遍；最好换一家模型）
  digest     事件综述（按时间重写一整段话）
  report     报刊撰写（日报/周报/月报的正文，5.7）
"""

from __future__ import annotations

import os
import time

from .. import config

#: 能力 -> (配置路径, 环境变量)。配置写在 `config/system.yaml` 的 `llm.models` 下。
CAPABILITIES: dict[str, tuple[str, str]] = {
    "prefilter": ("llm.models.prefilter", "CH_LLM_MODEL_PREFILTER"),
    "score": ("llm.models.score", "CH_LLM_MODEL_SCORE"),
    "understand": ("llm.models.understand", "CH_LLM_MODEL_UNDERSTAND"),
    "summarize": ("llm.models.summarize", "CH_LLM_MODEL_SUMMARIZE"),
    "structure": ("llm.models.structure", "CH_LLM_MODEL_STRUCTURE"),
    "translate": ("llm.models.translate", "CH_LLM_MODEL_TRANSLATE"),
    "group": ("llm.models.group", "CH_LLM_MODEL_GROUP"),
    "groupReview": ("llm.models.groupReview", "CH_LLM_MODEL_GROUP_REVIEW"),
    "digest": ("llm.models.digest", "CH_LLM_MODEL_DIGEST"),
    "report": ("llm.models.report", "CH_LLM_MODEL_REPORT"),
}

#: 一分钟的缓存：改配置下一次调用生效，但一轮里几十条不必每条都读一遍文件。
_TTL_SECONDS = 60.0
_cache: tuple[float, dict] | None = None


def forget() -> None:
    """Let the next call re-read. The settings page and tests use it."""
    global _cache
    _cache = None


def _document() -> dict:
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < _TTL_SECONDS:
        return _cache[1]
    try:
        document = config.read_document(config.SYSTEM_PATH) or {}
    except Exception:  # 配置坏了不该让一轮采集失败：回落到默认模型
        document = {}
    if not isinstance(document, dict):
        document = {}
    _cache = (now, document)
    return document


def model_for(capability: str) -> str | None:
    """这一档现在用哪个模型；没单独配就返回 None（意思是「用系统设置里那一个」）。"""
    path, env = CAPABILITIES[capability]
    chosen = os.environ.get(env)
    if not chosen:
        value = config._dig(_document(), path)
        chosen = str(value).strip() if value not in (None, "") else ""
    return chosen or None
