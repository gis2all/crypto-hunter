"""HTTP contract: the surface the SPA is built against."""

import copy
import json
import re

from app import config
from app.api.v1 import forms
from app.pipeline import schedule
from app.report import assemble

ARC = "arc"


def test_health_reports_environment(client):
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["db"] is True
    assert ARC in body["chains"]


def test_chains_come_from_config(client):
    chains = client.get("/api/v1/chains").json()
    assert {c["id"] for c in chains} == {"arc", "bsc"}
    arc = next(c for c in chains if c["id"] == ARC)
    assert arc["enabled"] is True
    assert arc["report"]["per_round"] == 5


def test_unknown_chain_is_404(client):
    assert client.get("/api/v1/chains/solana").status_code == 404


def test_run_lifecycle(client, no_live_rounds):
    created = client.post("/api/v1/runs", json={"chain_id": ARC})
    assert created.status_code == 201
    run = created.json()
    assert run["status"] == "pending"
    # Starting a round is the request's job; running it belongs to the scheduler.
    assert no_live_rounds == [(ARC, run["id"])]

    fetched = client.get(f"/api/v1/runs/{run['id']}").json()
    assert fetched["id"] == run["id"]

    listed = client.get(f"/api/v1/runs?chain_id={ARC}").json()
    assert [r["id"] for r in listed] == [run["id"]]

    assert client.get(f"/api/v1/runs/{run['id']}/reports").json() == []


def test_unknown_run_is_404(client):
    assert client.get("/api/v1/runs/nope").status_code == 404
    assert client.get("/api/v1/runs/nope/reports").status_code == 404


def test_run_stream_emits_current_status_then_done(client, monkeypatch):
    from app.core.models import RunStatus

    run = client.post("/api/v1/runs", json={"chain_id": ARC}).json()
    monkeypatch.setattr(
        "app.api.v1.runs._fetch",
        lambda run_id: {"status": RunStatus.FAILED.value, "error": "collector unavailable"},
    )
    with client.stream("GET", f"/api/v1/runs/{run['id']}/stream?timeout_s=5") as resp:
        text = "".join(chunk for chunk in resp.iter_text())
    assert "status" in text and "done" in text


def test_settings_default_to_yaml_then_version_up(client):
    default = client.get(f"/api/v1/chains/{ARC}/settings").json()
    assert default["version"] == 0 and default["source"] == "config/chains.yaml"
    assert default["payload"]["report"]["per_round"] == 5

    payload = {"pipeline.scoring.onchain_weight": 55, "pipeline.scoring.product_weight": 45}
    first = client.put(f"/api/v1/chains/{ARC}/settings", json={"patch": payload, "note": "tweak"}).json()
    assert first["version"] == 1 and first["source"] == "db"

    second = client.put(f"/api/v1/chains/{ARC}/settings", json={"patch": payload}).json()
    assert second["version"] == 2


def test_settings_reject_unknown_paths(client):
    """A typo'd path is a 422, not a knob that silently does nothing."""
    path = f"/api/v1/chains/{ARC}/settings"
    assert client.put(path, json={"patch": {"scoring.onchain_weight": 60}}).status_code == 422
    assert client.put(path, json={"patch": {"pipeline.prefilter.nope": 1}}).status_code == 422
    assert client.put(path, json={"patch": {"pipeline.prefilter": 5}}).status_code == 422


def test_weight_sum_is_guarded_on_the_real_path(client):
    """The knobs the settings page edits live under pipeline.scoring, not at the top."""
    path = f"/api/v1/chains/{ARC}/settings"
    both_weights = {"pipeline.scoring.onchain_weight": 60, "pipeline.scoring.product_weight": 50}
    assert client.put(path, json={"patch": both_weights}).status_code == 422

    factor_weight = {"pipeline.scoring.onchain_factor_weights.liquidity": 30}
    assert client.put(path, json={"patch": factor_weight}).status_code == 422


def test_settings_patch_takes_dotted_paths_and_coerces(client):
    path = f"/api/v1/chains/{ARC}/settings"
    resp = client.put(path, json={"patch": {"pipeline.prefilter.liquidity_min_usd": "31000"}})
    assert resp.status_code == 200
    prefilter = resp.json()["payload"]["pipeline"]["prefilter"]
    assert prefilter["liquidity_min_usd"] == 31000
    assert isinstance(prefilter["liquidity_min_usd"], int)
    assert prefilter["holders_min"] is not None


def test_interval_hours_drives_cron(client):
    path = f"/api/v1/chains/{ARC}/settings"
    body = client.put(path, json={"patch": {"schedule.interval_hours": 6}}).json()["payload"]
    assert body["schedule"]["interval_hours"] == 6
    assert body["schedule"]["cron"] == "0 2,8,14,20 * * *"


def test_settings_form_shows_the_saved_version_not_the_yaml(client):
    """A saved knob must come back on reload; the YAML is only the starting point."""
    path = f"/api/v1/chains/{ARC}/settings"
    client.put(path, json={"patch": {"pipeline.prefilter.liquidity_min_usd": 31000}})

    form = client.get(f"{path}/form").json()
    fields = {f["key"]: f for b in form["blocks"] for g in b["groups"] for f in g["fields"]}
    assert fields["pipeline.prefilter.liquidity_min_usd"]["value"] == "31000"
    assert form["version"] == 1


def test_a_blank_on_a_knob_that_has_a_number_is_refused_not_stored(client):
    """Only a knob that ships blank may be handed back blank. Clearing 流动性's floor
    would leave the round a rule it cannot compare at all, and that used to reach the
    database and surface as a 500 on the next save - a 422 at the door says which box."""
    path = f"/api/v1/chains/{ARC}/settings"
    resp = client.put(path, json={"patch": {"pipeline.prefilter.liquidity_min_usd": ""}})
    assert resp.status_code == 422
    assert "liquidity_min_usd" in resp.json()["detail"]

def test_a_text_knob_can_still_be_cleared(client):
    """The blank rule is about numbers only. Emptying a text knob is a legitimate
    edit, and it must not be caught by the net that stops a rule losing its number."""
    path = f"/api/v1/chains/{ARC}/settings"
    resp = client.put(path, json={"patch": {"schedule.timezone": ""}})
    assert resp.status_code == 200
    assert resp.json()["payload"]["schedule"]["timezone"] == ""

def test_the_holder_band_is_one_control_over_two_leaves(client):
    """持有人 became a band, and a band is one control: the widget carries both keys so
    a reader sees a floor and a ceiling on one row instead of two rules that look
    unrelated. The ceiling ships blank, so the value is a trailing comma."""
    form = client.get(f"/api/v1/chains/{ARC}/settings/form").json()
    fields = {f["key"]: f for b in form["blocks"] for g in b["groups"] for f in g["fields"]}
    field = fields["pipeline.prefilter.holders_min"]
    assert field["widget"] == "range"
    assert field["key2"] == "pipeline.prefilter.holders_max"
    assert field["value"] == "100,"


def test_a_ceiling_that_starts_blank_still_saves_as_a_number(client):
    """holders_max is null in the YAML - no ceiling is a legal answer. The first number
    typed into that box still has to land as an int: the round compares it against a
    holder count, and an int compared against a string raises instead of filtering.
    Emptying the box has to put it back to no ceiling."""
    path = f"/api/v1/chains/{ARC}/settings"
    saved = client.put(path, json={"patch": {"pipeline.prefilter.holders_max": "2000"}}).json()
    assert saved["payload"]["pipeline"]["prefilter"]["holders_max"] == 2000
    cleared = client.put(path, json={"patch": {"pipeline.prefilter.holders_max": ""}}).json()
    assert cleared["payload"]["pipeline"]["prefilter"]["holders_max"] is None

def test_settings_form_is_data_not_markup(client):
    form = client.get(f"/api/v1/chains/{ARC}/settings/form").json()
    assert form["chain_id"] == ARC
    assert [b["key"] for b in form["blocks"]] == ["onchain", "product", "run"]
    fields = [f for b in form["blocks"] for g in b["groups"] for f in g["fields"]]
    assert len({f["key"] for f in fields}) == len(fields)
    assert all("." in f["key"] for f in fields)
    assert {f["widget"] for f in fields} >= {"num", "range", "bool", "select", "checks", "ro", "sum"}
    assert isinstance(form["totals"], dict) and isinstance(form["lexicon"], list)
    blob = json.dumps(form, ensure_ascii=False)  # every label is data, never markup
    assert not re.search(r"</?(b|code|br|a|span|div|i)\b", blob)


def _fields(form):
    return {f["key"]: f for b in form["blocks"] for g in b["groups"] for f in g["fields"]}


def test_settings_form_carries_both_ends_of_a_range(client):
    """A range row is one control over two leaves, so it has to round-trip as a pair."""
    form = client.get(f"/api/v1/chains/{ARC}/settings/form").json()
    cap = _fields(form)["pipeline.prefilter.market_cap_min_usd"]
    assert cap["widget"] == "range"
    assert cap["key2"] == "pipeline.prefilter.market_cap_max_usd"
    assert cap["value"].split(",") == ["10000", "200000"]


def test_scan_rows_list_scanners_only(client):
    """GoPlus and Sourcify verify safety; offering them as scan sources would be a lie."""
    form = client.get(f"/api/v1/chains/{ARC}/settings/form").json()
    field = _fields(form)["pipeline.scan_sources"]
    assert [c["value"] for c in field["checks"]] == [
        "gmgn", "radardex", "tolly", "geckoterminal",
    ]


def test_the_freshness_knobs_are_controls_and_not_decoration(client):
    """Four knobs behind `core.lexicon.due`, plus the two read-only rows that say what they
    are doing right now. A knob on screen with no rule behind it is worse than no knob."""
    form = client.get(f"/api/v1/chains/{ARC}/settings/form").json()
    fields = _fields(form)
    for key in ("pipeline.lexicon.expire_days", "pipeline.lexicon.mcap_move_pct",
                "pipeline.lexicon.holders_gain_pct", "pipeline.lexicon.rewrite_skip_hours"):
        assert fields[key]["widget"] == "num", key
        assert fields[key]["value"], key
    assert fields["pipeline.scoring.lexicon_stale"]["widget"] == "ro"
    assert fields["report.research_enabled"]["widget"] == "bool"
    # The two read-only rows are fed by totals, so a totals field missing from the response
    # model would drop them on the floor - which is exactly what happened once already.
    assert form["totals"]["lexicon_stale"] == 0
    assert form["totals"]["lexicon_research"].startswith("已重档")


def test_a_knob_that_is_a_setting_can_be_changed(client):
    path = f"/api/v1/chains/{ARC}/settings"
    patched = client.put(
        path, json={"patch": {"pipeline.lexicon.rewrite_skip_hours": 12}}
    )
    assert patched.status_code == 200

    fields = _fields(client.get(f"{path}/form").json())
    assert fields["pipeline.lexicon.rewrite_skip_hours"]["value"] == "12"


def test_run_rows_say_what_a_change_does_instead_of_counting(client):
    """Not every row eliminates: the run block replaces淘汰数 with "下轮生效"."""
    form = client.get(f"/api/v1/chains/{ARC}/settings/form").json()
    fields = _fields(form)
    assert fields["schedule.interval_hours"]["drop_x"] == "下轮生效"
    assert fields["schedule.interval_hours"]["drop_count"] is None
    assert fields["schedule.cron"]["drop_x"] == ""


def test_caliber_line_is_generated_from_the_settings(client):
    """The overview口径 and the settings page read the same knobs, so they cannot drift."""
    lines = forms.caliber_text(config.chain(ARC))
    assert len(lines) == 3
    assert lines[0].startswith("硬过滤：市值 $10K – $200K ｜ 流动性 ≥ $5K")
    assert "陷阱占比 < 45%" in lines[1]
    assert lines[2] == "评分 = 质量分 55%（流动性/成交/持有人/集中度/捆绑）+ 赛道产品分 45%（词条库）"


def test_caliber_line_follows_a_change(client):
    """Change the knob, and the口径 sentence moves with it."""
    chain = copy.deepcopy(config.chain(ARC))
    chain["pipeline"]["prefilter"]["liquidity_min_usd"] = 31000
    lines = forms.caliber_text(chain)
    assert "流动性 ≥ $31K" in lines[0]


def test_caliber_reads_the_holder_band_only_when_there_is_a_ceiling(client):
    """With the ceiling blank the sentence stays the floor it always was, so an old
    snapshot reads exactly as it did; fill one in and the line becomes a band."""
    chain = copy.deepcopy(config.chain(ARC))
    chain["pipeline"]["prefilter"]["holders_max"] = 2000
    assert "持有人 100 – 2000" in forms.caliber_text(chain)[0]

def test_lexicon_rows_without_a_symbol_sort_last(client):
    """A drafted file has no symbol yet; it belongs at the end of the table, not in
    front of the named entries."""
    for ca, sym in (("0xaaa", "Zeta"), ("0xbbb", None), ("0xccc", "Alpha")):
        body = {"tag": "meme", "narrative": 3.0, "product": 3.0}
        if sym:
            body["symbol"] = sym
        assert client.put(f"/api/v1/lexicon/{ca}", json=body).status_code == 200

    rows = client.get(f"/api/v1/chains/{ARC}/settings/form").json()["lexicon"]
    assert [r["ca"] for r in rows] == ["0xccc", "0xaaa", "0xbbb"]


def test_push_group_carries_no_read_only_banner(client):
    """The banner was a `sub` row reading 只读状态（系统告诉你的，不是你配的）. Cut on
    2026-09-26: it restated what the controls above it already show, and in the narrowest
    group on the page it cost a whole line."""
    form = client.get(f"/api/v1/chains/{ARC}/settings/form").json()
    groups = {g["key"]: g for b in form["blocks"] for g in b["groups"]}
    push = groups["push"]
    assert push["note"] == ""
    assert [f for f in push["fields"] if f["widget"] == "sub"] == []


def test_push_content_chip_carries_the_card_count(client):
    form = client.get(f"/api/v1/chains/{ARC}/settings/form").json()
    chips = _fields(form)["report.push_content"]["checks"]
    labels = {c["value"]: c["label"] for c in chips}
    # The count is this round's selection, so it tracks the data instead of being fixed.
    assert labels["cards"] == f"{form['totals']['selected']} 张报告卡"
    assert labels["table"] == "对比表"


def test_read_only_rows_carry_the_prototype_action(client):
    """The credentials and copy rows sit next to an action in the prototype. Ours is
    inert, but it has to be there and say why, or the row reads differently."""
    fields = _fields(client.get(f"/api/v1/chains/{ARC}/settings/form").json())
    sources = fields["runtime.sources"]
    assert sources["ro_row"] is True and sources["ro_value"] is True
    assert sources["ro_button"] == "重新授权" and sources["ro_button_note"]
    copy_row = fields["runtime.copy"]
    assert copy_row["ro_button"] == "复制" and copy_row["ro_button_note"]


def test_credentials_read_in_scan_order(client):
    form = client.get(f"/api/v1/chains/{ARC}/settings/form").json()
    shown = _fields(form)["runtime.sources"]["value"]
    assert shown == "GMGN · RadarDEX · Tolly · GT · GoPlus · Sourcify"


def test_a_round_without_a_summary_reports_no_push_status(client):
    """The round-level facts table only exists for rounds we imported with it. A round
    without it has no delivery status - the reason belongs in its own field, never in
    the status the UI prints."""
    run = client.post("/api/v1/runs", json={"chain_id": ARC, "trigger": "manual"}).json()
    client.post("/api/v1/runs", json={"chain_id": ARC}).json()
    batch = client.get(f"/api/v1/runs/{run['id']}/batch").json()
    assert batch["push"]["status"] == ""
    assert batch["push"]["unavailable"]


def test_the_settings_run_button_hands_the_round_to_the_worker(client, monkeypatch):
    """Both ways to start a round have to submit it, and only one of them used to. The
    ticker reads the clock, so a row nobody submits sits at `pending` forever."""
    started = []
    monkeypatch.setattr(schedule, "submit",
                        lambda chain_id, run_id: started.append((chain_id, run_id)))
    body = client.post(f"/api/v1/chains/{ARC}/settings/run").json()
    assert started == [(ARC, body["id"])]

def test_measured_averages_come_from_the_scores_not_from_the_rounded_cards(client, isolated_db):
    """Averaging numbers that were already rounded rounds twice.

    These five real totals sum to 278.7389, so the mean is 55.7. Round each of them for
    display first (60.1 / 56.5 / 54.9 / 54.0 / 53.3) and the mean comes out 55.8.
    """
    from app.store import db

    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO runs (id, run_key, chain_id, trigger, status, selected)"
            " VALUES ('r1', 'r1', ?, 'manual', 'sent', 5)",
            (ARC,),
        )
        for index, total in enumerate([60.072, 56.5309, 54.9015, 53.9655, 53.269]):
            conn.execute(
                "INSERT INTO run_items (run_id, ca, decision, score, breakdown_json)"
                " VALUES ('r1', ?, 'selected', ?, ?)",
                ("0x%02d" % index, total,
                 json.dumps({"onchain": 70.0, "product": 46.0, "total": total})),
            )
        conn.commit()
    finally:
        conn.close()

    totals = client.get(f"/api/v1/chains/{ARC}/settings/form").json()["totals"]
    assert totals["total_avg"] == 55.7
    assert totals["onchain_avg"] == 70.0


def test_the_market_cap_reason_follows_the_rounds_own_bound():
    """A round that declines a token for市值 and the口径 line above it quote one number.

    The bound is read back from the settings the round ran under, never written into
    the sentence by hand.
    """
    metrics = {"holders": 281, "honeypot_pct": 0, "market_cap_usd": 40605.4}
    safety = {"lp_locked": True}
    assert assemble._decline_reasons(metrics, safety, 50000) == ["市值 <$50K"]
    assert assemble._decline_reasons(metrics, safety, 0) == []
    assert assemble._decline_reasons(metrics, safety, None) == []
    assert assemble._decline_reasons(metrics, safety, 20000) == []


def test_settings_can_be_copied_from_a_chain_that_is_not_collecting_yet(client):
    """BSC is declared and carries defaults, so it is a real copy source - the rail
    showing it as「待接」does not make the row meaningless."""
    form = client.get(f"/api/v1/chains/{ARC}/settings/form").json()
    assert _fields(form)["runtime.copy"]["value"] == "从「BSC 雷达」复制"


def test_lexicon_updated_date_comes_from_the_registry_file(tmp_path):
    """The lexicon dates its own content. The page must show that date, not the
    moment this database happened to import the file."""
    from app import seed_demo

    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps({"_meta": {"updated": "2026-09-21"}, "0xABC": {"sym": "BCAT"}}),
        encoding="utf-8",
    )
    assert seed_demo._registry_meta(path) == "2026-09-21"
    assert list(seed_demo._registry(path)) == ["0xabc"]
    assert seed_demo._registry_meta(tmp_path / "nope.json") is None


def test_recompute_is_explicitly_blocked(client):
    resp = client.post(f"/api/v1/chains/{ARC}/settings/recompute", json={"patch": {}})
    assert resp.status_code == 501
    assert resp.json()["blocked_by"]


def test_lexicon_pending_flow(client):
    assert client.get("/api/v1/lexicon").json() == []
    created = client.put(
        "/api/v1/lexicon/0xABC",
        json={"symbol": "BCAT", "tag": "meme", "narrative": 3.5, "product": 4.0},
    )
    assert created.status_code == 200
    assert created.json()["review_state"] == "pending"
    assert len(client.get("/api/v1/lexicon/pending").json()) == 1

    confirmed = client.put("/api/v1/lexicon/0xABC", json={"symbol": "BCAT", "review_state": "confirmed"})
    assert confirmed.json()["review_state"] == "confirmed"
    assert client.get("/api/v1/lexicon/pending").json() == []


def test_every_candidate_that_missed_the_cut_is_in_the_pool(isolated_db):
    """候选 - 入选 is the row count in 落选池.

    The overview prints those two numbers in the heading and then draws the table under
    them, so a candidate that never got a number cannot fall out of the list: the heading
    would count candidates the rows do not show. It gets the reason it has no number
    instead of the fact-level reasons, which for a token nobody could price are the same
    gap said three times.
    """
    from app.store import db

    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO runs (id, run_key, chain_id, trigger, status, scanned, dropped,"
            " candidates, selected) VALUES ('r2', 'r2', ?, 'manual', 'written', 10, 6, 3, 1)",
            (ARC,),
        )
        for ca, decision, score, pending, known in [
            ("0x01", "selected", 71.2, False, True),
            ("0x02", "candidate", 64.0, False, True),
            ("0x03", "candidate", None, True, False),
            ("0x04", "candidate", None, False, False),
            ("0x05", "dropped", None, False, False),
        ]:
            metrics = {"ca": ca, "symbol": ca, "honeypot_pct": 0}
            if known:
                metrics["holders"] = 240
            conn.execute(
                "INSERT INTO run_items (run_id, ca, decision, score, breakdown_json)"
                " VALUES ('r2', ?, ?, ?, ?)",
                (ca, decision, score, json.dumps({"pending_review": pending})),
            )
            conn.execute(
                "INSERT INTO snapshots (run_id, ca, metrics_json, safety_json, social_json,"
                " created_at) VALUES ('r2', ?, ?, '{}', '{}', '2026-09-24T00:00:00+08:00')",
                (ca, json.dumps(metrics)),
            )
        conn.commit()
    finally:
        conn.close()

    pool = assemble.decline("r2")
    assert [row["sym"] for row in pool] == ["0x02", "0x03", "0x04"]
    assert [row["score"] for row in pool] == [64.0, None, None]
    assert pool[0]["reason"] == "评分未进前 5 ｜ LP 未判定"
    assert pool[1]["reason"] == "本轮未产出评分 ｜ 未建档（本轮不参与排名）"
    assert pool[2]["reason"] == "本轮未产出评分 ｜ 数据缺失，本轮不参与排名"
