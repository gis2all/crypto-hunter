"""Fetch one macro source. RSS is parsed with the stdlib; generic JSON APIs
read items through a dotted path with per-field dotted names.

Anything the source layer cannot prove is raised as SourceFetchError - the
slot records it beside the source and moves on to the next one.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET

import httpx

from .rules import parse_dt

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
}
TIMEOUT = 20.0
_MAX_SUMMARY = 800

_STRIP_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class SourceFetchError(RuntimeError):
    pass


def fetch(src: dict, timeout: float = TIMEOUT) -> list[dict]:
    """One source into [{url, title, summary, published_at}]."""
    kind = str(src.get("type") or "rss").lower()
    if kind == "rss":
        return _rss(str(src.get("url") or ""), timeout)
    if kind == "json":
        return _json(src, timeout)
    raise SourceFetchError("unsupported source type: %r" % (src.get("type"),))


def _get(url: str, timeout: float) -> httpx.Response:
    if not url:
        raise SourceFetchError("empty url")
    try:
        resp = httpx.get(url, headers=UA, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        return resp
    except Exception as exc:
        raise SourceFetchError("%s: %s" % (url, repr(exc)[:160])) from exc


def _clean(raw: str | None) -> str:
    text = html.unescape(str(raw or ""))
    text = _STRIP_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:_MAX_SUMMARY]


def _rss(url: str, timeout: float) -> list[dict]:
    try:
        root = ET.fromstring(_get(url, timeout).content)
    except ET.ParseError as exc:
        raise SourceFetchError("%s: bad xml %r" % (url, exc)) from exc
    out = []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        title = _text(node, ("title",))
        if not title:
            continue
        out.append({
            "url": _link(node),
            "title": title.strip()[:300],
            "summary": _clean(_text(node, ("description", "summary", "content"))),
            "published_at": _text(node, ("pubDate", "published", "updated")),
        })
    return out


def _text(node: ET.Element, names: tuple[str, ...]) -> str:
    for child in node:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag in names and child.text:
            return child.text
    return ""


def _link(node: ET.Element) -> str:
    for child in node:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "link":
            if child.text and child.text.strip():
                return child.text.strip()
            for key in ("href",):
                if child.attrib.get(key):
                    return child.attrib[key].strip()
    return ""


def _dig(payload: object, path: str) -> object:
    node: object = payload
    for part in str(path or "").split("."):
        if isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node


def _json(src: dict, timeout: float) -> list[dict]:
    try:
        payload = _get(str(src.get("url") or ""), timeout).json()
    except ValueError as exc:
        raise SourceFetchError("bad json: %r" % (exc,)) from exc
    fields = src.get("fields") or {}
    raw_items = _dig(payload, str(src.get("items_path") or "items"))
    if not isinstance(raw_items, list):
        raise SourceFetchError("items_path %r is not a list" % (src.get("items_path"),))
    out = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        title = _dig(entry, str(fields.get("title") or "title"))
        if not title:
            continue
        published = _dig(entry, str(fields.get("published_at") or "published_at"))
        parsed = parse_dt(str(published)) if published else None
        out.append({
            "url": str(_dig(entry, str(fields.get("url") or "url")) or ""),
            "title": str(title).strip()[:300],
            "summary": _clean(str(_dig(entry, str(fields.get("summary") or "summary")) or "")),
            "published_at": published if parsed is None and published else (
                parsed.isoformat() if parsed else None),
        })
    return out