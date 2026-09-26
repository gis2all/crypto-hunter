"""Turn stored report text into segments the frontend can render safely.

Report bodies are written as a tiny inline-markup dialect (`<b>`, `<code>`,
`<a href="...">label</a>`). The API never ships raw HTML: it ships segments, and the
SPA renders text nodes and anchors. That keeps `dangerouslySetInnerHTML` out of the
codebase and keeps the injection surface at zero.

A bare URL in that text - or a markdown link the writer produced out of habit - becomes
a link segment too. Printing a URL and leaving it dead is the one outcome a reader
cannot use, and leaving that to the writer's markup discipline meant links arrived dead
whenever the model wrote `https://...` instead of the anchor dialect.
"""

from __future__ import annotations

import re
from html import unescape

ANCHOR = re.compile(r'<a\s+href="([^"]+)"\s*>(.*?)</a>', re.S)
BOLD = re.compile(r"<b>(.*?)</b>", re.S)
CODE = re.compile(r"<code>(.*?)</code>", re.S)
#: `[说明](url)` - what the model writes when it forgets the anchor dialect.
MARKDOWN = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
#: A URL standing on its own in the text. Trailing CJK punctuation is excluded so a
#: link at the end of a sentence does not swallow the full stop.
URL = re.compile(r"https?://[^\s<>\"'）)】，。；：！？、]+")
TAG = re.compile(r"</?[a-zA-Z][^>]*>")


def segments(text: str | None) -> list[dict]:
    """Split one stored body into `text` / `link` / `bold` / `code` segments."""
    if not text:
        return []
    out: list[dict] = []
    pos = 0
    for m in ANCHOR.finditer(text):
        if m.start() > pos:
            out.extend(_plain(text[pos:m.start()]))
        out.append({"kind": "link", "text": unescape(_strip(m.group(2))), "url": m.group(1)})
        pos = m.end()
    if pos < len(text):
        out.extend(_plain(text[pos:]))
    return [s for s in out if s["text"] != ""]


def _plain(chunk: str) -> list[dict]:
    """bold / code / markdown link / bare URL, in the order they appear in the text."""
    out: list[dict] = []
    pos = 0
    for pattern, kind in ((BOLD, "bold"), (CODE, "code"), (MARKDOWN, "link"), (URL, "link")):
        for m in pattern.finditer(chunk):
            if m.start() < pos:
                # Already consumed by an earlier pattern (a URL inside `[x](url)`, say).
                continue
            if m.start() > pos:
                out.append({"kind": "text", "text": unescape(_strip(chunk[pos:m.start()]))})
            if kind == "link":
                if m.lastindex:
                    out.append({"kind": "link", "text": unescape(_strip(m.group(1))),
                                "url": m.group(2)})
                else:
                    out.append({"kind": "link", "text": m.group(0), "url": m.group(0)})
            else:
                out.append({"kind": kind, "text": unescape(_strip(m.group(1)))})
            pos = m.end()
    if pos < len(chunk):
        out.append({"kind": "text", "text": unescape(_strip(chunk[pos:]))})
    return out


def _strip(chunk: str) -> str:
    return TAG.sub("", chunk)


def plain(text: str | None) -> str:
    """Everything readable in one string, for lengths and exports."""
    return "".join(s["text"] for s in segments(text))
