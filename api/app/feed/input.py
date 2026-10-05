"""分析输入的构造层: 一条材料进来, 每个判定档各自渲染一次。

AIHOT 的 `editorial/input.ts` 就是这个位置。为什么要把这件事从 `writing.py` 里搬出来: 上限
原本只有一套 (`htmlbody.MAX_CHARS`) —— 它同时是"库里存多少"和"喂给模型多少"。想按 AIHOT 把
正文存到 60k, 就得先把"喂给模型多少"独立成一层, 否则长文的模型输入会跟着涨三倍。

一次 `load` (AIHOT 的 `loadAnalyzeInput`), 四个渲染器:

1. 结构档 `structure_material` —— 正文截 7000 (AIHOT `buildMaterial` 的 `truncate(body, 7000)`);
2. 预筛与理解档 `context_material` —— 正文截 60000 (AIHOT `renderContext` 的 `capBody`);
3. 评分档 `score_input` —— 正文截 60000 (AIHOT `buildScoreInput` 的 `MAX_BODY_CHARS`);
4. 摘要档 `summary_material` —— 文章 6000 / 帖子 4000 / 引用 1500 (AIHOT 三个 `build*Prompt`)。

评分档**不给来源名与分级**: 输入里没有, 模型就没法拿名气加分 —— 这一条与 AIHOT 的
`buildScoreInput` 完全一致 (它只给时间、标题、正文)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from . import entities, htmlbody
from .sources import registry

#: 结构档的正文上限 (AIHOT `buildMaterial` 的 `truncate(body, 7000)`)。
STRUCTURE_BODY_CHARS = 7_000
#: 预筛与理解档的正文上限 (AIHOT `capBody` = `MAX_BODY_CHARS`)。
CONTEXT_BODY_CHARS = 60_000
#: 评分档的正文上限 (AIHOT `buildScoreInput` 的 `MAX_BODY_CHARS`)。
SCORE_BODY_CHARS = 60_000
#: 摘要档: 文章 6000 / 帖子 4000 / 引用 1500 (AIHOT 三个 `build*Prompt`)。
SUMMARY_BODY_CHARS = 6_000
SUMMARY_POST_CHARS = 4_000
QUOTED_CHARS = 1_500

#: AIHOT `input.ts` 的 `KIND_LABEL`, 加了我们自己的两种源。
KIND_LABEL = {
    "rss": "RSS",
    "web_list": "网页",
    "json_list": "网页接口",
    "x": "X 帖子",
    "blog": "官方博客",
    "exchange": "交易所公告",
    "regulator": "监管",
}


def clip(text: str, limit: int) -> str:
    """按**码点**截断, 尾巴一个省略号 (AIHOT `clampText`). 按字节截会切坏汉字。"""
    points = list(text or "")
    return text if len(points) <= limit else "".join(points[:limit]) + "\u2026"


def _quoted(row: Mapping[str, Any]) -> tuple[str, str]:
    """这条引用的那条推: (正文, 作者). 没有就是两个空串。"""
    try:
        quoted = json.loads(row.get("quoted_json") or "{}")
    except ValueError:
        return "", ""
    if not isinstance(quoted, dict):
        return "", ""
    return str(quoted.get("text") or "").strip(), str(quoted.get("handle") or "").strip()


def _media(row: Mapping[str, Any]) -> tuple[str, ...]:
    try:
        raw = json.loads(row.get("media_json") or "[]")
    except ValueError:
        return ()
    if not isinstance(raw, list):
        return ()
    return tuple(htmlbody.media_urls(raw))


def quoted_parts(row: Mapping[str, Any]) -> tuple[str, str]:
    """引用推文的作者与正文, 有才给 (作者在前)."""
    text, handle = _quoted(row)
    return handle, text


def quoted_block(row: Mapping[str, Any]) -> str:
    """引用推文那一段 (有才写): 写作站与材料渲染共用同一种格式。"""
    text, handle = _quoted(row)
    if not text:
        return ""
    return "引用推文（%s）:\n%s" % ("@" + handle if handle else "未署名", text)


@dataclass(frozen=True)
class AnalyzeInput:
    """一条材料在判定站眼里的样子 (AIHOT `AnalyzeInputArticle`)。

    只装判定站读得到的东西: 库里那一行有什么, 这里就有什么, 不做猜测 —— 渲染器要"材料够不够"
    时看的是这里, 而不是回去再读一次库。
    """

    external_id: str
    title: str
    url: str
    published_at: str
    body_html: str
    body_text: str
    author: str
    source_name: str
    source_kind: str
    source_tier: str
    source_first_party: bool
    quoted_text: str
    quoted_handle: str
    media: tuple[str, ...]
    #: 正文是不是去读过原文页 (AIHOT 的 `source.fetchesBody`)。读过的正文比源自带的可信。
    fetched: bool


def load(row: Mapping[str, Any], names: Mapping[str, str]) -> AnalyzeInput:
    """读一条材料 (AIHOT `loadAnalyzeInput`): 一次读完, 交给各个渲染器。"""
    source = registry.of(row["source_id"])
    quoted_text, quoted_handle = _quoted(row)
    return AnalyzeInput(
        external_id=str(row.get("external_id") or ""),
        title=(row.get("title") or "").strip(),
        url=str(row.get("url") or ""),
        published_at=str(row.get("published_at") or ""),
        body_html=row.get("body") or "",
        body_text=htmlbody.to_text(row.get("body") or ""),
        author=str(row.get("author") or "").strip(),
        source_name=names.get(row["source_id"], source.name),
        source_kind=source.kind or str(row.get("source_kind") or ""),
        source_tier=source.tier or "",
        source_first_party=bool(source.first_party),
        quoted_text=quoted_text,
        quoted_handle=quoted_handle,
        media=_media(row),
        fetched=bool(row.get("detail_at")),
    )


def missing_evidence(a: AnalyzeInput) -> bool:
    """除了标题什么都没有 (AIHOT `missingEvidence`): 预筛这时候的 BLOCK 只意味着"等材料"。"""
    return not (a.body_text.strip() or a.quoted_text.strip())


def _quality(a: AnalyzeInput) -> str:
    """材料有多全 (AIHOT `materialQuality`): 模型据此判断能说多满。"""
    if a.body_text.strip():
        return "完整正文（抓自原始网页）" if a.fetched else "完整正文（来自源自带的正文）"
    if a.quoted_text.strip():
        return "仅引用推文（主帖正文未抓到）"
    return "抓取失败，仅标题可用"


def _candidates(a: AnalyzeInput) -> str:
    """候选实体: 只许从这里挑 subjects (AIHOT 的 `subjects.filter((s) => s in ENTITIES)`)。

    词典里的写法认不出来就写「无」—— 空表也是一个答案, 比让模型自己编一个 id 好。
    """
    found = entities.candidates("%s %s" % (a.title, a.body_text))
    return ", ".join(found) if found else "无"


def structure_material(a: AnalyzeInput) -> str:
    """结构档的材料 (AIHOT `buildMaterial`): 来源块 + 材料块, 正文截 7000。"""
    lines = [
        "<source>",
        "名称：%s" % a.source_name,
        "类型：%s；分级：%s；一手来源：%s" % (
            KIND_LABEL.get(a.source_kind, a.source_kind or "未知"),
            a.source_tier or "未分级",
            "是" if a.source_first_party else "否"),
        "</source>",
        "<material>",
    ]
    if a.published_at:
        lines.append("发布时间：%s" % a.published_at)
    if a.author:
        lines.append("作者：%s" % a.author)
    lines.append("原文标题：%s" % a.title)
    lines.append("原文正文：")
    lines.append(clip(a.body_text, STRUCTURE_BODY_CHARS) or "（源没有给正文）")
    if a.quoted_text:
        lines.append("引用推文（%s）：" % ("@" + a.quoted_handle if a.quoted_handle else "未署名"))
        lines.append(clip(a.quoted_text, QUOTED_CHARS))
    lines.append("候选实体：%s" % _candidates(a))
    lines.append("原文链接：%s" % a.url)
    lines.append("</material>")
    return "\n".join(lines)


def context_material(a: AnalyzeInput, *, annotate_quoted: bool = False) -> str:
    """预筛与理解档的材料 (AIHOT `renderContext`): 正文截 60000。"""
    lines = ["来源: %s（%s，分级 %s）" % (
        a.source_name, KIND_LABEL.get(a.source_kind, a.source_kind or "未知"),
        a.source_tier or "未分级")]
    if a.author:
        lines.append("作者: %s" % a.author)
    if a.published_at:
        lines.append("发布时间: %s" % a.published_at)
    if a.media:
        lines.append("媒体: %d 张图" % len(a.media))
    lines.append("原文链接: %s" % a.url)
    lines.append("原文标题: %s" % a.title)
    if a.quoted_text:
        label = "@" + a.quoted_handle if a.quoted_handle else "原推"
        if annotate_quoted:
            lines.append("引用推文（%s）（以下是作者转发/引用的他人内容，不是作者本人的产出）:" % label)
            lines.append(a.quoted_text)
        else:
            lines.append("引用推文（%s）: %s" % (label, a.quoted_text))
    lines.append("")
    lines.append("原文正文:" if not (annotate_quoted and a.quoted_text) else "原文正文（作者自己的内容）:")
    lines.append(clip(a.body_text, CONTEXT_BODY_CHARS) or "（源没有给正文）")
    lines.append("")
    lines.append("材料质量: %s" % _quality(a))
    lines.append("候选实体: %s" % _candidates(a))
    return "\n".join(lines)


def score_input(a: AnalyzeInput) -> str:
    """评分档的材料 (AIHOT `buildScoreInput`): 只给时间、标题、正文, 不带来源与分级。

    引用那条推属于材料 (它是上下文, 不是噪音), 所以跟在正文后面; 正文空缺时退回标题 ——
    一条只有标题的材料照样要评分, 只是分数该低。
    """
    body = a.body_text.strip()
    if a.quoted_text:
        label = "@" + a.quoted_handle if a.quoted_handle else "原推文"
        quoted = "[引用 %s]：%s" % (label, a.quoted_text)
        body = "%s\n\n%s" % (body, quoted) if body else quoted
    return "\n\n".join([
        "请按系统规则评估以下单篇材料所代表的事件。只输出 attentionScore。",
        "【发布时间】\n%s" % a.published_at,
        "【标题】\n%s" % a.title,
        "【完整正文】\n%s" % clip(body or a.title, SCORE_BODY_CHARS),
    ])


def summary_material(a: AnalyzeInput, *, post: bool) -> str:
    """摘要档的材料: 文章截 6000, 帖子截 4000, 引用截 1500 (AIHOT 三个 `build*Prompt`)。

    摘要档不读媒体、不读来源分级 —— 它要判断的是"本体值不值得写", 那些只会带偏。
    """
    lines = ["来源: %s" % a.source_name]
    if a.author:
        lines.append("作者: %s" % a.author)
    lines.append("发布时间: %s" % a.published_at)
    lines.append("原文标题: %s" % a.title)
    lines.append("原文正文:")
    lines.append(clip(a.body_text, SUMMARY_POST_CHARS if post else SUMMARY_BODY_CHARS)
                 or "（源没有给正文）")
    lines.append("原文链接: %s" % a.url)
    if a.quoted_text:
        lines.append("")
        lines.append("引用推文（%s）:" % ("@" + a.quoted_handle if a.quoted_handle else "未署名"))
        lines.append(clip(a.quoted_text, QUOTED_CHARS))
    return "\n".join(lines)
