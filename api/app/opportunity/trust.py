"""How an entry earns the word 官方 - and the two states that are not it.

Three states, never two. The reference product this module borrows its shape from has 官方 and
社区核实中, which quietly puts "we never checked" into the second one; here that case is its own
answer, `unjudged`, and it is printed.

The reading is deliberately narrow: an entry is 官方 when **the project itself said it** - the
material is one of that project's own posts, or the entry's official URL appears in the material
that carried it. A quest platform listing somebody else's campaign is `community` however
official the campaign is, because a third party saying so is exactly what `community` means.
"""

from __future__ import annotations

from ..search.base import host_of

OFFICIAL = "official"
COMMUNITY = "community"
UNJUDGED = "unjudged"


def verdict(entry, material: dict, source: dict | None) -> tuple[str, str]:
    """(trust, basis) for one entry. `basis` is the sentence the page prints under it."""
    source = source or {}
    kind = str(source.get("kind") or "")
    target = str(source.get("target") or "").lstrip("@")
    author = str(material.get("source") or "").lstrip("@")

    if kind == "official" and target and target.lower() == author.lower():
        return OFFICIAL, "来自官方账号 @%s 自己的帖子" % target
    official_url = str(getattr(entry, "official_url", "") or "")
    host = host_of(official_url)
    text = str(material.get("text") or "").lower()
    if host and host in text:
        return OFFICIAL, "官方入口 %s 出现在材料里" % host
    if not kind and not author:
        return UNJUDGED, "没有来源可判"
    if kind in ("platform", "exchange", "catalog"):
        return COMMUNITY, "来自第三方平台或目录，官方未自述"
    return COMMUNITY, "来自搜索命中，非官方账号自述"
