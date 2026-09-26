"""The writing stage: lexicon drafts, then the report text.

Two jobs, in this order, because the second depends on the first.

1. **lexicon drafts** - a first entry for every shortlisted token that has none, written in
   batches (see `CH_LLM_DRAFT_BATCH`). This is
   not politeness: this chain's policy is 未入库标的「待建档，本轮不参与排名」, so a token
   with no entry has no product score and cannot be selected at all. The entry is stored
   `pending`; a human confirms it later, and the page says so until then.

   The same call also serves the **rewrite** path: an entry that has gone stale (see
   `core/lexicon` for what stale means) is written again by the same prompt against this
   round facts, so the words on a card and the numbers beside them are never four days apart.
2. **commentary** - 本期点评 for the picks, written against the same facts the cards show.

Both fail loudly and leave the round short rather than filling the gap with prose nobody
decided. A round whose model is unreachable is still a round: it is stored as written,
with no text, and the reason lands in `llm_calls` and in the round's note.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .. import cancel, llm
from ..core import lexicon
from ..core.models import RunStatus
from ..store import db
from . import brief as brief_builder
from . import facts
from .schema import LexiconDraft, validate_commentary, validate_lexicon

PROMPT_DIR = Path(__file__).with_name("prompts")
PROMPT_VERSION = "commentary.md@4"


def read_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _entry_payload(entry, symbol: str | None = None) -> dict:
    """The model's own fields, plus the ticker it was never asked to invent.

    The symbol comes off the round's own metrics, not out of the draft: a lexicon
    row keyed by nothing but its CA is a row nobody can read. It stays None when the
    source never gave one, which the page renders as a dash.
    """
    payload = entry.model_dump()
    payload["symbol"] = symbol or None
    return payload


#: Entries per draft call. Nine fields of prose each, plus the tokens a reasoning model
#: spends thinking before it writes: asked for forty entries in one call, the model runs out
#: of ceiling mid-object and answers with something that is not JSON, which loses every
#: entry instead of the last few. Measured on this chain: five entries cost ~2.5k completion
#: tokens, so five fits a ceiling an order of magnitude smaller than what we send.
DEFAULT_DRAFT_BATCH = 5


def _batch_size() -> int:
    """How many entries one draft call writes. Read per call, like the endpoint itself."""
    raw = os.environ.get("CH_LLM_DRAFT_BATCH", "").strip()
    try:
        return max(1, int(raw or DEFAULT_DRAFT_BATCH))
    except ValueError:
        return DEFAULT_DRAFT_BATCH


def write_lexicon(chain: dict, targets: list[dict], run_id: str, rewrite: bool = False,
                  notes: list[str] | None = None,
                  on_batch: Callable[[float, float], None] | None = None) -> dict[str, dict]:
    """Entries for the tokens this round has to write or rewrite. Stored `pending`.

    One call per batch, validated per batch, so a batch the model cannot answer costs its
    own entries and nothing else; `notes` gets the reason and the round stores it as the
    round own note. Returns `{}` when the model is not reachable at all, which is what makes
    that note honest: the tokens stay out of the ranking instead of being scored on a number
    nobody produced.

    `rewrite=True` is the refresh path: these entries are stale by the rules in
    `core.lexicon` and this call replaces their words. A `confirmed` entry is skipped (see
    `store_entries`) and comes back absent, so the caller keeps the old text and says on the
    card that it was left alone. Only the CAs whose rows were actually written are returned.
    """
    if not targets or not llm.configured():
        return {}
    entries: dict[str, dict] = {}
    failed: list[str] = []
    batches = _chunks(targets, _batch_size())
    for index, batch in enumerate(batches):
        # Between two batches, and only there: the entries of the batches already written are
        # in `entries` and are stored once, after the loop - so a round stopped here leaves no
        # half-written batch behind, it leaves none.
        cancel.raise_if(run_id)
        try:
            entries.update(_draft_batch(batch, run_id))
        except llm.LLMUnavailable as exc:
            failed.append("%d 条：%s" % (len(batch), exc))
        if on_batch:
            on_batch(index + 1, len(batches))
    if not entries:
        raise llm.LLMUnavailable("lexicon draft rejected: %s" % "; ".join(failed[:2]))
    if failed:
        (notes if notes is not None else []).append(
            "词条草稿部分未生成（%d 批：%s）" % (len(failed), "; ".join(failed[:2])))
    metrics = {row["ca"].lower(): row["metrics"] for row in targets}
    # The verdict travels with the numbers: without it every entry written here would carry
    # no safety baseline and the refresh rule that reads one could never fire.
    safety = {row["ca"].lower(): row.get("facts") for row in targets}
    reasons = ({row["ca"].lower(): row.get("reason") for row in targets}
               if rewrite else None)
    written = store_entries(chain["id"], entries, metrics=metrics, reasons=reasons,
                            safety=safety)
    skipped = [ca for ca in entries if ca not in written]
    if rewrite and skipped:
        (notes if notes is not None else []).append(
            "人工已确认，本轮跳过自动改写 %d 条：%s"
            % (len(skipped), "、".join(ca[:10] for ca in skipped[:3])))
    return {ca: entry for ca, entry in entries.items() if ca in written}


def draft_lexicon(chain: dict, gaps: list[dict], run_id: str,
                  notes: list[str] | None = None,
                  on_batch: Callable[[float, float], None] | None = None) -> dict[str, dict]:
    """First entries for shortlisted tokens that have none."""
    return write_lexicon(chain, gaps, run_id, notes=notes, on_batch=on_batch)


def refresh_lexicon(chain: dict, targets: list[dict], run_id: str,
                    notes: list[str] | None = None,
                    on_batch: Callable[[float, float], None] | None = None) -> dict[str, dict]:
    """Rewrite the entries whose refresh condition fired this round.

    Same call as a first draft, on purpose: an entry being replaced is written by the same
    prompt, against the same facts, as one being created, so the two can never drift into two
    different standards of evidence.
    """
    return write_lexicon(chain, targets, run_id, rewrite=True, notes=notes, on_batch=on_batch)


def _chunks(items: list, size: int) -> list[list]:
    return [items[start:start + size] for start in range(0, len(items), size)]


def _draft_batch(gaps: list[dict], run_id: str) -> dict[str, dict]:
    """One call, one batch. Raises rather than returning a half-batch.

    The context is the same sheet the card draws (`report/facts.py`), not a summary of it:
    while this was eight fields wide the model wrote about the only block it had - the safety
    rows - and an entry that reads like a security report is what that produces.
    """
    context = "\n\n".join(facts.block(gap["metrics"], gap["facts"]) for gap in gaps)
    raw = llm.complete_json(
        [
            {"role": "system", "content": read_prompt("lexicon.md")},
            {"role": "user", "content": context},
        ],
        purpose="lexicon_draft",
        run_id=run_id,
        prompt_version="lexicon.md@4",
    )
    issues = validate_lexicon(raw, [gap["ca"] for gap in gaps])
    if issues:
        raise llm.LLMUnavailable("lexicon draft rejected: %s" % "; ".join(issues[:4]))
    draft = LexiconDraft.model_validate(raw)
    symbols = {gap["ca"].lower(): (gap.get("symbol") or "").strip() for gap in gaps}
    return {
        ca.lower(): _entry_payload(entry, symbols.get(ca.lower()))
        for ca, entry in draft.entries.items()
    }



def store_entries(chain_id: str, entries: dict[str, dict], metrics: dict[str, dict] | None = None,
                  reasons: dict[str, str | None] | None = None,
                  observed_at: str | None = None,
                  safety: dict[str, dict] | None = None) -> set[str]:
    """Write entries as `pending`: a draft is a starting point, not a verdict.

    Every write also stamps the baseline the entry was written against - the numbers and
    the safety verdict the refresh conditions in `core.lexicon` measure movement from.
    Without them a 40% move has nothing to compare against and the rules are decoration;
    the verdict matters the same way, because an entry written while the LP question was
    open has to be written again once it is answered.

    Returns the CAs that were actually written. A `confirmed` entry is a human decision and
    this function does not overturn it: the WHERE clause drops those rows, rowcount comes
    back 0, and the caller is told. That is how a card can say the words are old and also
    say why they were left alone.
    """
    now = observed_at or db.now_iso()
    metrics = metrics or {}
    reasons = reasons or {}
    safety = safety or {}
    written: set[str] = set()
    conn = db.connect()
    try:
        for ca, entry in entries.items():
            base = lexicon.mark_written(None, metrics.get(ca), now, safety=safety.get(ca))
            cur = conn.execute(
                "INSERT INTO lexicon (ca, chain_id, symbol, tag, narrative, product, social, concl,"
                " narr_text, prod_text, mech_text, pros, cons, review_state, updated_at,"
                " baseline_mcap, baseline_holders, baseline_at, baseline_safety,"
                " refresh_reason, research_state)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, 'none')"
                " ON CONFLICT(ca) DO UPDATE SET symbol=COALESCE(excluded.symbol, lexicon.symbol),"
                " tag=excluded.tag, narrative=excluded.narrative,"
                " product=excluded.product, social=excluded.social, concl=excluded.concl,"
                " narr_text=excluded.narr_text, prod_text=excluded.prod_text,"
                " mech_text=excluded.mech_text, pros=excluded.pros, cons=excluded.cons,"
                " updated_at=excluded.updated_at, baseline_mcap=excluded.baseline_mcap,"
                " baseline_holders=excluded.baseline_holders, baseline_at=excluded.baseline_at,"
                " baseline_safety=excluded.baseline_safety,"
                " refresh_reason=excluded.refresh_reason, research_state='none',"
                " research_note=NULL"
                " WHERE lexicon.review_state <> 'confirmed'",
                (ca, chain_id, entry.get("symbol"), entry.get("tag"), entry.get("narrative"),
                 entry.get("product"), entry.get("social"), entry.get("concl"),
                 entry.get("narr_text"),
                 entry.get("prod_text"), entry.get("mech_text"), entry.get("pros"),
                 entry.get("cons"), now,
                 base["baseline_mcap"], base["baseline_holders"], base["baseline_at"],
                 base["baseline_safety"], reasons.get(ca)),
            )
            if cur.rowcount:
                written.add(ca)
    finally:
        conn.close()
    return written


def prune_lexicon(chain_id: str, keep: list[str] | None = None) -> int:
    """Delete the entries that never earned a place, and return how many went.

    The entry is 45% of the score, so it has to exist before a token can be ranked - which
    means the draft stage writes one for every judged token, not only for the ones that win.
    Left at that the store only grows: 138 entries, 116 of which had never been on a report,
    and 88 of those came in through the old "every field answered 未知" pass.

    An entry survives if a human confirmed it, if it was ever selected, or if it was judged
    again this round. `keep` is that last set, passed in rather than read back because this
    runs before `run_items` for the current round is written.
    """
    live = {ca.lower() for ca in (keep or [])}
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT ca FROM lexicon WHERE chain_id = ? AND review_state <> 'confirmed'"
            " AND ca NOT IN (SELECT ca FROM run_items WHERE decision = 'selected')",
            (chain_id,),
        ).fetchall()
        doomed = [row["ca"] for row in rows if str(row["ca"]).lower() not in live]
        if not doomed:
            return 0
        marks = ",".join("?" for _ in doomed)
        conn.execute("DELETE FROM lexicon_sources WHERE ca IN (%s)" % marks, doomed)
        conn.execute("DELETE FROM lexicon WHERE ca IN (%s)" % marks, doomed)
        return len(doomed)
    finally:
        conn.close()

def write_commentary(chain: dict, picked: list, changes: list[dict], notes: list[str],
                     run_id: str, near_miss: list | None = None) -> dict[str, str]:
    """本期点评 for the picks. Empty when the model is unreachable - the cards then show
    the round's facts without a written verdict, which is a visible gap rather than a
    fabricated sentence."""
    if not picked or not llm.configured():
        return {}
    context = brief_builder.build(picked, changes, notes, near_miss)
    raw = llm.complete_json(
        [
            {"role": "system", "content": read_prompt("commentary.md")},
            {"role": "user", "content": context},
        ],
        purpose="commentary",
        run_id=run_id,
        prompt_version=PROMPT_VERSION,
    )
    issues = validate_commentary(raw, [item.ca for item in picked])
    if issues:
        raise llm.LLMUnavailable("commentary rejected: %s" % "; ".join(issues[:4]))
    return {key.lower(): value for key, value in (raw.get("per_token") or {}).items()}


#: What the card calls each kind of link. The keys are the feeds' own, so this is a display
#: map and not a parser - an unknown kind prints as itself rather than as "链接".
SOCIAL_LABELS = {"twitter": "X", "telegram": "TG", "github": "GitHub", "docs": "文档",
                 "discord": "Discord"}


def _field(source: Any, name: str) -> Any:
    """One reader for both shapes of a snapshot: a model or a dict."""
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _social_block(item, entry: dict) -> str:
    """The 社媒 section: the entry's own sentence, plus every entry point it left out.

    The writer reads these pages, so its sentence is the part worth keeping. But a sentence
    that names X and forgets the Telegram leaves the reader unable to reach it, and the round
    has already resolved those URLs - so whatever the sentence does not mention is appended
    as a bare URL, which the card renders as a link.
    """
    text = str(entry.get("social") or "").strip()
    metrics = getattr(item, "metrics", None)
    links = dict(_field(metrics, "socials") or {})
    missing = [(SOCIAL_LABELS.get(str(kind), str(kind)), url)
               for kind, url in links.items() if url and url not in text]
    missing += [("官网", url) for url in (_field(metrics, "websites") or [])
                if url and url not in text]
    if not missing:
        return text
    tail = " ｜ ".join("%s %s" % (label, url) for label, url in missing)
    return ("%s ｜ %s" % (text, tail)) if text else tail


def report_payload(item, entry: dict | None, note: str, deltas: list[dict]) -> dict:
    """The words on one card. Everything numeric comes from the snapshot, not from here.

    `rows` and `safety` are deliberately left out: the card derives them from the round's
    own facts, and recording a second copy of a number is how the two copies start to
    disagree.
    """
    entry = entry or {}
    tag = entry.get("tag") or ""
    narrative = entry.get("narrative")
    fit = "叙事贴合 %s/5" % (("%.1f" % float(narrative)).rstrip("0").rstrip(".")) if narrative is not None else ""
    bodies = {"产品完成度": entry.get("prod_text"), "机制与 dev": entry.get("mech_text"),
              "社媒": _social_block(item, entry), "优势": entry.get("pros"),
              "风险": entry.get("cons")}
    sections = {title: body for title, body in bodies.items() if body}
    return {
        "tag": tag,
        "fit_label": fit,
        "verdict": entry.get("concl") or "",
        "note": note,
        # When the words are older than the numbers beside them, the card says so. This
        # is the whole fix for a card that once printed a four-day-old market cap in its
        # verdict while the table showed today.
        "entry_note": getattr(item, "entry_note", "") or "",
        "deltas": deltas,
        "sections": sections,
    }


class _Stored:
    """The two attributes `report_payload` reads off a candidate, for a round that is over.

    Nothing is being decided here: the score, the ranks and the safety block were settled at
    publish and are read straight off the row, so the stored snapshot stands in for the
    candidate. `entry_note` is empty on purpose - it exists to say the words are older than
    the numbers beside them, and after this rewrite they are the newest words the product has.
    """

    def __init__(self, metrics: Mapping[str, Any]):
        self.metrics = metrics
        self.entry_note = ""


def _entries(conn, cas: list[str]) -> dict[str, dict]:
    marks = ",".join("?" * len(cas))
    rows = conn.execute("SELECT * FROM lexicon WHERE ca IN (%s)" % marks, cas).fetchall()
    return {str(row["ca"]).lower(): dict(row) for row in rows}


def _stored_metrics(row: Mapping[str, Any]) -> dict:
    """A snapshot as `report_payload` wants to read it.

    The round keeps a token's links in their own column rather than inside the metrics, and
    merges them back for the 社媒 block only: that block appends whatever the entry's own
    sentence forgot to name, so the links have to be there when the sentence is rewritten.
    """
    out = json.loads(row["metrics_json"] or "{}")
    social = json.loads(row["social_json"] or "{}")
    for key in ("socials", "websites"):
        if social.get(key):
            out[key] = social[key]
    return out


def restate_cards(chain_id: str, cas: Iterable[str]) -> int:
    """Give the cards of an open round the words the deep pass just wrote.

    The publish copies an entry's words onto the card, and the reading that replaces them
    finishes a minute later, so for that minute the card is the only place in the product
    still saying 市值 $98.4K ｜ 持有人 1,216 and nothing about what the thing is. The card is
    what a person reads, so the words have to travel back the way they came.

    This is the one exception to "a card does not change after it is sent", and it is drawn
    narrowly on both axes. Only rounds still at `sent` are in reach at all: a settled round is
    a record of what was known that hour. And of those, only the newest publication of the
    card. `sent` is meant to last a minute, but a round nobody drives can sit there for hours,
    and putting today's words - which carry today's numbers - onto a report from yesterday
    would leave the card arguing with the table underneath it.

    Nothing the round decided moves: the numbers, the rank rows and the safety block are read
    from the snapshot when the card is drawn, and 本期点评 and the deltas are carried over from
    the payload as published. Returns how many cards were rewritten.
    """
    wanted = [str(ca).lower() for ca in cas]
    if not wanted:
        return 0
    marks = ",".join("?" * len(wanted))
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT p.run_id AS run_id, p.ca AS ca, p.payload_json AS payload_json,"
            " p.created_at AS published_at, s.metrics_json AS metrics_json,"
            " s.social_json AS social_json"
            " FROM reports p JOIN runs r ON r.id = p.run_id"
            " LEFT JOIN snapshots s ON s.run_id = p.run_id AND s.ca = p.ca"
            " WHERE r.chain_id = ? AND r.status = ? AND p.ca IN (%s)"
            " ORDER BY p.created_at DESC" % marks,
            (chain_id, RunStatus.SENT.value, *wanted),
        ).fetchall()
        if not rows:
            return 0
        newest = rows[0]["published_at"]
        rows = [row for row in rows if row["published_at"] == newest]
        entries = _entries(conn, [str(row["ca"]).lower() for row in rows])
        rewritten = 0
        for row in rows:
            entry = entries.get(str(row["ca"]).lower())
            old = json.loads(row["payload_json"] or "{}")
            if not entry or not old:
                # The entry was pruned between the write and here. The card keeps the words
                # it has rather than losing them to a blank.
                continue
            payload = {**old, **report_payload(
                _Stored(_stored_metrics(row)), entry, old.get("note") or "",
                old.get("deltas") or [],
            )}
            if payload == old:
                continue
            conn.execute(
                "UPDATE reports SET verdict = ?, payload_json = ? WHERE run_id = ? AND ca = ?",
                (payload["verdict"], json.dumps(payload, ensure_ascii=False),
                 row["run_id"], row["ca"]),
            )
            rewritten += 1
        return rewritten
    finally:
        conn.close()
