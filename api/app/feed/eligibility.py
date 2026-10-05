"""资格与判定: "够不够格"只在这一处回答 (5.5, AIHOT 的 `publication/rules.ts` + `scope.ts`)。

原先"够不够格"散在四处, 每处各写一遍自己的那一条线:

| 哪一处 | 它管的那条线 |
| --- | --- |
| `rules.py` | 材料的属性: 可见性、档位、能不能印全文、能不能再分发 |
| `scope.py` | 那一行的**时间**: 过没过发布门、下没下架 |
| `analysis.py` | 精选线 (`CURATED_MIN_SCORE`) 与规则版的 `selected` |
| `writing.py` | 模型答完之后 `picked_in_curated` 那一份判定 |

散着写的代价不是多敲几个字, 而是这四条线会**分家**: 页面按一条线印"精选", 入库那一站按
另一条线决定要不要译正文 —— 于是精选里混着没有正文的条目, 而没有任何一处能读出这件事。
(先前就发生过一次: 精选线的数字在两处各写一遍, 改了一处。)

所以这里做两件事, 一件也不多:

1. 一张**表** (`GATES`): 每一道门槛一行 —— 它问的问题、需要的输入、以及"为什么是这个口径"。
   谓词本身仍写在原来的模块里 (`rules` / `scope` / `analysis`), 这一层只是把它们**列全**:
   接口行为不变, 变的是"现在有一处能一眼看完全部门槛"。
2. 一个**谓词** `qualify(facts)`: 把表里凑得齐输入的门槛按顺序跑一遍, 回"过了哪些、卡在哪一行"。

`explain()` 是给运维读口与管理台用的同一张表, 换成给人看的形状; `explain_item()` 是给详情页
用的 —— 一条为什么没进精选, 页面上能指到具体哪一栏, 而不是只有"分数不够"。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping

from . import analysis, rules, scope


@dataclass(frozen=True)
class Gate:
    """一道门槛。

    `needs` 是这道门槛要问的输入名。凑不齐就**跳过**而不是判失败 —— 一个只有内存里那几格的
    读者 (页面) 与一个手上有整行的人 (运维读口) 都会调这里, 前者答不上来的一栏不该变成一次
    静默的淘汰。
    """

    key: str
    label: str
    question: str
    why: str
    needs: tuple[str, ...]
    ask: Callable[[Mapping], bool]


#: 顺序就是判定的顺序: 先问"能不能读"(下架/发布门), 再问"进没进池子", 最后才是"够不够精选"。
GATES: tuple[Gate, ...] = (
    Gate("withdrawn", "没下架", "这一行贴了 withdrawn 标记没有？",
         "下架不是删除: 已经转发出去的链接要能解释为什么没了, 所以它是一道终审而不是一次 UPDATE。",
         ("withdrawn_at",),
         lambda facts: not scope.is_withdrawn(dict(facts))),
    Gate("released", "过了发布门", "现在这一刻过了发布门没有？",
         "门是 %d 秒的缓冲: 三分钟里还能补正文、补来源名、把一条误判收回来, 而读者看不见这个窗口。"
         % rules.RELEASE_DELAY_SECONDS,
         ("released_at", "seen_at", "now"),
         lambda facts: scope.is_released(dict(facts), facts["now"])),
    Gate("pool", "进了池子", "编辑类源、预筛过了、中文标题与摘要都在？",
         "四条缺一不可。少了标题或摘要的那一条没有可印的东西, 而预筛没过的是另一行业务。",
         ("participation", "relevance", "title", "summary"),
         lambda facts: rules.is_pool_eligible(facts["participation"], facts["relevance"],
                                              facts["title"], facts["summary"])),
    Gate("item_page", "有自己的一页", "这一条有没有自己的详情页？",
         "下架的与不做编辑的源没有: 一个没有可印正文的源, 给它一页就是在印一块空白。",
         ("visibility", "participation"),
         lambda facts: rules.has_item_page(facts["visibility"], facts["participation"])),
    Gate("selectable", "够精选", "进了池子、判过选了、这一档还允许？",
         "EXCLUDE_MP 这一档是明确写死「不许进精选」的, 所以它是资格而不是分数 —— "
         "分数再高也越不过它。",
         ("eligible", "judged_selected", "tier"),
         lambda facts: rules.is_selectable(bool(facts["eligible"]), facts["judged_selected"],
                                           facts["tier"])),
    Gate("evidence", "是证据", "这一条是自己的事实的**证据**, 不只提了一句？",
         "盘点 / 日报把几件事装在一起, 它对那些事只是提及 —— 复合分析 (scope=composite) 不是任何"
         "一件的证据, 所以它不进材料、不进热度, 也不立事件 (AIHOT 的 evidenceCondition)。",
         ("analysis_scope",),
         lambda facts: (facts["analysis_scope"] or "primary") != "composite"),
    Gate("indexable", "可被收录", "公开、有摘要、没被排除、而且入选或手动收录过？",
         "收录比「印在页面上」严一档: 一个没有被排除的编辑决定, 才是能被搜索引用的那一条。",
         ("visibility", "has_summary", "selected", "seo_indexed_at", "seo_excluded_at"),
         lambda facts: rules.is_indexable(facts["visibility"], bool(facts["has_summary"]),
                                          bool(facts["selected"]), facts["seo_indexed_at"],
                                          facts["seo_excluded_at"])),
    Gate("body_full", "能印全文", "清单允许印全文, 而且这一版确实读到了正文？",
         "「能抓到」不等于「可以印」: 图书馆与付费墙源在清单里写的就是不允许。",
         ("site_fulltext", "body_status", "has_body", "source"),
         lambda facts: rules.body_mode_of(facts["source"], facts["body_status"],
                                          bool(facts["has_body"])) == "full"),
    Gate("redistribute", "能再分发", "能不能把正文交给站外 (RSS / API)？",
         "比「印在站内」严一档: 站内能印不等于能让别人转走。",
         ("syndicate_fulltext", "body_mode", "source"),
         lambda facts: rules.may_redistribute(facts["source"], facts["body_mode"])),
)


def _facts_item(item, source, now: datetime | None = None) -> dict:
    """一条在内存里的条目能答上来的那几格。答不上来的那几道门槛会被跳过。"""
    return {
        "participation": source.participation,
        "relevance": getattr(item, "relevance", None) or "pass",
        "title": item.display_title,
        "summary": item.summary,
        "visibility": source.visibility,
        "eligible": bool(getattr(item, "selected", False)) or item.summary != "",
        "judged_selected": getattr(item, "selected", False),
        "tier": getattr(item, "tier", "") or "",
        "has_summary": bool(item.summary),
        "selected": bool(getattr(item, "selected", False)),
        "seo_indexed_at": getattr(item, "seo_indexed_at", None),
        "seo_excluded_at": getattr(item, "seo_excluded_at", None),
        "site_fulltext": bool(getattr(source, "site_fulltext", False)),
        "syndicate_fulltext": bool(getattr(source, "syndicate_fulltext", False)),
        "body_status": "ok" if (item.discovery.body or "").strip() else "missing",
        "has_body": bool((item.discovery.body or "").strip()),
        "body_mode": item.body_mode,
        "analysis_scope": getattr(item, "analysis_scope", ""),
        "source": source,
    }


def qualify(facts: Mapping) -> dict:
    """按 `GATES` 跑一遍。回 `{ok, blocked_by, rows}`; 凑不齐输入的门槛记 `null`。"""
    rows: list[dict] = []
    blocked = ""
    for gate in GATES:
        missing = [name for name in gate.needs if name not in facts]
        if missing:
            rows.append({"key": gate.key, "label": gate.label, "ok": None,
                         "detail": "缺少输入：%s" % ", ".join(missing)})
            continue
        try:
            ok = bool(gate.ask(facts))
        except Exception as exc:  # 一道门槛自己坏了, 不该把整页变成 500
            rows.append({"key": gate.key, "label": gate.label, "ok": None, "detail": repr(exc)})
            continue
        rows.append({"key": gate.key, "label": gate.label, "ok": ok, "detail": ""})
        if not ok and not blocked:
            blocked = gate.key
    return {"ok": not blocked, "blocked_by": blocked, "rows": rows}


def explain() -> dict:
    """这一层的自述: 每一道门槛叫什么、问什么、为什么是这个口径。运维读口与管理台读它。"""
    return {
        "release_delay_seconds": rules.RELEASE_DELAY_SECONDS,
        "curated_min_score": analysis.CURATED_MIN_SCORE,
        "gates": [{"key": gate.key, "label": gate.label, "question": gate.question,
                   "why": gate.why, "needs": list(gate.needs)} for gate in GATES],
    }


def explain_item(item, source, now: datetime | None = None) -> dict:
    """一条的判定明细: 过了哪几道、卡在哪一道。"""
    return qualify(_facts_item(item, source, now))
