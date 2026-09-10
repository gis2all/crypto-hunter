"""Semantic de-duplication for cards that describe the same project event.

The deterministic project/event keys are deliberately cheap: they catch exact spelling changes,
not synonyms such as `免费打新` / `免费 Mint 体验`. Candidate groups are built from shared source
posts and rare title tokens; the model is only asked to split one candidate group into distinct
events. It never sees the whole shelf, and a failed call leaves the cards untouched for retry.
"""

from __future__ import annotations

import json
import re

from ..llm import client as llm
from ..store import db
from . import model

DEDUP_VERSION = "dedup.md@3"
MAX_CARDS = 1000
MAX_MERGE = 4

_WORDS = frozenset((
    "the", "and", "for", "with", "from", "this", "that", "new", "live", "update",
    "free", "mint", "nft", "nfts", "token", "tokens", "airdrop", "launch", "launchpad",
    "testnet", "mainnet", "project", "protocol", "app", "apps", "agent", "agents", "ai",
    "chain", "robinhood", "solana", "bitcoin", "ethereum", "base", "bsc", "sui", "tron",
    "near", "cosmos", "points", "quest", "campaign", "public", "sale", "today", "last",
    "day", "whitelist", "genesis", "defi", "vault", "meme", "memecoin", "wallet", "binance",
    "earn", "activity", "boost", "devnet", "launch", "launchpad", "usd", "usdt", "usdc",
    "btc", "eth", "sol", "bnb", "免费", "铸造", "白名单", "空投", "活动", "任务",
    "项目", "上线", "更新", "公告", "今天", "最后", "美元", "积分", "测试网", "主网",
    "质押", "推出", "存入", "发射台", "公平发射", "系列", "代币", "流动性质押", "指南",
    "教程", "开放", "领取", "奖励", "池", "生态", "合约", "钱包", "交易",
))


def _tokens(text: object) -> set[str]:
    body = str(text or "").lower()
    found = set(re.findall(r"\$[a-z0-9]+|[a-z]{4,}|[\u4e00-\u9fff]{2,}", body))
    return {token for token in found if token not in _WORDS}


def _groups(cards: list[dict]) -> list[list[dict]]:
    """Connected components from source posts and rare title tokens."""
    parent = {card["slug"]: card["slug"] for card in cards}

    def find(slug: str) -> str:
        while parent[slug] != slug:
            parent[slug] = parent[parent[slug]]
            slug = parent[slug]
        return slug

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    tokens = {card["slug"]: _tokens(card.get("title")) for card in cards}
    frequency: dict[str, int] = {}
    for found in tokens.values():
        for token in found:
            frequency[token] = frequency.get(token, 0) + 1

    by_source: dict[str, str] = {}
    for card in cards:
        source = str(card.get("source_ref") or "")
        if source:
            if source in by_source:
                union(card["slug"], by_source[source])
            else:
                by_source[source] = card["slug"]
    for index, left in enumerate(cards):
        for right in cards[index + 1:]:
            shared = tokens[left["slug"]] & tokens[right["slug"]]
            rare_one = any(len(token) >= 4 and frequency.get(token, 0) <= 4
                           for token in shared)
            if len(shared) >= 2 or rare_one:
                union(left["slug"], right["slug"])

    buckets: dict[str, list[dict]] = {}
    for card in cards:
        buckets.setdefault(find(card["slug"]), []).append(card)
    return [group for group in buckets.values()
            if len(group) > 1 and any(not card.get("dedup_at") for card in group)]


def _ask(group: list[dict]) -> list[list[str]]:
    payload = [{
        "slug": card["slug"], "title": card.get("title") or "",
        "kind": card.get("kind") or "", "category": card.get("category") or "",
        "summary": str(card.get("summary") or "")[:300],
        "organizer": card.get("organizer") or "",
        "deadline": card.get("deadline") or "", "source_ref": card.get("source_ref") or "",
    } for card in group]
    messages = [
        {"role": "system", "content": model.read_prompt("dedup.md")},
        {"role": "user", "content": json.dumps({"cards": payload}, ensure_ascii=False)},
    ]
    raw = llm.complete_json(messages, purpose="opportunity_dedup", prompt_version=DEDUP_VERSION)
    allowed = {card["slug"] for card in group}
    seen: set[str] = set()
    out: list[list[str]] = []
    for item in (raw or {}).get("groups") or []:
        slugs = []
        for slug in (item or {}).get("slugs") or []:
            slug = str(slug)
            if slug in allowed and slug not in seen:
                seen.add(slug)
                slugs.append(slug)
        if 1 < len(slugs) <= MAX_MERGE:
            out.append(slugs)
    return out


def _merge(group: list[dict], slugs: list[str]) -> bool:
    cards = [card for card in group if card["slug"] in slugs]
    if len(cards) < 2:
        return False
    keep = min(cards, key=lambda card: (card.get("trust") != "official",
                                        card.get("user_state") != "active",
                                        card.get("discovered_at") or ""))
    latest = max(cards, key=lambda card: card.get("discovered_at") or "")
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE opportunities SET kind = ?, category = ?, title = ?, organizer = ?,"
            " summary = ?, risk = ?, barrier = ?, maturity = ?, started_at = ?, deadline = ?,"
            " status = ?, reason = ?, heat = ?, heat_basis = ?, extract_version = ?,"
            " novelty = ?, event_group = ?, dedup_at = ?, updated_at = ? WHERE slug = ?",
            (latest["kind"], latest["category"], latest["title"],
             latest["organizer"] or keep["organizer"], latest["summary"], latest["risk"],
             latest["barrier"], latest["maturity"], latest["started_at"], latest["deadline"],
             latest["status"], latest["reason"], latest["heat"], latest["heat_basis"],
             latest["extract_version"], latest["novelty"], keep["slug"], db.now_iso(),
             db.now_iso(), keep["slug"]))
        steps = latest.get("steps") or []
        if steps:
            conn.execute("DELETE FROM opportunity_steps WHERE slug = ?", (keep["slug"],))
            for index, step in enumerate(steps):
                conn.execute(
                    "INSERT INTO opportunity_steps (slug, ordinal, title, body, source_url, source_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (keep["slug"], index, step.get("title"), step.get("body"),
                     step.get("source_url"), step.get("source_at")))
        for card in cards:
            if card["slug"] == keep["slug"]:
                continue
            conn.execute("DELETE FROM opportunity_steps WHERE slug = ?", (card["slug"],))
            conn.execute("DELETE FROM opportunities WHERE slug = ?", (card["slug"],))
    finally:
        conn.close()
    return True


def run(*, limit: int | None = None, tell=None) -> dict:
    counters = {"groups": 0, "calls": 0, "merged": 0, "failed": 0}
    if not llm.configured():
        return counters
    cards = db.opportunity_items(limit=MAX_CARDS)
    groups = _groups(cards)
    if limit is not None:
        groups = groups[:limit]
    total = len(groups) or 1
    for index, group in enumerate(groups, start=1):
        counters["groups"] += 1
        try:
            counters["calls"] += 1
            merged_groups = _ask(group)
        except Exception:  # noqa: BLE001 - a failed call leaves this group for the next scan
            counters["failed"] += 1
            continue
        for slugs in merged_groups:
            if _merge(group, slugs):
                counters["merged"] += 1
        conn = db.connect()
        try:
            for card in group:
                conn.execute(
                    "UPDATE opportunities SET event_group = COALESCE(event_group, slug),"
                    " dedup_at = ? WHERE slug = ?", (db.now_iso(), card["slug"]))
        finally:
            conn.close()
        if tell:
            tell(index, total)
    return counters
