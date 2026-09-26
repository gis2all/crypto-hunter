"""Step-0 源可达性探测：拉真实响应，把字段、限频、是否要 key 回填到 api/app/sources/SOURCES.md。

用法：
    cd api && uv run python scripts/probe_sources.py            # 探全部
    cd api && uv run python scripts/probe_sources.py gmgn rd    # 只探指定源

产物：data/probe/probe_<ts>.json（摘要） + data/probe/samples/<name>.json（原始响应头部分）
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

API_ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = API_ROOT.parent / "data" / "probe"
SAMPLES = OUT_ROOT / "samples"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120", "Accept": "*/*"}
UAG = dict(UA); UAG["Referer"] = "https://gmgn.ai/"
UAJ = dict(UA); UAJ["Accept"] = "application/json"
BS = dict(UA); BS["Accept"] = "application/json, */*"; BS["Referer"] = "https://explorer.arc.io/"

RPC = "https://rpc.arc-scan.org"


def sample_ca() -> str:
    """一个真实 CA：优先环境变量，其次上一代 registry。"""
    env = os.environ.get("CH_PROBE_CA")
    if env:
        return env.lower()
    for path in (Path(r"D:\Code\arc\push\registry.json"),):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            for ca in data:
                if isinstance(ca, str) and ca.startswith("0x") and len(ca) == 42:
                    return ca.lower()
        except Exception:
            pass
    raise SystemExit("no sample CA: set CH_PROBE_CA")


def shape(value, depth: int = 0) -> str:
    if isinstance(value, dict):
        keys = list(value)[:14]
        return "{%s}" % ", ".join(str(k) for k in keys)
    if isinstance(value, list):
        return "[%d x %s]" % (len(value), shape(value[0], depth + 1) if value else "?")
    return type(value).__name__


def rpc_call(method: str, params: list):
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                           headers={"Content-Type": "application/json", "User-Agent": UA["User-Agent"]})
    return resp


def probes(ca: str) -> list[dict]:
    pool_hint = "0x" + "0" * 40
    return [
        {"name": "geckoterminal.pools", "url": "https://api.geckoterminal.com/api/v2/networks/arc/pools?page=1", "headers": UAJ},
        {"name": "geckoterminal.new_pools", "url": "https://api.geckoterminal.com/api/v2/networks/arc/new_pools?page=1", "headers": UAJ},
        {"name": "geckoterminal.trending", "url": "https://api.geckoterminal.com/api/v2/networks/arc/trending_pools?page=1", "headers": UAJ},
        {"name": "geckoterminal.token_pools", "url": "https://api.geckoterminal.com/api/v2/networks/arc/tokens/%s/pools?page=1" % ca, "headers": UAJ},
        {"name": "geckoterminal.ohlcv", "url": "https://api.geckoterminal.com/api/v2/networks/arc/pools/%s/ohlcv/day?limit=10&currency=usd" % pool_hint, "headers": UAJ, "optional": True},
        {"name": "geckoterminal.tokens", "url": "https://api.geckoterminal.com/api/v2/networks/arc/tokens?page=1", "headers": UAJ},
        {"name": "radardex.tokens", "url": "https://api.radardex.pro/tokens?limit=2000&sort=mcap&dir=desc&window=24h", "headers": UA},
        {"name": "gmgn.rank", "url": "https://gmgn.ai/defi/quotation/v1/rank/arc/swaps/24h?orderby=swaps&direction=desc&limit=100&page=1", "headers": UAG},
        {"name": "gmgn.token_stat", "url": "https://gmgn.ai/api/v1/token_stat/arc/%s" % ca, "headers": UAG},
        {"name": "gmgn.token_dev_info", "url": "https://gmgn.ai/api/v1/token_dev_info/arc/%s" % ca, "headers": UAG},
        {"name": "dexscreener.token", "url": "https://api.dexscreener.com/latest/dex/tokens/%s" % ca, "headers": UA},
        {"name": "goplus.security", "url": "https://api.gopluslabs.io/api/v1/token_security/5042?contract_addresses=%s" % ca, "headers": UAJ},
        {"name": "sourcify.contract", "url": "https://sourcify.dev/server/v2/contract/5042/%s?fields=compilation" % ca, "headers": UAJ},
        {"name": "blockscout.address", "url": "https://explorer.arc.io/api/v2/addresses/%s" % ca, "headers": BS},
        {"name": "blockscout.holders", "url": "https://explorer.arc.io/api/v2/tokens/%s/holders" % ca, "headers": BS},
    ]


def one(client: httpx.Client, spec: dict) -> dict:
    started = time.time()
    row = {"name": spec["name"], "url": spec["url"]}
    try:
        resp = client.get(spec["url"], headers=spec["headers"])
        row["status"] = resp.status_code
        row["ms"] = int((time.time() - started) * 1000)
        row["bytes"] = len(resp.content)
        row["ctype"] = resp.headers.get("content-type", "")
        for key in ("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after", "server"):
            if resp.headers.get(key):
                row[key] = resp.headers[key]
        try:
            payload = resp.json()
            row["shape"] = shape(payload)
            row["sample"] = payload
        except Exception:
            row["body_head"] = resp.text[:200]
    except Exception as exc:
        row["error"] = repr(exc)[:160]
        row["ms"] = int((time.time() - started) * 1000)
    return row


def main() -> None:
    wanted = [a.lower() for a in sys.argv[1:]]
    ca = sample_ca()
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    SAMPLES.mkdir(parents=True, exist_ok=True)
    print("probe CA: %s" % ca)
    print("%-28s %-6s %6s %9s  %s" % ("source", "status", "ms", "bytes", "shape"))
    rows = []
    with httpx.Client(timeout=35.0, follow_redirects=True) as client:
        for spec in probes(ca):
            if wanted and not any(spec["name"].startswith(w) for w in wanted):
                continue
            row = one(client, spec)
            rows.append(row)
            print("%-28s %-6s %6s %9s  %s" % (
                row["name"], row.get("status") or "ERR", row.get("ms"), row.get("bytes") or 0,
                row.get("shape") or (row.get("error") or row.get("body_head", ""))[:90]))
            if "sample" in row:
                with io_open(SAMPLES / (row["name"] + ".json")) as fh:
                    json.dump(row.pop("sample"), fh, ensure_ascii=False, indent=1)
            time.sleep(1.0)
        rpc = rpc_call("eth_blockNumber", [])
        rows.append({"name": "rpc.arc-scan", "status": rpc.status_code, "ms": None,
                     "shape": rpc.text[:80]})
        print("%-28s %-6s %6s %9s  %s" % ("rpc.arc-scan", rpc.status_code, "-", len(rpc.content), rpc.text[:60]))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUT_ROOT / ("probe_%s.json" % stamp)
    with io_open(path) as fh:
        json.dump({"ca": ca, "at": stamp, "rows": rows}, fh, ensure_ascii=False, indent=1)
    print("summary -> %s" % path)


def io_open(path):
    return open(path, "w", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
