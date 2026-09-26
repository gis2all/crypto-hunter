"""Load one reference round into the database so the UI can be built and judged.

The collectors are still unimplemented (`sources/*` raise `step-0 probe pending`), so a
round cannot be produced end to end yet. This module reads one *finished* round from
the earlier pipeline (read-only, never written to) and writes it into this project's
schema: token master rows, the round, its per-token decisions, the immutable metric and
safety snapshots, the round-level facts, the written report payloads and the lexicon.

It is a fixture loader, not a pipeline. Two things it must never do:

* pretend to be collection - it reads a directory and says so in `run.trigger`,
* invent a number - anything the reference round did not record is left NULL, which the
  API renders as the missing-value marker.

Usage: `uv --directory api run python -m app.seed_demo`
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .core.models import RunStatus
from .store import db

DEFAULT_RUN = r"D:\Code\arc\push\out\run_20260923_2000"
DEFAULT_REGISTRY = r"D:\Code\arc\push\registry.json"

# Counts the reference round recorded for itself. Each rule's numbers are independent
# per-rule tallies over the 3,199-token universe; they sum to universe - candidates.
RECORDED_RULE_COUNTS = [
    ("market_cap", "市值", 2417),
    ("liquidity", "流动性", 318),
    ("holders", "持有人", 262),
    ("top10", "Top10 集中度", 121),
    ("bundling", "捆绑率", 46),
    ("honeypot", "貔貅", 18),
    ("volume_24h", "24h 成交", 4),
    ("source_unverified", "源码已验证", 22),
    ("lp_unlocked", "LP 已锁", 9),
    ("dangerous_perm", "无危险权限", 0),
    ("entrapment", "陷阱占比", 4),
]

METRIC_KEYS = ("市值", "流动性", "24h 成交", "换手", "持有人", "Top10", "捆绑", "税", "貔貅")
HEAD = re.compile(r"^<b>▍([^·]+)·([^·]+)·(.*)</b>$")
SECTION = re.compile(r"^【([^】]+)】(.*)$")
CA_RE = re.compile(r"(0x[0-9a-fA-F]{40})")
LLM_MARK = re.compile(r"\{\{LLM:[^}]+\}\}")


def read(path: Path) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def split_pipes(text: str) -> list[str]:
    out, depth, cur = [], 0, []
    for ch in text:
        if ch in "（(":
            depth += 1
        elif ch in "）)":
            depth = max(0, depth - 1)
        if ch == "｜" and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [c.strip() for c in out if c.strip()]


def parse_sections(text: str) -> tuple[str, dict[str, str]]:
    lines = text.splitlines()
    head = lines[0].strip() if lines else ""
    sections: dict[str, str] = {}
    cur: str | None = None
    buf: list[str] = []
    for raw in lines[1:]:
        line = raw.strip()
        m = SECTION.match(line)
        if m:
            if cur:
                sections[cur] = "\n".join(buf).strip()
            cur = m.group(1).strip()
            buf = [m.group(2).strip()] if m.group(2).strip() else []
            continue
        if cur is not None:
            buf.append(raw)
    if cur:
        sections[cur] = "\n".join(buf).strip()
    return head, sections


def as_kv(block: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    ordered = sorted(METRIC_KEYS, key=len, reverse=True)
    for line in block.splitlines():
        if line.startswith("较上期"):
            continue
        for chunk in split_pipes(line):
            for key in ordered:
                if chunk.startswith(key):
                    pairs.setdefault(key, chunk[len(key):].strip())
                    break
    return pairs


def safety_display(block: str) -> dict:
    """The 安全 block as written: the two pill rows and the chips, in order.

    Kept as text because the round's own words carry detail the spread facts do not
    (a source-match note, a share the raw feed reports in a different unit).
    """
    rows: dict[str, str] = {}
    for line in block.splitlines():
        if "：" in line:
            key, val = line.split("：", 1)
            rows[key.strip()] = val.strip()
    chips = []
    for chunk in split_pipes(rows.get("筹码与团队", "")):
        parts = chunk.rsplit(" ", 1)
        chips.append([parts[0], parts[1]] if len(parts) == 2 else [chunk, ""])
    return {
        "perms": split_pipes(rows.get("合约权限", "")),
        "lp": rows.get("LP 锁定", ""),
        "chips": chips,
    }


def delta_pairs(block: str) -> list[dict]:
    for line in block.splitlines():
        if line.startswith("较上期"):
            body = line.split("：", 1)[-1]
            out = []
            for chunk in split_pipes(body):
                parts = chunk.split(" ", 1)
                if len(parts) == 2:
                    out.append({"key": parts[0], "value": parts[1]})
            return out
    return []


def as_number(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    return float(m.group(0)) if m else None


def iso(seconds: float | None) -> str | None:
    if not seconds:
        return None
    return datetime.fromtimestamp(seconds / 1000.0, tz=timezone.utc).isoformat()


def metrics_from(raw: dict) -> dict:
    stat = raw.get("_stat") or {}
    pools = raw.get("_pools") or []
    detail = ((raw.get("_e") or {}).get("ds") or {})
    main_liq = pools[0]["liq"] if pools else None
    total_liq = sum(p.get("liq") or 0 for p in pools) or None
    mcap = raw.get("mcap")
    vol24 = raw.get("vol24")
    return {
        "ca": raw["address"].lower(),
        "symbol": raw.get("sym"),
        "market_cap_usd": mcap,
        "liquidity_usd": raw.get("liq"),
        "volume_24h_usd": vol24,
        "turnovers": (vol24 / mcap) if (vol24 and mcap) else None,
        "holders": raw.get("holders"),
        "top10_pct": _pct(raw.get("top10")),
        "bundling_pct": _pct(raw.get("bundler")),
        "honeypot_pct": _pct(raw.get("entrap")),
        "honeypot_note": raw.get("honeypot"),
        "honeypot_detected": None,
        "sniper_pct": _pct(raw.get("sniper")),
        "insider_pct": _pct(stat.get("rat")),
        "buy_tax_pct": raw.get("btax"),
        "sell_tax_pct": raw.get("stax"),
        "pools": len(pools) or None,
        "pool_volume_usd": sum(p.get("vol24") or 0 for p in pools) or None,
        "main_pool": pools[0].get("name") if pools else None,
        "main_pool_share_pct": (100.0 * main_liq / total_liq) if (main_liq and total_liq) else None,
        "price_usd": raw.get("price"),
        "price_series": [p[1] for p in ((raw.get("path") or {}).get("journey") or [])],
        "price_ath_usd": (raw.get("path") or {}).get("ath"),
        "drawdown_pct": (raw.get("path") or {}).get("dd"),
        "price_change_24h_pct": raw.get("chg24"),
        "launchpad": raw.get("launchpad"),
        "socials": _socials(detail),
        "websites": list(raw.get("ds_web") or []),
        "dex_pair": raw.get("pair"),
        "gmgn_url": "https://gmgn.ai/arc/token/%s" % raw["address"].lower(),
        "debot_url": "https://debot.ai/token/arc/%s" % raw["address"].lower(),
        "dexscreener_url": "https://dexscreener.com/arc/%s" % raw["address"].lower(),
        "liquidity_rank": raw.get("liq_rank"),
        "volume_rank": raw.get("vol_rank"),
        "holders_rank": raw.get("hold_rank"),
        "created_at": iso(detail.get("created")),
        "provenance": {"market_cap_usd": raw.get("src") or "dexscreener"},
    }


def safety_from(raw: dict) -> dict:
    sec = raw.get("sec") or {}
    stat = raw.get("_stat") or {}
    by = []
    if sec.get("gp"):
        by.append("GoPlus")
    if sec.get("match") and sec.get("match") != "none":
        by.append("Sourcify")
    return {
        "source_verified": sec.get("verified"),
        "source_verified_by": by,
        "source_match": sec.get("match"),
        "lp_locked": sec.get("locked"),
        "lp_note": sec.get("lock_basis"),
        "dangerous_perms": sec.get("dang"),
        "dev_holding_pct": _pct(stat.get("dev")),
        "entrapment_pct": _pct(raw.get("entrap")),
        "insider_pct": _pct(stat.get("rat")),
        "sniper_pct": _pct(raw.get("sniper")),
        "bundling_pct": _pct(raw.get("bundler")),
        "top10_pct": _pct(raw.get("top10")),
        "holders": raw.get("holders"),
        "dev_created_count": ((raw.get("_e") or {}).get("stat") or {}).get("creator_created_count"),
    }


def _pct(value) -> float | None:
    return None if value is None else round(float(value) * 100, 4)


def _socials(detail: dict) -> dict:
    out = {}
    for item in ((detail.get("info") or {}).get("socials") or []):
        kind = item.get("type")
        if kind and kind not in out:
            out[kind] = item.get("url")
    return out


def write_round(run_dir: Path, registry_path: Path) -> dict:
    data = json.loads(read(run_dir / "data.json"))
    commentary = json.loads(read(run_dir / "commentary.json"))
    notes = commentary.get("per_token") or {}
    sent = json.loads(read(run_dir / "sent.json"))
    registry = _registry(registry_path)
    lexicon_updated = _registry_meta(registry_path)

    top = {t["address"].lower(): t for t in data["top"]}
    ranked = sorted(data["top"], key=lambda t: -(t.get("final") or 0))
    scanned = data["scanned"]
    universe = int(data.get("universe") or 0)
    candidates = int(data.get("candidates") or len(scanned))
    selected = len(ranked)

    _reset_db()
    db.init_db()
    now = db.now_iso()

    chain_cfgs = config.load_chains()
    conn = db.connect()
    try:
        _write_chains(conn, chain_cfgs, now)
        version_id = _write_settings(conn, chain_cfgs["arc"], now)
        run_id = "demo-20260923-2000"
        _write_run(conn, run_id, version_id, universe, candidates, selected, sent, data, now)
        _write_tokens(conn, scanned, now)
        _write_items(conn, run_id, ranked, scanned)
        _write_snapshots(conn, run_id, scanned, now)
        _write_summary(conn, run_id, run_dir, data, sent, now)
        _write_reports(conn, run_id, ranked, run_dir, top, notes, now)
        _write_lexicon(conn, registry, top, lexicon_updated or now)
    finally:
        conn.close()

    return {
        "run_id": run_id,
        "universe": universe,
        "candidates": candidates,
        "selected": selected,
        "lexicon": len(registry),
    }


def _registry(path: Path) -> dict:
    if not path or not path.exists():
        return {}
    try:
        raw = json.loads(read(path))
    except Exception:
        return {}
    return {k.lower(): v for k, v in raw.items() if k != "_meta" and isinstance(v, dict)}


def _registry_meta(path: Path) -> str | None:
    """The lexicon file dates its own content. That date is what the page shows as
    「最近更新」- not the moment this database happened to import the file."""
    if not path or not path.exists():
        return None
    try:
        raw = json.loads(read(path))
    except Exception:
        return None
    meta = raw.get("_meta") or {}
    updated = meta.get("updated")
    return str(updated) if updated else None


def _reset_db() -> None:
    """The demo seed owns this database. Dropping the file is the migration."""
    for candidate in (config.DB_PATH, Path(str(config.DB_PATH) + "-wal"), Path(str(config.DB_PATH) + "-shm")):
        if candidate.exists():
            candidate.unlink()
    print("reset", config.DB_PATH)


def _write_chains(conn: sqlite3.Connection, chains: dict, now: str) -> None:
    for chain_id, cfg in chains.items():
        conn.execute(
            "INSERT OR REPLACE INTO chains (id, label, enabled, config_json, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (chain_id, cfg.get("label") or chain_id, 1 if cfg.get("enabled") else 0,
             json.dumps(cfg, ensure_ascii=False), now),
        )


SETTINGS_SECTIONS = ("schedule", "pipeline", "report", "alerts")


def _write_settings(conn: sqlite3.Connection, cfg: dict, now: str) -> int:
    payload = {k: cfg.get(k) for k in SETTINGS_SECTIONS}
    cur = conn.execute(
        "INSERT INTO settings_versions (chain_id, version, payload_json, note, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        ("arc", 1, json.dumps(payload, ensure_ascii=False), "from config/chains.yaml", now),
    )
    return int(cur.lastrowid)


def _write_run(conn, run_id, version_id, universe, candidates, selected, sent, data, now) -> None:
    ok = sum(1 for v in sent.values() if v.get("ok"))
    _ = ok
    conn.execute(
        "INSERT INTO runs (id, run_key, chain_id, trigger, scheduled_at, status, scanned,"
        " dropped, candidates, selected, settings_version_id, started_at, finished_at, error)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (run_id, "demo-2026-09-23T20:00+08", "arc", "reference", "2026-09-23T20:00+08",
         RunStatus.SENT.value, universe, max(0, universe - candidates), candidates, selected,
         version_id, now, now),
    )


def _write_tokens(conn, scanned, now) -> None:
    for raw in scanned:
        conn.execute(
            "INSERT OR REPLACE INTO tokens (ca, chain_id, symbol, decimals, launchpad, created_at,"
            " first_seen_run, updated_at) VALUES (?, ?, ?, NULL, ?, NULL, ?, ?)",
            (raw["address"].lower(), "arc", raw.get("sym"), raw.get("launchpad"),
             "demo-20260923-2000", now),
        )


def _write_items(conn, run_id, ranked, scanned) -> None:
    selected_cas = {t["address"].lower() for t in ranked}
    for index, raw in enumerate(ranked, start=1):
        conn.execute(
            "INSERT OR REPLACE INTO run_items (run_id, ca, decision, drop_reason, score, rank,"
            " breakdown_json) VALUES (?, ?, 'selected', NULL, ?, ?, ?)",
            (run_id, raw["address"].lower(), round((raw.get("final") or 0) * 100, 4), index,
             json.dumps({
                 "onchain": round((raw.get("q") or 0) * 100, 4),
                 "product": round((raw.get("fit") or 0) * 100, 4),
                 "total": round((raw.get("final") or 0) * 100, 4),
                 "factors": [],
                 "pending_review": not raw.get("in_registry"),
                 "notes": [],
             }, ensure_ascii=False)),
        )
    for raw in sorted(scanned, key=lambda t: -(t.get("final") or 0)):
        ca = raw["address"].lower()
        if ca in selected_cas:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO run_items (run_id, ca, decision, drop_reason, score, rank,"
            " breakdown_json) VALUES (?, ?, 'candidate', NULL, ?, NULL, ?)",
            (run_id, ca,
             None if raw.get("final") is None else round(raw["final"] * 100, 4),
             json.dumps({
                 "onchain": round((raw.get("q") or 0) * 100, 4) if raw.get("q") is not None else None,
                 "product": round((raw.get("fit") or 0) * 100, 4) if raw.get("fit") is not None else None,
                 "total": None if raw.get("final") is None else round(raw["final"] * 100, 4),
                 "factors": [],
                 "pending_review": not raw.get("in_registry"),
                 "notes": [],
             }, ensure_ascii=False)),
        )


def _write_snapshots(conn, run_id, scanned, now) -> None:
    for raw in scanned:
        conn.execute(
            "INSERT OR REPLACE INTO snapshots (run_id, ca, metrics_json, safety_json, social_json,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, raw["address"].lower(),
             json.dumps(metrics_from(raw), ensure_ascii=False),
             json.dumps(safety_from(raw), ensure_ascii=False),
             json.dumps({"socials": _socials((raw.get("_e") or {}).get("ds") or {}),
                         "websites": list(raw.get("ds_web") or [])}, ensure_ascii=False),
             now),
        )


def _write_summary(conn, run_id, run_dir, data, sent, now) -> None:
    _, batch = parse_sections(read(run_dir / "msgs" / "msg6.txt"))
    changes = []
    for line in (batch.get("本期变化") or "").splitlines():
        if "：" not in line:
            continue
        kind, detail = line.split("：", 1)
        kind, detail = kind.strip(), detail.strip()
        sym = re.match(r"^([^（]+)", detail)
        changes.append({
            "kind": kind,
            "direction": "in" if kind.startswith("新进") else "out",
            "sym": (sym.group(1).strip() if sym else detail),
            "detail": detail,
        })
    caliber = []
    for line in (batch.get("口径") or "").splitlines():
        if line.strip():
            caliber.extend(split_pipes(line.strip()))
    sources = []
    for key, label in (("gmgn", "gmgn"), ("rd", "radardex"), ("gt", "geckoterminal")):
        count = (data.get("sources") or {}).get(key)
        if count is not None:
            sources.append({"name": label, "count": int(count)})
    ok = sum(1 for v in sent.values() if v.get("ok"))
    payload = {
        "batch_label": "2026-09-23 20:00 +08",
        "rule_counts": [
            {"key": key, "label": label, "count": count} for key, label, count in RECORDED_RULE_COUNTS
        ],
        "changes": changes,
        "caliber": caliber,
        "sources": sources,
        "push": {"status": "已送达 %d/%d" % (ok, len(sent)), "delay": "扫描 +6 分钟"},
        "scoring": {
            "onchain_weight": (config.chain("arc").get("pipeline") or {}).get("scoring", {}).get(
                "onchain_weight", 55),
            "product_weight": (config.chain("arc").get("pipeline") or {}).get("scoring", {}).get(
                "product_weight", 45),
        },
        "note": "本轮的过滤、评分与文案由上一代管线产出，采集与评分在本仓库尚未实现。",
    }
    conn.execute(
        "INSERT OR REPLACE INTO run_summary (run_id, payload_json, created_at) VALUES (?, ?, ?)",
        (run_id, json.dumps(payload, ensure_ascii=False), now),
    )


def _write_reports(conn, run_id, ranked, run_dir, top, notes, now) -> None:
    for index, raw in enumerate(ranked, start=1):
        ca = raw["address"].lower()
        text = read(run_dir / "msgs" / ("msg%d.txt" % index))
        head, sections = parse_sections(text)
        meta = HEAD.match(head)
        tag = meta.group(2).strip() if meta else ""
        tail = meta.group(3).strip() if meta else ""
        fit = re.search(r"叙事贴合\s*([\d.]+)\s*/\s*5", tail)
        kv = as_kv(sections.get("数据", ""))
        payload = {
            "tag": tag,
            "fit_label": ("叙事贴合 %s/5" % fit.group(1)) if fit else "",
            # The 链上质量 rows exactly as the round wrote them. The card shows these
            # rather than re-deriving them, so the report reads as it was sent.
            "rows": {key: kv[key] for key in METRIC_KEYS if kv.get(key)},
            "safety": safety_display(sections.get("安全", "")),
            "verdict": LLM_MARK.sub("", sections.get("结论", "")).strip(),
            "note": notes.get(ca, ""),
            "deltas": delta_pairs(sections.get("数据", "")),
            "sections": {
                title: sections[title]
                for title in ("产品完成度", "机制与 dev", "社媒", "优势", "风险")
                if sections.get(title)
            },
        }
        conn.execute(
            "INSERT OR REPLACE INTO reports (run_id, ca, rank, verdict, status, payload_json,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, ca, index, payload["verdict"], "sent",
             json.dumps(payload, ensure_ascii=False), now),
        )
    del top


def _write_lexicon(conn, registry: dict, top: dict, updated: str) -> None:
    for ca, entry in registry.items():
        conn.execute(
            "INSERT OR REPLACE INTO lexicon (ca, chain_id, symbol, tag, narrative, product,"
            " social, concl, narr_text, prod_text, mech_text, pros, cons, watch, scenario,"
            " review_state, evidence_json, updated_at)"
            " VALUES (?, 'arc', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                ca,
                entry.get("sym"),
                entry.get("tag"),
                entry.get("narrative"),
                entry.get("product"),
                entry.get("social"),
                entry.get("concl"),
                entry.get("narr_text"),
                entry.get("prod_text"),
                entry.get("mech_text"),
                entry.get("pros"),
                entry.get("cons"),
                entry.get("watch"),
                entry.get("scenario"),
                "confirmed" if ca in top else "pending",
                updated,
            ),
        )


def main() -> None:
    run_dir = Path(os.environ.get("CH_DEMO_RUN", DEFAULT_RUN))
    registry_path = Path(os.environ.get("CH_DEMO_REGISTRY", DEFAULT_REGISTRY))
    if not (run_dir / "data.json").exists():
        raise SystemExit("reference run not found: %s" % run_dir)
    summary = write_round(run_dir, registry_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
