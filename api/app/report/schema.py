"""The contract the LLM must satisfy; mirrors report/prompts/*.md.

Two drafts are possible, and neither is free-form: the lexicon entry carries the two
numbers that feed the 45% product block, so an entry without them could not be scored,
and the commentary carries the words that go on a card.
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
CAPS = {"concl": 500, "narr_text": 150, "prod_text": 200, "mech_text": 200,
        "pros": 100, "cons": 100}

#: 本期点评 has to carry 变化 + 对比 + 看点隐患, so its cap is wider than any single field's.
PER_TOKEN_CAP = 300


class NewTokenEntry(BaseModel):
    """A lexicon draft for a token that has no entry yet. Human confirmation is separate.

    `narrative` and `product` are the 0-5 judgements the product block is computed from.
    They are required because an entry without them cannot enter the ranking - and a
    required field the model cannot skip is cheaper than a score that silently does not
    exist.
    """

    tag: str
    narrative: float = Field(ge=0, le=5)
    product: float = Field(ge=0, le=5)
    concl: str
    narr_text: str
    prod_text: str
    mech_text: str
    pros: str
    cons: str
    social: str = ""


class LexiconDraft(BaseModel):
    entries: dict[str, NewTokenEntry] = Field(default_factory=dict)


class Commentary(BaseModel):
    per_token: dict[str, str] = Field(default_factory=dict)
    overall: str = ""
    new_tokens: dict[str, NewTokenEntry] = Field(default_factory=dict)


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
    """Problems with a lexicon draft, empty when acceptable."""
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


def validate_commentary(
    raw: dict,
    expected_cas: list[str],
    min_chars: int = 160,
    concl_min_chars: int = 160,
) -> list[str]:
    """Problems with a commentary draft, empty when acceptable. Cheap enough to run
    before storing.

    `min_chars` is the floor for the per-token 本期点评, which is the card body and has to be a
    real paragraph. `concl` is the same paragraph the lexicon draft carries, and both prompts
    ask 200-500 chars for it, so it gets the same floor as the lexicon path: a draft the
    lexicon accepts must not be rejected here. The caps are logged, not enforced - see
    `CAPS`.

    The floor sits below what the prompts ask for on purpose. 160 catches a one-line throwaway
    without turning a 180-character answer - a real, dense answer that simply ran shorter than
    asked - into a rejected draft, and a rejected draft costs a whole batch of entries, not one
    sentence."""
    try:
        draft = Commentary.model_validate(raw)
    except ValidationError as exc:
        return _schema_issues(exc)

    issues: list[str] = []
    lowered = {k.lower(): v for k, v in draft.per_token.items()}
    for ca in expected_cas:
        text = lowered.get(ca.lower())
        if not text:
            issues.append(f"missing per_token: {ca}")
        elif len(text) < min_chars:
            issues.append(f"too short: {ca} ({len(text)} < {min_chars})")
        elif len(text) > PER_TOKEN_CAP:
            log.warning("commentary %s: per_token over cap (%d > %d chars)", ca,
                        len(text), PER_TOKEN_CAP)
    for ca, entry in draft.new_tokens.items():
        if len(entry.concl) < concl_min_chars:
            issues.append(
                f"new_tokens.concl too short: {ca} "
                f"({len(entry.concl)} < {concl_min_chars})"
            )
        _log_long(ca, entry, "commentary")
    return issues
