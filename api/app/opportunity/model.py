"""The two questions this module asks a model, and the repair pass they share.

One place for the parts that are the same in both passes: where the prompts live, how a
material is laid out for the model, how much of it is sent, and what happens when the answer
does not validate.

The repair pass is copied from the writing pass on purpose (`report/research.py`): the answer
that failed goes back **with the finding that failed it**, and the ask is to correct that answer
rather than to write a new one. A second read of the same material is how the same field goes
missing twice. Unlike the writing pass, only the expensive question is repaired - a
classification whose only problem is a category the build does not have is recorded as it
stands, because one more call to fix a label costs as much as the label was worth.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from ..llm import client as llm

log = logging.getLogger("crypto-hunter.opportunity")

PROMPT_DIR = Path(__file__).with_name("prompts")

#: How much of a material the model sees. A post is a few hundred characters and is never cut;
#: a listing page is cut, because the alternative is sending a megabyte of navigation to read
#: six rows - and the rows are at the top, which is what the cut keeps.
POST_CHARS = 4000
PAGE_CHARS = 12000
#: How many traced pages one extraction may see. The trace reads at most five (two sites,
#: three profiles); the extraction takes the first three so a long docs page cannot push
#: the post itself out of the prompt.
PAGES_MAX = 3

#: What a rejected answer is told before it is asked again. Its own JSON goes back with it.
REPAIR_HEAD = """上一次的回答没有通过校验：
%s

请针对上面这些问题**重写**一次，仍然只输出 JSON，不要解释。材料没写的东西一律留空，不要补。"""


def read_prompt(name: str) -> str:
    """One prompt file, read at call time so a prompt edited on disk is used by the next scan."""
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def material_message(material: dict, *, chars: int) -> str:
    """One material as the user half of the question, in the shape both prompts describe.

    A traced lead arrives with the pages the trace read (the project's own site and account), and
    they are appended as a second section rather than replacing the post: the post is what was
    found, the pages are what proves whose it is - and the extraction is asked to write from both
    (`opportunity/trace.py`).
    """
    source = str(material.get("source") or "")
    kind = material.get("source_kind")
    label = ("X 账号 @%s" % source) if kind == "post" else ("站点 %s" % source)
    text = str(material.get("text") or "")[:chars]
    parts = [
        "## 材料",
        "来源：%s" % label,
        "URL：%s" % (material.get("url") or material.get("ref") or ""),
        "时间：%s" % (material.get("at") or ""),
        "",
        "正文：",
        text,
    ]
    pages = material.get("official_pages") or []
    if pages:
        parts += ["", "## 官方来源（溯源读到的，优先用它）"]
        for index, page in enumerate(pages[:PAGES_MAX], start=1):
            body = str(page.get("text") or "")[:PAGE_CHARS]
            parts += ["### 官方页 %d：%s" % (index, page.get("url") or ""), body, ""]
    return "\n".join(parts)


def question(name: str, version: str, purpose: str, material: dict, reader: Callable,
             *, chars: int, repair: bool = False) -> tuple[object, list[str]]:
    """Ask one question about one material, and repair the answer once when asked to.

    `reader` is the validator from `schema.py`: it returns the value it could read plus the
    problems it found, and a value of `None` means nothing could be read at all. Both are
    returned to the caller - the problems are what a scan counts and a human debugs.
    """
    messages = [
        {"role": "system", "content": read_prompt(name)},
        {"role": "user", "content": material_message(material, chars=chars)},
    ]
    raw = llm.complete_json(messages, purpose=purpose, prompt_version=version)
    value, issues = reader(raw)
    if not repair or not issues:
        return value, issues
    log.info("opportunity: %s answer rejected (%d issues), asking once more",
             purpose, len(issues))
    messages += [
        {"role": "assistant", "content": json.dumps(raw, ensure_ascii=False)[:6000]},
        {"role": "user", "content": REPAIR_HEAD % "\n".join(issues[:8])},
    ]
    again = llm.complete_json(messages, purpose=purpose + "_repair",
                              prompt_version=version + "+repair")
    value, issues = reader(again)
    return value, issues
