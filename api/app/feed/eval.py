"""金标评测 (5.22).

「写得好不好」是一个没有客观答案的问题 —— 所以它需要一个有客观答案的替身: 一份**人工标好的
样本** (`eval_golden.json`), 以及一个把机器答案与人那个对一遍的函数。它量的是**回归**: 换提示词、
换模型、改阈值之后, 那些原本判对的还对不对。

标的是能被人工标准的那一部分 —— 清单里第一档到第六档。它们都是纯函数: 给定输入, 答案
唯一。"这一句话写得像不像中文" 不在这里 —— 它没有唯一答案, 而且一个模型去评另一个
模型写的话, 得到的是一个意见而不是一个读数。真要量那一块, 需要的是另一批人手写的参考译文,
而不是再加一个判官 —— 那份样本还没有, 所以这里不假装有。

一个只读、不花钱的读口: 它不写库, 也不调模型。跑一遍就是把样本对一遍, 对不上的一条一条列出来 ——
「不合格」比「合格率 91%」更能告诉人改哪里。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import editorial, rules
from .models import Discovery, Event, Item

GOLDEN = Path(__file__).with_name("eval_golden.json")


def _event(given: dict) -> Event:
    """把一个样本的输入做成一件事 —— 用真的 `Event` / `Item` / `Discovery`, 不用一个临时对象:
    标的既然是回归, 它就得在“构造函数改了参数”时也跟着变红。
    """
    kinds = list(given.get("kinds") or [])
    items = [
        Item(discovery=Discovery(source_kind=kind, source_id="golden-%d" % index,
                                 external_id="golden-%d" % index, title="", url="",
                                 published_at=""))
        for index, kind in enumerate(kinds)
    ]
    return Event(identity_key="golden", kind="", kind_label="", title="", url="",
                 first_seen_at="", last_seen_at="", items=items,
                 sources=int(given.get("sources") or 0))


#: 轴 -> 答案。每一个都是调一个真函数, 不是在这里重写一遍那些规则。
AXES = {
    "certainty": lambda given: editorial.certify(_event(given)),
    "channel": lambda given: rules.channel_of(str(given["kind"]), bool(given.get("x_post"))),
    "pool": lambda given: rules.is_pool_eligible(
        str(given["participation"]), given.get("relevance"),
        given.get("title"), given.get("summary")),
    "selectable": lambda given: rules.is_selectable(
        bool(given["eligible"]), given.get("judged"), str(given["tier"])),
    "page": lambda given: rules.has_item_page(str(given["visibility"]), str(given["mode"])),
}


def golden() -> dict:
    """读一遍标准答案。读坏了就报错 —— 一份读不出来的金标不是“没有金标”,
    而是“指标在撒谎”。
    """
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def score() -> dict:
    """把样本对一遍。回总数、过了几条、各轴的合格率, 以及每一条没对上的细节。"""
    document = golden()
    cases = document.get("cases") or []
    misses: list[dict] = []
    per_axis: dict[str, list[int]] = {}
    for case in cases:
        axis = str(case.get("axis") or "")
        run = AXES.get(axis)
        counter = per_axis.setdefault(axis, [0, 0])
        counter[1] += 1
        if run is None:
            misses.append({"id": case.get("id"), "axis": axis, "want": case.get("want"),
                           "got": None, "why": "不认识这个轴"})
            continue
        got = run(case.get("given") or {})
        if got == case.get("want"):
            counter[0] += 1
            continue
        misses.append({"id": case.get("id"), "axis": axis, "want": case.get("want"),
                       "got": got, "why": case.get("why") or ""})
    total = len(cases)
    passed = total - len(misses)
    return {
        "version": int(document.get("version") or 0),
        "total": total,
        "passed": passed,
        "agreement": round(passed / total, 4) if total else 0.0,
        "axes": [{"axis": axis, "passed": won, "total": seen}
                 for axis, (won, seen) in sorted(per_axis.items())],
        "misses": misses,
    }
