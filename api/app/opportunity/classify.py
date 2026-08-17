"""Manual fallback: classify one material at a time as activity, project, or neither.

The scan job no longer calls this module; it uses `group.py`, which reads the whole pending
batch in one full-text call. The per-post path stays as a manual fallback for inspecting one
material. A verdict that cannot be read is recorded as `other` rather than retried - "we could
not tell" is a reading, and a pass that retried every unreadable answer would spend its budget
on the least promising material it has.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor

from ..llm import client as llm
from ..store import db
from . import model, settings
from .schema import CLASSIFY_VERSION, MaterialVerdict, read_verdict

log = logging.getLogger("crypto-hunter.opportunity")

#: The fallback pass's own cap. The scan job no longer uses it (it calls \`group.run\`, whose cap is
#: \`group.DEFAULT_LIMIT\`): a manual per-post pass that ran until it had classified everything would
#: spend a round's worth of tokens on the first wide timeline it met.
#:
#: 150 since 2026-10-19. The queue is ordered by the gate's score, and after the rebuild there were
#: ~400 posts waiting: at 60 a scan a high-scoring post could sit three scans deep (the user asked
#: why DarkHorse, found before the rebuild, was gone - it was at 148). With four workers 150
#: questions cost about two minutes, against a scan that already takes nine.
MAX_CLASSIFY = 150

#: How much of a post is sent. A post is short; the cut is here so one pathological post cannot
#: set the price of the whole pass.
POST_CHARS = model.POST_CHARS

#: 一次问几个。串行是四分钟的等待（2026-10-18 实测：一轮扫描 collect 六分钟、classify 四分钟，
#: 而"看着像卡住"说的就是这四分钟）。宽度取小值：一台机器问一个模型，天花板在服务商那一边 ——
#: 与调研池同一条理由（`report/research.py` DEFAULT_WORKERS）。
CLASSIFY_WORKERS = int(os.environ.get("CH_OPPORTUNITY_CLASSIFY_WORKERS", "4") or 4)


def classify(material: dict) -> tuple[MaterialVerdict | None, list[str]]:
    """One material, one question. Raises `LLMUnavailable` when the model cannot be reached."""
    chars = model.POST_CHARS if material.get("source_kind") == "post" else model.PAGE_CHARS
    value, issues = model.question("classify.md", CLASSIFY_VERSION, "opportunity_classify",
                                   material, read_verdict, chars=chars)
    return value if isinstance(value, MaterialVerdict) else None, issues


def run(limit: int | None = None, tell=None, workers: int = CLASSIFY_WORKERS) -> dict:
    """Classify what is waiting, newest first. Returns the pass's own counters.

    A material the model could not be reached for is left unclassified on purpose: the next
    scan will ask again, and marking it `other` would turn "we did not ask" into a judgement
    about the material - the one substitution this codebase never makes.

    **Concurrent, unlike the first cut.** Sixty questions one at a time is four minutes of a
    scan spent waiting, which is what "it looks stuck" turned out to mean (measured 2026-10-18:
    a scan's collect takes six minutes and its classify took four). The width is small on
    purpose - this is one machine asking one model, and the same reasoning as the research
    pass's own pool applies: the ceiling is the provider, not the loop.
    """
    counters = {"asked": 0, "activity": 0, "project": 0, "other": 0, "unreadable": 0,
                "issues": 0}
    materials = db.opportunity_materials(
        unclassified=True, limit=limit or settings.value("classify_per_scan", MAX_CLASSIFY))
    total = len(materials) or 1
    done = [0]

    def ask(material: dict) -> tuple[dict, object, list[str], str]:
        try:
            verdict, issues = classify(material)
        except llm.LLMUnavailable as exc:
            return material, None, [], repr(exc)
        return material, verdict, issues, ""

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        for material, verdict, issues, unavailable in pool.map(ask, materials):
            done[0] += 1
            if unavailable:
                # The model went away: everything still in flight is left unclassified, and the
                # rest of the batch is not asked. Counted, never turned into a verdict.
                log.info("opportunity: classification stopped at %s: %s", material["ref"],
                         unavailable)
                counters["unreadable"] += 1
                if tell:
                    tell(done[0], total)
                break
            counters["asked"] += 1
            counters["issues"] += len(issues)
            kind = verdict.kind if verdict else "other"
            counters["other" if kind in ("opinion", "news", "other") else kind] += 1
            db.opportunity_mark(material["ref"], material["source_kind"], classified=kind)
            # 名字要落库：它既是主题的归并键，也是溯源二次搜索的唯一输入（2026-10-19 补）。
            db.opportunity_name_put(material["ref"], getattr(verdict, "name", "") or "")
            if tell:
                tell(done[0], total)
    return counters
