"""Schema upgrades that cannot be expressed as ALTER TABLE ADD COLUMN."""

from __future__ import annotations

import sqlite3

from app import config
from app.store import db


def test_a_ca_only_tokens_table_is_rebuilt_with_chain_identity(
        tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    try:
        legacy.executescript("""
            PRAGMA user_version = 28;
            CREATE TABLE tokens (
              ca TEXT PRIMARY KEY,
              chain_id TEXT NOT NULL,
              symbol TEXT,
              decimals INTEGER,
              launchpad TEXT,
              created_at TEXT,
              first_seen_run TEXT,
              peak_market_cap_usd REAL,
              peak_market_cap_at TEXT,
              updated_at TEXT NOT NULL
            );
            INSERT INTO tokens
              (ca, chain_id, symbol, peak_market_cap_usd, updated_at)
            VALUES
              ('0xabc', 'arc', 'LEGACY', 12345, '2026-09-01T00:00:00+08:00');
        """)
        legacy.commit()
    finally:
        legacy.close()

    monkeypatch.setattr(config, "DB_PATH", path)
    db.init_db()

    conn = db.connect()
    try:
        keys = {
            row["name"]: row["pk"]
            for row in conn.execute("PRAGMA table_info(tokens)")
            if row["pk"]
        }
        row = conn.execute(
            "SELECT chain_id, symbol, peak_market_cap_usd FROM tokens WHERE ca = '0xabc'"
        ).fetchone()
    finally:
        conn.close()

    assert keys == {"chain_id": 1, "ca": 2}
    assert tuple(row) == ("arc", "LEGACY", 12345)
