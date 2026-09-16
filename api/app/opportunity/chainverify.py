"""Automatic chain verification: public registries first, official docs as the fallback.

No human step. `sync()` pulls Chainlist / CoinGecko / DefiLlama into `chain_registry`; names the
registries do not know are asked about once, with their own pages as evidence, by
`verify_unknown()`. A name with no evidence stays `unverified` instead of becoming a filter row
that looks like a public chain.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import httpx

from ..llm import client as llm
from ..store import db
from . import chainsearch, chains, model, schema

CHAINLIST = "https://chainid.network/chains.json"
COINGECKO = "https://api.coingecko.com/api/v3/asset_platforms"
DEFILLAMA = "https://api.llama.fi/chains"
TIMEOUT = 30.0
VERIFY_VERSION = "chain_verify.md@1"
VERIFY_BATCH = 5

_TESTNET = ("testnet", "sepolia", "goerli", "devnet", "holesky", "mumbai", "amoy",
            "chapel", "fuji", "coston", "kovan", "ropsten", "rinkeby", "mordor")

#: Established networks whose type/public reading does not need a model. Everything else that
#: only has a seed row goes through the official-docs pass.
TRUSTED_CHAINS = frozenset((
    "bitcoin", "ethereum", "solana", "bsc", "tron", "near", "cardano", "stacks", "kaspa",
    "aleo", "flare", "hyperliquid", "monero", "zcash", "multiversx", "icp", "cosmos",
    "osmosis", "stellar", "sui", "aptos", "avax", "sei", "injective", "sonic", "monad",
    "berachain", "megaeth", "somnia", "pharos", "assetchain", "rayls", "metal", "bttc",
    "base", "arbitrum", "optimism", "polygon", "ink", "xlayer", "robinhood", "giwa",
    "starknet", "blast", "mantle", "linea", "scroll", "zksync", "ronin", "arc",
))
_SOCIAL = ("x.com", "twitter.com")

#: Official entry points for names the search engines did not surface on their own. These are
#: URLs only - the type/public verdict still comes from the page text and the model.
OFFICIAL_HINTS = {
    "strium": ("https://strium.org/", "https://strium.org/blog/strium-announcement"),
    "txflow": ("https://www.txflow.com/", "https://docs.txflow.com/"),
    "zig": ("https://zigchain.com/",),
    "sovren": ("https://www.sovrentech.io/", "https://www.sovrentech.io/whitepaper"),
}


def _norm(value: object) -> str:
    return " ".join(str(value or "").lower().split())


def _squash(value: object) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", _norm(value))


def _testnet(name: object) -> bool:
    low = _norm(name)
    return any(marker in low for marker in _TESTNET)


def _recent(value: object, days: int = 7) -> bool:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - stamp).total_seconds() < days * 86400
    except (TypeError, ValueError):
        return False


def _aliases() -> dict[str, str]:
    out: dict[str, str] = {}
    for key, aliases in schema.CHAIN_ALIASES.items():
        out[_squash(key)] = key
        for alias in aliases:
            out[_squash(alias)] = key
    for key in schema.CHAIN_TYPES:
        out[_squash(key)] = key
        out[_squash(schema.ECOSYSTEM_LABELS.get(key, key))] = key
    return out


def _key_for(*names: object) -> str:
    aliases = _aliases()
    for name in names:
        if not name:
            continue
        for candidate in (_squash(name),
                          _squash(re.sub(r"\b(mainnet|chain|network|blockchain)\b",
                                         " ", _norm(name)))):
            if candidate in aliases:
                return aliases[candidate]
    first = _norm(names[0] if names else "")
    if re.match(r"^[a-z][a-z0-9 .\-]{1,29}$", first):
        return first
    return ""


def _get(url: str):
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        response = client.get(url, headers={"User-Agent": "crypto-hunter/1.0"})
        response.raise_for_status()
        return response.json()


def _merge(rows: dict[str, dict], key: str, label: str, kind: str, public: bool,
           source: str, evidence: str, confidence: str, priority: int) -> None:
    if not key:
        return
    current = rows.get(key)
    if current is None:
        rows[key] = {"key": key, "label": label, "type": kind, "is_public": public,
                     "source": source, "evidence_url": evidence, "confidence": confidence,
                     "priority": priority, "verified_at": db.now_iso()}
        return
    # A curated type (the static seed) wins over a registry guess; a real L2 parent can refine it.
    if priority > current["priority"]:
        if key not in schema.ECOSYSTEM_LABELS:
            current["label"] = label or current["label"]
        current["source"] = source
        current["evidence_url"] = evidence or current["evidence_url"]
        current["confidence"] = confidence
        current["priority"] = priority
        if key not in schema.CHAIN_TYPES or kind == "l2":
            current["type"] = kind
    current["is_public"] = bool(current["is_public"] or public)


def sync(tell=None) -> dict:
    """Refresh the registry from the public sources. A dead source is a count, not a failure."""
    rows: dict[str, dict] = {}
    # Official-docs verdicts survive a registry sync unless a public source knows the key. They
    # sit at priority 2: above the seed, below Chainlist.
    for key, existing in db.chain_registry_all().items():
        if existing.get("source") == "official_docs" and key not in TRUSTED_CHAINS:
            rows[key] = {**existing, "priority": 2}
    for key, kind in schema.CHAIN_TYPES.items():
        if key in rows:
            continue
        label = schema.ECOSYSTEM_LABELS.get(key, key[:1].upper() + key[1:])
        _merge(rows, key, label, kind, True, "seed", "",
               "high" if key in TRUSTED_CHAINS else "low", 0)
    counters = {"chainlist": 0, "coingecko": 0, "defillama": 0, "chains": 0}
    try:
        for item in _get(CHAINLIST) or []:
            name = str(item.get("name") or "")
            if not name or _testnet(name):
                continue
            key = _key_for(name, item.get("shortName"))
            parent = item.get("parent") or {}
            kind = "l2" if parent else "l1"
            explorers = item.get("explorers") or []
            rpc = item.get("rpc") or []
            evidence = str((explorers[0] or {}).get("url") or "") if explorers else ""
            _merge(rows, key, name, kind, bool(rpc and explorers), "chainlist",
                   evidence, "high", 3)
            counters["chainlist"] += 1
    except Exception as exc:
        counters["chainlist_error"] = str(exc)[:160]
    try:
        for item in _get(COINGECKO) or []:
            name = str(item.get("name") or "")
            if not name or _testnet(name):
                continue
            key = _key_for(name, item.get("shortname"), item.get("id"))
            kind = schema.CHAIN_TYPES.get(key, "l1")
            public = bool(item.get("chain_identifier") or item.get("native_coin_id"))
            _merge(rows, key, name, kind, public, "coingecko",
                   "https://api.coingecko.com/api/v3/asset_platforms", "medium", 2)
            counters["coingecko"] += 1
    except Exception as exc:
        counters["coingecko_error"] = str(exc)[:160]
    try:
        for item in _get(DEFILLAMA) or []:
            name = str(item.get("name") or "")
            if not name or _testnet(name):
                continue
            key = _key_for(name)
            kind = schema.CHAIN_TYPES.get(key, "l1")
            _merge(rows, key, name, kind, bool(item.get("tvl")), "defillama",
                   "https://api.llama.fi/chains", "medium", 1)
            counters["defillama"] += 1
    except Exception as exc:
        counters["defillama_error"] = str(exc)[:160]
    payload = []
    for row in rows.values():
        row = dict(row)
        row.pop("priority", None)
        payload.append(row)
    counters["chains"] = db.chain_registry_put(payload)
    chains.reset()
    if tell:
        tell("链注册表：%d 条（chainlist %d / coingecko %d / defillama %d）"
             % (counters["chains"], counters["chainlist"], counters["coingecko"],
                counters["defillama"]))
    return counters


def _pages_for_key(key: str) -> list[dict]:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT o.source_ref, o.official_url, p.text, p.trace_json FROM opportunities o"
            " LEFT JOIN opportunity_posts p ON p.url = o.source_ref"
            " WHERE o.ecosystem = ? AND o.user_state = 'active'"
            " ORDER BY o.discovered_at DESC LIMIT 1", (key,)).fetchone()
    finally:
        conn.close()
    official_host = ""
    if row is not None and row["official_url"]:
        official_host = re.sub(r"^https?://", "", str(row["official_url"])).split("/")[0].lower()
    tokens = [token for token in re.split(r"[^a-z0-9]+", key.lower()) if len(token) >= 3]

    def usable(url: str) -> bool:
        low = str(url or "").lower()
        if not low or any(social in low for social in _SOCIAL):
            return False
        if official_host and official_host in low:
            return True
        return any(token in low for token in tokens) or not tokens

    pages: list[dict] = []
    seen: set[str] = set()

    def add(url: str, text: str) -> None:
        if not url or not text or url in seen:
            return
        seen.add(url)
        pages.append({"url": url, "text": text[:1800]})
    if row is not None:
        raw = row["trace_json"]
        if raw:
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                payload = {}
            for page in payload.get("page_sources") or []:
                if str(page.get("text") or "").strip():
                    url = str(page.get("url") or "")
                    if usable(url):
                        add(url, str(page["text"]))
            for index, body in enumerate(payload.get("pages") or []):
                if str(body or "").strip():
                    site = str(payload.get("site") or "") if index == 0 else ""
                    if usable(site):
                        add(site, str(body))
    if len(pages) < 4:
        for query in ("%s blockchain" % key, "%s layer 1" % key,
                      "%s docs" % key, "%s whitepaper" % key):
            try:
                results = chainsearch.search(query, match=key)[:3]
            except Exception:
                continue
            for result in results:
                if result["url"] in seen or not usable(result["url"]):
                    continue
                add(result["url"], "%s\n%s" % (result["title"], result["snippet"]))
            if len(pages) >= 6:
                break
    if key in OFFICIAL_HINTS:
        for url in OFFICIAL_HINTS.get(key, ()):
            try:
                with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
                    response = client.get(url, headers={"User-Agent": "crypto-hunter/1.0"})
                    response.raise_for_status()
                body = chainsearch._text(response.text)[:1800]
            except Exception:
                continue
            if body:
                add(url, body)
    return [page for page in pages if page["url"]][:5]


def verify_unknown(limit: int = 20, tell=None, force: bool = False) -> dict:
    """Ask the model about names no registry knew, with their own pages as the only evidence."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT ecosystem, count(*) AS n FROM opportunities"
            " WHERE user_state = 'active' AND ecosystem IS NOT NULL"
            "   AND ecosystem NOT IN ('未判定', '多链')"
            " GROUP BY ecosystem ORDER BY n DESC").fetchall()
    finally:
        conn.close()
    known = chains.registry()
    unknown = []
    for row in rows:
        key = str(row["ecosystem"])
        meta = known.get(key) or {}
        if force and meta.get("source") == "official_docs":
            unknown.append(key)
        elif not meta.get("is_public"):
            if force or meta.get("source") != "official_docs" \
                    or not _recent(meta.get("verified_at")):
                unknown.append(key)
        elif meta.get("source") == "seed" and meta.get("confidence") == "low":
            # A curated seed without registry evidence still needs a source (e.g. the six new L1s).
            unknown.append(key)
    unknown = unknown[:max(1, int(limit))]
    counters = {"checked": 0, "verified": 0, "unverified": 0, "no_pages": 0}
    for start in range(0, len(unknown), VERIFY_BATCH):
        batch = []
        for key in unknown[start:start + VERIFY_BATCH]:
            pages = _pages_for_key(key)
            if pages:
                batch.append({"key": key,
                              "name": schema.ECOSYSTEM_LABELS.get(
                                  key, " ".join(part[:1].upper() + part[1:]
                                                for part in key.split())),
                              "pages": pages})
            else:
                counters["no_pages"] += 1
        if not batch:
            continue
        messages = [
            {"role": "system", "content": model.read_prompt("chain_verify.md")},
            {"role": "user", "content": json.dumps({"items": batch}, ensure_ascii=False)},
        ]
        try:
            raw = llm.complete_json(messages, purpose="opportunity_chain_verify",
                                    prompt_version=VERIFY_VERSION)
        except llm.LLMUnavailable:
            continue
        by_key = {item["key"]: item for item in batch}
        existing = chains.registry()
        writes = []
        for item in (raw or {}).get("items") or []:
            key = str((item or {}).get("key") or "")
            source = by_key.get(key)
            if source is None:
                continue
            counters["checked"] += 1
            urls = {page["url"] for page in source["pages"]}
            evidence = str(item.get("evidence_url") or "")
            quote = str(item.get("quote") or "").strip()
            kind = str(item.get("type") or "unverified").lower()
            public = bool(item.get("is_public"))
            if kind not in {"l1", "l2", "appchain", "testnet"} or evidence not in urls \
                    or not quote or any(social in evidence.lower() for social in _SOCIAL):
                kind, public = "unverified", False
                evidence = ""
            if kind == "unverified":
                counters["unverified"] += 1
                previous = existing.get(key) or {}
                if previous.get("is_public") and previous.get("type") in {
                        "l1", "l2", "appchain", "testnet"}:
                    # A failed retry must not downgrade a chain that already has evidence.
                    continue
            else:
                counters["verified"] += 1
            writes.append({"key": key, "label": source["name"], "type": kind,
                           "is_public": public, "source": "official_docs",
                           "evidence_url": evidence, "confidence": "medium",
                           "verified_at": db.now_iso()})
        if writes:
            db.chain_registry_put(writes)
            chains.reset()
        if tell:
            tell("核验未知链：%d 个" % len(writes))
    return counters


_GENERIC_KEYS = frozenset((
    "metric", "metal", "flare", "sonic", "blast", "mantle", "linea", "scroll",
    "stacks", "injective", "optimism", "ink", "arc", "base", "near", "sui", "sei",
    "tron", "zig", "avax", "cosmos", "solana", "ethereum",
))


def _match_registry(text: str, organizer: str = "") -> set[str]:
    """Registry keys whose label/key appears as a whole phrase in the card's own words."""
    low = _norm(text)
    handle = _squash(organizer)
    found: set[str] = set()
    for key, row in chains.registry().items():
        if not row.get("is_public") or key in _GENERIC_KEYS:
            continue
        for raw in (row.get("label"), key):
            label = _norm(raw)
            if len(_squash(label)) < 6:
                continue
            if re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(label), low):
                found.add(key)
                break
        if len(_squash(key)) >= 6 and _squash(key) in handle:
            found.add(key)
    return found


def backfill_cards(tell=None) -> dict:
    """Use the registry to pull 未判定 cards back to a chain (or 多链 when several match)."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT slug, title, organizer, summary FROM opportunities"
            " WHERE user_state = 'active' AND ecosystem = '未判定'").fetchall()
    finally:
        conn.close()
    counters = {"cards": len(rows), "single": 0, "multi": 0, "left": 0}
    for row in rows:
        found = _match_registry("%s %s" % (row["title"] or "", row["summary"] or ""),
                                row["organizer"] or "")
        if len(found) == 1:
            db.opportunity_set_ecosystem(row["slug"], next(iter(found)), checked=True)
            counters["single"] += 1
        elif len(found) > 1:
            db.opportunity_set_ecosystem(row["slug"], schema.MULTI, checked=True)
            counters["multi"] += 1
        else:
            counters["left"] += 1
    if tell:
        tell("未判定回填：%d 张，补一条 %d / 多链 %d / 仍留 %d"
             % (len(rows), counters["single"], counters["multi"], counters["left"]))
    return counters


def reaudit_multi(tell=None) -> dict:
    """Keep 多链 only when the card itself names two chains; otherwise settle on one or retry."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT o.slug, o.title, o.organizer, o.summary, p.trace_json"
            " FROM opportunities o LEFT JOIN opportunity_posts p ON p.url = o.source_ref"
            " WHERE o.user_state = 'active' AND o.ecosystem = '多链'").fetchall()
    finally:
        conn.close()
    counters = {"cards": len(rows), "kept": 0, "single": 0, "reset": 0}
    for row in rows:
        page_text = ""
        if row["trace_json"]:
            try:
                payload = json.loads(row["trace_json"])
            except (TypeError, ValueError):
                payload = {}
            page_text = " ".join(str(body or "") for body in payload.get("pages") or [])[:4000]
        text = "%s %s %s" % (row["title"] or "", row["summary"] or "", page_text)
        named = chainsearch._chains_in(text)
        if len(named) >= 2:
            counters["kept"] += 1
            continue
        if len(named) == 1:
            db.opportunity_rechain(row["slug"], next(iter(named)))
            counters["single"] += 1
            continue
        found = _match_registry(text, row["organizer"] or "")
        if len(found) == 1:
            db.opportunity_rechain(row["slug"], next(iter(found)))
            counters["single"] += 1
        else:
            db.opportunity_reset_ecosystem(row["slug"])
            counters["reset"] += 1
    if tell:
        tell("多链复核：%d 张，保留 %d / 归一条 %d / 回到未判定 %d"
             % (len(rows), counters["kept"], counters["single"], counters["reset"]))
    return counters


def main() -> None:
    print(sync(tell=print))
    print(verify_unknown(tell=print))
    print(backfill_cards(tell=print))
    print(reaudit_multi(tell=print))


if __name__ == "__main__":
    main()
