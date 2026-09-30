"""HTTP contract: the surface the SPA is built against."""

import copy
import json
import re

from app import config
from app.api.v1 import forms
from app.pipeline import schedule
from app.report import assemble
from app.store import db

ARC = "arc"
ROBINHOOD = "robinhood"


def test_health_reports_environment(client):
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["db"] is True
    assert ARC in body["chains"]


def test_chains_come_from_config(client):
    chains = client.get("/api/v1/chains").json()
    assert {c["id"] for c in chains} == {"arc", "bsc", "robinhood"}
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
    assert default["version"] == 0 and default["source"] == "config/chains/arc.yaml"
    assert default["payload"]["report"]["per_round"] == 5

    payload = {"pipeline.scoring.onchain_weight": 55, "pipeline.scoring.offchain_weight": 45}
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
    """The knobs the settings page edits live under pipeline.scoring, not at the top, and
    the refusal reads like the page does: it names the box and prints the current sum
    instead of leaving the operator to add five numbers by hand."""
    path = f"/api/v1/chains/{ARC}/settings"

    both = {"pipeline.scoring.onchain_weight": 60, "pipeline.scoring.offchain_weight": 50}
    blocked = client.put(path, json={"patch": both})
    assert blocked.status_code == 422
    assert blocked.json()["detail"] == (
        "链上质量权重 ＋ 链下调研权重必须等于 100%，当前 110%（链上质量 60% ＋ 链下调研 50%）")

    onchain = {"pipeline.scoring.onchain_factor_weights.liquidity": 30}
    blocked = client.put(path, json={"patch": onchain})
    assert blocked.status_code == 422
    assert "链上质量五项权重合计要等于 100%，当前 110%" in blocked.json()["detail"]
    assert "流动性 30%" in blocked.json()["detail"]

    offchain = {"pipeline.scoring.offchain_factor_weights.f_team": 30}
    blocked = client.put(path, json={"patch": offchain})
    assert blocked.status_code == 422
    assert "链下调研五项权重合计要等于 100%，当前 110%" in blocked.json()["detail"]
    assert "团队与 dev 可信度 30%" in blocked.json()["detail"]

    # Rebalanced, the same knob saves: the check is on the sum, not on any single row.
    balanced = {"pipeline.scoring.offchain_factor_weights.f_team": 30,
                "pipeline.scoring.offchain_factor_weights.f_substance": 10}
    saved = client.put(path, json={"patch": balanced})
    assert saved.status_code == 200
    weights = saved.json()["payload"]["pipeline"]["scoring"]["offchain_factor_weights"]
    assert weights["f_team"] == 30 and weights["f_substance"] == 10


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


def test_a_saved_version_reaches_the_next_round(client):
    '''Saving was only half the promise: the round has to pin it, and none did.

    Every round the app created left `settings_version_id` NULL, so the round read the
    YAML while the settings page showed the saved version - two answers to one question
    on two halves of the same screen.
    '''
    run = client.post("/api/v1/runs", json={"chain_id": ARC}).json()
    assert run["settings_version_id"] is None

    client.put(f"/api/v1/chains/{ARC}/settings",
               json={"patch": {"pipeline.prefilter.liquidity_min_usd": 31000}})
    run = client.post("/api/v1/runs", json={"chain_id": ARC}).json()
    assert run["settings_version_id"] is not None

    settings = config.settings_payload(ARC, run["settings_version_id"])
    assert settings["pipeline"]["prefilter"]["liquidity_min_usd"] == 31000


def test_a_round_already_queued_keeps_the_version_it_was_created_under(client):
    '''A save must not move a knob under a round that is already written down.'''
    client.put(f"/api/v1/chains/{ARC}/settings",
               json={"patch": {"pipeline.prefilter.liquidity_min_usd": 31000}})
    queued_run = client.post("/api/v1/runs", json={"chain_id": ARC}).json()

    client.put(f"/api/v1/chains/{ARC}/settings",
               json={"patch": {"pipeline.prefilter.liquidity_min_usd": 9000}})

    settings = config.settings_payload(ARC, queued_run["settings_version_id"])
    assert settings["pipeline"]["prefilter"]["liquidity_min_usd"] == 31000


def test_a_knob_the_yaml_dropped_is_not_carried_into_the_next_version(client):
    '''A merge only ever adds, so an orphan rode along in every version after it.

    `product_weight` was renamed to `offchain_weight` and `unlisted_policy` was dropped,
    but each save merged the version before it forward - so both stayed in the stored
    payload, read by nobody and read by the archive as if the round had run that way.
    '''
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO settings_versions (chain_id, version, payload_json, note, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (ARC, 1, json.dumps({"pipeline": {"scoring": {
                "product_weight": 45, "unlisted_policy": "pending_review",
                "onchain_weight": 55, "offchain_weight": 45}}}), "seeded", db.now_iso()),
        )
    finally:
        conn.close()

    saved = client.put(f"/api/v1/chains/{ARC}/settings",
                       json={"patch": {"pipeline.scoring.onchain_weight": 55}}).json()
    scoring = saved["payload"]["pipeline"]["scoring"]
    assert "product_weight" not in scoring
    assert "unlisted_policy" not in scoring
    assert scoring["offchain_weight"] == 45
    assert scoring["onchain_factor_weights"]["liquidity"] == 20


def test_a_stored_version_is_read_against_the_yaml_shape(client):
    '''Every version already in the database carries such a key; a round reading one
    must see the settings it actually ran with.'''
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO settings_versions (chain_id, version, payload_json, note, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (ARC, 1, json.dumps({"pipeline": {
                "prefilter": {"liquidity_min_usd": 31000},
                "scoring": {"unlisted_policy": "pending_review"}}}), "seeded", db.now_iso()),
        )
        version_id = cur.lastrowid
    finally:
        conn.close()

    settings = config.settings_payload(ARC, version_id)
    assert settings["pipeline"]["prefilter"]["liquidity_min_usd"] == 31000
    assert "unlisted_policy" not in settings["pipeline"]["scoring"]


def test_a_patch_to_a_knob_the_yaml_dropped_is_refused(client):
    '''The guard exists so that an edit which cannot apply is refused, not stored.

    An orphan in the saved version used to count as a known path, so a patch aimed at one
    was accepted and then pruned away: the page reported a save and nothing changed.
    '''
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO settings_versions (chain_id, version, payload_json, note, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (ARC, 1, json.dumps({"pipeline": {"scoring": {"product_weight": 45}}}),
             "seeded", db.now_iso()),
        )
    finally:
        conn.close()

    resp = client.put(f"/api/v1/chains/{ARC}/settings",
                      json={"patch": {"pipeline.scoring.product_weight": 99}})
    assert resp.status_code == 422
    assert "product_weight" in resp.json()["detail"]


def test_a_blank_on_a_knob_that_has_a_number_is_refused_not_stored(client):
    """Only a knob that ships blank may be handed back blank. Clearing 流动性's floor
    would leave the round a rule it cannot compare at all, and that used to reach the
    database and surface as a 500 on the next save - a 422 at the door says which box,
    in the words printed next to it and not in the path this layer happens to walk."""
    path = f"/api/v1/chains/{ARC}/settings"
    resp = client.put(path, json={"patch": {"pipeline.prefilter.liquidity_min_usd": ""}})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "「流动性」不能留空：这个格子必须有个数"

    # A range is one label over two boxes, so the refusal has to say which end went blank.
    resp = client.put(path, json={"patch": {"pipeline.prefilter.market_cap_max_usd": ""}})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "「市值上限」不能留空：这个格子必须有个数"

    # The page posts a nested group, not a dotted path (toPatch builds the tree), and the
    # label map has to survive that shape as well - it is threaded through the whole walk.
    resp = client.put(path, json={"patch": {"pipeline": {"prefilter": {"liquidity_min_usd": ""}}}})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "「流动性」不能留空：这个格子必须有个数"

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
    assert [b["key"] for b in form["blocks"]] == ["onchain", "offchain", "run"]
    fields = [f for b in form["blocks"] for g in b["groups"] for f in g["fields"]]
    assert len({f["key"] for f in fields}) == len(fields)
    assert all("." in f["key"] for f in fields)
    assert {f["widget"] for f in fields} >= {"num", "range", "bool", "select", "checks", "ro"}
    assert isinstance(form["totals"], dict)
    assert "lexicon" not in form, "the off-chain block is scored, not browsed"
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
    """GoPlus and Sourcify verify safety; offering them as scan sources would be a lie.

    The row is also the chain's own list rather than a constant: Arc scans RadarDEX and
    Tolly, Robinhood scans its launchpads, and neither page may offer the other's.
    """
    arc = _fields(client.get(f"/api/v1/chains/{ARC}/settings/form").json())
    assert [c["value"] for c in arc["pipeline.scan_sources"]["checks"]] == [
        "gmgn", "radardex", "tolly", "geckoterminal",
    ]

    robinhood = _fields(client.get(f"/api/v1/chains/{ROBINHOOD}/settings/form").json())
    assert [c["value"] for c in robinhood["pipeline.scan_sources"]["checks"]] == [
        "gmgn", "coinbarrel", "pez", "peeps", "geckoterminal",
    ]


def test_the_offchain_block_weights_its_five_judgements(client):
    """The five judgements are weights now (2026-09-28), drawn like the on-chain block's
    factors: a 20% knob each. The anchor a judgement is scored against and the 数据来源
    line both read once, so they live in the group's 说明 instead of taking five rows."""
    form = client.get(f"/api/v1/chains/{ARC}/settings/form").json()
    fields = _fields(form)
    assert fields["pipeline.scoring.offchain_weight"]["widget"] == "num"
    for key in ("f_completeness", "f_substance", "f_team", "f_narrative", "f_community"):
        row = fields["pipeline.scoring.offchain_factor_weights." + key]
        assert row["widget"] == "num", key
        assert row["value"] == "20", key
        assert row["unit"] == "%", key
    assert "pipeline.scoring.source" not in fields
    groups = {g["key"]: g for b in form["blocks"] for g in b["groups"]}
    detail = groups["scoring"]["detail"]
    assert "数据来源：每轮全新调研" in detail
    assert "0–5 · 有界面 / 能交互 / 收费并迭代" in detail
    assert "0–5 · 互动率与持续性，粉丝数无效" in detail
    # There is no research on/off switch any more: the reading pass is what writes the
    # entry, and an entry is what the 45% block is scored from - it cannot be optional.
    assert "report.research_enabled" not in fields


def test_a_knob_that_is_a_setting_can_be_changed(client):
    path = f"/api/v1/chains/{ARC}/settings"
    patched = client.put(path, json={"patch": {"report.per_round": 3}})
    assert patched.status_code == 200

    fields = _fields(client.get(f"{path}/form").json())
    assert fields["report.per_round"]["value"] == "3"


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
    assert lines[2] == "评分 = 质量分 55%（流动性/成交/持有人/集中度/捆绑）+ 链下调研分 45%（每轮全新调研）"


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


def test_read_only_rows_carry_no_inert_action(client):
    """数据源凭据 used to sit next to a dead 重新授权 button, and 复制参数 next to a dead
    复制. Neither action exists, so the rows carry the reading alone."""
    fields = _fields(client.get(f"/api/v1/chains/{ARC}/settings/form").json())
    sources = fields["runtime.sources"]
    assert sources["ro_row"] is True and sources["ro_value"] is True
    assert "runtime.copy" not in fields
    assert not any(
        key in field for field in fields.values() for key in ("ro_button", "ro_button_note")
    )


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
                        lambda chain_id, run_id, config_id=None:
                        started.append((chain_id, run_id)))
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
            " VALUES ('r1', 'r1', ?, 'manual', 'researched', 5)",
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


def test_settings_carry_no_cross_chain_copy_row(client):
    """复制参数 was never wired up, so the row is gone rather than left inert."""
    form = client.get(f"/api/v1/chains/{ARC}/settings/form").json()
    assert "runtime.copy" not in _fields(form)


def test_eliminations_travel_in_the_group_note(client):
    """本轮淘汰 left the rows: the counts are a reading of the group, not a second value
    beside every knob, so they print inside the header note."""
    form = client.get(f"/api/v1/chains/{ARC}/settings/form").json()
    groups = {g["key"]: g for b in form["blocks"] for g in b["groups"]}
    for key in ("hard", "safety"):
        group = groups[key]
        assert group["detail"].count("本轮淘汰：") == 1
        assert all(f["drop_count"] is None for f in group["fields"])


def test_the_scan_reading_names_each_scanner_and_keeps_it_off_the_row():
    """The four switches carry no numbers any more: what each scanner fed the round is a
    reading, and it reads in the group note."""
    from app.api.v1 import forms
    from app.api.v1.schemas import FieldOut

    chain = config.chain(ARC)
    built = forms.build(
        chain,
        {
            "sources": [{"name": "gmgn", "count": 119}, {"name": "radardex", "count": 3025}],
            "universe": 3630,
        },
        {},
    )
    groups = {g["key"]: g for b in built["blocks"] for g in b["groups"]}
    raw = next(f for f in groups["output"]["fields"] if f["key"] == "pipeline.scan_sources")
    field = FieldOut.model_validate(raw)
    assert field.drop_count is None
    assert all(c.count is None for c in field.checks)
    assert groups["output"]["detail"].splitlines()[0] == (
        "本轮扫描源：GMGN 活跃榜 119 · RadarDEX 发射台 3,025；去重后进入漏斗 3,630 个。"
    )


def test_the_block_weights_live_in_the_notes_not_beside_the_titles(client):
    """占 55% / 占 45% are gone from the block headers. Each weight is stated in the note of
    the group that owns its knob, and the page header still spells the split out; 运行 never
    scored, so it says nothing at all."""
    form = client.get(f"/api/v1/chains/{ARC}/settings/form").json()
    assert not any("weight" in block for block in form["blocks"])
    groups = {g["key"]: g for b in form["blocks"] for g in b["groups"]}
    assert groups["quality"]["detail"].startswith("链上质量占综合分 55%。")
    assert groups["scoring"]["detail"].startswith("链下调研占综合分 45%。")
    bar = "".join(run["text"] for run in form["note_bar"])
    assert "链上质量 55%" in bar and "链下调研 45%" in bar

def test_the_group_readings_left_the_page_for_the_note(client):
    """漏斗, 提示不淘汰 and 本轮违规 are one-click readings now: the group draws no
    trailing line, and every one of them is in the header's 说明."""
    form = client.get(f"/api/v1/chains/{ARC}/settings/form").json()
    groups = {g["key"]: g for b in form["blocks"] for g in b["groups"]}
    hard, safety = groups["hard"], groups["safety"]
    assert hard["note"] == "" and "漏斗：扫描" in hard["detail"]
    assert safety["note"] == ""
    assert "提示不淘汰：mintable、blacklist、pausable、is_proxy" in safety["detail"]
    assert "本轮违规：" in safety["detail"]


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
