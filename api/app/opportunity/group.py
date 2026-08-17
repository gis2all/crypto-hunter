"""Batch grouping: one full-text model call turns gate-passed posts into topics.

The replacement for per-post classification. Every unclassified post is sent whole (no text
truncation), with a stable pN alias; the model only assigns ids to project groups, irrelevant,
or uncertain. The program maps the ids back, merges groups by handle/name, retries unresolved ids
twice, then folds any remainder into `other`; it writes the existing `project_name + classified`
columns. Trace, topic and extract stay unchanged.
"""

from __future__ import annotations

import logging

from ..llm import client as llm
from ..store import db
from . import model, settings, topic

log = logging.getLogger("crypto-hunter.opportunity")

GROUP_VERSION = "group.md@1"
DEFAULT_LIMIT = 1000
#: One correction call was not enough on a 249-post batch (9 missing + 5 uncertain after it).
#: Each call only sees the shrinking unresolved set, so a second correction is cheap and bounded.
MAX_REPAIRS = 2


def _lines(aliases: dict[str, dict]) -> list[str]:
    out = []
    for alias, post in aliases.items():
        text = str(post.get("text") or "").replace("\n", " ")
        out.append("[%s] @%s | score=%d | %s" % (
            alias, str(post.get("source") or "").lstrip("@"),
            int(post.get("score") or 0), text))
    return out


def _messages(posts: list[dict], aliases: dict[str, dict], *, repair: bool = False) -> list[dict]:
    body = "\n".join(_lines(aliases))
    lead = ("以下 %d 条帖子在上一次分组中没有被分配。请只处理这些帖子，"
            "逐条核对每个 [pN]，输出前确认没有遗漏，"
            "仍然遵守每条帖子必须且只能进入一个桶的规则。\n\n" % len(posts)) if repair \
        else ("以下是 %d 条帖子。\n\n" % len(posts))
    return [
        {"role": "system", "content": model.read_prompt("group.md")},
        {"role": "user", "content": lead + body},
    ]


def _ask(messages: list[dict], purpose: str) -> dict:
    try:
        raw = llm.complete_json(messages, purpose=purpose, prompt_version=GROUP_VERSION)
    except Exception as exc:  # noqa: BLE001 - the stage reports one visible failure
        raise RuntimeError("分组模型调用失败：%r" % exc) from exc
    if not isinstance(raw, dict):
        raise RuntimeError("分组模型没有返回 JSON 对象")
    return raw


def _parse(raw: dict, aliases: dict[str, dict]) -> dict:
    groups = raw.get("groups")
    if not isinstance(groups, list):
        raise RuntimeError("分组结果缺少 groups 数组")
    unknown = 0
    kept_groups = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        raw_ids = group.get("ids")
        if not isinstance(raw_ids, list):
            continue
        ids = []
        for value in raw_ids:
            key = str(value)
            if key in aliases:
                if key not in ids:
                    ids.append(key)
            else:
                unknown += 1
        kept_groups.append({
            "name": str(group.get("name") or "").strip(),
            "handle": str(group.get("handle") or "").strip().lstrip("@"),
            "kind": str(group.get("kind") or "").strip().lower(),
            "urls": group.get("urls") if isinstance(group.get("urls"), list) else [],
            "ids": ids,
        })

    def bucket(name: str) -> list[str]:
        nonlocal unknown
        values = raw.get(name)
        if not isinstance(values, list):
            return []
        out = []
        for value in values:
            key = str(value)
            if key in aliases:
                if key not in out:
                    out.append(key)
            else:
                unknown += 1
        return out

    return {"groups": kept_groups, "irrelevant": bucket("irrelevant"),
            "uncertain": bucket("uncertain"), "unknown": unknown}


def _finalize(parts: list[dict], aliases: dict[str, dict]) -> dict:
    merged: dict[str, dict] = {}
    dropped = 0
    unknown = 0
    valid: list[dict] = []
    for part in parts:
        unknown += int(part.get("unknown") or 0)
        for group in part.get("groups") or []:
            if (not group.get("ids") or not group.get("name")
                    or group.get("kind") not in ("activity", "project")):
                dropped += 1
                continue
            valid.append(group)

    # A handle is the stronger identity: merge those first, in the order the model gave them.
    for group in valid:
        if not group["handle"]:
            continue
        key = "handle:" + group["handle"].lower()
        if key not in merged:
            merged[key] = {"name": group["name"], "handle": group["handle"],
                           "kind": group["kind"], "ids": []}
        merged[key]["ids"].extend(group["ids"])

    by_name: dict[str, dict] = {}
    for group in merged.values():
        name = topic.normalise(group["name"])
        if name and name not in by_name:
            by_name[name] = group

    # A group that named no handle joins the handle group with the same normalised name,
    # regardless of which one the model printed first. Two different handles stay apart.
    for group in valid:
        if group["handle"]:
            continue
        name = topic.normalise(group["name"])
        target = by_name.get(name)
        if target is None:
            key = "name:" + name
            if key not in merged:
                merged[key] = {"name": group["name"], "handle": "",
                               "kind": group["kind"], "ids": []}
                by_name[name] = merged[key]
            target = merged[key]
        target["ids"].extend(group["ids"])

    assigned: dict[str, dict] = {}
    duplicates = 0
    for group in merged.values():
        seen = set()
        for ref in group["ids"]:
            if ref in seen:
                continue
            seen.add(ref)
            if ref in assigned:
                duplicates += 1
                continue
            assigned[ref] = group

    irrelevant: set[str] = set()
    for part in parts:
        for ref in part.get("irrelevant") or []:
            if ref not in assigned:
                irrelevant.add(ref)

    uncertain: set[str] = set()
    for part in parts:
        for ref in part.get("uncertain") or []:
            if ref not in assigned and ref not in irrelevant:
                uncertain.add(ref)

    all_ids = set(aliases)
    missing = all_ids - set(assigned) - irrelevant - uncertain
    groups = []
    seen_groups: set[int] = set()
    for group in assigned.values():
        marker = id(group)
        if marker not in seen_groups:
            seen_groups.add(marker)
            groups.append(group)
    return {"assigned": assigned, "groups": groups, "irrelevant": irrelevant,
            "uncertain": uncertain, "missing": missing, "duplicates": duplicates,
            "dropped": dropped, "unknown": unknown}


def run(*, limit: int | None = None, tell=None) -> dict:
    """Group every unclassified post; corrections stay bounded and never write partially."""
    cap = int(settings.value("classify_per_scan", DEFAULT_LIMIT) if limit is None else limit)
    pending = int((db.opportunity_queues().get("unclassified") or {}).get("count") or 0)
    if pending > cap:
        raise RuntimeError("待分组 %d 条，超过「分组帖子上限」%d；请调高设定后整轮重跑"
                           % (pending, cap))
    posts = db.opportunity_materials(unclassified=True, limit=max(1, cap))
    total = len(posts)
    if tell:
        tell(0, total)
    if not posts:
        return {"asked": 0, "groups": 0, "grouped": 0, "irrelevant": 0, "uncertain": 0,
                "missing": 0, "duplicates": 0, "dropped": 0, "unknown": 0,
                "repair_calls": 0, "activity": 0, "project": 0, "unreadable": 0}
    aliases = {"p%d" % index: post for index, post in enumerate(posts, start=1)}
    parts = [_parse(_ask(_messages(posts, aliases), "opportunity_group"), aliases)]
    final = _finalize(parts, aliases)
    repair_calls = 0
    while repair_calls < MAX_REPAIRS:
        unresolved = sorted(final["missing"] | final["uncertain"])
        if not unresolved:
            break
        # Correct only the ids the previous answer left unresolved. This is not another
        # grouping batch: each call sees a shrinking set and must finish it.
        subset = [aliases[key] for key in unresolved]
        subset_aliases = {key: aliases[key] for key in unresolved}
        parts.append(_parse(_ask(_messages(subset, subset_aliases, repair=True),
                                 "opportunity_group_repair"), subset_aliases))
        final = _finalize(parts, aliases)
        repair_calls += 1
    assignments = [{"ref": aliases[ref]["ref"], "kind": group["kind"],
                    "name": group["name"]}
                   for ref, group in final["assigned"].items()]
    unresolved = final["missing"] | final["uncertain"]
    if unresolved:
        log.warning("opportunity: %d posts remain unresolved after %d repairs; "
                    "marking them other", len(unresolved), repair_calls)
    irrelevant = [aliases[ref]["ref"] for ref in final["irrelevant"]]
    # A model omission or an explicit "uncertain" is not a reason to keep the whole batch out of
    # the database. The two buckets are recorded as `other` (no card), while the groups that were
    # resolved still land in one transaction.
    unresolved_refs = [aliases[ref]["ref"] for ref in unresolved]
    db.opportunity_group_put(assignments, irrelevant + unresolved_refs)
    log.info("opportunity: grouped %d posts into %d groups (%d irrelevant, %d duplicates,"
             " %d repair calls, %d unresolved)", total, len(final["groups"]), len(irrelevant),
             final["duplicates"], repair_calls, len(unresolved))
    if tell:
        tell(total, total)
    return {"asked": total, "groups": len(final["groups"]),
            "grouped": len(assignments), "irrelevant": len(irrelevant),
            "uncertain": len(unresolved), "missing": 0, "duplicates": final["duplicates"],
            "dropped": final["dropped"], "unknown": final["unknown"],
            "repair_calls": repair_calls,
            "activity": sum(1 for group in final["groups"] if group["kind"] == "activity"),
            "project": sum(1 for group in final["groups"] if group["kind"] == "project"),
            "unreadable": 0}
