"""主题层：一张卡 = 一个项目（2026-10-19, user）。

The unit was a post; it becomes a **topic**. Seven CONSO posts are one thing to read, and the card
should be written from the project's own material - community posts are what *points* at it.

This module does the merging and hands the extraction one material per topic:

- the key is the project (the name the classification pass read, else the author's handle);
- the topic's text is its posts, labelled with who said each one and when, so the model can tell
  the official announcement from a bystander's summary;
- the topic's official pages (from the trace) ride along, and the extraction is asked to write
  from them first;
- one `ref` represents the topic (the highest-scoring post) and the rest are marked as read with
  it, so a later scan does not turn them into cards of their own.
"""

from __future__ import annotations

import json
import logging
import re

from ..store import db

log = logging.getLogger("crypto-hunter.opportunity")

#: How many posts one topic may gather. A project with more than this many posts in the queue is
#: being talked about a lot; the cap keeps one loud project from filling the prompt.
MAX_POSTS_PER_TOPIC = 10

#: How many topics one extraction pass reads by default, and the floor for the post scan. The
#: real scan is raised by `materials()` when the requested topic cap needs more posts; otherwise
#: a 200-topic cap would silently read only 120 posts and stop at a few dozen topics.
TOPICS_PER_PASS = 12
SCAN_POSTS = 120


def normalise(name: str) -> str:
    """A project's name as a merge key: lower case, no spaces, no punctuation.

    `Vault Street` and `VaultStreet` are one project and were two cards until this existed; CJK
    survives (the pattern keeps letters and digits of any script), so 「共识」 stays itself.
    """
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(name or "").lower())


def key_of(material: dict) -> str:
    """What makes two posts the same topic: the project, else the account that posted.

    `source` is the material row's name for the handle (`handle AS source` in the query) - reading
    `handle` here merged every post in the queue into one bucket called "handle:", which is a bug
    the trust test caught before a scan could.
    """
    name = normalise(str(material.get("project_name") or ""))
    if name:
        return name
    return "handle:" + normalise(str(material.get("source") or "").lstrip("@"))


def merge(materials: list[dict]) -> list[dict]:
    """Group posts into topics, best-scoring topic first.

    The order is the best score inside the topic, so a topic with one strong post outranks a topic
    with five weak ones - the gate's own reading decides which project is worth the model's time.
    """
    groups: dict[str, list[dict]] = {}
    for material in materials:
        groups.setdefault(key_of(material), []).append(material)
    topics = []
    for key, posts in groups.items():
        posts.sort(key=lambda row: float(row.get("score") or 0), reverse=True)
        kept = posts[:MAX_POSTS_PER_TOPIC]
        head = kept[0]
        topics.append({
            "key": key,
            "project": head.get("project_name") or head.get("source") or "",
            "ref": head["ref"],
            "source": head.get("source") or "",
            "source_kind": "post",
            "url": head.get("url") or "",
            "at": head.get("at") or "",
            "source_target": head.get("source_target") or "",
            "trace_level": head.get("trace_level") or "",
            "trace_json": head.get("trace_json") or "",
            "consumed": [row["ref"] for row in posts],
            "posts": len(kept),
            "score": max(float(row.get("score") or 0) for row in kept),
            "text": _topic_text(kept),
        })
    topics.sort(key=lambda topic: topic["score"], reverse=True)
    return topics


def _topic_text(posts: list[dict]) -> str:
    """Every post of the topic, each labelled with its author and time.

    The label matters: the model is asked to write from the project's own words, and it can only
    tell those from a bystander's if the text says who said what.
    """
    parts = []
    for post in posts:
        handle = str(post.get("source") or "").lstrip("@") or "unknown"
        at = str(post.get("at") or "")[:16]
        parts.append("【@%s %s】\n%s" % (handle, at, str(post.get("text") or "").strip()))
    return "\n\n".join(parts)


def materials(limit: int = TOPICS_PER_PASS, scan: int = SCAN_POSTS) -> list[dict]:
    """The topics waiting for extraction, as one material each, official pages attached."""
    # `limit` is a topic count; one topic may consume up to MAX_POSTS_PER_TOPIC posts in the
    # prompt. Read enough posts that the topic cap is not blocked by the old 120-post floor.
    scan = max(int(scan), int(limit) * MAX_POSTS_PER_TOPIC)
    rows = db.opportunity_materials(unclassified=False, limit=scan)
    topics = merge(rows)[:limit]
    for topic in topics:
        topic["official_pages"] = _pages_of(topic)
    return topics


def _pages_of(topic: dict) -> list[dict]:
    """The pages the trace read for this topic's lead."""
    raw = topic.get("trace_json")
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return []
    site = str(payload.get("site") or "")
    out = []
    for index, body in enumerate(payload.get("pages") or []):
        if str(body or "").strip():
            out.append({"url": site if index == 0 and site else "溯源读到的官方页",
                        "text": str(body)})
    return out


def consumed_refs(topic: dict) -> list[str]:
    """The posts this topic stands for, the representative included."""
    return list(dict.fromkeys(topic.get("consumed") or [topic["ref"]]))
