"""When an entry may be reused, when it has to be written again, and what happens next.

The entry is 45% of the score *and* the words on the card, so these rules are the difference
between a card that ages and a card that lies. Two of them are worth stating twice: a refresh
condition outranks the "written recently" window, and a human-confirmed entry is never
overwritten by either path.
"""

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.core import lexicon
from app.core.models import SafetyFacts, TokenMetrics
from app.pipeline.decide import Engine
from app.report import draft, facts, research
from app.store import db

CA = "0x" + "a" * 40
CA2 = "0x" + "b" * 40
CA3 = "0x" + "c" * 40
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _entry(**over) -> dict:
    base = {
        "ca": CA,
        "updated_at": (NOW - timedelta(hours=1)).isoformat(),
        "baseline_mcap": 100_000.0,
        "baseline_holders": 500,
        "review_state": "pending",
        "tag": "meme",
        "narrative": 3.0,
        "product": 3.0,
        "concl": "x" * 200,
    }
    base.update(over)
    return base


def _metrics(**over) -> dict:
    base = {"market_cap_usd": 100_000.0, "holders": 500}
    base.update(over)
    return base


def _due(entry, metrics=None, cfg=None, safety=None):
    return lexicon.due(entry, metrics if metrics is not None else _metrics(), cfg, NOW,
                       safety=safety)


# ------------------------------------------------------------------ the four ways out


def test_a_token_with_no_entry_is_created():
    verdict = _due(None)
    assert verdict.action == "create"
    assert verdict.reason == "新建档"


def test_a_fresh_untouched_entry_is_reused():
    verdict = _due(_entry())
    assert verdict.action == "reuse"
    assert verdict.age_hours == pytest.approx(1.0)


def test_a_market_cap_move_rewrites_at_the_threshold_and_not_before():
    assert _due(_entry(), _metrics(market_cap_usd=140_000)).action == "rewrite"
    assert _due(_entry(), _metrics(market_cap_usd=60_000)).action == "rewrite", "down too"
    assert _due(_entry(), _metrics(market_cap_usd=139_000)).action == "reuse"


def test_a_holder_gain_rewrites_but_a_drop_does_not():
    assert _due(_entry(), _metrics(holders=700)).action == "rewrite"
    assert _due(_entry(), _metrics(holders=300)).action == "reuse", "lost holders: not news"


def test_an_entry_past_its_expiry_is_rewritten():
    old = _entry(updated_at=(NOW - timedelta(days=3)).isoformat())
    verdict = _due(old)
    assert verdict.action == "rewrite"
    assert verdict.expired is True


def test_past_the_skip_window_but_not_expired_is_still_rewritten():
    # Default window is 6h, expiry 2 days: 20 hours old is neither fresh nor expired, and
    # the entry still gets read again because the numbers beside it moved on.
    stale = _entry(updated_at=(NOW - timedelta(hours=20)).isoformat())
    verdict = _due(stale)
    assert verdict.action == "rewrite"
    assert verdict.expired is False


def test_a_refresh_condition_outranks_the_skip_window():
    """A token that doubled in the last hour is the one worth re-reading. The cheap window
    must not be able to skip past it."""
    fresh = _entry(updated_at=(NOW - timedelta(hours=1)).isoformat())
    verdict = _due(fresh, _metrics(market_cap_usd=200_000), {"rewrite_skip_hours": 6})
    assert verdict.action == "rewrite"
    assert "市值较建档" in verdict.reason


def test_an_entry_with_no_timestamp_is_rewritten_rather_than_trusted():
    verdict = _due(_entry(updated_at=""))
    assert verdict.action == "rewrite"
    assert verdict.age_hours is None


def test_an_entry_with_no_baseline_does_not_invent_a_move():
    bare = _entry(baseline_mcap=None, baseline_holders=None)
    assert _due(bare, _metrics(market_cap_usd=900_000)).action == "reuse"


def test_a_zero_baseline_is_not_a_division():
    zero = _entry(baseline_mcap=0)
    assert _due(zero, _metrics(market_cap_usd=500_000)).action == "reuse"


def test_the_settings_are_read_with_defaults_for_everything_missing():
    assert lexicon.settings(None) == lexicon.DEFAULTS
    assert lexicon.settings({"expire_days": "3"})["expire_days"] == 3.0
    assert lexicon.settings({"expire_days": "not a number"})["expire_days"] == 2.0
    assert lexicon.settings({"expire_days": -1})["expire_days"] == 2.0, "a negative day is no rule"


def test_the_baseline_is_whatever_the_entry_was_written_against():
    base = lexicon.mark_written(None, _metrics(market_cap_usd=12.5, holders=7), "2026-09-25T00:00:00")
    assert base == {"baseline_mcap": 12.5, "baseline_holders": 7,
                    "baseline_at": "2026-09-25T00:00:00", "baseline_safety": None}


def test_the_safety_baseline_is_the_verdict_the_words_were_written_against():
    facts = SafetyFacts(source_verified=True, dangerous_perms=[], lp_locked=False)
    base = lexicon.mark_written(None, _metrics(), "2026-09-25T00:00:00", safety=facts)
    assert base["baseline_safety"] == "源码 已验证 ｜ 权限 无危险权限 ｜ LP 未锁"
    assert lexicon.mark_written(None, TokenMetrics(ca=CA, holders=9), None)["baseline_mcap"] is None


#: What a card printed while the LP question was still open.
OPEN_VERDICT = "源码 未判定 ｜ 权限 未判定 ｜ LP 未判定"


def test_a_verdict_that_lands_rewrites_the_entry():
    """The entry said "LP 未判定" because that was the answer when it was written. When the
    answer arrives the sentence is wrong, and no price move is needed to make it so."""
    entry = _entry(baseline_safety=OPEN_VERDICT)
    facts = SafetyFacts(source_verified=True, dangerous_perms=[], lp_locked=True)
    verdict = _due(entry, safety=facts)
    assert verdict.action == "rewrite"
    assert verdict.reason == ("安全判定变化（源码 未判定→已验证、权限 未判定→无危险权限、"
                              "LP 未判定→已锁）")


def test_only_the_verdicts_that_moved_are_named():
    entry = _entry(baseline_safety="源码 已验证 ｜ 权限 无危险权限 ｜ LP 未判定")
    facts = SafetyFacts(source_verified=True, dangerous_perms=[], lp_locked=True)
    assert _due(entry, safety=facts).reason == "安全判定变化（LP 未判定→已锁）"


def test_a_verdict_that_did_not_move_is_not_a_reason():
    facts = SafetyFacts(source_verified=True, dangerous_perms=[], lp_locked=True)
    entry = _entry(baseline_safety=lexicon.safety_signature(facts))
    assert _due(entry, safety=facts).action == "reuse"


def test_a_round_that_could_not_answer_the_safety_question_does_not_rewrite():
    """A missing reading is not a change. Rewriting every entry on a source outage would be
    the expensive way to lose the evidence the card already carries."""
    entry = _entry(baseline_safety="源码 已验证 ｜ 权限 无危险权限 ｜ LP 已锁")
    assert _due(entry, safety=SafetyFacts()).action == "reuse"
    assert _due(entry, safety=None).action == "reuse"


def test_an_entry_with_no_stored_verdict_is_not_rewritten_by_this_rule():
    """Rows written before the column existed say nothing about what they were written
    against, so there is nothing to compare - and firing on them would rewrite the whole
    table in one round. `db.init_db` stamps them from their own snapshot instead."""
    facts = SafetyFacts(source_verified=True, dangerous_perms=[], lp_locked=True)
    assert _due(_entry(), safety=facts).action == "reuse"


def test_a_reading_that_answered_nothing_is_still_a_reading():
    """Two different blanks, and the difference is the whole rule: an empty block from a
    round that asked and got nothing is a value worth storing; no block at all is not."""
    assert lexicon.safety_signature(SafetyFacts()) == lexicon.UNDETERMINED
    assert lexicon.safety_signature(None) == ""
    assert lexicon.safety_signature({}) == lexicon.UNDETERMINED


def test_an_entry_written_while_no_source_answered_is_due_when_one_does():
    """The headline case: the card says 「LP 未判定」 because that was all anyone knew, and
    the round that finally locks the LP makes that sentence wrong."""
    entry = _entry(baseline_safety=lexicon.UNDETERMINED)
    facts = SafetyFacts(source_verified=True, dangerous_perms=[], lp_locked=True)
    assert _due(entry, safety=facts).action == "rewrite"


def test_a_verdict_that_went_quiet_does_not_rewrite():
    """The mirror image, and it must not fire: a reading that stops answering is a source
    going down, not a token changing, and rewriting on it would lose the evidence."""
    entry = _entry(baseline_safety="源码 已验证 ｜ 权限 无危险权限 ｜ LP 已锁")
    assert _due(entry, safety=SafetyFacts()).action == "reuse"


def test_the_verdict_words_are_the_ones_the_card_prints():
    """`safety_signature` decides a rewrite; `report.facts` writes the line the writer quotes
    and the card draws. Two wordings for one verdict would mean a card contradicting the
    reason its own row recorded."""
    for locked in (True, False, None):
        verdicts = SafetyFacts(source_verified=True, dangerous_perms=[], lp_locked=locked)
        line = facts.safety_line(TokenMetrics(ca=CA), verdicts)
        for word in lexicon.safety_signature(verdicts).split(" ｜ "):
            assert word in line, "%r is not in %r" % (word, line)


def test_a_database_without_the_column_gets_it_filled_from_its_own_snapshot(isolated_db):
    """The migration reads the verdict back off the snapshot nearest each entry's own
    timestamp: the reading the writer had in hand, not today's. A later snapshot must not
    count - that is exactly the change the entry is being compared against."""
    for run_id in ("old", "new", "blank"):
        _run(run_id)
    conn = db.connect()
    try:
        conn.execute("ALTER TABLE lexicon DROP COLUMN baseline_safety")
        for ca in (CA, CA2):
            conn.execute(
                "INSERT INTO lexicon (ca, chain_id, symbol, updated_at) VALUES (?, 'arc', 'T', ?)",
                (ca, "2026-09-20T00:00:00+00:00"))
        for run_id, ca, at, facts in (
                ("old", CA, "2026-09-19T23:00:00+00:00", {"source_verified": True}),
                ("new", CA, "2026-09-20T01:00:00+00:00", {"lp_locked": True}),
                ("blank", CA2, "2026-09-19T23:00:00+00:00", {})):
            conn.execute(
                "INSERT INTO snapshots (run_id, ca, metrics_json, safety_json, social_json,"
                " created_at) VALUES (?, ?, '{}', ?, '{}', ?)",
                (run_id, ca, json.dumps(facts), at))
        db.init_db(conn)
        rows = {row["ca"]: row["baseline_safety"]
                for row in conn.execute("SELECT ca, baseline_safety FROM lexicon").fetchall()}
    finally:
        conn.close()
    assert rows[CA] == "源码 已验证 ｜ 权限 未判定 ｜ LP 未判定", "the snapshot from its own era"
    assert rows[CA2] == lexicon.UNDETERMINED, "a blank reading is recorded, not skipped"


def test_the_deep_pass_sheet_carries_the_safety_verdict(isolated_db):
    """The bug behind a card that read 「安全块三项全部未判定」 beside a locked LP: this pass
    pulled the snapshot's numbers and never its verdict, so the widest sheet in the pipeline
    was the one that could not see it."""
    _run("run1")
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO snapshots (run_id, ca, metrics_json, safety_json, social_json,"
            " created_at) VALUES ('run1', ?, ?, ?, '{}', ?)",
            (CA, json.dumps({"ca": CA, "symbol": "T"}),
             json.dumps({"source_verified": True, "dangerous_perms": [], "lp_locked": True}),
             NOW.isoformat()),
        )
    finally:
        conn.close()

    metrics, safety = research._latest_metrics(CA)

    assert safety["lp_locked"] is True
    assert "LP 已锁" in research._facts_line(CA, "T", metrics, safety)


def test_the_age_reads_in_the_unit_a_reader_can_use():
    assert lexicon.age_text(None) == "时间未知"
    assert lexicon.age_text(0.4) == "不到 1 小时前"
    assert lexicon.age_text(30) == "30 小时前"
    assert lexicon.age_text(60) == "2.5 天前"

# ------------------------------------------------------------------ the writing stage


def _payload(**over) -> dict:
    base = {"tag": "meme", "narrative": 2, "product": 2, "concl": "x" * 200,
            "narr_text": "a", "prod_text": "b", "mech_text": "c", "pros": "d", "cons": "e",
            "social": ""}
    base.update(over)
    return base


def _row(ca: str) -> dict:
    conn = db.connect()
    try:
        return dict(conn.execute("SELECT * FROM lexicon WHERE ca = ?", (ca,)).fetchone())
    finally:
        conn.close()


def _confirm(ca: str) -> None:
    conn = db.connect()
    try:
        conn.execute("UPDATE lexicon SET review_state = 'confirmed' WHERE ca = ?", (ca,))
    finally:
        conn.close()


def test_a_write_stamps_the_baseline_a_rewrite_is_measured_against(isolated_db):
    draft.store_entries("arc", {CA: _payload()}, metrics={CA: _metrics()},
                        observed_at=NOW.isoformat())
    row = _row(CA)
    assert row["baseline_mcap"] == 100_000
    assert row["baseline_holders"] == 500
    assert row["baseline_at"] == NOW.isoformat()
    assert row["research_state"] == "none", "a new entry has not been researched yet"

    draft.store_entries("arc", {CA: _payload()}, metrics={CA: _metrics(market_cap_usd=180_000)},
                        reasons={CA: "市值较建档 +80%"},
                        observed_at=(NOW + timedelta(days=1)).isoformat())
    row = _row(CA)
    assert row["baseline_mcap"] == 180_000, "the measuring stick moves with the rewrite"
    assert row["refresh_reason"] == "市值较建档 +80%"


def test_a_confirmed_entry_is_never_overwritten(isolated_db):
    draft.store_entries("arc", {CA: _payload()}, metrics={CA: _metrics()})
    _confirm(CA)

    written = draft.store_entries("arc", {CA: _payload(tag="机器写的")}, metrics={CA: _metrics()})

    assert written == set(), "the caller is told, so it can say why on the card"
    assert _row(CA)["tag"] == "meme", "the human words survive"


def test_a_refresh_that_was_refused_is_reported_on_the_round(isolated_db, monkeypatch):
    draft.store_entries("arc", {CA: _payload()}, metrics={CA: _metrics()})
    _confirm(CA)
    monkeypatch.setattr(draft.llm, "configured", lambda: True)
    monkeypatch.setattr(draft.llm, "complete_json",
                        lambda messages, **kw: {"entries": {CA: _payload()}})
    notes: list[str] = []

    out = draft.refresh_lexicon(
        {"id": "arc"},
        [{"ca": CA, "symbol": "T", "metrics": TokenMetrics(ca=CA), "facts": SafetyFacts(),
          "reason": "档案已 3.0 天未重写"}],
        "run1", notes=notes,
    )

    assert out == {}
    assert notes and "人工已确认" in notes[0]


# ---------------------------------------------------------------------- the deep pass


class _Result:
    def __init__(self, cas):
        self.picked = [type("Pick", (), {"ca": ca})() for ca in cas]


def _run(run_id: str, status: str = "sent") -> None:
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO runs (id, run_key, chain_id, trigger, status, scanned,"
            " dropped, candidates, selected) VALUES (?, ?, 'arc', 'manual', ?, 0, 0, 0, 0)",
            (run_id, run_id, status),
        )
    finally:
        conn.close()


def _snapshot(ca: str, socials: dict | None = None, websites: list | None = None,
              run_id: str = "run1") -> None:
    """The newest facts on file for a token, which is where the deep pass reads its links."""
    _run(run_id)
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO snapshots (run_id, ca, metrics_json, safety_json, social_json,"
            " created_at) VALUES (?, ?, ?, '{}', ?, ?)",
            (run_id, ca,
             json.dumps({"ca": ca, "symbol": "T", "market_cap_usd": 100_000.0}),
             json.dumps({"socials": socials or {}, "websites": websites or []}),
             NOW.isoformat()),
        )
    finally:
        conn.close()


def _queued(ca: str = CA) -> None:
    conn = db.connect()
    try:
        conn.execute("UPDATE lexicon SET research_state = 'queued' WHERE ca = ?", (ca,))
    finally:
        conn.close()


class _Published:
    """What the round hands `report_payload` at publish: a snapshot, and the badge a reused
    entry earns."""

    def __init__(self, metrics: dict, entry_note: str = ""):
        self.metrics = metrics
        self.entry_note = entry_note


def _publish(ca: str = CA, run_id: str = "run1", entry_note: str = "",
             at: str | None = None) -> None:
    """The report row as the publish leaves it: the entry's words, copied out of the lexicon."""
    payload = draft.report_payload(
        _Published({"socials": {"twitter": "https://x.com/t"},
                    "websites": ["https://t.xyz"]}, entry_note),
        _row(ca), "本期点评", [{"key": "市值", "value": "+0%"}],
    )
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO reports (run_id, ca, rank, verdict, status, payload_json,"
            " created_at) VALUES (?, ?, 1, ?, 'draft', ?, ?)",
            (run_id, ca, payload["verdict"], json.dumps(payload, ensure_ascii=False),
             at or NOW.isoformat()),
        )
    finally:
        conn.close()


def _card(ca: str = CA, run_id: str = "run1") -> dict:
    conn = db.connect()
    try:
        row = dict(conn.execute("SELECT * FROM reports WHERE run_id = ? AND ca = ?",
                                (run_id, ca)).fetchone())
    finally:
        conn.close()
    row["payload"] = json.loads(row["payload_json"])
    return row


def test_only_the_picks_are_queued(isolated_db):
    draft.store_entries("arc", {CA: _payload(), CA2: _payload()}, metrics={})
    chain = {"id": "arc", "report": {"research_enabled": True}}

    assert research.queue(chain, _Result([CA])) == [CA]

    assert _row(CA)["research_state"] == "queued"
    assert _row(CA2)["research_state"] == "none", "a candidate nobody published is not read"


def test_a_chain_that_turned_research_off_queues_nothing(isolated_db):
    draft.store_entries("arc", {CA: _payload()}, metrics={})
    chain = {"id": "arc", "report": {"research_enabled": False}}

    assert research.queue(chain, _Result([CA])) == []
    assert _row(CA)["research_state"] == "none"


def test_an_entry_already_researched_is_not_read_again(isolated_db):
    draft.store_entries("arc", {CA: _payload()}, metrics={})
    conn = db.connect()
    conn.execute("UPDATE lexicon SET research_state = 'done' WHERE ca = ?", (CA,))
    conn.close()

    research.queue({"id": "arc", "report": {}}, _Result([CA]))

    assert _row(CA)["research_state"] == "done"


def test_a_claim_that_died_with_its_process_is_put_back_in_the_line(isolated_db):
    draft.store_entries("arc", {CA: _payload()}, metrics={})
    conn = db.connect()
    conn.execute("UPDATE lexicon SET research_state = 'running' WHERE ca = ?", (CA,))
    conn.close()

    assert research.requeue_running() == 1
    assert _row(CA)["research_state"] == "queued"


def _model_answering(monkeypatch, seen: dict, payload: dict | None = None):
    def fake(messages, **kw):
        seen["purpose"] = kw.get("purpose")
        seen["prompt"] = messages[0]["content"]
        seen["pages"] = messages[1]["content"]
        return payload or {"entries": {CA: _payload(tag="读过官网")}}

    monkeypatch.setattr(research.llm, "complete_json", fake)


def _page(monkeypatch, body: str = "real copy " * 30):
    monkeypatch.setattr(research.http, "get",
                        lambda url, headers=None, **kw: "<html><body>%s</body></html>" % body)


def test_the_deep_pass_reads_the_pages_and_rewrites_the_entry(isolated_db, monkeypatch):
    draft.store_entries("arc", {CA: _payload()}, metrics={CA: _metrics()})
    _snapshot(CA, socials={"twitter": "https://x.com/t"}, websites=["https://t.xyz"])
    _queued()
    seen: dict = {}
    _page(monkeypatch)
    _model_answering(monkeypatch, seen)

    out = research.work_once(limit=1)

    assert out == [{"ca": CA, "chain_id": "arc", "state": "done", "pages": 2,
                    "text": "rewritten"}]
    assert seen["purpose"] == "research"
    assert "https://t.xyz" in seen["pages"], "the model is handed what was read"
    row = _row(CA)
    assert row["tag"] == "读过官网"
    assert row["research_state"] == "done"
    assert row["research_note"] is None
    assert row["baseline_mcap"] == 100_000, "a research rewrite restamps the baseline"
    assert "重档调研" in row["refresh_reason"]
    urls = sorted(source["url"] for source in research.sources_for(CA))
    assert urls == ["https://t.xyz", "https://x.com/t"]


def test_a_token_with_no_readable_page_fails_and_says_so(isolated_db, monkeypatch):
    draft.store_entries("arc", {CA: _payload()}, metrics={CA: _metrics()})
    _queued()

    out = research.work_once(limit=1)

    assert out[0]["state"] == "failed"
    row = _row(CA)
    assert row["research_state"] == "failed"
    assert "外链" in row["research_note"]
    assert row["tag"] == "meme", "a failed pass leaves the entry it found"


def test_a_page_that_answers_with_nothing_is_not_evidence(isolated_db, monkeypatch):
    draft.store_entries("arc", {CA: _payload()}, metrics={CA: _metrics()})
    _snapshot(CA, socials={"twitter": "https://x.com/t"})
    _queued()
    _page(monkeypatch, body="")

    assert research.work_once(limit=1)[0]["state"] == "failed"
    assert _row(CA)["tag"] == "meme"


def test_research_files_the_evidence_but_leaves_confirmed_words_alone(isolated_db, monkeypatch):
    draft.store_entries("arc", {CA: _payload()}, metrics={CA: _metrics()})
    _confirm(CA)
    _snapshot(CA, websites=["https://t.xyz"])
    _queued()
    seen: dict = {}
    _page(monkeypatch)
    _model_answering(monkeypatch, seen)

    out = research.work_once(limit=1)

    assert out[0]["text"] == "kept"
    row = _row(CA)
    assert row["tag"] == "meme"
    assert row["research_state"] == "done"
    assert "人工已确认" in row["research_note"]
    assert research.sources_for(CA), "the pages are worth keeping either way"

def test_the_researched_words_go_back_onto_the_card_that_was_published(isolated_db, monkeypatch):
    """The card is drawn from the draft and read a minute later, so the reading has to travel
    back: otherwise the only thing the product says about a token is its numbers."""
    draft.store_entries("arc", {CA: _payload()}, metrics={CA: _metrics()})
    _snapshot(CA, socials={"twitter": "https://x.com/t"}, websites=["https://t.xyz"])
    _publish(entry_note="解读写于 3.0 天前 · 本轮数据已更新")
    _queued()
    seen: dict = {}
    _page(monkeypatch)
    story = "c0 是一条面向碳信用的测试链，配套区块浏览器 c0scan，官方在 X 上更新进度。" * 5
    _model_answering(monkeypatch, seen, {"entries": {CA: _payload(
        tag="基建", narrative=4, concl=story,
        prod_text="发射台已上线，能铸造和交易", mech_text="无回购，dev 持仓归零",
        pros="唯一有真实产品的标的", cons="深度薄，97% 挤在主池",
        social="X @c0scan 与官网都在更新")}})

    out = research.work_once(limit=1)

    assert out[0]["text"] == "rewritten"
    card = _card()
    assert card["verdict"] == story
    assert card["verdict"] == card["payload"]["verdict"], "the column the page falls back to"
    assert card["payload"]["tag"] == "基建"
    assert card["payload"]["fit_label"] == "叙事贴合 4/5"
    sections = card["payload"]["sections"]
    assert sections["产品完成度"] == "发射台已上线，能铸造和交易"
    assert sections["机制与 dev"] == "无回购，dev 持仓归零"
    assert sections["优势"] == "唯一有真实产品的标的"
    assert sections["社媒"].startswith("X @c0scan 与官网都在更新")
    assert "https://t.xyz" in sections["社媒"], "a link the sentence forgot is still on the card"
    assert card["payload"]["note"] == "本期点评", "点评 belongs to the round, not to the entry"
    assert card["payload"]["deltas"] == [{"key": "市值", "value": "+0%"}]
    assert card["payload"]["entry_note"] == "", "these words are no longer older than the numbers"


def test_a_round_that_already_settled_keeps_the_words_it_published(isolated_db, monkeypatch):
    draft.store_entries("arc", {CA: _payload()}, metrics={CA: _metrics()})
    _run("run1", status="researched")
    _snapshot(CA, websites=["https://t.xyz"])
    _publish()
    _queued()
    _page(monkeypatch)
    _model_answering(monkeypatch, {})

    research.work_once(limit=1)

    assert _card()["payload"]["tag"] == "meme", "a settled round is a record of that hour"


def test_a_pass_that_read_nothing_leaves_the_card_alone(isolated_db, monkeypatch):
    draft.store_entries("arc", {CA: _payload()}, metrics={CA: _metrics()})
    _snapshot(CA, websites=["https://t.xyz"])
    _publish()
    _queued()
    _page(monkeypatch, body="")

    assert research.work_once(limit=1)[0]["state"] == "failed"
    assert _card()["payload"]["verdict"] == "x" * 200


def test_a_confirmed_entry_is_not_carried_onto_the_card(isolated_db, monkeypatch):
    draft.store_entries("arc", {CA: _payload()}, metrics={CA: _metrics()})
    _confirm(CA)
    _snapshot(CA, websites=["https://t.xyz"])
    _publish()
    _queued()
    _page(monkeypatch)
    _model_answering(monkeypatch, {}, {"entries": {CA: _payload(tag="读过官网")}})

    assert research.work_once(limit=1)[0]["text"] == "kept"
    assert _card()["payload"]["tag"] == "meme", "the human words stay on the card too"


def test_an_older_round_that_never_settled_keeps_its_words(isolated_db, monkeypatch):
    """Restating an old round would print today's words - and today's numbers - in a report
    whose table shows that hour's. Only the newest publication of the card is in reach."""
    draft.store_entries("arc", {CA: _payload()}, metrics={CA: _metrics()})
    _snapshot(CA, websites=["https://t.xyz"], run_id="run1")
    _publish(run_id="run1", at="2026-09-25T05:00:00+08:00")
    _snapshot(CA, websites=["https://t.xyz"], run_id="run2")
    _publish(run_id="run2", at="2026-09-25T09:00:00+08:00")
    _queued()
    _page(monkeypatch)
    _model_answering(monkeypatch, {}, {"entries": {CA: _payload(tag="读过官网")}})

    research.work_once(limit=1)

    assert _card(run_id="run2")["payload"]["tag"] == "读过官网", "the newest card is upgraded"
    assert _card(run_id="run1")["payload"]["tag"] == "meme", "the older one keeps its words"


# ------------------------------------------------------------------ the round reads it


def _token(ca: str = CA) -> TokenMetrics:
    return TokenMetrics(ca=ca, symbol="T", market_cap_usd=100_000, liquidity_usd=30_000,
                        volume_24h_usd=9_000, holders=500, top10_pct=10.0, bundling_pct=1.0)


class _Source:
    name = "gmgn"
    chains = ("arc",)

    def fetch_universe(self, chain_id, limit):
        return [_token()]

    def fetch_token(self, chain_id, ca):
        return _token(ca), SafetyFacts()


class _Reader:
    def lock_facts(self, chain, *, ca, pair, creator, launchpad):
        return True, "test basis", {}


CHAIN = {"id": "arc", "label": "Arc", "pipeline": {}, "sources": []}


def _settings(**lexicon_cfg) -> dict:
    return {
        "schedule": {},
        "scoring": {"onchain_weight": 55, "product_weight": 45},
        "report": {"per_round": 1},
        "pipeline": {
            "enrich_limit": 3,
            "lexicon": lexicon_cfg or {},
            "prefilter": {"market_cap_min_usd": 50_000, "market_cap_max_usd": 200_000,
                          "liquidity_min_usd": 8_000, "holders_min": 150},
            "safety": {},
        },
    }


def test_a_stale_entry_is_rewritten_and_a_failed_rewrite_says_so_on_the_card(isolated_db):
    """The rewrite runs before the score - the entry is 45% of the score as well as the
    words - and a rewrite that fails keeps the old entry rather than losing the token."""
    entry = _entry(updated_at=(NOW - timedelta(days=5)).isoformat())
    asked: list[str] = []

    def refresh(targets, tell):
        asked.extend(target["ca"] for target in targets)
        assert targets[0]["reason"], "the reason travels with the target into the row"
        return {}

    engine = Engine(CHAIN, sources=[_Source()], reader=_Reader(),
                    lexicon=lambda ca: entry, refresh=refresh)

    result = engine.run(_settings(expire_days=2), "run-1")

    assert asked == [CA]
    picked = result.picked[0]
    assert picked.entry_note.startswith("解读写于 ")
    assert "天前" in picked.entry_note and "本轮重写未完成" in picked.entry_note
    assert picked.entry["tag"] == "meme", "the old words still score and still print"


def test_a_reused_entry_says_how_old_its_words_are(isolated_db):
    """Reuse is the common case, not the exception: with a six-hour window and a round every
    three, an entry written this morning is read twice. The card still has to say so."""
    # Anchored to the wall clock, not to `NOW`: the engine reads `datetime.now()`, so an
    # entry written half an hour before a fixed timestamp falls out of the six-hour window
    # the moment that timestamp is yesterday - which is how this test started failing.
    entry = _entry(updated_at=(datetime.now().astimezone()
                               - timedelta(minutes=30)).isoformat())
    calls: list[list] = []

    def refresh(targets, tell):
        calls.append([t.get("reason") for t in targets])
        return {}

    engine = Engine(CHAIN, sources=[_Source()], reader=_Reader(),
                    lexicon=lambda ca: entry, refresh=refresh)

    result = engine.run(_settings(), "run-1")

    assert calls == [], "nothing to rewrite, so nothing is asked"
    assert result.picked[0].entry_note.startswith("解读写于 ")
    assert "本轮数据已更新" in result.picked[0].entry_note


def test_an_entry_rewritten_this_round_carries_no_badge(isolated_db):
    """The badge means "older than the numbers". A rewrite makes the words this round, and
    a badge that showed up anyway would train a reader to ignore it."""
    entry = _entry(updated_at=(NOW - timedelta(days=5)).isoformat())

    def refresh(targets, tell):
        return {CA: _payload(tag="重写过的")}

    engine = Engine(CHAIN, sources=[_Source()], reader=_Reader(),
                    lexicon=lambda ca: entry, refresh=refresh)

    result = engine.run(_settings(expire_days=2), "run-1")

    assert result.picked[0].entry_note == ""
    assert result.picked[0].entry["tag"] == "重写过的"
    assert result.picked[0].entry["narrative"] == 2, "the rewritten entry is what scores"


def test_a_confirmed_entry_that_is_stale_says_it_was_left_alone(isolated_db):
    entry = _entry(updated_at=(NOW - timedelta(days=5)).isoformat(), review_state="confirmed")
    engine = Engine(CHAIN, sources=[_Source()], reader=_Reader(), lexicon=lambda ca: entry)

    result = engine.run(_settings(expire_days=2), "run-1")

    assert "人工已确认" in result.picked[0].entry_note

def test_a_round_reaches_researched_only_when_every_card_is_settled(isolated_db):
    """`sent` is where a round was for ever: the deep pass had no state to end in. It does
    now, and the answer is derived from the rows - which survives a restart."""
    from app.pipeline import run as pipeline_run

    draft.store_entries("arc", {CA: _payload(), CA2: _payload()}, metrics={})
    _run("run1")
    conn = db.connect()
    try:
        for index, ca in enumerate((CA, CA2), start=1):
            conn.execute(
                "INSERT INTO reports (run_id, ca, rank, verdict, status, payload_json,"
                " created_at) VALUES ('run1', ?, ?, 'x', 'written', '{}', ?)",
                (ca, index, NOW.isoformat()),
            )
        conn.execute("UPDATE lexicon SET research_state = 'done' WHERE ca = ?", (CA,))
    finally:
        conn.close()

    assert pipeline_run.researched_runs("arc") == [], "one entry is still queued"

    conn = db.connect()
    conn.execute("UPDATE lexicon SET research_state = 'failed' WHERE ca = ?", (CA2,))
    conn.close()

    assert pipeline_run.researched_runs("arc") == ["run1"], "failed counts as settled"
    pipeline_run.mark_researched("run1")
    assert pipeline_run._row("run1")["status"] == "researched"

def test_a_link_an_earlier_round_learned_is_still_a_link(isolated_db):
    """A source that goes quiet this round must not blank a website we have had for days.
    Without this the deep pass reports "no links" on a token whose links are in the table."""
    _run("old1")
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO snapshots (run_id, ca, metrics_json, safety_json, social_json,"
            " created_at) VALUES ('old1', ?, '{}', '{}', ?, ?)",
            (CA, json.dumps({"socials": {"twitter": "https://x.com/t"},
                             "websites": ["https://t.xyz"]}), NOW.isoformat()),
        )
    finally:
        conn.close()
    _snapshot(CA)  # newer row, and this time the sources had nothing to say

    metrics, _ = research._latest_metrics(CA)

    assert research.links_for(metrics) == [
        ("website", "https://t.xyz"), ("twitter", "https://x.com/t")]


def _counting_pages(monkeypatch, delay: float = 0.05) -> dict:
    """Read the pages for real, and count how many are in flight at once. Each one is slowed
    down enough that a pass reading them one after another shows up as a peak of one."""
    live = {"now": 0, "peak": 0}
    guard = threading.Lock()
    inner = research._read

    def wrapped(kind, url, budget):
        with guard:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        try:
            time.sleep(delay)
            return inner(kind, url, budget)
        finally:
            with guard:
                live["now"] -= 1

    monkeypatch.setattr(research, "_read", wrapped)
    return live


def test_a_token_links_are_read_at_the_same_time(isolated_db, monkeypatch):
    """One card's links are four different hosts. Read one after another and the card waits for
    four page loads; read together and it waits for the slowest one. The order still holds -
    the website first - because the prompt and the stored evidence both read it that way."""
    draft.store_entries("arc", {CA: _payload()}, metrics={CA: _metrics()})
    _snapshot(CA, socials={"twitter": "https://x.com/t", "telegram": "https://t.me/t",
                           "github": "https://g.com/t"}, websites=["https://t.xyz"])
    _queued()
    live = _counting_pages(monkeypatch)
    _page(monkeypatch)  # the pages are fake, the reads are real
    seen: dict = {}
    _model_answering(monkeypatch, seen)

    assert research.work_once(limit=1)[0]["state"] == "done"
    assert live["peak"] == 4, "four links, four reads in flight"
    assert seen["pages"].index("### website https://t.xyz") < seen["pages"].index(
        "### twitter https://x.com/t"), "the order the list was built in is the order read"


def test_a_pass_researches_its_cards_at_the_same_time(isolated_db, monkeypatch):
    """Five cards, five page loads and five model calls. A pass that does them one at a time
    costs the sum; the cards are independent of each other, so it should cost the slowest."""
    for ca in (CA, CA2, CA3):
        draft.store_entries("arc", {ca: _payload()}, metrics={ca: _metrics()})
        _queued(ca)
    live = {"now": 0, "peak": 0}
    guard = threading.Lock()

    def fake_one(row):
        with guard:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        time.sleep(0.05)
        with guard:
            live["now"] -= 1
        return {"ca": str(row["ca"]), "chain_id": "arc", "state": "done"}

    monkeypatch.setattr(research, "one", fake_one)

    out = research.work_once(limit=3)

    assert live["peak"] == 3, "three cards, three reads in flight"
    assert sorted(row["ca"] for row in out) == sorted([CA, CA2, CA3])
    assert {row["state"] for row in out} == {"done"}


def test_a_card_that_fails_leaves_its_neighbours_alone(isolated_db, monkeypatch):
    """The per-card boundary has to survive the concurrency: one card without a readable page
    writes `failed` on its own row, and the two beside it are still researched."""
    for ca in (CA, CA2, CA3):
        draft.store_entries("arc", {ca: _payload()}, metrics={ca: _metrics()})
        _queued(ca)

    def fake_one(row):
        if str(row["ca"]) == CA2:
            raise ValueError("页面上什么都没有")
        return {"ca": str(row["ca"]), "chain_id": "arc", "state": "done"}

    monkeypatch.setattr(research, "one", fake_one)

    out = {row["ca"]: row for row in research.work_once(limit=3)}

    assert out[CA2]["state"] == "failed"
    assert out[CA]["state"] == "done" and out[CA3]["state"] == "done"
    assert _row(CA2)["research_state"] == "failed"
    assert _row(CA)["research_state"] != "failed", "the neighbours are not dragged down"
