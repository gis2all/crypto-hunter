"""The contract the LLM must satisfy; mirrors report/prompts/*.md.

The entry is not free-form: the lexicon entry carries the five 0-5 judgements the 45%
off-chain block is computed from, so an entry without them could not be scored, and the prose
beside them is what the card prints.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError

log = logging.getLogger(__name__)


#: The per-field lengths both prompts ask for. These are **not** enforcement: a draft is one
#: batch of entries, and rejecting a batch over one long field costs every entry in it - the
#: same reason the floors above sit below what the prompts ask for. A field that runs long is
#: logged and stored; the writer is what has to change, not the reader.
#:
#: `concl` is the verdict at the top of the card; the rest are its 链下调研 fold, one field
#: per block and one block per judgement, named the way the card prints them. A field here
#: with no block on the card would be prose nobody reads, and a block with no field would be a
#: heading with nothing under it - so this list and the card's blocks move together.
CAPS = {"concl": 500, "prod_text": 200, "substance_text": 200, "team_text": 200,
        "narr_text": 200, "social": 200, "badge": 16}


class NewTokenEntry(BaseModel):
    """The lexicon entry for one token: the five judgements and the prose beside them.

    The five `f_*` fields are the 0-5 judgements the off-chain block is computed from -
    产品完成度 / 项目实质与亮点 / 团队与 dev 可信度 / 叙事与官方关系 / 社区活跃度. They
    are required because an entry without them cannot enter the ranking, and a required
    field the model cannot skip is cheaper than a score that silently does not exist.
    There is no "undetermined": 0 is the judgement that the search found nothing.

    The prose fields are the card's own blocks, one per judgement, named the way the card
    prints them: `concl` is the verdict at the top, and the five under it are the 链下调研
    fold. `test_prompts.py` reads the JSON example out of every prompt and compares it with
    this model, so a field added here without its prompt - or the reverse - fails there
    instead of silently in a round.
    """

    tag: str
    f_completeness: float = Field(ge=0, le=5)
    f_substance: float = Field(ge=0, le=5)
    f_team: float = Field(ge=0, le=5)
    f_narrative: float = Field(ge=0, le=5)
    f_community: float = Field(ge=0, le=5)
    badge: str = ""
    concl: str
    prod_text: str
    substance_text: str
    team_text: str
    narr_text: str
    social: str = ""


class LexiconDraft(BaseModel):
    entries: dict[str, NewTokenEntry] = Field(default_factory=dict)


def _log_long(ca: str, entry: Any, kind: str) -> None:
    """Name the fields that ran past their cap, and do nothing else about it.

    Over-length is a writing problem, and the only thing a validator can do about it is
    throw away work that is otherwise correct. The log is where it lands.
    """
    for name, cap in CAPS.items():
        text = str(getattr(entry, name, "") or "")
        if len(text) > cap:
            log.warning("%s %s: %s over cap (%d > %d chars)", kind, ca, name,
                        len(text), cap)


def _schema_issues(exc: ValidationError) -> list[str]:
    """Every schema problem, not just the first. A draft repaired one error per model
    call is a draft that costs one call per error."""
    return [
        "schema: %s %s" % (err.get("loc"), err.get("msg")) for err in exc.errors()[:8]
    ]


def validate_lexicon(raw: dict, expected_cas: list[str], min_chars: int = 160) -> list[str]:
    """Problems with a lexicon entry, empty when acceptable."""
    try:
        draft = LexiconDraft.model_validate(raw)
    except ValidationError as exc:
        return _schema_issues(exc)

    issues: list[str] = []
    lowered = {key.lower(): entry for key, entry in draft.entries.items()}
    for ca in expected_cas:
        entry = lowered.get(ca.lower())
        if entry is None:
            issues.append("missing entry: %s" % ca)
            continue
        if len(entry.concl) < min_chars:
            issues.append("concl too short: %s (%d < %d)" % (ca, len(entry.concl), min_chars))
        if not entry.tag:
            issues.append("tag missing: %s" % ca)
        _log_long(ca, entry, "lexicon")
    return issues
